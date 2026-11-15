from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.routes import DB, AuthenticatedUser
from app.db import get_settings
from app.documents.models import Document
from app.documents.routes import require_session_cookie
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError, fingerprint
from app.rag.models import RagIndexJob
from app.rag.service import enqueue, latest_version, request_operation, search

router = APIRouter(
    prefix="/rag",
    tags=["personal-search"],
    dependencies=[Depends(require_session_cookie)],
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=50)


def enabled() -> None:
    if not get_settings().rag_enabled:
        raise HTTPException(503, "Personal search is disabled")


@router.post("/search")
def semantic_search(
    data: SearchRequest,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    enabled()
    settings = get_settings()
    if not data.query.strip() or data.k > settings.rag_search_max_k:
        raise HTTPException(422, "Invalid search request")
    response.headers["Cache-Control"] = "no-store"
    try:
        items = search(
            db,
            user.id,
            data.query.strip(),
            data.k,
            settings,
            OllamaEmbeddings(settings),
            QdrantVectors(settings),
        )
    except RagError:
        raise HTTPException(503, "Personal search is temporarily unavailable") from None
    return {"items": items, "k": data.k}


def document_job(
    db: DB,
    user_id: UUID,
    document_id: UUID,
    *,
    lock: bool = False,
) -> tuple[Document, RagIndexJob | None]:
    query = select(Document).where(Document.id == document_id, Document.owner_id == user_id)
    document = db.scalar(query.with_for_update() if lock else query)
    if document is None:
        raise HTTPException(404, "Document not found")
    version = latest_version(db, document_id)
    if version is None:
        return document, None
    query_job = select(RagIndexJob).where(RagIndexJob.text_version_id == version.id)
    return document, db.scalar(query_job.with_for_update() if lock else query_job)


@router.get("/documents/{document_id}/index")
def index_status(
    document_id: UUID,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    enabled()
    document, job = document_job(db, user.id, document_id)
    response.headers["Cache-Control"] = "no-store"
    if job is None:
        return {
            "status": "not_indexed",
            "attempts": 0,
            "chunk_count": 0,
            "error_code": None,
            "operation": "index",
            "text_version_id": None,
        }
    status = job.status
    if status == "ready" and job.fingerprint != fingerprint(get_settings()):
        status = "outdated"
    if document.status != "ready" and job.operation == "index":
        status = "unavailable"
    return {
        "status": status,
        "attempts": job.attempts,
        "chunk_count": job.chunk_count,
        "error_code": job.error_code,
        "operation": job.operation,
        "text_version_id": str(job.text_version_id),
    }


def change_index(
    document_id: UUID,
    response: Response,
    db: DB,
    user_id: UUID,
    operation: str,
) -> dict[str, str]:
    enabled()
    response.headers["Cache-Control"] = "no-store"
    document, job = document_job(db, user_id, document_id, lock=True)
    if operation == "index" and document.status != "ready":
        raise HTTPException(409, "Document text is not ready")
    version = latest_version(db, document_id)
    if version is None:
        raise HTTPException(409, "Document text is not ready")
    created = job is None
    if job is None:
        enqueue(db, document, version)
        job = db.scalar(
            select(RagIndexJob).where(RagIndexJob.text_version_id == version.id).with_for_update()
        )
    assert job is not None
    if (
        not created
        and operation == "index"
        and job.status in {"pending", "published", "processing"}
    ):
        raise HTTPException(409, "Index operation is already scheduled")
    if (
        operation == "delete"
        and job.operation == "delete"
        and job.status
        in {
            "pending",
            "published",
            "processing",
            "deleted",
        }
    ):
        return {"status": job.status}
    request_operation(db, job, operation)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "pending"}


@router.post("/documents/{document_id}/index", status_code=202)
def reindex(
    document_id: UUID,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
) -> dict[str, str]:
    return change_index(document_id, response, db, user.id, "index")


@router.delete("/documents/{document_id}/index", status_code=202)
def remove_index(
    document_id: UUID,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
) -> dict[str, str]:
    return change_index(document_id, response, db, user.id, "delete")
