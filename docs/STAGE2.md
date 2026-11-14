# Этап 2: авторизация и ручная приёмка

Код подготовлен; команды, тесты, сборки и миграции Codex не запускал.
Этап не принят до проверки и подтверждения пользователя. Этап 3 не начат.
Работайте только с синтетическими учётными записями, без медицинских данных.

## Реализовано

- `GET /auth/csrf`, `POST /auth/register`, `POST /auth/login`,
  `POST /auth/logout`, `GET /auth/me`. В браузере префикс `/api`.
- Пароли 12–128 символов, Argon2id (64 MiB, 3 итерации, parallelism 2),
  обновление хеша при входе при изменении параметров. Email нормализуется в lowercase.
- Случайные 256-bit токены; SHA-256 в БД, HttpOnly host-only session cookie,
  SameSite=Lax, Secure по конфигурации, абсолютный TTL 8 часов.
- CSRF для всех изменяющих auth-запросов, включая вход и регистрацию:
  точный Origin, double-submit cookie/header; logout дополнительно проверяет
  привязку токена к сессии. Logout выполняется кнопкой POST, не переходом GET.
- Лимит всех попыток входа/регистрации: 10 на email и 50 на адрес клиента за
  фиксированное окно 15 минут, PostgreSQL row locks для нескольких процессов.
  HTTP 429 и Retry-After; неверные пароль/аккаунт дают одинаковый HTTP 401.
- Ошибки валидации не возвращают введённые пароли. Профиль не возвращает хеши.
- Next.js: `/login`, `/register`, приватные `/chat`, `/documents`, `/analyses`,
  `/settings`, `/profile` (переход к настройкам), `/logout`.
  Будущие функции показывают только сообщение о недоступности.

## Переменные окружения

Существующий `.env` агентом не изменён. Для стандартного Docker dev новые
переменные имеют defaults, совпадающие с `.env.example`; менять секреты не нужно.
При другом порте/домене вручную укажите `AUTH_ORIGIN` как адрес браузера без
завершающего `/`, например `http://localhost:3001`. Не смешивайте localhost и
127.0.0.1. Для HTTPS: `AUTH_ORIGIN=https://ваш-домен`, `AUTH_COOKIE_SECURE=true`.
Production требует оба условия; локальный HTTP проверяйте через dev override.
`AUTH_SESSION_SECONDS` — 60–604800, по умолчанию 28800.
`POSTGRES_*` передаются существующим backend/worker; пароль тот же, что у
существующей роли приложения. MLflow credentials не используются.

## Сборка и миграция (Git Bash из корня проекта)

Перед миграцией сделайте резервную копию и проверьте возможность восстановления
в отдельной среде. При конфликте существующих таблиц с именами `users`,
`auth_sessions`, `auth_login_limits` остановитесь: не применяйте stamp или удаление.
Миграция добавляет таблицы и `alembic_version`, не пересоздаёт БД и не удаляет данные.
Откат downgrade намеренно запрещён; изменения схемы исправляются согласованной
forward migration. Откат образа не откатывает схему.

```bash
cd /c/workplace/Medical_AI_New
dc() { docker compose --env-file .env -f infrastructure/compose.yaml -f infrastructure/compose.dev.yaml "$@"; }
dc config --quiet
mkdir -p backups
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backups/before-stage2.dump
# Убедитесь, что pg_dump завершился успешно, а файл не пустой.
test -s backups/before-stage2.dump

dc build backend worker frontend
dc run --rm --no-deps backend alembic upgrade head
dc run --rm --no-deps backend alembic current
dc run --rm --no-deps backend alembic check
dc up -d --no-deps --wait --wait-timeout 240 backend worker frontend
dc ps -a
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:3000/health/live
curl --fail http://localhost:5000/health
```

Ожидается успешная сборка, ревизия `0001_auth`, отсутствие новых операций
в `alembic check`, healthy у сервисов, HTTP 200 у health endpoints. Liveness
не проверяет наличие миграций. Проверяйте код возврата каждой команды; при ошибке
не продолжайте. Существующие хранилища должны уже работать после этапа 1.

## Unit, integration, lint и типы

