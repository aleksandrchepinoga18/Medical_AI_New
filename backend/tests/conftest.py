"""Unit tests use synthetic settings; integration fixtures retain their test environment."""

import os
from collections.abc import Iterator

import pytest
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.core.config import Settings
from app.db import get_settings


@pytest.fixture(autouse=True)
def isolate_unit_settings(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    # Inspect names only. Never print environment values or open the working .env.
    settings_names = {name.casefold() for name in Settings.model_fields}
    for name in tuple(os.environ):
        if name.casefold() in settings_names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REDIS_PASSWORD", "synthetic-unit-redis-password")

    def unit_sources(
        cls: type[Settings],
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Keep synthetic monkeypatch.setenv tests working, but exclude dotenv/secrets files.
        return init_settings, env_settings

    monkeypatch.setattr(Settings, "settings_customise_sources", classmethod(unit_sources))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
