import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.routes import DB, AuthenticatedUser
from app.db import get_settings
from app.documents.models import Document, DocumentTextVersion
from app.documents.schemas import DocumentDetails, DocumentList, DocumentView, TextVersionView
from app.documents.service import create_upload, owned_document, retry_document
from app.integrations.object_storage import (
    CONTENT_TYPES,
    ObjectStorage,
    OriginalNotFound,
    StorageError,
)


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
async def upload_file(request: Request) -> AsyncIterator[UploadFile]:
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
                max_fields=0,
                max_part_size=maximum,
            ).parse()
    except TimeoutError:
        raise HTTPException(408, "Upload timed out") from None
    except (MultiPartException, MultipartParseError):
        raise HTTPException(413 if exceeded else 400, "Invalid or oversized upload") from None
    try:
        file = form.get("file")
        if len(form.multi_items()) != 1 or not isinstance(file, UploadFile):
            raise HTTPException(400, "Exactly one file field is required")
        if file.size is None or file.size > maximum:
            raise HTTPException(413, "File is too large")
        yield file
    finally:
        with CancelScope(shield=True):
            await form.close()


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
) -> DocumentList:
    response.headers["Cache-Control"] = "no-store"
    total = db.scalar(
        select(func.count()).select_from(Document).where(Document.owner_id == user.id)
    )
    documents = db.scalars(
        select(Document)
        .where(Document.owner_id == user.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return DocumentList(
        items=[DocumentView.model_validate(doc) for doc in documents],
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
    response.headers["Cache-Control"] = "no-store"
    return result


class OriginalResponse(StreamingResponse):
    def __init__(self, stream: Iterator[bytes], first: bytes, headers: dict[str, str]) -> None:
        self.original_stream = stream

        def chunks() -> Iterator[bytes]:
            yield first
            yield from stream

        super().__init__(chunks(), headers=headers, media_type="application/octet-stream")

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
def download(document_id: UUID, user: AuthenticatedUser, db: DB, storage: Storage) -> Response:
    document = owned_document(db, document_id, user.id)
    if document.status == "uploading" or document.error_code in {
        "UPLOAD_INCOMPLETE",
        "CHECKSUM_MISMATCH",
        "ORIGINAL_MISSING",
    }:
        raise HTTPException(409, "Original upload is not confirmed")
    owner_id, object_key = user.id, document.object_key
    filename = quote(document.original_filename, safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
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
    return OriginalResponse(stream, first, headers)


@router.post("/{document_id}/retry", status_code=202, response_model=DocumentView)
def retry(document_id: UUID, response: Response, user: AuthenticatedUser, db: DB) -> DocumentView:
    document = retry_document(db, document_id, user.id, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return DocumentView.model_validate(document)
