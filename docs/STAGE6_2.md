# Этап 6.2. Наполнение медицинского корпуса

Раздел `/knowledge` показывает реестр администраторам из `knowledge_admins`.
Проверка доступа выполняется на сервере через `GET /knowledge/access`.
Этот маршрут возвращает `204` администратору с действующей сессией, `403`
обычному пользователю и `401` без сессии. Интерфейс использует API этапа 6.1
и добавляет операции подтверждения с публикацией и скачивания текущего оригинала.
Используются текущие PostgreSQL, MinIO, BGE-M3 и коллекция
`medical_ai_knowledge_hybrid_v1`.

## Запуск в существующем dev-стеке

В командах ниже используется уже определённая в Git Bash функция `gc` вашего
рабочего Compose-проекта. Запускайте из корня репозитория. В dev-стеке исходники
backend и frontend подключены через bind mounts. Откройте приложение после
автоперезагрузки. При необходимости перечитайте исходники перезапуском:

```bash
gc restart backend frontend
```

Для этих изменений достаточно исходников из bind mounts. Миграции и
пересборка образов не требуются.

## Первый источник через интерфейс

1. Возьмите **Complete Blood Count (CBC)** из [каталога](MEDICAL_KNOWLEDGE_CATALOG.md).
   Сохраните текст раздела Medical Tests в PDF с текстовым слоем или в DOCX,
   сохранив заголовок, атрибуцию NLM, официальный URL и дату обновления. Условия
   использования и ограничения указаны в каталоге.
2. Войдите существующим администратором. Откройте **Медицинская база знаний →
   Добавить источник**. Укажите название, тип «Справка о лабораторном тесте»,
   автора и издателя National Library of Medicine, официальный URL, направление
   «Общий анализ крови», язык `en`, юрисдикцию «США», дату обновления
   `2024-10-15` и метку версии `2024-10-15`. Выберите файл и нажмите **Загрузить**.
3. В карточке нажмите **Скачать оригинал**. Сверьте файл с официальной
   страницей, метаданными и условиями воспроизведения.
4. Нажмите **Подтвердить и опубликовать** и подтвердите действие. SHA-256
   вычисляется сервером при загрузке и подставляется автоматически.
   После подтверждения сервер запускает индексацию. Статус **Доступен для
   поиска** появляется после её успешного завершения. При ошибке нажмите
   **Повторить обработку**.
5. В блоке **Проверка медицинского поиска** запросите `complete blood count`.
   Сверьте фрагмент, название, URL, версию, локатор и оценку RRF. При необходимости
   откройте **Дополнительно → Снять с публикации**. Там же доступны
   редактирование метаданных, технические сведения и **Загрузить новую
   версию**. Новая версия требует отдельного подтверждения.

Для нескольких источников отметьте карточки и нажмите **Подтвердить и
опубликовать выбранные**. Операции выполняются последовательно. Ошибка одного
источника отображается в его карточке, и обработка пакета продолжается.
Состояния **Обрабатывается** и **Ошибка** отображаются в текущей вкладке.
Подтверждение проверки сохраняется на сервере при ошибке индексации. После
обновления страницы этот источник можно снова отправить на обработку.

Оригинал доступен администратору через
`GET /knowledge/sources/{id}/versions/{version_id}/original`. Запрос
`POST /knowledge/sources/{id}/review-publish` проверяет роль, CSRF, версию,
ревизию метаданных и SHA-256. Отдельные операции API этапа 6.1 остаются
доступными.

## Пакетный импорт

Формат показан в [синтетическом JSON-манифесте](knowledge-manifest.example.json).
Скопируйте его как `knowledge-corpus/manifest.json`. Укажите файлы, реальные
SHA-256 и метаданные, включая публичный URL источника. Путь каждого файла
задаётся относительно манифеста с разделителями `/`. Хеш в примере служит
заполнителем. Учётные данные вводятся
при запуске. Убедитесь, что право на полный текст распространяется на способ
использования корпуса.

Передайте скрипт и локальный каталог в работающий backend без сборки образа:

```bash
import_dir=$(gc exec -T backend mktemp -d) &&
  MSYS2_ARG_CONV_EXCL='*' gc cp ./backend/scripts/import_medical_knowledge.py backend:/app/scripts/import_medical_knowledge.py &&
  MSYS2_ARG_CONV_EXCL='*' gc cp ./knowledge-corpus/. "backend:$import_dir/"
```

