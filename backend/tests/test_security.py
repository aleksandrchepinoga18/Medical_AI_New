from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from app.auth.models import User
from app.auth.routes import Credentials, require_owner, set_cookie
from app.auth.security import digest, hasher, verify_password
from app.db import get_settings


def test_argon2id_and_wrong_password() -> None:
    encoded = hasher.hash("synthetic-password-123")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "synthetic-password-123")
    assert not verify_password(encoded, "incorrect-password")
    assert digest("secret") != "secret"


def test_identity_normalization_and_password_redaction() -> None:
    value = Credentials(email=" Test@Example.org ", password="synthetic-password")
    assert value.email == "test@example.org"
    assert "synthetic-password" not in repr(value)


def test_owner_policy() -> None:
    user = User(id=uuid4(), email="owner@example.org", password_hash="unused")
    require_owner(user.id, user)
    with pytest.raises(HTTPException) as error:
        require_owner(uuid4(), user)
    assert error.value.status_code == 404


def test_secure_cookie_and_production_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_PASSWORD", "synthetic-unused")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="HTTPS"):
            get_settings()
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
        monkeypatch.setenv("AUTH_ORIGIN", "https://medical.example.org")
        get_settings.cache_clear()
        response = Response()
        set_cookie(response, "medical_session", "synthetic-token", httponly=True)
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Domain=" not in cookie
    finally:
        get_settings.cache_clear()
