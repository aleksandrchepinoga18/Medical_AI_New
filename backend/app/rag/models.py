"""Independent indexing state; original document status is never changed here."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Base


class RagIndexJob(Base):
    __tablename__ = "rag_index_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','published','processing','ready','failed','deleted')",
            name="ck_rag_job_status",
        ),
        CheckConstraint("operation IN ('index','delete')", name="ck_rag_job_operation"),
        CheckConstraint("attempts >= 0", name="ck_rag_job_attempts"),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_rag_job_lease",
        ),
        Index("ix_rag_jobs_dispatch", "status", "available_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    text_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_text_versions.id", ondelete="RESTRICT"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    operation: Mapped[str] = mapped_column(String(8), server_default="index")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation: Mapped[UUID | None]
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class RagChunk(Base):
    __tablename__ = "rag_chunks"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND start_char >= 0 AND end_char > start_char",
            name="ck_rag_chunk_offsets",
        ),
        Index("ix_rag_chunks_version", "text_version_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    text_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_text_versions.id", ondelete="RESTRICT"),
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
