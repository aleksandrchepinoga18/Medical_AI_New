"""Lazy PostgreSQL connections; never create tables or migrate at startup."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings


def database_url(settings: Settings) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.app_env == "production" and (
        not settings.auth_cookie_secure or not settings.auth_origin.startswith("https://")
    ):
        raise ValueError("Production auth requires HTTPS origin and Secure cookies")
    return settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(database_url(get_settings()), pool_pre_ping=True, hide_parameters=True)


def get_db() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
