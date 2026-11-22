"""Add the shared medical knowledge registry and dedicated administrator grants."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0007_medical_knowledge"
down_revision = "0006_document_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_admins",
        sa.Column("user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_table(
        "medical_sources",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("material_type", sa.String(100), nullable=False),
        sa.Column("author", sa.String(300), nullable=False),
        sa.Column("publisher", sa.String(300), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False, unique=True),
        sa.Column("doi", sa.String(255), unique=True),
        sa.Column("pmid", sa.String(20), unique=True),
        sa.Column("publication_date", sa.Date),
        sa.Column("update_date", sa.Date),
        sa.Column("specialty", sa.String(150), nullable=False),
        sa.Column("language", sa.String(35), nullable=False),
        sa.Column("jurisdiction", sa.String(150), nullable=False),
    )
    op.create_table(
        "medical_source_versions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("medical_sources.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("uploaded_by", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_revision", sa.Uuid, nullable=False),
        sa.Column("reviewed_by", sa.Uuid, sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.String(1000)),
        sa.Column("format", sa.String(4), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("generation", sa.Uuid, unique=True),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.UniqueConstraint("source_id", "version", name="uq_medical_source_version"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'verified', 'published', 'outdated', 'rejected')",
            name="ck_medical_version_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('verified', 'published') OR "
            "(reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND review_note IS NOT NULL)",
            name="ck_medical_version_review",
        ),
        sa.CheckConstraint(
            "status <> 'published' OR generation IS NOT NULL", name="ck_medical_version_publish",
        ),
        sa.CheckConstraint(
            "(generation IS NULL AND fingerprint IS NULL AND chunk_count = 0) OR "
            "(generation IS NOT NULL AND fingerprint IS NOT NULL AND chunk_count > 0)",
            name="ck_medical_version_index",
        ),
    )
    op.create_index(
        "uq_medical_current_version", "medical_source_versions", ["source_id"], unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "medical_chunks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("version_id", sa.Uuid,
                  sa.ForeignKey("medical_source_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column("locators", JSONB, nullable=False),
    )
    op.create_index("ix_medical_chunks_version_id", "medical_chunks", ["version_id"])


def downgrade() -> None:
    raise RuntimeError("Preserve the medical registry and its originals; roll back the app image")