```bash
dc exec -T backend python -m ruff check .
dc exec -T backend python -m ruff format --check .
dc exec -T backend python -m mypy app
dc exec -T backend python -m pytest -m 'not integration'
dc exec -T frontend npm run lint
dc exec -T frontend npm run typecheck
dc exec -T frontend npm test

# Один раз создайте отдельную БД только для синтетических тестов.
# Если она уже существует, повторять CREATE DATABASE не нужно.
dc exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE medical_ai_stage2_test"'
dc run --rm --no-deps -e POSTGRES_DB=medical_ai_stage2_test -e RUN_AUTH_INTEGRATION=1 backend python -m pytest -m integration
```

Тесты не принимают имя БД без суффикса `_test`, не выполняют drop/truncate,
оставляют синтетические записи. Не указывайте рабочую БД или БД MLflow.
Проверяются additive migration, повторный upgrade, schema drift, сохранение
посторонней таблицы, регистрация/вход/выход, истечение и ротация сессий, CSRF,
лимиты, 401/404 и изоляция двух пользователей через тестовый защищённый маршрут.
Ожидаются exit code 0, integration tests без skips при установленном флаге.
CI использует отдельный ephemeral PostgreSQL service, не рабочий Compose.

Если lock-файлы добавлялись локально после исходного этапа, их нужно обновить
с новыми Python dependencies перед сборкой: SQLAlchemy, Alembic, psycopg,
argon2-cffi, email-validator. Команды разрешения зависимостей — в OPERATIONS.md;
агент их не выполняет.

## Ручная проверка браузером

1. Откройте `http://localhost:3000/settings` без входа: переход на `/login`.
2. Зарегистрируйте синтетический аккаунт. Проверьте email и ID в профиле,
   навигацию «Чат», «Документы», «Анализы», «Настройки».
3. В DevTools проверьте cookie: `medical_session` имеет HttpOnly и SameSite=Lax;
   на HTTPS также Secure. Токен не должен появляться в localStorage.
4. Выйдите кнопкой; обновите приватную страницу — требуется вход.
5. Войдите снова; неправильный пароль даёт общее сообщение. 11-я попытка для
   одного email в пределах 15 минут даёт 429 (регистрация также считается).
6. В другом профиле браузера создайте второго пользователя: `/api/auth/me`
   возвращает только его ID/email. Без cookie endpoint возвращает 401.
7. Запрос POST logout без CSRF либо с чужим Origin должен возвращать 403.
8. Проверьте, что прежние runs и артефакты MLflow остались доступны.

## Ограничения и риски

- Сборка, lint/typecheck, тесты и миграции не проверены запуском агентом.
  Браузерные сценарии проверяются вручную; автоматические E2E здесь не добавлены.
- Нужны TLS и доверенный reverse proxy до внешнего доступа. Нельзя включать
  доверие произвольным X-Forwarded-For. Через Next.js адрес клиента у backend
  может быть общим для пользователей: IP-лимит тогда общий, email-лимит независим.
- Нет подтверждения email, восстановления пароля, MFA, административных ролей
  и отзыва всех устройств. Они не входят в текущее задание.
- Просроченные сессии и старые счётчики не удаляются автоматически; до production
  нужна согласованная политика очистки и защита от массового создания аккаунтов.
- Наличие email может быть косвенно определено по отказу повторной регистрации;
  текст ошибки не сообщает прямо о существовании аккаунта.
- Изоляция будущих документов/чатов проверяется сейчас на reusable dependency;
  сами бизнес-ресурсы и их ACL будут реализованы в соответствующих этапах.
- Реальные медицинские данные и production-допуск требуют полной security-приёмки.

## Файлы этапа

Созданы: `backend/app/db.py`, `backend/app/auth/{__init__,models,security,routes}.py`,
`backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`,
`backend/alembic/versions/0001_auth.py`, `backend/tests/test_security.py`,
`backend/tests/test_auth_integration.py`, `frontend/app/auth-client.ts`,
`frontend/app/auth-form.tsx`, `frontend/app/login/page.tsx`,
`frontend/app/register/page.tsx`, `frontend/app/(private)/layout.tsx`,
`frontend/app/(private)/{chat,documents,analyses,settings,profile,logout}/page.tsx`,
`frontend/tests/auth.test.ts`, `docs/STAGE2.md`.

Изменены: `backend/pyproject.toml`, `backend/app/core/config.py`, `backend/app/main.py`,
`frontend/app/page.tsx`, `frontend/next.config.ts`, `infrastructure/compose.yaml`,
`.env.example`, `.github/workflows/ci.yml`, `README.md`, `docs/ARCHITECTURE.md`,
`docs/OPERATIONS.md`, `docs/CODEX_TASKS.md` (существующий план сохранён).
