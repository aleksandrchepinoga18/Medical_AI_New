"""Explicit, repeatable backfill for existing ready RAG generations."""

import sys

from app.db import get_engine, get_settings
from app.rag.adapters import QdrantVectors, RagError
from app.rag.bm25_index import backfill


def main() -> None:
    settings = get_settings()
    if sys.argv[1:] != ["populate"] or not settings.rag_enabled:
        raise RuntimeError("Enable RAG and pass explicit populate argument")
    documents, chunks = backfill(get_engine(), settings, QdrantVectors(settings))
    print(f"Current documents indexed: {documents}. Hybrid points verified: {chunks}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = error.code if isinstance(error, RagError) else "HYBRID_BACKFILL_FAILED"
        print(
            f"Hybrid backfill stopped ({code}). Verify Qdrant and PostgreSQL, then rerun.",
            file=sys.stderr,
        )
        sys.exit(1)
