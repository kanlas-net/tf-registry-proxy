"""HTTP layer: routing, registry protocol responses and a stdlib server."""

from __future__ import annotations

import hmac
import json
import logging
import re
import signal
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Optional
from urllib.parse import unquote, urlsplit

from .cache import CacheService
from .config import Config
from .errors import BadRequest, NotFound, ProxyError, Unauthorized
from .locking import LockManager, NullLockManager
from .models import ModuleRef
from .storage.base import Storage
from .upstream import X_TERRAFORM_GET
from .workers import BackgroundQueue

JSON_TYPE = "application/json"
MAX_ADMIN_BODY = 64 * 1024
_START = time.time()

_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/\.well-known/terraform\.json/?$"), "discovery"),
    ("GET", re.compile(r"^/healthz/?$"), "health"),
    ("GET", re.compile(r"^/metrics/?$"), "metrics"),
    ("GET", re.compile(r"^/?$"), "index"),
    ("GET", re.compile(r"^/v1/modules/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/versions/?$"), "versions"),
    (
        "GET",
        re.compile(r"^/v1/modules/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)/download/?$"),
        "download",
    ),
    ("GET", re.compile(r"^/archive/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)\.zip$"), "archive"),
    ("GET", re.compile(r"^/api/v1/cache/?$"), "cache_list"),
    ("POST", re.compile(r"^/api/v1/prefetch/?$"), "prefetch"),
    (
        "DELETE",
        re.compile(r"^/api/v1/cache/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/(?P<ver>[^/]+)/?$"),
        "purge_version",
    ),
    ("DELETE", re.compile(r"^/api/v1/cache/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<prov>[^/]+)/?$"), "purge_module"),
]


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    stream: Optional[Iterator[bytes]] = None
    content_length: Optional[int] = None

    def json(self) -> dict:
        return json.loads((self.body or b"{}").decode("utf-8"))

    @property
    def length(self) -> Optional[int]:
        if self.stream is not None:
            return self.content_length
        if self.body is not None:
            return len(self.body)
        return self.content_length


def error_body(code: str, message: str) -> bytes:
    return json.dumps({"errors": [{"code": code, "message": message}]}, ensure_ascii=False).encode("utf-8")


