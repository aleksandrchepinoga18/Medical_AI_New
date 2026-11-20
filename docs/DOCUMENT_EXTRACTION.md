# Извлечение документов в карточке и чате

Обычная загрузка PDF выполняет PyMuPDF Tables в существующем subprocess.
Текст и локаторы сохраняются прежним путём. DOCX по-прежнему обрабатывает
python-docx. Извлечение таблиц ограничено 400 строками на PDF. Если сбор
таблиц остановился по лимиту или ошибка затронула страницу, локальный
результат отмечается как `truncated`, а текст и локаторы сохраняются.

## Конфигурация

| Переменная | Значение по умолчанию | Назначение |
| --- | --- | --- |
| `DOCUMENTS_EXTRACTION_PROVIDER` | `pymupdf_tables` | Значение `gigachat_ultra` разрешает отдельную внешнюю операцию. Обычная обработка остаётся локальной. |
| `LLM_PROVIDER` | `ollama` | Независимый выбор генерации ответа через существующий LLM Gateway. |
| `GIGACHAT_CREDENTIALS` | пусто | Ключ OAuth для worker. Передаётся как секрет через окружение. |
| `GIGACHAT_SCOPE` | `GIGACHAT_API_PERS` | Область OAuth для поддерживаемого аккаунта Ultra. |
| `GIGACHAT_MODEL` | `GigaChat-3-Ultra` | Идентификатор запроса из benchmark. Версия сборки берётся из ответа, например `32.9.16.9`. |
| `GIGACHAT_CA_DIR` | `./certs/gigachat` | Каталог на хосте для Compose. Относительный путь считается от каталога первого compose-файла (`infrastructure`). |
| `GIGACHAT_CA_BUNDLE` | пусто | Дополнительный доверенный PEM внутри worker при необходимости. |

При ручном развёртывании внешнего режима подключается
`infrastructure/compose.extraction.yaml` к обычному набору Compose-файлов.
Он добавляет исходящую сеть существующему worker и передаёт ключ только в его
контейнер. Доступ к GigaChat ограничивается в клиенте фиксированными HTTPS
адресами OAuth и API. Для ограничения исходящих соединений на уровне сети
используйте политику размещения. Ключ вводите скрыто в Git Bash или передавайте
через хранилище секретов. Override монтирует `GIGACHAT_CA_DIR` только для чтения
в `/run/gigachat-ca`. Контекст TLS использует стандартные CA Python и добавляет
CA из `GIGACHAT_CA_BUNDLE` для OAuth и основного API. HTTP-клиент не использует
прокси из переменных окружения.
Не печатайте развёрнутое окружение Compose с загруженным ключом.

## Подключение сертификата в Docker

