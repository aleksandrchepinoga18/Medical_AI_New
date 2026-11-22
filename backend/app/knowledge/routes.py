from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.auth.routes import DB, AuthenticatedUser
from app.core.config import Settings
from app.db import get_settings
from app.documents.providers import ProcessingError
from app.documents.routes import OriginalResponse, bounded_upload_form, require_session_cookie
from app.integrations.object_storage import OriginalNotFound, StorageError
from app.knowledge import service
from app.knowledge.models import KnowledgeAdmin, MedicalSource, MedicalVersion
from app.knowledge.schemas import (
    IndexRequest,
    ReviewPublishRequest,
    SourceMetadata,
    SourceUpload,
    StatusChange,
)
from app.knowledge.storage import KnowledgeStorage
from app.rag.adapters import RagError
from app.rag.routes import SearchRequest

router = APIRouter(
    prefix="/knowledge", tags=["medical-knowledge"], dependencies=[Depends(require_session_cookie)],
)


def require_admin(db: DB, user: AuthenticatedUser) -> UUID:
    if db.get(KnowledgeAdmin, user.id) is None:
        raise HTTPException(403, "Medical knowledge administrator required")
    return user.id


Admin = Annotated[UUID, Depends(require_admin)]
Config = Annotated[Settings, Depends(get_settings)]


@router.get("/access", status_code=204, dependencies=[Depends(require_admin)])
def access(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@lru_cache
def get_storage() -> KnowledgeStorage:
    try:
        return KnowledgeStorage.from_settings(get_settings())
    except ValueError:
        raise HTTPException(503, "Medical source storage is not configured") from None


async def upload(
    request: Request, db: DB, settings: Settings, admin: UUID, source_id: UUID | None = None,
) -> dict[str, Any]:
    async with bounded_upload_form(request, metadata=True) as form:
        file, metadata = form["file"], form["metadata"]
        assert isinstance(file, UploadFile) and isinstance(metadata, str)
        try:
            data = SourceUpload.model_validate_json(metadata)
        except ValidationError:
            raise HTTPException(422, "Invalid medical source metadata") from None
        return await run_in_threadpool(
            service.store_version,
            db, get_storage(), settings, admin, data, file.file, file.filename or "", source_id,
        )


@router.post("/sources", status_code=201)
async def create_source(
    request: Request, response: Response, db: DB, admin: Admin, settings: Config,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await upload(request, db, settings, admin)


@router.get("/sources", dependencies=[Depends(require_admin)])
def registry(
    response: Response,
    db: DB,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    statement = (
        select(MedicalSource, MedicalVersion)
        .join(MedicalVersion, MedicalVersion.source_id == MedicalSource.id)
        .where(MedicalVersion.is_current)
        .order_by(MedicalSource.id)
        .limit(limit + 1)
    )
    if after is not None:
        statement = statement.where(MedicalSource.id > after)
    rows = db.execute(statement).all()
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [service.source_snapshot(source, version) for source, version in rows[:limit]],
        "next_after": rows[limit - 1][0].id if len(rows) > limit else None,
    }


@router.put("/sources/{source_id}/metadata", dependencies=[Depends(require_admin)])
def edit_source(
    source_id: UUID, data: SourceMetadata, db: DB, response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.update_metadata(db, source_id, data)


@router.get(
    "/sources/{source_id}/versions/{version_id}/original", dependencies=[Depends(require_admin)],
)
def download_original(source_id: UUID, version_id: UUID, db: DB) -> Response:
    version = db.scalar(select(MedicalVersion).where(
        MedicalVersion.id == version_id, MedicalVersion.source_id == source_id,
        MedicalVersion.is_current,
    ))
    if version is None:
        raise HTTPException(404, "Medical source version not found")
    extension = "pdf" if version.format == "pdf" else "docx"
    name = f"medical-source-{source_id}-{version_id}.{extension}"
    headers = {
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(version.size_bytes),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    db.rollback()  # Release the DB connection before starting the download.
    stream = get_storage().stream_version(source_id, version_id)
    try:
        first = next(stream)
    except OriginalNotFound:
        raise HTTPException(404, "Medical source original not found") from None
    except (StorageError, StopIteration):
        raise HTTPException(503, "Medical source original unavailable") from None
    return OriginalResponse(stream, first, headers)


@router.put("/sources/{source_id}/version")
async def replace_version(
    source_id: UUID, request: Request, response: Response, db: DB, admin: Admin, settings: Config,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await upload(request, db, settings, admin, source_id)


@router.put("/sources/{source_id}/status")
def set_status(
    source_id: UUID, data: StatusChange, response: Response, db: DB, admin: Admin, settings: Config,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.change_status(db, settings, source_id, admin, data)
    except RagError:
        raise HTTPException(503, "Medical source index unavailable") from None


@contextmanager
def indexing_errors() -> Iterator[None]:
    try:
        yield
    except (OriginalNotFound, ProcessingError) as error:
        code = "ORIGINAL_MISSING" if isinstance(error, OriginalNotFound) else str(error)
        if code == "PROCESSING_TIMEOUT":
            raise HTTPException(503, "Medical source processing timed out") from None
        raise HTTPException(
            422, {"message": "Medical source extraction failed", "code": code},
        ) from None
    except (StorageError, RagError):
        raise HTTPException(503, "Medical source indexing unavailable") from None


@router.post("/sources/{source_id}/index", dependencies=[Depends(require_admin)])
def start_index(
    source_id: UUID, data: IndexRequest, response: Response, db: DB, settings: Config,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with indexing_errors():
        return service.index_source(db, settings, get_storage(), source_id, data.version_id)


@router.post("/sources/{source_id}/review-publish")
def review_publish(
    source_id: UUID, data: ReviewPublishRequest, response: Response, db: DB,
    admin: Admin, settings: Config,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with indexing_errors():
        return service.review_and_publish(db, settings, get_storage(), source_id, admin, data)


@router.post("/search")
def search_knowledge(
    data: SearchRequest,
    response: Response,
    db: DB,
    user: AuthenticatedUser,
    settings: Config,
) -> dict[str, Any]:
    query = data.query.strip()
    if not query or data.k > settings.rag_search_max_k:
        raise HTTPException(422, "Invalid search request")
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"items": service.search(db, settings, query, data.k), "k": data.k}
    except RagError:
        raise HTTPException(503, "Medical knowledge search unavailable") from None
