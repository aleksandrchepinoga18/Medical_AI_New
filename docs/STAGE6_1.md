# Этап 6.1. Медицинская база знаний

Модуль `app.knowledge` добавляет общий медицинский корпус. Операции реестра доступны
пользователям с назначением в `knowledge_admins`. Поиск доступен всем пользователям
с действующей сессией. Запросы записи и поиска используют существующие проверки
Origin и CSRF. Медицинский чат и Agent Loop на этом этапе продолжают работать
по своим существующим маршрутам.

## Данные и индексация

| Хранилище | Данные |
|---|---|
| PostgreSQL, `knowledge_admins` | UUID аккаунта администратора из `users` |
| PostgreSQL, `medical_sources` | UUID источника, название, тип материала, автор или организация, издатель, URL, DOI, PMID, даты публикации и обновления, специализация, язык, юрисдикция |
| PostgreSQL, `medical_source_versions` | UUID и метка версии, принадлежность источнику, признак текущей версии, статус, автор и время загрузки, SHA-256, формат, размер, ревизия проверки, проверяющий, дата и примечание проверки, поколение индекса, отпечаток настроек и число чанков |
| PostgreSQL, `medical_chunks` | UUID чанка, версия, текст, символьные смещения, локаторы страницы PDF или абзаца и строки таблицы DOCX |
| MinIO | Неизменяемый оригинал по ключу `knowledge/{source_id}/versions/{version_id}/original` в существующем закрытом bucket документов |
| Qdrant | Коллекция `medical_ai_knowledge_hybrid_v1` на существующем сервере Qdrant |

Миграция `0007_medical_knowledge` следует за `0006_document_extraction` и добавляет
четыре таблицы и индексы. Назначения администраторов изначально отсутствуют. Уникальные
ограничения действуют для нормализованного URL, DOI, PMID и пары «источник — версия».
Версию обновляют по UUID существующего источника. PostgreSQL разрешает только одну
текущую версию на источник. Повтор загрузки той же текущей версии с теми же
метаданными и SHA-256 возвращает существующую запись.

Статусы версии API — `uploaded`, `verified`, `published`, `outdated`, `rejected`.
Реестр дополнительно показывает `verification_status` и `freshness_status`.
Проверяющий сверяет происхождение с официальной страницей издателя, идентификаторы,
версию и содержимое, убеждается в отсутствии персональных данных и передаёт SHA-256
проверенного файла и примечание. Сервер сопоставляет SHA-256 и токен
`review_revision` с текущей версией. При загрузке, проверке, индексации и публикации
сервер отклоняет точные совпадения SHA-256 с записями персональных документов.
API загрузки принимает только отдельный файл и библиографические поля. Сервер
генерирует ключ MinIO из UUID самого медицинского источника и его версии.

Существующие валидатор файлов, проверка контрольной суммы при чтении, локальный
парсер PDF/DOCX, разбиение на чанки и пакетный вызов BGE-M3 используются повторно.
В Qdrant один пункт на чанк и поколение. Именованные векторы — `dense` с Cosine
и размерностью из настроек BGE-M3 (1024 по умолчанию), `bm25` со sparse
представлением и модификатором IDF. Для объединения веток используется серверный RRF.
В payload хранятся `pipeline`, `source_id`, `version_id`, `chunk_id`,
`generation`, `fingerprint`, `ordinal`, `start_char`, `end_char` и
`locators`. Идентификатор пункта — UUIDv5 от UUID чанка и поколения.

Индексация пишет новое поколение и сверяет число точек. PostgreSQL сохраняет
поколение вместе с чанками после успешной записи. Поиск передаёт в обе ветки
Hybrid Search только поколения текущих проверенных и опубликованных версий с
актуальным отпечатком настроек. После ответа Qdrant PostgreSQL повторно проверяет
доступность каждого результата и отдаёт текст, локаторы и библиографические
сведения. Содержание источников возвращается как данные; модуль работает без LLM.

## API

Пути указаны для backend. Через существующий frontend-прокси добавляется `/api`.