class ProxyApplication:
    """All request handling, independent of the socket layer (easy to unit test)."""

    def __init__(
        self,
        config: Config,
        cache: CacheService,
        storage: Storage,
        queue: BackgroundQueue,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.storage = storage
        self.queue = queue
        self.logger = logger or logging.getLogger("tfproxy.http")

    # ---- entry point -------------------------------------------------------
    def handle(
        self,
        method: str,
        path: str,
        query: str = "",
        headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> Response:
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        method = method.upper()
        path = path or "/"
        head_request = method == "HEAD"
        probe = "GET" if head_request else method

        for name, pattern in [(n, p) for verb, p, n in _ROUTES if verb == probe]:
            match = pattern.match(path)
            if not match:
                continue
            # Route on the raw path and decode each captured segment afterwards: decoding
            # first would let %2F, %3F or %23 rewrite the route itself, and a decoded
            # separator or '?' inside a segment must reach validation as a plain bad id.
            params = {key: unquote(value) for key, value in match.groupdict().items()}
            try:
                response = getattr(self, f"_on_{name}")(params, headers, body, query, method)
            except ProxyError as exc:
                response = self._error(exc.status, exc.code, exc.message, exc.headers)
            except Exception as exc:  # pragma: no cover - last line of defence
                self.logger.exception("unhandled error on %s %s", method, path)
                response = self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")
            if head_request:
                if response.content_length is None and response.body is not None:
                    response.content_length = len(response.body)
                response.body = None
                response.stream = None
            return response
        return self._error(404, "no_route", f"no handler for {method} {path}")

    # ---- discovery / ops ---------------------------------------------------
    def _on_discovery(self, _params, _headers, _body, _query, _method) -> Response:
        return self._json(200, {"modules.v1": "/v1/modules/"}, {"Cache-Control": "public, max-age=3600"})

    def _on_index(self, _params, _headers, _body, _query, _method) -> Response:
        return self._json(
            200,
            {
                "name": "tf-registry-proxy",
                "modules.v1": self.config.public_base() + "/v1/modules/",
                "backends": self.storage.describe(),
            },
        )

    def _on_health(self, _params, _headers, _body, _query, _method) -> Response:
        stats = self.queue.stats()
        return self._json(
            200,
            {
                "status": "ok",
                "backends": self.storage.describe(),
                "queued": stats["queued"],
                "active": stats["active"],
                "instance": self.cache.locks.instance,
                "locks": self.cache.locks.stats(),
            },
        )

    def _on_metrics(self, _params, _headers, _body, _query, _method) -> Response:
        lines = [f"tfproxy_{name} {value}" for name, value in sorted(self.cache.metrics().items())]
        lines.append(f"tfproxy_uptime_seconds {int(self.cache.clock() - _START)}")
        return Response(
            status=200,
            headers={"Content-Type": "text/plain; version=0.0.4"},
            body="\n".join(lines).encode("utf-8"),
        )

    # ---- registry protocol -------------------------------------------------
    def _on_versions(self, params, _headers, _body, _query, _method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"])
        document = self.cache.versions(ref)
        return Response(
            status=200,
            headers={
                "Content-Type": JSON_TYPE,
                "Cache-Control": f"public, max-age={self.config.ttl_versions}",
                "X-TFP-Cache": document.source,
                "X-TFP-Fetched-At": f"{document.fetched_at:.3f}",
                "X-TFP-Age": f"{document.age:.0f}",
            },
            body=document.payload,
        )

    def _on_download(self, params, headers, _body, _query, _method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        info = self.cache.download(ref)
        if info.cacheable:
            base = self.config.public_base(headers.get("host", ""), _scheme(headers, self.config))
            subdir = f"//{info.subdir}" if info.subdir else ""
            getter = f"{base}/archive/{ref.namespace}/{ref.name}/{ref.provider}/{ref.version}.zip{subdir}"
            self.cache.schedule_archive(ref)
        else:
            getter = info.raw
            self.logger.info("%s@%s has a non-HTTP source, passing it through", ref.module_id, ref.version)
        return Response(
            status=204,
            headers={
                X_TERRAFORM_GET: getter,
                "X-TFP-Cache": info.source,
                "X-TFP-Source": "proxy" if info.cacheable else "upstream",
            },
        )

    def _on_archive(self, params, _headers, _body, _query, method) -> Response:
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        meta = self.cache.archive_meta(ref)
        source = "hit" if meta is not None else "miss"
        if meta is None:
            # The background job may still be running (or this client got here first):
            # fall back to a synchronous fetch instead of answering 404.
            meta = self.cache.ensure_archive(ref)
        try:
            handle = self.cache.open_archive(ref)
        except NotFound:
            # Purged or evicted between the stat and the open: fetch it once, then fail.
            self.cache.ensure_archive(ref)
            handle = self.cache.open_archive(ref)
        size = handle.size or (meta.size if meta else None) or 0
        headers = {
            "Content-Type": "application/zip",
            "X-TFP-Cache": source,
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        if meta and meta.sha256:
            headers["ETag"] = f'"{meta.sha256}"'
        if method == "HEAD":
            handle.close()
            return Response(status=200, headers=headers, content_length=size)
        return Response(status=200, headers=headers, stream=_closing_stream(handle), content_length=size)

    # ---- admin API ---------------------------------------------------------
    def _require_admin(self, headers: dict[str, str]) -> None:
        token = self.config.admin_token
        if not token:
            return
        supplied = headers.get("x-admin-token") or headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, token):
            raise Unauthorized()

    def _on_cache_list(self, _params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        objects = self.cache.cached_modules()
        return self._json(
            200,
            {
                "objects": len(objects),
                "modules": sorted(
                    {"/".join(key.split("/")[1:4]) for key in objects if key.startswith("modules/")}
                ),
                "keys": objects,
                "metrics": self.cache.metrics(),
                "jobs": self.queue.stats(),
                "backends": self.storage.describe(),
            },
        )

    def _on_prefetch(self, _params, headers, body, _query, _method) -> Response:
        self._require_admin(headers)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise BadRequest("request body must be a JSON object")
        ref = ModuleRef.build(
            str(payload.get("namespace", "")), str(payload.get("name", "")), str(payload.get("provider", ""))
        )
        versions = payload.get("versions")
        if isinstance(versions, str):
            versions = [versions]
        if not versions:
            document = self.cache.versions(ref)
            from .semver import newest, versions_from_payload

            latest = newest(versions_from_payload(document.json))
            versions = [latest] if latest else []
        scheduled, cached, queued = [], [], []
        for version in versions:
            one = ModuleRef.build(ref.namespace, ref.name, ref.provider, str(version))
            if self.cache.has_archive(one):
                cached.append(str(version))
            elif self.cache.schedule_archive(one):
                scheduled.append(str(version))
            else:
                queued.append(str(version))
        return self._json(
            202,
            {"module": ref.module_id, "scheduled": scheduled, "already_cached": cached, "already_queued": queued},
        )

    def _on_purge_module(self, params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"])
        return self._json(200, {"removed": self.cache.purge(ref)})

    def _on_purge_version(self, params, headers, _body, _query, _method) -> Response:
        self._require_admin(headers)
        ref = ModuleRef.build(params["ns"], params["name"], params["prov"], params["ver"])
        return self._json(200, {"removed": self.cache.purge(ref)})

    # ---- helpers -----------------------------------------------------------
    def _json(self, status: int, payload: dict, headers: Optional[dict[str, str]] = None) -> Response:
        merged = {"Content-Type": JSON_TYPE}
        merged.update(headers or {})
        return Response(status=status, headers=merged, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, code: str, message: str, headers: Optional[dict[str, str]] = None) -> Response:
        merged = {"Content-Type": JSON_TYPE, "Cache-Control": "no-store"}
        merged.update(headers or {})
        return Response(
            status=status,
            headers=merged,
            body=error_body(code, message),
        )


def _closing_stream(handle) -> Iterator[bytes]:
    try:
        yield from handle.chunks()
    finally:
        handle.close()


def _scheme(headers: dict[str, str], config: Config) -> str:
    forwarded = headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return "https" if config.tls_cert else "http"


# --------------------------------------------------------------------------
# Socket server
# --------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "tf-registry-proxy"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: ProxyApplication

    def log_message(self, fmt: str, *args) -> None:  # route to logging, not stderr
        self.app.logger.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _read_body(self, limit: int = MAX_ADMIN_BODY) -> bytes:
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        try:
            wanted = int(raw)
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        if wanted > limit:
            raise BadRequest(f"request body larger than {limit} bytes")
        return self.rfile.read(wanted)

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        headers = {key: value for key, value in self.headers.items()}
        try:
            body = self._read_body() if method in ("POST", "PUT", "PATCH") else b""
            response = self.app.handle(method, parts.path, parts.query, headers, body)
        except ProxyError as exc:
            headers = {"Content-Type": JSON_TYPE, "Cache-Control": "no-store"}
            headers.update(getattr(exc, "headers", {}) or {})
            response = Response(status=exc.status, headers=headers, body=error_body(exc.code, exc.message))
        except Exception as exc:  # pragma: no cover - defensive
            self.app.logger.exception("transport error")
            response = Response(
                status=500,
                headers={"Content-Type": JSON_TYPE},
                body=error_body("internal_error", f"{type(exc).__name__}: {exc}"),
            )
        self._send(response, head=(method == "HEAD"))

    def _send(self, response: Response, head: bool = False) -> None:
        body = response.body or b""
        length = response.length
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if response.status not in (204, 304):
            if length is None:
                # Size unknown: frame the body as real HTTP chunks and close the socket
                # afterwards, so a client that stops reading cannot wedge keep-alive.
                self.send_header("Transfer-Encoding", "chunked")
                self.close_connection = True
            else:
                self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head:
            return
        try:
            if response.stream is not None:
                if length is None:
                    for chunk in response.stream:
                        if chunk:
                            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    for chunk in response.stream:
                        self.wfile.write(chunk)
            elif body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client hung up
            self.close_connection = True


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: ProxyApplication) -> None:
        self.app = app
        _Handler.app = app
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address) -> None:  # pragma: no cover - noise filter
        import traceback

        self.app.logger.warning("connection from %s failed: %s", client_address, traceback.format_exc(limit=2))

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="tfproxy-http", daemon=True)
        thread.start()
        return thread

    @property
    def port(self) -> int:
        return self.server_address[1]


def build_application(config: Config) -> tuple[ProxyApplication, Storage, BackgroundQueue]:
    """Wire storage + upstream client + background queue into an application."""
    from .storage import build_storage
    from .upstream import ArchiveDownloader, RegistryClient

    logger = logging.getLogger("tfproxy")
    storage = build_storage(config)
    registry = RegistryClient(
        config.upstream_url,
        request_timeout=config.request_timeout,
        user_agent=config.user_agent,
    )
    downloader = ArchiveDownloader(
        download_timeout=config.download_timeout,
        max_bytes=config.max_archive_bytes,
        user_agent=config.user_agent,
        tmp_dir=config.cache_dir,
    )
    queue = BackgroundQueue(workers=config.workers, maxsize=config.queue_max, logger=logger.getChild("background"))
    queue.start()
    if config.locks_enabled:
        locks: LockManager = LockManager(
            storage, ttl=config.lock_ttl, clock=time.time, logger=logger.getChild("locks")
        )
    else:
        locks = NullLockManager(ttl=config.lock_ttl, logger=logger.getChild("locks"))
    cache = CacheService(
        config=config, storage=storage, registry=registry, downloader=downloader, queue=queue, locks=locks
    )
    app = ProxyApplication(config, cache, storage, queue)
    return app, storage, queue


def make_server(config: Config, app: Optional[ProxyApplication] = None) -> ProxyServer:
    if app is None:
        app, _storage, _queue = build_application(config)
    server = ProxyServer((config.listen_host, config.listen_port), app)
    if config.tls_cert and config.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(config: Config) -> None:
    """Run until SIGINT/SIGTERM, then shut the worker threads down cleanly."""
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app, storage, queue = build_application(config)
    server = make_server(config, app)
    log = logging.getLogger("tfproxy")
    log.info(
        "tf-registry-proxy on %s://%s:%s -> upstream %s, backends %s",
        "https" if config.tls_cert else "http",
        config.listen_host,
        server.port,
        config.upstream_url,
        storage.describe(),
    )
    server.serve_in_thread()
    stop = threading.Event()

    def _shutdown(signum, _frame):  # pragma: no cover - signal path
        log.info("signal %s, shutting down", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _shutdown)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass
    try:
        stop.wait()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.shutdown()
        server.server_close()
        queue.shutdown(wait=True)
        log.info("stopped")
