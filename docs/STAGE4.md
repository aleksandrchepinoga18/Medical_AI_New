# Этап 4 — персональный RAG: код подготовлен, ручная приёмка ожидается

Codex не запускал Docker, тесты, линтеры, typecheck, миграции, bootstrap, npm или Git.
Рабочий `.env` не читался и не изменялся. Все команды ниже выполняет пользователь
в Git Bash. При ошибке остановитесь; не продолжайте следующий блок автоматически.
[Список файлов](STAGE4_IMPLEMENTATION_FILES.md).

Выполнены только проверки чтением: AST-разбор 50 Python-файлов без импорта приложения
и исполнения тестов, разбор JSON evaluation, согласованность 15 RAG-переменных
между Settings/Compose/example и наличие 35 путей из списка изменений. Эти проверки
не подтверждают поведение сервисов, форматирование, типы или прохождение тестов.

## Архитектура и границы

```text
PDF/DOCX → существующее извлечение → DocumentTextVersion + RagIndexJob (одна SQL-транзакция)
  → существующий outbox/Redis/Celery → чанки → локальный Ollama /api/embed
  → Qdrant upsert → очистка старых поколений → публикация ready-поколения в PostgreSQL

Сессия пользователя → POST /rag/search → актуальные ready-поколения из PostgreSQL
  → embedding запроса → Qdrant с user_id + generation + fingerprint + pipeline
  → повторная проверка SQL-владельца/версии/статуса → фрагменты SQL и источники
```

Нет генерации ответа, чата, медицинской интерпретации, веб-поиска, LangGraph,
BM25 или reranker. Adapters отделены от retrieval и indexing для будущих расширений.
MinIO, исходные документы и статусы извлечения при индексации не изменяются.

Новая аддитивная миграция `0003_personal_rag` после `0002_documents` создаёт только
`rag_chunks` и `rag_index_jobs` с FK RESTRICT. Downgrade запрещён для сохранения данных.
Миграции и backfill автоматически при старте не выполняются.

Chunking использует Unicode-символы, не токены: размер/overlap задаются отдельно.
UUIDv5 чанка зависит от text_version_id, алгоритма, конфигурации и точных offsets.
Сохраняются user_id, document_id, text_version_id, chunk_id, ordinal, offsets,
текст и пересекающиеся page/block locators. Qdrant хранит вектор и служебный payload,
а возвращаемый пользователю текст читается из PostgreSQL.

Index job уникален для версии текста. Статусы: pending, published, processing,
ready, failed, deleted; операция index/delete. Ошибки индексации не меняют
Document.status. Backoff экспоненциальный до 300 секунд, budget ограничен.
Dispatcher резервирует попытку до отправки; потерянная доставка и умерший worker
восстанавливаются после lease. Session advisory lock PostgreSQL не допускает двух
живых обработчиков одной версии. Жёсткий Celery timeout освобождает соединение при гибели процесса.

Каждая попытка получает отдельное поколение; point UUIDv5 зависит от chunk UUID
и поколения. Дубликат доставки не начинает вторую обработку. Публикация выполняется
только действующим lease. Document-wide cleanup сериализован блокировкой документа;
фильтр удаления содержит pipeline + user_id + document_id. Инициализация коллекции
ничего не удаляет. Существующая коллекция проверяется по dimension/Cosine,
несовместимые payload indexes отклоняются; недостающие keyword indexes добавляются.

При reindex прежнее поколение сразу скрывается, затем загружается новое. Поиск
доступен только для последней версии текста и Document.status=ready. Проверяются
fingerprint конфигурации и текущее SQL-поколение, поэтому частичные/устаревшие точки
не участвуют в поиске. Возможен период отсутствия документа в выдаче при reindex.

## API и UI

| Endpoint | Назначение |
| --- | --- |
| POST `/rag/search` | `{ "query": "...", "k": 5 }`, query 1–2000 символов, k <= configured max |
| GET `/rag/documents/{id}/index` | Статус последней версии; not_indexed/outdated/unavailable вычисляются дополнительно |
| POST `/rag/documents/{id}/index` | Первая индексация или reindex; 202; активная операция даёт 409 |
| DELETE `/rag/documents/{id}/index` | Исключение из поиска и фоновое удаление векторов; оригинал/SQL-текст сохраняются |

