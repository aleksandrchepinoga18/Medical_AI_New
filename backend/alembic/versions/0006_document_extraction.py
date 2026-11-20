"""Add versioned extraction and explicit external job authorization."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_document_extraction"
down_revision = "0005_document_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_text_versions",
        sa.Column("structured_result", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column(
        "document_ingestion_jobs",
        sa.Column("external_request", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_document_structured_result",
        "document_text_versions",
        "jsonb_typeof(structured_result) = 'object'",
    )
    op.create_check_constraint(
        "ck_document_job_external_request",
        "document_ingestion_jobs",
        "jsonb_typeof(external_request) = 'object'",
    )


def downgrade() -> None:
    raise RuntimeError("Preserve extraction results and authorizations; roll back the app image")
