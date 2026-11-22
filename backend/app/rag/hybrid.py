"""Named vectors and server-side BM25/RRF for Qdrant 1.15.5."""

import math
import re
from typing import Any
from uuid import UUID

from app.rag.adapters import QdrantVectors, RagError, generation_filter

_SURROUNDING_PUNCTUATION = re.compile(
    r"""(?<!\S)[(\[{"'«„“]+|[.,;:!?…)\]}"'»”]+(?!\S)"""
)


def bm25(text: str) -> dict[str, Any]:
    # Trim prose punctuation around terms while preserving internal dots and underscores.
    return {
        "text": _SURROUNDING_PUNCTUATION.sub("", text),
        "model": "qdrant/bm25",
        "options": {
            "language": "none",
            "tokenizer": "whitespace",
            "lowercase": True,
            "avg_len": 160.0,
        },
    }


class QdrantHybrid(QdrantVectors):
    def __init__(self, dense: QdrantVectors) -> None:
        self.settings = dense.settings
        self.http = dense.http
        self.path = dense.path + "_hybrid_v2"

    def schema(self) -> dict[str, Any]:
        return {
            "vectors": {
                "dense": {"size": self.settings.rag_embedding_dimensions, "distance": "Cosine"},
            },
            "sparse_vectors": {"bm25": {"modifier": "idf"}},
        }

    def inspect(self) -> dict[str, Any]:
        try:
            result = self.http.request("GET", self.path)["result"]
            params = result["config"]["params"]
            vectors, sparse_vectors = params["vectors"], params["sparse_vectors"]
            if set(vectors) != {"dense"} or set(sparse_vectors) != {"bm25"}:
                raise RagError("HYBRID_COLLECTION_MISMATCH")
            dense, sparse = vectors["dense"], sparse_vectors["bm25"]
            expected = self.schema()["vectors"]["dense"]
            if any(dense.get(key) != value for key, value in expected.items()):
                raise RagError("HYBRID_COLLECTION_MISMATCH")
            if sparse.get("modifier") != "idf":
                raise RagError("HYBRID_COLLECTION_MISMATCH")
        except (KeyError, TypeError, AttributeError):
            raise RagError("HYBRID_COLLECTION_MISMATCH") from None
        return dict(result)

    def count(self, user_id: UUID, generations: list[str]) -> int:
        return self.count_scoped(generation_filter(self.settings, user_id, generations))

    def count_scoped(self, scoped: dict[str, Any]) -> int:
        result = self.http.request(
            "POST",
            self.path + "/points/count",
            {"filter": scoped, "exact": True},
        )
        try:
            count = result["result"]["count"]
        except (KeyError, TypeError):
            raise RagError("INVALID_HYBRID_RESPONSE") from None
        if type(count) is not int:
            raise RagError("INVALID_HYBRID_RESPONSE")
        return count

    def query_hybrid(
        self,
        user_id: UUID,
        vector: list[float],
        text: str,
        generations: list[str],
        expected: int,
        k: int,
    ) -> list[dict[str, Any]]:
        scoped = generation_filter(self.settings, user_id, generations)
        return self.query_scoped(vector, text, scoped, expected, k)

    def query_scoped(
        self,
        vector: list[float],
        text: str,
        scoped: dict[str, Any],
        expected: int,
        k: int,
    ) -> list[dict[str, Any]]:
        self.inspect()
        if self.count_scoped(scoped) != expected:
            raise RagError("HYBRID_COVERAGE_INCOMPLETE")
        result = self.http.request(
            "POST",
            self.path + "/points/query",
            {
                "prefetch": [
                    {"query": vector, "using": "dense", "filter": scoped, "limit": k},
                    {"query": bm25(text), "using": "bm25", "filter": scoped, "limit": k},
                ],
                "query": {"fusion": "rrf"},
                "filter": scoped,
                "limit": k,
                "with_payload": True,
                "with_vector": False,
            },
        )
        try:
            points = result["result"]["points"]
        except (KeyError, TypeError):
            raise RagError("INVALID_HYBRID_RESPONSE") from None
        if not isinstance(points, list) or any(
            not isinstance(point, dict)
            or not isinstance(point.get("id"), str)
            or not isinstance(point.get("payload"), dict)
            or not isinstance(point.get("score"), (int, float))
            or not math.isfinite(point["score"])
            for point in points
        ):
            raise RagError("INVALID_HYBRID_RESPONSE")
        return points
