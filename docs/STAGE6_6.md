# Этап 6.6. Evaluation чата

CLI запускает существующие маршруты авторизации и чата внутри тестового процесса.
Он использует отдельный PostgreSQL из `compose.rag-test.yaml`. При
`--retrieval qdrant` создание, индексация и поиск используют Qdrant того же
тестового стека и коллекцию `medical_ai_evaluation_v1`. Seed добавляет данные
в тестовый PostgreSQL и, при выборе Qdrant, в тестовую коллекцию. Миграции
вне CLI. Запускайте только на синтетическом стенде.

## Данные и методика

`backend/evaluation/chat_benchmark.json` содержит 30 сценариев и восемь
запросов с разметкой релевантности. Два владельца получают синтетические
документы. Семь справочных записей содержат короткие англоязычные
тексты для benchmark по каталогу MedlinePlus. Две отдельные
записи проверяют исключение неопубликованной и устаревшей версии.
Фиксированный PubMed транспорт содержит библиографический пример по
PMID 19671910 и две явно синтетические публикации. В рабочий PubMed эти
синтетические идентификаторы не отправляются.

| Группа | Методика |
| --- | --- |
| Retrieval | Recall и Precision на 3 и 5, MRR по полученному рангу и доля запросов с релевантным источником. Precision делится на K даже при короткой выдаче. Измеряется задержка каждого запроса. |
| Agent | Множество инструментов сверяется с эталонным множеством сценария. Учитываются число вызовов, ошибки инструментов, лишний PubMed, смешанные запросы, fallback и ошибки контекста. |
| Ответы | Проверка заявленных фактов, точных чисел и единиц, расчётных значений, источников и ссылок. По каждому сценарию фиксируются корректный, частичный, отсутствующий, неподтверждённый ответ или техническая ошибка. |
| Безопасность | ACL, повторная проверка истории, согласие, отсутствие утечки в PubMed, статусы источников, отсутствие аннотации и отбрасывание внедрённых вымышленных сведений. Проверки перечислены отдельно по сценарию. |
| Производительность | Время полного ответа, Tool Calling и вызовов провайдера, число логических вызовов модели. Время обработки моделью включает сетевой обмен и получение потока. |

RRF в оценку достоверности не входит. Набор мал и размечен по источникам.
Для запросов с разметкой в JSON сохраняются `ranked_sources` в порядке
выдачи после фильтрации и `first_relevant_rank` (счёт с единицы).
В сценариях `draft-hidden` и `stale-hidden` проверяются идентификаторы
запрещённых источников и контрольные строки их содержимого в выдаче
инструмента и ответе. Посторонние опубликованные источники учитываются в
`retrieval_quality.irrelevant_sources` и в отдельном счётчике группы
`summary.retrieval`. Recall, Precision и MRR рассчитываются по запросам
с размеченными релевантными источниками.
В `history-recheck` проверка безопасности использует отдельное синтетическое
сообщение. Проба проверяет видимость ссылки после сохранения и её удаление
из истории после перевода версии в `outdated`. Отсутствие цитаты в ответе
модели влияет на качество ответа. Если начальное состояние пробы не
подтверждено, она отмечается как `unverified`.

`answers` оценивает завершившиеся ответы, включая ответы из fallback.
`reliability` показывает все попытки оценки ответов, число тайм-аутов,
технические ошибки, покрытие и число ответов из fallback. Маршрутизация и
ошибки инструментов учитываются по всем попыткам, включая тайм-ауты.
Проверка текста детерминированная и может не принять корректное перефразирование.
Отдельные проверки предупреждений считают только оговорки, отмеченные
правилами. Свободный медицинский вывод требует экспертной оценки.

В offline модель заменена сценарием. Ответы PubMed фиксированы также в
live для сопоставимости. Персональный поиск получает фиксированные кандидаты
и проходит через штатную проверку владельца и версии. Реальный поиск Qdrant
относится к общей базе знаний и доступен в обоих режимах. Проверка внедрённых вымышленных сведений
использует фиксированный ответ модели даже в live и исключена из агрегатов
реальной модели. Отчёт связывает результаты с SHA‑256 датасета и переданным
оператором Git commit. Сравнение использует общее подмножество успешно
выполненных live‑сценариев двух полных запусков с одинаковым датасетом,
коммитом и конфигурацией retrieval. Сценарии с fallback исключаются из
сравнения. Лимиты генерации провайдеров указаны в отчётах.

## Запуск без пересборки

