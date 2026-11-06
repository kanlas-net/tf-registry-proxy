# AGENTS.md

Instructions for coding agents working in this repository. Read this before you change
anything; most rules below are enforced by a test, so "it works locally" is not enough.

`tf-registry-proxy` is a caching reverse proxy for the Terraform **module** registry protocol
(registry protocol v1), written against the Python standard library only.

## Commands

```bash
# the whole suite (~35 s)
python3 -m unittest discover -s tests -t .

# one file / one test
python3 -m unittest tests.test_locking -v
python3 -m unittest tests.test_cache.VersionsTest.test_stale_is_served_and_refreshed_in_background

# run the proxy
python3 -m tfproxy serve --port 8080 --backend fs --cache-dir .tf-cache

# the suite inside the image (what CI runs)
docker build --target test -t tfproxy-test . && docker run --rm tfproxy-test
```

`skipped=4` is the expected result, not a failure: those four are the SigV4 cross-check against
`botocore`, which is an optional extra (`pip install .[dev]` runs them). Inside the image the
count is `skipped=8` — the four workflow tests skip themselves because `.github/` is not in the
build context.

Ruff is configured in `pyproject.toml` (line length 118, rules `E,F,W,I,UP,B,C4,SIM`, ignoring
`E501, SIM105, B904`) but is **not** installed by default. Do not claim lint passed if you never
ran it.

## Hard constraints

1. **Standard library only at runtime.** `dependencies = []` in `pyproject.toml` is deliberate,
   and `ProjectMetadataTest.test_no_third_party_imports_in_the_package` walks every file in
   `tfproxy/` and fails on a non-stdlib import. Adding a dependency is a design decision, not a
   convenience — the S3 V4 signature exists as hand-written code (`tfproxy/sigv4.py`) for exactly
   this reason.
2. **Python >= 3.10.** CI runs 3.10, 3.11, 3.12 and 3.13. No syntax that only works on 3.11+.
3. **The version lives in two places**: `tfproxy.__version__` and `version` in `pyproject.toml`.
   A test asserts they match.
4. **Every `TFP_*` setting must be documented.** `ReadmeTest.test_every_setting_is_documented`
   extracts the names from `tfproxy/config.py` and fails if one is missing from the README table.
5. **`.dockerignore` must keep `tests/`, `README.md`, `pyproject.toml` and `docker-compose.yml`
   in the build context.** The `test` stage does `COPY . /src` and runs the suite, so excluding
   them silently breaks the image build. `DockerfileTest.test_dockerignore_keeps_everything_the_suite_needs`
   guards this.
6. **CI must never get `packages: write`.** Only `release.yml` publishes, and a test asserts
   `ci.yml` does not contain that permission.
7. **Any job whose script calls `gh` or `git` must run `actions/checkout` first.** Both died with
   `failed to run git: fatal: not a git repository` once. Guarded by
   `test_jobs_that_shell_out_to_git_or_gh_check_the_tree_out`.
8. **Tag and branch filters in workflows are globs, not regular expressions.** `v*.*.*` is correct;
   the matcher is checked against real tag names in
   `test_release_tag_filter_actually_matches_the_version_tag`.
9. **The repository is English-only** — code, comments, docs and test data. Do not add text in
   another language.
10. **A re-pushed tag must publish the release again.** Deleting a tag turns its release into a
   draft instead of deleting it, and `gh release view` still finds drafts, so a step that only
   checks existence skips publishing forever. Guarded by
   `test_a_leftover_draft_release_is_published_not_skipped`.
11. **Liveness never touches the network.** `/healthz` stays a local check, so an S3 outage cannot
   restart every pod. Anything that probes storage belongs on `/readyz` (and in
   `tfproxy healthcheck`), and its result is cached for `TFP_HEALTH_CACHE_TTL` because kubelet polls
   far more often than a bucket should be LISTed. See `tests.test_health`.
12. **Metric labels come from a bounded set** (route, op, result, reason, backend). A module id
   only ever reaches a label through `tfproxy_archive_pending_age_seconds`, which is capped by
   `TFP_METRICS_PENDING_SERIES` and forgets the series that disappeared. Error *messages* are never
   labels — that is what `UpstreamError.reason` is for.

## Naming trap

`cache.metrics` is the Prometheus bundle; the counter snapshot served by `/api/v1/cache` is
`cache.counters()`. They used to share the name, and a `Metrics object is not callable` is that
clash reappearing.

## Layout

