from typing import Any
from uuid import UUID, uuid5

from app.core.config import KNOWLEDGE_COLLECTION, Settings
from app.rag.adapters import QdrantVectors, RagError, fingerprint, match
from app.rag.chunking import Chunk
from app.rag.hybrid import QdrantHybrid, bm25

KNOWLEDGE_PIPELINE = "medical-ai-knowledge-v1"


class KnowledgeVectors(QdrantHybrid):
    payload_indexes = (
        "pipeline", "source_id", "version_id", "generation", "fingerprint", "chunk_id",
    )

    def __init__(self, settings: Settings) -> None:
        super().__init__(QdrantVectors(settings))
        self.path = f"/collections/{KNOWLEDGE_COLLECTION}"

    def scope(self, generations: list[str]) -> dict[str, Any]:
        return {
            "must": [
                match("pipeline", KNOWLEDGE_PIPELINE),
                match("fingerprint", fingerprint(self.settings)),
                {"key": "generation", "match": {"any": generations}},
            ],
        }

    def remove_old_generations(self, source_id: UUID, keep: UUID | None) -> None:
        scoped: dict[str, Any] = {
            "must": [match("pipeline", KNOWLEDGE_PIPELINE), match("source_id", str(source_id))],
        }
        if keep is not None:
            scoped["must_not"] = [match("generation", str(keep))]
        self.http.request("POST", self.path + "/points/delete?wait=true", {"filter": scoped})

    def write_batch(
        self,
        source_id: UUID,
        version_id: UUID,
        generation: UUID,
        chunks: list[Chunk],
        vectors: list[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise RagError("INVALID_VECTOR_BATCH")
        scope = {
            "pipeline": KNOWLEDGE_PIPELINE,
            "source_id": str(source_id),
            "version_id": str(version_id),
            "generation": str(generation),
            "fingerprint": fingerprint(self.settings),
        }
        points = [
            {
                "id": str(uuid5(chunk.id, str(generation))),
                "payload": {
                    **scope, "chunk_id": str(chunk.id), "ordinal": chunk.ordinal,
                    "start_char": chunk.start, "end_char": chunk.end, "locators": chunk.sources,
                },
                "vector": {"dense": vector, "bm25": bm25(chunk.text)},
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self.http.request("PUT", self.path + "/points?wait=true", {"points": points})