Из корня проекта в Git Bash. Нужны уже имеющиеся образы из тестового стека
RAG и работающие сервисы синтетического проекта. Команды ниже используют
`medical-ai-rag-local-tests` и существующую сеть Ollama
`medical-ai-rag-inference-test` из [HYBRID_RAG.md](HYBRID_RAG.md).
Для live-override нужен Docker Compose 2.33.1 или новее с поддержкой
`gw_priority` в Docker Engine.

```bash
rag_test_project=medical-ai-rag-local-tests
rt() {
  COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_PROFILES= MSYS_NO_PATHCONV=1 \
    MSYS2_ARG_CONV_EXCL='*' MSYS2_ENV_CONV_EXCL='GIGACHAT_CA_BUNDLE;GIGACHAT_CREDENTIALS' \
    docker compose -p "$rag_test_project" -f infrastructure/compose.rag-test.yaml \
    -f infrastructure/compose.rag-test-local-model.yaml "$@"
}
rt_live() {
  rt -f infrastructure/compose.rag-test-live-evaluation.yaml "$@"
}
backend_src=$(cygpath -am "$PWD/backend")
mkdir -p evaluation-reports
report_dir=$(cygpath -am "$PWD/evaluation-reports")
rt_container() {
  local compose_cmd=$1
  shift
  "$compose_cmd" run --rm --no-deps --pull never \
    -v "$backend_src/app:/app/app:ro" -v "$backend_src/tests:/app/tests:ro" \
    -v "$backend_src/evaluation:/app/evaluation:ro" \
    -v "$backend_src/scripts:/app/scripts:ro" \
    -v "$backend_src/alembic:/app/alembic:ro" -v "$backend_src/alembic.ini:/app/alembic.ini:ro" \
    -v "$report_dir:/reports" "$@"
}
rt_run() {
  rt_container rt tests "$@"
}
rt_run python -m pytest -q tests/test_chat_benchmark.py
rt_run ruff check evaluation/chat_metrics.py evaluation/chat_fixtures.py \
  evaluation/chat_runtime.py evaluation/extraction_tracking.py \
  scripts/evaluate_chat.py tests/test_chat_benchmark.py

rt up -d --no-build --pull never --wait postgres
# Только для тестовой БД со схемой, которая ещё не подготовлена.
rt_run alembic upgrade head
```

Подставьте фактический commit проверяемого кода. Dry run проверяет
локальную конфигурацию и датасет без расчёта метрик или записи в MLflow.

```bash
revision=6a1953f  # Замените на commit проверяемого кода.
rt_run python -m scripts.evaluate_chat --dry-run --provider ollama --commit "$revision"
rt_run python -m scripts.evaluate_chat --mode offline --provider ollama --commit "$revision" \
  --output /reports/offline.json --csv /reports/offline.csv
```

Для замера только реального Knowledge RAG используйте тестовый Qdrant и
запущенный Docker Ollama с уже доступной моделью BGE‑M3. Ollama в этом
варианте вычисляет эмбеддинги; ответы чата и PubMed берутся из фикстур.
В отчёте будут `mode=offline` и `retrieval_backend=qdrant`.

```bash
rt_run python -m pytest -q \
  tests/test_chat_benchmark.py::test_offline_qdrant_measures_retrieval_without_live_generation_or_main_collection

rt_run python -m scripts.evaluate_chat --mode offline --provider ollama \
  --retrieval qdrant --commit "$revision" \
  --output /reports/offline-qdrant.json --csv /reports/offline-qdrant.csv
```

Для live Ollama используйте существующую Ollama по адресу
`http://ollama:11434` и модель `qwen2.5:7b-instruct`. Для retrieval через
Qdrant нужны запущенный тестовый Qdrant и модель `bge-m3:567m` в Ollama.
Обе команды `rt_run` для offline и live Ollama используют внутренние сети.

