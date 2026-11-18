import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document, DocumentTextVersion
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError, fingerprint
from app.rag.hybrid import QdrantBm25, rrf
from app.rag.models import RagChunk, RagIndexJob

logger = logging.getLogger(__name__)


def enqueue(db: Session, document: Document, version: DocumentTextVersion) -> None:
    db.execute(
        insert(RagIndexJob)
        .values(
            user_id=document.owner_id,
            document_id=document.id,
            text_version_id=version.id,
        )
        .on_conflict_do_nothing(index_elements=["text_version_id"])
    )


def latest_version(db: Session, document_id: UUID) -> DocumentTextVersion | None:
    return db.scalar(
        select(DocumentTextVersion)
        .where(
            DocumentTextVersion.document_id == document_id,
        )
        .order_by(DocumentTextVersion.version.desc())
        .limit(1)
    )


def eligible(user_id: UUID, settings: Settings) -> Select[tuple[RagIndexJob]]:
    latest = (
        select(func.max(DocumentTextVersion.version))
        .where(
            DocumentTextVersion.document_id == Document.id,
        )
        .correlate(Document)
        .scalar_subquery()
    )
    return (
        select(RagIndexJob)
        .join(
            Document,
            Document.id == RagIndexJob.document_id,
        )
        .join(DocumentTextVersion, DocumentTextVersion.id == RagIndexJob.text_version_id)
        .where(
            Document.owner_id == user_id,
            RagIndexJob.user_id == user_id,
            Document.status == "ready",
            DocumentTextVersion.document_id == Document.id,
            DocumentTextVersion.version == latest,
            RagIndexJob.status == "ready",
            RagIndexJob.operation == "index",
            RagIndexJob.generation.is_not(None),
            RagIndexJob.fingerprint == fingerprint(settings),
        )
    )


def request_operation(db: Session, job: RagIndexJob, operation: str) -> None:
    job.status, job.operation = "pending", operation
    job.attempts, job.error_code, job.generation = 0, None, None
    job.lease_token = job.lease_expires_at = None
    job.available_at = job.updated_at = datetime.now(UTC)
    db.commit()


def search(
    db: Session,
    user_id: UUID,
    query: str,
    k: int,
    settings: Settings,
    embeddings: OllamaEmbeddings,
    vectors: QdrantVectors,
    *,
    require_hybrid: bool = False,
) -> list[dict[str, Any]]:
    jobs = list(db.scalars(eligible(user_id, settings)))
    if not jobs:
        return []
    documents_by_generation = {str(job.generation): job.document_id for job in jobs}
    generations = list(documents_by_generation)
    expected = sum(job.chunk_count for job in jobs)
    # End the session snapshot before the remote call; revalidate all hits afterwards.
    db.commit()
    lexical = None
    if settings.rag_retrieval_mode == "hybrid":
        with ThreadPoolExecutor(max_workers=1) as executor:
            bm25 = executor.submit(
                QdrantBm25(vectors).query, user_id, query, generations, expected, k
            )
            dense = vectors.query(user_id, embeddings.embed([query])[0], generations, k)
            try:
                lexical = bm25.result()
            except (RagError, KeyError, TypeError, AttributeError) as error:
                code = error.code if isinstance(error, RagError) else "INVALID_BM25_RESPONSE"
                if require_hybrid:
                    raise RagError(code) from None
                if code != "BM25_NO_TERMS":
                    logger.warning("rag_bm25_fallback code=%s", code)
    else:
        dense = vectors.query(user_id, embeddings.embed([query])[0], generations, k)
    db.expire_all()
    candidate_generations = {
        str(hit.get("payload", {}).get("generation")) for hit in (dense + (lexical or []))
    } & set(generations)
    if not candidate_generations:
        return []
    # Extraction, reindex, deletion and publication all lock Document first. Share
    # locks keep this final authorization/version snapshot stable while materializing hits.
    db.execute(
        select(Document.id)
        .where(
            Document.id.in_([documents_by_generation[value] for value in candidate_generations]),
            Document.owner_id == user_id,
        )
        .order_by(Document.id)
        .with_for_update(read=True)
    )
    # Read eligibility after taking the locks, including changes committed while waiting.
    current_query = eligible(user_id, settings).where(
        RagIndexJob.generation.in_([UUID(value) for value in candidate_generations]),
    )
    current = {str(job.generation): job for job in db.scalars(current_query)}
    dense_items = materialize_hits(db, user_id, dense, current)
    if lexical is None:
        return dense_items
    return rrf(dense_items, materialize_hits(db, user_id, lexical, current), k=k)


def materialize_hits(
    db: Session,
    user_id: UUID,
    hits: list[dict[str, Any]],
    current: dict[str, RagIndexJob],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for hit in hits:
        payload = hit.get("payload", {})
        job = current.get(str(payload.get("generation")))
        if job is None or payload.get("user_id") != str(user_id):
            continue
        try:
            chunk_id = UUID(str(payload["chunk_id"]))
            point_id = UUID(str(hit["id"]))
            score = float(hit["score"])
        except (KeyError, ValueError, TypeError):
            continue
        if (
            not math.isfinite(score)
            or point_id != uuid5(chunk_id, str(job.generation))
            or chunk_id in seen
        ):
            continue
        chunk = db.get(RagChunk, chunk_id)
        if (
            chunk is None
            or chunk.user_id != user_id
            or chunk.document_id != job.document_id
            or chunk.text_version_id != job.text_version_id
            or payload.get("document_id") != str(chunk.document_id)
            or payload.get("text_version_id") != str(chunk.text_version_id)
        ):
            continue
        seen.add(chunk_id)
        document = db.get(Document, chunk.document_id)
        assert document is not None
        anchor = "extracted-text"
        if chunk.sources:
            source = chunk.sources[0]
            anchor = f"source-{source['kind']}-{source['index']}"
        output.append(
            {
                "chunk_id": str(chunk.id),
                "document_id": str(chunk.document_id),
                "text_version_id": str(chunk.text_version_id),
                "ordinal": chunk.ordinal,
                "text": chunk.text,
                "score": score,
                "sources": [
                    {
                        **source,
                        "url": (
                            f"/documents/{chunk.document_id}"
                            f"#source-{source['kind']}-{source['index']}"
                        ),
                    }
                    for source in chunk.sources
                ],
                "filename": document.original_filename,
                "source_url": f"/documents/{chunk.document_id}#{anchor}",
            }
        )
    return output
