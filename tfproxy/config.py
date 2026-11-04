"""Configuration: command line arguments and ``TFP_*`` environment variables."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .errors import ConfigError

_BACKEND_RE = re.compile(r"^(fs|s3)$")
DEFAULT_UPSTREAM = "https://registry.terraform.io"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    # A typo like TFP_LOCKS=flase must not silently switch the feature off.
    raise ConfigError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {raw!r}")


@dataclass
class S3Config:
    bucket: str
    region: str = "us-east-1"
    endpoint_url: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    session_token: Optional[str] = None
    prefix: str = "tfregistry"
    #: ``None`` = auto: path style for custom endpoints, virtual-host style for AWS.
    path_style: Optional[bool] = None
    #: Use ``UNSIGNED-PAYLOAD`` instead of hashing the body (only valid over HTTPS).
    unsigned_payload: bool = False
    #: Send ``If-None-Match: *`` when creating lock objects (AWS, MinIO, R2 support it).
    conditional_locks: bool = False
    #: Second read-back delay used by the lock create handshake when conditional
    #: writes are not available.
    lock_verify_delay: float = 0.25

    def resolved_path_style(self) -> bool:
        if self.path_style is not None:
            return self.path_style
        if not self.endpoint_url:
            return False
        host = _host_of(self.endpoint_url)
        return not host.endswith("amazonaws.com")

    def validate(self) -> None:
        if not self.bucket:
            raise ConfigError("S3 backend selected but no bucket configured (TFP_S3_BUCKET)")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,62}$", self.bucket):
            raise ConfigError(f"invalid S3 bucket name: {self.bucket!r}")
        if not self.region:
            raise ConfigError("S3 backend selected but no region configured (TFP_S3_REGION)")
        if self.endpoint_url and not self.endpoint_url.startswith(("http://", "https://")):
            raise ConfigError("TFP_S3_ENDPOINT must start with http:// or https://")
        if bool(self.access_key) != bool(self.secret_key):
            raise ConfigError("S3 credentials must come as a pair (access key + secret key)")


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    #: Base URL of the upstream registry (no trailing slash).
    upstream_url: str = DEFAULT_UPSTREAM
    #: Public base URL of *this* proxy; used to build absolute X-Terraform-Get URLs.
    #: When empty it is derived from the request ``Host`` header.
    external_base_url: str = ""

    backends: list[str] = field(default_factory=lambda: ["fs"])
    cache_dir: str = "./cache"

    #: Seconds a version list may be served from cache before revalidation.
    ttl_versions: int = 3600
    #: Seconds a resolved download source may be cached (versions are immutable).
    ttl_download: int = 604800
    #: Serve a stale cached document while refreshing it in the background.
    stale_revalidate: bool = True

    #: Background prefetch when a client asks for the version list.
    prefetch_latest: bool = True

    max_archive_mb: int = 512
    #: Socket timeout for the small registry JSON calls (versions, download).
    request_timeout: float = 10.0
    #: Socket timeout while streaming an archive (per socket read, not total).
    download_timeout: float = 60.0
    user_agent: str = "tf-registry-proxy"

    workers: int = 4
    queue_max: int = 2000
    #: Copy objects fetched from S3 into the local disk cache on the fly.
    promote_to_fs: bool = True

    #: Distributed download locks (needed when several instances share one bucket).
    locks_enabled: bool = True
    #: Seconds before a lock left behind by a killed process may be broken.
    lock_ttl: int = 900
    #: Seconds a blocking request waits for another instance to finish a download.
    lock_wait: int = 120

    admin_token: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    log_level: str = "INFO"

    s3: Optional[S3Config] = None

    # ---- derived -----------------------------------------------------------
    @property
    def max_archive_bytes(self) -> int:
        return self.max_archive_mb * 1024 * 1024

    def validate(self) -> "Config":
        if self.ttl_versions <= 0 or self.ttl_download <= 0:
            raise ConfigError("TTLs must be positive")
        if not 0 <= self.listen_port <= 65535:
            raise ConfigError("listen port out of range (0 means 'pick a free port')")
        if self.workers < 1:
            raise ConfigError("at least one background worker is required")
        if self.max_archive_mb < 1:
            raise ConfigError("max archive size must be at least 1 MiB")
        if not self.backends:
            raise ConfigError("at least one storage backend is required")
        unknown = [b for b in self.backends if not _BACKEND_RE.match(b)]
        if unknown:
            raise ConfigError(f"unknown storage backend(s): {', '.join(unknown)}")
        if len(set(self.backends)) != len(self.backends):
            raise ConfigError("storage backends must be unique")
        if "s3" in self.backends:
            if self.s3 is None:
                raise ConfigError("backend 's3' selected but S3 is not configured")
            self.s3.validate()
        if self.external_base_url and not self.external_base_url.startswith(("http://", "https://")):
            raise ConfigError("external base URL must start with http:// or https://")
        if self.lock_ttl <= 0:
            raise ConfigError("lock TTL must be positive")
        if self.lock_wait < 0:
            raise ConfigError("lock wait must be zero or positive")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ConfigError("TLS needs both a certificate and a key")
        return self

    def public_base(self, request_host: str = "", scheme: str = "http") -> str:
        """Base URL used in generated ``X-Terraform-Get`` values."""
        if self.external_base_url:
            return self.external_base_url.rstrip("/")
        if request_host:
            return f"{scheme}://{request_host}"
        host = self.listen_host if self.listen_host != "0.0.0.0" else "127.0.0.1"
        return f"{'https' if self.tls_cert else scheme}://{host}:{self.listen_port}"

    # ---- factories ---------------------------------------------------------
    @classmethod
    def from_env(cls, environ: Optional[dict[str, str]] = None) -> "Config":
        """Build a config from the environment.

        With an explicit *environ* the process environment is swapped for its lifetime;
        that is meant for the single-threaded startup path and tests, not for hot reload
        while workers are running.
        """
        if environ is not None:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(environ)
            try:
                return cls._from_env()
            finally:
                os.environ.clear()
                os.environ.update(saved)
        return cls._from_env()

    @classmethod
    def _from_env(cls) -> "Config":
        backends = [b.strip() for b in (_env("TFP_BACKENDS", "fs") or "fs").split(",") if b.strip()]
        s3: Optional[S3Config] = None
        if "s3" in backends:
            s3 = S3Config(
                bucket=_env("TFP_S3_BUCKET", "") or "",
                region=_env("TFP_S3_REGION", "us-east-1") or "us-east-1",
                endpoint_url=_env("TFP_S3_ENDPOINT"),
                access_key=_env("TFP_S3_ACCESS_KEY") or _env("AWS_ACCESS_KEY_ID"),
                secret_key=_env("TFP_S3_SECRET_KEY") or _env("AWS_SECRET_ACCESS_KEY"),
                session_token=_env("TFP_S3_SESSION_TOKEN") or _env("AWS_SESSION_TOKEN"),
                prefix=(_env("TFP_S3_PREFIX", "tfregistry") or "tfregistry").strip("/"),
                path_style=_env_bool("TFP_S3_PATH_STYLE", False) if _env("TFP_S3_PATH_STYLE") else None,
                unsigned_payload=_env_bool("TFP_S3_UNSIGNED_PAYLOAD", False),
                conditional_locks=_env_bool("TFP_S3_CONDITIONAL_LOCKS", False),
                lock_verify_delay=float(_env_int("TFP_S3_LOCK_VERIFY_MS", 250)) / 1000.0,
            )
        return cls(
            listen_host=_env("TFP_HOST", "0.0.0.0") or "0.0.0.0",
            listen_port=_env_int("TFP_PORT", 8080),
            upstream_url=(_env("TFP_UPSTREAM_URL", DEFAULT_UPSTREAM) or DEFAULT_UPSTREAM).rstrip("/"),
            external_base_url=(_env("TFP_EXTERNAL_URL", "") or "").rstrip("/"),
            backends=backends,
            cache_dir=_env("TFP_CACHE_DIR", "./cache") or "./cache",
            ttl_versions=_env_int("TFP_TTL_VERSIONS", 3600),
            ttl_download=_env_int("TFP_TTL_DOWNLOAD", 604800),
            stale_revalidate=_env_bool("TFP_STALE_REVALIDATE", True),
            prefetch_latest=_env_bool("TFP_PREFETCH_LATEST", True),
            max_archive_mb=_env_int("TFP_MAX_ARCHIVE_MB", 512),
            request_timeout=float(_env_int("TFP_REQUEST_TIMEOUT", 10)),
            download_timeout=float(_env_int("TFP_DOWNLOAD_TIMEOUT", 60)),
            workers=_env_int("TFP_WORKERS", 4),
            queue_max=_env_int("TFP_QUEUE_MAX", 2000),
            promote_to_fs=_env_bool("TFP_PROMOTE_TO_FS", True),
            locks_enabled=_env_bool("TFP_LOCKS", True),
            lock_ttl=_env_int("TFP_LOCK_TTL", 900),
            lock_wait=_env_int("TFP_LOCK_WAIT", 120),
            admin_token=_env("TFP_ADMIN_TOKEN", "") or "",
            tls_cert=_env("TFP_TLS_CERT", "") or "",
            tls_key=_env("TFP_TLS_KEY", "") or "",
            log_level=_env("TFP_LOG_LEVEL", "INFO") or "INFO",
            s3=s3,
        ).validate()

    @classmethod
    def from_args(cls, argv: Optional[Sequence[str]] = None) -> "Config":
        parser = build_arg_parser()
        return config_from_namespace(parser.parse_args(argv))


def add_server_options(parser: argparse.ArgumentParser) -> None:
    """Add every server option to *parser* (shared by the CLI and its subcommands)."""
    parser.add_argument("--host", dest="listen_host", default=None, help="bind address (TFP_HOST)")
    parser.add_argument("--port", dest="listen_port", type=int, default=None, help="bind port (TFP_PORT)")
    parser.add_argument("--upstream", dest="upstream_url", default=None,
                        help="upstream registry base URL (TFP_UPSTREAM_URL)")
    parser.add_argument("--external-url", dest="external_base_url", default=None,
                        help="public base URL of this proxy (TFP_EXTERNAL_URL)")
    parser.add_argument("--backend", default=None, help="comma separated backends: fs, s3 or fs,s3 (TFP_BACKENDS)")
    parser.add_argument("--cache-dir", dest="cache_dir", default=None, help="directory for the filesystem backend")
    parser.add_argument("--ttl-versions", dest="ttl_versions", type=int, default=None,
                        help="version list TTL in seconds (TFP_TTL_VERSIONS)")
    parser.add_argument("--ttl-download", dest="ttl_download", type=int, default=None,
                        help="download metadata TTL in seconds (TFP_TTL_DOWNLOAD)")
    parser.add_argument("--no-stale-revalidate", dest="no_stale_revalidate", action="store_true",
                        help="never answer from a stale cache entry")
    parser.add_argument("--no-prefetch", dest="no_prefetch", action="store_true",
                        help="do not prefetch archives when a client lists versions")
    parser.add_argument("--max-archive-mb", dest="max_archive_mb", type=int, default=None,
                        help="hard size limit per archive (TFP_MAX_ARCHIVE_MB)")
    parser.add_argument("--request-timeout", dest="request_timeout", type=float, default=None,
                        help="socket timeout for registry JSON calls (TFP_REQUEST_TIMEOUT)")
    parser.add_argument("--download-timeout", dest="download_timeout", type=float, default=None,
                        help="socket timeout while streaming an archive (TFP_DOWNLOAD_TIMEOUT)")
    parser.add_argument("--workers", dest="workers", type=int, default=None, help="background download workers")
    parser.add_argument("--queue-max", dest="queue_max", type=int, default=None, help="background queue size")
    parser.add_argument("--lock-ttl", dest="lock_ttl", type=int, default=None,
                        help="seconds before a stale download lock may be broken (TFP_LOCK_TTL)")
    parser.add_argument("--lock-wait", dest="lock_wait", type=int, default=None,
                        help="seconds to wait for another instance to finish a download (TFP_LOCK_WAIT)")
    parser.add_argument("--no-locks", dest="no_locks", action="store_true",
                        help="disable distributed download locks (single instance only)")
    parser.add_argument("--admin-token", dest="admin_token", default=None,
                        help="token required by /api/v1/* (TFP_ADMIN_TOKEN)")
    parser.add_argument("--tls-cert", dest="tls_cert", default=None, help="PEM certificate for HTTPS")
    parser.add_argument("--tls-key", dest="tls_key", default=None, help="PEM key for HTTPS")
    parser.add_argument("--log-level", dest="log_level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--s3-bucket", dest="s3_bucket", default=None)
    parser.add_argument("--s3-region", dest="s3_region", default=None)
    parser.add_argument("--s3-endpoint", dest="s3_endpoint", default=None)
    parser.add_argument("--s3-access-key", dest="s3_access_key", default=None)
    parser.add_argument("--s3-secret-key", dest="s3_secret_key", default=None)
    parser.add_argument("--s3-prefix", dest="s3_prefix", default=None)
    parser.add_argument("--s3-path-style", dest="s3_path_style", action="store_true")


_SIMPLE_ATTRS = (
    "listen_host",
    "listen_port",
    "upstream_url",
    "external_base_url",
    "cache_dir",
    "ttl_versions",
    "ttl_download",
    "max_archive_mb",
    "request_timeout",
    "download_timeout",
    "workers",
    "queue_max",
    "admin_token",
    "lock_ttl",
    "lock_wait",
    "tls_cert",
    "tls_key",
    "log_level",
)


def config_from_namespace(namespace: argparse.Namespace, base: Optional[Config] = None) -> Config:
    """Build a :class:`Config` from parsed CLI options on top of the environment."""
    cfg = base or Config.from_env()
    for attr in _SIMPLE_ATTRS:
        value = getattr(namespace, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    backend = getattr(namespace, "backend", None)
    if backend:
        cfg.backends = [b.strip() for b in backend.split(",") if b.strip()]
    if getattr(namespace, "no_stale_revalidate", False):
        cfg.stale_revalidate = False
    if getattr(namespace, "no_prefetch", False):
        cfg.prefetch_latest = False
    if getattr(namespace, "no_locks", False):
        cfg.locks_enabled = False
    if "s3" in cfg.backends:
        s3 = cfg.s3 or S3Config(bucket="")
        for dest, attr in (
            ("s3_bucket", "bucket"),
            ("s3_region", "region"),
            ("s3_endpoint", "endpoint_url"),
            ("s3_access_key", "access_key"),
            ("s3_secret_key", "secret_key"),
            ("s3_prefix", "prefix"),
        ):
            value = getattr(namespace, dest, None)
            if value is not None:
                setattr(s3, attr, value)
        if getattr(namespace, "s3_path_style", False):
            s3.path_style = True
        cfg.s3 = s3
    cfg.upstream_url = cfg.upstream_url.rstrip("/")
    cfg.external_base_url = cfg.external_base_url.rstrip("/")
    if cfg.s3 is not None:
        cfg.s3.prefix = str(cfg.s3.prefix or "").strip("/")
    return cfg.validate()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tfproxy",
        description="Caching reverse proxy for the Terraform module registry protocol.",
    )
    add_server_options(parser)
    return parser