Для диагностики передайте `--case ИДЕНТИФИКАТОР` для каждого нужного сценария.
CLI печатает статус и длительность после каждого сценария. В JSON поле
`llm_requests` показывает фазу (`tool_round`, `grounded_answer`,
`fallback_answer`), раунд, длительность, время до первого события, имена
инструментов и код ошибки. `context_bytes` измеряет сериализованные
сообщения и схемы в UTF-8. `timeout_origin` различает тайм-аут запроса
к провайдеру и отмену по общему дедлайну. Если при истечении срока ещё выполнялся
инструмент, отчёт включает `timed_out_tool`. Для ошибок валидации
`argument_issues` содержит имена известных параметров и типы ошибок.
В каждом раунде `offered_tools` перечисляет доступные модели инструменты,
а `tools` — вызовы, полученные от модели. Для GigaChat `finish_reason`
показывает способ завершения потока. `missing_tools` на уровне сценария
перечисляет ожидавшиеся инструменты, которые Agent Loop не выполнил.
Диагностика исключает содержимое сообщений и значения параметров.
Для персональных сценариев отчёт сравнивает эталон с результатом инструмента
(`tools[].expected_content`), контекстом инструмента, переданным модели
(`llm_requests[].input_expected_content`), текстом модели до проверки
(`llm_requests[].expected_content`) и опубликованным ответом
(`published_expected_content`). `format_equivalent_numeric_hits` отдельно
учитывает некоторые варианты записи тех же чисел и единиц.
В `personal-search` также отмечается наличие русского названия показателя.
Эти поля не влияют на исходные оценки и сохраняют только счётчики и флаги.
Укажите `--retrieval fixed` в обоих прогонах, если задача только сравнить
генерацию на тех же фиксированных источниках.

```bash
rt_run env LLM_OLLAMA_URL=http://ollama:11434 \
  python -m scripts.evaluate_chat --mode live --provider ollama --retrieval qdrant \
  --commit "$revision" --output /reports/ollama-live.json --csv /reports/ollama-live.csv
```

Выбор live GigaChat и `--allow-synthetic-cloud` означает согласие на
отправку только подготовленных синтетических сообщений. Live-override
назначает исходящий шлюз только контейнеру `tests` и сохраняет доступ к
изолированным PostgreSQL, Redis, Qdrant и Ollama. Compose монтирует
`infrastructure/certs/gigachat/russian_trusted_root_ca_pem.crt` read-only и
сам задаёт `GIGACHAT_CA_BUNDLE`. Модель по умолчанию `GigaChat-3-Ultra`,
scope — `GIGACHAT_API_PERS`.

```bash
rt_gigachat() (
  set +x
  : "${revision:?Укажите commit проверяемого кода в переменной revision}"
  read -rsp 'Ключ авторизации GigaChat: ' GIGACHAT_CREDENTIALS || return 1
  printf '\n'
  export GIGACHAT_CREDENTIALS
  rt_container rt_live -e GIGACHAT_CREDENTIALS tests python -m scripts.evaluate_chat \
    --mode live --provider gigachat --allow-synthetic-cloud --retrieval qdrant \
    --commit "$revision" "$@"
)

rt_gigachat --output /reports/gigachat-live.json --csv /reports/gigachat-live.csv
```

Ключ считывается без отображения и живёт в окружении временного контейнера и
подоболочки на время запуска. CLI перед сценариями проверяет локальный PEM
и получает токен через штатный OAuth с TLS и лимитом 10 секунд. При сбое
CLI возвращает код ошибки до выполнения сценариев. Для проверки только OAuth
запустите `rt_gigachat --preflight-only`. Ожидается
`"state": "gigachat_oauth_checked"`. `--dry-run` проверяет локальные настройки
и не обращается в сеть.

### Продолжение GigaChat на базе `42375cb`

JSON обновляется атомарно после каждого сценария. `--resume` сверяет SHA‑256
и версию датасета, выбранные сценарии, режим, провайдер, модель, настройки
retrieval, лимиты модели и переданную оператором ревизию. Для отчёта от
`42375cb` запускайте из рабочего дерева на этом же HEAD с правками этапа 6.6.
Поле `legacy_scenarios` отмечает число строк из отчёта без checkpoint-метаданных.

`complete=true` означает, что записаны результаты всех сценариев. Код
возврата остаётся ненулевым при технической ошибке, отсутствующем ответе
или проваленной либо непроверенной критической проверке. Одиночный тайм-аут
не останавливает цикл. Если к дедлайну синхронный инструмент ещё работал,
прогон останавливается после записи ошибки. Live GigaChat также
останавливается при проблемах конфигурации, авторизации, TLS OAuth или
ограничении частоты. Для повторной попытки отдельного сценария используйте
`--case` с отдельным отчётом.

```bash
revision=42375cb
checkpoint=/reports/gigachat-live.json
resumed=/reports/gigachat-live-resumed.json
if [[ -f evaluation-reports/gigachat-live-resumed.json ]]; then
  checkpoint="$resumed"
fi
rt_gigachat --dry-run --resume "$checkpoint" &&
  rt_gigachat --resume "$checkpoint" --output "$resumed" \
  --csv /reports/gigachat-live-resumed.csv
```

