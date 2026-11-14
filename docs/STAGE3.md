# Этап 3 — Document Ingestion, ручная приёмка ожидается

Подэтапы 3.1–3.3 подготовлены по отдельным поручениям пользователя. Ниже сохранена
инструкция 3.1; актуальные дополнения 3.2–3.3 и команды — в конце документа.
Не повторяйте provisioning уже созданного MinIO bucket/user. В текущей реализации
bootstrap не вызывается. [Список файлов 3.2–3.3](STAGE3_IMPLEMENTATION_FILES.md).

Код подготовлен, приёмка ожидается. Codex не запускал сборки, миграции,
bootstrap, тесты и smoke-проверки. Все команды ниже выполняет пользователь
в Git Bash из корня проекта. При ошибке команды остановитесь и разберите результат.
Следующие части этапа 3 не начинаются без проверки и подтверждения пользователя.

## Подэтап 3.1 — исходные границы реализации

Добавлены три SQLAlchemy-модели в общей metadata приложения и аддитивная миграция
`0002_documents` после `0001_auth`. Auth-модели, миграция `0001_auth`, API и сессии
не изменены. Нет новых endpoints, Celery tasks, извлечения PDF/DOCX и Frontend.
Нет интеграции с Qdrant, RAG, LLM или бизнес-логирования MLflow.

| Таблица | Содержимое и ограничения |
| --- | --- |
| `documents` | Владелец FK users, имя, pdf/docx, положительный размер, SHA-256 из 64 lowercase hex символов, уникальный object key, статус, nullable idempotency UUID, timestamps, безопасный код ошибки |
| `document_text_versions` | FK документа, положительная версия, normalized text, JSONB-массив локаторов, версия экстрактора, время создания; unique(document_id, version) |
| `document_ingestion_jobs` | FK документа, положительная версия, статус, неотрицательные attempts, available_at, lease UUID/expiry, код ошибки, timestamps; unique(document_id, version) |

Статусы документов: `uploading`, `queued`, `processing`, `ready`, `failed`,
`needs_ocr`. Статусы заданий: `pending`, `published`, `processing`, `succeeded`,
`failed`. Это только ограничения допустимых значений, не реализованный workflow.
Lease UUID и срок должны либо оба отсутствовать, либо оба присутствовать.
Индексы: owner/created/id, owner/SHA-256, job status/available, lease expiry.
Unique(owner, idempotency_key) не объединяет разных владельцев; NULL допускает
обычные независимые загрузки. SHA-256 не уникален глобально.

Все новые FK имеют ON DELETE RESTRICT, ORM delete/delete-orphan cascades нет.
Downgrade запрещён, чтобы случайный откат не удалил документы. `updated_at`
будет явно обновляться сервисом следующего подэтапа; DB trigger сейчас не создаётся.
Локаторы и их элементы потребуют прикладной валидации при реализации извлечения.

## Object storage

`ObjectStorage.from_settings()` явно создаёт MinIO SDK client, без запросов при
импорте приложения. Подключение не создаёт bucket и не меняет политики.
Методы:

- `upload_original(owner_id, document_id, source, document_format)` принимает
  бинарный поток, возвращает object_key, размер и SHA-256 исходных байтов.
- `open_original(...)` — context manager для чтения с закрытием HTTP response
  и освобождением соединения, в том числе при ошибке.
- `stream_original(...)` — iterator порциями по 64 KiB. При раннем завершении
  потребитель обязан вызвать `close()` у iterator; будущий HTTP endpoint должен
  гарантировать это при отключении клиента.

Ключ: `users/{owner_uuid}/documents/{document_uuid}/originals/{random_uuid}`.
Имя файла не участвует в ключе. Каждый upload получает новый случайный ключ;
повторный вызов не перезаписывает предыдущий оригинал. Сохранение ключа в БД
и удаление неподтверждённых загрузок будут обязанностью будущего service layer.
Проверка ключа запрещает выход из заданного namespace владельца/документа.
UUID должен поступать из проверенного серверного контекста: сам storage-модуль
не проверяет сессию, наличие документа или право клиента на переданный owner_id.

