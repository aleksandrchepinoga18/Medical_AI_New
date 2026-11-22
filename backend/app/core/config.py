"""Environment configuration without network connections at import time."""

import re
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KNOWLEDGE_COLLECTION = "medical_ai_knowledge_hybrid_v1"


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
    documents_extraction_provider: Literal["pymupdf_tables", "gigachat_ultra"] = "pymupdf_tables"
    gigachat_credentials: SecretStr = SecretStr("")
    gigachat_scope: Literal["GIGACHAT_API_PERS"] = "GIGACHAT_API_PERS"
    gigachat_model: Literal["GigaChat-3-Ultra"] = "GigaChat-3-Ultra"
    gigachat_ca_bundle: str = ""
    rag_enabled: bool = False
    rag_retrieval_mode: Literal["dense", "hybrid"] = "dense"
    chat_enabled: bool = False
    llm_provider: Literal["ollama", "gigachat"] = "ollama"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_ollama_url: Literal["http://ollama:11434", "http://host.docker.internal:11434"] = (
        "http://ollama:11434"
    )
    llm_timeout_seconds: int = Field(default=180, ge=5, le=600)
    llm_context_tokens: int = Field(default=4096, ge=2048, le=32768)
    llm_max_tokens: int = Field(default=1024, ge=32, le=4096)
    llm_gigachat_timeout_seconds: int = Field(default=180, ge=5, le=600)
    llm_gigachat_context_tokens: int = Field(default=16384, ge=2048, le=128000)
    llm_gigachat_max_tokens: int = Field(default=2048, ge=32, le=16000)
    chat_gigachat_history_chars: int = Field(default=4000, ge=1000, le=64000)
    chat_gigachat_context_chars: int = Field(default=6500, ge=1000, le=64000)
    chat_history_chars: int = Field(default=2000, ge=1000, le=64000)
    chat_context_chars: int = Field(default=2000, ge=1000, le=32000)
    rag_chunk_chars: int = Field(default=1200, ge=100, le=8000)
    rag_chunk_overlap: int = Field(default=200, ge=0, le=4000)
    rag_ollama_url: str = "http://ollama:11434"
    rag_embedding_model: str = "bge-m3:567m"
    rag_embedding_digest: str = ""
    rag_embedding_dimensions: int = Field(default=1024, ge=1, le=8192)
    rag_embedding_batch_size: int = Field(default=8, ge=1, le=64)
    rag_http_timeout: int = Field(default=60, ge=1, le=300)
    rag_index_seconds: int = Field(default=900, ge=30, le=7200)
    rag_max_attempts: int = Field(default=5, ge=1, le=10)
    rag_qdrant_url: str = "http://qdrant:6333"
    rag_collection: str = "medical_ai_personal_v1"
    rag_qdrant_api_key: SecretStr = SecretStr("")
    rag_search_max_k: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def validate_rag(self) -> "Settings":
        if self.llm_gigachat_max_tokens + 1024 >= self.llm_gigachat_context_tokens:
            raise ValueError("GigaChat context must leave space for prompts and tool results")
        if self.rag_chunk_overlap >= self.rag_chunk_chars:
            raise ValueError("RAG chunk overlap must be smaller than chunk size")
        if self.rag_collection == KNOWLEDGE_COLLECTION:
            raise ValueError("Personal RAG collection conflicts with medical knowledge")
        if self.rag_embedding_digest and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.rag_embedding_digest,
        ):
            raise ValueError("RAG embedding digest must be a full SHA-256 digest")
        return self

    @property
    def generation_timeout(self) -> int:
        return (
            self.llm_gigachat_timeout_seconds if self.llm_provider == "gigachat"
            else self.llm_timeout_seconds
        )

    @property
    def generation_context(self) -> int:
        return (
            self.llm_gigachat_context_tokens if self.llm_provider == "gigachat"
            else self.llm_context_tokens
        )

    @property
    def generation_output(self) -> int:
        return (
            self.llm_gigachat_max_tokens if self.llm_provider == "gigachat" else self.llm_max_tokens
        )

    @property
    def generation_model(self) -> str:
        return self.gigachat_model if self.llm_provider == "gigachat" else self.llm_model

    @property
    def generation_history_chars(self) -> int:
        return (
            self.chat_gigachat_history_chars if self.llm_provider == "gigachat"
            else self.chat_history_chars
        )

    @property
    def generation_tool_chars(self) -> int:
        return (
            self.chat_gigachat_context_chars if self.llm_provider == "gigachat"
            else self.chat_context_chars
        )

    def redis_url(self, database: int) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{database}"
