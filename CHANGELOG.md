# Changelog

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning is
[SemVer](https://semver.org/). A release is marked by a git tag `vX.Y.Z`, which is also what
publishes the image to `ghcr.io`.

## [Unreleased]

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
