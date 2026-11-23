"""Allow PubMed citations in the medical registry before an original is uploaded."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0008_pubmed_citations"
down_revision = "0007_medical_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("medical_sources", sa.Column("pubmed_citation", JSONB, nullable=True))


def downgrade() -> None:
    raise RuntimeError("Preserve saved PubMed citations and their originals")
