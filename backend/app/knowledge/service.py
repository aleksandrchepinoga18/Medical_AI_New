from datetime import UTC, datetime
from time import monotonic
from typing import Any, BinaryIO
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models import Document
from app.documents.processing import verified_file
from app.documents.providers import ProcessingError, extraction_provider
from app.documents.validation import inspect_upload
from app.integrations.object_storage import StorageError
from app.knowledge.models import KnowledgeAdmin, MedicalChunk, MedicalSource, MedicalVersion
from app.knowledge.schemas import (
    PubMedCitation,
    ReviewPublishRequest,
    SourceMetadata,
    SourceUpload,
    StatusChange,
    VersionView,
)
from app.knowledge.storage import KnowledgeStorage
from app.knowledge.vectors import KNOWLEDGE_PIPELINE, KnowledgeVectors
from app.rag.adapters import OllamaEmbeddings, RagError, embed_chunk_batches, fingerprint
from app.rag.chunking import chunk_text

TRANSITIONS = {
    "uploaded": {"verified", "rejected", "outdated"},
    "verified": {"published", "rejected", "outdated"},
    "published": {"rejected", "outdated"},
    "outdated": {"verified", "rejected"},
    "rejected": {"verified"},
}


def source_snapshot(source: MedicalSource, version: MedicalVersion | None) -> dict[str, Any]:
    result = {
        "id": source.id,
        **{name: getattr(source, name) for name in SourceMetadata.model_fields},
        "current_version": VersionView.model_validate(version) if version else None,
        "verification_status": (
            "rejected" if version and version.status == "rejected"
            else "verified" if version and version.reviewed_by else "pending"
        ),
        "freshness_status": "outdated" if version and version.status == "outdated" else "current",
    }
    if source.pubmed_citation is not None:
        result["pubmed_citation"] = source.pubmed_citation
    return result


def lock_source(db: Session, source_id: UUID) -> tuple[MedicalSource, MedicalVersion | None]:
    source = db.scalar(
        select(MedicalSource).where(MedicalSource.id == source_id).with_for_update(),
    )
    if source is None:
        raise HTTPException(404, "Medical source not found")
    version = db.scalar(select(MedicalVersion).where(
        MedicalVersion.source_id == source.id, MedicalVersion.is_current,
    ))
    return source, version


def source_and_version(
    db: Session, source_id: UUID, expected_version: UUID | None = None,
) -> tuple[MedicalSource, MedicalVersion]:
    source, version = lock_source(db, source_id)
    if version is None:
        raise HTTPException(409, "Medical source has no original")
    if expected_version is not None and version.id != expected_version:
        raise HTTPException(409, "Medical source version changed")
    return source, version


def metadata_values(data: SourceMetadata) -> dict[str, Any]:
    return {**data.model_dump(exclude={"version", "rights_confirmed"}), "url": str(data.url)}


def flush_identity(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) != "23505":
            raise
        db.rollback()
        raise HTTPException(409, "Medical source identity or version already exists") from None


def reject_personal_original(db: Session, sha256: str) -> None:
    if db.scalar(select(Document.id).where(Document.sha256 == sha256).limit(1)) is not None:
        raise HTTPException(409, "Original is ineligible for the medical corpus")


def save_pubmed_citation(db: Session, citation: PubMedCitation) -> dict[str, Any]:
    doi = citation.doi if citation.doi and len(citation.doi) <= 255 else None
    source = MedicalSource(
        title=citation.title[:500], material_type="PubMed",
        author=(", ".join(citation.authors) or "Не указаны")[:300],
        publisher=(citation.journal or "Не указан")[:300],
        url=citation.url, doi=doi, pmid=citation.pmid,
        publication_date=citation.publication_date,
        specialty="Не указано", language=citation.language or "und", jurisdiction="Не указана",
        pubmed_citation=citation.model_dump(
            mode="json", include=set(PubMedCitation.model_fields),
        ),
    )
    db.add(source)
    flush_identity(db)
    result = source_snapshot(source, None)
    db.commit()
    return result


