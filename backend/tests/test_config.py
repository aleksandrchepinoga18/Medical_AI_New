from pydantic import SecretStr

from app.core.config import Settings


def test_redis_password_is_url_encoded() -> None:
    settings = Settings(redis_password=SecretStr("p@ss:/?#%"))
    assert settings.redis_url(1) == "redis://:p%40ss%3A%2F%3F%23%25@redis:6379/1"


def test_configuration_repr_hides_password() -> None:
    settings = Settings(redis_password=SecretStr("private-test-password"))
    assert "private-test-password" not in repr(settings)


def test_unit_settings_use_defaults_without_ambient_configuration() -> None:
    settings = Settings()
    assert settings.app_env == "development"
    assert settings.documents_s3_bucket == "medical-ai-documents"
    assert settings.documents_s3_access_key.get_secret_value() == ""
    assert settings.documents_s3_secret_key.get_secret_value() == ""
    assert settings.documents_max_bytes == 20971520
    assert settings.rag_enabled is False
    assert settings.rag_embedding_digest == ""
    assert settings.rag_qdrant_api_key.get_secret_value() == ""
    assert settings.redis_password.get_secret_value() == "synthetic-unit-redis-password"


def test_unit_settings_ignore_dotenv_and_secret_files(tmp_path) -> None:
    dotenv = tmp_path / "synthetic.env"
    dotenv.write_text(
        "DOCUMENTS_S3_SECRET_KEY=synthetic-dotenv-secret\nRAG_CHUNK_CHARS=900\n",
        encoding="utf-8",
    )
    (tmp_path / "documents_s3_secret_key").write_text("synthetic-file-secret", encoding="utf-8")
    settings = Settings(_env_file=dotenv, _secrets_dir=tmp_path)
    assert settings.documents_s3_secret_key.get_secret_value() == ""
    assert settings.rag_chunk_chars == 1200


def test_explicit_synthetic_settings_override_synthetic_environment(monkeypatch) -> None:
    monkeypatch.setenv("DOCUMENTS_S3_SECRET_KEY", "synthetic-environment-secret")
    settings = Settings(documents_s3_secret_key=SecretStr("synthetic-explicit-secret"))
    assert settings.documents_s3_secret_key.get_secret_value() == "synthetic-explicit-secret"
