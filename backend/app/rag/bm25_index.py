"""Populate the companion index from committed chunks without re-embedding."""

import logging
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document, DocumentTextVersion
from app.rag.adapters import QdrantVectors, RagError
from app.rag.chunking import chunk_text
from app.rag.hybrid import QdrantBm25
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import eligible, latest_version

logger = logging.getLogger(__name__)


def sync_generation(db: Session, job: RagIndexJob, index: QdrantBm25) -> int:
    """Caller locks Document before selecting the current job and keeps it locked."""
    generation = job.generation
    if generation is None:
        raise RagError("BM25_GENERATION_MISSING")
    version = db.get(DocumentTextVersion, job.text_version_id)
    if version is None:
        raise RagError("BM25_TEXT_VERSION_MISSING")
    # A text version can also contain stored chunks from earlier chunking settings.
    # Derive the published layout with the same settings as the eligible dense job.
    settings = index.settings
    chunk_ids = [
        chunk.id
        for chunk in chunk_text(
            version.id,
            version.normalized_text,
            version.locators,
            settings.rag_chunk_chars,
            settings.rag_chunk_overlap,
        )
    ]
    if len(chunk_ids) != job.chunk_count:
        raise RagError("BM25_CHUNK_COUNT_MISMATCH")
    query = select(RagChunk).where(
        RagChunk.user_id == job.user_id,
        RagChunk.document_id == job.document_id,
        RagChunk.text_version_id == job.text_version_id,
    )
    for offset in range(0, len(chunk_ids), 64):
        batch_ids = chunk_ids[offset : offset + 64]
        chunks = list(db.scalars(query.where(RagChunk.id.in_(batch_ids))))
        if len(chunks) != len(batch_ids):
            raise RagError("BM25_CHUNK_COUNT_MISMATCH")
        index.upsert(chunks, generation)
    return len(chunk_ids)


def sync_published(
    engine: Engine,
    settings: Settings,
    vectors: QdrantVectors,
    user_id: UUID,
    document_id: UUID,
    version_id: UUID,
    operation: str,
    generation: UUID,
) -> None:
    index = QdrantBm25(vectors)
    try:
        with Session(engine) as db:
            document = db.scalar(
                select(Document)
                .where(Document.id == document_id, Document.owner_id == user_id)
                .with_for_update()
            )
            if document is None:
                return
            if operation == "delete":
                latest = latest_version(db, document_id)
                job = db.scalar(
                    select(RagIndexJob).where(
                        RagIndexJob.document_id == document_id,
                        RagIndexJob.user_id == user_id,
                        RagIndexJob.text_version_id == version_id,
                        RagIndexJob.status == "deleted",
                        RagIndexJob.operation == "delete",
                    )
                )
                if latest is not None and latest.id == version_id and job is not None:
                    index.cleanup(user_id, document_id, None)
                return
            job = db.scalar(
                eligible(user_id, settings).where(
                    RagIndexJob.document_id == document_id,
                    RagIndexJob.generation == generation,
                )
            )
            if job is not None:
                index.ensure()
                sync_generation(db, job, index)
                index.cleanup(user_id, document_id, generation)
    except Exception as error:
        # Dense has been published. Backfill can repair a partial or failed sparse write.
        code = error.code if isinstance(error, RagError) else "BM25_INDEX_UNAVAILABLE"
        logger.warning("rag_bm25_index_failed code=%s", code)


def backfill(engine: Engine, settings: Settings, index: QdrantBm25) -> tuple[int, int]:
    index.ensure()
    last_id: UUID | None = None
    documents = chunks = 0
    while True:
        with Session(engine) as db:
            query = select(Document.id).where(Document.status == "ready")
            if last_id is not None:
                query = query.where(Document.id > last_id)
            ids = list(db.scalars(query.order_by(Document.id).limit(256)))
        if not ids:
            return documents, chunks
        for document_id in ids:
            with Session(engine) as db:
                document = db.get(Document, document_id, with_for_update=True)
                if document is None:
                    continue
                job = db.scalar(
                    eligible(document.owner_id, settings).where(
                        RagIndexJob.document_id == document_id
                    )
                )
                if job is not None:
                    chunks += sync_generation(db, job, index)
                    documents += 1
        last_id = ids[-1]
