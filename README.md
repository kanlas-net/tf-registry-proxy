# tf-registry-proxy

Кеширующий обратный прокси для **модульного реестра Terraform** (registry protocol v1).
Клиент получает ответ сразу, а архив докачивается в фоне и дальше отдаётся с диска или из S3.
Хранение — локальный каталог, S3-совместимое хранилище или оба слоя сразу.

Только стандартная библиотека Python: ни одной сторонней зависимости в рантайме
(ни FastAPI, ни boto3 — подпись S3 V4 реализована в `tfproxy/sigv4.py`).

## Как это работает

```
terraform init
   │  GET /v1/modules/<ns>/<name>/<prov>/versions
   ├─► прокси ──► registry.terraform.io        (кэш по TTL, stale-while-revalidate)
   │  GET /v1/modules/<ns>/<name>/<prov>/<ver>/download
   ├─► прокси: 204 + X-Terraform-Get: https://proxy/archive/<ns>/<name>/<prov>/<ver>.zip
   │            └── задача в фоновую очередь: скачать архив с источника
   │  GET /archive/<ns>/<name>/<prov>/<ver>.zip
   └─► прокси: байты из fs → S3 → (если нет) синхронная загрузка с источника
```

* Список версий и `X-Terraform-Get` кэшируются как документы с TTL.
* Архив скачивается **один раз**: фоновая задача, дедупликация по ключу, локальный lock
  на процесс и распределённый lock-файл в S3 между инстансами.
* Источник не `http(s)` (например `git::https://…`) прокси честно пробрасывает как есть —
  такой архив закешировать нельзя.

## Быстрый старт

### Docker

```bash
docker build -t tf-registry-proxy .
docker run -d --name tfproxy -p 8080:8080 \
  -e TFP_BACKENDS=fs -e TFP_CACHE_DIR=/var/cache/tf-registry-proxy \
  -v tfproxy-cache:/var/cache/tf-registry-proxy \
  tf-registry-proxy
curl -s localhost:8080/healthz
```

Тесты можно выполнить и внутри образа:

```bash
docker build --target test -t tfproxy-test . && docker run --rm tfproxy-test
```

### Без Docker

```bash
python3 -m tfproxy serve --port 8080 --backend fs --cache-dir .tf-cache
python3 -m tfproxy prefetch hashicorp/consul/aws          # прогреть кэш
python3 -m tfproxy cache --list                            # что лежит в кэше
python3 -m tfproxy info                                    # итоговая конфигурация
```

### Конфиг клиента Terraform

См. `examples/terraform-cli-config.tf` (переменная `TF_CLI_CONFIG_FILE`):

```hcl
host "registry.terraform.io" {
  services = {
    modules.v1 = "https://tf.example.com/v1/modules/"
  }
}
```

После этого `source = "hashicorp/consul/aws"` работает как раньше, но весь трафик идёт
через прокси. Второй вариант — использовать имя прокси прямо в источнике
(`tf.example.com/hashicorp/consul/aws`): тогда `services` не нужен, прокси сам отдаёт
`/.well-known/terraform.json`.

## Конфигурация

Все опции доступны и как переменные окружения (`TFP_*`), и как флаги CLI (`tfproxy serve --help`).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TFP_HOST` / `TFP_PORT` | `0.0.0.0` / `8080` | адрес слушателя |
| `TFP_UPSTREAM_URL` | `https://registry.terraform.io` | вышестоящий реестр |
| `TFP_EXTERNAL_URL` | из заголовка `Host` | базовый URL прокси для `X-Terraform-Get` |
| `TFP_BACKENDS` | `fs` | `fs`, `s3` или `fs,s3` (чтение: fs → s3, запись: оба) |
| `TFP_CACHE_DIR` | `./cache` | каталог дискового бэкенда |
| `TFP_TTL_VERSIONS` | `3600` | TTL списка версий, сек |
| `TFP_TTL_DOWNLOAD` | `604800` | TTL `X-Terraform-Get`, сек |
| `TFP_STALE_REVALIDATE` | `true` | отдавать устаревший кэш и обновлять его в фоне |
| `TFP_PREFETCH_LATEST` | `true` | при запросе версий сразу качать архив последней версии |
| `TFP_MAX_ARCHIVE_MB` | `512` | жёсткий предел размера архива |
| `TFP_WORKERS` / `TFP_QUEUE_MAX` | `4` / `2000` | фоновые загрузчики |
| `TFP_REQUEST_TIMEOUT` | `10` | таймаут сокета для коротких JSON-запросов к реестру, сек |
| `TFP_DOWNLOAD_TIMEOUT` | `60` | таймаут сокета при качании архива (на один read, не на всю загрузку), сек |
| `TFP_PROMOTE_TO_FS` | `true` | при попадании в S3 подогревать дисковый кэш |
| `TFP_LOCKS` | `true` | распределённые lock-файлы |
| `TFP_LOCK_TTL` | `900` | время жизни lock-файла, сек |
| `TFP_LOCK_WAIT` | `120` | сколько ждать чужой загрузки, сек |
| `TFP_S3_BUCKET` / `TFP_S3_REGION` | — | бакет и регион |
| `TFP_S3_ENDPOINT` | AWS | endpoint для MinIO / Yandex Object Storage / R2 / Ceph |
| `TFP_S3_PREFIX` | `tfregistry` | префикс ключей |
| `TFP_S3_ACCESS_KEY` / `TFP_S3_SECRET_KEY` / `TFP_S3_SESSION_TOKEN` | — | ключи (есть фолбэк на `AWS_*`) |
| `TFP_S3_PATH_STYLE` | авто для своих endpoint | `https://host/bucket/key` вместо виртуального хоста |
| `TFP_S3_CONDITIONAL_LOCKS` | `false` | создавать локи через `If-None-Match: *` |
| `TFP_S3_LOCK_VERIFY_MS` | `250` | пауза перед проверкой lock-объекта, если условных записей нет |
| `TFP_S3_UNSIGNED_PAYLOAD` | `false` | `UNSIGNED-PAYLOAD` при PUT (только HTTPS) |
| `TFP_ADMIN_TOKEN` | пусто | токен для `/api/v1/*` |
| `TFP_TLS_CERT` / `TFP_TLS_KEY` | пусто | HTTPS без реверс-прокси |
| `TFP_LOG_LEVEL` | `INFO` | логирование |