def store_version(
    db: Session,
    storage: KnowledgeStorage,
    settings: Settings,
    uploader_id: UUID,
    data: SourceUpload,
    file: BinaryIO,
    filename: str,
    source_id: UUID | None = None,
) -> dict[str, Any]:
    values = metadata_values(data)
    upload = inspect_upload(file, filename, settings)
    reject_personal_original(db, upload.sha256)
    if source_id is None:
        source = MedicalSource(**values)
        db.add(source)
    else:
        source, old = lock_source(db, source_id)
        if source.pubmed_citation is not None and not data.rights_confirmed:
            raise HTTPException(422, "PubMed original requires rights confirmation")
        require_same_pubmed_identity(source, values)
        if old is not None:
            if old.version == data.version and old.sha256 == upload.sha256 and all(
                getattr(source, key) == value for key, value in values.items()
            ):
                return source_snapshot(source, old)
            old.is_current = False
            if old.status != "rejected":
                old.status = "outdated"
        for key, value in values.items():
            setattr(source, key, value)
    flush_identity(db)
    version = MedicalVersion(
        source_id=source.id, version=data.version, is_current=True, status="uploaded",
        uploaded_by=uploader_id, uploaded_at=datetime.now(UTC), format=upload.format,
        size_bytes=upload.size, sha256=upload.sha256,
    )
    db.add(version)
    flush_identity(db)
    try:
        original = storage.upload_version(source.id, version.id, file, upload.format)
        if original.sha256 != upload.sha256 or original.size_bytes != upload.size:
            raise StorageError("Original checksum mismatch")
    except StorageError:
        raise HTTPException(503, "Medical source storage unavailable") from None
    result = source_snapshot(source, version)
    db.commit()
    return result


def require_same_pubmed_identity(source: MedicalSource, values: dict[str, Any]) -> None:
    if source.pubmed_citation is not None and any(
        getattr(source, field) != values[field] for field in ("pmid", "doi", "url")
    ):
        raise HTTPException(422, "PubMed identifiers and official URL are fixed")


