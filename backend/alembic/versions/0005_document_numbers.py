"""Persist per-owner document numbers and allocate them for every insert."""

import sqlalchemy as sa

from alembic import op

revision = "0005_document_numbers"
down_revision = "0004_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_document_number", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("documents", sa.Column("document_number", sa.BigInteger(), nullable=True))
    # This establishes stored numbers once. Runtime allocation uses the owner's counter.
    op.execute("""
        WITH numbered AS (
            SELECT id, row_number() OVER (PARTITION BY owner_id ORDER BY created_at, id) AS number
            FROM documents
        )
        UPDATE documents AS d SET document_number = n.number
        FROM numbered AS n WHERE d.id = n.id
    """)
    op.execute("""
        UPDATE users AS u SET last_document_number = d.last_number
        FROM (SELECT owner_id, max(document_number) AS last_number
              FROM documents GROUP BY owner_id) AS d
        WHERE u.id = d.owner_id
    """)
    op.alter_column("documents", "document_number", nullable=False)
    op.create_check_constraint("ck_documents_number_positive", "documents", "document_number > 0")
    op.create_unique_constraint(
        "uq_documents_owner_number", "documents", ["owner_id", "document_number"]
    )
    op.execute("""
        CREATE FUNCTION assign_document_number() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE users SET last_document_number = last_document_number + 1
            WHERE id = NEW.owner_id RETURNING last_document_number INTO NEW.document_number;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER assign_document_number BEFORE INSERT ON documents
        FOR EACH ROW EXECUTE FUNCTION assign_document_number()
    """)
    op.execute("""
        CREATE FUNCTION preserve_document_number() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.document_number IS DISTINCT FROM OLD.document_number
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
                RAISE EXCEPTION 'Document identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_document_number BEFORE UPDATE OF document_number, owner_id
        ON documents FOR EACH ROW EXECUTE FUNCTION preserve_document_number()
    """)


def downgrade() -> None:
    raise RuntimeError("Keep assigned document numbers; roll back the application image")
