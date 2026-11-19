"""Additive document schema using the existing application's metadata."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.models import Base, User


class DocumentStatus(StrEnum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    NEEDS_OCR = "needs_ocr"


class JobStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_documents_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
        CheckConstraint("format IN ('pdf', 'docx')", name="ck_documents_format"),
        CheckConstraint(
            "status IN ('uploading','queued','processing','ready','failed','needs_ocr')",
            name="ck_documents_status",
        ),
        UniqueConstraint("object_key", name="uq_documents_object_key"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_documents_owner_request"),
        UniqueConstraint("owner_id", "document_number", name="uq_documents_owner_number"),
        CheckConstraint("document_number > 0", name="ck_documents_number_positive"),
        Index("ix_documents_owner_created_id", "owner_id", "created_at", "id"),
        Index("ix_documents_owner_sha256", "owner_id", "sha256"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    document_number: Mapped[int] = mapped_column(BigInteger, server_default=FetchedValue())
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(8))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), server_default="uploading")
    idempotency_key: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    owner: Mapped[User] = relationship()
    text_versions: Mapped[list["DocumentTextVersion"]] = relationship(
        back_populates="document", passive_deletes="all"
    )
    ingestion_jobs: Mapped[list["DocumentIngestionJob"]] = relationship(
        back_populates="document", passive_deletes="all"
    )


class DocumentTextVersion(Base):
    __tablename__ = "document_text_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_text_version"),
        CheckConstraint("version > 0", name="ck_document_text_version"),
        CheckConstraint("jsonb_typeof(locators) = 'array'", name="ck_document_text_locators"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    normalized_text: Mapped[str] = mapped_column(Text)
    locators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    extractor_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    document: Mapped[Document] = relationship(back_populates="text_versions")


class DocumentIngestionJob(Base):
    __tablename__ = "document_ingestion_jobs"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_ingestion_version"),
        CheckConstraint("version > 0", name="ck_document_job_version"),
        CheckConstraint("attempts >= 0", name="ck_document_job_attempts"),
        CheckConstraint(
            "status IN ('pending','published','processing','succeeded','failed')",
            name="ck_document_job_status",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_document_job_lease",
        ),
        Index("ix_document_jobs_status_available", "status", "available_at"),
        Index("ix_document_jobs_lease_expires", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    document: Mapped[Document] = relationship(back_populates="ingestion_jobs")