При первом продолжении dry run показывает 17 сохранённых и 13 оставшихся
сценариев. Первый вызов модели соответствует сценарию №18 `pubmed-doi`.
Попытка №17 `pubmed-pmid` сохраняет техническую ошибку, а `no_abstract`
получает значение `null` (`unverified`). Исходный JSON сохраняется отдельно.

### Точечная проверка диагностики

```bash
rt_run python -m pytest -q -m 'not integration' \
  tests/test_chat_benchmark.py tests/test_gigachat_gateway.py tests/test_gigachat_extraction.py
rt_run python -m pytest -q \
  tests/test_chat_benchmark.py::test_offline_personal_tools_expose_expected_fixture_content
```

Первый запуск проверяет checkpoint, продолжение, обработку ошибок и
тайм-аутов на заглушках. Второй использует подготовленную синтетическую БД
и проверяет, что эталон найден в выдаче инструмента и передан в модельный
контекст во всех шести сценариях.

В сохранённом прогоне `grounded_answer` имел общий дедлайн сценария
180 секунд, тайм-аут запроса 180 секунд и отдельный тайм-аут соединения
HTTPX 5 секунд. Ранее подтип `gigachat_timeout` отсутствовал, поэтому
5054 мс указывают на лимит соединения только косвенно. В новых отчётах
`timeout_kind` различает `connect`, `read`, `write`, `pool` и
`deadline`. Ошибка получения токена указывается как `oauth_timeout`.
Для проверки одного запроса с прежними условиями поиска и явным согласием
используйте отдельный отчёт.

```bash
rt_gigachat --case pubmed-pmid --output /reports/gigachat-pmid-diagnostic.json
```

В шести partial есть следующие эталоны и данные синтетических документов.

| Сценарий | Эталон и его источник | Что видно в старом отчёте |
|---|---|---|
| `potassium-first` | doc1, `4.2 mmol/L` | `get_documents` вместо `analyze_measurements` |
| `hemoglobin-first` | doc1, `132 g/L` | `get_documents` вместо `analyze_measurements` |
| `glucose-second` | doc2, `5.8 mmol/L` | `get_documents` вместо `analyze_measurements` |
| `personal-search` | doc1, `Creatinine` | `get_documents` вместо `search_documents` |
| `compare-creatinine` | doc1 → doc2, `1.04 − 0.91 = +0.13` | Вызван `analyze_measurements` |
| `trend-creatinine` | doc1 → doc2, `0.13 / 0.91 × 100 ≈ +14.29%` | Вызван `analyze_measurements` |

Во всех шести строках зафиксировано предупреждение о неподтверждённых
утверждениях и есть нужные ссылки. Сохранённых аргументов инструментов,
результатов инструментов и текста до фильтрации нет. Поэтому нельзя
установить, где именно пропало совпадение в каждой исторической строке.
У `personal-search` исходный критерий принимает только английское имя.
Числовой критерий требует точное написание единицы и знака, если они
заданы в эталоне. Например, `4,2 ммоль/л` даёт 0 для эталона
`4.2 mmol/L`. Чтобы проверить эти причины на новых ответах, повторите
только шесть сценариев в диагностический файл.

```bash
rt_gigachat --case potassium-first --case hemoglobin-first --case glucose-second \
  --case personal-search --case compare-creatinine --case trend-creatinine \
  --output /reports/gigachat-personal-diagnostic.json
```

### Проверка изоляции

```bash
for compose_cmd in rt rt_live; do
  "$compose_cmd" config --format json | python -c '
import json, sys
config = json.load(sys.stdin)
live = sys.argv[1] == "rt_live"
networks = config["networks"]
services = config["services"]
assert networks["default"]["internal"] is True
assert networks["inference"]["name"] == "medical-ai-rag-inference-test"
assert set(networks) == ({"default", "inference", "evaluation_egress"}
                         if live else {"default", "inference"})
assert all(set(services[n]["networks"]) == {"default"} and not services[n].get("ports")
           for n in ("postgres", "redis", "qdrant"))
with_egress = [n for n, service in services.items()
               if "evaluation_egress" in service.get("networks", {})]
assert with_egress == (["tests"] if live else [])
assert set(services["tests"]["networks"]) == set(networks)
if live:
    assert services["tests"]["networks"]["evaluation_egress"]["gw_priority"] == 1
    assert not networks["evaluation_egress"].get("internal", False)
    env = services["tests"]["environment"]
    assert "GIGACHAT_CREDENTIALS" not in env
    assert any(v["target"] == env["GIGACHAT_CA_BUNDLE"] and v.get("read_only")
               for v in services["tests"]["volumes"])
print(sys.argv[1], "isolation OK")
' "$compose_cmd" || break
done

docker network inspect --format '{{.Name}} Internal={{.Internal}}' \
  "${rag_test_project}_default" medical-ai-rag-inference-test
# После rt_gigachat --preflight-only:
docker network inspect --format '{{.Name}} Internal={{.Internal}}' \
  "${rag_test_project}_evaluation_egress"
```

