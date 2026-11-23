# Этап 5 — LLM Gateway и персональный чат

Статус: реализация подготовлена, запуск и приёмка пользователем ещё не выполнены.
Рабочий `.env` не изменён. Чат выключен по умолчанию (`CHAT_ENABLED=false`).

## Архитектура

Next.js `/chat` → авторизованный `/chat` API → агентный цикл → Gateway →
`LLM_OLLAMA_URL/api/chat` (по умолчанию `http://ollama:11434/api/chat`).
В гибридном режиме генерация идёт в Windows Ollama через
`http://host.docker.internal:11434/api/chat`, embeddings остаются в Docker Ollama.
Новых коллекций, embedding-моделей и сервисов нет.
Gateway предоставляет generate, stream, структурированный вывод и поток native tool calls.
Один Agent Loop работает с Ollama и GigaChat-3-Ultra. RAG использует локальные
адреса в обоих режимах. URL облачного API закреплён в адаптере, HTTP-клиенты
отключают прокси из окружения и не переходят по перенаправлениям.
`httpx==0.28.1` перенесён из dev в runtime: нужен для асинхронного streaming и отмены;
новый SDK или новая версия зависимости не добавлены.

Миграция `0004_chat` после `0003_personal_rag` создаёт только chat_conversations и
chat_messages. Существующие таблицы не меняет. Downgrade намеренно запрещён.
Все обращения к диалогам ограничены владельцем; мутации используют существующую
session auth/CSRF. DELETE удаляет только выбранный собственный диалог и его сообщения.

Генерация защищена блокировкой строки и арендой с UUID. Повторный запрос во время
активной аренды получает 409. Пользовательская попытка хранится как pending, после
успеха становится complete, после отмены — interrupted, при недоступности поиска
— failed. Ответ ассистента записывается одной транзакцией после завершения
агентного цикла или резервного расчёта и проверки источников. После аварийного завершения
процесса просроченная попытка отображается interrupted и восстанавливается при следующей
отправке. В LLM попадают только завершённые сообщения. Повторная отправка создаёт
новую попытку; автоматического повтора запросов нет.

Поток — NDJSON: start, delta, done(sources), error(code). Тексты ошибок провайдера,
промпты, ответы и retrieval не логируются и не отправляются в MLflow. SQL engine
использует hide_parameters. На reverse proxy также нельзя включать запись тел запросов.

Qwen получает завершённую историю в пределах бюджета и номера последних
обсуждавшихся документов. Она отвечает на общий вопрос сразу или выбирает
инструменты с проверяемыми аргументами:

- `get_documents` по номерам из `documents` возвращает подтверждённые измерения,
  текст в пределах бюджета и ссылки на источники. Для большого документа
  возвращается частичный контекст с признаком `partial`. Вызов без номеров
  возвращает страницу каталога. `after` означает только курсор страницы.
- `search_documents` возвращает выдержки из существующего RAG для больших
  документов и поиска по коллекции. При пустой
  выдаче по явно выбранным документам используется актуальный извлечённый текст.
- `analyze_measurements` возвращает подтверждённые измерения, динамику,
  сравнение двух документов или проверку референсов. Разницы и проценты
  вычисляет существующий код на `Decimal`.

Цикл ограничен пятью обращениями к модели и шестью вызовами инструментов на
сообщение. Один дедлайн генерации действует на все обращения к Ollama. Backend
валидирует параметры, проверяет владельца и версии и при необходимости
возвращает модели код ошибки инструмента. Перед выдачей ответа проверяет
источники повторно. Инструкции внутри документов и истории считаются данными.
История хранит ответы обоих типов в одном формате. Технический сбой включает
существующий резервный программный путь. После `start` ошибка поиска
передаётся событием NDJSON `error(retrieval_unavailable)`.
В `chat_agent_tool` фиксируются имя инструмента, статус, код, число актуальных
документов, подтверждённых измерений и разных источников. При пустой странице
для запроса с явно указанным номером модель получает подсказку точного поиска.
Если она завершает ответ по номеру после получения только каталога или без
результата поиска либо анализа, срабатывает резервный путь.

На старом Ollama текст при передаче `tools` может прийти в одном блоке.
Контур использует тот же NDJSON-поток и выдаёт ответ после проверки источников.
На вход модели поступают вопрос, релевантные завершённые реплики текущего
диалога и данные вызванных инструментов. Чат не выдаёт диагнозов или
назначений лечения.

## GigaChat-3-Ultra

