import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import ColumnElement, Date, cast, func, or_, select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.routes import DB, AuthenticatedUser
from app.db import get_settings
from app.documents.gigachat import UPLOAD_BYTES
from app.documents.models import Document, DocumentIngestionJob, DocumentTextVersion
from app.documents.schemas import (
    EXTERNAL_UNRESOLVED,
    VERSION_REQUEST,
    DocumentDetails,
    DocumentList,
    DocumentListItem,
    DocumentView,
    ExternalExtractionInput,
    ExternalExtractionView,
    TextVersionView,
)
from app.documents.service import (
    authorize_external,
    create_upload,
    owned_document,
    refresh_local,
    retry_document,
)
from app.integrations.object_storage import (
    CONTENT_TYPES,
    ObjectStorage,
    OriginalNotFound,
    StorageError,
)
from app.rag.models import RagIndexJob
from app.rag.service import index_state


def require_session_cookie(request: Request) -> None:
    if not request.cookies.get("medical_session"):
        raise HTTPException(401, "Authentication required")


router = APIRouter(
    prefix="/documents", tags=["documents"], dependencies=[Depends(require_session_cookie)]
)


class ClosingMultipartParser(MultiPartParser):
    header_bytes: int = 0

    def on_part_begin(self) -> None:
        self.header_bytes = 0
        super().on_part_begin()

    def count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > 16384:
            raise MultiPartException("Multipart headers too large")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.count_header(end - start)
        super().on_header_value(data, start, end)

    async def parse(self) -> FormData:
        try:
            form = await super().parse()
            completed = {
                id(item.file) for _, item in form.multi_items() if isinstance(item, UploadFile)
            }
            if any(id(file) not in completed for file in self._files_to_close_on_error):
                raise MultiPartException("Incomplete multipart upload")
            return form
        except BaseException:
            # Starlette only cleans these on MultiPartException; also handle malformed
            # multipart and client cancellation. Covered by parser lifecycle tests.
            for file in self._files_to_close_on_error:
                file.close()
            raise


@lru_cache
def get_storage() -> ObjectStorage:
    try:
        return ObjectStorage.from_settings(get_settings())
    except ValueError:
        raise HTTPException(503, "Document storage is not configured") from None


Storage = Annotated[ObjectStorage, Depends(get_storage)]


@asynccontextmanager
async def bounded_upload_form(
    request: Request, *, metadata: bool = False,
) -> AsyncIterator[FormData]:
    maximum = get_settings().documents_max_bytes
    envelope_limit = maximum + 65536
    if "multipart/form-data" not in request.headers.get("content-type", "").lower():
        raise HTTPException(415, "Expected multipart upload")
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "Invalid content length") from None
    if length < 0:
        raise HTTPException(400, "Invalid content length")
    if length > envelope_limit:
        raise HTTPException(413, "File is too large")
    exceeded = False

    async def limited_stream() -> AsyncGenerator[bytes, None]:
        nonlocal exceeded
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > envelope_limit:
                exceeded = True
                # MultiPartParser closes partial UploadFiles on MultiPartException.
                raise MultiPartException("Upload too large")
            yield chunk

    try:
        async with asyncio.timeout(get_settings().documents_upload_grace_seconds):
            form = await ClosingMultipartParser(
                request.headers,
                limited_stream(),
                max_files=1,
                max_fields=int(metadata),
                max_part_size=16384 if metadata else maximum,
            ).parse()
    except TimeoutError:
        raise HTTPException(408, "Upload timed out") from None
    except (MultiPartException, MultipartParseError):
        raise HTTPException(413 if exceeded else 400, "Invalid or oversized upload") from None
    try:
        file = form.get("file")
        expected = ["file", "metadata"] if metadata else ["file"]
        if sorted(key for key, _ in form.multi_items()) != expected or not isinstance(
            file, UploadFile
        ):
            raise HTTPException(400, "Exactly one file field is required")
        if metadata and not isinstance(form.get("metadata"), str):
            raise HTTPException(400, "Metadata must be a text field")
        if file.size is None or file.size > maximum:
            raise HTTPException(413, "File is too large")
        yield form
    finally:
        with CancelScope(shield=True):
            await form.close()


