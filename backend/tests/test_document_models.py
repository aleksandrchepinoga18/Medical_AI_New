from uuid import uuid4

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable

from app.auth.models import Base, User
from app.documents.models import (
    Document,
    DocumentIngestionJob,
    DocumentStatus,
    DocumentTextVersion,
    JobStatus,
)


def test_relationships_share_auth_metadata_without_delete_cascades() -> None:
    configure_mappers()
    user = User(id=uuid4(), email="synthetic@example.org", password_hash="not-real")
    document = Document(owner=user)
    version = DocumentTextVersion(document=document, version=1)
    job = DocumentIngestionJob(document=document, version=1)
    assert version in document.text_versions
    assert job in document.ingestion_jobs
    for model in (Document, DocumentTextVersion, DocumentIngestionJob):
        assert model.metadata is Base.metadata
        for foreign_key in model.__table__.foreign_keys:
            assert foreign_key.ondelete == "RESTRICT"
        for relation in model.__mapper__.relationships:
            assert "delete" not in relation.cascade
            assert "delete-orphan" not in relation.cascade


def test_schema_has_named_constraints_indexes_and_postgresql_types() -> None:
    table = Document.__table__
    unique_columns = {
        tuple(column.name for column in item.columns)
        for item in table.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert ("object_key",) in unique_columns
    assert ("owner_id", "idempotency_key") in unique_columns
    assert ("sha256",) not in unique_columns  # No cross-user deduplication or existence leak.
    assert {index.name for index in table.indexes} == {
        "ix_documents_owner_created_id",
        "ix_documents_owner_sha256",
    }
    for model, statuses in ((Document, DocumentStatus), (DocumentIngestionJob, JobStatus)):
        checks = [
            str(c.sqltext) for c in model.__table__.constraints if isinstance(c, CheckConstraint)
        ]
        assert all(any(f"'{status.value}'" in check for check in checks) for status in statuses)
    ddl = str(CreateTable(DocumentTextVersion.__table__).compile(dialect=dialect()))
    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
