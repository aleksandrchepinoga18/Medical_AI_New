from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.models import User
from app.core.config import Settings
from app.documents.gigachat import UPLOAD_BYTES
from app.documents.models import Document, DocumentIngestionJob
from app.documents.schemas import (
    EXTERNAL_UNRESOLVED,
    ExternalAuthorization,
    ExternalExtractionInput,
    LocalRefreshRequest,
    VersionRequest,
)
from app.documents.validation import inspect_upload
from app.integrations.object_storage import ObjectStorage, StorageError, make_object_key
from app.rag.service import latest_version

ACTIVE = ("uploading", "queued", "processing")
ACTIVE_JOBS = ("pending", "published", "processing")


def owned_document(
    db: Session, document_id: UUID, owner_id: UUID, *, lock: bool = False
) -> Document:
    query = select(Document).where(Document.id == document_id, Document.owner_id == owner_id)
    if lock:
        query = query.with_for_update()
    document = db.scalar(query)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document


def create_upload(
    db: Session,
    owner_id: UUID,
    source: BinaryIO,
    filename: str,
    request_key: UUID,
    settings: Settings,
    storage: ObjectStorage,
) -> Document:
    metadata = inspect_upload(source, filename, settings)
    # Serialize a user's quota/idempotency checks; no lock is held during MinIO I/O.
    db.scalar(select(User).where(User.id == owner_id).with_for_update())
    existing = db.scalar(
        select(Document).where(
            Document.owner_id == owner_id, Document.idempotency_key == request_key
        )
    )
    if existing:
        if (existing.sha256, existing.size_bytes, existing.format) != (
            metadata.sha256,
            metadata.size,
            metadata.format,
        ):
            raise HTTPException(409, "Idempotency key was used for another file")
        db.commit()
        return existing
    total = db.scalar(
        select(func.count()).select_from(Document).where(Document.owner_id == owner_id)
    )
    active = db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.owner_id == owner_id, Document.status.in_(ACTIVE))
    )
    if (total or 0) >= settings.documents_max_per_user or (
        active or 0
    ) >= settings.documents_max_active:
        raise HTTPException(429, "Document quota reached", headers={"Retry-After": "60"})
    document_id = uuid4()
    key = make_object_key(owner_id, document_id)
    document = Document(
        id=document_id,
        owner_id=owner_id,
        original_filename=metadata.filename,
        format=metadata.format,
        size_bytes=metadata.size,
        sha256=metadata.sha256,
        object_key=key,
        status="uploading",
        idempotency_key=request_key,
    )
    job_id = uuid4()
    reservation = DocumentIngestionJob(
        id=job_id,
        document_id=document_id,
        version=1,
        status="pending",
        available_at=datetime.now(UTC) + timedelta(seconds=settings.documents_upload_grace_seconds),
    )
    db.add(document)
    db.flush()
    db.add(reservation)
    db.commit()  # Durable key reservation precedes the object write.
    try:
        result = storage.upload_original(
            owner_id, document_id, source, metadata.format, object_key=key
        )
        if (result.sha256, result.size_bytes) != (metadata.sha256, metadata.size):
            raise StorageError("Upload checksum mismatch")
    except StorageError:
        # The remote write may have succeeded despite a lost response. Keep reservation/outbox;
        # recovery verifies the exact object after the grace period, without deleting anything.
        raise HTTPException(503, "Upload could not be confirmed; retry with the same key") from None
    job = db.scalar(
        select(DocumentIngestionJob).where(DocumentIngestionJob.id == job_id).with_for_update()
    )
    assert job is not None
    document = owned_document(db, document_id, owner_id, lock=True)
    if job.status != "pending" or document.status != "uploading":
        db.commit()
        return document  # Recovery already owns or completed a slow upload.
    now = datetime.now(UTC)
    document.status, document.updated_at = "queued", now
    job.available_at = now
    db.commit()
    return document


def retry_document(db: Session, document_id: UUID, owner_id: UUID, settings: Settings) -> Document:
    db.scalar(select(User).where(User.id == owner_id).with_for_update())
    document = owned_document(db, document_id, owner_id, lock=True)
    if document.status not in {"failed"} or document.error_code in {
        "UPLOAD_INCOMPLETE",
        "CHECKSUM_MISMATCH",
        "ORIGINAL_MISSING",
    }:
        raise HTTPException(409, "Document cannot be retried in its current state")
    active = db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.owner_id == owner_id, Document.status.in_(ACTIVE))
    )
    if (active or 0) >= settings.documents_max_active:
        raise HTTPException(429, "Document quota reached", headers={"Retry-After": "60"})
    version = (
        db.scalar(
            select(func.max(DocumentIngestionJob.version)).where(
                DocumentIngestionJob.document_id == document.id
            )
        )
        or 0
    )
    if version >= settings.documents_max_versions:
        raise HTTPException(429, "Document retry limit reached")
    document.status, document.error_code = "queued", None
    document.updated_at = datetime.now(UTC)
    db.add(
        DocumentIngestionJob(
            document_id=document.id,
            version=version + 1,
            status="pending",
        ),
    )
    db.commit()
    return document