Проверьте файлы и манифест без подключения к API:

```bash
MSYS2_ARG_CONV_EXCL='*' gc exec -T backend python -m scripts.import_medical_knowledge "$import_dir/manifest.json" --dry-run
```

Для загрузки используйте ту же директорию. Пароль передаётся через stdin без
отображения, отчёт сохраняется локально:

```bash
(
  set +x +v
  set -o pipefail
  read -r -p 'Knowledge admin email: ' import_email || exit 1
  read -r -s -p 'Knowledge admin password: ' import_password || exit 1
  printf '\n' >&2
  printf '%s\n' "$import_email" "$import_password" |
    MSYS2_ARG_CONV_EXCL='*' gc exec -T backend python -m scripts.import_medical_knowledge "$import_dir/manifest.json" \
      --credentials-stdin > knowledge-import-report.json
)
```

`ready` означает успешную локальную проверку, `uploaded` — подтверждённую
загрузку, `already_uploaded` — совпадение идентификаторов, версии и SHA-256
с записью реестра. `failed` содержит причину по конкретному файлу. При любой
ошибке код возврата ненулевой. При нарушении схемы манифеста весь импорт
останавливается до загрузки. Повторите ту же команду после исправления ошибок.
CLI заново проходит реестр по страницам и пропускает уже загруженные файлы,
включая записи, сохранённые до потери HTTP-ответа. Для совпадающего источника
с другим файлом или версией используйте операцию обновления в интерфейсе.
Импорт создаёт записи в состоянии `uploaded`. Проверку и публикацию выполняет
администратор.

## Подготовка семи материалов MedlinePlus

