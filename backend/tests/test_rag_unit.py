import importlib.util
import re
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError
from app.rag.chunking import chunk_text
from app.rag.evaluation import metrics


def config(**updates):
    return Settings(redis_password="synthetic", **updates)


def test_chunks_are_deterministic_and_keep_offsets_and_sources():
    version = uuid4()
    locators = [
        {"kind": "page", "index": 1, "start": 0, "end": 7},
        {"kind": "page", "index": 2, "start": 8, "end": 15},
    ]
    chunks = chunk_text(version, "abcdefg hijklmn", locators, 10, 3)
    assert chunks == chunk_text(version, "abcdefg hijklmn", locators, 10, 3)
    assert [(c.start, c.end) for c in chunks] == [(0, 10), (7, 15)]
    assert chunks[0].text[-3:] == chunks[1].text[:3]
    assert chunks[0].sources == locators
    assert chunks[1].sources == [locators[1]]
    assert chunks[0].id != chunk_text(uuid4(), "abcdefg hijklmn", locators, 10, 3)[0].id
    assert len({c.id for c in chunks}) == len(chunks)
    assert chunk_text(version, "   ", [], 10, 0) == []


def test_invalid_overlap_rejected_in_settings_and_chunker():
    with pytest.raises(ValueError):
        chunk_text(uuid4(), "abc", [], 2, 2)
    with pytest.raises(ValidationError):
        config(rag_chunk_chars=100, rag_chunk_overlap=100)


@pytest.mark.parametrize(
    "values",
    [[[1.0]], [[0.0, 0.0]], [[float("nan"), 1.0]], [[True, 1.0]], []],
)
def test_embedding_dimension_and_values_fail_closed(monkeypatch, values):
    adapter = OllamaEmbeddings(config(rag_embedding_dimensions=2))
    monkeypatch.setattr(adapter.http, "request", lambda *args: {"embeddings": values})
    with pytest.raises(RagError):
        adapter.embed(["synthetic"])


def test_embedding_request_disallows_silent_truncation(monkeypatch):
    adapter = OllamaEmbeddings(config(rag_embedding_dimensions=2))
    calls = []

    def request(*args):
        calls.append(args)
        return {"embeddings": [[0.5, 0.5]]}

    monkeypatch.setattr(adapter.http, "request", request)
    assert adapter.embed(["synthetic"]) == [[0.5, 0.5]]
    assert calls[0][2]["truncate"] is False
    with pytest.raises(RagError):
        OllamaEmbeddings(config(rag_ollama_url="https://external.example"))
    with pytest.raises(RagError):
        OllamaEmbeddings(config(rag_embedding_model="model:cloud"))


def test_replaced_model_digest_is_rejected_before_embedding(monkeypatch):
    adapter = OllamaEmbeddings(config(rag_embedding_digest="0" * 64))
    calls = []

    def request(*args):
        calls.append(args)
        return {"models": [{"name": "bge-m3:567m", "digest": "1" * 64}]}

    monkeypatch.setattr(adapter.http, "request", request)
    with pytest.raises(RagError, match="EMBEDDING_MODEL_CHANGED"):
        adapter.embed(["synthetic"])
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_existing_incompatible_collection_is_never_recreated(monkeypatch):
    adapter = QdrantVectors(config())
    calls = []

    def request(method, path, body=None):
        calls.append((method, path))
        return {
            "result": {
                "config": {
                    "params": {
                        "vectors": {
                            "size": 7,
                            "distance": "Cosine",
                        },
                    },
                },
            },
        }

    monkeypatch.setattr(adapter.http, "request", request)
    with pytest.raises(RagError, match="COLLECTION_DIMENSION_MISMATCH"):
        adapter.ensure()
    assert all(method == "GET" for method, _ in calls)


def test_query_and_deletion_always_include_server_owner_filter(monkeypatch):
    owner, document = uuid4(), uuid4()
    adapter = QdrantVectors(config())
    calls = []
    monkeypatch.setattr(adapter, "inspect", lambda: {})

    def request(method, path, body):
        calls.append(body)
        return {"result": {"points": []}}

    monkeypatch.setattr(adapter.http, "request", request)
    adapter.query(owner, [1.0], [str(uuid4())], 5)
    adapter.delete_document(owner, document)
    for body in calls:
        assert {"key": "user_id", "match": {"value": str(owner)}} in body["filter"]["must"]
    assert any(item["key"] == "generation" for item in calls[0]["filter"]["must"])
    assert any(item["key"] == "document_id" for item in calls[1]["filter"]["must"])


def test_metrics_have_known_values_and_deduplicate_documents():
    result = metrics(["irrelevant", "a", "a", "b"], {"a": 3, "b": 1}, 3)
    assert result["recall"] == 1.0
    assert result["mrr"] == 0.5
    assert 0 < result["ndcg"] < 1
    assert metrics(["a", "b"], {"a": 3, "b": 1}, 2)["ndcg"] == 1.0
    assert metrics([], {}, 3) == {"recall": 0, "mrr": 0, "ndcg": 0}


def test_rag_routes_reject_missing_session_without_connecting_to_services():
    with TestClient(app) as client:
        assert client.post("/rag/search", json={"query": "synthetic"}).status_code == 401
        path = f"/rag/documents/{uuid4()}/index"
        for method in (client.get, client.post, client.delete):
            assert method(path).status_code == 401


def test_rag_migration_is_additive():
    path = Path(__file__).parents[1] / "alembic/versions/0003_personal_rag.py"
    spec = importlib.util.spec_from_file_location("rag_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
        },
    )
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue().upper()
    assert sql.count("CREATE TABLE") == 2
    assert not re.search(r"(?m)^\s*(DROP|ALTER TABLE|TRUNCATE|DELETE)\b", sql)
    assert module.down_revision == "0002_documents"