## S3 и несколько инстансов на один бакет

Локи нужны ровно для этого сценария. Перед загрузкой архива инстанс атомарно создаёт
объект `locks/modules/<ns>/<name>/<prov>/<ver>.zip.lock` с JSON-записью:

```json
{"token": "…", "key": "modules/…/1.0.0.zip", "instance": "host:pid",
 "started_at": 1793780000.0, "expires_at": 1793780900.0}
```

* создал лок — качаем; не создал — ждём (`TFP_LOCK_WAIT`), пока объект появится;
* `expires_at` просрочен → лок считается мусором от убитого процесса и перебивается
  (`TFP_LOCK_TTL`), поэтому `kill -9` или OOM не блокируют модуль навсегда;
* длинная загрузка обновляет `expires_at` перед записью объекта;
* удалить можно только свой лок (сверка `token`), чужой не трогается;
* после записи (или ошибки) лок снимается в `finally`.

Два способа атомарного создания:

1. `TFP_S3_CONDITIONAL_LOCKS=true` — PUT с `If-None-Match: *`, хранилище само отказывает
   (AWS S3, MinIO, R2, Ceph RGW). Строгая взаимная блокировка.
2. По умолчанию — PUT, короткая пауза и повторное чтение: лок остаётся нашим, только если
   в бакете лежат ровно наши байты. Работает на любом S3-совместимом хранилище; при
   редкой гонке возможны два «победителя», но оба скачивают одинаковые байты, а TTL
   ограничивает последствия.

Для дискового бэкенда лок создаётся через `O_CREAT|O_EXCL` (атомарно), но диск локален для
каждого инстанса, поэтому арбитром всегда выступает S3-слой.

## Endpoints

| Метод | Путь | Кто | Описание |
|---|---|---|---|
| GET | `/.well-known/terraform.json` | все | `{"modules.v1": "/v1/modules/"}` |
| GET | `/v1/modules/{ns}/{name}/{prov}/versions` | все | список версий (кэш) |
| GET | `/v1/modules/{ns}/{name}/{prov}/{ver}/download` | все | `204` + `X-Terraform-Get` |
| GET/HEAD | `/archive/{ns}/{name}/{prov}/{ver}.zip` | все | байты архива |
| GET | `/healthz` | все | статус, бэкенды, очередь, статистика локов |
| GET | `/metrics` | все | Prometheus text format |
| GET | `/api/v1/cache` | admin | что закэшировано + счётчики |
| POST | `/api/v1/prefetch` | admin | прогреть: `{"namespace","name","provider","versions"}` |
| DELETE | `/api/v1/cache/{ns}/{name}/{prov}[/{ver}]` | admin | вычистить модуль или версию |

Заголовки ответов: `X-TFP-Cache: hit|miss|stale|stale-on-error`, `X-TFP-Source: proxy|upstream`,
`ETag` (sha256 архива), `Retry-After: 5` при `503 download_in_progress`.

## Тесты

```bash
python3 -m unittest discover -s tests -t . -v
```

Поднятые в тестах фейки — это настоящие HTTP-серверы: `tests/support/fake_upstream.py`
(реестр + раздача архивов) и `tests/support/fake_s3.py` (PUT/GET/HEAD/DELETE/ListObjectsV2,
проверяет `Authorization`, `x-amz-content-sha256` и `If-None-Match`).

Что проверяется отдельно от остального:

* `test_sigv4.py` — подпись S3 V4 по эталонным векторам, посчитанным `botocore.auth.SigV4Auth`
  (при установленном botocore добавляется пофайловое сравнение с botocore);
* `test_locking.py` — два `LockManager` на одно хранилище: ровно один победитель, перебой
  просроченного лока, `refresh`, `wait_for_release`;
* `test_server_registry.py::SharedS3Test` — два инстанса прокси на один бакет: архив
  скачивается один раз, второй инстанс отдаёт его из S3, параллельная загрузка блокируется;
* `test_cache.py` — TTL, stale-while-revalidate, `stale-on-error`, дедупликация фоновых задач.

## Ограничения

* Протокол **модулей**. Провайдеры (`/v1/providers/...`, `SHA256SUMS`, подпись ключами) не
  проксируются — для них есть `getproviders`-зеркала.
* Terraform требует HTTPS для внешних хостов; для продакшена поставьте TLS
  (`TFP_TLS_CERT`/`TFP_TLS_KEY` или реверс-прокси). `http://` удобно только для локальных
  экспериментов на `127.0.0.1`.
* Диапазонные (`Range`) запросы архивов не поддерживаются: архив всегда отдаётся целиком,
  размер известен заранее.
* Кэш метаданных и архивы — обычные объекты: при желании их можно забирать напрямую из
  бакета, но путь `meta/…` и `locks/…` лучше держать внутри прокси.
* Фоновая загрузка не переживает перезапуск процесса: незавершённые задачи встанут в очередь
  заново при следующем запросе клиента.