Все endpoint требуют авторизованную сессию. POST/DELETE, включая поиск, используют
существующую проверку Origin и session-bound CSRF. user_id берётся исключительно
из сессии; дополнительные поля поискового JSON запрещены. Чужой document ID даёт
404. Ответы не кешируются. Query передаётся в теле, не URL. Не включайте логирование
HTTP request bodies, embeddings или извлечённого текста во внешнем proxy.

Next.js: `/search`, навигация «Поиск», результаты с фрагментами, score и page/block;
ссылки открывают якоря извлечённого текста в `/documents/{id}`. У документов есть
независимая панель готовности к поиску, reindex и исключение из поиска. HTML из
документов отображается как текст. Существующий `/api` proxy и private layout сохранены.

## Новые переменные окружения

| Переменная | Default / назначение |
| --- | --- |
| RAG_ENABLED | false; включить вручную после подготовки |
| RAG_CHUNK_CHARS | 1200, диапазон 100–8000 |
| RAG_CHUNK_OVERLAP | 200, 0–4000 и строго меньше размера чанка |
| RAG_OLLAMA_URL | http://ollama:11434 |
| RAG_EMBEDDING_MODEL | bge-m3:567m |
| RAG_EMBEDDING_DIGEST | пусто; рекомендуется полный SHA-256 из /api/tags до первой индексации |
| RAG_EMBEDDING_DIMENSIONS | 1024; каждый фактический вектор проверяется |
| RAG_EMBEDDING_BATCH_SIZE | 8, диапазон 1–64 |
| RAG_HTTP_TIMEOUT | 60 секунд на запрос, диапазон 1–300 |
| RAG_INDEX_SECONDS | 900 секунд на попытку, диапазон 30–7200; hard timeout +60, lease +120 |
| RAG_MAX_ATTEMPTS | 5, диапазон 1–10 |
| RAG_QDRANT_URL | http://qdrant:6333 |
| RAG_COLLECTION | medical_ai_personal_v1; выбрать отдельное свободное имя |
| RAG_QDRANT_API_KEY | пусто для существующего внутреннего Qdrant; SecretStr, не выводить |
| RAG_SEARCH_MAX_K | 20, диапазон 1–50 |

Default RAG_ENABLED=false сохраняет прежний pipeline. Для обработки индексации
нужен также существующий DOCUMENTS_OUTBOX_ENABLED=true. При включении RAG новые
ready-версии получают job в транзакции извлечения. Для старых документов есть явный
backfill; существующие ready/failed/deleted jobs он не сбрасывает.

Model/digest/dimension/chunk settings входят в fingerprint. Изменение требует
reindex. При смене размерности выберите новую коллекцию; существующую не пересоздавайте.
При непустом digest адаптер сверяет локальную модель через /api/tags перед embedding.
Пустой digest не защищает от замены содержимого model tag.

Разрешены только локальные имена endpoint: ollama, qdrant, host.docker.internal,
localhost, 127.0.0.1. URL credentials, proxy environment и HTTP redirects не используются.
Cloud model names отвергаются. Нет fallback и автоматической загрузки модели.

## Ручная подготовка и проверки — Git Bash

Сохраните прежний COMPOSE_PROJECT_NAME, имена БД и volumes. Не копируйте example
поверх `.env`. Существующие PostgreSQL/Redis/MinIO/Qdrant должны быть доступны;
документы этапа 3 и его scoped MinIO credentials должны быть настроены.

Development-функции ниже; для production замените compose.dev.yaml на
compose.production.yaml во всех трёх функциях и сохраните существующие HTTPS/auth settings.