Данные staging держатся в памяти до 1 MiB, далее во временном файле; общий лимит
по умолчанию 20 MiB. Временный файл закрывается автоматически. Из production
используется уже существующий `/tmp` tmpfs. Лимиты HTTP/multipart будут отдельной
задачей: текущий модуль не является загрузочным endpoint и не проверяет структуру PDF/DOCX.

SDK ошибки превращаются в безопасные StorageError/OriginalNotFound, без ответа
сервера и credentials. Нет delete, публичных URL, presigned URLs и автоматического
создания объектов при старте. Backend не получает MinIO root credentials.

## Новые переменные

Существующий `.env` не изменён. Добавьте значения вручную только после просмотра
этой инструкции; не перезаписывайте его копированием `.env.example`.

| Переменная | Default / настройка |
| --- | --- |
| `DOCUMENTS_S3_ENDPOINT` | `minio:9000`, host:port без URL scheme/пути/credentials; IPv6 в этой версии не поддержан |
| `DOCUMENTS_S3_SECURE` | `false` для существующей внутренней HTTP-сети; `true` только при настроенном TLS endpoint |
| `DOCUMENTS_S3_BUCKET` | `medical-ai-documents`; новый bucket, lowercase DNS label, отличается от MLflow |
| `DOCUMENTS_S3_ACCESS_KEY` | В Compose пустой default; в example `medical_ai_documents`; отдельная новая учётная запись |
| `DOCUMENTS_S3_SECRET_KEY` | В Compose пустой default; задайте независимый случайный секрет не короче 16 символов, без CHANGE_ME |
| `DOCUMENTS_S3_REGION` | `us-east-1` |
| `DOCUMENTS_MAX_BYTES` | `20971520`, разрешён диапазон 1–1073741824 байт |

Дополнительно существующий `MLFLOW_ARTIFACT_BUCKET` передаётся backend/worker
для запрета совпадения buckets; новые credentials не передаются MLflow.
Storage validation отложена до явного создания клиента: отсутствие новых ключей
не ломает существующие health/auth/Celery при запуске.

Не используйте root или MLflow access/secret для документов. MinIO bootstrap
отклоняет такое совпадение. Не меняйте COMPOSE_PROJECT_NAME, PostgreSQL credentials,
MLflow bucket, существующие volumes или имена БД.

## Что изменяет постоянные данные

| Операция | Воздействие |
| --- | --- |
| build, unit tests, lint, typecheck, config --quiet | Не меняют рабочую PostgreSQL/MinIO; сборка скачивает зависимости и создаёт образы |
| alembic current/check, SQL SELECT, storage check без флага | Чтение; storage проверяет list access, не создаёт объекты |
| alembic upgrade 0002_documents | Создаёт 3 таблицы, индексы, FK/check/unique; обновляет alembic_version; существующие таблицы не изменяет |
| init-documents.py provision | Создаёт новый закрытый bucket, policy и scoped user в существующем MinIO; меняет постоянные MinIO/IAM metadata |
| storage check --write-synthetic | Добавляет один небольшой синтетический объект, оставляет его в bucket; PostgreSQL не меняет |
| up --no-deps backend worker | Перезапускает процессы, применяет env; сам bootstrap/миграции не выполняет |

Перед persistent операциями:

1. Примите результаты этапа 2, проверьте доступность текущих сервисов.
2. Подтвердите текущую ревизию `0001_auth`; проверьте отсутствие конфликтующих
   таблиц. Если `0002_documents` уже применена, upgrade повторно будет no-op.
   При иных ревизиях/конфликтах остановитесь, не используйте stamp и удаление.
3. Сохраните backup PostgreSQL, проверьте читаемость архива и восстановление
   в отдельной среде. Сохраните существующий backup MinIO, включая конфигурацию IAM.
