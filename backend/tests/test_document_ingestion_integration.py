"""Explicitly enabled against the isolated document test Compose only; no table cleanup."""

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf
import pytest
from alembic.config import Config
from docx import Document as DocxDocument
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import Settings
from app.db import database_url, get_db, get_settings
from app.documents import processing
from app.documents.models import DocumentIngestionJob, DocumentTextVersion
from app.documents.processing import ExtractionResult, process_job
from app.documents.routes import get_storage
from app.integrations.object_storage import ObjectStorage, StorageError
from app.main import app
from app.workers.ingestion_dispatcher import dispatch_once

pytestmark = pytest.mark.integration
ORIGIN = "http://localhost:3000"
PAST = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def environment() -> Iterator[tuple[Engine, ObjectStorage, Settings]]:
    if os.environ.get("RUN_DOCUMENT_INTEGRATION") != "1":
        pytest.skip("Run through isolated compose.documents-test.yaml")
    get_settings.cache_clear()
    settings = get_settings()
    if (
        settings.postgres_db != "medical_ai_documents_test"
        or not settings.documents_s3_bucket.endswith("-test")
    ):
        pytest.fail("Refusing document integration tests outside dedicated test resources")
    engine = create_engine(database_url(settings), hide_parameters=True)
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    command.check(Config(str(Path(__file__).parents[1] / "alembic.ini")))
    client = Minio(
        settings.documents_s3_endpoint,
        access_key=settings.documents_s3_access_key.get_secret_value(),
        secret_key=settings.documents_s3_secret_key.get_secret_value(),
        secure=settings.documents_s3_secure,
        region=settings.documents_s3_region,
    )
    # Test-only bucket in an isolated, tmpfs MinIO. Never provision production credentials.
    if not client.bucket_exists(settings.documents_s3_bucket):
        client.make_bucket(settings.documents_s3_bucket)
    storage = ObjectStorage.from_settings(settings)

    def database() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_storage] = lambda: storage
    yield engine, storage, settings
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client(environment) -> Iterator[TestClient]:
    with TestClient(app, client=(uuid4().hex, 50000)) as value:
        csrf = headers(value)
        response = value.post(
            "/auth/register",
            headers=csrf,
            json={
                "email": f"{uuid4()}@example.org",
                "password": "synthetic-password-123",
            },
        )
        assert response.status_code == 201
        yield value


def headers(client: TestClient, key: UUID | None = None) -> dict[str, str]:
    response = client.get("/auth/csrf")
    assert response.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": response.json()["csrf_token"],
        "Idempotency-Key": str(key or uuid4()),
    }


def pdf_bytes(text: str = "Synthetic document only") -> bytes:
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), text)
        return document.tobytes()


def upload(
    client: TestClient,
    *,
    key: UUID | None = None,
    payload: bytes | None = None,
    filename: str = "synthetic.pdf",
):
    return client.post(
        "/documents",
        headers=headers(client, key),
        files={"file": (filename, payload or pdf_bytes(), "application/octet-stream")},
    )


def due_job(engine: Engine, document_id: str) -> UUID:
    with Session(engine) as db:
        job = db.scalar(
            select(DocumentIngestionJob)
            .where(DocumentIngestionJob.document_id == UUID(document_id))
            .order_by(DocumentIngestionJob.version.desc())
        )
        assert job
        job.available_at = PAST
        db.commit()
        return job.id


def test_owner_isolation_csrf_and_idempotency(client: TestClient, environment, monkeypatch) -> None:
    original_upload = environment[1].upload_original
    writes = []

    def counted_upload(*args, **kwargs):
        writes.append(True)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(environment[1], "upload_original", counted_upload)
    key, payload = uuid4(), pdf_bytes()
    first = upload(client, key=key, payload=payload)
    assert first.status_code == 202
    document_id = first.json()["id"]
    assert "object_key" not in first.text and "owner_id" not in first.text
    assert upload(client, key=key, payload=payload).json()["id"] == document_id
    different = upload(client, key=key, payload=pdf_bytes("Different synthetic content"))
    assert different.status_code == 409
    assert len(writes) == 1
    assert client.get("/documents").json()["total"] == 1
    assert client.get(f"/documents/{document_id}/download").content == payload
    assert client.post(f"/documents/{document_id}/retry").status_code == 403
    bad_origin = {**headers(client), "Origin": "https://other.example"}
    response = client.post("/documents", headers=bad_origin, files={"file": ("a.pdf", payload)})
    assert response.status_code == 403
    with TestClient(app, client=(uuid4().hex, 50000)) as other:
        assert other.get("/documents").status_code == 401
        assert other.post("/documents", files={"file": ("a.pdf", payload)}).status_code == 401
        assert (
            other.post(
                "/auth/register",
                headers=headers(other),
                json={
                    "email": f"{uuid4()}@example.org",
                    "password": "synthetic-password-123",
                },
            ).status_code
            == 201
        )
        assert other.get("/documents").json()["total"] == 0
        for path in (f"/documents/{document_id}", f"/documents/{document_id}/download"):
            assert other.get(path).status_code == 404
        response = other.post(f"/documents/{document_id}/retry", headers=headers(other))
        assert response.status_code == 404
        assert upload(other, key=key, payload=payload).json()["id"] != document_id


