from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.rag.adapters import QdrantVectors, RagError, generation_filter
from app.rag.hybrid import QdrantHybrid, bm25
from app.rag.routes import SearchRequest


def test_mode_defaults_to_dense_and_is_server_only():
    assert Settings().rag_retrieval_mode == "dense"
    with pytest.raises(ValidationError):
        Settings(rag_retrieval_mode="unknown")
    with pytest.raises(ValidationError):
        SearchRequest(query="synthetic", retrieval_mode="hybrid")


def test_bm25_input_preserves_medical_identifiers_and_internal_punctuation():
    model = bm25('«Гемоглобин» (HbA1c), TSH: BRCA1; BRAF V600E, MAP2K1 и c.68_69delAG? −5,25%.')
    assert model == {
        "model": "qdrant/bm25",
        "text": "Гемоглобин HbA1c TSH BRCA1 BRAF V600E MAP2K1 и c.68_69delAG −5,25%",
        "options": {
            "language": "none",
            "tokenizer": "whitespace",
            "lowercase": True,
            "avg_len": 160.0,
        },
    }


def test_native_query_requires_coverage_and_filters_each_prefetch(monkeypatch):
    settings = Settings()
    index = QdrantHybrid(QdrantVectors(settings))
    owner, generation = uuid4(), str(uuid4())
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"result": {"config": {"params": index.schema()}}}
        if path.endswith("/count"):
            return {"result": {"count": 2}}
        return {"result": {"points": [{"id": str(uuid4()), "score": 1.0, "payload": {}}]}}

    monkeypatch.setattr(index.http, "request", request)
    with pytest.raises(RagError, match="HYBRID_COVERAGE_INCOMPLETE"):
        index.query_hybrid(owner, [1.0], "BRCA1", [generation], 3, 5)
    assert [method for method, _, _ in calls] == ["GET", "POST"]
    assert index.query_hybrid(owner, [1.0], "BRCA1", [generation], 2, 5)
    _, path, body = calls[-1]
    assert path.endswith("_hybrid_v2/points/query")
    assert body["query"] == {"fusion": "rrf"}
    expected = generation_filter(settings, owner, [generation])
    assert body["filter"] == calls[-2][2]["filter"] == expected
    assert body["prefetch"] == [
        {"query": [1.0], "using": "dense", "limit": 5, "filter": expected},
        {"query": bm25("BRCA1"), "using": "bm25", "limit": 5, "filter": expected},
    ]
