"""Manual storage verification. Default is read-only; explicit smoke mode adds one object."""

import hashlib
import sys
from collections.abc import Callable
from io import BytesIO
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from app.db import get_settings
from app.integrations.object_storage import ObjectStorage, validate_storage_settings


def expect_denied(action: Callable[[], object]) -> None:
    try:
        action()
    except S3Error as error:
        if error.code == "AccessDenied":
            return
        raise RuntimeError("Unexpected storage verification response") from None
    raise RuntimeError("Storage isolation check failed")


def main() -> None:
    if sys.argv[1:] not in ([], ["--write-synthetic"]):
        raise RuntimeError("Use no arguments or --write-synthetic")
    settings = get_settings()
    validate_storage_settings(settings)
    client = Minio(
        settings.documents_s3_endpoint,
        access_key=settings.documents_s3_access_key.get_secret_value(),
        secret_key=settings.documents_s3_secret_key.get_secret_value(),
        secure=settings.documents_s3_secure,
        region=settings.documents_s3_region,
    )
    anonymous = Minio(
        settings.documents_s3_endpoint,
        secure=settings.documents_s3_secure,
        region=settings.documents_s3_region,
    )
    # Consume the lazy iterator to actually check server-side permissions.
    next(client.list_objects(settings.documents_s3_bucket, prefix="users/"), None)
    expect_denied(lambda: next(client.list_objects(settings.mlflow_artifact_bucket), None))
    expect_denied(lambda: next(anonymous.list_objects(settings.documents_s3_bucket), None))
    if sys.argv[1:] == ["--write-synthetic"]:
        storage = ObjectStorage.from_settings(settings)
        owner, document = uuid4(), uuid4()
        payload = b"Medical AI stage 3.1 synthetic storage check; not a medical document.\n"
        original = storage.upload_original(owner, document, BytesIO(payload), "pdf")
        received = b"".join(storage.stream_original(owner, document, original.object_key))
        if received != payload or original.sha256 != hashlib.sha256(received).hexdigest():
            raise RuntimeError("Original bytes or checksum mismatch")

        def anonymous_read() -> None:
            response = anonymous.get_object(settings.documents_s3_bucket, original.object_key)
            try:
                response.read(1)
            finally:
                response.close()
                response.release_conn()

        expect_denied(anonymous_read)
        print("PASS: upload/read/stream/checksum/private-object; one synthetic object retained.")
        print(f"Synthetic object key: {original.object_key}")
    else:
        print("PASS: bucket readable with scoped credentials; MLflow and anonymous listing denied.")
        print("No objects created. Use explicit --write-synthetic to test original round-trip.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: verify document bucket, credentials and policies privately.", file=sys.stderr)
        sys.exit(1)
