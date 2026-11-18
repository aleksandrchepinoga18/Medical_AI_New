# Гибридный поиск по личным документам

## План изменения индекса и отката

В Dockerfile закреплён Qdrant `v1.15.5`. Текущая коллекция использует безымянный
dense-вектор BGE-M3 с метрикой Cosine. Фактическую версию работающего контейнера
и схему коллекции нужно подтвердить перед развёртыванием. Добавление нового
имени векторов к существующей коллекции через `create_vector_name` доступно
начиная с Qdrant 1.18. В 1.15.5 создаём отдельную sparse-коллекцию
`<RAG_COLLECTION>_bm25_v1` с `modifier: idf` в том же Qdrant. Query API работает
в пределах одной коллекции, поэтому приложение запускает обе ветки параллельно
и применяет RRF после проверки попаданий по PostgreSQL.

Перед включением сделайте резервные копии PostgreSQL и Qdrant. Backfill читает
сохранённые чанки только текущих опубликованных поколений и добавляет sparse-точки
с теми же идентификаторами. Старые dense-векторы и оригиналы документов сохраняются.
Чтобы откатиться, переключите backend и worker на `RAG_RETRIEVAL_MODE=dense`.
Sparse-коллекцию можно оставить для повторной приёмки.

[Гибридный поиск в Qdrant](https://qdrant.tech/documentation/search-tuning/hybrid-search/)

## Поиск и индексация

- `RAG_RETRIEVAL_MODE=dense` по умолчанию выполняет прежний BGE-M3/Cosine
  поиск в `RAG_COLLECTION`. `hybrid` запускает его параллельно с BM25.
  Настройка серверная. Тело `POST /rag/search` по-прежнему принимает `query` и `k`.
- Часть BM25 с частотой термина и нормировкой длины рассчитывается в Python
  (`k1=1.2`, `b=0.75`, фиксированная опорная длина 160 токенов). Qdrant
  применяет IDF и ищет по sparse-векторам через Query API. Токенизация сохраняет
  кириллицу, идентификаторы вроде `c.68_69delAG`, составные даты и единицы вроде
  `ммоль/л`. Десятичная запятая нормализуется в точку. Индекс хранит
  32-битные хеши токенов с примесью UUID пользователя и служебные поля.
  [Sparse-векторы и BM25](https://qdrant.tech/course/essentials/day-3/sparse-retrieval-demo/)
- Обе ветки используют одинаковые фильтры по pipeline, пользователю, fingerprint
  и текущим поколениям. После поиска PostgreSQL заново проверяет владельца,
  версию и статус документа и индекса, а также принадлежность чанка и UUID точки.
  Источники, страницы PDF, блоки DOCX и ссылки берутся из проверенных данных
  PostgreSQL.
- Каждая ветка запрашивает `k` чанков. RRF суммирует `1 / (60 + rank)`
  по веткам, где rank начинается с единицы. Повторы одного `chunk_id` внутри
  ветки учитываются однократно. При равенстве приоритет у порядка dense.
  Поле `score` в гибридной выдаче содержит балл RRF. Его шкала отличается от Cosine,
  который фронтенд сейчас подписывает как «Сходство».
- Перед лексическим поиском Qdrant считает точки по разрешённым поколениям.
  При неполном покрытии, недоступности BM25 или запросе без лексических токенов
  используется обычная dense-выдача с прежними баллами. Ошибка dense
  обрабатывается через существующий API. В предупреждениях
  `rag_bm25_fallback` и `rag_bm25_index_failed` записывается только код причины.
- После публикации dense-индекса worker в режиме `hybrid` синхронизирует
  sparse-точки под блокировкой строки документа. При повторной индексации
  и удалении индекса он очищает старые sparse-поколения. При сбое публикация
  dense остаётся в силе. Повторный backfill восстанавливает текущее поколение.
  Скрипт использует UPSERT с идентификатором `UUIDv5(chunk_id, generation)`.
  Каждый документ проверяется заново под блокировкой. Текущая схема разбиения
  определяет, какие сохранённые чанки относятся к опубликованному поколению.
  SQL-миграция отсутствует.

## Ручные проверки в Git Bash

Используйте имя и файлы рабочего Compose-проекта. Пример ниже соответствует
режиму разработки с BGE-M3 в Docker и Qwen через Windows API.

```bash
cd /c/workplace/Medical_AI_New
dc=(docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml -f infrastructure/compose.rag.yaml)

"${dc[@]}" build backend
"${dc[@]}" run --rm --no-deps backend python -m ruff check app tests scripts
"${dc[@]}" run --rm --no-deps backend python -m ruff format --check app tests scripts
"${dc[@]}" run --rm --no-deps backend python -m mypy app
"${dc[@]}" run --rm --no-deps backend python -m pytest -m 'not integration'

COMPOSE_DISABLE_ENV_FILE=1 docker compose -p medical-ai-chat-test \
  -f infrastructure/compose.chat-test.yaml run --build --rm tests
```

### Локальные интеграционные тесты с основной Ollama

Нужен Docker Compose 2.24.4 или новее. `worker` и `tests` обращаются к работающему
`medical-ai-ollama-1` по `http://ollama:11434` через отдельную внутреннюю bridge-сеть.
Порты на хосте не публикуются. PostgreSQL, Redis и Qdrant используют `tmpfs`
и подключены только к внутренней сети тестового проекта. Модель читается самим
рабочим сервером Ollama из его постоянного тома.

Один раз создайте сеть и подключите работающий контейнер с сетевым алиасом.
Если сеть уже создана и контейнер в ней, повторять эти два действия не нужно.
После пересоздания рабочего контейнера подключите его к сети снова.

```bash
docker compose version
docker inspect --format '{{.State.Running}}' medical-ai-ollama-1
docker network create --driver bridge --internal medical-ai-rag-inference-test
docker network connect --alias ollama medical-ai-rag-inference-test medical-ai-ollama-1
docker network inspect --format '{{.Driver}} {{.Internal}} {{range .Containers}}{{.Name}} {{end}}' \
  medical-ai-rag-inference-test
```

Ожидаются `true` для контейнера и `bridge true medical-ai-ollama-1` для новой
сети перед запуском тестов. При повторном использовании проверьте тип сети,
флаг `Internal` и список подключённых контейнеров. Если осталась запущенная
тестовая Ollama от старого проекта, её можно остановить отдельно.

```bash
COMPOSE_DISABLE_ENV_FILE=1 docker compose -p medical-ai-hybrid-rag-test \
  -f infrastructure/compose.rag-test.yaml stop ollama
```

```bash
rt() {
  COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_PROFILES= docker compose -p medical-ai-rag-local-tests \
    -f infrastructure/compose.rag-test.yaml \
    -f infrastructure/compose.rag-test-local-model.yaml "$@"
}
rt config --quiet
rt config --services
rt build
rt run --rm --no-deps tests python -c '
from app.db import get_settings
from app.rag.adapters import OllamaEmbeddings
s = get_settings()
e = OllamaEmbeddings(s)
models = e.http.request("GET", "/api/tags")["models"]
assert any(
    m.get("name") == s.rag_embedding_model and m.get("digest") for m in models
), s.rag_embedding_model
print(s.rag_embedding_model, len(e.embed(["Synthetic RAG integration readiness check."])[0]))
'
rt up -d --wait --wait-timeout 300 postgres redis qdrant worker
rt run --rm tests python -m pytest -q -m integration tests/test_rag_integration.py \
  -k 'not test_hybrid_retrieval_evaluation'
rt run --rm tests python -m pytest -s -q \
  tests/test_rag_integration.py::test_hybrid_retrieval_evaluation
rt down
docker network disconnect medical-ai-rag-inference-test medical-ai-ollama-1
```

В списке сервисов `rt config --services` ожидаются `postgres`, `redis`, `qdrant`,
`worker` и `tests`. Проверка из контейнера должна вывести `bge-m3:567m 1024`.
Далее ожидаются регрессии этапов 4–5 и JSON-отчёт evaluation. Локальные тесты
делят вычислительные ресурсы с рабочей Ollama. Для сравнения задержек запускайте
evaluation в период без параллельных запросов к ней. Если вы отключили рабочий
контейнер от тестовой сети, перед следующим запуском повторите `docker network connect`.

Для CI используйте только `compose.rag-test.yaml` по [инструкции этапа 4](STAGE4.md).
Этот вариант запускает собственную Ollama с временным хранилищем и требует
подготовить модель в CI. При необходимости повторите фронтенд-проверки по
[этапу 5](STAGE5.md).

## Evaluation

Сценарии в `backend/evaluation/hybrid_rag.json` определяют 15 синтетических
PDF/DOCX, 16 запросов, двух пользователей, градации релевантности и ложные совпадения
по датам и значениям. Тест генерирует файлы во временном каталоге, извлекает
текст штатными парсерами и использует PostgreSQL, две свежие коллекции Qdrant
и реальную BGE-M3. При fallback в hybrid тест завершится ошибкой.

- Для каждого `k` (3 и 5) каждый режим вызывает обычный retriever с этим
  лимитом. Recall, MRR и nDCG усредняются по запросам с релевантными документами.
  Метрики считаются по документам после дедупликации. nDCG использует оценки
  релевантности из набора.
- p50 и p95 измеряются после прогрева на трёх повторах каждого запроса с
  чередованием режимов. Измерение включает PostgreSQL, embedding, Qdrant и
  подготовку результатов. Генерация чата в измерение не входит.
- Для дат и значений отчёт отдельно показывает долю запросов с размеченным
  ложным совпадением на первом месте и среди первых `k` результатов. Для
  вопросов без релевантного документа измеряется доля непустых выдач.
  Попадание чужого документа проваливает тест.
- JSON содержит результаты обеих веток сравнения, разницу, ранги по каждому
  сценарию, предварительный вывод по метрикам ранжирования и digest модели.
  Сравнивайте также ложные совпадения и задержку. Равные или худшие показатели
  означают, что преимущество этого режима на данном наборе не подтверждено.
  Числовые результаты ожидают ручного запуска.

## Безопасное развёртывание

1. Завершите ручные тесты и сравните отчёт. Проверьте фактическую версию Qdrant,
   действующую схему и текущую ревизию БД до изменения индексов.

   ```bash
   "${dc[@]}" exec -T backend python -c '
   from app.db import get_settings
   from app.rag.adapters import QdrantVectors
   v = QdrantVectors(get_settings())
   print(v.http.request("GET", "/")["version"])
   p = v.inspect()["config"]["params"]
   print({k: p.get(k) for k in ("vectors", "sparse_vectors")})
   '
   "${dc[@]}" run --rm --no-deps backend alembic current
   ```

   Ожидаются Qdrant `1.15.5`, безымянный вектор Cosine с действующей
   размерностью BGE-M3 и ревизия `0004_chat`. При расхождении сначала
   проверьте рабочий проект, образ и конфигурацию.
2. В согласованное окно остановите backend и worker. Сохраните копии PostgreSQL
   и Qdrant вне Docker-томов. Следующие команды snapshot рассчитаны на штатный
   локальный Qdrant без API-ключа. Для защищённого экземпляра используйте
   действующую процедуру backup. Проверьте отдельное восстановление копий.

   ```bash
   "${dc[@]}" stop backend worker
   mkdir -p backups
   pg_copy="backups/before-hybrid-$(date +%Y%m%d-%H%M%S).dump"
   "${dc[@]}" exec -T postgres sh -c \
     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$pg_copy"
   test -s "$pg_copy"
   "${dc[@]}" exec -T postgres pg_restore --list < "$pg_copy" > /dev/null
   "${dc[@]}" exec -T qdrant curl -fsS -X POST http://127.0.0.1:6333/snapshots
   # Подставьте name из ответа Qdrant.
   snapshot='ИМЯ_ИЗ_ОТВЕТА.snapshot'
   "${dc[@]}" exec -T qdrant curl -fsS \
     "http://127.0.0.1:6333/snapshots/$snapshot" > "backups/$snapshot"
   test -s "backups/$snapshot"
   ```

3. Соберите backend и worker. Запустите явный backfill до переключения режима.
   Его можно повторить после прерывания. В случае ошибки можно возобновить
   приложение с `RAG_RETRIEVAL_MODE=dense`. Сверьте ненулевое число точек, если
   в рабочей системе уже есть успешно проиндексированные документы. Для
   загруженных документов без dense-индекса применяйте обычную индексацию
   [этапа 4](STAGE4.md). В режиме `hybrid` worker заполнит BM25 после публикации.

   ```bash
   export RAG_RETRIEVAL_MODE=dense
   "${dc[@]}" build backend worker
   "${dc[@]}" run --rm --no-deps backend python -m scripts.backfill_bm25 populate
   ```

4. Включите `hybrid` одновременно для backend и worker. Дальнейшие запуски
   Compose должны получить то же серверное значение. Режим Ollama для embeddings
   и генерации задаётся действующими настройками этапа 5.

   ```bash
   export RAG_RETRIEVAL_MODE=hybrid
   "${dc[@]}" up -d --no-deps backend worker
   "${dc[@]}" exec -T backend python -c 'from app.db import get_settings; print(get_settings().rag_retrieval_mode)'
   "${dc[@]}" exec -T worker python -c 'from app.db import get_settings; print(get_settings().rag_retrieval_mode)'
   ```

   Ожидается `hybrid` в обоих контейнерах. Проверьте на синтетических загрузках
   поиск, переиндексацию, удаление индекса, переход к странице PDF или блоку DOCX
   и источники в локальном стриминге Qwen. Второй пользователь должен видеть
   только свои документы. До приёмки достаточно переключиться обратно на dense.

   ```bash
   export RAG_RETRIEVAL_MODE=dense
   "${dc[@]}" up -d --no-deps backend worker
   ```

   После отката можно оставить sparse-коллекцию на месте. Перед повторным
   включением выполните backfill. Восстановление из snapshot требуется только
   при потере данных. В штатном откате достаточно смены режима.

## Почему чат прикрепляет PDF к общим вопросам

Сейчас чат запрашивает top-k для каждого сообщения и помещает все поместившиеся
выдержки и ссылки в контекст. Dense может вернуть ближайший PDF для произвольного
вопроса при наличии хотя бы одного документа. RRF определяет порядок относительно
кандидатов и также может присвоить первое место нерелевантному фрагменту. Формат
источников и логика чата в этом изменении сохранены.

Отдельное решение состоит из определения намерения запроса и проверки пригодности
выдержки в качестве доказательства. Общие вопросы должны отвечаться без личного
контекста, а для запроса о личных результатах следует оценивать кандидатов после
retrieval и разрешать пустую выдачу. Порог пригодности нужно подобрать на
размеченных общих, персональных и переформулированных вопросах с реальными
сложными негативами. Для точных запросов подтверждение должно учитывать
совпадение показателя, даты, значения и единицы в одном фрагменте. Оценивать
следует отдельно долю лишних ссылок и долю персональных вопросов, потерявших
достаточный источник. Балл RRF напрямую для такого порога не подходит.

## Ограничения и приёмка

- Набор синтетический. В нём нет оценки OCR, больших документов, таблиц с
  разорванными строками и клинической пригодности. Токенизация не выполняет
  лемматизацию и преобразование форматов дат или единиц измерения.
- Длина 160 токенов фиксирована, а Qdrant 1.15.5 использует общую статистику IDF
  коллекции. Хеши различаются по пользователям, хотя редкие 32-битные коллизии
  возможны. Фильтры Qdrant и проверка PostgreSQL ограничивают доступ к документам.
  [Изоляция статистики Qdrant](https://qdrant.tech/documentation/manage-data/multitenancy/)
- Backfill и синхронизация держат блокировку только на обрабатываемом документе.
  После сбоя очистки или периода работы в dense старые sparse-точки могут
  оставаться физически. Текущие SQL-фильтры исключают их из выдачи. Учитывайте
  sparse-коллекцию в процедурах хранения, backup и полного удаления.
- Для включения нужны успешные регрессии этапов 4–5, нулевые утечки между
  пользователями, подтверждённый fallback и корректные ссылки. Качество и
  задержку сравните с dense по отчёту и ручным сценариям. При отсутствии
  подтверждённого выигрыша оставьте `dense`.
