"""Small REST adapters. Local Ollama only, no SDK fallback or automatic model pull."""

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator
from typing import Any, ClassVar
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid5

from app.core.config import Settings
from app.rag.chunking import Chunk

PIPELINE = "medical-ai-personal-v1"
PERSONAL_PAYLOAD_INDEXES = (
    "pipeline", "user_id", "document_id", "text_version_id",
    "generation", "fingerprint", "chunk_id",
)


class RagError(Exception):
    def __init__(self, code: str = "RAG_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise RagError("LOCAL_ENDPOINT_REQUIRED")


def fingerprint(settings: Settings) -> str:
    values = [
        PIPELINE,
        settings.rag_embedding_model,
        settings.rag_embedding_digest,
        settings.rag_embedding_dimensions,
        settings.rag_chunk_chars,
        settings.rag_chunk_overlap,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class JsonHttp:
    def __init__(self, url: str, timeout: int, key: str = "") -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname
            not in {
                "ollama",
                "qdrant",
                "host.docker.internal",
                "localhost",
                "127.0.0.1",
            }
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RagError("LOCAL_ENDPOINT_REQUIRED")
        self.url, self.timeout, self.key = url.rstrip("/"), timeout, key

    def request(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["api-key"] = self.key
        request = Request(
            self.url + path,
            method=method,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            # Ignore ambient HTTP proxies. Do not log requests, responses or document text.
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                request,
                timeout=self.timeout,
            ) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise RagError("RESPONSE_LIMIT")
                return json.loads(data)
        except HTTPError as error:
            code = error.code
            error.close()
            raise RagError(f"HTTP_{code}") from None
        except RagError:
            raise
        except Exception:
            raise RagError() from None


class OllamaEmbeddings:
    def __init__(self, settings: Settings) -> None:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_.:-]+", settings.rag_embedding_model)
            or "cloud" in settings.rag_embedding_model.lower()
        ):
            raise RagError("LOCAL_MODEL_REQUIRED")
        self.settings = settings
        self.http = JsonHttp(settings.rag_ollama_url, settings.rag_http_timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > self.settings.rag_embedding_batch_size:
            raise RagError("INVALID_EMBEDDING_BATCH")
        if self.settings.rag_embedding_digest:
            models = self.http.request("GET", "/api/tags").get("models", [])
            if not any(
                model.get("name") == self.settings.rag_embedding_model
                and model.get("digest") == self.settings.rag_embedding_digest
                for model in models
            ):
                raise RagError("EMBEDDING_MODEL_CHANGED")
        result = self.http.request(
            "POST",
            "/api/embed",
            {
                "model": self.settings.rag_embedding_model,
                "input": texts,
                "truncate": False,
            },
        )
        vectors = result.get("embeddings") if isinstance(result, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RagError("INVALID_EMBEDDING_RESPONSE")
        checked: list[list[float]] = []
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.settings.rag_embedding_dimensions
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in vector
                )
            ):
                raise RagError("EMBEDDING_DIMENSION_MISMATCH")
            values = [float(v) for v in vector]
            if not any(values):
                raise RagError("ZERO_EMBEDDING")
            checked.append(values)
        return checked


def embed_chunk_batches(
    embeddings: OllamaEmbeddings,
    chunks: list[Chunk],
    batch_size: int,
    check_deadline: Callable[[], None],
) -> Iterator[tuple[list[Chunk], list[list[float]]]]:
    for offset in range(0, len(chunks), batch_size):
        check_deadline()
        batch = chunks[offset : offset + batch_size]
        yield batch, embeddings.embed([chunk.text for chunk in batch])


def match(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "match": {"value": value}}


def generation_filter(settings: Settings, user_id: UUID, generations: list[str]) -> dict[str, Any]:
    return {
        "must": [
            match("pipeline", PIPELINE),
            match("user_id", str(user_id)),
            match("fingerprint", fingerprint(settings)),
            {"key": "generation", "match": {"any": generations}},
        ],
    }


def ensure_payload_indexes(
    http: JsonHttp,
    path: str,
    info: dict[str, Any],
    fields: tuple[str, ...] = PERSONAL_PAYLOAD_INDEXES,
) -> None:
    schema = info.get("payload_schema", {})
    for field in fields:
        if field in schema:
            if schema[field].get("data_type") != "keyword":
                raise RagError("PAYLOAD_INDEX_MISMATCH")
        else:
            http.request(
                "PUT",
                path + "/index?wait=true",
                {"field_name": field, "field_schema": "keyword"},
            )


class QdrantVectors:
    payload_indexes: ClassVar[tuple[str, ...]] = PERSONAL_PAYLOAD_INDEXES

    def __init__(self, settings: Settings) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", settings.rag_collection):
            raise RagError("INVALID_COLLECTION")
        self.settings = settings
        self.path = f"/collections/{settings.rag_collection}"
        self.http = JsonHttp(
            settings.rag_qdrant_url,
            settings.rag_http_timeout,
            settings.rag_qdrant_api_key.get_secret_value(),
        )

    def inspect(self) -> dict[str, Any]:
        try:
            result = self.http.request("GET", self.path)["result"]
            vectors = result["config"]["params"]["vectors"]
            if not isinstance(vectors, dict):
                raise RagError("COLLECTION_DIMENSION_MISMATCH")
        except (KeyError, TypeError):
            raise RagError("INVALID_COLLECTION_RESPONSE") from None
        if (
            vectors.get("size") != self.settings.rag_embedding_dimensions
            or vectors.get("distance") != "Cosine"
        ):
            raise RagError("COLLECTION_DIMENSION_MISMATCH")
        return dict(result)

    def schema(self) -> dict[str, Any]:
        return {
            "vectors": {
                "size": self.settings.rag_embedding_dimensions,
                "distance": "Cosine",
            },
        }

    def ensure(self) -> None:
        try:
            info = self.inspect()
        except RagError as error:
            if error.code != "HTTP_404":
                raise
            try:
                self.http.request("PUT", self.path, self.schema())
            except RagError as conflict:
                if conflict.code not in {"HTTP_400", "HTTP_409"}:
                    raise
            info = self.inspect()
        ensure_payload_indexes(self.http, self.path, info, self.payload_indexes)

    def upsert(
        self,
        user_id: UUID,
        document_id: UUID,
        version_id: UUID,
        generation: UUID,
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise RagError("INVALID_VECTOR_BATCH")
        self.http.request(
            "PUT",
            self.path + "/points?wait=true",
            {
                "points": [
                    {
                        "id": str(uuid5(chunk.id, str(generation))),
                        "vector": vector,
                        "payload": {
                            "pipeline": PIPELINE,
                            "user_id": str(user_id),
                            "document_id": str(document_id),
                            "text_version_id": str(version_id),
                            "generation": str(generation),
                            "fingerprint": fingerprint(self.settings),
                            "chunk_id": str(chunk.id),
                            "ordinal": chunk.ordinal,
                            "sources": chunk.sources,
                        },
                    }
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ],
            },
        )

    def delete_document(
        self,
        user_id: UUID,
        document_id: UUID,
        *,
        keep_generation: UUID | None = None,
    ) -> None:
        scoped: dict[str, Any] = {
            "must": [
                match("pipeline", PIPELINE),
                match("user_id", str(user_id)),
                match("document_id", str(document_id)),
            ],
        }
        if keep_generation is not None:
            scoped["must_not"] = [match("generation", str(keep_generation))]
        self.http.request("POST", self.path + "/points/delete?wait=true", {"filter": scoped})

    def query(
        self,
        user_id: UUID,
        vector: list[float],
        generations: list[str],
        k: int,
    ) -> list[dict[str, Any]]:
        if not generations:
            return []
        self.inspect()  # Never create or change collections on a read path.
        result = self.http.request(
            "POST",
            self.path + "/points/query",
            {
                "query": vector,
                "limit": k,
                "with_payload": True,
                "with_vector": False,
                "filter": generation_filter(self.settings, user_id, generations),
            },
        )
        return list(result["result"]["points"])
