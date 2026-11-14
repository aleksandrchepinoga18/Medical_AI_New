"""Create document persistence without altering existing application tables."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_documents"
down_revision = "0001_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploading"),
        sa.Column("idempotency_key", sa.Uuid()),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
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
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
        sa.CheckConstraint("format IN ('pdf', 'docx')", name="ck_documents_format"),
        sa.CheckConstraint(
            "status IN ('uploading','queued','processing','ready','failed','needs_ocr')",
            name="ck_documents_status",
        ),
        sa.UniqueConstraint("object_key", name="uq_documents_object_key"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_documents_owner_request"),
    )
    op.create_index("ix_documents_owner_created_id", "documents", ["owner_id", "created_at", "id"])
    op.create_index("ix_documents_owner_sha256", "documents", ["owner_id", "sha256"])
    op.create_table(
        "document_text_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("locators", postgresql.JSONB(), nullable=False),
        sa.Column("extractor_version", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("document_id", "version", name="uq_document_text_version"),
        sa.CheckConstraint("version > 0", name="ck_document_text_version"),
        sa.CheckConstraint("jsonb_typeof(locators) = 'array'", name="ck_document_text_locators"),
    )
    op.create_table(
        "document_ingestion_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
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
        sa.UniqueConstraint("document_id", "version", name="uq_document_ingestion_version"),
        sa.CheckConstraint("version > 0", name="ck_document_job_version"),
        sa.CheckConstraint("attempts >= 0", name="ck_document_job_attempts"),
        sa.CheckConstraint(
            "status IN ('pending','published','processing','succeeded','failed')",
            name="ck_document_job_status",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)", name="ck_document_job_lease"
        ),
    )
    op.create_index(
        "ix_document_jobs_status_available", "document_ingestion_jobs", ["status", "available_at"]
    )
    op.create_index(
        "ix_document_jobs_lease_expires", "document_ingestion_jobs", ["lease_expires_at"]
    )


def downgrade() -> None:
    raise RuntimeError("Document data must be preserved; use a reviewed forward migration")