def test_rejected_formats_and_body_limits(client: TestClient, monkeypatch) -> None:
    assert (
        client.post(
            "/documents", headers=headers(client), files={"file": ("bad.pdf", b"not-pdf")}
        ).status_code
        == 415
    )
    limited = get_settings().model_copy(update={"documents_max_bytes": 100})
    monkeypatch.setattr("app.documents.routes.get_settings", lambda: limited)
    response = upload(client, payload=b"%PDF-" + b"x" * 70000)
    assert response.status_code == 413
    body = (
        b'--test\r\nContent-Disposition: form-data; name="file"; filename="oversize.pdf"'
        b"\r\nContent-Type: application/pdf\r\n\r\n%PDF-" + b"x" * 70000 + b"\r\n--test--\r\n"
    )
    chunked = client.post(
        "/documents",
        headers={**headers(client), "Content-Type": "multipart/form-data; boundary=test"},
        content=iter(body[index : index + 1024] for index in range(0, len(body), 1024)),
    )
    assert chunked.status_code == 413
    assert client.get("/documents").json()["total"] == 0


@pytest.mark.parametrize("format_", ["pdf", "docx"])
def test_actual_worker_pipeline_and_duplicate_delivery(
    client: TestClient, environment, format_: str
) -> None:
    from app.workers.celery_app import celery_app

    engine, _, settings = environment
    payload = pdf_bytes()
    if format_ == "docx":
        document = DocxDocument()
        document.add_paragraph("Synthetic document only")
        buffer = BytesIO()
        document.save(buffer)
        payload = buffer.getvalue()
    response = upload(client, payload=payload, filename=f"synthetic.{format_}")
    assert response.status_code == 202
    document_id = response.json()["id"]
    job_id = due_job(engine, document_id)

    def publish(value: UUID) -> None:
        assert value == job_id
        celery_app.send_task(
            "documents.process", args=[str(value)], queue="document_ingestion", retry=False
        )

    assert dispatch_once(engine, publish, settings)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        details = client.get(f"/documents/{document_id}").json()
        if details["status"] in {"ready", "failed", "needs_ocr"}:
            break
        time.sleep(0.25)
    assert details["status"] == "ready", details.get("error_code")
    assert "Synthetic document only" in details["text_version"]["normalized_text"]
    celery_app.send_task(
        "documents.process", args=[str(job_id)], queue="document_ingestion", retry=False
    )
    process_job(job_id, engine, environment[1], settings)
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(DocumentTextVersion)
                .where(DocumentTextVersion.document_id == UUID(document_id))
            )
            == 1
        )


def test_lost_upload_response_recovers_reserved_object(
    client: TestClient, environment, monkeypatch
) -> None:
    engine, storage, settings = environment
    original_upload = storage.upload_original

    def lost_ack(*args, **kwargs):
        original_upload(*args, **kwargs)
        raise StorageError("synthetic lost response")

    monkeypatch.setattr(storage, "upload_original", lost_ack)
    assert upload(client).status_code == 503
    item = client.get("/documents").json()["items"][0]
    assert item["status"] == "uploading"
    job_id = due_job(engine, item["id"])
    process_job(job_id, engine, storage, settings)
    assert client.get(f"/documents/{item['id']}").json()["status"] == "ready"


def test_backoff_retry_and_single_result(client: TestClient, environment, monkeypatch) -> None:
    engine, storage, settings = environment
    item = upload(client).json()
    job_id = due_job(engine, item["id"])

    def unavailable(*args):
        raise StorageError("synthetic temporary failure")

    monkeypatch.setattr(processing, "extract_original", unavailable)
    process_job(job_id, engine, storage, settings)
    with Session(engine) as db:
        job = db.get(DocumentIngestionJob, job_id)
        assert job and job.attempts == 1 and job.status == "pending"
        assert job.available_at > datetime.now(UTC)
    process_job(job_id, engine, storage, settings)  # Duplicate cannot bypass backoff.
    monkeypatch.setattr(
        processing, "extract_original", lambda *args: ExtractionResult(text="synthetic")
    )
    due_job(engine, item["id"])
    process_job(job_id, engine, storage, settings)
    process_job(job_id, engine, storage, settings)
    details = client.get(f"/documents/{item['id']}").json()
    assert details["status"] == "ready" and details["text_version"]["version"] == 1


def test_expired_worker_is_fenced_and_recovered(
    client: TestClient, environment, monkeypatch
) -> None:
    engine, storage, settings = environment
    item = upload(client).json()
    job_id = due_job(engine, item["id"])

    def stale_attempt(*args):
        with Session(engine) as db:
            job = db.get(DocumentIngestionJob, job_id)
            job.lease_token = uuid4()
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            job.available_at = PAST
            db.commit()
        return ExtractionResult(text="stale result must not persist")

    monkeypatch.setattr(processing, "extract_original", stale_attempt)
    process_job(job_id, engine, storage, settings)
    assert client.get(f"/documents/{item['id']}").json()["text_version"] is None
    published = []
    assert dispatch_once(engine, published.append, settings)
    assert published == [job_id]
    monkeypatch.setattr(
        processing, "extract_original", lambda *args: ExtractionResult(text="new attempt")
    )
    process_job(job_id, engine, storage, settings)
    details = client.get(f"/documents/{item['id']}").json()
    assert details["text_version"]["normalized_text"] == "new attempt"


def test_broker_failures_bounded_and_manual_retry(client: TestClient, environment) -> None:
    engine, _, settings = environment
    item = upload(client).json()

    def unavailable(value: UUID) -> None:
        raise ConnectionError("synthetic broker failure")

    for _ in range(settings.documents_max_attempts):
        due_job(engine, item["id"])
        assert dispatch_once(engine, unavailable, settings)
    details = client.get(f"/documents/{item['id']}").json()
    assert details["status"] == "failed" and details["error_code"] == "QUEUE_UNAVAILABLE"
    assert client.post(f"/documents/{item['id']}/retry", headers=headers(client)).status_code == 202
    assert client.post(f"/documents/{item['id']}/retry", headers=headers(client)).status_code == 409
