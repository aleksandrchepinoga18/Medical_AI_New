# Medical AI New

Medical AI New — самостоятельная веб-платформа для работы с медицинскими документами, поиска по личным данным и проверенным источникам, а также общения с LLM. Проект в активной разработке. Текущий контур предназначен для разработки и проверки на синтетических данных. Промышленная готовность, сертификация и медицинская валидация не подтверждены.

## Возможности

- Регистрация, вход, защищённые сессии и личный кабинет.
- Загрузка PDF/DOCX, хранение оригиналов, фоновое извлечение текста, версионирование и личный поиск.
- Чат с историей, потоковыми ответами и проверяемыми ссылками. Локальная генерация через Ollama/Qwen. GigaChat доступен при отдельной настройке и явном согласии.
- Отдельная медицинская база знаний с проверкой и публикацией версий. Контролируемый поиск PubMed.
- Извлечение лабораторных показателей из PDF, отображение строк и источников, сравнение и динамика в чате с расчётами на Python.
- Синтетические бенчмарки RAG, чата и извлечения. MLflow Tracking и Model Registry для экспериментов.

Ограничения текущего объёма включают отсутствие локального OCR, поддержки TXT/JPG/PNG, сквозного удаления документов, ручного подтверждения показателей, отдельной аналитики с графиками и пользовательского экспорта. OpenAI, DeepSeek и YandexGPT остаются в плане. Состояние проверок, результаты Evaluation и критерии промышленного допуска приведены в [аудите проекта](docs/PROJECT_AUDIT.md).

## Архитектура и стек

Medical AI New использует модульный монолит FastAPI. Next.js обращается к API через same-origin proxy. PostgreSQL хранит пользователей, диалоги, метаданные, версии извлечения и состояния задач. MinIO хранит оригиналы, Qdrant — индексы. Redis и Celery обслуживают фоновые задания. MLflow использует отдельную PostgreSQL и отдельный закрытый bucket.

| Каталог | Содержимое |
|---|---|
| `backend/` | Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic, Celery, тесты и Evaluation |
| `frontend/` | Next.js App Router, TypeScript, Tailwind CSS и Vitest |
| `infrastructure/` | Compose, Dockerfile, конфигурации хранилищ и MLflow |
| `docs/` | ТЗ, архитектура, эксплуатация, аудит и порядок работы с GitHub |
| `.github/` | CI и сканирование локальных файлов и Git-истории |

[ТЗ](docs/TZ.md) · [Архитектура](docs/ARCHITECTURE.md) · [Эксплуатация](docs/OPERATIONS.md)

## Локальный запуск

Команды выполняет пользователь из корня проекта в Git Bash для Windows. Нужны Docker Desktop с Linux containers и Docker Compose v2. Если `.env` отсутствует, создайте его из шаблона и задайте независимые случайные значения вместо `CHANGE_ME`. Храните файл локально и передавайте его через `--env-file`. Порты приложений по умолчанию привязаны к loopback.

```bash
cp -n .env.example .env
# Перед следующими командами заполните локальный .env в редакторе.
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }

dc config --quiet
dc build
dc up -d --wait postgres
# Только для локальной БД разработки.
dc run --rm --no-deps backend python -m alembic upgrade head
dc up -d --wait --wait-timeout 240
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
```

Откройте [приложение](http://localhost:3000) и [локальный MLflow](http://localhost:5000). Чат и RAG требуют локальных моделей и настроек из [руководства по чату](docs/STAGE5.md) и [Hybrid RAG](docs/HYBRID_RAG.md). Для остановки с сохранением томов используйте `dc down`. MinIO Server и Client собираются из release-артефактов с закреплёнными версиями и SHA‑256. [Операции с MinIO](docs/OPERATIONS.md#исправление-установки-minio-client)

## Проверки

```bash
dc exec backend python -m ruff check .
dc exec backend python -m mypy app
dc exec backend python -m pytest -m 'not integration'
dc exec frontend npm run lint
dc exec frontend npm run typecheck
dc exec frontend npm test
```

GitHub Actions выполняет эти проверки, сканирование истории и зависимостей, сборку runtime-образов и тесты миграций на временной PostgreSQL. Для интеграции документов CI использует отдельный синтетический Compose. Сценарии RAG, облачные бенчмарки и MLflow проверяются отдельно. [Первый push и CI](docs/GITHUB_SETUP.md) · [Evaluation 6.6](docs/STAGE6_6.md)

## Безопасность и работа с репозиторием

Рабочие ключи, `.env`, медицинские документы, выгрузки, локальные отчёты и логи остаются вне Git. В репозитории хранятся синтетические фикстуры, методика Evaluation, проверенные агрегаты и техническая документация. Перед push выполните локальную проверку истории по [инструкции GitHub](docs/GITHUB_SETUP.md).

API проверяет владельца документов и историю источников. Внешние вызовы требуют явного согласия. Для доступа за пределами локального стенда нужно отдельно подтвердить TLS, разграничение доступа, резервное восстановление, удаление данных и требования к обработке медицинской информации.

Правила для Codex находятся в [AGENTS.md](AGENTS.md). История решений и план этапов сохранены в [CODEX_TASKS.md](docs/CODEX_TASKS.md).
