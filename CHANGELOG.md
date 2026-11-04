# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версионирование —
[SemVer](https://semver.org/lang/ru/). Релиз помечен git-тегом `vX.Y.Z`, он же
публикует образ в `ghcr.io`.

## [1.0.0] - 2026-11-04

Первый публичный релиз. Протокол модулей Terraform (registry protocol v1) реализован
полностью, backends — локальный каталог и S3-совместимое хранилище.

### Added

- `GET /.well-known/terraform.json`, `/v1/modules/.../versions`,
  `/v1/modules/.../<version>/download`, `/archive/...zip` — клиент `terraform init`
  работает через прокси без изменений.
- Кэш документов по TTL, `stale-while-revalidate`, `stale-on-error`.
- Фоновая докачка архива с дедупликацией: `204` + `X-Terraform-Get` клиенту сразу.
- Бэкенды `fs`, `s3`, оба сразу (`fs,s3`): чтение fs → s3, запись в оба, подогрев
  дискового кэша при попадании в S3.
- Распределённые lock-файлы в S3 для нескольких инстансов на один бакет, с TTL и
  перебоем просроченного лока; условный `If-None-Match: *` при `TFP_S3_CONDITIONAL_LOCKS`.
- Подпись S3 V4 на стандартной библиотеке (`tfproxy/sigv4.py`), проверена по векторам
  `botocore.auth.SigV4Auth`.
- `/healthz`, `/metrics` (Prometheus text format), административные
  `/api/v1/cache`, `/api/v1/prefetch` с токеном `TFP_ADMIN_TOKEN`.
- CLI `tfproxy serve|prefetch|cache|info`, HTTPS напрямую через `TFP_TLS_CERT`/`TFP_TLS_KEY`.
- CI (тесты на Python 3.10–3.13, сборка образа, smoke-тест) и публикация multi-arch
  образа (`linux/amd64`, `linux/arm64`) в GitHub Container Registry по тегу `vX.Y.Z`.

### Known limitations

- Протокол провайдеров (`/v1/providers/...`) не проксируется.
- `Range`-запросы архивов не поддерживаются, архив отдаётся целиком.
- Незавершённые фоновые загрузки не переживают перезапуск процесса.