4. Убедитесь, что новые bucket/access key/policy не принадлежат существующим данным
   или пользователям. Policy name зарезервирован: `medical-ai-documents-storage`.
5. Проверьте настройки и scripts; не публикуйте `.env`, вывод полного Compose config
   или приватные диагностические данные. Не используйте shell tracing.

## Команды: сначала без изменений рабочей БД и MinIO

```bash
cd /c/workplace/Medical_AI_New
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }

dc config --quiet
docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml config --quiet
dc build backend worker
dc run --rm --no-deps backend python -m ruff check .
dc run --rm --no-deps backend python -m ruff format --check .
dc run --rm --no-deps backend python -m mypy app
dc run --rm --no-deps backend python -m pytest -m 'not integration'
dc run --rm --no-deps backend alembic current
```

Если ранее локально созданы Python lock-файлы, обновите их под новую зависимость
`minio==7.2.18` по OPERATIONS.md до сборки. Dockerfile выбирает lock при его наличии.
Без обновления lock установка --no-deps может оставить SDK неустановленным.
Unit tests используют fake storage и offline PostgreSQL DDL: к рабочим хранилищам
не подключаются, не доказывают реальное применение SQL или работу MinIO policy.

Проверка таблиц и счётчиков без вывода персональных данных:

```bash
dc exec -T postgres sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT version_num FROM alembic_version;
SELECT count(*) AS users_count FROM users;
SELECT count(*) AS sessions_count FROM auth_sessions;
SELECT to_regclass('public.documents'),
       to_regclass('public.document_text_versions'),
       to_regclass('public.document_ingestion_jobs');
SQL
```

До первого upgrade три regclass должны быть NULL. Сохраните counts для сравнения;
sessions могут изменяться при входах/выходах пользователей.

## Backup и ручная миграция PostgreSQL

```bash
mkdir -p backups
stage31_backup="backups/before-stage31-$(date +%Y%m%d-%H%M%S).dump"
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$stage31_backup"
test -s "$stage31_backup"
dc exec -T postgres pg_restore --list < "$stage31_backup" > /dev/null
```

Успешный list проверяет читаемость архива, но не заменяет restore в отдельной среде.
После проверки backup и условий выше:

```bash
dc run --rm --no-deps backend alembic upgrade 0002_documents
dc run --rm --no-deps backend alembic current
dc run --rm --no-deps backend alembic check
```

Ожидается ревизия 0002_documents и отсутствие новых autogenerate operations.
Создание FK требует блокировок PostgreSQL; применяйте миграцию в согласованное
окно, особенно при активных транзакциях пользователей. PostgreSQL DDL выполняется
в транзакции Alembic; MinIO provisioning — отдельная операция без общей транзакции.
Повторите SQL SELECT выше: новые таблицы существуют, число users сохранено.
Просмотрите новые ограничения без чтения содержимого документов:

```bash
dc exec -T postgres sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT conrelid::regclass AS table_name, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid IN ('documents'::regclass, 'document_text_versions'::regclass,
                  'document_ingestion_jobs'::regclass)
ORDER BY 1, 2;
SQL
```

## MinIO: отдельная явная процедура

Существующий `minio-init` по-прежнему запускает только прежний MLflow init.sh.
Новый Python script подключён read-only mount; Compose startup его не вызывает.
Образ client уже содержит Python, пересборка MinIO не требуется.
Script использует существующий внутренний MinIO `http://minio:9000` для управления;
изменённый TLS/внешний endpoint требует отдельной процедуры provisioning.

Только после проверки новых имён, secrets и backup:

```bash
dc run --rm --no-deps --entrypoint python minio-init /bootstrap/init-documents.py provision
```

