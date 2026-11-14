from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    format: str
    size_bytes: int
    sha256: str
    status: str
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentView]
    total: int
    offset: int
    limit: int


class TextVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    normalized_text: str
    locators: list[dict[str, str | int]]
    extractor_version: str


class DocumentDetails(DocumentView):
    text_version: TextVersionView | None = None
