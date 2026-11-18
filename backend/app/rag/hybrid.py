"""BM25 sparse vectors in a companion Qdrant collection and rank-based fusion."""

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any
from uuid import UUID, uuid5

from app.rag.adapters import (
    PIPELINE,
    QdrantVectors,
    RagError,
    ensure_payload_indexes,
    fingerprint,
    generation_filter,
    match,
)
from app.rag.models import RagChunk

FIELD = "bm25"
_TOKEN = re.compile(r"(?:[+-](?=\d))?[^\W_]+(?:[.,:/+>_-][^\W_]+)*[%‰]?")
_DECIMAL = re.compile(r"^([+-]?\d+),(\d+)([%‰]?)$")
_K1, _B, _AVG_LENGTH = 1.2, 0.75, 160


def terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.translate(str.maketrans({"−": "-", "–": "-", "‑": "-"}))
    return [_DECIMAL.sub(r"\1.\2\3", token) for token in _TOKEN.findall(normalized)]


def sparse(user_id: UUID, text: str, *, query: bool) -> dict[str, list[int] | list[float]]:
    tokens = terms(text)
    counts = Counter(
        int.from_bytes(
            hashlib.blake2s(term.encode(), key=user_id.bytes, digest_size=4).digest(), "big"
        )
        or 1
        for term in tokens
    )
    norm = _K1 * (1 - _B + _B * len(tokens) / _AVG_LENGTH)
    indices = sorted(counts)
    values = [
        1.0 if query else (counts[index] * (_K1 + 1)) / (counts[index] + norm)
        for index in indices
    ]
    # A punctuation-only chunk has no searchable terms but still counts toward coverage.
    if not indices and not query:
        return {"indices": [0], "values": [1.0]}
    return {"indices": indices, "values": values}


class QdrantBm25:
    def __init__(self, dense: QdrantVectors) -> None:
        self.settings = dense.settings
        self.http = dense.http
        self.path = dense.path + "_bm25_v1"

    def ensure(self) -> None:
        try:
            info = self.http.request("GET", self.path)["result"]
        except RagError as error:
            if error.code != "HTTP_404":
                raise
            try:
                self.http.request(
                    "PUT",
                    self.path,
                    {"vectors": {}, "sparse_vectors": {FIELD: {"modifier": "idf"}}},
                )
            except RagError as conflict:
                # Qdrant versions return either bad request or conflict for an existing name.
                if conflict.code not in {"HTTP_400", "HTTP_409"}:
                    raise
            info = self.http.request("GET", self.path)["result"]
        params = info["config"]["params"]
        sparse_field = (params.get("sparse_vectors") or {}).get(FIELD, {})
        if params.get("vectors") or sparse_field.get("modifier") != "idf":
            raise RagError("BM25_COLLECTION_MISMATCH")
        ensure_payload_indexes(self.http, self.path, info)

    def count(self, user_id: UUID, generations: list[str]) -> int:
        result = self.http.request(
            "POST",
            self.path + "/points/count",
            {"filter": generation_filter(self.settings, user_id, generations), "exact": True},
        )
        count = result.get("result", {}).get("count") if isinstance(result, dict) else None
        if type(count) is not int:
            raise RagError("INVALID_BM25_RESPONSE")
        return count

    def upsert(self, chunks: list[RagChunk], generation: UUID) -> None:
        index_fingerprint = fingerprint(self.settings)
        self.http.request(
            "PUT",
            self.path + "/points?wait=true",
            {
                "points": [
                    {
                        "id": str(uuid5(chunk.id, str(generation))),
                        "vector": {FIELD: sparse(chunk.user_id, chunk.text, query=False)},
                        "payload": {
                            "pipeline": PIPELINE,
                            "fingerprint": index_fingerprint,
                            "generation": str(generation),
                            "user_id": str(chunk.user_id),
                            "document_id": str(chunk.document_id),
                            "text_version_id": str(chunk.text_version_id),
                            "chunk_id": str(chunk.id),
                        },
                    }
                    for chunk in chunks
                ],
            },
        )

    def cleanup(self, user_id: UUID, document_id: UUID, generation: UUID | None) -> None:
        scoped = {
            "must": [
                match("pipeline", PIPELINE),
                match("user_id", str(user_id)),
                match("document_id", str(document_id)),
            ],
        }
        if generation is not None:
            scoped["must_not"] = [match("generation", str(generation))]
        try:
            self.http.request("POST", self.path + "/points/delete?wait=true", {"filter": scoped})
        except RagError as error:
            if error.code != "HTTP_404":
                raise

    def query(
        self, user_id: UUID, query: str, generations: list[str], expected: int, k: int
    ) -> list[dict[str, Any]]:
        vector = sparse(user_id, query, query=True)
        if not vector["indices"]:
            raise RagError("BM25_NO_TERMS")
        if self.count(user_id, generations) != expected:
            raise RagError("BM25_COVERAGE_INCOMPLETE")
        result = self.http.request(
            "POST",
            self.path + "/points/query",
            {
                "query": vector,
                "using": FIELD,
                "limit": k,
                "with_payload": True,
                "with_vector": False,
                "filter": generation_filter(self.settings, user_id, generations),
            },
        )
        points = result["result"]["points"]
        if not isinstance(points, list) or any(
            not isinstance(point, dict)
            or not isinstance(point.get("id"), str)
            or not isinstance(point.get("payload"), dict)
            or not isinstance(point.get("score"), (int, float))
            or not math.isfinite(point["score"])
            for point in points
        ):
            raise RagError("INVALID_BM25_RESPONSE")
        return points


def rrf(*rankings: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    items: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for item in ranking:
            chunk_id = item["chunk_id"]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1 / (60 + len(seen))
            items.setdefault(chunk_id, item)
    # Python's stable sort preserves first encounter order (dense first) on ties.
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    return [{**items[key], "score": scores[key]} for key in ranked[:k]]