| Метод и путь | Условия и формат |
|---|---|
| `POST /knowledge/sources` | Администратор. Multipart с полем `file` (PDF/DOCX) и текстовым полем `metadata` (JSON). Возвращает `201` и карточку с UUID, меткой версии и SHA-256. Дубликат идентичности даёт `409`. |
| `GET /knowledge/sources` | Администратор. Список с `limit` (1–100) и курсором `after` из ответа `next_after`. |
| `PUT /knowledge/sources/{id}/metadata` | Администратор. Полный JSON метаданных без поля `version`. Изменение метаданных возвращает текущую версию в `uploaded` и требует повторной проверки и публикации. |
| `PUT /knowledge/sources/{id}/version` | Администратор. Тот же multipart, что при создании, с новой меткой версии. Прежняя версия исключается из выдачи, новая получает `uploaded`. |
| `PUT /knowledge/sources/{id}/status` | Администратор. JSON с `version_id` и `status`. Для `verified` дополнительно нужны `review_revision`, `verified_sha256` и `review_note`. Для `published` нужны выполненная проверка и готовый индекс. |
| `POST /knowledge/sources/{id}/index` | Администратор. JSON с `version_id`. Синхронный вызов для версии в `verified` или `published`. Повтор при существующем полном индексе возвращает текущий результат. |
| `POST /knowledge/search` | Пользователь с сессией. JSON `{"query":"synthetic example","k":5}`. Возвращает `items` с фрагментами, локаторами и ссылками на версии; при отсутствии доступных источников список пуст. |

Пример значения поля `metadata` для синтетического файла.

```json
{
  "title": "Synthetic guideline",
  "material_type": "guideline",
  "author": "Synthetic organization",
  "publisher": "Synthetic publisher",
  "url": "https://example.org/synthetic-guideline",
  "doi": null,
  "pmid": null,
  "publication_date": "2025-01-01",
  "update_date": null,
  "specialty": "Synthetic specialty",
  "language": "en",
  "jurisdiction": "Synthetic jurisdiction",
  "version": "1"
}
```

## Применение из Git Bash

Пример для работающего dev-стека с Ollama в Compose. Если рабочий стек использует
другой набор Compose-файлов или Ollama на Windows, используйте его текущую
команду `dc` и проверьте доступность уже установленной BGE-M3 в этом Ollama.
Применяйте миграцию после резервной копии PostgreSQL по действующей процедуре.

```bash
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml -f infrastructure/compose.rag.yaml "$@"; }
dc build backend
dc run --rm --no-deps backend alembic upgrade head
dc run --rm --no-deps backend alembic current
dc run --rm --no-deps --entrypoint python minio-init /bootstrap/init-documents.py provision-knowledge
dc up -d --no-deps backend
```

Ожидаются ревизия `0007_medical_knowledge (head)` и сообщение MinIO
`Knowledge prefix policy attached to the document storage account.` Команда
`provision-knowledge` выполняется один раз после штатного provisioning закрытого
bucket документов. Она добавляет отдельную политику для `knowledge/*` существующему
аккаунту backend. При прерванном provisioning проверьте, появилась ли политика и
прикрепилась ли она к аккаунту, прежде чем повторять действия.
Backend создаёт коллекцию Qdrant при первом запуске индексации после получения
валидного embedding.

Для первого назначения подставьте UUID уже зарегистрированного доверенного
администратора из `GET /auth/me`. Выполните команду с доступом оператора БД.

```bash
read -rp 'Admin account UUID: ' admin_id
dc exec -T postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' sh -v admin_id="$admin_id" <<'SQL'
INSERT INTO knowledge_admins (user_id) VALUES (:'admin_id'::uuid)
ON CONFLICT (user_id) DO NOTHING;
SELECT user_id FROM knowledge_admins WHERE user_id = :'admin_id'::uuid;
SQL
```

## Проверка из Git Bash