```bash
cd /c/workplace/Medical_AI_New
rc() {
  docker compose --env-file .env -f infrastructure/compose.yaml \
    -f infrastructure/compose.dev.yaml -f infrastructure/compose.rag.yaml "$@"
}
rd() {
  docker compose --env-file .env -f infrastructure/compose.yaml \
    -f infrastructure/compose.dev.yaml -f infrastructure/compose.rag.yaml \
    -f infrastructure/compose.rag-download.yaml "$@"
}
rt() {
  docker compose -p medical-ai-stage4-tests \
    -f infrastructure/compose.rag-test.yaml "$@"
}

rc config --quiet
rd config --quiet
rt config --quiet
rc ps -a
rc build backend worker frontend
```

Новые Python/npm зависимости не нужны. Для lint/unit используйте development-образы:

```bash
rc run --rm --no-deps backend python -m ruff check .
rc run --rm --no-deps backend python -m ruff format --check .
rc run --rm --no-deps backend python -m mypy app
rc run --rm --no-deps backend python -m pytest -m 'not integration'
rc run --rm --no-deps frontend npm run lint
rc run --rm --no-deps frontend npm run typecheck
rc run --rm --no-deps frontend npm test
```

Ожидается exit code 0. Проверки агентом не выполнялись, совместимость не подтверждена.
В production-конфигурации runtime-образы не включают инструменты разработки;
эти проверки выполняйте с development override или отдельным test Compose.

## Изолированная интеграция и evaluation

Этот Compose самостоятельный: без рабочих env-файлов, портов, named volumes или
рабочих сетей. PostgreSQL/Redis/Qdrant/Ollama используют tmpfs. Загрузка модели
потребует сети, нескольких ГБ диска/RAM и времени CPU. Не переносите команду pytest
с RUN_RAG_INTEGRATION в рабочее окружение: fixture сама применяет миграции только
к medical_ai_rag_test и проверяет имена тестовых ресурсов до подключения.

```bash
rt build
rt up -d --wait --wait-timeout 300 postgres redis qdrant ollama
rt exec ollama ollama pull bge-m3:567m
rt up -d --wait --wait-timeout 300 worker
rt run --rm tests
rt run --rm tests python -m pytest -m integration \
  tests/test_rag_integration.py::test_synthetic_retrieval_evaluation -s
```

Ожидаются успешные tests без skips и JSON с dataset, model, полным digest,
dimensions, k, recall, mrr, ndcg. Сохраните JSON для сравнения повторных запусков.
Набор: `backend/evaluation/personal_rag.json`, 6 коротких синтетических документов
и 6 вопросов на русском/английском. Relevance оценивается на уровне документов,
повторы документов в ranking удаляются с сохранением первого места.

Recall@K = доля найденных релевантных документов; MRR@K = reciprocal rank первого
релевантного; nDCG@K использует gain=2^grade-1 и log2(rank+1). Усреднение macro.
Предварительные пороги fixture: Recall@3 >=0.8, MRR@3 >=0.7, nDCG@3 >=0.7.
Это критерии будущей проверки, а не полученные измерения. Набор не является
клиническим benchmark. Для воспроизводимости сравнивайте одинаковые model digest,
image versions, chunk config и dataset version.

Покрыты реальные Celery/Redis доставки, дубли, PostgreSQL advisory lock, потерянная
доставка, ошибки брокера/Ollama/Qdrant, устаревшая попытка, повторная индексация,
физическое удаление точек, смена text version, SQL/HTTP owner isolation, CSRF и
транзакционный hook извлечения. Сам extractor в hook-тесте подменён синтетическим
результатом; настоящие PDF/DOCX проверяет существующий stage 3 integration suite:

```bash
docker compose -p medical-ai-stage3-tests \
  -f infrastructure/compose.documents-test.yaml build
docker compose -p medical-ai-stage3-tests \
  -f infrastructure/compose.documents-test.yaml up -d --wait \
  --wait-timeout 300 postgres redis minio worker
docker compose -p medical-ai-stage3-tests \
  -f infrastructure/compose.documents-test.yaml run --rm tests
```

## Backup и явная аддитивная миграция