Оба адаптера возвращают в Agent Loop один тип событий для текста и инструментов.
GigaChat использует SSE на `/v1/chat/completions` и сохраняет
`functions_state_id` в пределах текущей генерации. Результат инструмента
передаётся сообщением `function` с именем и JSON. OAuth и проверка TLS используют
существующий код извлечения. Токен переиспользуется в рамках одной генерации.
При первом HTTP 401 адаптер обновляет токен и повторяет запрос один раз.
[Функции GigaChat](https://developers.sber.ru/docs/ru/gigachat/guides/functions/generating-arguments-for-custom-functions)
· [Поток SSE](https://developers.sber.ru/docs/ru/gigachat/guides/response-token-streaming)
· [OAuth](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-token).

По умолчанию выбран Ollama. При настройке GigaChat пользователь подтверждает
каждое облачное отправление. Согласие включает сообщение, относящуюся к нему
историю этого диалога и данные выбранных инструментами документов. Backend
проверяет согласие до обращения к провайдеру. Согласие на извлечение PDF здесь
не действует. Агент передаёт выдержки и измерения из инструментов, а оригинал PDF
остаётся в хранилище. Источники проверяются перед сохранением ответа. При
явном выборе GigaChat технический сбой показывается в чате. Старые API-запросы
без выбора провайдера сохраняют режим с помеченным локальным резервным
ответом. Фильтрация GigaChat завершает запрос с кодом `gigachat_filtered`.

В настройках облака окно приложения по умолчанию составляет 16 384 токена,
лимит ответа — 2 048 токенов, дедлайн — 180 секунд. Эти параметры и бюджеты
истории и результатов задаются отдельно от Ollama. Документация Ultra указывает
максимальное окно модели в 128 тыс. токенов. Приложение использует меньшее
начальное окно и консервативную оценку по UTF-8, чтобы ограничить объём передачи.
Используется последовательный Function Calling API v1.
[Контекст модели](https://developers.sber.ru/docs/ru/gigachat/models/updates)
· [Метод генерации](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat).

Настройки облака задаются через `LLM_GIGACHAT_CONTEXT_TOKENS`,
`LLM_GIGACHAT_MAX_TOKENS`, `LLM_GIGACHAT_TIMEOUT_SECONDS`,
`CHAT_GIGACHAT_HISTORY_CHARS` и `CHAT_GIGACHAT_CONTEXT_CHARS`.
Для Ultra используется действующая авторизация `GIGACHAT_API_PERS`.

### Настройка выбора провайдера без пересборки

В Git Bash из корня проекта используйте набор Compose-файлов текущего dev-стека.
Пример для стандартного стека с локальным RAG и готовыми образами:

```bash
chat_compose=(--env-file .env
  -f infrastructure/compose.yaml
  -f infrastructure/compose.dev.yaml
  -f infrastructure/compose.rag.yaml)
dc() { docker compose "${chat_compose[@]}" "$@"; }
gc() {
  local cloud_compose=(-f infrastructure/compose.gigachat-chat.yaml)
  MSYS2_ENV_CONV_EXCL=GIGACHAT_CA_BUNDLE docker compose "${chat_compose[@]}" "${cloud_compose[@]}" "$@"
}
read -rsp 'Ключ авторизации GigaChat: ' GIGACHAT_CREDENTIALS; printf '\n'
export GIGACHAT_CREDENTIALS
# Если дополнительно нужен PEM, задайте имя уже установленного сертификата.
# export GIGACHAT_CA_BUNDLE=/run/gigachat-ca/russian_trusted_root_ca_pem.crt
LLM_PROVIDER=ollama gc config --quiet &&
LLM_PROVIDER=ollama gc up -d --no-deps --no-build --pull never --force-recreate --wait backend
```

При первом применении изменений чата пересоздайте также dev-frontend. Команда
использует существующий образ `:development` и монтирует актуальные исходники.
Frontend из старого runtime-образа содержит интерфейс на момент сборки.

```bash
gc up -d --no-deps --no-build --pull never --force-recreate --wait frontend
```

Если GigaChat уже настроен в backend с dev bind mounts, достаточно обновить
вкладку и открыть диалог. `LLM_PROVIDER` задаёт модель по умолчанию.
Чат читает названия и статусы из `GET /api/chat/{id}` и отправляет поле
`llm_provider` с каждым сообщением. Для следующих переключений служит поле
**Провайдер модели** в чате.

При пустом ключе GigaChat помечен как **Не настроен**, при ошибке локальной
настройки TLS — **Недоступен**. Сеть и авторизация проверяются при отправке.
Проверьте переключение на синтетических сообщениях. Ollama отвечает без
облачного согласия. GigaChat требует новой отметки для каждого сообщения.
Запрос в GigaChat без `external_consent: "gigachat"` получает
`403 external_consent_required`.

### Синтетическая проверка Function Calling

При включённом GigaChat выполните пробу. Она передаёт только фиктивный номер и код.

```bash
gc exec -T backend python - <<'PY'
import asyncio
from app.db import get_settings
from app.llm.gateway import ToolCall, gateway

async def main():
    settings = get_settings()
    assert settings.llm_provider == "gigachat"
    model = gateway(settings, external_consent=True)
    functions = [{"type": "function", "function": {
        "name": "get_test_code", "description": "Return the synthetic code for a number.",
        "parameters": {"type": "object", "properties": {"number": {"type": "integer"}},
                       "required": ["number"]},
    }}]
    messages = [
        {"role": "system", "content": "Call get_test_code first. Then return the code."},
        {"role": "user", "content": "Get the synthetic test code for number 6."},
    ]
    events = [part async for part in model.tool_stream(messages, functions)]
    (call,) = [part for part in events if isinstance(part, ToolCall)]
    assert call.name == "get_test_code" and call.arguments == {"number": 6} and call.state_id
    messages += [
        {"role": "assistant", "content": "", "tool_calls": [call.message()]},
        {"role": "tool", "tool_name": call.name, "content": '{"code":68427}'},
    ]
    final = [part async for part in model.tool_stream(messages, functions)]
    assert all(isinstance(part, str) for part in final) and "68427" in "".join(final)
    print("GigaChat Function Calling round trip OK")

asyncio.run(main())
PY
```

### Проверка схем инструментов GigaChat при HTTP 422

Синтетическая проба отправляет простую схему одного аргумента. Реальный агент
использует три описания из Pydantic. Они дополнительно содержат служебные
поля и ограничения, в том числе верхнюю границу 64-битного номера документа.
Адаптер GigaChat v1 отправляет типы, описания, перечни значений, свойства,
обязательные поля и типы элементов массивов. Все ограничения аргументов
сохраняются в Python. Порядок ролей и параметры генерации сохраняются.

В журнале `gigachat_validation_rejected` выводит классифицированную причину
отказа, число сообщений и функций, режим вызова и лимит токенов. Исходный
ответ GigaChat и содержимое запроса в журнал не попадают. При переходе на
резервный путь новый ответ сохраняется с пометкой «Резервный ответ · локальная обработка».

При работающем облачном dev-backend сравните исходные схемы Pydantic и те же
схемы после адаптации. Команда отправляет только статические описания
инструментов в официальный `POST /v1/functions/validate`.
[Валидатор функций](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/function-validation)
· [Причины HTTP 422](https://developers.sber.ru/docs/ru/gigachat/api/errors-description).

```bash
gc exec -T backend python - <<'PY'
import asyncio

import httpx

from app.chat.agent import tool_definitions
from app.db import get_settings
from app.documents.gigachat import API, GigaChatExtraction, gigachat_tls_context
from app.llm.gigachat import gigachat_functions, validation_reason


async def main():
    settings = get_settings()
    assert settings.gigachat_credentials.get_secret_value(), "GigaChat credentials required"
    tools = tool_definitions()
    variants = (("original", [tool["function"] for tool in tools]),
                ("wire", gigachat_functions(tools)))
    wire_ok = True
    async with httpx.AsyncClient(
        verify=gigachat_tls_context(settings.gigachat_ca_bundle),
        trust_env=False, follow_redirects=False, timeout=30,
    ) as client:
        auth = GigaChatExtraction(
            client, settings.gigachat_credentials, settings.gigachat_model,
            "schema", scope=settings.gigachat_scope,
        )
        headers = await auth.headers(timeout=30)
        for variant, functions in variants:
            for function in functions:
                response = await client.post(
                    f"{API}/functions/validate", headers=headers, json=function,
                )
                try:
                    parsed = response.json()
                except ValueError:
                    parsed = None
                body = parsed if isinstance(parsed, dict) else {}
                errors = body.get("errors") or []
                warnings = body.get("warnings") or []
                ok = (response.is_success and isinstance(parsed, dict)
                      and body.get("status", response.status_code) == 200 and not errors)
                print(variant, function["name"], f"http={response.status_code}",
                      f"valid={ok}", f"reason={validation_reason(body)}",
                      f"errors={len(errors)}", f"warnings={len(warnings)}")
                if variant == "wire":
                    wire_ok &= ok
    assert wire_ok, "A GigaChat wire schema was rejected"


asyncio.run(main())
PY
```

Ожидается `valid=True` для всех трёх строк `wire`. Сопоставьте строки
`original`, чтобы установить причину исходного отказа. Если обе версии
проходят валидацию, повторите синтетический сценарий чата и проверьте
`gigachat_validation_rejected` по идентификатору генерации.

### Сравнение на документе №6

Для каждого провайдера откройте новый диалог и последовательно задайте четыре
вопроса по синтетическому `04-multipage.pdf`:

1. Объясни результаты документа №6.
2. Как изменился уровень креатинина?
3. На сколько процентов он изменился?
4. Как изменились показатели альбумина и калия?

В первом ответе должны учитываться пять подтверждённых строк. Креатинин
`0.91 → 1.04 mg/dL`, разница `+0.13 mg/dL` и `+14.29%`. Альбумин
`4.2 → 4.2 g/dL`, изменение `0%`. Калий `4.3 mmol/L` представлен одной
точкой, поэтому динамику определить нельзя. Проверьте полноту, источники,
точность чисел и медицинских выводов, работу уточнений и выбор инструментов.
Сгруппируйте журнал по `generation`. `chat_agent_round` показывает раунды,
`ollama_request` и `gigachat_request` показывают HTTP-запросы к модели,
`chat_agent_tool` показывает операции, `chat_generation_complete` даёт
длительность. Отметьте ответы, прошедшие только через резервный путь.

```bash
dc logs --since=15m backend | grep -E 'chat_(agent_round|agent_tool|agent_fallback|generation_complete)|ollama_(request|stream_done)|gigachat_(request|stream_done)'
read -r -p 'Провайдер (ollama/gigachat): ' chat_provider
read -r -p 'Успешные ответы из 4, медиана мс, всего запросов к модели: ' passed median calls
dc exec -T -e COMPARE_PROVIDER="$chat_provider" -e COMPARE_PASSED="$passed" \
  -e COMPARE_MEDIAN_MS="$median" -e COMPARE_CALLS="$calls" mlflow python - <<'PY'
import os
import mlflow

provider = os.environ["COMPARE_PROVIDER"]
assert provider in {"ollama", "gigachat"}
passed = int(os.environ["COMPARE_PASSED"])
assert 0 <= passed <= 4
mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("medical-ai-chat-provider-comparison")
with mlflow.start_run(run_name=f"{provider} synthetic 04-multipage"):
    mlflow.log_param("provider", provider)
    mlflow.log_metrics({
        "passed": passed,
        "median_ms": float(os.environ["COMPARE_MEDIAN_MS"]),
        "model_requests": int(os.environ["COMPARE_CALLS"]),
    })
PY
```

В MLflow записываются агрегаты сравнения. Тексты вопросов, диалогов, выдержки
и ответы в эксперимент не передаются.
[Правила проверки источников и фактов](HYBRID_RAG.md#как-чат-проверяет-источники-и-факты)

## Конфигурация

Нужно одновременно `RAG_ENABLED=true` и `CHAT_ENABLED=true`.
`LLM_MODEL=qwen2.5:7b-instruct`, `LLM_TIMEOUT_SECONDS=180` (полное время генерации),
`LLM_OLLAMA_URL=http://ollama:11434` (по умолчанию, только серверная настройка),
`LLM_CONTEXT_TOKENS=4096`, `LLM_MAX_TOKENS=1024`, `CHAT_HISTORY_CHARS=2000`,
`CHAT_CONTEXT_CHARS=2000` — значения по умолчанию.
Сообщение ограничено 2000 символами, история для модели — 40 сообщениями и символьным
бюджетом, retrieval — максимум 5 фрагментами, ответ — 64000 символами. connect timeout 5 s,
тайм-аут ожидания данных равен `LLM_TIMEOUT_SECONDS` (connect timeout остаётся 5 s).
Общий дедлайн через `asyncio.timeout` ограничивает весь запрос, включая чтение потока.
Контекст Ollama задаёт `LLM_CONTEXT_TOKENS`
(2048–32768): Gateway передаёт значение в `options.num_ctx` тела POST `/api/chat`
для обычных ответов, streaming и structured output. Выбор модели только на сервере.
История UI показывает последние 200 сообщений, список — последние 100 диалогов.

### Экономичный профиль CPU

При ограниченной RAM вручную задайте в рабочем `.env`:

```dotenv
LLM_CONTEXT_TOKENS=2048
LLM_MAX_TOKENS=256
CHAT_HISTORY_CHARS=1000
CHAT_CONTEXT_CHARS=1000
```

RAG остаётся включённым: уменьшается только объём выдержек, передаваемых генератору.
`CHAT_HISTORY_CHARS` ограничивает завершённые сообщения истории (1000–64000 символов),
`CHAT_CONTEXT_CHARS` — суммарный текст retrieval (1000–32000 символов).
Эти настройки не изменяют `bge-m3:567m`, Qdrant или индексы и не требуют переиндексации.
При наличии RAM можно начать с 4096 токенов и бюджетов 2000/2000, затем увеличивать
значения в допустимых пределах, измеряя память. Для прежнего большого профиля:
16384 токена и бюджеты 16000/8000. Бюджеты заданы в символах, не в токенах:
учитывайте также системный промпт, текущий вопрос и резерв для ответа.

Снижение контекста уменьшает расход памяти, но не гарантирует загрузку Qwen 7B при
4,6 ГиБ свободной RAM: память нужна также весам модели и embeddings. Если Ollama
по-прежнему сообщает нехватку памяти, потребуется больше доступной RAM. Автоматической
смены модели, отключения RAG или переключения на внешнего провайдера нет.

После ручного изменения `.env` пересоздайте backend командой из раздела запуска:
обычный restart не обновляет environment контейнера. Unit-тест с mock transport
проверяет фактическое поле `options.num_ctx` HTTP-запроса во всех трёх режимах и
отклонение значений вне допустимых границ; Ollama для этого теста не нужна.

### Гибридная разработка: Windows Qwen и Docker embeddings

Backend принимает ровно два значения `LLM_OLLAMA_URL`:
`http://ollama:11434` и `http://host.docker.internal:11434`.
Другие схемы, порты, адреса, credentials, пути, query, fragment и завершающий `/`
отклоняются Pydantic при чтении конфигурации. URL задаётся на backend, redirects
и proxy environment выключены. Для перехода между Docker и Windows Ollama
требуется явное изменение конфигурации.

Для гибридного режима вручную задайте в рабочем `.env`:

```dotenv
LLM_OLLAMA_URL=http://host.docker.internal:11434
LLM_MODEL=qwen2.5:7b-instruct
RAG_OLLAMA_URL=http://ollama:11434
```

Настройки CHAT_ENABLED/RAG_ENABLED и лимиты генерации сохраняются. Compose передаёт
LLM_OLLAMA_URL в backend отдельно от RAG_OLLAMA_URL. Qwen уже установлена в Windows:
повторно скачивать её в Docker для гибридного режима не требуется. Docker Ollama
должна продолжать обслуживать `bge-m3:567m`; коллекция Qdrant и fingerprint индекса
от смены LLM_OLLAMA_URL не меняются. Переиндексация и миграция не нужны.

Windows Ollama должна слушать интерфейс, доступный Docker Desktop, на порту 11434.
Если она слушает только loopback, настройте её серверный `OLLAMA_HOST` и перезапустите
Windows Ollama вручную. Например, после выхода из Ollama в системном трее можно
запустить её в отдельном окне Git Bash:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Не запускайте второй сервер поверх уже работающего. При таком bind ограничьте
доступ к 11434 средствами Windows Firewall локальной средой Docker; публичный доступ
не требуется. OLLAMA_ORIGINS для браузера не нужен: запрос отправляет backend.

После изменения `.env` используйте массив `dc` из раздела запуска. При первом
применении изменений исходного кода пересоберите backend, затем пересоздайте его:

```bash
"${dc[@]}" build backend
"${dc[@]}" up -d --no-deps backend
```

Обычный restart не обновляет environment. Для возврата к полностью контейнерной схеме
(в том числе на Linux) вручную верните `LLM_OLLAMA_URL=http://ollama:11434`, убедитесь,
что Qwen установлена в Docker Ollama по инструкции ниже, и пересоздайте backend
той же командой. RAG_OLLAMA_URL остаётся прежним в обоих режимах. Для стандартного
Linux deployment host.docker.internal не используется и extra_hosts не требуется.

Проверьте на синтетическом вопросе: retrieval использует Docker embeddings, поток
генерации приходит из выбранной Ollama, история содержит complete и ответ ассистента.
При недоступной Windows Ollama ожидается безопасная ошибка без fallback.
Unit-тесты используют mock transport: проверяют оба URL во всех режимах Gateway,
отклонение запрещённых адресов, запрет redirects/proxy environment и неизменность
embedding URL, модели, Qdrant и fingerprint. Ранее добавленные регрессии length,
финального NDJSON без newline, тайм-аутов и interrupted сохранены.

## Безопасный запуск (Git Bash, выполняет пользователь)

Сначала проверки на синтетических данных. Отдельный Compose ниже нельзя объединять
с рабочим Compose; PostgreSQL использует tmpfs, Ollama и рабочие документы не нужны.

```bash
cd /c/workplace/Medical_AI_New
docker compose -p medical-ai-chat-test -f infrastructure/compose.chat-test.yaml build tests
docker compose -p medical-ai-chat-test -f infrastructure/compose.chat-test.yaml run --rm tests
```

Unit и frontend проверки в development-окружении проекта:

```bash
dc=(docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml -f infrastructure/compose.rag.yaml)
"${dc[@]}" build backend frontend
"${dc[@]}" run --rm --no-deps backend python -m pytest -m 'not integration'
"${dc[@]}" run --rm --no-deps frontend npm test
"${dc[@]}" run --rm --no-deps frontend npm run typecheck
"${dc[@]}" run --rm --no-deps frontend npm run lint
"${dc[@]}" run --rm --no-deps backend ruff check app/chat app/llm tests/test_chat_integration.py tests/test_llm_gateway.py
```

Ожидается отсутствие ошибок. Эти команды агент не выполнял. Unit используют mock transport;
интеграционные проверки чата — реальную отдельную PostgreSQL и подставленные RAG/LLM.
Регрессионные проверки реального RAG выполняются отдельно по STAGE4.md.

После успешных проверок: сделайте резервную копию PostgreSQL по действующей процедуре.
Установка Qwen требует доступа Ollama к реестру моделей, передаются только данные
скачивания модели, медицинские документы не используются. Уже установленную
`bge-m3:567m` не удалять и не переустанавливать.

Следующая команда `exec ollama ollama pull` относится к полностью контейнерному
режиму. В гибридном режиме с уже установленной Windows Qwen пропустите её;
проверка Docker embeddings и остальные действия приёмки остаются актуальными.

```bash
"${dc[@]}" up -d ollama
"${dc[@]}" exec ollama ollama list
"${dc[@]}" exec ollama ollama pull qwen2.5:7b-instruct
"${dc[@]}" exec ollama ollama list
"${dc[@]}" run --rm --no-deps backend alembic upgrade head
"${dc[@]}" run --rm --no-deps backend alembic current
```

Ожидается наличие обеих моделей и ревизия `0004_chat`. Затем вручную задайте в `.env`
`CHAT_ENABLED=true`, сохранив проверенные настройки этапа 4 и `RAG_ENABLED=true`.

```bash
"${dc[@]}" up -d backend frontend
```

Проверьте `/chat` на синтетических документах до использования медицинских данных.
Для отключения вручную верните `CHAT_ENABLED=false` и пересоздайте backend той же
командой. Данные чата сохраняются; удаление volumes и downgrade не нужны.

## Приёмка

- Создать, переименовать, открыть и удалить свой диалог; продолжить после reload.
- Получить потоковый общий ответ Qwen. Источники и уведомление о личных документах
  отсутствуют. Уточнение по общему вопросу использует предыдущие общие реплики.
- Спросить о ночном кашле в своей выписке и проверить, что ссылка ведёт к странице
  с совпавшей выдержкой. Для вопроса без подтверждения ожидается уведомление
  и пустой список источников.
- Спросить о конкретном показателе, дате и единице. Сверить дословную строку и ссылку.
  При разных результатах для одной области поиска ожидается запрос уточнения
  без источников. Запрос при недоступном hybrid может использовать dense-кандидатов,
  к которым применяется та же проверка фактов.
- Остановить длинный ответ, обновить страницу: завершённого ответа ассистента нет.
- Остановить Ollama вручную: безопасная ошибка, текст вопроса остаётся для повтора.
- Второй пользователь не видит чужие диалоги, API возвращает 404, документы защищены.
- Проверить 401 без сессии, 403 без CSRF, 409 при одновременной отправке, 422 для
  длинного сообщения, 503 при выключенном флаге; логи не содержат содержимого вопросов.
- На документе с prompt injection убедиться, что инструкции из него не выполняются.
- Проверить browser Network: LLM-запросы идут только через backend в локальную Ollama.
- Повторить проверки этапов 1–4. Этап не считается принятым до явного подтверждения пользователя.

## Ограничения

Инструкции снижают риск prompt injection, но не гарантируют достоверность медицинских
выводов модели. Источники подтверждают происхождение контекста, а не каждое утверждение.
Модель может напечатать выдуманный ID/URL обычным текстом; такие строки не становятся
ссылками интерфейса. Нет медицинской верификации, автоматического лечения или диагностики.
После доставки done, потерянной браузером, ответ уже может быть сохранён: история
сервера является источником истины. Отмена через AbortController зависит от прохождения
disconnect через Next.js proxy; проверить вручную. После падения процесса аренда
освобождается по времени. Общий лимит параллельных диалогов пользователя и постраничная
навигация истории пока отсутствуют. Hardware/RAM и скорость Qwen требуют измерения.
Линтеры, typecheck, тесты, реальная Qwen и end-to-end streaming ещё не запускались.

## Исправление interrupted после HTTP 200 Ollama

В исходном Gateway `done:true` с `done_reason:"length"` ошибочно превращался в
`GatewayError("output_limit")`. При достижении `num_predict` это штатное завершение
протокола, хотя ответ может быть обрезан по смыслу. Ошибка не давала маршруту дойти
до транзакции сохранения: вместо done отправлялся error, finally переводил попытку
в interrupted. Теперь такой ответ сохраняется; structured output по-прежнему
проходит Pydantic, а лимит 64000 символов сохраняется. Реальный разрыв без done
остаётся ошибкой, неполный ответ не сохраняется.

Дополнительно устранён жёсткий read timeout 30 s, который мог прерывать холодный
старт Qwen раньше общего дедлайна. Финальная NDJSON-запись обрабатывается и без
завершающего перевода строки, включая фрагментированные байты UTF-8.

HTTP 200 означает начало успешного HTTP-ответа, но не доказывает получение done
или сохранение в БД. Чтобы различить причины при ручной проверке, добавлены события:
`ollama_stream_done reason=stop|length|other`, `chat_generation_pending`,
`chat_generation_complete`, `chat_generation_error code=...`,
`chat_generation_cancelled`, `chat_generation_interrupted`.
У событий чата только случайный generation UUID; содержимое, user ID, имена
документов и исключения upstream не логируются. Ошибки пишутся на WARNING,
переходы — на INFO (для просмотра нужен INFO уровень соответствующих Python-логгеров).

Frontend разбирает start/delta/done/error, а ответ читает до EOF. При error даже с
HTTP 200 показывает ошибку; после этого перечитывает серверную историю. AbortController
используется кнопкой остановки и cleanup effect; при стабильном router обычные
обновления partial не меняют dependency report и не перезапускают effect.
Next.js rewrite проксирует `/api/chat` в backend; его фактическое поведение при
disconnect требует ручной проверки. Изменений frontend proxy и логики отмены нет.
Повторный запрос с активной арендой получает 409 до изменения pending; после успешного
сохранения аренда освобождается. Это закреплено интеграционными регрессиями.

По одному логу HTTP 200 нельзя утверждать, какая из веток сработала в конкретном
запуске. Указанные дефекты установлены чтением кода; выполнение регрессий и
подтверждение на реальной Qwen остаются за пользователем.

## Итоговый текстовый чат

Обычные сообщения и анализ документов проходят через общий Agent Loop и
выбранный в настройках Gateway. Режим JSON остаётся доступен для машинных
результатов. Внешнее извлечение продолжает требовать отдельного согласия
на конкретный документ.

Python вычисляет сравнения, динамику и отклонения, проверяет принадлежность
документов и передаёт модели исходный вопрос, проверенные строки, расчёты, полные
малые документы или выдержки RAG с метками `[S1]`. Именованный раздел читается
из сохранённого извлечения. При отсутствии раздела чат сообщает об этом.
Для больших документов поиск ограничен выбранными документами пользователя.
Уточнение с указательными словами может продолжить последний ответ с
источниками. Для документов история ограничена текущими, доступными
источниками в том же наборе документов. История имеет собственный бюджет.

Браузер сразу получает поток обычного ответа. Текст, опирающийся на документы,
удерживается до повторной проверки владельца, версии, поколения индекса и
хеша структурированных данных. Интерфейс показывает ссылки, полученные от
backend. Метки модели вне переданного набора удаляются. При сбое модели
доступные подтверждённые данные сохраняются вместе с сообщением о сбое.

Привязка к источнику защищает от подмены ссылок. Медицинскую истинность каждого
предложения свободного текста автоматически установить нельзя. Системный
промпт требует разделять факты документа и общие сведения, игнорировать
инструкции в документах, сообщать о недостающих данных и избегать диагнозов
и назначений. Числовые ответы и вычисления Python остаются проверяемой частью.

Удалены схема и валидаторы справочного текста, второй системный промпт и
переписывание обзора по совпадению слов или чисел. Старые парсеры фраз и
таблиц остаются для точных вопросов о датах, генетических фактах и числах в
документах без сохранённых измерений. Они больше не определяют допустимость
произвольного текстового ответа. Для вычислений используются существующие
функции Python как часть подготовки контекста. Дополнительный цикл инструментов
для этого не требуется.

### Бюджеты для рабочей станции с 16 ГБ RAM

| Параметр | Экономичный старт | Для развёрнутых ответов после проверки памяти |
| --- | --- | --- |
| `LLM_CONTEXT_TOKENS` | `2048` | `4096` |
| `LLM_MAX_TOKENS` | `384` | `768` |
| `LLM_TIMEOUT_SECONDS` | `180` | `180`, затем по фактической задержке |
| `CHAT_HISTORY_CHARS` | `1000` | `2000` |
| `CHAT_CONTEXT_CHARS` | `1000` | `2500` |

Это стартовые профили. Фактический расход зависит от квантизации, размещения в
RAM и VRAM и параллельной работы BGE-M3. Входной бюджет оценивается
консервативно по UTF-8. Выдержки и история сокращаются первыми, исходный вопрос
передаётся целиком при его вмещении. Слишком длинный вопрос получает явное
сообщение. Дополнительных лимитов на 256 или 512 токенов и фиксированного числа
измерений для текстового ответа нет. При лимите генерации доступный текст
обычного ответа сохраняется. При отмене незавершённая попытка остаётся
`interrupted`.

### Однократная финальная проверка без пересборки

Используйте Git Bash и существующие локальные тестовые образы. PostgreSQL здесь
изолирован. HTTP-вызовы Ollama и выдача Qdrant в интеграционных тестах подменены.

```bash
cd /c/workplace/Medical_AI_New
ct() {
  COMPOSE_DISABLE_ENV_FILE=1 MSYS_NO_PATHCONV=1 docker compose -p medical-ai-chat-test \
    -f infrastructure/compose.chat-test.yaml "$@"
}
backend_src=$(cygpath -am "$PWD/backend")
ct up -d --no-build --pull never --wait --wait-timeout 180 postgres &&
ct run --no-deps --pull never --rm \
  -v "$backend_src/app:/app/app:ro" -v "$backend_src/tests:/app/tests:ro" \
  -v "$backend_src/alembic:/app/alembic:ro" \
  -v "$backend_src/alembic.ini:/app/alembic.ini:ro" \
  -v "$backend_src/evaluation:/app/evaluation:ro" \
  tests python -m pytest -q --durations=8 tests/test_chat_evidence.py \
    tests/test_llm_gateway.py tests/test_gigachat_gateway.py tests/test_chat_integration.py

dc exec -T frontend npm test -- tests/chat-api.test.ts tests/chat-ui.test.tsx
dc exec -T frontend npm run typecheck
dc logs --since=10m backend
```

`dc` — обычная команда рабочего dev-проекта с тем же набором Compose-файлов.
После автоперезагрузки backend проверьте тот экземпляр Ollama и ту модель,
которые действительно использует приложение. Проверка отправляет синтетический
вопрос и результат инструмента через тот же Gateway:

```bash
dc exec -T backend python - <<'PY'
import asyncio

import httpx

from app.db import get_settings
from app.llm.gateway import ToolCall, gateway

settings = get_settings()
with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
    version = client.get(settings.llm_ollama_url + "/api/version")
    version.raise_for_status()
    show = client.post(settings.llm_ollama_url + "/api/show", json={"model": settings.llm_model})
    show.raise_for_status()
    print("Ollama", version.json()["version"], settings.llm_model, show.json().get("capabilities"))

async def probe():
    model = gateway(settings)
    tools = [{"type": "function", "function": {
        "name": "get_test_code",
        "description": "Read the synthetic test code.",
        "parameters": {"type": "object", "properties": {}},
    }}]
    messages = [
        {"role": "system", "content": "Use get_test_code to retrieve the code. Then report it."},
        {"role": "user", "content": "What is the test code? It is only available through the tool."},
    ]
    events = [event async for event in model.tool_stream(messages, tools)]
    (call,) = [event for event in events if isinstance(event, ToolCall)]
    assert call.name == "get_test_code" and call.arguments == {}
    messages += [
        {"role": "assistant", "content": "", "tool_calls": [call.message()]},
        {"role": "tool", "tool_name": call.name, "content": '{"code":"68427"}'},
    ]
    result = [event async for event in model.tool_stream(messages, tools)]
    assert all(isinstance(event, str) for event in result) and "68427" in "".join(result)
    print("Native tool round trip OK")

asyncio.run(probe())
PY
```

В Compose закреплён Ollama 0.6.8. В исходниках этой версии есть потоковая передача
вызовов инструментов, а библиотека Ollama публикует Qwen2.5:7b-instruct
с поддержкой tools. Фактический шаблон локально установленной модели проверяется
пробой выше.
[Ollama 0.6.8](https://github.com/ollama/ollama/blob/v0.6.8/server/routes.go#L1457-L1485)
· [Qwen2.5:7b-instruct](https://ollama.com/library/qwen2.5:7b-instruct).

Используйте уже загруженные синтетические документы. Не запускайте benchmark
заново. Проверьте одним проходом:

1. Общий диалог и следующий вопрос. История сохраняется, общие ответы идут
   потоком без поиска документов.
2. Объяснение одного документа и нескольких его показателей. Сверьте числа и
   единицы с карточкой. Попросите ещё одно пояснение без номера документа.
3. Конкретный показатель, отклонения, сравнение и динамика. Возьмите документы
   с разными значениями, единицами и датами. Точные вычисления сверьте по
   сохранённым строкам. В `04-multipage.pdf` изменение Albumin составляет 0%,
   для Potassium есть одна точка. Запросите оба показателя по-русски и по-английски.
4. Запрос о заключении и произвольный вопрос по нескольким документам.
   Проверьте документ с разделом и `01-grid.pdf` без заключения. Ссылки на
   источники должны вести на актуальные страницы.
5. Отмена генерации через «Остановить», вопрос о недоступном документе и
   недостаток данных. Убедитесь, что незавершённый ответ не сохраняется и что
   недоступные источники не появляются в истории.

Последовательные вызовы, неверный аргумент и недоступность Ollama проверяются
автоматизированным сценарием `test_native_agent_dialogue_sources_and_fallback`
с HTTP-подменой провайдера. Рабочий Ollama для этого останавливать не нужно.
Для успешных ответов после вызова инструментов в рабочем журнале ожидается
`chat_context_ready path=agent`.

Для итоговой записи в уже работающем локальном MLflow введите фактическое
число пройденных сценариев из списка и медиану `duration_ms` для успешных
синтетических запросов из `chat_generation_complete`. Сохраняются только
агрегаты.

```bash
read -r -p 'Пройдено сценариев из пяти: ' chat_passed
read -r -p 'Медиана duration_ms: ' chat_latency
dc exec -T -e CHAT_PASSED="$chat_passed" -e CHAT_LATENCY_MS="$chat_latency" mlflow python - <<'PY'
import os

import mlflow

mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("medical-ai-chat-final")
with mlflow.start_run(run_name="synthetic acceptance"):
    mlflow.log_metrics({
        "scenarios_passed": int(os.environ["CHAT_PASSED"]),
        "scenarios_total": 5,
        "latency_median_ms": float(os.environ["CHAT_LATENCY_MS"]),
    })
PY
```
