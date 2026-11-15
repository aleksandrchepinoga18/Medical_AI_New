# Файлы этапа 4

Созданы:

- `backend/alembic/versions/0003_personal_rag.py`
- `backend/app/rag/__init__.py`
- `backend/app/rag/models.py`
- `backend/app/rag/chunking.py`
- `backend/app/rag/adapters.py`
- `backend/app/rag/service.py`
- `backend/app/rag/indexing.py`
- `backend/app/rag/routes.py`
- `backend/app/rag/evaluation.py`
- `backend/scripts/backfill_rag.py`
- `backend/evaluation/personal_rag.json`
- `backend/tests/test_rag_unit.py`
- `backend/tests/test_rag_integration.py`
- `frontend/lib/rag-api.ts`
- `frontend/components/documents/personal-search.tsx`
- `frontend/components/documents/index-panel.tsx`
- `frontend/app/(private)/search/page.tsx`
- `frontend/tests/rag-api.test.ts`
- `frontend/tests/rag-ui.test.tsx`
- `infrastructure/compose.rag.yaml`
- `infrastructure/compose.rag-download.yaml`
- `infrastructure/compose.rag-test.yaml`
- `docs/STAGE4.md`
- `docs/STAGE4_IMPLEMENTATION_FILES.md`

Изменены:

- `.env.example` — документированы optional RAG-настройки; рабочий `.env` не изменён.
- `backend/app/core/config.py` — настройки и валидация.
- `backend/alembic/env.py` — регистрация RAG metadata для schema drift.
- `backend/app/documents/processing.py` — атомарное создание index job после извлечения.
- `backend/app/workers/ingestion_dispatcher.py` — дополнительный outbox в прежнем потоке.
- `backend/app/workers/celery_app.py` — очередь и задача индексации в существующем worker.
- `backend/app/main.py` — подключение защищённого RAG router.
- `frontend/components/app-shell.tsx` — пункт поиска.
- `frontend/components/documents/document-details.tsx` — статус поиска и якоря источников.
- `frontend/tests/documents-ui.test.tsx` — изоляция прежних UI-тестов от нового panel,
  полный locators-контракт синтетического mock.
- `infrastructure/compose.yaml` — передача optional RAG environment Backend/Worker.

Зависимости Python/npm не добавлены: REST использует стандартную библиотеку Python.
Существующие миграции, MinIO bootstrap и данные не изменены. Git-команды не выполнялись;
список составлен по выполненным редакторским изменениям, а не по Git diff.