После проверки кода/тестов сохраните backup PostgreSQL и проверьте отдельное
восстановление. Для существующих ценных коллекций Qdrant заранее сохраните snapshot
вне его volume. Перед backfill/reindex/deletion нужен backup и PostgreSQL, и Qdrant:
их изменения не объединены общей транзакцией.

```bash
mkdir -p backups
rag_backup="backups/before-stage4-$(date +%Y%m%d-%H%M%S).dump"
rc exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$rag_backup"
test -s "$rag_backup"
rc exec -T postgres pg_restore --list < "$rag_backup" > /dev/null
rc run --rm --no-deps backend alembic current
```

Ожидается `0002_documents` или уже `0003_personal_rag`. При другой ревизии,
конфликтующих таблицах или неизвестном состоянии остановитесь. Не используйте stamp.
Успешный pg_restore --list не заменяет отдельное восстановление backup.

Для существующего single-node Qdrant без API key можно создать полный snapshot:

```bash
rc exec -T qdrant curl --fail --silent --show-error \
  -X POST http://127.0.0.1:6333/snapshots
```

Это явная запись backup-файла в Qdrant, не изменение точек. Скопируйте `name` из
ответа в переменную и выгрузите архив за пределы volume:

```bash
rag_snapshot='ИМЯ_ИЗ_ОТВЕТА.snapshot'
rc exec -T qdrant curl --fail --silent --show-error \
  "http://127.0.0.1:6333/snapshots/$rag_snapshot" > "backups/$rag_snapshot"
test -s "backups/$rag_snapshot"
```

Проверьте восстановление отдельно. Если Qdrant уже настроен с API key, используйте
свою защищённую процедуру snapshot, не вставляя секрет в команды/вывод. Убедитесь,
что snapshot успешно выгружен, прежде чем продолжать операции с рабочими индексами.

После backup и проверки состояния:

```bash
rc run --rm --no-deps backend alembic upgrade 0003_personal_rag
rc run --rm --no-deps backend alembic current
rc run --rm --no-deps backend alembic check
```

Ожидается 0003_personal_rag и отсутствие schema drift. FK требуют блокировок
PostgreSQL: применяйте миграцию в согласованное окно. Bootstrap MinIO повторять не нужно.

## Локальная модель и включение

Ollama — единственный новый сервис, необходимый для локальных embeddings.
Существующий Qdrant используется повторно. Новый volume ollama-models хранит модель.
Порты Ollama/Qdrant наружу не публикуются. Обычный Ollama подключён только к
внутренней сети vectors; временный download override добавляет management с egress.

```bash
rd up -d --no-deps --wait --wait-timeout 300 ollama
rd exec ollama ollama pull bge-m3:567m
rc up -d --no-deps --force-recreate --wait --wait-timeout 300 ollama
rc run --rm --no-deps backend python -c \
  'import json,urllib.request; r=json.load(urllib.request.urlopen("http://ollama:11434/api/tags")); print(json.dumps([{k:m[k] for k in ("name","digest")} for m in r["models"]]))'
```

Последняя команда выводит только имена и SHA-256 моделей, без env/секретов.
Не оставляйте download override в обычной команде запуска. Данные модели сохраняются
при пересоздании контейнера. Не выполняйте down -v или очистку volumes.

Теперь вручную добавьте новые переменные из таблицы в существующий `.env`:
RAG_ENABLED=true, полный RAG_EMBEDDING_DIGEST найденной bge-m3:567m и остальные defaults.
При existing collection выбирайте отдельное имя; не направляйте cleanup на чужую коллекцию.
DOCUMENTS_OUTBOX_ENABLED должен оставаться true. Проверьте, что shell environment
не переопределяет эти переменные; не выводите секреты и полный Compose config.

```bash
rc config --quiet
rc run --rm --no-deps backend python scripts/backfill_rag.py schedule
rc up -d --no-deps --wait --wait-timeout 300 backend worker frontend
rc ps -a
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
```

