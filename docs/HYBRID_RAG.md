# Гибридный поиск по личным документам

## Совместимость Qdrant 1.15.5

Dockerfile закрепляет `qdrant/qdrant:v1.15.5`. В этой версии одна коллекция
поддерживает именованные dense и sparse-векторы. Query API принимает `prefetch`
и `{"fusion":"rrf"}`, REST-эквивалент `Fusion.RRF`. Backfill создаёт
`<RAG_COLLECTION>_hybrid_v2` с векторами `dense` (BGE-M3, 1024, Cosine)
и `bm25` (sparse, `modifier: idf`). Действующие `RAG_COLLECTION` и
`<RAG_COLLECTION>_bm25_v1` сохраняются. [Схема коллекций 1.15](https://api.qdrant.tech/v-1-15-x/api-reference/collections/create-collection) · [Query API 1.15](https://api.qdrant.tech/v-1-15-x/api-reference/search/query-points) · [Гибридный поиск](https://qdrant.tech/documentation/search/text-search/hybrid-search/)

При REST-запросе с `model: "qdrant/bm25"` Qdrant 1.15.5 сам токенизирует
текст и строит sparse-вектор. При индексации сервер вычисляет веса термов
и нормировку длины, при поиске применяет IDF по статистике коллекции.
Этот путь работает с отсутствующим адресом inference. FastEmbed также умеет
строить BM25-векторы локально в Python, а Qdrant Cloud предоставляет удалённый
inference. В проекте используется встроенная модель сервера через текущий
REST-адаптер. Название модели фиксировано в коде, дополнительные зависимости,
сервисы и скачивание модели для BM25 не требуются. BGE-M3 используется через
основную Docker Ollama, Qwen через действующий Windows Ollama API.
[Маршрутизация inference 1.15.5](https://github.com/qdrant/qdrant/blob/v1.15.5/src/common/inference/service.rs) · [Локальная модель 1.15.5](https://github.com/qdrant/qdrant/blob/v1.15.5/src/common/inference/local_model.rs) · [FastEmbed BM25](https://github.com/qdrant/fastembed/blob/v0.7.3/fastembed/sparse/bm25.py) · [Cloud Inference](https://qdrant.tech/documentation/cloud/inference/)

## Настройки BM25 для смешанных RU/EN документов

Локальный `qdrant/bm25` в 1.15.5 принимает параметры в `Document.options`:

| Параметр | Поддержка в 1.15.5 |
| --- | --- |
| `language` | Выбирает язык стандартного стеммера и списка стоп-слов. Без параметра используется английский. Значение `none` отключает выбор языковых настроек. |
| `tokenizer` | `word` по умолчанию. Доступны также `whitespace`, `prefix` и `multilingual`. |
| `stemmer` | Явное переопределение одним Snowball-стеммером, например `{"type":"snowball","language":"russian"}`. |
| `stopwords` | Один язык или объект с `languages` и `custom`. Можно объединить несколько списков стоп-слов. |

В этом API `multilingual` — значение `tokenizer`. Оно определяет разбиение
текста. Автоматического выбора Snowball-стеммера на уровне терма в BM25 1.15.5
нет. [REST-схема BM25](https://github.com/qdrant/qdrant/blob/v1.15.5/lib/api/src/rest/schema.rs#L153) · [Типы токенизаторов, стеммеров и стоп-слов](https://github.com/qdrant/qdrant/blob/v1.15.5/lib/segment/src/data_types/index.rs#L154) · [Построение векторов BM25](https://github.com/qdrant/qdrant/blob/v1.15.5/src/common/inference/bm25.rs)

```json
{"language":"none","tokenizer":"whitespace","lowercase":true,"avg_len":160.0}
```

Эти параметры применяются к чанку и запросу одной функцией. Стемминг и
исключение стоп-слов отключены. Qdrant разбивает текст по пробельным символам
и приводит термы к нижнему регистру. Перед отправкой в BM25 приложение
убирает ограниченный набор знаков по краям слов, например скобки вокруг
`(HbA1c)` или точку после `c.68_69delAG.`. Внутренние точки и подчёркивания
сохраняются. `BRAF V600E` даёт два самостоятельных терма. Анализ регистров и
пунктуации применяется только ко входу BM25. Текст источника, позиции и вход
BGE-M3 сохраняются. [Правила токенизации Qdrant](https://qdrant.tech/documentation/manage-data/indexing/#tokenizers)

BM25 сопоставляет формы слов по токенам. Переводы и совпадения по смыслу
обрабатываются веткой BGE-M3, обе ветки по-прежнему объединяет нативный RRF.
Для этой конфигурации подходит текущая версия Qdrant и первый backfill
`_hybrid_v2`. Если `_hybrid_v2` уже заполнена с предыдущими параметрами,
перед включением `hybrid` повторите backfill в режиме `dense`, чтобы обновить
sparse-векторы опубликованных поколений.

## Поиск и индексация

- `RAG_RETRIEVAL_MODE=dense` по умолчанию выполняет прежний BGE-M3/Cosine
  поиск в `RAG_COLLECTION`. `hybrid` запрашивает две ветки в новой коллекции.
  Настройка серверная. Тело `POST /rag/search` по-прежнему принимает `query` и `k`.
- Текст фрагмента передаётся локальному Qdrant в запросе построения вектора.
  Payload новой точки содержит только метаданные доступа и идентификаторы.
- Обе ветки используют одинаковые фильтры по pipeline, пользователю, fingerprint
  и текущим поколениям. После поиска PostgreSQL заново проверяет владельца,
  версию и статус документа и индекса, а также принадлежность чанка и UUID точки.
  Источники, страницы PDF, блоки DOCX и ссылки берутся из проверенных данных
  PostgreSQL.
- Каждая ветка запрашивает `k` кандидатов. Сервер объединяет результаты
  по ID точки и ранжирует их встроенным RRF с константой 2 и позициями от нуля.
  Прежний baseline использовал другую константу и порядок при равных баллах.
  В гибридной выдаче `score` содержит RRF, в dense — Cosine.
  [RRF в 1.15.5](https://github.com/qdrant/qdrant/blob/v1.15.5/lib/segment/src/common/reciprocal_rank_fusion.rs)
- Точное число гибридных точек сверяется с числом чанков в разрешённых
  поколениях. При расхождении, недоступности коллекции или ошибке Qdrant запрос
  возвращается к dense с уже вычисленным embedding. Интеграционная оценка
  требует успешного гибридного запроса. Журналы приложения содержат коды причин.
- До переключения режима backfill читает опубликованные поколения и сохранённые
  чанки под блокировкой строки документа. Он проверяет UUID и payload исходной
  точки, размерность и значения вектора, затем пишет оба вектора под тем же
  `UUIDv5(chunk_id, generation)` с `wait=true`. Повторный запуск идемпотентен
  и повторяет UPSERT уже обработанных пакетов. Чанки и документы остаются
  в PostgreSQL и MinIO. Новая SQL-миграция не нужна.
- После создания новой коллекции worker синхронизирует её при публикации
  поколения в любом режиме и убирает из неё прежние поколения документа.
  Сбой синхронизации оставляет dense доступным, повторный backfill восстанавливает
  покрытие. Явное удаление индекса удаляет точки документа из всех трёх
  коллекций. При ошибке применяется штатный повтор задания удаления.

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
set -o pipefail
rt run --rm -T tests python -m pytest -s -q \
  tests/test_rag_integration.py::test_hybrid_retrieval_evaluation | tee hybrid-v2.out
rt down
docker network disconnect medical-ai-rag-inference-test medical-ai-ollama-1
```

В списке сервисов `rt config --services` ожидаются `postgres`, `redis`, `qdrant`,
`worker` и `tests`. Проверка из контейнера должна вывести `bge-m3:567m 1024`.
Далее ожидаются регрессии этапов 4–5 и JSON-отчёт evaluation. Локальные тесты
делят вычислительные ресурсы с рабочей Ollama. Для сравнения задержек запускайте
evaluation в период без параллельных запросов к ней. Если вы отключили рабочий
контейнер от тестовой сети, перед следующим запуском повторите `docker network connect`.

Регрессии включают `test_hybrid_mixed_language_terms_and_cross_language_retrieval`.
Ожидается, что sparse-поиск найдёт `HbA1c`, `TSH`, `BRCA1`, `BRAF V600E`,
`MAP2K1`, `c.68_69delAG` и кириллическое название в смешанных DOCX.
Для `c.68_69delAC` и `68_69delAG` ожидается пустой sparse-результат.
На вопрос «Где указан гликированный гемоглобин?» ожидается первое место
англоязычного описания HbA1c через BGE-M3 и через гибридный запрос с источником.

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
- Для дат и значений отчёт отдельно показывает точность первого результата
  и долю запросов с размеченным ложным совпадением на первом месте и среди
  первых `k` результатов. Для
  вопросов без релевантного документа измеряется доля непустых выдач.
  Попадание чужого документа проваливает тест.
- JSON содержит результаты обеих веток сравнения, разницу, ранги по каждому
  сценарию, предварительный вывод по метрикам ранжирования и digest модели.
  Сравнивайте также ложные совпадения и задержку. Равные или худшие показатели
  означают, что преимущество этого режима на данном наборе не подтверждено.
  Числовые результаты ожидают ручного запуска.

Для сравнения с прежним Hybrid RAG сохраните JSON-строку из его
`test_hybrid_retrieval_evaluation` в `hybrid-baseline.json` в корне проекта.
Если отчёт ранее не сохранялся, получите его тем же тестом на отдельной копии
предыдущей реализации в синтетическом Compose-проекте. Набор запросов, digest,
разбиение, Qdrant и число повторов должны совпадать. Для сопоставимой latency
используйте ту же машину и сравнимую нагрузку.

Замените обычный запуск evaluation в блоке выше запуском с baseline ниже.
Для повторной оценки сначала выполните `rt down` и заново поднимите
синтетические сервисы командой `rt up` из блока выше. В JSON появится
`hybrid_minus_previous_hybrid` с дельтами качества, точности дат и чисел и p50/p95.

```bash
baseline_path="$(pwd -W)/hybrid-baseline.json"
MSYS_NO_PATHCONV=1 rt run --rm -T \
  -v "$baseline_path:/baseline.json:ro" -e RAG_EVALUATION_BASELINE=/baseline.json \
  tests python -m pytest -s -q tests/test_rag_integration.py::test_hybrid_retrieval_evaluation \
  | tee hybrid-v2-comparison.out
```

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
   При двух текущих ready-документах и 17 актуальных чанках ожидается
   `Current documents indexed: 2. Hybrid points verified: 17.` Сверьте результат
   с текущими поколениями в PostgreSQL, если набор успел измениться. Для
   проверки возобновляемости повторите команду и сверьте итог. При ошибке
   запустите сервисы в `dense` и проверьте код причины. Для документов без
   dense-индекса действует обычная [индексация этапа 4](STAGE4.md).

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

   Смена режима возвращает прежний dense-поиск. Коллекции и данные сохраняются.
   Перед повторным включением проверьте покрытие повторным backfill.

## Как чат проверяет источники и факты

- Общий вопрос, например «Как измеряют калий в анализе крови?», сразу
  передаётся Qwen. Вопрос с явной ссылкой на свой анализ, выписку, загруженный
  файл, извлечённый текст или личный показатель относится к личным документам.
  Если короткое уточнение «А у меня?» не называет показатель, чат просит
  уточнить вопрос. Для поиска в чате используется настроенный `RAG_SEARCH_MAX_K`.
  `/rag/search` по-прежнему выдаёт ранжированных кандидатов для самостоятельного
  просмотра. Баллы Cosine и RRF используются только для ранжирования кандидатов.
- Для описательного вопроса требуется совпадение соседней пары значимых слов
  темы в одном предложении или строке. Контекст содержит
  целые выдержки в рамках настроенного бюджета. Повторные ссылки убираются.
- Перечень показателей, резюме, разбор результатов и сравнение используют
  актуальное извлечение PostgreSQL. В запросах можно указать постоянные
  номера документов. Ссылка на карточку, имя загрузки и точное упоминание
  в актуальном тексте также поддерживаются. [Сценарии чата и номера документов](DOCUMENT_NUMBERS.md)
- Для точного ответа имя показателя, значение и единица должны образовывать
  одну запись. Например, подходят строки `Калий: 5,2 ммоль/л` и
  `Калий | 5,2 | ммоль/л | 2025-02-11`. Дату принимаем из той же записи,
  в отдельном поле строки или в завершающих скобках. Для вопроса о дате
  документа нужна явно подписанная дата. Форматы `YYYY-MM-DD` и
  `DD.MM.YYYY` сверяются как календарные даты. Значения сверяются через
  Decimal, а дробная запятая и точка считаются равнозначными. Известные
  написания одного показателя и единицы на RU/EN сопоставляются без
  пересчёта величин. Подписанный референсный диапазон в скобках не считается
  результатом измерения. В генетическом вопросе нужно назвать ген. Ген и вариант
  должны стоять в одной записи, например `BRCA1 c.68_69delAG` или `BRAF V600E`.
- Для точного совпадения сервер возвращает исходную строку и ссылку на
  содержащую её страницу или блок. Число у другого показателя, другая дата,
  другая единица, похожий вариант и оборванная строка не подтверждают запрос.
  Противоречащие друг другу подходящие записи дают уведомление о разных
  результатах. При отсутствии подтверждения тоже возвращается уведомление.
  Оба уведомления идут без ссылок и вызова Qwen.
- Перед сохранением ответов сервер повторно сверяет владельца, версию текста и
  поколение индекса. История сохраняется. При чтении ссылки проверяются по
  действующей версии, старые личные выдержки в новые промпты не подставляются.
  При переходе с hybrid на dense проверка доказательств остаётся той же.

Проверка консервативна. Она рассчитана на один явно названный показатель,
дату или генетический вариант и ограниченный словарь RU/EN. Она может
пропустить сложные таблицы, диапазоны, квалифицированные значения вроде
`<5,2`, неподдерживаемые записи HGVS, варианты формулировок и даты из
отдельных заголовков. Неоднозначные вопросы требуют уточнения. Сверка конфликтов
ограничена найденными кандидатами и не заменяет полный разбор всех документов.

Для ручной проверки используйте Git Bash из корня проекта. `dc` определён
в разделе ручных проверок. Интеграция чата использует отдельный PostgreSQL и
подменяет только удалённые вызовы, включая ошибку hybrid и ответ dense.

```bash
"${dc[@]}" build backend
"${dc[@]}" run --rm --no-deps backend python -m ruff check app tests
"${dc[@]}" run --rm --no-deps backend python -m mypy app
"${dc[@]}" run --rm --no-deps backend python -m pytest -q \
  tests/test_chat_evidence.py tests/test_llm_gateway.py

COMPOSE_DISABLE_ENV_FILE=1 docker compose -p medical-ai-chat-test \
  -f infrastructure/compose.chat-test.yaml run --build --rm tests
```

Ожидаются потоковые ответы и история общих реплик, точные ссылки только на
подтверждающие фрагменты, пустые источники при конфликте или отсутствии данных
и безопасная ошибка при полной недоступности retrieval. Проверки с настоящими
Qdrant и Ollama описаны выше, проверка браузера — в [этапе 5](STAGE5.md#приёмка).

### Диагностика карточки и ссылки из извлечённого текста

В PostgreSQL `documents` хранит UUID, владельца, имя загрузки и ключ объекта.
Оригинал хранится в MinIO по ключу, сгенерированному из UUID. Таблица
`document_text_versions` связывает извлечённый текст и координаты источников
с UUID документа. `rag_index_jobs` связывает текущую версию с поколением, а
`rag_chunks` хранит фрагменты. Qdrant хранит векторы и идентификаторы,
используемые для проверки попаданий по PostgreSQL.

Карточка загружает последнюю версию через `GET /api/documents/{id}`. Встроенного
просмотра PDF в карточке нет, кнопка «Оригинал» запускает отдельное скачивание.
Текст может быть доступен при сбое скачивания. В консоли браузера на
авторизованной карточке можно проверить код скачивания без сохранения файла.

```javascript
const id = location.pathname.split("/").filter(Boolean).at(-1);
const original = await fetch(`/api/documents/${encodeURIComponent(id)}/download`);
console.log(original.status, original.headers.get("content-disposition")?.startsWith("attachment"));
await original.body?.cancel();
```

`200` с признаком attachment означает, что началась отдача файла.
`409` означает неподтверждённую загрузку, `404` при доступной карточке —
отсутствующий объект, `503` — сбой чтения хранилища.

Для диагностики конкретного упоминания подставьте UUID карточки и UUID
владельца из ответа `/api/auth/me` в его сессии. В Git Bash запустите команду
через уже работающий backend рабочего Compose-проекта (`dc` выше).
SQL-транзакция работает только на чтение. Скрипт выводит признаки
совпадения, статусы и счётчики без текста документа и реквизитов MinIO.

```bash
read -r -p 'UUID владельца: ' owner_id
read -r -p 'UUID документа: ' document_id
read -r -p 'Строка для поиска в тексте: ' reference
"${dc[@]}" exec -T backend python - "$owner_id" "$document_id" "$reference" <<'PY'
import sys
from uuid import UUID
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.db import get_engine, get_settings
from app.documents.models import Document
from app.rag.adapters import QdrantVectors, RagError, generation_filter
from app.rag.hybrid import QdrantHybrid
from app.rag.models import RagIndexJob
from app.rag.service import eligible, latest_version

owner, document_id = map(UUID, sys.argv[1:3])
reference = sys.argv[3].casefold()
if not reference:
    raise SystemExit("Укажите непустую строку")
settings = get_settings()
with Session(get_engine()) as db:
    db.execute(text("SET TRANSACTION READ ONLY"))
    document = db.scalar(select(Document).where(
        Document.id == document_id, Document.owner_id == owner
    ))
    if document is None:
        raise SystemExit("Документ не найден у указанного владельца")
    version = latest_version(db, document.id)
    job = db.scalar(eligible(owner, settings).where(RagIndexJob.document_id == document.id))
    print("document_status", document.status, "error_code", document.error_code)
    print("upload_filename_matches", document.original_filename.casefold() == reference)
    print("latest_version", version.version if version else None)
    print("latest_text_contains_reference", bool(
        version and reference in version.normalized_text.casefold()
    ))
    print("latest_locator_count", len(version.locators) if version else 0)
    print("eligible_current_index", job is not None)
    if job is not None:
        generations = [str(job.generation)]
        vectors = QdrantVectors(settings)
        scope = generation_filter(settings, owner, generations)
        print("expected_points", job.chunk_count)
        try:
            result = vectors.http.request("POST", vectors.path + "/points/count", {
                "filter": scope, "exact": True
            })
            print("dense_points", result["result"]["count"])
        except RagError as error:
            print("dense_count_error", error.code)
        try:
            print("hybrid_points", QdrantHybrid(vectors).count(owner, generations))
        except RagError as error:
            print("hybrid_count_error", error.code)
PY
```

Если имя загрузки отличается, а строка найдена в актуальном тексте, это
упоминание в данной карточке. Оно само по себе не доказывает, что отдельно
сохранён названный файл. Опубликованное поколение требуется для векторного
поиска. Обзор использует авторизованное актуальное извлечение напрямую и
доступен независимо от скачивания оригинала и покрытия Qdrant.

## Ограничения и приёмка

- Набор синтетический. OCR, большие документы, разорванные строки таблиц и
  клиническая пригодность требуют отдельной оценки. Токенизация сохраняет
  пунктуацию внутри терма, поэтому `5.2` и `5,2` остаются разными термами,
  а запись `BRCA1,BRCA2` без пробела остаётся одним термом.
- Длина 160 токенов фиксирована. Нативный BM25 использует общие идентификаторы
  термов и статистику IDF в коллекции. Документы другого пользователя могут
  влиять на порядок лексических результатов, а фильтры Qdrant и проверка
  PostgreSQL изолируют доступ к самим результатам. Если нужна изоляция
  статистики, оставьте dense до отдельного решения.
  [Изоляция статистики Qdrant](https://qdrant.tech/documentation/manage-data/multitenancy/)
- Backfill и синхронизация держат блокировку только на обрабатываемом документе.
  После сбоя синхронизации старые поколения могут оставаться в новой коллекции
  до повторного backfill. SQL исключает их из выдачи. Учитывайте три коллекции
  в процедурах хранения, backup и полного удаления.
- Для включения нужны успешные регрессии этапов 4–5, нулевые утечки между
  пользователями, подтверждённый fallback и корректные ссылки. Качество и
  задержку сравните с dense по отчёту и ручным сценариям. При отсутствии
  подтверждённого выигрыша оставьте `dense`.
