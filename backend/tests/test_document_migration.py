import importlib.util
import re
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_migration_generates_only_additive_document_ddl() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0002_documents.py"
    spec = importlib.util.spec_from_file_location("document_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0002_documents"
    assert migration.down_revision == "0001_auth"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE") == 3
    for name in ("documents", "document_text_versions", "document_ingestion_jobs"):
        assert f"CREATE TABLE {name}" in sql
    assert "REFERENCES users (id) ON DELETE RESTRICT" in sql
    assert not re.search(r"(?m)^\s*(DROP|DELETE|TRUNCATE|ALTER TABLE|UPDATE)\b", sql)
    with pytest.raises(RuntimeError, match="preserved"):
        migration.downgrade()


def test_extraction_migration_adds_nullable_columns_only() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0006_document_extraction.py"
    spec = importlib.util.spec_from_file_location("document_extraction_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0005_document_numbers"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "ALTER TABLE document_text_versions ADD COLUMN structured_result JSONB;" in sql
    assert "ALTER TABLE document_ingestion_jobs ADD COLUMN external_request JSONB;" in sql
    assert not re.search(r"(?m)^\s*(DROP|DELETE|TRUNCATE|CREATE TABLE|UPDATE)\b", sql)
    with pytest.raises(RuntimeError, match="Preserve"):
        migration.downgrade()