Backfill создаёт SQL jobs для прежних ready-документов; новый worker начнёт их
обрабатывать и писать в Qdrant. Коллекция/keyword indexes создаются при первой
индексации, если отсутствуют. При несовпадении dimension/Cosine — отказ, без recreate.
Backend health/Celery ping не доказывают работоспособность RAG. Для статуса и поиска
используйте авторизованный UI/API с CSRF; curl liveness выше — только регрессия процесса.
Повторный backfill не сбрасывает failed/deleted/ready jobs; используйте UI для reindex.

## Критерии приёмки

1. Unit/lint/typecheck/frontend и оба isolated integration suite проходят, schema drift нет.
2. Два пользователя загружают синтетические PDF/DOCX; owner isolation сохраняется
   при одинаковом тексте, подстановке document ID и попытке передать user_id в поиск.
3. Извлечение завершается ready; отдельно видны pending/processing/ready индексации.
4. Поиск находит ожидаемые фрагменты, источник открывает правильную страницу/блок.
5. Дубликаты доставки и повторный reindex не дают повторов чанков; старые поколения
   и предыдущие text versions не доступны в поиске.
6. При недоступных Ollama/Qdrant исходный документ остаётся ready, index job получает
   retry/failed; после восстановления и reindex поиск снова работает.
7. «Убрать из поиска» немедленно исключает документ, затем удаляет его точки;
   оригинал, SQL-текст, документы других пользователей и коллекции сохраняются.
8. Несовместимая размерность/подменённый model digest дают безопасный отказ.
9. Синтетическая evaluation достигает порогов; отчёт сохраняет model digest.
10. Прежние авторизация, CSRF, загрузка/скачивание и MLflow остаются работоспособны.

## Риски и ограничения

- Нет общей транзакции PostgreSQL–Qdrant. Векторы незавершённых/умерших попыток
  могут физически остаться, но отфильтрованы по активному SQL-поколению. Следующая
  успешная индексация/операция удаления документа очищает их; глобального GC нет.
- Старые SQL chunks сохранены для аудита. Это дополнительные копии текста, которые
  должны учитываться в backup, хранении и будущем полном удалении данных.
- Reindex скрывает документ до успеха; это сознательный отказ от выдачи старого индекса.
- Финальная cleanup-операция держит блокировку документа и job во время HTTP запроса.
  Нужны наблюдение за задержками, лимитами пула PostgreSQL, CPU/RAM и timeout под нагрузкой.
- CPU-вариант Ollama может быть медленным. GPU/resource limits не настроены; при
  необходимости пользователь выбирает их отдельно, без изменения провайдера.
- Маленький eval — smoke-набор, не оценка медицинского качества. Score — cosine similarity,
  не вероятность, диагноз или подтверждение правильности текста.
- Нет reranker/BM25, search rate limit и глобальной квоты индексации; K/query/batch/time
  ограничены, но multi-tenant production требует отдельной нагрузочной приёмки.
- SQL-проверка выбирает актуальные поколения пользователя целиком. При очень большом
  числе документов нужен отдельный подход к масштабированию фильтра.
- Внутренний Qdrant прежнего Compose не требует ключа; доступ сервисов доверенный.
  HTTP владелец защищён сервером приложения, но это не изоляция от администратора Docker/БД.
- Ollama image закреплён по version tag, не digest; перед production нужны проверка
  безопасности образов и фиксация digest. Пустой model digest допускается только как
  явно известное ограничение; для приёмки закрепите его до первой индексации.
- Новые HTTP contracts сверены с официальными API, но реальные версии компонентов
  агентом не запускались. Требуются все ручные проверки, включая форматирование.

Контракты: [Ollama embed](https://docs.ollama.com/api/embed),
[Ollama tags](https://docs.ollama.com/api/tags),
[BGE-M3 model](https://ollama.com/library/bge-m3:567m),
[Qdrant 1.15 query](https://api.qdrant.tech/v-1-15-x/api-reference/search/query-points),
[Qdrant 1.15 payload index](https://api.qdrant.tech/v-1-15-x/api-reference/indexes/create-field-index).

Этап 4 не считается принятым до результатов пользователя и его явного подтверждения.
