"""Durable leased indexing with generation fencing across PostgreSQL and Qdrant."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import Engine, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document, DocumentTextVersion
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError, fingerprint
from app.rag.chunking import chunk_text
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import latest_version


def retry_or_fail(job: RagIndexJob, settings: Settings, code: str) -> None:
    permanent = code in {
        "NO_TEXT",
        "EMBEDDING_DIMENSION_MISMATCH",
        "EMBEDDING_MODEL_CHANGED",
        "COLLECTION_DIMENSION_MISMATCH",
        "PAYLOAD_INDEX_MISMATCH",
        "LOCAL_MODEL_REQUIRED",
        "LOCAL_ENDPOINT_REQUIRED",
        "INVALID_COLLECTION",
    }
    job.status = "failed" if permanent or job.attempts >= settings.rag_max_attempts else "pending"
    job.error_code = code
    job.lease_token = job.lease_expires_at = None
    job.updated_at = datetime.now(UTC)
    job.available_at = job.updated_at + timedelta(seconds=min(300, 5 * 2**job.attempts))


def dispatch_index_once(
    engine: Engine,
    publish: Callable[[UUID, UUID], None],
    settings: Settings,
) -> bool:
    now = datetime.now(UTC)
    with Session(engine) as db:
        job = db.scalar(
            select(RagIndexJob)
            .where(
                RagIndexJob.status.in_(("pending", "published", "processing")),
                RagIndexJob.available_at <= now,
                or_(RagIndexJob.lease_expires_at.is_(None), RagIndexJob.lease_expires_at <= now),
            )
            .order_by(RagIndexJob.available_at, RagIndexJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False
        if job.attempts >= settings.rag_max_attempts:
            retry_or_fail(job, settings, "INDEX_RETRY_EXHAUSTED")
            db.commit()
            return True
        token = uuid4()
        job.attempts += 1
        job.status, job.lease_token = "published", token
        job.lease_expires_at = now + timedelta(seconds=settings.rag_index_seconds + 120)
        job.updated_at = now
        job_id = job.id
        db.commit()  # Reserve before publication; lost messages are recovered after expiry.
    try:
        publish(job_id, token)
    except Exception:
        with Session(engine) as db:
            job = db.scalar(
                select(RagIndexJob)
                .where(
                    RagIndexJob.id == job_id,
                    RagIndexJob.lease_token == token,
                    RagIndexJob.status == "published",
                )
                .with_for_update()
            )
            if job is not None:
                retry_or_fail(job, settings, "INDEX_QUEUE_UNAVAILABLE")
                db.commit()
    return True


def process_index(
    job_id: UUID,
    token: UUID,
    engine: Engine,
    settings: Settings,
    embeddings: OllamaEmbeddings,
    vectors: QdrantVectors,
) -> None:
    # A session advisory lock prevents two live processes indexing the same version,
    # even if a delivery lease expires. Process/connection death releases it in PostgreSQL.
    key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        if not acquired:
            return
        try:
            _process_index(job_id, token, engine, settings, embeddings, vectors)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


def _process_index(
    job_id: UUID,
    token: UUID,
    engine: Engine,
    settings: Settings,
    embeddings: OllamaEmbeddings,
    vectors: QdrantVectors,
) -> None:
    with Session(engine) as db:
        job = db.scalar(select(RagIndexJob).where(RagIndexJob.id == job_id).with_for_update())
        if (
            job is None
            or job.status != "published"
            or job.lease_token != token
            or job.lease_expires_at is None
            or job.lease_expires_at <= datetime.now(UTC)
        ):
            return
        job.status = "processing"
        version = db.get(DocumentTextVersion, job.text_version_id)
        assert version is not None
        text, locators = version.normalized_text, version.locators
        user_id, document_id, version_id, operation = (
            job.user_id,
            job.document_id,
            job.text_version_id,
            job.operation,
        )
        db.commit()
    deadline = monotonic() + settings.rag_index_seconds

    def check_deadline() -> None:
        if monotonic() >= deadline:
            raise RagError("INDEX_TIMEOUT")

    try:
        chunks = (
            []
            if operation == "delete"
            else chunk_text(
                version_id,
                text,
                locators,
                settings.rag_chunk_chars,
                settings.rag_chunk_overlap,
            )
        )
        if operation == "index" and not chunks:
            raise RagError("NO_TEXT")
        if operation == "index":
            batch_size = settings.rag_embedding_batch_size
            for offset in range(0, len(chunks), batch_size):
                check_deadline()
                batch = chunks[offset : offset + batch_size]
                values = embeddings.embed([chunk.text for chunk in batch])
                if offset == 0:
                    vectors.ensure()  # Real embedding dimensions validated before creation.
                check_deadline()
                vectors.upsert(user_id, document_id, version_id, token, batch, values)
                with Session(engine) as db:
                    for chunk in batch:
                        db.execute(
                            insert(RagChunk)
                            .values(
                                id=chunk.id,
                                user_id=user_id,
                                document_id=document_id,
                                text_version_id=version_id,
                                ordinal=chunk.ordinal,
                                start_char=chunk.start,
                                end_char=chunk.end,
                                text=chunk.text,
                                sources=chunk.sources,
                            )
                            .on_conflict_do_nothing(index_elements=["id"])
                        )
                    db.commit()
        # Lock document before job, matching API lock order. All document-wide vector
        # cleanup is serialized here; stale attempts never publish or delete newer data.
        with Session(engine) as db:
            document = db.scalar(
                select(Document)
                .where(
                    Document.id == document_id,
                    Document.owner_id == user_id,
                )
                .with_for_update()
            )
            job = db.scalar(select(RagIndexJob).where(RagIndexJob.id == job_id).with_for_update())
            assert job is not None
            if (
                job.lease_token != token
                or job.status != "processing"
                or job.lease_expires_at is None
                or job.lease_expires_at <= datetime.now(UTC)
            ):
                return
            latest = latest_version(db, document_id)
            if (
                document is None
                or latest is None
                or latest.id != version_id
                or (operation == "index" and document.status != "ready")
            ):
                job.status, job.error_code = "failed", "INDEX_SUPERSEDED"
                job.lease_token = job.lease_expires_at = None
                db.commit()
                return
            check_deadline()
            try:
                vectors.delete_document(
                    user_id,
                    document_id,
                    keep_generation=token if operation == "index" else None,
                )
            except RagError as error:
                if operation != "delete" or error.code != "HTTP_404":
                    raise
            check_deadline()
            job.status = "ready" if operation == "index" else "deleted"
            job.generation = token if operation == "index" else None
            job.fingerprint = fingerprint(settings)
            job.chunk_count = len(chunks)
            job.error_code = None
            job.lease_token = job.lease_expires_at = None
            job.updated_at = datetime.now(UTC)
            db.commit()
    except Exception as error:
        code = error.code if isinstance(error, RagError) else "INDEX_UNAVAILABLE"
        with Session(engine) as db:
            job = db.scalar(
                select(RagIndexJob)
                .where(
                    RagIndexJob.id == job_id,
                    RagIndexJob.lease_token == token,
                )
                .with_for_update()
            )
            if job is not None:
                retry_or_fail(job, settings, code)
                db.commit()


def process_index_task(job_id: str, token: str) -> None:
    from app.db import get_engine, get_settings

    settings = get_settings()
    if settings.rag_enabled:
        process_index(
            UUID(job_id),
            UUID(token),
            get_engine(),
            settings,
            OllamaEmbeddings(settings),
            QdrantVectors(settings),
        )