Перед изменениями script читает списки buckets/users/policies. При любом совпадении
отказывается перезаписывать ресурс. Повторный provision после успеха намеренно
завершается отказом; повторную проверку выполняйте через storage check ниже.
При частичном сбое новые ресурсы могут остаться: автоматического rollback/удаления
нет. Сначала исследуйте состояние, не меняйте MLflow и не повторяйте bootstrap вслепую.
Секреты не печатаются; операция создания user передаёт secret в argv mc внутри
одноразового контейнера, поэтому административный доступ к контейнерам должен быть ограничен.

Policy разрешает только ListBucket/GetBucketLocation для document bucket и
GetObject/PutObject/AbortMultipartUpload для его `users/*`. DeleteObject, управление
IAM и доступ к MLflow не предоставляются. Bucket создаётся с private default.

Проверка соединения и scoped credentials, без записи:

```bash
dc run --rm --no-deps backend python scripts/check_document_storage.py
```

Ожидается PASS: scoped listing document bucket разрешён, listing MLflow bucket
и anonymous listing запрещены. В MinIO Console отдельно проверьте private policy
и единственную привязку policy к новому user. Read-only проверка не доказывает
отсутствие public GetObject; это дополнительно проверяется синтетическим round-trip.

Явная проверка загрузки, чтения, stream, SHA-256 и anonymous GetObject:

```bash
dc run --rm --no-deps backend python scripts/check_document_storage.py --write-synthetic
```

Создаётся один маленький синтетический объект с новым UUID, его ключ выводится
для проверки в Console. Это байты для проверки storage, не полноценный PDF и
не медицинские данные. Объект остаётся; SQL-запись не создаётся. Не используйте
реальные документы, удаления и команды очистки. Ожидается PASS без раскрытия secrets.

## Регрессия и приёмка

```bash
dc up -d --no-deps --wait --wait-timeout 240 backend worker
dc ps -a
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
curl --fail http://localhost:5000/health
```

Backend/worker должны остаться healthy. Проверьте прежний вход, профиль и выход
в браузере; MLflow UI должен показывать прежние runs/Registry и скачивать прежний
артефакт. Health endpoints сами не проверяют новую схему или storage.
Новые HTTP-страницы/загрузка через UI в 3.1 отсутствуют по заданию.

Оставшиеся ограничения: unit-тесты и проверки не запускались агентом; реальная
совместимость SDK/mc и schema drift требуют ручной проверки. Multi-user ACL
в будущих endpoints, транзакция PostgreSQL–MinIO, retries/outbox, очистка orphan
объектов и обработка файлов будут реализованы в следующих подэтапах после приёмки.

## Подэтапы 3.2–3.3: реализованный код, ещё не принят

Добавлены endpoints (в браузере с `/api`):

| Endpoint | Контракт |
| --- | --- |
| POST /documents | multipart file PDF/DOCX; сессия, CSRF, UUID Idempotency-Key; 202 с документом |
| GET /documents?offset=0&limit=25 | Только свои документы, total и пагинация, limit <=100 |
| GET /documents/{id} | Свои метаданные и последняя версия normalized_text/locators |
| GET /documents/{id}/download | Только свой оригинал, attachment/no-store/nosniff; поток с гарантированным close |
| POST /documents/{id}/retry | Сессия/CSRF, новая версия для failed; 409 при недопустимом состоянии, 429 при исчерпании квоты |

Нет удаления, OCR, TXT/JPG/PNG, LLM/RAG/Qdrant-функций. Исходное расширенное
задание сохранено в CODEX_TASKS, текущий объём ограничен последующими поручениями.
Приватные страницы: /documents, /documents/upload, /documents/{id}.
Список обновляется каждые 4 секунды при активных документах. При сетевой ошибке
можно обновить список вручную; после 401 требуется новый вход. Неуспешная загрузка
сохраняет Idempotency-Key до смены выбранного файла; автоматического POST retry нет.
Извлечённый текст показывается как текст, HTML/Markdown не исполняется.

### Надёжность и постоянные данные