def update_metadata(
    db: Session, source_id: UUID, data: SourceMetadata,
) -> dict[str, Any]:
    source, version = lock_source(db, source_id)
    values = metadata_values(data)
    require_same_pubmed_identity(source, values)
    if any(getattr(source, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(source, key, value)
        if version:
            version.status = "uploaded"
            version.review_revision = uuid4()
            version.reviewed_by = version.reviewed_at = version.review_note = None
        flush_identity(db)
    result = source_snapshot(source, version)
    db.commit()
    return result


def change_status(
    db: Session, settings: Settings, source_id: UUID, reviewer_id: UUID, change: StatusChange,
) -> dict[str, Any]:
    source, version = source_and_version(db, source_id, change.version_id)
    apply_status(db, settings, version, reviewer_id, change)
    result = source_snapshot(source, version)
    db.commit()
    return result


def apply_status(
    db: Session, settings: Settings, version: MedicalVersion, reviewer_id: UUID,
    change: StatusChange,
) -> None:
    if change.status == "verified" and change.review_revision != version.review_revision:
        raise HTTPException(409, "Medical source review revision changed")
    if change.status == version.status:
        return
    if change.status not in TRANSITIONS[version.status]:
        raise HTTPException(409, "Invalid medical source status transition")
    if change.status == "verified":
        if change.verified_sha256 != version.sha256 or not change.review_note:
            raise HTTPException(422, "Verification requires the original checksum and review note")
        reject_personal_original(db, version.sha256)
        version.reviewed_by = reviewer_id
        version.reviewed_at = datetime.now(UTC)
        version.review_note = change.review_note
    elif change.status == "published":
        reject_personal_original(db, version.sha256)
        if version.generation is None or version.fingerprint != fingerprint(settings):
            raise HTTPException(409, "Medical source must be indexed with the current settings")
        if not index_complete(KnowledgeVectors(settings), version):
            raise HTTPException(409, "Medical source index is incomplete")
    elif change.status == "rejected":
        version.reviewed_by = version.reviewed_at = version.review_note = None
    if change.status in {"outdated", "rejected"}:
        version.review_revision = uuid4()
    version.status = change.status


def index_complete(vectors: KnowledgeVectors, version: MedicalVersion) -> bool:
    try:
        vectors.inspect()
        return vectors.count_scoped(vectors.scope([str(version.generation)])) == version.chunk_count
    except RagError as error:
        if error.code == "HTTP_404":
            return False
        raise


def index_source(
    db: Session,
    settings: Settings,
    storage: KnowledgeStorage,
    source_id: UUID,
    version_id: UUID,
) -> dict[str, Any]:
    source, version = source_and_version(db, source_id, version_id)
    write_index(db, settings, storage, version)
    result = source_snapshot(source, version)
    db.commit()
    return result


def write_index(
    db: Session, settings: Settings, storage: KnowledgeStorage, version: MedicalVersion,
) -> None:
    if version.status not in {"verified", "published"}:
        raise HTTPException(409, "Medical source must be verified before indexing")
    reject_personal_original(db, version.sha256)
    current_fingerprint = fingerprint(settings)
    vectors = KnowledgeVectors(settings)
    if version.fingerprint == current_fingerprint and index_complete(vectors, version):
        return

    processing_deadline = monotonic() + settings.documents_processing_seconds
    with verified_file(
        storage.open_version(version.source_id, version.id),
        version.size_bytes, version.sha256, settings.documents_max_bytes,
        processing_deadline,
    ) as original:
        extracted = extraction_provider("pymupdf_tables", settings).extract(
            original, version.format, processing_deadline,
        )
    if extracted.status != "ready" or not extracted.text.strip():
        raise ProcessingError("NO_INDEXABLE_TEXT")
    chunks = chunk_text(
        version.id, extracted.text, extracted.locators,
        settings.rag_chunk_chars, settings.rag_chunk_overlap,
    )
    embeddings = OllamaEmbeddings(settings)
    generation = uuid4()
    deadline = monotonic() + settings.rag_index_seconds

    def check_deadline() -> None:
        if monotonic() >= deadline:
            raise RagError("INDEX_TIMEOUT")

    batches = embed_chunk_batches(
        embeddings, chunks, settings.rag_embedding_batch_size, check_deadline,
    )
    for batch_number, (batch, values) in enumerate(batches):
        if batch_number == 0:
            vectors.ensure()
            vectors.remove_old_generations(version.source_id, version.generation)
        check_deadline()
        vectors.write_batch(version.source_id, version.id, generation, batch, values)
    check_deadline()
    if vectors.count_scoped(vectors.scope([str(generation)])) != len(chunks):
        raise RagError("HYBRID_COVERAGE_INCOMPLETE")

    # The source row stays locked until this generation and its chunks commit together.
    db.execute(delete(MedicalChunk).where(MedicalChunk.version_id == version.id))
    db.add_all([
        MedicalChunk(
            id=chunk.id, version_id=version.id, text=chunk.text,
            start_char=chunk.start, end_char=chunk.end, locators=chunk.sources,
        )
        for chunk in chunks
    ])
    version.generation = generation
    version.fingerprint = current_fingerprint
    version.chunk_count = len(chunks)


def review_and_publish(
    db: Session, settings: Settings, storage: KnowledgeStorage, source_id: UUID,
    reviewer_id: UUID, review: ReviewPublishRequest,
) -> dict[str, Any]:
    source, version = source_and_version(db, source_id, review.version_id)
    if version.review_revision != review.review_revision:
        raise HTTPException(409, "Medical source review revision changed")
    if version.sha256 != review.verified_sha256:
        raise HTTPException(422, "Medical source checksum changed")
    if version.status == "published":
        return source_snapshot(source, version)
    if version.status != "verified":
        apply_status(db, settings, version, reviewer_id, StatusChange(
            version_id=review.version_id, status="verified", review_revision=review.review_revision,
            verified_sha256=review.verified_sha256,
            review_note="Administrator reviewed the original and confirmed full-text reuse rights.",
        ))
        db.commit()  # Keep the review if indexing fails. Recheck it under the next source lock.
        source, version = source_and_version(db, source_id, review.version_id)
        if version.review_revision != review.review_revision:
            raise HTTPException(409, "Medical source review revision changed")
        if version.status == "published":
            return source_snapshot(source, version)
    write_index(db, settings, storage, version)
    if db.scalar(
        select(KnowledgeAdmin.user_id).where(KnowledgeAdmin.user_id == reviewer_id)
    ) is None:
        raise HTTPException(403, "Medical knowledge administrator required")
    apply_status(
        db, settings, version, reviewer_id,
        StatusChange(version_id=review.version_id, status="published"),
    )
    result = source_snapshot(source, version)
    db.commit()
    return result


def eligible(settings: Settings) -> ColumnElement[bool]:
    return and_(
        MedicalVersion.is_current,
        MedicalVersion.status == "published",
        MedicalVersion.reviewed_by.is_not(None),
        MedicalVersion.generation.is_not(None),
        MedicalVersion.fingerprint == fingerprint(settings),
    )


def search(
    db: Session, settings: Settings, query: str, k: int,
) -> list[dict[str, Any]]:
    conditions = eligible(settings)
    published = db.execute(
        select(MedicalVersion.generation, MedicalVersion.chunk_count).where(conditions),
    ).all()
    if not published:
        return []
    generations = [str(generation) for generation, _ in published]
    vectors = KnowledgeVectors(settings)
    points = vectors.query_scoped(
        OllamaEmbeddings(settings).embed([query])[0], query, vectors.scope(generations),
        sum(count for _, count in published), k,
    )
    try:
        chunk_ids = [UUID(point["payload"]["chunk_id"]) for point in points]
    except (KeyError, TypeError, AttributeError, ValueError):
        raise RagError("INVALID_HYBRID_RESPONSE") from None

    # Recheck eligibility after the network call so a concurrent unpublish or version change
    # cannot revive the previous snapshot. Read all source metadata and text from PostgreSQL.
    rows = db.execute(
        select(MedicalChunk, MedicalVersion, MedicalSource)
        .join(MedicalVersion, MedicalVersion.id == MedicalChunk.version_id)
        .join(MedicalSource, MedicalSource.id == MedicalVersion.source_id)
        .where(conditions, MedicalChunk.id.in_(chunk_ids)),
    )
    by_id = {chunk.id: (chunk, version, source) for chunk, version, source in rows}
    items = []
    seen: set[UUID] = set()
    for chunk_id, point in zip(chunk_ids, points, strict=True):
        if chunk_id not in by_id or chunk_id in seen:
            continue
        chunk, version, source = by_id[chunk_id]
        assert version.generation is not None
        payload = point["payload"]
        if (
            point["id"] != str(uuid5(chunk.id, str(version.generation)))
            or payload.get("generation") != str(version.generation)
            or payload.get("source_id") != str(source.id)
            or payload.get("version_id") != str(version.id)
            or payload.get("pipeline") != KNOWLEDGE_PIPELINE
            or payload.get("fingerprint") != version.fingerprint
        ):
            continue
        seen.add(chunk_id)
        items.append({
            "chunk_id": chunk.id, "text": chunk.text, "locators": chunk.locators,
            "start_char": chunk.start_char, "end_char": chunk.end_char, "score": point["score"],
            "source": {
                "id": source.id, "title": source.title, "url": source.url,
                "doi": source.doi, "pmid": source.pmid, "publisher": source.publisher,
                "version": version.version, "version_id": version.id,
                "publication_date": source.publication_date, "update_date": source.update_date,
            },
        })
    return items
