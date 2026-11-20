from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

from app.documents.extraction_data import StructuredExtraction

EXTERNAL_UNRESOLVED = (
    "EXTERNAL_UPLOAD_UNCONFIRMED",
    "EXTERNAL_CLEANUP_UNCONFIRMED",
    "EXTERNAL_INTERRUPTED",
)


class ExternalExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gigachat_ultra"]
    consent: StrictBool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_version: int = Field(gt=0, strict=True)


class ExternalAuthorization(ExternalExtractionInput):
    owner_id: UUID
    consented_at: datetime
    pending_file_id: str | None = None


class LocalRefreshRequest(BaseModel):
    provider: Literal["pymupdf_tables"] = "pymupdf_tables"
    owner_id: UUID
    sha256: str
    text_version: int


VersionRequest = ExternalAuthorization | LocalRefreshRequest
VERSION_REQUEST: TypeAdapter[VersionRequest] = TypeAdapter(
    Annotated[VersionRequest, Field(discriminator="provider")]
)


class ExternalExtractionView(BaseModel):
    job_id: UUID
    status: str
    error_code: str | None
    text_version: int


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_number: int
    original_filename: str
    format: str
    size_bytes: int
    sha256: str
    status: str
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class DocumentListItem(DocumentView):
    index_status: str


class DocumentList(BaseModel):
    items: list[DocumentListItem]
    total: int
    offset: int
    limit: int


class TextVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    normalized_text: str
    locators: list[dict[str, str | int]]
    extractor_version: str
    structured_result: StructuredExtraction | None = None


class DocumentDetails(DocumentView):
    text_version: TextVersionView | None = None
    local_refresh: ExternalExtractionView | None = None
    external_extraction: ExternalExtractionView | None = None
    external_extraction_enabled: bool = False
