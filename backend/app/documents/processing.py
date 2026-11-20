import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.extraction_data import Evidence, StructuredExtraction
from app.documents.extraction_data import ExtractionResult as ExtractionResult
from app.documents.models import Document, DocumentIngestionJob, DocumentTextVersion
from app.documents.providers import ProcessingError as ProcessingError
from app.documents.providers import confirm_sources, extraction_provider
from app.documents.schemas import VERSION_REQUEST, ExternalAuthorization, VersionRequest
from app.integrations.object_storage import ObjectStorage, OriginalNotFound, StorageError
from app.rag.service import enqueue, latest_version


@contextmanager
def verified_original(
    document: Document, storage: ObjectStorage, settings: Settings, deadline: float,
) -> Iterator[Path]:
    with TemporaryDirectory(prefix="medical-ingestion-") as directory:
        source = Path(directory) / "original"
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
        yield source


def extract_original(
    document: Document, storage: ObjectStorage, settings: Settings
) -> ExtractionResult:
    deadline = monotonic() + settings.documents_processing_seconds
    with verified_original(document, storage, settings, deadline) as source:
        return extraction_provider("pymupdf_tables", settings).extract(
            source, document.format, deadline,
        )


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
    if job.external_request is not None:
        return
    document.status = "queued" if retry else "failed"
    document.error_code = None if retry else code
    document.updated_at = now


def authorized_version(
    db: Session, document: Document, authorization: VersionRequest,
) -> DocumentTextVersion | None:
    if (
        (isinstance(authorization, ExternalAuthorization) and not authorization.consent)
        or document.owner_id != authorization.owner_id
        or document.sha256 != authorization.sha256
        or document.status not in {"ready", "needs_ocr"}
    ):
        return None
    version = latest_version(db, document.id)
    if version is None or version.version != authorization.text_version:
        return None
    return version


def process_job(job_id: UUID, engine: Engine, storage: ObjectStorage, settings: Settings) -> None:
    token = uuid4()
    authorization: VersionRequest | None = None
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
        if job.external_request is not None:
            try:
                authorization = VERSION_REQUEST.validate_python(job.external_request)
            except ValidationError:
                authorization = None
            if authorization is None or authorized_version(db, document, authorization) is None:
                fail_attempt(
                    db, job, document, "EXTERNAL_NOT_AUTHORIZED", settings, transient=False,
                )
                db.commit()
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
        job.updated_at = datetime.now(UTC)
        was_uploading = document.status == "uploading"
        if authorization is None:
            document.updated_at = job.updated_at
            document.status, document.error_code = "processing", None
        db.commit()
        version, document_id = job.version, document.id
        db.expunge(document)
    extracted: ExtractionResult | None = None
    error_code, transient = "", False
    pending_file_id = None
    try:
        if not isinstance(authorization, ExternalAuthorization):
            extracted = extract_original(document, storage, settings)
        else:
            provider = extraction_provider(
                authorization.provider, settings,
                authorization=authorization, upload_name=f"document-extraction-{job_id.hex}.pdf",
            )
            deadline = monotonic() + settings.documents_processing_seconds
            with verified_original(document, storage, settings, deadline) as source:
                extracted = provider.extract(source, document.format, deadline)
    except OriginalNotFound:
        error_code = "UPLOAD_INCOMPLETE" if was_uploading else "ORIGINAL_MISSING"
    except StorageError:
        error_code, transient = "STORAGE_UNAVAILABLE", not isinstance(
            authorization, ExternalAuthorization,
        )
    except ProcessingError as error:
        error_code = str(error)
        pending_file_id = error.pending_file_id
    except Exception:
        error_code = (
            "EXTERNAL_INTERRUPTED" if isinstance(authorization, ExternalAuthorization)
            else "PROCESSING_FAILED"
        )
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
            if pending_file_id is not None and job.external_request is not None:
                job.external_request = {**job.external_request, "pending_file_id": pending_file_id}
            fail_attempt(db, job, document, error_code, settings, transient=transient)
            db.commit()
            return
        assert extracted is not None
        existing = None
        if authorization is not None:
            existing = authorized_version(db, document, authorization)
            if existing is None:
                fail_attempt(
                    db, job, document, "EXTERNAL_NOT_AUTHORIZED", settings, transient=False,
                )
                db.commit()
                return
        if isinstance(authorization, ExternalAuthorization):
            assert existing is not None
            assert extracted.structured is not None and extracted.structured.external is not None
            structured = extracted.structured.model_dump(mode="json")
            if existing.structured_result is not None:
                structured = {**existing.structured_result, "external": structured["external"]}
            existing.structured_result = structured
        elif existing is not None and (
            existing.normalized_text == extracted.text
            and existing.locators == extracted.locators
        ):
            assert extracted.structured is not None
            if existing.structured_result is not None:
                previous = StructuredExtraction.model_validate(existing.structured_result)
                if previous.external is not None:
                    rows = [
                        Evidence(
                            name=row.name, value=row.value, unit=row.unit, reference=row.reference,
                            date=row.date, page=row.reported_page, quote=row.reconstructed_row,
                        )
                        for row in previous.external.measurements
                    ]
                    previous.external.measurements = confirm_sources(
                        rows, extracted.table_rows, truncated=extracted.tables_truncated,
                    )
                    extracted.structured.external = previous.external
            existing.structured_result = extracted.structured.model_dump(mode="json")
            if document.status != extracted.status:
                if settings.rag_enabled and extracted.status == "ready":
                    enqueue(db, document, existing)
                document.status = extracted.status
                document.error_code = "OCR_REQUIRED" if extracted.status == "needs_ocr" else None
                document.updated_at = datetime.now(UTC)
        else:
            library = "pymupdf" if document.format == "pdf" else "python-docx"
            text_version = DocumentTextVersion(
                document_id=document_id,
                version=version,
                normalized_text=extracted.text,
                locators=extracted.locators,
                extractor_version=f"{document.format}:{package_version(library)}:stage3-norm-v1",
                structured_result=(
                    extracted.structured.model_dump(mode="json") if extracted.structured else None
                ),
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
