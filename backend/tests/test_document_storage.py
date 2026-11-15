import hashlib
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.integrations.object_storage import (
    CHUNK_BYTES,
    ObjectStorage,
    StorageError,
    make_object_key,
    validate_object_key,
    validate_storage_settings,
)


def settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "redis_password": "synthetic-unused",
        "documents_s3_access_key": "synthetic-access",
        "documents_s3_secret_key": "synthetic-secret-not-real",
        **updates,
    }
    return Settings.model_validate(values)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.body = BytesIO(data)
        self.closed = False
        self.released = False
        self.fail = False

    def read(self, amt: int) -> bytes:
        assert amt <= CHUNK_BYTES
        if self.fail:
            raise OSError("private-internal-error")
        return self.body.read(amt)

    def close(self) -> None:
        self.closed = True
        self.body.close()

    def release_conn(self) -> None:
        self.released = True


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[str, str] = {}
        self.response: FakeResponse | None = None
        self.fail = False

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        if self.fail:
            raise OSError("private-internal-error")
        content = data.read()
        assert len(content) == length
        self.objects[bucket_name, object_name] = content
        self.metadata = metadata
        return None

    def get_object(self, bucket_name: str, object_name: str) -> FakeResponse:
        if self.fail:
            raise OSError("private-internal-error")
        self.response = FakeResponse(self.objects[bucket_name, object_name])
        return self.response


def test_upload_hash_stream_and_close() -> None:
    client = FakeClient()
    storage = ObjectStorage(settings(), client)
    owner, document = uuid4(), uuid4()
    content = b"synthetic original\x00" * 10000
    result = storage.upload_original(owner, document, BytesIO(content), "pdf")
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert client.metadata == {"sha256": result.sha256}
    assert b"".join(storage.stream_original(owner, document, result.object_key)) == content
    assert client.response and client.response.closed and client.response.released


def test_early_download_cancellation_and_read_failure_release_connection() -> None:
    client = FakeClient()
    storage = ObjectStorage(settings(), client)
    owner, document = uuid4(), uuid4()
    result = storage.upload_original(owner, document, BytesIO(b"x" * 100000), "docx")
    stream = storage.stream_original(owner, document, result.object_key)
    next(stream)
    stream.close()
    assert client.response and client.response.closed and client.response.released
    with pytest.raises(StorageError, match="Original read failed"):
        with storage.open_original(owner, document, result.object_key) as response:
            assert client.response
            client.response.fail = True
            response.read(16)
    assert client.response and client.response.closed and client.response.released


def test_invalid_or_cross_owner_keys_never_reach_storage() -> None:
    owner, document = uuid4(), uuid4()
    key = make_object_key(owner, document)
    validate_object_key(owner, document, key)
    for bad_key in (key + "/../original", "../../secret", key.replace(str(owner), str(uuid4()))):
        with pytest.raises(ValueError):
            validate_object_key(owner, document, bad_key)
    assert key != make_object_key(owner, document)
    with pytest.raises(ValueError):
        validate_object_key(owner, UUID(int=0), key)


def test_upload_limits_and_safe_errors() -> None:
    client = FakeClient()
    storage = ObjectStorage(settings(documents_max_bytes=10), client)
    for data in (b"", b"x" * 11):
        with pytest.raises(ValueError):
            storage.upload_original(uuid4(), uuid4(), BytesIO(data), "pdf")
    with pytest.raises(ValueError):
        storage.upload_original(uuid4(), uuid4(), BytesIO(b"x"), "exe")
    assert not client.objects
    client.fail = True
    with pytest.raises(StorageError) as error:
        storage.upload_original(uuid4(), uuid4(), BytesIO(b"x"), "pdf")
    assert "private-internal-error" not in str(error.value)


@pytest.mark.parametrize(
    "updates",
    [
        {"documents_s3_endpoint": "http://user:secret@minio:9000/path"},
        {"documents_s3_endpoint": "minio:99999"},
        {"documents_s3_bucket": "mlflow-artifacts"},
        {"documents_s3_bucket": "../bucket"},
        {"documents_s3_access_key": ""},
        {"documents_s3_secret_key": "CHANGE_ME_SECRET"},
    ],
)
def test_reject_unsafe_configuration(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_storage_settings(settings(**updates))


def test_secrets_hidden_and_existing_configuration_still_works() -> None:
    configured = settings()
    validate_storage_settings(configured)
    assert "synthetic-secret-not-real" not in repr(configured)
    assert "synthetic-access" not in repr(configured)
    # The shared unit fixture removes ambient settings and disables file sources.
    original = Settings(_env_file=None, redis_password=SecretStr("synthetic-unused"))
    assert original.documents_s3_access_key.get_secret_value() == ""
    assert original.documents_s3_secret_key.get_secret_value() == ""
    with pytest.raises(ValidationError):
        settings(documents_max_bytes=0)
