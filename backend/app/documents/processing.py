import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document, DocumentIngestionJob, DocumentTextVersion
from app.integrations.object_storage import ObjectStorage, OriginalNotFound, StorageError
from app.rag.service import enqueue


class ProcessingError(Exception):
    pass


class ExtractionResult(BaseModel):
    text: str = ""
    locators: list[dict[str, str | int]] = Field(default_factory=list)
    status: Literal["ready", "needs_ocr"] = "ready"
    error_code: str | None = None


def extract_original(
    document: Document, storage: ObjectStorage, settings: Settings
) -> ExtractionResult:
    deadline = monotonic() + settings.documents_processing_seconds
    with TemporaryDirectory(prefix="medical-ingestion-") as directory:
        source, output = Path(directory) / "original", Path(directory) / "result.json"
        digest, size = hashlib.sha256(), 0
        with source.open("wb") as file:
            with storage.open_original(
                document.owner_id, document.id, document.object_key
            ) as response:
                while chunk := response.read(65536):
                    if monotonic() >= deadline:
                        raise ProcessingError("PROCESSING_TIMEOUT")
                    size += len(chunk)
                    if size > settings.documents_max_bytes or size > document.size_bytes:
                        raise ProcessingError("CHECKSUM_MISMATCH")
                    digest.update(chunk)
                    file.write(chunk)
        if size != document.size_bytes or digest.hexdigest() != document.sha256:
            raise ProcessingError("CHECKSUM_MISMATCH")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProcessingError("PROCESSING_TIMEOUT")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.documents.extract",
                    str(source),
                    document.format,
                    str(output),
                    str(settings.documents_max_pages),
                    str(settings.documents_max_text_chars),
                    str(settings.documents_max_unpacked_bytes),
                    str(settings.documents_processing_seconds),
                    str(os.getpid()),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    **{
                        key: os.environ[key]
                        for key in ("SystemRoot", "WINDIR")
                        if key in os.environ
                    },
                },
            )
        except subprocess.TimeoutExpired:
            raise ProcessingError("PROCESSING_TIMEOUT") from None
        if result.returncode or not output.exists():
            raise ProcessingError("PARSER_FAILED")
        if output.stat().st_size > settings.documents_max_text_chars * 12 + 2000000:
            raise ProcessingError("TEXT_LIMIT")
        extracted = ExtractionResult.model_validate_json(output.read_text(encoding="utf-8"))
        if len(extracted.text) > settings.documents_max_text_chars:
            raise ProcessingError("TEXT_LIMIT")
        if extracted.error_code:
            raise ProcessingError(extracted.error_code)
        return extracted


def fail_attempt(
    db: Session,
    job: DocumentIngestionJob,
    document: Document,
    code: str,
    settings: Settings,
    *,
    transient: bool,
) -> None:
    now = datetime.now(UTC)
    retry = transient and job.attempts < settings.documents_max_attempts
    job.status = "pending" if retry else "failed"
    job.error_code = code
    job.lease_token = job.lease_expires_at = None
    job.available_at = now + timedelta(seconds=min(300, 5 * (2**job.attempts)))
    job.updated_at = now
    document.status = "queued" if retry else "failed"
    document.error_code = None if retry else code
    document.updated_at = now


def process_job(job_id: UUID, engine: Engine, storage: ObjectStorage, settings: Settings) -> None:
    token = uuid4()
    with Session(engine, expire_on_commit=False) as db:
        job = db.scalar(
            select(DocumentIngestionJob).where(DocumentIngestionJob.id == job_id).with_for_update()
        )
        if job is None or job.status not in {"pending", "published"}:
            return
        # A duplicated message must not consume a scheduled backoff or unfinished upload.
        if job.available_at > datetime.now(UTC) and job.status == "pending":
            return
        document = db.get(Document, job.document_id)
        assert document is not None
        latest = db.scalar(
            select(func.max(DocumentIngestionJob.version)).where(
                DocumentIngestionJob.document_id == document.id
            )
        )
        if latest != job.version:
            return
        if job.status == "pending" and job.lease_token is None:
            # Dispatcher normally reserves the attempt before publication; a delayed
            # duplicate after backoff may also serve as delivery, within the same budget.
            if job.attempts >= settings.documents_max_attempts:
                fail_attempt(db, job, document, "RETRY_EXHAUSTED", settings, transient=False)
                db.commit()
                return
            job.attempts += 1
        job.status = "processing"
        job.lease_token = token
        job.lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.documents_processing_seconds + 120
        )
        job.updated_at = document.updated_at = datetime.now(UTC)
        was_uploading = document.status == "uploading"
        document.status, document.error_code = "processing", None
        db.commit()
        version, document_id = job.version, document.id
        db.expunge(document)
    extracted: ExtractionResult | None = None
    error_code, transient = "", False
    try:
        extracted = extract_original(document, storage, settings)
    except OriginalNotFound:
        error_code = "UPLOAD_INCOMPLETE" if was_uploading else "ORIGINAL_MISSING"
    except StorageError:
        error_code, transient = "STORAGE_UNAVAILABLE", True
    except ProcessingError as error:
        error_code = str(error)
    except Exception:
        error_code = "PROCESSING_FAILED"
    with Session(engine) as db:
        job = db.scalar(
            select(DocumentIngestionJob).where(DocumentIngestionJob.id == job_id).with_for_update()
        )
        if (
            job is None
            or job.status != "processing"
            or job.lease_token != token
            or job.lease_expires_at is None
            or job.lease_expires_at <= datetime.now(UTC)
        ):
            return  # An expired attempt cannot commit over a newer attempt.
        document = db.scalar(
            select(Document).where(Document.id == document_id).with_for_update(),
        )
        assert document is not None
        if error_code:
            fail_attempt(db, job, document, error_code, settings, transient=transient)
        else:
            assert extracted is not None
            library = "pymupdf" if document.format == "pdf" else "python-docx"
            text_version = DocumentTextVersion(
                document_id=document_id,
                version=version,
                normalized_text=extracted.text,
                locators=extracted.locators,
                extractor_version=f"{document.format}:{package_version(library)}:stage3-norm-v1",
            )
            db.add(text_version)
            if settings.rag_enabled and extracted.status == "ready":
                db.flush()
                enqueue(db, document, text_version)
            document.status = extracted.status
            document.error_code = "OCR_REQUIRED" if extracted.status == "needs_ocr" else None
            document.updated_at = datetime.now(UTC)
            job.status, job.error_code = "succeeded", None
            job.lease_token = job.lease_expires_at = None
            job.updated_at = datetime.now(UTC)
        db.commit()
