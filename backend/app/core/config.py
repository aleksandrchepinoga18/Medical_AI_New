"""Environment configuration without network connections at import time."""

from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    app_env: str = "development"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "medical_ai"
    postgres_user: str = "medical_ai"
    postgres_password: SecretStr = SecretStr("")
    auth_origin: str = "http://localhost:3000"
    auth_cookie_secure: bool = False
    auth_session_seconds: int = Field(default=28800, ge=60, le=604800)
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: SecretStr
    mlflow_tracking_uri: str = "http://mlflow:5000"
    mlflow_registry_uri: str = "http://mlflow:5000"
    mlflow_experiment_name: str = "medical-ai-development"
    mlflow_tracing_enabled: bool = False

    def redis_url(self, database: int) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{database}"
