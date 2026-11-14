# Файлы подэтапов 3.2–3.3

Список дополняет файлы этапа 3.1, описанные в STAGE3.md. Код подготовлен,
проверки агентом не запускались, приёмка ожидается.

Новые Backend-файлы:

- `backend/app/documents/schemas.py`
- `backend/app/documents/validation.py`
- `backend/app/documents/service.py`
- `backend/app/documents/routes.py`
- `backend/app/documents/extract.py`
- `backend/app/documents/processing.py`
- `backend/app/documents/tasks.py`
- `backend/app/workers/ingestion_dispatcher.py`
- `backend/tests/test_document_extraction.py`
- `backend/tests/test_document_upload_limits.py`
- `backend/tests/test_document_processing_limits.py`
- `backend/tests/test_document_ingestion_integration.py`

Изменены Backend-файлы: `backend/app/core/config.py`, `backend/app/main.py`,
`backend/app/integrations/object_storage.py`, `backend/app/workers/celery_app.py`,
`backend/pyproject.toml`, `backend/tests/test_health.py`.

Новые Frontend-файлы:

- `frontend/postcss.config.mjs`, `frontend/components.json`, `frontend/app/globals.css`
- `frontend/lib/utils.ts`, `frontend/lib/documents-api.ts`
- `frontend/components/app-shell.tsx`
- `frontend/components/ui/button.tsx`, `card.tsx`, `badge.tsx`, `input.tsx`, `sheet.tsx`, `README.md`
- `frontend/components/documents/status-badge.tsx`, `document-list.tsx`, `upload-form.tsx`, `document-details.tsx`
- `frontend/app/(private)/documents/upload/page.tsx`
- `frontend/app/(private)/documents/[id]/page.tsx`
- `frontend/tests/documents-api.test.ts`, `documents-ui.test.tsx`, `app-shell.test.tsx`

Изменены Frontend-файлы: `frontend/package.json`, `frontend/tsconfig.json`,
`frontend/app/layout.tsx`, `frontend/app/(private)/layout.tsx`,
`frontend/app/(private)/documents/page.tsx`, `frontend/app/auth-form.tsx`,
`frontend/app/(private)/logout/page.tsx` (для auth-форм только оформление, логика сохранена).

Инфраструктура: новые `infrastructure/compose.documents-test.yaml`; изменения
`infrastructure/compose.yaml`, `infrastructure/compose.dev.yaml`, `.env.example`,
`.github/workflows/ci.yml`. Новых production-сервисов нет.

Документация: `docs/STAGE3.md`, `docs/ARCHITECTURE.md`, `docs/CODEX_TASKS.md`,
этот список. Реальный `.env`, Backend auth-код и миграции `0001_auth`, `0002_documents`
в подэтапах 3.2–3.3 не изменялись. Bootstrap MinIO не выполнялся.