Для прямого подключения начните с `Russian Trusted Root CA` НУЦ Минцифры.
Сбер указывает этот корень для OAuth. Сертификат берите по ссылке из официальной
инструкции с включённой проверкой HTTPS. Если загрузка через `curl` завершается
ошибкой доверия, скачайте PEM со страницы Госуслуг через доверенный браузер
и сохраните его под тем же именем.
[Инструкция Сбера](https://developers.sber.ru/docs/ru/gigachat/certificates)

```bash
cd /c/workplace/Medical_AI_New
ca_dir=infrastructure/certs/gigachat
ca_name=russian_trusted_root_ca_pem.crt
curl --fail --silent --show-error --location --proto '=https' \
  --output "$ca_dir/$ca_name" \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
openssl x509 -in "$ca_dir/$ca_name" -noout -subject -issuer -dates -sha256 -fingerprint
export GIGACHAT_CA_BUNDLE="/run/gigachat-ca/$ca_name"
```

Убедитесь, что файл читается как PEM, содержит `Russian Trusted Root CA` и срок
действия не истёк. Если официальная инструкция для фактической цепочки требует
промежуточный `Russian Trusted Sub CA`, скачайте его на Госуслугах. Проверьте
`openssl verify -CAfile "$ca_dir/$ca_name" "$ca_dir/russian_trusted_sub_ca_pem.crt"`,
затем объедините проверенные PEM в отдельный файл и укажите его в переменной.

Адрес `198.18.0.133` находится в диапазоне `198.18.0.0/15` для сетевого
тестирования. Такой адрес может выдать VPN с подстановкой DNS. Он сам по себе
не показывает, какой CA подписал цепочку. Если проверка с корнем Минцифры
не проходит, проверьте правила VPN для `ngw.devices.sberbank.ru:9443` и
`api.giga.chat:443` из Docker. Если организация намеренно подменяет TLS,
возьмите одобренный CA у её администратора и сверьте SHA-256 по отдельному
доверенному каналу. Сертификаты из непроверенного соединения не добавляйте
в bundle. [Реестр IPv4 IANA](https://www.iana.org/assignments/iana-ipv4-special-registry/iana-ipv4-special-registry.xhtml)

Выполните команды в той же сессии, из которой обычно запускаете Compose.
Сохраните привычные `-f` и имя проекта. В примере используется dev-проект.
Compose снова берёт ключ из уже настроенного источника при создании worker.
Если ключ существовал только в окружении прежнего контейнера, заранее
передайте его из вашего хранилища секретов в локальное окружение.

```bash
dc() {
  MSYS2_ENV_CONV_EXCL=GIGACHAT_CA_BUNDLE docker compose --env-file .env \
    -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml \
    -f infrastructure/compose.extraction.yaml "$@"
}
dc config --quiet
dc up -d --no-deps --no-build --force-recreate --wait --wait-timeout 180 worker
dc exec -T worker sh -ec 'test -n "$GIGACHAT_CA_BUNDLE" && test -r "$GIGACHAT_CA_BUNDLE"'
```

Для подключения CA достаточно пересоздать worker. В dev новый Python-код и
сообщения интерфейса доступны из bind mounts. Если используются runtime-образы,
для новых логов пересоберите worker, а для новых сообщений интерфейса отдельно
обновите frontend обычным способом. Проверки ниже не используют PDF и выводят
только статус, безопасный код ошибки или HTTP-статус.

```bash
dc exec -T worker python - <<'PY'
import asyncio

import httpx

from app.core.config import Settings
from app.documents.gigachat import API, GigaChatExtraction, gigachat_tls_context, is_tls_error
from app.llm.gateway import GatewayError


async def main():
    settings = Settings()
    if not settings.gigachat_credentials.get_secret_value().strip():
        raise SystemExit("OAuth: EXTERNAL_NOT_CONFIGURED")
    try:
        tls = gigachat_tls_context(settings.gigachat_ca_bundle)
    except (OSError, ValueError):
        raise SystemExit("TLS: EXTERNAL_TLS_CONFIG") from None
    async with httpx.AsyncClient(
        verify=tls, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(60, connect=5),
    ) as client:
        adapter = GigaChatExtraction(
            client, settings.gigachat_credentials, settings.gigachat_model,
            "schema", scope=settings.gigachat_scope,
        )
        try:
            headers = await adapter.headers(timeout=60)
        except GatewayError as error:
            raise SystemExit(f"OAuth: {error}") from None
        print("OAuth: OK")
        try:
            response = await client.get(f"{API}/models", headers=headers)
        except httpx.TimeoutException:
            raise SystemExit("GET /models: timeout") from None
        except httpx.HTTPError as error:
            category = "tls" if is_tls_error(error) else "network"
            raise SystemExit(f"GET /models: {category}") from None
        print(f"GET /models: HTTP {response.status_code}")
        if not response.is_success:
            raise SystemExit(1)


asyncio.run(main())
PY
```

Ожидаются `OAuth: OK` и `GET /models: HTTP 200`. Затем загрузите синтетический
PDF. До явного согласия в карточке должен появиться только результат PyMuPDF.
Нажмите «Обработать через GigaChat Ultra», подтвердите передачу и дождитесь
результата. В `dc logs --since=5m worker` ожидаются четыре события
`gigachat_extraction` со статусом `succeeded` для `oauth`, `upload`, `completion`
и `delete`. Ошибки содержат `stage`, `duration_ms`, `error_category` и
`error_code`. При неподтверждённой загрузке или удалении по-прежнему действует
блокировка повторной отправки.

## Внешняя операция и источники

Аутентифицированный владелец отправляет
`POST /documents/{document_id}/external-extraction` с действующим CSRF и JSON
в следующем формате. `sha256` и `text_version` берутся из карточки документа.

```json
{
  "provider": "gigachat_ultra",
  "consent": true,
  "sha256": "<sha256 оригинала>",
  "text_version": 1
}
```

API принимает PDF до 5 МиБ в состоянии `ready` или `needs_ocr`. Оно сохраняет
разрешение с владельцем, версией, хешем и временем в существующем задании
ingestion. Состояние доступно владельцу через
`GET /documents/{document_id}/external-extraction/{job_id}`. Повтор того же
запроса возвращает активное или успешно завершённое задание для той же версии.
Внешние задания расходуют существующий лимит заданий на документ.

Worker повторно проверяет разрешение, читает оригинал через MinIO и сверяет
размер и SHA-256 перед извлечением. Обычная загрузка, retry, индексация и чат
внешних заданий не создают. Локальный запуск и внешние запросы делят общий
бюджет обработки, внутри которого резервируется время на удаление вложения.
Ключ, запрос, медицинский текст и тело ответа провайдера в технические логи
не записываются. После сетевой ошибки с неизвестным результатом загрузки,
неподтверждённого удаления или потери worker в процессе повторная внешняя
отправка блокируется. Для ручной сверки имя вложения строится как
`document-extraction-<job_id без дефисов>.pdf`. Если провайдер вернул ID,
при ошибке удаления он хранится в служебном поле задания. Локальный результат
остаётся доступным.

Результат `text_version.structured_result` в карточке содержит версию схемы
и блоки `local` и `external`. Для внешних записей `reported_page` хранит
утверждение модели. `source` содержит страницу и прямоугольник PyMuPDF только
при совпадении всех полей с единственной локальной строкой на той же странице.
Если локальная проверка неполна, точный источник отсутствует или страница
расходится, статус `unverified`. Повторы дают `ambiguous`. Поле
`reconstructed_row` хранит реконструкцию ячеек. Статус `confirmed` обозначает
привязку к исходной таблице. Скан без независимого подтверждения остаётся
`unverified`, даже если ответ модели заполнен.

Внешний результат добавляется к той же версии текста, сохраняя её текст,
локаторы и ссылки индекса. Миграция `0006_document_extraction` добавляет два
nullable JSONB-поля. Старые строки остаются читаемыми и получают
`structured_result: null` до нового извлечения. Перед включением внешнего
режима версии backend и worker должны поддерживать новую схему.

## Карточка, старые документы и чат

- В карточке PDF можно открыть оригинал в браузере и скачать его из MinIO.
  Просмотр использует существующий `/download?inline=true` с прежней проверкой
  владельца. Для DOCX доступно скачивание оригинала. Отдельная кнопка
  скачивает извлечённый текст как TXT.
- Карточка показывает результат и статус каждого провайдера. Диалог Ultra
  подтверждает передачу полного оригинала конкретного PDF. В запрос входят
  SHA-256 и версия, показанная при открытии диалога. При изменении версии
  нужно открыть диалог заново.
- `POST /documents/{document_id}/refresh-extraction` обновляет структурированные
  данные из MinIO через существующий worker. Если текст и локаторы совпали,
  версия и индекс сохраняются. При изменившемся тексте или локаторах worker
  создаёт версию штатным способом. Миграция для этой интеграции не требуется.
- Для результатов, сравнений, отклонений и динамики чат сначала читает
  подтверждённые структурированные записи. Python проверяет границы,
  совместимость единиц и даты. При запросе объяснения Ollama получает уже
  подготовленные данные. Чат выводит вычисленные значения и отдельно сообщает
  количество непроверенных и неоднозначных записей. Источники подтверждённых
  измерений привязаны к локаторам и сверяются с текущим структурированным
  результатом перед публикацией и при чтении истории.
- RAG остаётся для вопросов по обычному тексту. Для старых версий без
  `structured_result` остаётся прежний путь. После проверки покрытия
  кандидатами на удаление станут восстановление таблиц из плоского текста
  в `chat/lab_tables.py` и числовой разбор обзоров в
  `chat/document_context.py` и `chat/evidence.py`. До удаления нужно
  перевести их оставшихся потребителей и проверить старые версии.

## Ручная проверка в Git Bash

```bash
cd /c/workplace/Medical_AI_New/backend
python -m pytest -q tests/test_document_extraction_providers.py \
  tests/test_gigachat_extraction.py tests/test_document_extraction_benchmark.py \
  tests/test_document_processing_limits.py tests/test_document_migration.py
```

Ожидаются локальные результаты с координатами, сохранённые поля старого
документа и HTTP-вызовы только через подменный транспорт тестов. Проверка
миграции генерирует SQL в памяти.

Для проверки связи API, worker и PostgreSQL используйте отдельный тестовый
Compose-проект с синтетическими данными. Его тестовая БД применяет миграции
сама во время подготовки теста. Если тестовые образы уже собраны и Python-
зависимости не изменились, подключите dev override. Исходники, тесты и миграции
монтируются только для чтения. Worker пересоздаётся, чтобы заново импортировать
актуальный код, а зависимости берутся из тестовых образов.

```bash
cd /c/workplace/Medical_AI_New
dt() {
  COMPOSE_DISABLE_ENV_FILE=1 docker compose -p medical-ai-documents-extraction-test \
    -f infrastructure/compose.documents-test.yaml \
    -f infrastructure/compose.documents-test.dev.yaml "$@"
}
dt config --quiet &&
dt up -d --no-build --pull never --wait --wait-timeout 240 postgres redis minio &&
dt up -d --no-deps --no-build --pull never --force-recreate \
  --wait --wait-timeout 240 worker &&
dt run --no-deps --pull never --rm tests \
  python -m pytest -q tests/test_document_ingestion_integration.py \
  -k 'external_requires_owner_consent or refresh_legacy_pdf'
```

В интеграционной проверке отправка без разрешения и доступ другого аккаунта
отклоняются, разрешённый запрос обрабатывается через MockTransport один раз,
вложение удаляется, а версия текста и локаторы сохраняются.

При отсутствии локального образа или при изменении зависимостей нужна сборка
соответствующего тестового образа. Для диагностики ошибки хеша в Python-слое
используйте `BUILDKIT_PROGRESS=plain dt build worker`. Сохраните строку с именем
и URL артефакта перед сообщением `THESE PACKAGES DO NOT MATCH THE HASHES` и шаг,
на котором она появилась. Замаскируйте учётные данные в URL перед передачей
лога. Даже без `requirements-dev.lock` pip сверяет хеш из ссылки индекса,
когда индекс его публикует. Сверьте ожидаемый SHA-256 с метаданными точного
файла в доверенном индексе и проверьте путь загрузки из Docker, включая
зеркало, прокси или VPN, если они используются. При расхождении сборка должна
остаться заблокированной до выяснения причины.
[Документация pip](https://pip.pypa.io/en/stable/topics/secure-installs/#using-hashes-from-pypi-or-other-index-servers)

Для проверки карточки, локального обновления и чата:

```bash
cd /c/workplace/Medical_AI_New/frontend
npm test -- tests/documents-ui.test.tsx tests/documents-api.test.ts
npm run typecheck

cd /c/workplace/Medical_AI_New
COMPOSE_DISABLE_ENV_FILE=1 docker compose -p medical-ai-chat-test \
  -f infrastructure/compose.chat-test.yaml run --build --rm tests \
  python -m pytest -q tests/test_chat_integration.py -k structured_chat
```

Ожидаются отдельные ссылки просмотра и скачивания, запрос Ultra только после
подтверждения, прежние ID версии и индекса после обновления старого PDF,
и числовые ответы чата с источниками только из документов владельца.