```bash
dc run --rm --no-deps backend python -m pytest -q -m 'not integration'
dc run --rm --no-deps backend ruff check app/knowledge app/rag app/documents/routes.py app/documents/processing.py app/integrations/object_storage.py tests/test_medical_knowledge.py alembic/versions/0007_medical_knowledge.py
```

Ожидается успешное завершение команд на dev-образе backend. Интеграционный тест
использует существующий отдельный RAG-стек с синтетической PostgreSQL и Qdrant,
локальный BGE-M3 и тестовый транспорт MinIO. Остальные варианты запуска Ollama
описаны в [Hybrid RAG](HYBRID_RAG.md).

```bash
rt() { docker compose -p medical-ai-knowledge-test -f infrastructure/compose.rag-test.yaml "$@"; }
rt up -d --build postgres redis qdrant ollama
rt exec ollama ollama list
rt exec ollama ollama pull bge-m3:567m
rt build tests worker
rt run --rm tests python -m pytest -q -m integration tests/test_medical_knowledge.py
```

Ожидается `1 passed`. Тест проверяет роли и CSRF, дубликаты, принадлежность
оригинала к корпусу, переходы статусов, неудачную индексацию и повтор, оба поисковых
маршрута, публикацию новой версии и исключение старой.

## Однократная проверка с реальным MinIO

Запустите из корня репозитория в Git Bash, где уже определена функция `gc` для
работающего dev-стека. Используйте аккаунт с назначением в `knowledge_admins`.
Пароль вводится без отображения и передаётся через stdin.

```bash
(
  set +x +v
  set -o pipefail
  read -r -p 'Knowledge admin email: ' smoke_email || exit 1
  read -r -s -p 'Knowledge admin password: ' smoke_password || exit 1
  printf '\n'
  smoke_code=$(cat backend/scripts/smoke_medical_knowledge.py) || exit 1
  printf '%s\n' "$smoke_email" "$smoke_password" |
    MSYS2_ARG_CONV_EXCL='*' gc exec -T backend python -u -c "$smoke_code"
)
```

Ожидается `PASS: medical knowledge smoke check.` Скрипт генерирует одностраничный
PDF в памяти, подтверждает загрузку в MinIO и чтение при индексации, проверяет
публикацию, библиографические поля и локатор страницы 1 в Hybrid Search.
Затем он переводит источник в `outdated` и проверяет, что источник исчез из выдачи.
Запросы идут через HTTP API запущенного backend. Индексация и поиск используют
локальную BGE-M3.

При ошибке после загрузки скрипт всё равно пытается перевести источник в
`outdated`. Если статус не удалось подтвердить или процесс был прерван, используйте
выведенные UUID источника и версии. В сессии администратора отправьте
`PUT /knowledge/sources/{id}/status` с телом
`{"version_id":"<UUID версии>","status":"outdated"}`. Синтетическая запись и её
оригинал сохраняются для истории.

## Ограничения

- Происхождение, право на публикацию, отсутствие персональных данных в новых
  материалах и актуальность подтверждает администратор. SHA-256 выявляет совпадение
  байтов. Разные копии одного материала с различными идентификаторами требуют
  ручной сверки.
- Индексация синхронная. Для сканов и PDF с неполным текстовым слоем нужен
  подготовленный текстовый оригинал. Большие материалы ограничены квотами и
  таймаутами действующей обработки документов и RAG.
- Снимок библиографических полей хранится на уровне реестра источников. У старых
  версий сохраняются собственный файл, идентификатор, контрольная сумма, статус и
  данные проверки. Автоматическая очистка файлов и истории отсутствует.
- Пункты снятой версии могут оставаться в Qdrant до следующей перестройки индекса.
  Они исключены из кандидатов поиска; их токены до удаления могут влиять на общую
  статистику IDF коллекции. Незавершённая запись MinIO может оставить объект без
  записи реестра.
- Версия индекса зависит от отпечатка настроек embeddings и чанкинга.
  Для восстановления выдачи после изменения этих настроек запустите индексацию
  опубликованных версий. Модель и digest берутся из действующей конфигурации RAG.
