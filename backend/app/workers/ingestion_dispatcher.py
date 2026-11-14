"""Durable outbox publisher embedded in the existing Celery worker process."""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import database_url
from app.documents.models import Document, DocumentIngestionJob
from app.documents.processing import fail_attempt

logger = logging.getLogger(__name__)


def dispatch_once(engine: Engine, publish: Callable[[UUID], None], settings: Settings) -> bool:
    now, token = datetime.now(UTC), uuid4()
    with Session(engine, expire_on_commit=False) as db:
        job = db.scalar(
            select(DocumentIngestionJob)
            .where(
                DocumentIngestionJob.status.in_(("pending", "published", "processing")),
                DocumentIngestionJob.available_at <= now,
                or_(
                    DocumentIngestionJob.lease_expires_at.is_(None),
                    DocumentIngestionJob.lease_expires_at <= now,
                ),
            )
            .order_by(DocumentIngestionJob.available_at, DocumentIngestionJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False
        if job.attempts >= settings.documents_max_attempts:
            document = db.scalar(
                select(Document).where(Document.id == job.document_id).with_for_update(),
            )
            assert document is not None
            fail_attempt(db, job, document, "RETRY_EXHAUSTED", settings, transient=False)
            db.commit()
            return True
        job.status = "pending"
        job.attempts += 1  # Reserve the attempt before publication, including lost deliveries.
        job.lease_token = token
        job.lease_expires_at = now + timedelta(seconds=30)
        job.updated_at = now
        job_id = job.id
        db.commit()
    failed = False
    try:
        publish(job_id)
    except Exception:
        failed = True
    with Session(engine) as db:
        job = db.scalar(
            select(DocumentIngestionJob)
            .where(DocumentIngestionJob.id == job_id, DocumentIngestionJob.lease_token == token)
            .with_for_update()
        )
        if job is None:
            return True  # A fast consumer already owns or completed the job.
        job.lease_token = job.lease_expires_at = None
        job.updated_at = datetime.now(UTC)
        if failed:
            document = db.scalar(
                select(Document).where(Document.id == job.document_id).with_for_update(),
            )
            assert document is not None
            fail_attempt(db, job, document, "QUEUE_UNAVAILABLE", settings, transient=True)
        else:
            job.status = "published"
            # No delivery/worker acknowledgement: publish again after a bounded visibility window.
            job.available_at = datetime.now(UTC) + timedelta(
                seconds=settings.documents_processing_seconds + 120
            )
        db.commit()
    return True


class OutboxDispatcher:
    def __init__(self, settings: Settings, publish: Callable[[UUID], None]) -> None:
        self.settings, self.publish = settings, publish
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="document-outbox", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)

    def run(self) -> None:
        # Created after worker_ready/fork; never share live parent DB connections with children.
        engine = create_engine(
            database_url(self.settings),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000 -c lock_timeout=5000",
            },
        )
        try:
            while not self.stopped.is_set():
                try:
                    for _ in range(20):
                        if self.stopped.is_set() or not dispatch_once(
                            engine, self.publish, self.settings
                        ):
                            break
                except Exception:
                    # Never print driver parameters or document data; DB is authoritative.
                    logger.warning("document_outbox_unavailable")
                self.stopped.wait(self.settings.documents_outbox_interval)
        finally:
            engine.dispose()