Новые миграции не нужны. 0001_auth и 0002_documents сохранены без изменений.
Оригинал резервируется в PostgreSQL до MinIO PUT, с pending outbox в той же
транзакции. При потере ответа MinIO API возвращает 503, но известный оригинал
восстанавливается worker после grace. Повтор POST с тем же ключом не создаёт
второй объект. Прерванная до записи загрузка завершается UPLOAD_INCOMPLETE;
нужна новая загрузка с новым ключом. Существующие объекты/данные не удаляются.

Dispatcher в существующем worker проверяет outbox, публикует ID в существующий
Redis и восстанавливает недоставленные/просроченные задания. Возможна повторная
доставка, поэтому обработка защищена статусом, версией, lease UUID и unique index.
Поздний результат истёкшего lease не сохраняется. Автоматические повторы ограничены,
пауза растёт до 300 секунд. После исчерпания — failed и явный retry (в пределах
числа версий). Попытка резервируется до публикации: ошибки брокера, потерянные
доставки и обработка входят в общий budget, без двойного подсчёта при получении.
`document_outbox_unavailable` в worker log означает необходимость проверить БД/схему;
health ping Celery не доказывает работоспособность dispatcher.

**Запуск нового worker начинает запись статусов/outbox/извлечённого текста
для существующих pending заданий. Загрузка записывает PostgreSQL и MinIO;
retry добавляет задание.** Скачивание и просмотр данные не меняют.
Backend/worker не выполняют миграции, provisioning или очистку при старте.

### Лимиты и настройки

Новые optional переменные добавлены в .env.example и Compose с defaults;
реальный .env не изменён. Для стандартной конфигурации правки не требуются.

| Переменная | Default |
| --- | --- |
| DOCUMENTS_MAX_PAGES | 200, применяется к PDF |
| DOCUMENTS_MAX_TEXT_CHARS | 1000000 Unicode code points |
| DOCUMENTS_PROCESSING_SECONDS | 60, download/проверка/парсер; Celery hard timeout +60 секунд |
| DOCUMENTS_MAX_UNPACKED_BYTES | 83886080 для DOCX |
| DOCUMENTS_MAX_ATTEMPTS | 3, автоматические попытки обработки/ошибки публикации |
| DOCUMENTS_MAX_VERSIONS | 5, включая исходную обработку |
| DOCUMENTS_MAX_ACTIVE | 10 uploading/queued/processing на владельца |
| DOCUMENTS_MAX_PER_USER | 1000 документов на владельца |
| DOCUMENTS_UPLOAD_GRACE_SECONDS | 300, восстановление uploading; также timeout получения тела запроса |
| DOCUMENTS_OUTBOX_INTERVAL | 5 секунд |
| DOCUMENTS_OUTBOX_ENABLED | true; false используется только для управляемых интеграционных тестов |

Исходный DOCUMENTS_MAX_BYTES=20971520 сохранён; multipart overhead <=65536 байт.
Проверяется фактический размер тела независимо от Content-Length и размер файла;
multipart headers дополнительно ограничены 16 KiB на часть до staging.
DOCX: максимум 2048 ZIP entries и 10000 текстовых блоков, DTD/entities/макросы
отклоняются. PDF: максимум страниц, символов и время. На Linux parser ограничен
768 MiB address space и CPU. На Linux parser также привязан через
parent-death signal к Celery process: при hard timeout/падении родителя он завершается.
Размер output file ограничен, core dumps отключены. В environment parser не передаются
PostgreSQL/Redis/MinIO credentials; это не заменяет sandbox, поскольку процесс
всё ещё работает с тем же Unix UID. HTTP staging/ZIP directory/API процессы требуют
общих production resource limits отдельно. Внешний reverse proxy должен иметь
согласованные body/time/concurrency limits. Прямой Windows запуск не обеспечивает
Linux RLIMIT; для приёмки используется существующий Docker Linux runtime.

### Зависимости и совместимость

