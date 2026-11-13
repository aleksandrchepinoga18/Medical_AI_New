# Задачи Codex

Работа ведётся по [ТЗ](TZ.md) и [AGENTS.md](../AGENTS.md).
Следующий этап не начинается до ручной приёмки предыдущего пользователем.

## Этап 1 — подготовлены файлы, приёмка ожидается

- [x] Сохранить исходное ТЗ, добавить требование MLflow и правила Codex.
- [x] Создать AGENTS, README, env example, gitignore, архитектуру и эксплуатацию.
- [x] Создать минимальный FastAPI и Next.js без фиктивных бизнес-модулей.
- [x] Настроить PostgreSQL, MinIO, Redis/Celery и Qdrant в Compose.
- [x] Настроить MLflow Tracking UI, отдельный PostgreSQL backend и Model Registry.
- [x] Настроить MinIO artifact proxy, закрытый bucket и отдельного scoped user.
- [x] Добавить persistent volumes, сегментацию сетей, healthchecks и dependencies.
- [x] Разделить базовую конфигурацию, dev override и основу production override.
- [x] Подготовить CI: Ruff, mypy, ESLint, TypeScript, Pytest, Vitest, Docker builds.
- [x] Подготовить ручные Git Bash команды и синтетическую MLflow smoke-проверку.
- [ ] Пользователь создаёт Git-репозиторий и при необходимости GitHub remote.
- [ ] Пользователь разрешает зависимости, создаёт lock-файлы и проверяет версии.
- [ ] Пользователь запускает проверки, сборки, healthchecks и MLflow smoke.
- [ ] Пользователь проверяет сохранение старых данных после пересоздания контейнеров.
- [ ] Пользователь просматривает результаты GitHub Actions и принимает этап 1.

Галочки подготовки означают наличие файлов, а не успешный запуск.

## Исправление сборки MinIO — подготовлено, ручная проверка ожидается

- [x] Убрать `minio/mc` из Compose и Dockerfile; использовать локальный client target.
- [x] Установить mc и MinIO Server из официальных release assets с SHA-256,
  сохранив версии, данные, тома, сети и MLflow-конфигурацию.
- [x] Добавить сборку client target в CI и Git Bash команды приёмки.
- [ ] Пользователь собирает оба targets, проверяет версии, bootstrap/healthchecks.
- [ ] Пользователь проверяет MLflow smoke и сохранность прежних артефактов/метаданных.

## Дальнейшие работы из ТЗ — вне текущего этапа

Порядок и детализация согласуются после приёмки этапа 1:

1. SQLAlchemy/Alembic, реальные схемы, auth, пользовательский ACL и аудит.
2. Документы, MinIO, извлечение/OCR, фоновые идемпотентные задачи, удаление.
3. LLM Gateway, согласия на внешние вызовы, embedding и RAG с проверкой владельца.
4. Чат, лабораторные показатели, воспроизводимая аналитика и источники.
5. MLflow SDK: эксперименты LLM/embedding/RAG/prompts/evaluation, Registry lifecycle;
   Tracing только после redaction/ACL/retention. Сейчас бизнес-логирование отсутствует.
6. Расширение CI: миграции, integration/E2E/security/evaluation, dependency/secret
   scanning, image digests, GHCR и защищённый CD staging/production.
7. Backup/restore с проверкой восстановления, monitoring и нагрузочная приёмка.

LangGraph и Airflow не устанавливаются на ранних этапах. Новые модули создаются
вместе с реальными функциями и тестами, а не как пустые заглушки.
