"""Explicit additive scheduling of existing ready documents; no remote calls."""

import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine, get_settings
from app.documents.models import Document
from app.rag.service import enqueue, latest_version


def main() -> None:
    if sys.argv[1:] != ["schedule"] or not get_settings().rag_enabled:
        raise RuntimeError("Enable RAG and pass explicit schedule argument")
    # Bound memory and transactions. Existing jobs (including deleted/failed) are not reset.
    scheduled = 0
    last_id: UUID | None = None
    while True:
        with Session(get_engine()) as db:
            query = select(Document.id).where(Document.status == "ready")
            if last_id is not None:
                query = query.where(Document.id > last_id)
            document_ids = list(db.scalars(query.order_by(Document.id).limit(1000)))
        if not document_ids:
            break
        for document_id in document_ids:
            with Session(get_engine()) as db:
                document = db.scalar(
                    select(Document)
                    .where(
                        Document.id == document_id,
                        Document.status == "ready",
                    )
                    .with_for_update()
                )
                if document is None:
                    continue
                version = latest_version(db, document.id)
                if version is not None:
                    enqueue(db, document, version)
                    scheduled += 1
                db.commit()
        last_id = document_ids[-1]
    print(f"Ready documents considered: {scheduled}. Existing index jobs preserved.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("RAG scheduling stopped; verify schema and configuration privately.", file=sys.stderr)
        sys.exit(1)
