import math
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.rag.adapters import QdrantVectors, RagError, generation_filter
from app.rag.hybrid import QdrantBm25, rrf, sparse, terms
from app.rag.routes import SearchRequest


def test_mode_defaults_to_dense_and_is_server_only():
    assert Settings().rag_retrieval_mode == "dense"
    with pytest.raises(ValidationError):
        Settings(rag_retrieval_mode="unknown")
    with pytest.raises(ValidationError):
        SearchRequest(query="synthetic", retrieval_mode="hybrid")


def test_terms_preserve_identifiers_dates_values_and_units():
    assert terms("BRCA1 c.68_69delAG; 2025-01-18; 18.01.2025; −5,25 мг/л; 25%") == [
        "brca1",
        "c.68_69delag",
        "2025-01-18",
        "18.01.2025",
        "-5.25",
        "мг/л",
        "25%",
    ]
    assert terms("BRAF c.1799T>A") == ["braf", "c.1799t>a"]


def test_sparse_weights_use_bm25_tf_and_keep_tenant_terms_distinct():
    owner = UUID(int=1)
    query = sparse(owner, "ferritin ferritin", query=True)
    assert len(query["indices"]) == 1 and query["values"] == [1.0]
    assert sparse(UUID(int=2), "ferritin", query=True)["indices"] != query["indices"]
    document = sparse(owner, "ferritin ferritin", query=False)
    assert document["indices"] == query["indices"]
    assert document["values"] == pytest.approx([2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 2 / 160))])
    assert sparse(owner, "...", query=False)["indices"] == [0]
    assert sparse(owner, "...", query=True)["indices"] == []


def test_bm25_query_requires_coverage_and_uses_dense_authorization_filter(monkeypatch):
    settings = Settings()
    index = QdrantBm25(QdrantVectors(settings))
    owner, generation = uuid4(), str(uuid4())
    calls = []

    def request(method, path, body):
        calls.append((method, path, body))
        if path.endswith("/count"):
            return {"result": {"count": 2}}
        return {
            "result": {
                "points": [{"id": str(uuid4()), "score": 1.0, "payload": {"user_id": str(owner)}}]
            }
        }

    monkeypatch.setattr(index.http, "request", request)
    with pytest.raises(RagError, match="BM25_COVERAGE_INCOMPLETE"):
        index.query(owner, "BRCA1", [generation], 3, 5)
    assert len(calls) == 1
    assert index.query(owner, "BRCA1", [generation], 2, 5)
    expected_filter = generation_filter(settings, owner, [generation])
    assert all(call[2]["filter"] == expected_filter for call in calls)
    assert calls[-1][1].endswith("_bm25_v1/points/query")
    assert calls[-1][2]["using"] == "bm25"


def test_rrf_merges_by_chunk_id_without_double_counting_duplicates():
    a = {"chunk_id": "a", "score": 0.9, "source": "page-1"}
    b = {"chunk_id": "b", "score": 200}
    c = {"chunk_id": "c", "score": 5}
    result = rrf([a, b, a, c], [c, a], k=3)
    assert [hit["chunk_id"] for hit in result] == ["a", "c", "b"]
    assert math.isclose(result[0]["score"], 1 / 61 + 1 / 62)
    assert math.isclose(result[1]["score"], 1 / 63 + 1 / 61)
    assert result[0]["source"] == "page-1"
    assert rrf([a], [b], k=1)[0]["chunk_id"] == "a"