Backend: python-multipart 0.0.20, PyMuPDF 1.26.6, python-docx 1.2.0.
Starlette закреплён на 0.49.3 внутри разрешённого FastAPI 0.121.1 диапазона:
так adapter cleanup и constructor multipart проверяются против фиксированного API,
а не случайной transitive версии. Сам FastAPI не обновлён.
Frontend: Tailwind/@tailwindcss/postcss 4.1.17, PostCSS 8.5.6,
Radix Dialog/Slot, CVA, clsx, tailwind-merge, lucide-react; test dependencies
Testing Library/user-event/jest-dom/jsdom. Точные версии — package.json.
Next.js 16.4.5 и React 19.2.7 сохранены. Новых production-сервисов нет.

shadcn/ui components размещены в components/ui, адаптированы к приложению;
components.json сохраняет aliases и Tailwind CSS variables. CLI не запускался.
Dev Compose теперь монтирует components и lib; зависимости/PostCSS config
обновляются пересборкой образа, без удаления прежних node_modules volumes.

PyMuPDF использует AGPL/commercial licensing: до распространения/production
нужен выбранный применимый вариант. OCR и внешние model API не вызываются.
Примеры компонентов и конфигурации сверены с официальными источниками:
[shadcn Button](https://ui.shadcn.com/docs/components/radix/button),
[shadcn Sheet](https://ui.shadcn.com/docs/components/radix/sheet),
[Tailwind Next.js](https://tailwindcss.com/docs/installation/framework-guides/nextjs),
[python-docx Document](https://python-docx.readthedocs.io/en/latest/api/document.html),
[Celery 5.5.3](https://docs.celeryq.dev/en/v5.5.3/userguide/extending.html).
Multipart adapter сверяется с [Starlette 0.49.3](https://github.com/encode/starlette/blob/0.49.3/starlette/formparsers.py).

### Команды проверки 3.2–3.3 — Git Bash

Перед запуском: рабочие PostgreSQL/MinIO/Redis этапа 1 и схема 0002_documents
должны существовать; scoped bucket/credentials 3.1 должны быть настроены.
Сохраните backup и проверьте отдельное восстановление по инструкции 3.1 выше.
Если lock-файлы созданы локально, сначала обновите их под новые зависимости;
не удаляйте lock для обхода ошибок установки. Команды разрешения Python зависимостей
есть в OPERATIONS.md. Для Frontend npm install выполняет пользователь, если
необходимо обновить package-lock.json после просмотра package.json.

При наличии устаревшего Frontend lock-файла обновите его вручную до Docker build:

```bash
(
  cd frontend
  npm install --package-lock-only
)
```

Это разрешение зависимостей и запись lock-файла, без изменения рабочих БД/MinIO.

```bash
cd /c/workplace/Medical_AI_New
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }

dc config --quiet
docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml config --quiet
dc build backend worker frontend

dc run --rm --no-deps backend python -m ruff check .
dc run --rm --no-deps backend python -m ruff format --check .
dc run --rm --no-deps backend python -m mypy app
dc run --rm --no-deps backend python -m pytest -m 'not integration'
dc run --rm --no-deps frontend npm run lint
dc run --rm --no-deps frontend npm run typecheck
dc run --rm --no-deps frontend npm test

dc run --rm --no-deps backend alembic current
dc run --rm --no-deps backend alembic check
dc run --rm --no-deps backend python scripts/check_document_storage.py
```

Ожидается 0002_documents, отсутствие schema drift и успешные проверки.
Если текущая ревизия 0001_auth — остановитесь: после backup/проверки конфликтов
отдельно выполните только ранее подготовленную миграцию:

```bash
dc run --rm --no-deps backend alembic upgrade 0002_documents
dc run --rm --no-deps backend alembic current
dc run --rm --no-deps backend alembic check
```

Не повторяйте MinIO provisioning. Затем, учитывая запись outbox/статусов worker:

```bash
dc up -d --no-deps --wait --wait-timeout 240 backend worker frontend
dc ps -a
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
curl --fail http://localhost:5000/health
```

### Изолированные интеграционные тесты

Новый Compose не наследует рабочий, не публикует порты и не подключает рабочие
volumes. Состояние PostgreSQL/MinIO/Redis хранится в test-only tmpfs; credentials
синтетические. Test fixture явно создаёт только test bucket и применяет миграции
к medical_ai_documents_test. Production bootstrap scripts не вызываются.
Worker настоящий; его dispatcher отключён, чтобы тесты управляли сбоями и публикацией
через тот же dispatch_once. Отдельный тест отправляет задачу в настоящий Redis/Celery
и ожидает результат настоящего parser subprocess. Fake storage не заменяет этот тест.

```bash
tc() { docker compose -p medical-ai-stage3-tests -f infrastructure/compose.documents-test.yaml "$@"; }
tc config --quiet
tc build
tc up -d --wait --wait-timeout 240 postgres redis minio worker
tc run --rm tests
```

Запускать только с этим test Compose. RUN_DOCUMENT_INTEGRATION вне него не включать.
Тесты проверяют auth/CSRF, чужие ID/списки/скачивание/retry, повтор Idempotency-Key,
подмену файла, размер, реальную обработку PDF, потерянный MinIO acknowledgement,
backoff, дублирование задачи, lease fencing, восстановление истёкшей попытки,
ограничение ошибок публикации и ручной retry. Старые auth integration tests сохранены.
Ожидается exit code 0 без skips document integration tests. CI добавляет этот же
test job; агент его не запускал. Production topology и MLflow не меняются.

### Приёмка браузером и API

1. Войдите на localhost:3000; проверьте sidebar на desktop и мобильный Sheet:
   открытие, Escape, возврат фокуса, текущий раздел.
2. Откройте /documents: пустое состояние, переход к загрузке. Загрузите синтетические
   PDF с текстом и DOCX с абзацами/таблицей. Должны появиться queued/processing/ready,
   текст и локаторы; оригинал скачивается без изменения SHA-256.
3. Проверьте near-limit файл через браузерный /api proxy, не только прямо :8000.
   Внешний proxy и текущая версия Next.js могут накладывать дополнительные лимиты.
4. Скан/пустая страница PDF дают needs_ocr, без ложного сообщения о полном OCR;
   пароль/повреждённый документ/лимиты дают понятную ошибку. DOCX не получает
   выдуманные номера страниц; headers/footnotes/textboxes и сложные вложенные
   конструкции не гарантируются python-docx body extraction этой версии.
5. Попробуйте retry failed, затем повторную кнопку для queued: вторая операция
   запрещена; число версий ограничено. OCR_REQUIRED не имеет processing retry.
6. Во втором аккаунте список независим, чужие GET/details/download/retry дают 404.
   Без сессии — 401, без CSRF/с чужим Origin для POST — 403.
7. При сетевом сбое загрузки проверьте список; повтор того же файла без смены
   выбора использует тот же ключ и не создаёт копию. После обновления страницы
   ключ формы теряется: перед новой загрузкой проверьте список.
8. Проверьте прежнюю авторизацию/выход и прежние MLflow runs/Registry/артефакты.

Риски: проверки не запускались агентом; нужны ручная сборка, lint/typecheck,
integration и браузерная приёмка. Адаптер multipart cleanup использует внутренний
список файлов Starlette и покрыт lifecycle tests — при обновлении framework
требуется повторная проверка. Нет антивирусного сканирования и полноценного
песочничного запрета сети для native parser; CPU/память/timeout не заменяют
production security isolation. Backend storage credentials доверенные и имеют
доступ к bucket всех владельцев; end-user ACL обеспечивается API. Нет автоудаления
оригиналов/незавершённых multipart uploads. Клиентское скачивание использует Blob,
поэтому требует памяти под файл; Backend передаёт оригинал потоком.

После успешной ручной проверки требуется явное подтверждение этапа 3.
До него не начинать этап 4.
