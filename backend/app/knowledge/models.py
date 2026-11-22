from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Base


class KnowledgeAdmin(Base):
    __tablename__ = "knowledge_admins"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )


class MedicalSource(Base):
    __tablename__ = "medical_sources"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(500))
    material_type: Mapped[str] = mapped_column(String(100))
    author: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    doi: Mapped[str | None] = mapped_column(String(255), unique=True)
    pmid: Mapped[str | None] = mapped_column(String(20), unique=True)
    publication_date: Mapped[date | None]
    update_date: Mapped[date | None]
    specialty: Mapped[str] = mapped_column(String(150))
    language: Mapped[str] = mapped_column(String(35))
    jurisdiction: Mapped[str] = mapped_column(String(150))


class MedicalVersion(Base):
    __tablename__ = "medical_source_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "version", name="uq_medical_source_version"),
        Index(
            "uq_medical_current_version", "source_id", unique=True,
            postgresql_where=text("is_current"),
        ),
        CheckConstraint(
            "status IN ('uploaded', 'verified', 'published', 'outdated', 'rejected')",
            name="ck_medical_version_status",
        ),
        CheckConstraint(
            "status NOT IN ('verified', 'published') OR "
            "(reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND review_note IS NOT NULL)",
            name="ck_medical_version_review",
        ),
        CheckConstraint(
            "status <> 'published' OR generation IS NOT NULL", name="ck_medical_version_publish",
        ),
        CheckConstraint(
            "(generation IS NULL AND fingerprint IS NULL AND chunk_count = 0) OR "
            "(generation IS NOT NULL AND fingerprint IS NOT NULL AND chunk_count > 0)",
            name="ck_medical_version_index",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("medical_sources.id", ondelete="RESTRICT"))
    version: Mapped[str] = mapped_column(String(100))
    is_current: Mapped[bool]
    status: Mapped[str] = mapped_column(String(12))
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_revision: Mapped[UUID] = mapped_column(default=uuid4)
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(1000))
    format: Mapped[str] = mapped_column(String(4))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    generation: Mapped[UUID | None] = mapped_column(unique=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(default=0)


class MedicalChunk(Base):
    __tablename__ = "medical_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("medical_source_versions.id", ondelete="RESTRICT"), index=True,
    )
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int]
    end_char: Mapped[int]
    locators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
