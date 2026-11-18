"""Explicit, repeatable backfill for existing ready RAG generations."""

import sys

from app.db import get_engine, get_settings
from app.rag.adapters import QdrantVectors
from app.rag.bm25_index import backfill
from app.rag.hybrid import QdrantBm25


def main() -> None:
    settings = get_settings()
    if sys.argv[1:] != ["populate"] or not settings.rag_enabled:
        raise RuntimeError("Enable RAG and pass explicit populate argument")
    documents, chunks = backfill(get_engine(), settings, QdrantBm25(QdrantVectors(settings)))
    print(f"Current documents indexed: {documents}. Sparse points written: {chunks}.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "BM25 backfill stopped. Verify Qdrant and PostgreSQL privately, then rerun.",
            file=sys.stderr,
        )
        sys.exit(1)