| Path | Responsibility |
|---|---|
| `tfproxy/server.py` | HTTP layer: routing on the **raw** path, registry protocol responses, stdlib server |
| `tfproxy/cache.py` | TTL document cache, stale-while-revalidate, `stale-on-error` |
| `tfproxy/workers.py` | background queue: the "answer now, download later" half |
| `tfproxy/locking.py` | distributed download locks (`LockManager`) |
| `tfproxy/health.py` | liveness vs readiness, the storage/upstream probes, the startup retry loop |
| `tfproxy/metrics.py` | hand-written Prometheus registry and the `Metrics` bundle (every exported series) |
| `tfproxy/pending.py` | ledger of "a client asked for it, storage does not have it yet" |
| `tfproxy/storage/` | `base` interface (+ probes), `fs`, `s3`, `multi` (tiered: read first hit, write all) |
| `tfproxy/upstream.py` | HTTP client for the registry and for archive downloads |
| `tfproxy/sigv4.py` | hand-written AWS Signature V4 |
| `tfproxy/models.py` | module refs, storage keys, go-getter source parsing |
| `tfproxy/config.py` | CLI flags and `TFP_*` environment variables |
| `tests/support/` | `fake_upstream.py`, `fake_s3.py`, `harness.py` |

## Testing conventions

- The fakes are **real HTTP servers** on real sockets, not mocks. `fake_s3.py` actually validates
  `Authorization`, `x-amz-content-sha256` and `If-None-Match`, so a wrong signature fails there
  rather than being asserted directly.
- `ProxyHarness.create()` sets `prefetch_latest=True` by default. A test that purges or inspects
  the cache must call `self.wait()` after `cache.versions(...)`, otherwise a background prefetch
  lands mid-assertion.
- A job still queued when the harness shuts down stays in the queue. Assert
  `stats()["inflight"] == 0` instead of `wait_idle()` in that case.
- Do not add `time.sleep()`. Use the harness wait helpers; `wait_idle(timeout=...)` already polls.
- Cross-instance behaviour has its own test (`test_server_registry.py::SharedS3Test`) — two proxy
  instances, one bucket. Extend it rather than testing locking only in isolation.

## Do not regress

Each of these was a real bug found in review; the surrounding tests exist because of it.

- `storage/s3.py`: `_request` calls `rewind(data)` before **every** attempt and skips the retry
  when `replayable(data)` is false. A body that cannot be rewound must not be retried.
- `storage/fs.py`: a non-numeric `fetched_at` in the sidecar falls back to the file `mtime`; the
  file is fsynced before `os.replace`.
- `upstream.Headers` is a case-insensitive dict — `X-Terraform-Get` must not depend on how the
  upstream capitalised it.
- `server.py` routes on the **raw** request path and `unquote`s each captured segment afterwards.
  Decoding before routing lets `%2F` escape a route.
- `ArchiveDownloader.fetch` refuses any non-`http(s)` URL; `GetterSource.cacheable` compares the
  scheme case-insensitively.
- `LockManager.refresh` restores `handle.expires_at` when it fails; a failed refresh must not
  leave a lock looking permanently owned.
- `workers.start()` clears `_stop`, so a queue can be restarted.
- `StreamHandle.chunks()` raises `ValueError` after close.
- `config._env_bool` raises `ConfigError` on a value that is not a boolean.
- `storage/s3.py`: `probe()` uses `probe_timeout`, not the 60 s object timeout — a readiness probe
  must not hold a kubelet check for a minute.
- `metrics.py`: a pending limit of `0` means "no per-module series", not "unlimited", and gauges
  built from a snapshot must `forget()` the label combinations that vanished, or a module cached an
  hour ago keeps reporting an age forever.

## Releasing

```bash
# 1. bump tfproxy/__init__.py and pyproject.toml together
git commit -am "release 1.1.0"
git tag v1.1.0
git push origin main --follow-tags
```

- **A workflow file must already exist on the default branch before a tag can trigger it.**
  Pushing a new `release.yml` and its tag in the same `git push` silently publishes nothing.
  If you change a workflow, push the branch first, then tag.
- The tag must equal `tfproxy.__version__`, or the `verify` job fails before anything is pushed.
- **Re-tagging a version is allowed.** Delete the tag, push the new one; the release job publishes
  the draft the old tag left behind. Check the release page afterwards — a draft is invisible there.
- Semver as used here: **MAJOR** breaks the client or the config (protocol path, renamed or
  re-purposed `TFP_*` variable, bucket key format); **MINOR** is backwards compatible (endpoint,
  variable, backend); **PATCH** is fixes only.

## Before you finish

- Run the full suite; `python3 -m unittest discover -s tests -t .` must end `OK`.
- If you touched `Dockerfile`, `.dockerignore`, `pyproject.toml` or `.github/workflows/`, also
  build the test stage: `docker build --target test . && docker run --rm tfproxy-test`.
- If you added or renamed a setting, update the README table in the same commit.
- Never report a build, publish or deploy as successful without a check that proves it (HTTP
  status, command output, a live request).
