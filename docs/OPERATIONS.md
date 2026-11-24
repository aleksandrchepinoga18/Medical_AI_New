# Локальная эксплуатация Medical AI New

Актуальный локальный запуск описан в [README](../README.md). Для исторических
проверок авторизации, отдельной тестовой БД и ручных миграций используйте
[инструкцию этапа 2](STAGE2.md).

Все команды выполняет пользователь в **Git Bash для Windows**.
Codex их не запускал. Для разработки и проверки используйте только синтетические данные.
Условия промышленного допуска и состояние проверок приведены в [аудите](PROJECT_AUDIT.md).

Статус на 14 ноября 2026 года: этап 1 завершён и инфраструктура работает по сообщению
пользователя, исходный коммит — `c22fae1`. Инструкции ниже сохранены для эксплуатации
и повторной ручной проверки; они не означают необходимость заново инициализировать
проект или менять существующий `.env`. Codex повторные проверки не запускал.
Следующий этап начинается только после явного подтверждения пользователя.

## Предварительные условия

Docker Desktop должен использовать Linux containers и Compose v2 с поддержкой
`--wait`. Для Git Bash убедитесь, что файлы `.sh` имеют LF, как задано
`.gitattributes`. Не переносите shell-скрипты с CRLF в контейнер.
Зависимости и Docker images скачиваются только при ручном выполнении команд.
Не меняйте `COMPOSE_PROJECT_NAME` между пересозданиями, иначе будут выбраны другие тома.

```bash
cd /c/workplace/Medical_AI_New
docker compose version
cp -n .env.example .env
```

Замените все `CHANGE_ME` в `.env` редактором. Не публикуйте вывод
`docker compose config` без `--quiet`: он содержит секреты. Используйте отдельные
секреты для основной PostgreSQL, MLflow PostgreSQL, Redis, root MinIO и MLflow S3.
Настройки `POSTGRES_*` применяются к пустому тому при первом запуске; изменение
пароля в `.env` не меняет уже существующую роль PostgreSQL. Ротация выполняется
отдельной управляемой процедурой, без удаления тома.

## Варианты Compose

```bash
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }
pc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml "$@"; }
dc config --quiet
pc config --quiet
```

Базовый файл собирает runtime images. Dev override включает reload и mounts.
Production override — отдельная основа без dev mounts, с runtime targets
и ротацией логов. Он сохраняет доступ только с localhost. Для разных сред
нужны разные env-файлы, project names и резервные копии.

## Восстановление запуска frontend

Dev bind mount `../frontend:/app` скрывает содержимое `/app` из образа.
В исходниках нет `server.js`: его создаёт `next build` в `.next/standalone`.
Поэтому runtime-команда `node server.js` вместе с dev mounts приводит к
`Cannot find module '/app/server.js'`. Ранее оба targets использовали один
автоматический тег образа, а dev override наследовал CMD образа. Это позволяло
запустить runtime-образ с dev mounts после переключения конфигурации без сборки.

Теперь frontend использует отдельные теги `:development` и `:runtime` и явные
команды для каждого override. Production override сбрасывает mounts через
`!reset []`; нужна версия Docker Compose с поддержкой этого тега (проверьте
`pc config --quiet`). Применяйте production override после базового файла,
не подключая dev override. Не меняйте project name существующей установки.

`next.config.ts` сохраняет `output: "standalone"`. Builder проверяет наличие
`.next/standalone/server.js`, runtime копирует standalone, static и public.
Если public пока отсутствует, builder создаёт пустую директорию. WORKDIR —
`/app`, CMD — `node server.js`, стандартный entrypoint Node не переопределяется.
Healthcheck обоих режимов обращается к существующему `/health/live` на порту 3000.

Команды ниже выполняются вручную из корня проекта; зависимости frontend
должны уже работать. Они пересоздают только frontend, без удаления томов.

```bash
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }
pc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml "$@"; }

# Текущий контейнер: команда и mounts, без вывода environment с секретами.
docker inspect medical-ai-frontend-1 --format '{{json .Config.Cmd}} {{json .Mounts}}'
dc config --quiet
pc config --quiet

# Production: проверка файлов непосредственно в образе, без Compose mounts.
pc build frontend
MSYS_NO_PATHCONV=1 docker run --rm --entrypoint sh medical-ai-frontend:runtime -ec 'test -f /app/server.js && test -d /app/.next/static && test -d /app/public'
pc up -d --no-deps --force-recreate --wait --wait-timeout 180 frontend
pc ps frontend
pc logs --tail 80 frontend
pc exec -T frontend node -e "fetch('http://127.0.0.1:3000/health/live').then(async r=>{console.log(r.status,await r.text());if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
curl --fail http://localhost:3000/health/live

# Development: переключение обратно с пересборкой соответствующего target.
dc build frontend
dc up -d --no-deps --force-recreate --wait --wait-timeout 180 frontend
dc ps frontend
dc logs --tail 80 frontend
dc exec -T frontend node -e "console.log(process.env.NODE_ENV);require('node:fs').accessSync('node_modules/next/package.json')"
curl --fail http://localhost:3000/health/live
```

