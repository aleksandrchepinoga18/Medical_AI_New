"""Add personal index jobs and chunks. No alteration of existing tables."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0003_personal_rag"
down_revision = "0002_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_index_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "text_version_id",
            sa.Uuid(),
            sa.ForeignKey("document_text_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("operation", sa.String(8), nullable=False, server_default="index"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("generation", sa.Uuid()),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("text_version_id"),
        sa.CheckConstraint(
            "status IN ('pending','published','processing','ready','failed','deleted')",
            name="ck_rag_job_status",
        ),
        sa.CheckConstraint("operation IN ('index','delete')", name="ck_rag_job_operation"),
        sa.CheckConstraint("attempts >= 0", name="ck_rag_job_attempts"),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_rag_job_lease",
        ),
    )
    op.create_index("ix_rag_jobs_dispatch", "rag_index_jobs", ["status", "available_at"])
    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "text_version_id",
            sa.Uuid(),
            sa.ForeignKey("document_text_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sources", JSONB(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND start_char >= 0 AND end_char > start_char",
            name="ck_rag_chunk_offsets",
        ),
    )
    op.create_index("ix_rag_chunks_version", "rag_chunks", ["text_version_id"])


def downgrade() -> None:
    raise RuntimeError("RAG data must be preserved; use a reviewed forward migration")