Ожидаются `isolation OK` для обеих конфигураций, `Internal=true` для двух
тестовых сетей и `Internal=false` для отдельной egress-сети. PostgreSQL,
Redis и Qdrant подключены только к внутренней тестовой сети. Сети основного
приложения отсутствуют в конфигурации.

## MLflow

Для записи добавьте `--mlflow-uri http://localhost:5000` при запуске из
окружения с loopback-доступом к серверу. Из контейнера можно указать
`--mlflow-uri http://host.docker.internal:5000`, если опубликованный порт
доступен в вашей конфигурации Docker Desktop. При отказе соединения CLI
сохраняет локальный отчёт и помечает запись как неудавшуюся. Для отключения
записи не передавайте этот параметр.

В UI на `http://localhost:5000` откройте эксперимент
**medical-ai-evaluation**, затем run с именем режима и провайдера. Метрики
находятся в **Metrics**, параметры в **Parameters**, обезличенный отчёт —
**Artifacts → evaluation.json**. В нём только коды сценариев, проверки,
метрики, псевдонимы источников и безопасная конфигурация. Тексты сообщений,
документов, запросов PubMed, ключи и токены в отчёт не записываются.

### Импорт готового отчёта GigaChat

Для `gigachat-live-resumed.json` запуск Evaluation не нужен. Импортёр принимает
полный checkpoint этапа 6.6 от ревизии `42375cb` с датасетом `6.6.2` и
конфигурацией сохранённого прогона. Он сверяет хеш датасета и состав сценариев,
пересчитывает агрегаты из сохранённых строк и принимает только поля, коды
и псевдонимы синтетического отчёта. Свободный текст, неизвестные поля,
значения ключей и реальные идентификаторы в такой схеме отклоняются.

Из корня проекта в Git Bash, при работающем MLflow и уже собранном локальном
образе backend, выполните одну команду. Для нестандартного имени Compose-проекта
укажите его в shell-переменной `COMPOSE_PROJECT_NAME`.

```bash
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --pull never --read-only \
  --network "${COMPOSE_PROJECT_NAME:-medical-ai}_tracking" \
  --mount "type=bind,source=$(cygpath -am "$PWD/backend/evaluation"),target=/app/evaluation,readonly" \
  --mount "type=bind,source=$(cygpath -am "$PWD/backend/scripts"),target=/app/scripts,readonly" \
  --mount "type=bind,source=$(cygpath -am "$PWD/evaluation-reports/gigachat-live-resumed.json"),target=/report.json,readonly" \
  "${COMPOSE_PROJECT_NAME:-medical-ai}-backend" python -m scripts.register_chat_report \
  --report /report.json --mlflow-uri http://mlflow:5000
```

Контейнер подключён только к внутренней сети `tracking`. Эта сеть даёт
доступ к MLflow, а основной PostgreSQL подключён к отдельной сети
`app-db`. Для импорта достаточно файла отчёта, датасета и кода. Запросы
GigaChat, Agent Loop и retrieval при импорте не выполняются.

После импорта CLI печатает `run_id`. В эксперименте `medical-ai-evaluation`
ожидается запись `live gigachat (imported)` с параметрами `complete=true`,
`technical_errors=2`, `has_technical_errors=true`, `security_unverified=3`
и `validation_scope=synthetic_benchmark`. `Artifacts → evaluation.json`
содержит исходный JSON побайтно. Локальный файл остаётся без записи, поэтому
внутри артефакта сохраняется `mlflow.status=not_requested`. Повтор команды
создаёт ещё один run. Статус `FINISHED` означает успешную запись в MLflow.
Медицинская валидация находится вне области этого прогона.

Точечная проверка использует заглушку MLflow и проверяет, что неверные метрики,
неполный отчёт и чувствительные поля отклоняются до регистрации.

```bash
rt_run python -m pytest -q tests/test_register_chat_report.py
```