В команде `docker run` замените префикс `medical-ai`, если в `.env` задан другой
`COMPOSE_PROJECT_NAME`. Для curl используйте фактический `FRONTEND_PORT`, если
он отличается от 3000. Каждый режим должен стать `healthy`, вернуть HTTP 200
и `{"status":"ok"}`; development должен вывести `development`. В production
у frontend не должно быть mounts на `/app`, `/app/node_modules` или `/app/.next`.
Проверка двух режимов последовательная и оставляет frontend в development;
для возврата в production повторите его команду `pc up` выше.

### Исправление `next: not found` в development

Установка npm выполняется при сборке в `/app/node_modules`. Ранее named volume
`frontend-node-modules` скрывал этот каталог. Уже существующий том не обновляется
после пересборки образа, поэтому контейнер мог не видеть `node_modules/.bin/next`.
`next` указан в dependencies, а `npm run dev` добавляет локальный `.bin` в PATH;
глобальная установка Next.js и ручная подмена PATH не нужны.

Теперь development монтирует отдельно `app`, `tests` и существующие файлы
конфигурации. `/app/node_modules` и `/app/.next` остаются в записываемом слое
контейнера, подготовленном для пользователя node. Изменения в `app` видны сразу
и обрабатываются Next.js hot reload. Оба прежних dev-тома сохранены в декларации,
но больше не подключаются. Другие сервисы и их volumes не меняются.

Сборка явно включает devDependencies и проверяет исполняемый `.bin/next`.
После изменения `package.json` или lockfile нужна пересборка и пересоздание
frontend: manifest и зависимости используются из одного образа. Новые каталоги
верхнего уровня (например, `public` или `components`) нужно добавить в dev mounts,
если для них требуется hot reload. Кеш `.next` пересоздаётся вместе с контейнером.
Production продолжает запускать standalone через `node server.js`.

```bash
dc config --quiet
dc build frontend
dc up -d --no-deps --force-recreate --wait --wait-timeout 180 frontend
docker inspect medical-ai-frontend-1 --format '{{json .Mounts}}'
dc exec -T frontend sh -ec 'test -x node_modules/.bin/next; node_modules/.bin/next --version; npm ls next --depth=0'
dc ps frontend
dc logs --tail 80 frontend
curl --fail http://localhost:3000/health/live
```

Ожидается отсутствие mounts на `/app`, `/app/node_modules` и `/app/.next`,
версия Next.js из package.json, `healthy` и HTTP 200. Измените текст в
`frontend/app/page.tsx` и убедитесь, что открытая страница обновляется без
пересборки образа; затем верните текст. Сборка, запуск и тесты агентом не выполнялись.

## Исправление установки MinIO Client

Зависимость от Docker Hub образа `minio/mc:RELEASE.2025-04-16T18-13-26Z`
убрана из Dockerfile и `minio-init`. MinIO Server также устанавливается без
Docker-репозитория `minio/minio`, чтобы сборка не упиралась в его доступность.
Версии сохранены: `mc` — `RELEASE.2025-04-16T18-13-26Z`, server —
`RELEASE.2025-04-22T22-12-26Z`. Используются точные release assets официальных
репозиториев GitHub. SHA-256 взяты из метаданных **самих бинарных файлов**:

