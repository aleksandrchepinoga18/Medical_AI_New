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
RAG. Используйте своё имя существующего **синтетического** Compose-проекта.

```bash
rt() {
  COMPOSE_DISABLE_ENV_FILE=1 MSYS_NO_PATHCONV=1 docker compose \
    -p medical-ai-knowledge-test -f infrastructure/compose.rag-test.yaml "$@"
}
backend_src=$(cygpath -am "$PWD/backend")
mkdir -p evaluation-reports
report_dir=$(cygpath -am "$PWD/evaluation-reports")
rt_run() {
  rt run --rm --no-deps --pull never \
    -v "$backend_src/app:/app/app:ro" -v "$backend_src/tests:/app/tests:ro" \
    -v "$backend_src/evaluation:/app/evaluation:ro" \
    -v "$backend_src/scripts:/app/scripts:ro" \
    -v "$backend_src/alembic:/app/alembic:ro" -v "$backend_src/alembic.ini:/app/alembic.ini:ro" \
    -v "$report_dir:/reports" tests "$@"
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

Для live Ollama используйте существующий запущенный Ollama с
`qwen2.5:7b-instruct`. Пример ниже использует Windows Ollama для генерации.
С `--retrieval qdrant` нужны уже работающие тестовые Qdrant и Docker Ollama
с моделью эмбеддингов из настроек стенда, по умолчанию `bge-m3:567m`.
Укажите `--retrieval fixed` в обоих прогонах, если задача только сравнить
генерацию на тех же фиксированных источниках.

```bash
rt_run env LLM_OLLAMA_URL=http://host.docker.internal:11434 \
  python -m scripts.evaluate_chat --mode live --provider ollama --retrieval qdrant \
  --commit "$revision" --output /reports/ollama-live.json --csv /reports/ollama-live.csv
```

Выбор live GigaChat и `--allow-synthetic-cloud` означает согласие на
отправку только подготовленных синтетических сообщений. При недоступности
GigaChat прогон прекращается, неполный отчёт исключается из сравнения.

```bash
read -rsp 'Ключ авторизации GigaChat: ' GIGACHAT_CREDENTIALS; printf '\n'
export GIGACHAT_CREDENTIALS
MSYS2_ENV_CONV_EXCL=GIGACHAT_CA_BUNDLE rt run --rm --no-deps --pull never \
  -e GIGACHAT_CREDENTIALS \
  -v "$backend_src/app:/app/app:ro" \
  -v "$backend_src/evaluation:/app/evaluation:ro" \
  -v "$backend_src/scripts:/app/scripts:ro" \
  -v "$report_dir:/reports" tests \
  python -m scripts.evaluate_chat --mode live --provider gigachat --allow-synthetic-cloud \
  --retrieval qdrant --commit "$revision" --output /reports/gigachat-live.json \
  --csv /reports/gigachat-live.csv --compare /reports/ollama-live.json
unset GIGACHAT_CREDENTIALS
```

Если в GigaChat нужен собственный PEM, перед аргументом `tests` подключите
его read-only и передайте `-e GIGACHAT_CA_BUNDLE=/run/gigachat-ca/root.crt`.
Модель по умолчанию `GigaChat-3-Ultra`, scope — `GIGACHAT_API_PERS`.

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