Утилита запрашивает семь канонических HTTPS-страниц
[из каталога](MEDICAL_KNOWLEDGE_CATALOG.md). Она сохраняет основной текст,
подзаголовки и списки из раздела Medical Tests, пропускает блоки навигации,
рекламу, иллюстрации, ссылки в разделе References и размеченные сторонние
материалы. В каждом DOCX указаны исходный URL, дата обновления и атрибуция
National Library of Medicine. [Условия использования MedlinePlus](https://www.medlineplus.gov/about/using/usingcontent/)

Выполните из корня проекта в Git Bash с существующей функцией `gc`.
Команда использует образ работающего backend. Сформированные файлы записываются
напрямую в `C:/workplace/Medical_AI_New_Backup/knowledge-corpus/medlineplus/`.

```bash
corpus='/c/workplace/Medical_AI_New_Backup/knowledge-corpus/medlineplus'
mkdir -p "$corpus"
corpus_bind=$(cygpath -am "$corpus")
scripts_bind=$(cygpath -am "$PWD/backend/scripts")
backend_image=$(gc images -q backend)

MSYS2_ARG_CONV_EXCL='*' docker run --rm --pull never \
  --mount "type=bind,source=$scripts_bind,target=/app/scripts,readonly" \
  --mount "type=bind,source=$corpus_bind,target=/corpus" \
  "$backend_image" python -m scripts.prepare_medlineplus_corpus --output /corpus
```

Ожидаются семь DOCX и `manifest.json` с их SHA-256. Повторный запуск использует
те же имена файлов и оставляет побайтово совпадающие файлы нетронутыми. Поле
`version` содержит дату обновления страницы. Если источник с предыдущей версией
уже есть в реестре, загрузите новую версию через интерфейс.

Скопируйте комплект в запущенный backend и проверьте манифест прежним CLI:

```bash
import_dir=$(gc exec -T backend mktemp -d) &&
  MSYS2_ARG_CONV_EXCL='*' gc cp ./backend/scripts/import_medical_knowledge.py backend:/app/scripts/import_medical_knowledge.py &&
  MSYS2_ARG_CONV_EXCL='*' gc cp ../Medical_AI_New_Backup/knowledge-corpus/medlineplus/. "backend:$import_dir/"

MSYS2_ARG_CONV_EXCL='*' gc exec -T backend \
  python -m scripts.import_medical_knowledge "$import_dir/manifest.json" --dry-run
```

Ожидаются семь записей `ready`. Чтобы выполнить пакетную загрузку, введите
учётные данные существующего администратора:

```bash
(
  set +x +v
  set -o pipefail
  read -r -p 'Knowledge admin email: ' import_email || exit 1
  read -r -s -p 'Knowledge admin password: ' import_password || exit 1
  printf '\n' >&2
  printf '%s\n' "$import_email" "$import_password" |
    MSYS2_ARG_CONV_EXCL='*' gc exec -T backend \
      python -m scripts.import_medical_knowledge "$import_dir/manifest.json" \
        --credentials-stdin > "$corpus/import-report.json"
)
```

Ожидаются `uploaded` или, при совпадении с реестром, `already_uploaded`.
Перед публикацией администратор сверяет содержимое DOCX, происхождение и права.
Фильтр ориентируется на разметку, заголовки и пометки о правах. Если MedlinePlus
изменит шаблон, дату или URL, утилита завершится с ошибкой при невозможности
однозначно извлечь статью. Неразмеченные сторонние вставки требуют ручной сверки.

Синтетические тесты в существующем dev-образе можно выполнить без доступа к сети:

```bash
tests_bind=$(cygpath -am "$PWD/backend/tests")
MSYS2_ARG_CONV_EXCL='*' docker run --rm --pull never --network none \
  --mount "type=bind,source=$scripts_bind,target=/app/scripts,readonly" \
  --mount "type=bind,source=$tests_bind,target=/app/tests,readonly" \
  "$backend_image" python -m pytest -q tests/test_prepare_medlineplus_corpus.py
```

Ожидается `3 passed`. Тесты проверяют извлечение и исключения, запрет
посторонних URL и редиректов, совместимость манифеста и повторный запуск.

## Тестирование без сборки

Проверьте frontend и CLI в работающем dev-стеке. Копирование CLI нужно один раз
на текущий контейнер backend:

```bash
MSYS2_ARG_CONV_EXCL='*' gc cp ./backend/scripts/import_medical_knowledge.py backend:/app/scripts/import_medical_knowledge.py
gc exec -T frontend npm run typecheck
gc exec -T frontend npm test -- tests/knowledge-api.test.ts tests/knowledge-ui.test.tsx
gc exec -T backend python -m pytest -q tests/test_knowledge_import.py
gc exec -T backend ruff check app/knowledge tests/test_medical_knowledge.py
```

Ожидаются успешный typecheck, проверки прав доступа, подтверждения пакета,
скачивания оригинала и повторной обработки, отображения выдачи, валидации
и повторного импорта после сбоя. Для проверки прав
на реальном PostgreSQL и поиска в Qdrant используйте уже подготовленный
синтетический RAG-стек этапа 6.1 с моделью BGE-M3. Если у стека другое
имя Compose-проекта, используйте его в функции `rt`:

```bash
rt() {
  COMPOSE_DISABLE_ENV_FILE=1 MSYS_NO_PATHCONV=1 docker compose -p medical-ai-knowledge-test \
    -f infrastructure/compose.rag-test.yaml "$@"
}
backend_src=$(cygpath -am "$PWD/backend")
rt run --rm --no-deps --pull never \
  -v "$backend_src/app:/app/app:ro" -v "$backend_src/tests:/app/tests:ro" \
  -v "$backend_src/alembic:/app/alembic:ro" \
  tests python -m pytest -q -m integration tests/test_medical_knowledge.py
```

Ожидается `1 passed`. Тест применяет миграции в синтетической БД и проверяет
`401`, `403`, `204`, скачивание текущей версии и публикацию после успешной
индексации, включая повтор после сбоя.

## Ограничения

- `--dry-run` проверяет локальные файлы, метаданные и дубликаты внутри манифеста.
  Сверка с реестром выполняется при загрузке по текущим версиям. При параллельном
  запуске PostgreSQL защищает совпадающие URL, DOI и PMID. Защита по хешу для
  одновременно создаваемых записей с разными библиографическими идентификаторами
  ограничена снимком реестра. Для одного набора файлов запускайте один импорт.
- Лицензии и происхождение проверяет администратор. Источники не загружаются
  по URL. Индексация синхронная и требует извлекаемого текста.
- Раздел показывает текущую версию. Новая версия или изменение метаданных
  снимают источник с выдачи до повторной публикации. RRF — оценка из слияния
  результатов плотного и лексического поиска.