- [mc: список release assets и SHA-256](https://github.com/minio/mc/releases/expanded_assets/RELEASE.2025-04-16T18-13-26Z).
- [MinIO Server: список release assets и SHA-256](https://github.com/minio/minio/releases/expanded_assets/RELEASE.2025-04-22T22-12-26Z).
- [Существующий базовый образ Python в Docker Hub](https://hub.docker.com/layers/library/python/3.12.12-slim-bookworm/images/sha256-8b2968baaded00c58e3471b1bcf46d425eba26d7ab09c11d7bca829bcf7675c0).

`infrastructure/minio/releases.json` фиксирует версии и hashes, `download.py`
скачивает по HTTPS и проверяет SHA-256 до установки. Dockerfile содержит targets
`client` и `server`; оба собираются CI. Linux amd64 и arm64 выбираются через
BuildKit `TARGETARCH`. Для других архитектур сборка завершится явной ошибкой.
Новых apt/pip/Go-зависимостей нет. Доступ к GitHub и release-assets.githubusercontent.com
нужен во время сборки; при запуске загрузок нет. При недоступности источника или
ошибке hash сборка останавливается, без перехода на latest/зеркала.

Метаданные релизов и hashes проверены чтением официальных источников.
Скачивание бинарных файлов, их выполнение, сборка и совместимость в работающем
контейнере агентом не проверялись: это выполняет пользователь командами ниже.

Не перезаписывайте существующий `.env` и не меняйте `COMPOSE_PROJECT_NAME`.
Имена томов, `/data`, сети, env, bucket, MinIO user/policy и настройки
MLflow Tracking/Registry сохранены. Смена способа упаковки не обновляет версию
MinIO Server и не требует миграций данных.

```bash
cd /c/workplace/Medical_AI_New
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }

docker compose --env-file .env -f infrastructure/compose.yaml config --quiet
dc config --quiet
docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.production.yaml config --quiet

# Сборка двух targets из официальных release assets; данные не подключаются.
dc build --no-cache minio minio-init

# Версия клиента без подключения к MinIO и без запуска bootstrap.
dc run --rm --no-deps --entrypoint mc minio-init --version

# Запуск/обновление с существующими томами и зависимостями.
dc up -d --wait --wait-timeout 240
dc exec minio minio --version
dc exec minio mc --version
dc ps -a
dc logs --tail 100 minio-init

# Повторный bootstrap проверяет повторяемость инициализации.
dc run --rm minio-init

curl --fail http://localhost:5000/health
# Создаёт только новые синтетические записи, не заменяет проверку старых данных.
dc exec -e MLFLOW_TRACKING_URI=http://127.0.0.1:5000 mlflow python smoke.py
```

Ожидается `Verified ... sha256:...` при сборке, соответствующие версии `mc`
и MinIO Server, `healthy` у MinIO/MLflow, exit code 0 у `minio-init` и его
повторного запуска, HTTP 200 у MLflow и `PASS` у smoke. При необходимости
сборки остальных ещё не созданных образов выполните `dc build` перед `dc up`.
В MLflow UI дополнительно откройте **прежний** run/Registry version и скачайте
**прежний** артефакт. В MinIO Console проверьте прежние объекты. Это проверяет
сохранение данных; новый smoke-run сам по себе этого не доказывает.
Не выполняйте `down -v`, `volume rm`, `prune` и удаление данных.

## Локальные проверки приложений без Docker

Альтернатива `dc exec` для lint/typecheck/unit tests из README. Нужны
установленные пользователем Python 3.12 и Node.js 22. `python` должен указывать
именно на Python 3.12; при необходимости используйте `py -3.12` для venv.

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install './backend[dev]'
(
  cd backend
  python -m ruff check .
  python -m ruff format --check .
  python -m mypy app
  python -m pytest
)
(
  cd frontend
  npm install
  npm run lint
  npm run typecheck
  npm test
  npm run build
)
```

`npm install` создаёт настоящий `frontend/package-lock.json`. Сохраните его
в Git после проверки; CI и Dockerfile автоматически переходят на `npm ci`
при наличии lockfile. Проверка зависимостей, например `npm audit`, требует
отдельного просмотра результата пользователем; отсутствие найденных проблем
не заменяет security-приёмку.

Python direct dependencies закреплены в `backend/pyproject.toml` и
`infrastructure/mlflow/requirements.txt`. Полные транзитивные lock-файлы пока
не сгенерированы: это требует разрешения зависимостей, которое агенту запрещено.
Перед дальнейшей приёмкой пользователь должен разрешить и проверить полный
набор Python-зависимостей, сохранить lock с hashes и перевести CI/Docker install
на него. Например, после установки выбранной фиксированной версии `pip-tools`:

```bash
python -m piptools compile --extra dev --generate-hashes --output-file backend/requirements-dev.lock backend/pyproject.toml
python -m piptools compile --generate-hashes --output-file backend/requirements.lock backend/pyproject.toml
python -m piptools compile --generate-hashes --output-file infrastructure/mlflow/requirements.lock infrastructure/mlflow/requirements.txt
```

Эти команды выполняйте с Python 3.12. Сгенерированные lock-файлы нужно
просмотреть; Dockerfiles и workflow уже умеют использовать их при наличии.
Доступность всех image tags, совместимость пакетов и актуальность исправлений
безопасности не подтверждены выполнением. До production нужны аудит зависимостей,
актуальные security patches и закрепление образов по digest; `latest` не используется.

## MLflow: приёмка Tracking, S3 и Registry

```bash
dc build
dc up -d --wait --wait-timeout 240
dc ps -a
dc exec -e MLFLOW_TRACKING_URI=http://127.0.0.1:5000 mlflow python smoke.py
```

Ожидается `PASS`, имя `infrastructure-smoke-*`, run ID, версия Registry.
Smoke создаёт только синтетические записи и текстовый файл. Он не обучает
модель, не включает autologging и не является бизнес-интеграцией MLflow.
Версия в Registry ссылается на проверочный текст, она не предназначена для serving.

В [MLflow UI](http://localhost:5000) убедитесь, что видны параметры/метрики,
открывается `probe.txt` и есть запись Registry с run ID. В
[MinIO Console](http://localhost:9001) проверьте bucket `mlflow-artifacts`
(или заданный в `.env`) и объекты под `artifacts/`.
Не создавайте публичную bucket policy. В MLflow используется обычный отдельный
MinIO user с ограниченной policy, а не root или MinIO service-account API.

Проверка, что метаданные находятся именно в PostgreSQL:

```bash
dc exec mlflow-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT name, experiment_id FROM experiments ORDER BY experiment_id DESC LIMIT 5;"'
```

Проверка сохранения данных:

```bash
dc up -d --force-recreate --wait --wait-timeout 240
dc ps -a
```

Проверьте **тот же прежний** run/версию Registry и скачайте **тот же** артефакт
в UI. Повторно создаваемый эксперимент не доказывает сохранность старого.
Можно также выполнить `dc down`, затем `dc up -d --wait --wait-timeout 240`
и повторить просмотр. Томов эти команды не удаляют. Persistent volume не является
резервной копией и не защищает от сбоя диска.

## Диагностика и остановка

```bash
dc ps -a
dc logs --tail 100 minio-init
dc logs --tail 100 mlflow mlflow-db
dc logs --tail 100 backend worker frontend
dc down
```

Просматривайте логи локально; перед передачей удалите секреты и чувствительные
данные. Не включайте shell `set -x` вокруг команд с секретами. MLflow backend URI
передаётся процессу через окружение, не через аргументы командной строки.
Публикуемые UI не имеют общей authentication boundary; localhost — предел этапа 1.

## Обновление и резервное копирование

До изменения версии MLflow нужно остановить записи экспериментов, сохранить
backup `mlflow-db` и его MinIO bucket, проверить восстановление отдельно,
прочитать release/migration notes. В существующей БД применить `mlflow db upgrade`
вручную с тем же backend URI из защищённого окружения, затем обновлять сервис.
Не печатайте URI с паролем в истории команд и логах. `server.py` не запускает
команду upgrade автоматически. MLflow может инициализировать/обновлять свои
служебные таблицы при старте, поэтому первый запуск новой версии также требует backup.
Откат Docker-образа не откатывает SQL-схему.

Для PostgreSQL приложения, MLflow PostgreSQL, MinIO, Qdrant и Redis нужны
отдельные политики backup/restore, retention и проверки восстановления.
Автоматизация backup/restore не входит в текущую реализацию. Удаление томов
не является способом обновления или ротации секретов.

## Перед production и следующим этапом

- Принять результаты ручных проверок и CI; сохранить lock-файлы и digests.
- Настроить TLS, аутентификацию/ACL Tracking UI/API, ограничить административный доступ.
- Заменить env-файлы защищённой доставкой секретов; выделить PostgreSQL-роли
  с минимальными правами вместо bootstrap superusers, задать ресурсные лимиты.
- Реализовать авторизацию продукта, изоляцию пользователей, аудит и удаление.
- Проверить зависимости/секреты, backup/restore и процедуры обновлений.
- До Tracing и experiment logging определить redaction, согласия, retention,
  разрешённые поля и правила обработки медицинских данных.

Расширенный CI из ТЗ (миграции, integration/E2E, security/dependency scanning,
GHCR, staging/production CD) — дальнейшие задачи. Сейчас конфигурация CI
выполняет только lint, format/typecheck, unit tests, Compose validation и build
без push образов. Агент ни CI, ни локальные проверки не запускал.
