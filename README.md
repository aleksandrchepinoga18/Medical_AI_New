# Medical AI

Новый проект по [утверждённому ТЗ](docs/TZ.md). Этап 1: каркас FastAPI и Next.js,
PostgreSQL, MinIO, Redis/Celery, Qdrant, MLflow Tracking и Model Registry.
Бизнес-модули, обработка документов, LLM-вызовы и логирование экспериментов
на этом этапе не реализованы.

MinIO Server и Client устанавливаются из официальных GitHub release assets
с фиксированными версиями и проверкой SHA-256. `minio-init` собирается локально;
образы `minio/mc` и `minio/minio` не требуются. Инструкция пересборки без удаления
данных: [исправление установки MinIO Client](docs/OPERATIONS.md#исправление-установки-minio-client).

**Codex редактирует файлы. Все команды ниже выполняет пользователь в Git Bash
для Windows. Codex команды исполнения не запускает.**

На 14 ноября 2026 года пользователь подтвердил завершение этапа 1 и работающую
инфраструктуру; исходный код зафиксирован коммитом `c22fae1`.
План этапов 2–10 и вопросы сверки: [CODEX_TASKS.md](docs/CODEX_TASKS.md).
Этап 2 не начат; для начала требуется явное подтверждение пользователя.

## Файлы

```text
backend/                  FastAPI, настройки, Celery, unit tests
frontend/                 Next.js App Router, TypeScript, health endpoint, unit test
infrastructure/
  compose.yaml            общая инфраструктура и именованные тома
  compose.dev.yaml        reload и bind mounts для разработки
  compose.production.yaml основа отдельной production-конфигурации
  docker/                 Dockerfiles
  minio/                  idempotent bucket/account bootstrap
  mlflow/                 Tracking server, зависимости, ручная smoke-проверка
.github/workflows/ci.yml   lint, typecheck, unit tests, Docker build, Compose config
docs/                     ТЗ, архитектура, задачи, эксплуатация
AGENTS.md                 обязательные ограничения Codex
.env.example              имена переменных и примеры без рабочих секретов
```

## Ручная проверка

Требуются Docker Desktop с Linux containers и Compose v2, Git Bash, Git.
Для проверок вне Docker: Python 3.12 и Node.js 22. Команды предполагают
стандартные порты из `.env.example`.

```bash
cd /c/workplace/Medical_AI_New
cp -n .env.example .env
```

Откройте `.env` в редакторе и замените все `CHANGE_ME` независимыми случайными
значениями. Не загружайте `.env` командой `source`: это данные для Compose,
а не shell-скрипт. Не коммитьте `.env`. При использовании `$` в значениях
учитывайте интерполяцию Compose; для локальных секретов удобны случайные
буквы и цифры. Порты UI привязаны к `127.0.0.1`.

```bash
# Функция действует только в текущем окне Git Bash.
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }

# Только валидация; --quiet не печатает разрешённые секреты.
dc config --quiet
docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml config --quiet

# Далее — установка/сборка и запуск, только вручную.
dc build
dc up -d --wait --wait-timeout 240
dc ps -a
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
curl --fail http://localhost:5000/health

# Проверки внутри dev-контейнеров, без локальной установки Python/Node.
dc exec backend python -m ruff check .
dc exec backend python -m ruff format --check .
dc exec backend python -m mypy app
dc exec backend python -m pytest
dc exec frontend npm run lint
dc exec frontend npm run typecheck
dc exec frontend npm test

# Отдельная ручная интеграционная проверка MLflow; создаёт синтетические записи.
dc exec -e MLFLOW_TRACKING_URI=http://127.0.0.1:5000 mlflow python smoke.py
```

Ожидается: все проверки завершаются с кодом 0, долгоживущие сервисы имеют
статус `healthy`; `minio-init` завершён с кодом 0. Backend/frontend возвращают
`{"status":"ok"}`, MLflow `/health` — HTTP 200. Smoke-проверка выводит `PASS`,
имя эксперимента, run ID и номер синтетической версии в Registry. Она проверяет
сохранение метрик/параметров и двустороннюю передачу артефакта через proxy.
Проверочный артефакт — текст, он не является моделью для инференса.

UI: [приложение](http://localhost:3000), [MLflow](http://localhost:5000),
[MinIO Console](http://localhost:9001). В MinIO входят root-данными из `.env`;
MLflow работает отдельной учётной записью и имеет доступ только к своему bucket.
В MLflow найдите `infrastructure-smoke-*`, сохраните run ID и имя Registry,
откройте артефакт `probe.txt`.

```bash
# Пересоздание контейнеров без удаления томов.
dc up -d --force-recreate --wait --wait-timeout 240
dc ps -a
```

После пересоздания найдите прежний эксперимент/run и Registry version в UI,
снова скачайте прежний `probe.txt`. Новый запуск smoke создаст другие записи,
поэтому сам по себе не проверяет сохранение прежних данных.

```bash
# Остановка без удаления данных.
dc down
```

Не добавляйте `-v` к остановке. Полные инструкции, локальные проверки без Docker,
подготовка lock-файлов и ограничения production: [OPERATIONS.md](docs/OPERATIONS.md).

## Инициализация нового Git-репозитория (историческая инструкция)

Для текущего проекта инициализация уже выполнена, повторять её не требуется.

Git-репозиторий и remote агентом не создавались. После ручного просмотра файлов:

```bash
git init -b main
git status --short
git add .
git diff --cached --stat
# Проверьте, что .env и медицинские данные не попали в индекс.
git commit -m "Initialize Medical AI stage 1 infrastructure"
```

Создание remote и push выполняет пользователь отдельно. Workflow запускается
GitHub после push/PR либо вручную; автоматической публикации образов и CD нет.

## Статус

Этап 1 завершён по сообщению пользователя. В текущей сверке Codex выполнял
только чтение и статический просмотр, без сборок, миграций и тестов. Прямые зависимости
и теги образов зафиксированы; транзитивные lock-файлы и image digests ещё
не подготовлены. Совместимость и безопасность зависимостей нужно подтвердить
до production. Следующий этап — после проверки и явного подтверждения пользователем.
