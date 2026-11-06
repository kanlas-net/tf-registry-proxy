# tf-registry-proxy

[![CI](https://github.com/kanlas-net/tf-registry-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kanlas-net/tf-registry-proxy/actions/workflows/ci.yml)
[![Release](https://github.com/kanlas-net/tf-registry-proxy/actions/workflows/release.yml/badge.svg)](https://github.com/kanlas-net/tf-registry-proxy/actions/workflows/release.yml)
[![Version 1.0.0](https://img.shields.io/badge/version-1.0.0-blue)](https://github.com/kanlas-net/tf-registry-proxy/releases/tag/v1.0.0)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-tf--registry--proxy-2496ED?logo=docker&logoColor=white)](https://github.com/users/kanlas-net/packages/container/package/tf-registry-proxy)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A caching reverse proxy for the **Terraform module registry** (registry protocol v1).
The client gets its answer right away, while the archive is downloaded in the background
and afterwards served from disk or from S3.
Storage is a local directory, an S3-compatible bucket, or both tiers at once.

Standard library Python only: not a single third-party runtime dependency
(neither FastAPI nor boto3 — the S3 V4 signature is implemented in `tfproxy/sigv4.py`).

## How it works

```
terraform init
   │  GET /v1/modules/<ns>/<name>/<prov>/versions
   ├─► proxy ──► registry.terraform.io        (TTL cache, stale-while-revalidate)
   │  GET /v1/modules/<ns>/<name>/<prov>/<ver>/download
   ├─► proxy: 204 + X-Terraform-Get: https://proxy/archive/<ns>/<name>/<prov>/<ver>.zip
   │            └── a queue job: download the archive from the origin
   │  GET /archive/<ns>/<name>/<prov>/<ver>.zip
   └─► proxy: bytes from fs → S3 → (if missing) a synchronous download from the origin
```

* The version list and `X-Terraform-Get` are cached as documents with a TTL.
* An archive is downloaded **once**: a background job, deduplicated by key, with a local
  per-process lock and a distributed lock file in S3 shared between instances.
* A non-`http(s)` origin (for example `git::https://…`) is forwarded as it is — such an
  archive cannot be cached.

## Quick start

### Docker

The prebuilt image lives in the GitHub Container Registry and is public — no login needed:

```bash
docker run -d --name tfproxy -p 8080:8080 \
  -e TFP_BACKENDS=fs \
  -v tfproxy-cache:/var/cache/tf-registry-proxy \
  ghcr.io/kanlas-net/tf-registry-proxy:1.0.0
curl -s localhost:8080/healthz
```

Tags of the same image: `1.0.0` (exact version), `1.0` (minor), `1` (major),
`latest`. Built for `linux/amd64` and `linux/arm64`.

Build from source:

```bash
docker build -t tf-registry-proxy .
docker run -d --name tfproxy -p 8080:8080 \
  -e TFP_BACKENDS=fs -e TFP_CACHE_DIR=/var/cache/tf-registry-proxy \
  -v tfproxy-cache:/var/cache/tf-registry-proxy \
  tf-registry-proxy
curl -s localhost:8080/healthz
```

The tests can also be run inside the image:

```bash
docker build --target test -t tfproxy-test . && docker run --rm tfproxy-test
```

### Without Docker

```bash
python3 -m tfproxy serve --port 8080 --backend fs --cache-dir .tf-cache
python3 -m tfproxy prefetch hashicorp/consul/aws          # warm the cache
python3 -m tfproxy cache --list                            # what the cache holds
python3 -m tfproxy healthcheck                             # probe the storage, exit 1 if broken
python3 -m tfproxy info                                    # the resulting configuration
```

### Terraform client configuration

See `examples/terraform-cli-config.tf` (the `TF_CLI_CONFIG_FILE` variable):

```hcl
host "registry.terraform.io" {
  services = {
    modules.v1 = "https://tf.example.com/v1/modules/"
  }
}
```

After that `source = "hashicorp/consul/aws"` keeps working as before, but all the traffic
goes through the proxy. The second option is to use the proxy name directly in the source
(`tf.example.com/hashicorp/consul/aws`): then `services` is not needed, the proxy serves
`/.well-known/terraform.json` itself.

## Configuration

Every option is available both as an environment variable (`TFP_*`) and as a CLI flag
(`tfproxy serve --help`).

| Variable | Default | Purpose |
|---|---|---|
| `TFP_HOST` / `TFP_PORT` | `0.0.0.0` / `8080` | listen address |
| `TFP_UPSTREAM_URL` | `https://registry.terraform.io` | the upstream registry |
| `TFP_EXTERNAL_URL` | from the `Host` header | base URL of the proxy, used in `X-Terraform-Get` |
| `TFP_BACKENDS` | `fs` | `fs`, `s3` or `fs,s3` (read: fs → s3, write: both) |
| `TFP_CACHE_DIR` | `./cache` | directory of the disk backend |
| `TFP_TTL_VERSIONS` | `3600` | TTL of the version list, seconds |
| `TFP_TTL_DOWNLOAD` | `604800` | TTL of `X-Terraform-Get`, seconds |
| `TFP_STALE_REVALIDATE` | `true` | serve stale cache and refresh it in the background |
| `TFP_PREFETCH_LATEST` | `true` | on a version request, immediately queue the latest archive |
| `TFP_MAX_ARCHIVE_MB` | `512` | hard archive size limit |
| `TFP_WORKERS` / `TFP_QUEUE_MAX` | `4` / `2000` | background downloaders |
| `TFP_REQUEST_TIMEOUT` | `10` | socket timeout for short JSON requests to the registry, seconds |
| `TFP_DOWNLOAD_TIMEOUT` | `60` | socket timeout while downloading an archive (per read, not for the whole download), seconds |
| `TFP_PROMOTE_TO_FS` | `true` | on an S3 hit, warm the disk cache |
| `TFP_LOCKS` | `true` | distributed lock files |
| `TFP_LOCK_TTL` | `900` | lock file lifetime, seconds |
| `TFP_LOCK_WAIT` | `120` | how long to wait for someone else's download, seconds |
| `TFP_S3_BUCKET` / `TFP_S3_REGION` | — | bucket and region |
| `TFP_S3_ENDPOINT` | AWS | endpoint for MinIO / Yandex Object Storage / R2 / Ceph |
| `TFP_S3_PREFIX` | `tfregistry` | key prefix |
| `TFP_S3_ACCESS_KEY` / `TFP_S3_SECRET_KEY` / `TFP_S3_SESSION_TOKEN` | — | keys (there is a fallback to `AWS_*`) |
| `TFP_S3_PATH_STYLE` | auto for custom endpoints | `https://host/bucket/key` instead of a virtual host |
| `TFP_S3_CONDITIONAL_LOCKS` | `false` | create locks with `If-None-Match: *` |
| `TFP_S3_LOCK_VERIFY_MS` | `250` | pause before reading a lock object back, when conditional writes are unsupported |
| `TFP_S3_UNSIGNED_PAYLOAD` | `false` | `UNSIGNED-PAYLOAD` on PUT (HTTPS only) |
| `TFP_ADMIN_TOKEN` | empty | token for `/api/v1/*` |
| `TFP_TLS_CERT` / `TFP_TLS_KEY` | empty | HTTPS without a reverse proxy |
| `TFP_LOG_LEVEL` | `INFO` | logging |
| `TFP_HEALTH_PROBE_WRITE` | `true` | the storage probe also writes one small object (put → read back → delete) |
| `TFP_HEALTH_CACHE_TTL` | `5` | seconds a readiness result is reused, so kubelet polling does not hammer the bucket |
| `TFP_HEALTH_TIMEOUT` | `5` | socket timeout for probes, seconds (much shorter than a normal object operation) |
| `TFP_STARTUP_CHECK` | `true` | probe the storage once before serving |
| `TFP_STARTUP_CHECK_RETRIES` | `5` | how many attempts the startup probe gets |
| `TFP_STARTUP_CHECK_INTERVAL` | `3` | pause between startup probe attempts, seconds |
| `TFP_STARTUP_REQUIRED` | `false` | exit 1 instead of serving when the startup probe fails |
| `TFP_READY_STRICT` | `true` | `false` = one healthy backend is enough for `/readyz` (degraded but serving) |
| `TFP_READY_UPSTREAM` | `false` | a broken upstream registry also fails `/readyz` |
| `TFP_METRICS_PENDING_SERIES` | `50` | how many per-module pending series `/metrics` exports (`0` = totals only) |

## S3 and several instances on one bucket

Locks exist for exactly this scenario. Before downloading an archive, an instance atomically
creates the object `locks/modules/<ns>/<name>/<prov>/<ver>.zip.lock` with a JSON record:

```json
{"token": "…", "key": "modules/…/1.0.0.zip", "instance": "host:pid",
 "started_at": 1793780000.0, "expires_at": 1793780900.0}
```

* created the lock — we download; did not create it — we wait (`TFP_LOCK_WAIT`) until the
  object shows up;
* `expires_at` is in the past → the lock counts as leftovers from a killed process and gets
  taken over (`TFP_LOCK_TTL`), so `kill -9` or an OOM never blocks a module forever;
* a long download refreshes `expires_at` before writing the object;
* only your own lock can be deleted (the `token` is compared), someone else's is left alone;
* after the write (or an error) the lock is released in `finally`.

Two ways to create a lock atomically:

1. `TFP_S3_CONDITIONAL_LOCKS=true` — a PUT with `If-None-Match: *`, the storage refuses by
   itself (AWS S3, MinIO, R2, Ceph RGW). Strict mutual exclusion.
2. By default — a PUT, a short pause and a read-back: the lock stays ours only if exactly our
   bytes are in the bucket. Works against any S3-compatible storage; in a rare race there can
   be two "winners", but both download the same bytes and the TTL bounds the consequences.

For the disk backend the lock is created with `O_CREAT|O_EXCL` (atomic), but the disk is local
to each instance, so the S3 tier is always the arbiter.

## Endpoints

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/.well-known/terraform.json` | anyone | `{"modules.v1": "/v1/modules/"}` |
| GET | `/v1/modules/{ns}/{name}/{prov}/versions` | anyone | the version list (cached) |
| GET | `/v1/modules/{ns}/{name}/{prov}/{ver}/download` | anyone | `204` + `X-Terraform-Get` |
| GET/HEAD | `/archive/{ns}/{name}/{prov}/{ver}.zip` | anyone | the archive bytes |
| GET | `/healthz` | anyone | liveness: status, backends, queue, lock statistics — never touches the network |
| GET | `/readyz` | anyone | readiness: probes the storage backends (and with `TFP_READY_UPSTREAM`, the registry) |
| GET | `/metrics` | anyone | Prometheus text format |
| GET | `/api/v1/cache` | admin | what is cached + counters |
| GET | `/api/v1/pending` | admin | requested module versions that are still not in storage |
| POST | `/api/v1/prefetch` | admin | warm the cache: `{"namespace","name","provider","versions"}` |
| DELETE | `/api/v1/cache/{ns}/{name}/{prov}[/{ver}]` | admin | drop a module or a version |

Response headers: `X-TFP-Cache: hit|miss|stale|stale-on-error`, `X-TFP-Source: proxy|upstream`,
`ETag` (the sha256 of the archive), `Retry-After: 5` on `503 download_in_progress`.

## Health checks

Three ways to ask the same question, at three different costs:

| Check | Question | Fails when |
|---|---|---|
| `GET /healthz` | is the process wedged? | nothing but a dead process: it never touches the network |
| `GET /readyz` | can this instance serve a real `terraform init` right now? | a storage backend does not answer (or the registry, with `TFP_READY_UPSTREAM=true`) |
| `tfproxy healthcheck` | the same, once, from the command line | exit code 1, JSON report on stdout |

* A storage probe is a `ListObjectsV2` with `max-keys=1` plus, by default, one small
  write → read back → delete round trip. Read-only credentials pass a read probe and then fail
  every download, which is why the write is on; `TFP_HEALTH_PROBE_WRITE=false` turns it off. The
  probe reports the S3 error code (`AccessDenied`, `NoSuchBucket`, `SignatureDoesNotMatch`) rather
  than a bare HTTP status.
* The write probe writes `health/probe-<host>-<pid>.json`, not one shared object: two replicas on
  one bucket would otherwise delete each other's file and report a read-back mismatch that is not
  real.
* The result is cached for `TFP_HEALTH_CACHE_TTL` seconds, so a kubelet polling every 5 s does not
  put thousands of LIST calls on the bucket. `GET /readyz?force=1` skips the cache.
* The same checks run once at startup, with `TFP_STARTUP_CHECK_RETRIES` attempts, so a bucket that
  is mid-restart gets a chance. By default the proxy starts anyway and only reports "not ready";
  `TFP_STARTUP_REQUIRED=true` makes a failed startup fatal instead.
* `TFP_READY_STRICT=false` keeps an instance in rotation while at least one tier is healthy —
  the disk serving while the bucket is away — instead of taking every pod out at once.

```bash
tfproxy healthcheck                # JSON report, exit 1 when something is broken
tfproxy healthcheck --pending      # plus what is still waiting to be downloaded
tfproxy healthcheck --read-only    # skip the write probe
```

### Kubernetes

`examples/kubernetes.yaml` is a working Deployment + Service; the probe block is the part that
matters:

```yaml
startupProbe:                       # not ready until the bucket answers; 30 x 5s budget
  httpGet: {path: /readyz, port: http}
  periodSeconds: 5
  failureThreshold: 30
readinessProbe:
  httpGet: {path: /readyz, port: http}
  periodSeconds: 10
livenessProbe:
  httpGet: {path: /healthz, port: http}
  periodSeconds: 10
```

Keep liveness on `/healthz`: pointing it at `/readyz` turns an object-storage outage into a restart
storm, and the proxy has a warm cache it can serve while the bucket is away.

## Metrics

`GET /metrics` serves Prometheus text format 0.0.4, written by hand (`tfproxy/metrics.py`) — the
image has no client library to keep in step with the server.

| Series | Type | What it answers |
|---|---|---|
| `tfproxy_upstream_requests_total{op,result}` | counter | calls to the registry, `result` = `ok`, `error`, `not_found` |
| `tfproxy_upstream_errors_total{op,reason}` | counter | failed registry calls, `reason` = `unreachable`, `http_503`, `malformed`, … |
| `tfproxy_upstream_consecutive_failures{op}` | gauge | failures in a row, reset by the next success |
| `tfproxy_upstream_last_success_timestamp_seconds{op}` | gauge | when the registry last answered |
| `tfproxy_upstream_request_seconds{op}` | histogram | how long the registry takes |
| `tfproxy_archive_pending_modules` | gauge | versions clients asked for that are still not in storage |
| `tfproxy_archive_pending_oldest_seconds` | gauge | how long the oldest of them has been waiting |
| `tfproxy_archive_pending_age_seconds{module,version}` | gauge | the same per module (capped by `TFP_METRICS_PENDING_SERIES`) |
| `tfproxy_archive_downloads_total{result}` | counter | archive fetches from the origin: `ok`, `error`, `store_error`, `not_cacheable` |
| `tfproxy_archive_download_seconds` | histogram | time to fetch and store one archive |
| `tfproxy_http_requests_total{route,method,status}` | counter | everything the proxy answered, by route and status class |
| `tfproxy_http_errors_total{route,code}` | counter | 4xx/5xx answers, labelled with the protocol error code |
| `tfproxy_http_request_seconds{route}` | histogram | request latency per route |
| `tfproxy_storage_probe_up{backend}`, `tfproxy_storage_probe_seconds{backend}` | gauge | last probe result per backend |
| `tfproxy_storage_errors_total{op}` | counter | storage failures the proxy swallowed instead of failing a request |
| `tfproxy_jobs{name}`, `tfproxy_locks{name}` | gauge | background queue and distributed lock counters |
| `tfproxy_uptime_seconds`, `tfproxy_process_threads`, `tfproxy_process_max_rss_bytes`, `tfproxy_build_info{version}` | gauge | the process itself |
| `tfproxy_versions_hit`, `tfproxy_archive_fetches`, … | counter | the cache counters also visible in `/api/v1/cache` |

Three alerts worth wiring up first (`examples/prometheus-alerts.yaml` has them ready):

```yaml
# 1. the upstream registry is failing us — 404s are deliberately not counted here
- alert: TerraformRegistryUnreachable
  expr: increase(tfproxy_upstream_errors_total[5m]) > 3

# 2. nothing has come from the registry for 5 minutes, even at low traffic
- alert: TerraformRegistrySilent
  expr: time() - tfproxy_upstream_last_success_timestamp_seconds > 300

# 3. someone asked for a module and 10 minutes later it still is not in storage
- alert: TerraformModulesNotDownloaded
  expr: tfproxy_archive_pending_oldest_seconds > 600
```

`tfproxy_archive_pending_modules > 0` is the softer version of the third alert: it says "a download
is outstanding", which is normal for a few seconds and a problem after ten minutes.

`/metrics` is unauthenticated, because Prometheus scrapes without credentials. Every series above is
aggregate except `tfproxy_archive_pending_age_seconds{module,version}`, which names the modules you
serve — that one is opt-in (`TFP_METRICS_PENDING_SERIES`, default `0` = totals only). Keep the scrape
port inside the cluster network, or front it with whatever your Prometheus uses to authenticate.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

The fakes the tests bring up are real HTTP servers: `tests/support/fake_upstream.py`
(a registry plus archive serving) and `tests/support/fake_s3.py` (PUT/GET/HEAD/DELETE/
ListObjectsV2, checks `Authorization`, `x-amz-content-sha256` and `If-None-Match`).

Checked separately from the rest:

* `test_sigv4.py` — the S3 V4 signature against reference vectors computed by
  `botocore.auth.SigV4Auth` (with botocore installed, a per-file comparison against botocore
  is added);
* `test_locking.py` — two `LockManager` instances on one storage: exactly one winner, taking
  over an expired lock, `refresh`, `wait_for_release`;
* `test_server_registry.py::SharedS3Test` — two proxy instances on one bucket: the archive is
  downloaded once, the second instance serves it from S3, a parallel download is blocked;
* `test_cache.py` — TTL, stale-while-revalidate, `stale-on-error`, deduplication of background
  jobs.

## Versioning and builds

The version lives in two places and must match: `tfproxy.__version__` and `version` in
`pyproject.toml` (`tests/test_packaging.py` makes sure of that).

A release is a git tag `vX.Y.Z`. To ship one:

```bash
# 1. bump the version in tfproxy/__init__.py and pyproject.toml
git commit -am "release 1.1.0"
git tag v1.1.0
git push origin main --follow-tags
```

GitHub Actions does the rest:

| Workflow | When | What it does |
|---|---|---|
| `ci.yml` | push to `main`, PR, manual run | tests on Python 3.10–3.13, image build, `--target test` build with the suite run inside the image, smoke test of `/healthz` and `/.well-known/terraform.json` |
| `release.yml` | tag `vX.Y.Z` | compares the tag with `tfproxy.__version__`, runs the tests, builds `linux/amd64` + `linux/arm64`, publishes to `ghcr.io`, creates the GitHub Release |

Publishing runs under the service `GITHUB_TOKEN` (with `packages: write`); the repository needs
no extra secrets. Package visibility follows the repository — for a public repository the image
is public and is pulled without `docker login`.

Semver as used here:

* **MAJOR** — breaks the client or the configuration: a different protocol path, a `TFP_*` variable that was renamed or
  changed meaning, a different key format in the bucket;
* **MINOR** — backwards compatible: a new endpoint, a new variable, a new backend;
* **PATCH** — fixes only, nothing got worse for the client.

The image in the registry: `ghcr.io/kanlas-net/tf-registry-proxy`.

## Limitations

* The **module** protocol only. Providers (`/v1/providers/...`, `SHA256SUMS`, key signatures)
  are not proxied — `getproviders` mirrors exist for those.
* Terraform requires HTTPS for external hosts; for production add TLS
  (`TFP_TLS_CERT`/`TFP_TLS_KEY` or a reverse proxy). `http://` is convenient only for local
  experiments on `127.0.0.1`.
* Range requests for archives are not supported: an archive is always served in full, its size
  is known in advance.
* Metadata cache entries and archives are plain objects: if you want, you can read them straight
  from the bucket, but keep the `meta/…` and `locks/…` paths inside the proxy.
* Background downloads do not survive a process restart: unfinished jobs are queued again on the
  next client request.
