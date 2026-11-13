from pydantic import SecretStr

from app.core.config import Settings


def test_redis_password_is_url_encoded() -> None:
    settings = Settings(redis_password=SecretStr("p@ss:/?#%"))
    assert settings.redis_url(1) == "redis://:p%40ss%3A%2F%3F%23%25@redis:6379/1"


def test_configuration_repr_hides_password() -> None:
    settings = Settings(redis_password=SecretStr("private-test-password"))
    assert "private-test-password" not in repr(settings)
