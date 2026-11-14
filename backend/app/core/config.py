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
    mlflow_artifact_bucket: str = "mlflow-artifacts"
    documents_s3_endpoint: str = "minio:9000"
    documents_s3_secure: bool = False
    documents_s3_bucket: str = "medical-ai-documents"
    documents_s3_access_key: SecretStr = SecretStr("")
    documents_s3_secret_key: SecretStr = SecretStr("")
    documents_s3_region: str = "us-east-1"
    documents_max_bytes: int = Field(default=20971520, ge=1, le=1073741824)
    documents_max_pages: int = Field(default=200, ge=1, le=2000)
    documents_max_text_chars: int = Field(default=1000000, ge=1, le=10000000)
    documents_processing_seconds: int = Field(default=60, ge=5, le=600)
    documents_max_unpacked_bytes: int = Field(default=83886080, ge=1024, le=268435456)
    documents_max_attempts: int = Field(default=3, ge=1, le=10)
    documents_max_versions: int = Field(default=5, ge=1, le=20)
    documents_max_active: int = Field(default=10, ge=1, le=100)
    documents_max_per_user: int = Field(default=1000, ge=1, le=100000)
    documents_upload_grace_seconds: int = Field(default=300, ge=120, le=3600)
    documents_outbox_interval: int = Field(default=5, ge=1, le=60)
    documents_outbox_enabled: bool = True

    def redis_url(self, database: int) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{database}"