@asynccontextmanager
async def upload_file(request: Request) -> AsyncIterator[UploadFile]:
    async with bounded_upload_form(request) as form:
        file = form["file"]
        assert isinstance(file, UploadFile)
        yield file


@router.post("", status_code=202, response_model=DocumentView)
async def upload(
    request: Request, response: Response, user: AuthenticatedUser, db: DB, storage: Storage
) -> DocumentView:
    # Auth/CSRF dependencies complete before multipart parsing or disk staging starts.
    try:
        request_key = UUID(request.headers.get("idempotency-key", ""))
    except ValueError:
        raise HTTPException(400, "A UUID Idempotency-Key header is required") from None
    owner_id = user.id
    # Do not hold the authentication transaction/connection while receiving the body.
    await run_in_threadpool(db.rollback)
    async with upload_file(request) as file:
        document = await run_in_threadpool(
            create_upload,
            db,
            owner_id,
            file.file,
            file.filename or "",
            request_key,
            get_settings(),
            storage,
        )
        response.headers["Cache-Control"] = "no-store"
        return DocumentView.model_validate(document)


@router.get("", response_model=DocumentList)
def list_documents(
    response: Response,
    user: AuthenticatedUser,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    q: Annotated[str, Query(max_length=255)] = "",
    sort: Literal["number", "name", "date"] = "date",
    direction: Literal["asc", "desc"] = "desc",
) -> DocumentList:
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    filters: list[ColumnElement[bool]] = [Document.owner_id == user.id]
    term = q.strip()
    if term:
        terms = [Document.original_filename.icontains(term, autoescape=True)]
        digits = term.removeprefix("№").strip()
        if digits.isascii() and digits.isdecimal() and len(digits) <= 18:
            terms.append(Document.document_number == int(digits))
        try:
            uploaded_on = date.fromisoformat(term)
        except ValueError:
            pass
        else:
            local_date = cast(func.timezone("Europe/Moscow", Document.created_at), Date)
            terms.append(local_date == uploaded_on)
        filters.append(or_(*terms))
    total = db.scalar(
        select(func.count()).select_from(Document).where(*filters)
    )
    latest = (
        select(DocumentTextVersion.id)
        .where(DocumentTextVersion.document_id == Document.id)
        .order_by(DocumentTextVersion.version.desc())
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )
    column = {
        "number": Document.document_number,
        "name": func.lower(Document.original_filename),
        "date": Document.created_at,
    }[sort]
    documents = db.execute(
        select(Document, RagIndexJob)
        .outerjoin(
            RagIndexJob,
            (RagIndexJob.text_version_id == latest) & (RagIndexJob.user_id == user.id),
        )
        .where(*filters)
        .order_by(column.asc() if direction == "asc" else column.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return DocumentList(
        items=[
            DocumentListItem(
                **DocumentView.model_validate(doc).model_dump(),
                index_status=index_state(doc, job, settings),
            )
            for doc, job in documents
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentDetails)
def details(
    document_id: UUID, response: Response, user: AuthenticatedUser, db: DB
) -> DocumentDetails:
    document = owned_document(db, document_id, user.id)
    version = db.scalar(
        select(DocumentTextVersion)
        .where(DocumentTextVersion.document_id == document.id)
        .order_by(DocumentTextVersion.version.desc())
        .limit(1)
    )
    result = DocumentDetails.model_validate(document)
    result.text_version = TextVersionView.model_validate(version) if version else None
    if version is not None:
        jobs = (
            select(DocumentIngestionJob)
            .where(DocumentIngestionJob.document_id == document.id)
            .order_by(DocumentIngestionJob.version.desc()).limit(1)
        )
        provider = DocumentIngestionJob.external_request["provider"].as_string()
        for name, field in (
            ("pymupdf_tables", "local_refresh"), ("gigachat_ultra", "external_extraction"),
        ):
            job = db.scalar(jobs.where(provider == name))
            if job is not None:
                view = extraction_job_view(job)
                if view.text_version == version.version or (
                    name == "pymupdf_tables" and job.version == version.version
                ) or view.error_code in EXTERNAL_UNRESOLVED:
                    setattr(result, field, view)
    result.external_extraction_enabled = (
        get_settings().documents_extraction_provider == "gigachat_ultra"
        and document.format == "pdf"
        and document.size_bytes <= UPLOAD_BYTES
    )
    response.headers["Cache-Control"] = "no-store"
    return result


class OriginalResponse(StreamingResponse):
    def __init__(
        self, stream: Iterator[bytes], first: bytes, headers: dict[str, str],
        media_type: str = "application/octet-stream",
    ) -> None:
        self.original_stream = stream

        def chunks() -> Iterator[bytes]:
            yield first
            yield from stream

        super().__init__(chunks(), headers=headers, media_type=media_type)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # StreamingResponse's background callback is not guaranteed on disconnect/errors.
            close = getattr(self.original_stream, "close", None)
            if close:
                with CancelScope(shield=True):
                    await run_in_threadpool(close)


@router.get("/{document_id}/download")
def download(
    document_id: UUID, user: AuthenticatedUser, db: DB, storage: Storage,
    inline: bool = False,
) -> Response:
    document = owned_document(db, document_id, user.id)
    if document.status == "uploading" or document.error_code in {
        "UPLOAD_INCOMPLETE",
        "CHECKSUM_MISMATCH",
        "ORIGINAL_MISSING",
    }:
        raise HTTPException(409, "Original upload is not confirmed")
    owner_id, object_key = user.id, document.object_key
    filename = quote(document.original_filename, safe="")
    preview = inline and document.format == "pdf"
    disposition = "inline" if preview else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
        "Content-Type": CONTENT_TYPES[document.format],
        "Content-Length": str(document.size_bytes),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    db.rollback()  # No DB connection is needed for a slow original download.
    stream = storage.stream_original(owner_id, document_id, object_key)
    try:
        first = next(stream)
    except OriginalNotFound:
        raise HTTPException(404, "Original not found") from None
    except (StorageError, StopIteration):
        raise HTTPException(503, "Original is temporarily unavailable") from None
    media_type = "application/pdf" if preview else "application/octet-stream"
    return OriginalResponse(stream, first, headers, media_type)


@router.post("/{document_id}/retry", status_code=202, response_model=DocumentView)
def retry(document_id: UUID, response: Response, user: AuthenticatedUser, db: DB) -> DocumentView:
    document = retry_document(db, document_id, user.id, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return DocumentView.model_validate(document)


def extraction_job_view(job: DocumentIngestionJob) -> ExternalExtractionView:
    authorization = VERSION_REQUEST.validate_python(job.external_request)
    return ExternalExtractionView(
        job_id=job.id, status=job.status, error_code=job.error_code,
        text_version=authorization.text_version,
    )


@router.post("/{document_id}/external-extraction", status_code=202)
def request_external_extraction(
    document_id: UUID, data: ExternalExtractionInput, response: Response,
    user: AuthenticatedUser, db: DB,
) -> ExternalExtractionView:
    job = authorize_external(db, document_id, user.id, data, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return extraction_job_view(job)


@router.post("/{document_id}/refresh-extraction", status_code=202)
def refresh_extraction(
    document_id: UUID, response: Response, user: AuthenticatedUser, db: DB,
) -> ExternalExtractionView:
    job = refresh_local(db, document_id, user.id, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return extraction_job_view(job)


@router.get("/{document_id}/external-extraction/{job_id}")
def external_extraction_status(
    document_id: UUID, job_id: UUID, response: Response, user: AuthenticatedUser, db: DB,
) -> ExternalExtractionView:
    owned_document(db, document_id, user.id)
    job = db.scalar(select(DocumentIngestionJob).where(
        DocumentIngestionJob.id == job_id,
        DocumentIngestionJob.document_id == document_id,
        DocumentIngestionJob.external_request["provider"].as_string() == "gigachat_ultra",
    ))
    if job is None:
        raise HTTPException(404, "External extraction not found")
    response.headers["Cache-Control"] = "no-store"
    return extraction_job_view(job)
