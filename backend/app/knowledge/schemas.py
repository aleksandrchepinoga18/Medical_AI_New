from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

SourceStatus = Literal["uploaded", "verified", "published", "outdated", "rejected"]
ShortLabel = Annotated[str, StringConstraints(min_length=1, max_length=150)]


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_control_characters(cls, value: object) -> object:
        if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Control characters are forbidden")
        return value


class SourceMetadata(KnowledgeInput):
    title: str = Field(min_length=1, max_length=500)
    material_type: str = Field(min_length=1, max_length=100)
    author: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    doi: str | None = Field(default=None, max_length=255, pattern=r"^10\.\d{4,9}/\S+$")
    pmid: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    publication_date: date | None = None
    update_date: date | None = None
    specialty: ShortLabel
    language: str = Field(pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$", max_length=35)
    jurisdiction: ShortLabel

    @field_validator("url")
    @classmethod
    def provenance_url(cls, url: HttpUrl) -> HttpUrl:
        if url.scheme != "https" or url.username is not None or url.password is not None:
            raise ValueError("An HTTPS provenance URL without credentials is required")
        normalized = urlsplit(str(url))._replace(fragment="").geturl()
        if len(normalized) > 2048:
            raise ValueError("Provenance URL is too long")
        return HttpUrl(normalized)

    @field_validator("doi")
    @classmethod
    def normalize_doi(cls, doi: str | None) -> str | None:
        return doi.lower() if doi else None

    @model_validator(mode="after")
    def validate_dates(self) -> "SourceMetadata":
        if self.publication_date and self.update_date and self.update_date < self.publication_date:
            raise ValueError("Update precedes publication")
        return self


class SourceUpload(SourceMetadata):
    version: str = Field(min_length=1, max_length=100)


class StatusChange(KnowledgeInput):
    version_id: UUID
    status: SourceStatus
    review_revision: UUID | None = None
    verified_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_note: str | None = Field(default=None, min_length=1, max_length=1000)


class IndexRequest(KnowledgeInput):
    version_id: UUID


class ReviewPublishRequest(KnowledgeInput):
    version_id: UUID
    review_revision: UUID
    verified_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: str
    status: SourceStatus
    is_current: bool
    format: str
    size_bytes: int
    sha256: str
    uploaded_by: UUID
    uploaded_at: datetime
    review_revision: UUID
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    chunk_count: int
