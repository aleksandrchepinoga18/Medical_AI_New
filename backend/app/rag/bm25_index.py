"""Copy published dense vectors and index their committed chunks in Qdrant."""

import logging
import math
from uuid import UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document, DocumentTextVersion
from app.rag.adapters import PIPELINE, QdrantVectors, RagError, fingerprint
from app.rag.chunking import chunk_text
from app.rag.hybrid import QdrantHybrid, bm25
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import eligible

logger = logging.getLogger(__name__)


def sync_generation(
    db: Session, job: RagIndexJob, source: QdrantVectors, index: QdrantHybrid
) -> int:
    """Caller locks Document before selecting the current job and keeps it locked."""
    generation = job.generation
    if generation is None:
        raise RagError("HYBRID_GENERATION_MISSING")
    version = db.get(DocumentTextVersion, job.text_version_id)
    if version is None:
        raise RagError("HYBRID_TEXT_VERSION_MISSING")
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
    if not chunk_ids or len(chunk_ids) != job.chunk_count:
        raise RagError("HYBRID_CHUNK_COUNT_MISMATCH")
    scope = {
        "pipeline": PIPELINE,
        "fingerprint": fingerprint(settings),
        "generation": str(generation),
        "user_id": str(job.user_id),
        "document_id": str(job.document_id),
        "text_version_id": str(job.text_version_id),
    }
    query = select(RagChunk).where(
        RagChunk.user_id == job.user_id,
        RagChunk.document_id == job.document_id,
        RagChunk.text_version_id == job.text_version_id,
    )
    for offset in range(0, len(chunk_ids), 64):
        batch_ids = chunk_ids[offset : offset + 64]
        chunks = list(db.scalars(query.where(RagChunk.id.in_(batch_ids))))
        if len(chunks) != len(batch_ids):
            raise RagError("HYBRID_CHUNK_COUNT_MISMATCH")
        by_id = {str(uuid5(chunk.id, str(generation))): chunk for chunk in chunks}
        result = source.http.request(
            "POST",
            source.path + "/points",
            {
                "ids": list(by_id),
                "with_vector": True,
                "with_payload": [*scope, "chunk_id"],
            },
        )
        try:
            stored = {point["id"]: point for point in result["result"]}
        except (KeyError, TypeError):
            raise RagError("INVALID_DENSE_COPY_RESPONSE") from None
        if stored.keys() != by_id.keys():
            raise RagError("HYBRID_DENSE_POINTS_MISSING")
        points = []
        for point_id, chunk in by_id.items():
            point = stored[point_id]
            payload = {**scope, "chunk_id": str(chunk.id)}
            vector = point.get("vector")
            if (
                point.get("payload") != payload
                or not isinstance(vector, list)
                or len(vector) != settings.rag_embedding_dimensions
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in vector
                )
                or not any(vector)
            ):
                raise RagError("HYBRID_DENSE_POINT_MISMATCH")
            points.append(
                {
                    "id": point_id,
                    "payload": payload,
                    "vector": {"dense": vector, "bm25": bm25(chunk.text)},
                }
            )
        index.http.request("PUT", index.path + "/points?wait=true", {"points": points})
    if index.count(job.user_id, [str(generation)]) != len(chunk_ids):
        raise RagError("HYBRID_COVERAGE_INCOMPLETE")
    index.delete_document(job.user_id, job.document_id, keep_generation=generation)
    return len(chunk_ids)


def sync_published(
    engine: Engine,
    settings: Settings,
    vectors: QdrantVectors,
    user_id: UUID,
    document_id: UUID,
    generation: UUID,
) -> None:
    index = QdrantHybrid(vectors)
    try:
        try:
            index.inspect()
        except RagError as error:
            if error.code == "HTTP_404":
                return  # Only the explicit backfill creates the collection.
            raise
        with Session(engine) as db:
            document = db.scalar(
                select(Document)
                .where(Document.id == document_id, Document.owner_id == user_id)
                .with_for_update()
            )
            if document is None:
                return
            job = db.scalar(
                eligible(user_id, settings).where(
                    RagIndexJob.document_id == document_id,
                    RagIndexJob.generation == generation,
                )
            )
            if job is not None:
                sync_generation(db, job, vectors, index)
    except Exception as error:
        # Dense has been published. Backfill can repair a partial or failed hybrid write.
        code = error.code if isinstance(error, RagError) else "HYBRID_INDEX_UNAVAILABLE"
        logger.warning("rag_hybrid_index_failed code=%s", code)


def delete_copies(source: QdrantVectors, user_id: UUID, document_id: UUID) -> None:
    # Retained legacy points must also respect explicit user deletion.
    legacy = QdrantVectors(source.settings)
    legacy.path = source.path + "_bm25_v1"
    for target in (QdrantHybrid(source), legacy):
        try:
            target.delete_document(user_id, document_id)
        except RagError as error:
            if error.code != "HTTP_404":
                raise


def backfill(engine: Engine, settings: Settings, source: QdrantVectors) -> tuple[int, int]:
    if source.http.request("GET", "/").get("version") != "1.15.5":
        raise RagError("HYBRID_QDRANT_VERSION_MISMATCH")
    source.inspect()
    index = QdrantHybrid(source)
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
                    chunks += sync_generation(db, job, source, index)
                    documents += 1
        last_id = ids[-1]
