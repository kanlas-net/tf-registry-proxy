# Changelog

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning is
[SemVer](https://semver.org/). A release is marked by a git tag `vX.Y.Z`, which is also what
publishes the image to `ghcr.io`.

## [Unreleased]

### Added

- **Readiness and startup checks.** `GET /readyz` probes every storage backend (a
  `ListObjectsV2` plus a small write/read/delete round trip) and answers `503` with the reason
  while a bucket is unreachable; `GET /healthz` stays a cheap liveness check that never touches the
  network. The same probes run once at startup, and `tfproxy healthcheck` runs them from the
  command line (exit 1 when broken). New settings: `TFP_HEALTH_PROBE_WRITE`, `TFP_HEALTH_CACHE_TTL`,
  `TFP_HEALTH_TIMEOUT`, `TFP_STARTUP_CHECK`, `TFP_STARTUP_CHECK_RETRIES`, `TFP_STARTUP_CHECK_INTERVAL`,
  `TFP_STARTUP_REQUIRED`, `TFP_READY_STRICT`, `TFP_READY_UPSTREAM`.
- **Prometheus metrics.** `/metrics` is now a real registry (`tfproxy/metrics.py`, still standard
  library only) with counters, gauges and histograms:
  `tfproxy_upstream_errors_total` / `tfproxy_upstream_consecutive_failures` /
  `tfproxy_upstream_last_success_timestamp_seconds` for alerting on the upstream registry,
  `tfproxy_archive_pending_modules` / `tfproxy_archive_pending_oldest_seconds` /
  `tfproxy_archive_pending_age_seconds` for "clients asked and the archive never arrived",
  plus HTTP latency and error counters, storage probe results, queue and lock gauges and process
  stats. The old `tfproxy_*` cache counters are exported under the same names.
- `GET /api/v1/pending`: which module versions are still waiting for their archive, how long and
  with which error.
- `examples/kubernetes.yaml` (Deployment + Service with startup/readiness/liveness probes) and
  `examples/prometheus-alerts.yaml` (ready-made alert rules).

### Changed

- `CacheService.metrics()` (the counter snapshot served by `/api/v1/cache`) is now
  `CacheService.counters()`; the `metrics` attribute holds the Prometheus bundle, which is what
  `ProxyApplication.metrics` holds too.

### Fixed

- Re-tagging a version no longer leaves the release unpublished. Deleting a tag turns its release
  into a draft instead of deleting it, and the release job skipped every release it could still
  see — including that invisible draft. The job now publishes a leftover draft with
  `gh release edit --draft=false`.

## [1.0.0] - 2026-11-04

First public release. The Terraform module protocol (registry protocol v1) is implemented in
full, backends are a local directory and an S3-compatible storage.

### Added

- `GET /.well-known/terraform.json`, `/v1/modules/.../versions`,
  `/v1/modules/.../<version>/download`, `/archive/...zip` — `terraform init` keeps working
  through the proxy unchanged.
- TTL document cache, `stale-while-revalidate`, `stale-on-error`.
- Background archive download with deduplication: the client gets `204` + `X-Terraform-Get`
  right away.
- The `fs`, `s3` and combined (`fs,s3`) backends: read fs → s3, write to both, warm the disk
  cache on an S3 hit.
- Distributed lock files in S3 for several instances on one bucket, with a TTL and take-over of
  an expired lock; conditional `If-None-Match: *` under `TFP_S3_CONDITIONAL_LOCKS`.
- An S3 V4 signature on the standard library (`tfproxy/sigv4.py`), verified against
  `botocore.auth.SigV4Auth` vectors.
- `/healthz`, `/metrics` (Prometheus text format), the administrative `/api/v1/cache` and
  `/api/v1/prefetch` guarded by `TFP_ADMIN_TOKEN`.
- The `tfproxy serve|prefetch|cache|info` CLI, HTTPS directly via `TFP_TLS_CERT`/`TFP_TLS_KEY`.
- CI (tests on Python 3.10–3.13, image build, smoke test) and multi-arch image publishing
  (`linux/amd64`, `linux/arm64`) to the GitHub Container Registry on the `vX.Y.Z` tag.

### Known limitations

- The provider protocol (`/v1/providers/...`) is not proxied.
- `Range` requests for archives are not supported, an archive is served in full.
- Unfinished background downloads do not survive a process restart.