def authorize_external(
    db: Session,
    document_id: UUID,
    owner_id: UUID,
    data: ExternalExtractionInput,
    settings: Settings,
) -> DocumentIngestionJob:
    db.scalar(select(User).where(User.id == owner_id).with_for_update())
    document = owned_document(db, document_id, owner_id, lock=True)
    if not data.consent:
        raise HTTPException(403, "Explicit consent is required")
    if settings.documents_extraction_provider != data.provider:
        raise HTTPException(409, "External extraction is disabled")
    if document.format != "pdf" or document.size_bytes > UPLOAD_BYTES:
        raise HTTPException(415, "PDF is outside the supported external extraction scope")
    version = latest_version(db, document.id)
    if (
        document.status not in {"ready", "needs_ocr"}
        or document.sha256 != data.sha256
        or version is None
        or version.version != data.text_version
    ):
        raise HTTPException(409, "The requested original and text version are not current")
    jobs = select(DocumentIngestionJob).where(DocumentIngestionJob.document_id == document.id)
    if db.scalar(jobs.where(DocumentIngestionJob.error_code.in_(EXTERNAL_UNRESOLVED)).limit(1)):
        raise HTTPException(409, "Previous external upload or cleanup is unconfirmed")
    last = db.scalar(jobs.order_by(DocumentIngestionJob.version.desc()).limit(1))
    if last is not None and last.external_request is not None:
        if last.external_request.get("provider") == "pymupdf_tables":
            if last.status in ACTIVE_JOBS:
                raise HTTPException(409, "Local extraction is in progress")
        else:
            previous = ExternalAuthorization.model_validate(last.external_request)
            if previous.text_version == data.text_version and (
                last.status in ACTIVE_JOBS or last.status == "succeeded"
            ):
                db.commit()
                return last
    authorization = ExternalAuthorization(
        **data.model_dump(), owner_id=owner_id, consented_at=datetime.now(UTC),
    )
    return queue_extraction(db, document, last, authorization, settings)


def refresh_local(
    db: Session, document_id: UUID, owner_id: UUID, settings: Settings,
) -> DocumentIngestionJob:
    db.scalar(select(User).where(User.id == owner_id).with_for_update())
    document = owned_document(db, document_id, owner_id, lock=True)
    version = latest_version(db, document.id)
    if document.format != "pdf" or document.status not in {"ready", "needs_ocr"} or version is None:
        raise HTTPException(409, "Local refresh requires a processed PDF")
    last = db.scalar(
        select(DocumentIngestionJob)
        .where(DocumentIngestionJob.document_id == document.id)
        .order_by(DocumentIngestionJob.version.desc()).limit(1)
    )
    if last is not None and last.status in ACTIVE_JOBS:
        if last.external_request and last.external_request.get("provider") == "pymupdf_tables":
            db.commit()
            return last
        raise HTTPException(409, "Extraction is in progress")
    request = LocalRefreshRequest(
        owner_id=owner_id, sha256=document.sha256, text_version=version.version,
    )
    return queue_extraction(db, document, last, request, settings)


def queue_extraction(
    db: Session, document: Document, last: DocumentIngestionJob | None,
    request: VersionRequest, settings: Settings,
) -> DocumentIngestionJob:
    active = db.scalar(
        select(func.count())
        .select_from(DocumentIngestionJob)
        .join(Document, Document.id == DocumentIngestionJob.document_id)
        .where(
            Document.owner_id == document.owner_id,
            DocumentIngestionJob.external_request.is_not(None),
            DocumentIngestionJob.status.in_(ACTIVE_JOBS),
        )
    )
    if (active or 0) >= settings.documents_max_active:
        raise HTTPException(429, "Document quota reached", headers={"Retry-After": "60"})
    next_version = (last.version if last else 0) + 1
    if next_version > settings.documents_max_versions:
        raise HTTPException(429, "Document retry limit reached")
    job = DocumentIngestionJob(
        document_id=document.id,
        version=next_version,
        status="pending",
        external_request=request.model_dump(mode="json"),
    )
    db.add(job)
    db.commit()
    return job
