"""Original storage. Future callers must authorize access and validate file contents."""

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Protocol, cast
from uuid import UUID, uuid4

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Timeout

from app.core.config import Settings

CHUNK_BYTES = 65536
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class StorageError(Exception):
    """Contains no SDK response or credentials."""


class OriginalNotFound(StorageError):
    pass


class ObjectResponse(Protocol):
    def read(self, amt: int) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class BinaryReader(Protocol):
    """Minimal binary input required by the storage client's upload operation."""

    def read(self, size: int = -1, /) -> bytes: ...


class SafeObjectResponse:
    def __init__(self, response: ObjectResponse) -> None:
        self._response = response

    def read(self, amt: int) -> bytes:
        try:
            return self._response.read(amt)
        except Exception:
            raise StorageError("Original read failed") from None

    def close(self) -> None:
        self._response.close()

    def release_conn(self) -> None:
        self._response.release_conn()


class StorageClient(Protocol):
    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryReader,
        length: int,
        content_type: str,
        metadata: dict[str, str],
    ) -> object: ...

    def get_object(self, bucket_name: str, object_name: str) -> ObjectResponse: ...


@dataclass(frozen=True)
class StoredOriginal:
    object_key: str
    size_bytes: int
    sha256: str


def validate_storage_settings(settings: Settings) -> None:
    endpoint = settings.documents_s3_endpoint
    if not re.fullmatch(r"[a-zA-Z0-9.-]+:[0-9]{1,5}", endpoint):
        raise ValueError("Document S3 endpoint must be host:port without credentials or path")
    if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("Invalid document S3 port")
    bucket = settings.documents_s3_bucket
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
        or bucket == settings.mlflow_artifact_bucket
    ):
        raise ValueError("Document bucket must be a separate lowercase DNS label")
    for value in (settings.documents_s3_access_key, settings.documents_s3_secret_key):
        secret = value.get_secret_value()
        if not secret or secret.startswith("CHANGE_ME"):
            raise ValueError("Dedicated document storage credentials must be configured")
    if len(settings.documents_s3_secret_key.get_secret_value()) < 16:
        raise ValueError("Document storage secret must have at least 16 characters")


def make_object_key(owner_id: UUID, document_id: UUID) -> str:
    if not isinstance(owner_id, UUID) or not isinstance(document_id, UUID):
        raise ValueError("Server UUIDs are required")
    return f"users/{owner_id}/documents/{document_id}/originals/{uuid4()}"


def validate_object_key(owner_id: UUID, document_id: UUID, key: str) -> None:
    if not isinstance(owner_id, UUID) or not isinstance(document_id, UUID):
        raise ValueError("Server UUIDs are required")
    prefix = f"users/{owner_id}/documents/{document_id}/originals/"
    suffix = key.removeprefix(prefix)
    try:
        valid = key.startswith(prefix) and str(UUID(suffix)) == suffix
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid document object key")


class ObjectStorage:
    def __init__(self, settings: Settings, client: StorageClient) -> None:
        validate_storage_settings(settings)
        self._client = client
        self._bucket = settings.documents_s3_bucket
        self._max_bytes = settings.documents_max_bytes

    @classmethod
    def from_settings(cls, settings: Settings) -> "ObjectStorage":
        validate_storage_settings(settings)
        client = Minio(
            settings.documents_s3_endpoint,
            access_key=settings.documents_s3_access_key.get_secret_value(),
            secret_key=settings.documents_s3_secret_key.get_secret_value(),
            secure=settings.documents_s3_secure,
            region=settings.documents_s3_region,
            http_client=PoolManager(timeout=Timeout(connect=5, read=30), retries=False, maxsize=4),
        )
        return cls(settings, cast(StorageClient, client))

    def upload_original(
        self,
        owner_id: UUID,
        document_id: UUID,
        source: BinaryReader,
        document_format: str,
        *,
        object_key: str | None = None,
    ) -> StoredOriginal:
        if document_format not in CONTENT_TYPES:
            raise ValueError("Only PDF and DOCX originals are supported")
        key = object_key or make_object_key(owner_id, document_id)
        validate_object_key(owner_id, document_id, key)
        checksum = hashlib.sha256()
        size = 0
        with SpooledTemporaryFile(max_size=1048576, mode="w+b") as staged:
            while chunk := source.read(CHUNK_BYTES):
                size += len(chunk)
                if size > self._max_bytes:
                    raise ValueError("Document exceeds configured storage size limit")
                checksum.update(chunk)
                staged.write(chunk)
            if size == 0:
                raise ValueError("Empty originals are not supported")
            staged.seek(0)
            sha256 = checksum.hexdigest()
            try:
                self._client.put_object(
                    self._bucket,
                    key,
                    staged,
                    size,
                    content_type=CONTENT_TYPES[document_format],
                    metadata={"sha256": sha256},
                )
            except Exception:
                raise StorageError("Original upload failed") from None
        return StoredOriginal(key, size, sha256)

    @contextmanager
    def open_original(
        self, owner_id: UUID, document_id: UUID, object_key: str
    ) -> Iterator[ObjectResponse]:
        validate_object_key(owner_id, document_id, object_key)
        try:
            response = self._client.get_object(self._bucket, object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise OriginalNotFound("Original not found") from None
            raise StorageError("Original read failed") from None
        except Exception:
            raise StorageError("Original read failed") from None
        try:
            yield SafeObjectResponse(response)
        finally:
            try:
                try:
                    response.close()
                finally:
                    response.release_conn()
            except Exception:
                raise StorageError("Original connection cleanup failed") from None

    def stream_original(
        self, owner_id: UUID, document_id: UUID, object_key: str
    ) -> Iterator[bytes]:
        try:
            with self.open_original(owner_id, document_id, object_key) as response:
                while chunk := response.read(CHUNK_BYTES):
                    yield chunk
        except (ValueError, StorageError):
            raise
        except Exception:
            raise StorageError("Original stream failed") from None
