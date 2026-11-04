"""Command line entry point: ``python -m tfproxy``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional, Sequence

from .cache import CacheService
from .config import Config, add_server_options, config_from_namespace
from .errors import ProxyError
from .server import build_application, serve
from .storage.base import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tfproxy", description="Caching proxy for the Terraform module registry protocol."
    )
    sub = parser.add_subparsers(dest="command")

    add_server_options(sub.add_parser("serve", help="run the proxy (default command)"))

    add_server_options(sub.add_parser("info", help="print the resolved configuration and exit"))

    prefetch = sub.add_parser("prefetch", help="download module archives into the cache and exit")
    add_server_options(prefetch)
    prefetch.add_argument("modules", nargs="+", help="module addresses, e.g. hashicorp/consul/aws@5.0.0")

    cache = sub.add_parser("cache", help="inspect or purge the cache")
    add_server_options(cache)
    cache.add_argument("--list", action="store_true", help="list cached objects (default)")
    cache.add_argument("--purge", action="store_true", help="drop everything known about MODULE")
    cache.add_argument("module", nargs="?", help="namespace/name/provider[@version]")
    return parser


def _split_module(address: str) -> tuple[str, str, str, Optional[str]]:
    """Split ``namespace/name/provider[@version]``.

    Raises ``ValueError`` (not ``SystemExit``) so a bad address in a list of many is
    reported per address instead of aborting the whole command.
    """
    left, _, version = address.partition("@")
    parts = left.split("/")
    if len(parts) != 3:
        raise ValueError(f"module must look like namespace/name/provider[@version], got {address!r}")
    return parts[0], parts[1], parts[2], version or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "serve")
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    config = config_from_namespace(args)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if command == "serve":
        serve(config)
        return 0

    app, storage, queue = build_application(config)
    try:
        if command == "info":
            print(json.dumps(safe_config(config), indent=2, sort_keys=True))
            return 0
        if command == "prefetch":
            return cmd_prefetch(app.cache, args.modules)
        if command == "cache":
            return cmd_cache(app.cache, storage, args)
        parser.error(f"unknown command {command!r}")  # pragma: no cover
        return 2
    finally:
        queue.shutdown(wait=True)


def safe_config(config: Config) -> dict:
    """Configuration summary without any secret material."""
    data = {
        "listen": f"{config.listen_host}:{config.listen_port}",
        "upstream_url": config.upstream_url,
        "external_base_url": config.external_base_url or "(from Host header)",
        "backends": config.backends,
        "cache_dir": config.cache_dir,
        "ttl_versions": config.ttl_versions,
        "ttl_download": config.ttl_download,
        "stale_revalidate": config.stale_revalidate,
        "prefetch_latest": config.prefetch_latest,
        "max_archive_mb": config.max_archive_mb,
        "workers": config.workers,
        "tls": bool(config.tls_cert),
        "admin_token": "set" if config.admin_token else "unset",
    }
    if config.s3:
        data["s3"] = {
            "bucket": config.s3.bucket,
            "region": config.s3.region,
            "endpoint": config.s3.endpoint_url or "aws",
            "prefix": config.s3.prefix,
            "credentials": "set" if config.s3.access_key else "absent",
            "path_style": config.s3.resolved_path_style(),
        }
    return data


def cmd_prefetch(cache: CacheService, addresses: list[str]) -> int:
    from .models import ModuleRef
    from .semver import newest, versions_from_payload

    code = 0
    for address in addresses:
        try:
            namespace, name, provider, version = _split_module(address)
            ref = ModuleRef.build(namespace, name, provider, version)
        except (ValueError, ProxyError) as exc:
            print(f"{address}: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
            continue
        try:
            if not version:
                document = cache.versions(ref)
                version = newest(versions_from_payload(document.json))
                if not version:
                    print(f"{ref.module_id}: upstream has no versions", file=sys.stderr)
                    code = 1
                    continue
                ref = ModuleRef.build(namespace, name, provider, version)
            meta = cache.ensure_archive(ref)
            print(f"{ref.module_id}@{version}: {meta.size} bytes sha256={meta.sha256} [{meta.backend}]")
        except Exception as exc:
            print(f"{ref.module_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
    return code


def cmd_cache(cache: CacheService, storage: Storage, args: argparse.Namespace) -> int:
    from .models import ModuleRef

    if args.module:
        try:
            namespace, name, provider, version = _split_module(args.module)
            ref = ModuleRef.build(namespace, name, provider, version)
        except (ValueError, ProxyError) as exc:
            print(f"{args.module}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if args.purge:
            print(json.dumps({"purged": cache.purge(ref)}, indent=2))
            return 0
        if version:
            meta = cache.archive_meta(ref)
            if meta is None:
                print(f"{ref.module_id}@{version}: not cached", file=sys.stderr)
                return 1
            print(json.dumps(meta.__dict__, indent=2, default=str))
            return 0
        print(json.dumps({"versions": cache.cached_modules(ref.meta_prefix())}, indent=2))
        return 0

    keys = cache.cached_modules()
    for key in keys:
        meta = storage.stat(key)
        print(f"{meta.size if meta else '?'}\t{key}")
    if not keys:
        print("(cache is empty)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
