"""Only a dedicated *_test PostgreSQL database is accepted. No table deletion."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.auth.models import AuthSession
from app.auth.routes import AuthenticatedUser, require_owner
from app.auth.security import digest
from app.db import database_url, get_db, get_settings
from app.main import app

pytestmark = pytest.mark.integration
ORIGIN = "http://localhost:3000"
PASSWORD = "synthetic-password-123"


@pytest.fixture(scope="module")
def database() -> Iterator[None]:
    if os.environ.get("RUN_AUTH_INTEGRATION") != "1":
        pytest.skip("Set RUN_AUTH_INTEGRATION=1 with a dedicated PostgreSQL *_test database")
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.postgres_db.endswith("_test") or settings.postgres_db == "mlflow_test":
        pytest.fail("Integration tests require a dedicated application *_test database")
    engine = create_engine(database_url(settings), hide_parameters=True)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    # Sentinel proves additive migration leaves unrelated data intact.
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS stage2_sentinel (id integer PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO stage2_sentinel VALUES (1) ON CONFLICT DO NOTHING"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.check(config)
    assert {"users", "auth_sessions", "auth_login_limits"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM stage2_sentinel WHERE id=1")) == 1

    def override() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override
    yield
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client(database: None) -> Iterator[TestClient]:
    with TestClient(app, client=(uuid4().hex, 50000)) as value:
        yield value


def headers(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/csrf")
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def register(client: TestClient) -> dict[str, str]:
    data = {"email": f"{uuid4()}@example.org", "password": PASSWORD}
    response = client.post("/auth/register", json=data, headers=headers(client))
    assert response.status_code == 201
    return data


def test_session_csrf_logout_and_expiry(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401
    credentials = register(client)
    response = client.get("/auth/me")
    assert response.json()["email"] == credentials["email"]
    assert "password_hash" not in response.text
    assert response.headers["cache-control"] == "no-store"
    token = client.cookies["medical_session"]
    assert client.post("/auth/logout").status_code == 403
    csrf = headers(client)
    response = client.post("/auth/logout", headers={**csrf, "Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/auth/logout", headers=csrf).status_code == 204
    assert client.get("/auth/me").status_code == 401
    with TestClient(app) as revoked:
        revoked.cookies.set("medical_session", token)
        assert revoked.get("/auth/me").status_code == 401
    response = client.post("/auth/login", json=credentials, headers=headers(client))
    assert response.status_code == 200
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert "SameSite=lax" in response.headers.get_list("set-cookie")[0]
    assert client.cookies["medical_session"] != token
    with get_db_override() as db:
        session = db.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == digest(client.cookies["medical_session"])
            )
        )
        assert session is not None
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert client.get("/auth/me").status_code == 401


def test_duplicate_wrong_password_and_session_rotation(client: TestClient) -> None:
    credentials = register(client)
    old_token = client.cookies["medical_session"]
    old_csrf = headers(client)
    duplicate = client.post("/auth/register", json=credentials, headers=old_csrf)
    assert duplicate.status_code == 400
    wrong = client.post(
        "/auth/login", json={**credentials, "password": "incorrect-password"}, headers=old_csrf
    )
    assert wrong.status_code == 401
    response = client.post("/auth/login", json=credentials, headers=old_csrf)
    assert response.status_code == 200
    assert client.cookies["medical_session"] != old_token
    assert client.post("/auth/logout", headers=old_csrf).status_code == 403
    with TestClient(app) as stolen:
        stolen.cookies.set("medical_session", old_token)
        assert stolen.get("/auth/me").status_code == 401


@contextmanager
def get_db_override() -> Iterator[Session]:
    yield from app.dependency_overrides[get_db]()


def test_csrf_credentials_errors_and_throttle(client: TestClient) -> None:
    data = {"email": f"{uuid4()}@example.org", "password": PASSWORD}
    assert client.post("/auth/login", json=data).status_code == 403
    csrf = headers(client)
    for _ in range(10):
        response = client.post("/auth/login", json=data, headers=csrf)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid credentials"}
    assert client.post("/auth/login", json=data, headers=csrf).status_code == 429
    response = client.post("/auth/register", json={**data, "password": "short"}, headers=csrf)
    assert response.status_code == 422
    assert "short" not in response.text


def test_api_owner_isolation(client: TestClient) -> None:
    # Test-only router verifies the same dependency future modules will consume.
    from uuid import UUID

    def resource(owner_id: UUID, user: AuthenticatedUser) -> dict[str, str]:
        require_owner(owner_id, user)
        return {"owner": str(user.id)}

    app.add_api_route("/_test/owners/{owner_id}", resource)
    try:
        register(client)
        first_id = client.get("/auth/me").json()["id"]
        assert client.get(f"/_test/owners/{first_id}").status_code == 200
        with TestClient(app) as second:
            assert second.get(f"/_test/owners/{first_id}").status_code == 401
            register(second)
            assert second.get(f"/_test/owners/{first_id}").status_code == 404
            assert second.get("/auth/me").json()["id"] != first_id
    finally:
        app.router.routes.pop()
