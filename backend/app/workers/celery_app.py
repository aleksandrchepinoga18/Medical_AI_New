"""Document ingestion and personal indexing share the existing broker and worker."""

from celery import Celery
from celery.signals import worker_process_init, worker_ready, worker_shutdown
from kombu import Queue

from app.core.config import Settings
from app.db import get_engine
from app.documents.tasks import process_document_task
from app.rag.indexing import process_index_task
from app.workers.ingestion_dispatcher import OutboxDispatcher

settings = Settings()
celery_app = Celery(
    "medical_ai",
    broker=settings.redis_url(0),
    backend=settings.redis_url(1),
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    result_expires=86400,
    worker_hijack_root_logger=False,
    worker_prefetch_multiplier=1,
    broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    task_queues=(Queue("celery"), Queue("document_ingestion"), Queue("document_indexing")),
)

celery_app.task(
    name="documents.process",
    queue="document_ingestion",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    time_limit=settings.documents_processing_seconds + 60,
)(process_document_task)

celery_app.task(
    name="rag.index",
    queue="document_indexing",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    time_limit=settings.rag_index_seconds + 60,
)(process_index_task)

dispatcher: OutboxDispatcher | None = None


def start_outbox(**kwargs: object) -> None:
    from uuid import UUID

    global dispatcher

    def publish(job_id: UUID) -> None:
        celery_app.send_task(
            "documents.process",
            args=[str(job_id)],
            queue="document_ingestion",
            task_id=str(job_id),
            ignore_result=True,
            retry=False,
        )

    def index_publish(job_id: UUID, token: UUID) -> None:
        celery_app.send_task(
            "rag.index",
            args=[str(job_id), str(token)],
            queue="document_indexing",
            ignore_result=True,
            retry=False,
        )

    if settings.documents_outbox_enabled:
        dispatcher = OutboxDispatcher(settings, publish, index_publish)
        dispatcher.start()


def stop_outbox(**kwargs: object) -> None:
    if dispatcher:
        dispatcher.stop()


def reset_connections(**kwargs: object) -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose(close=False)
        get_engine.cache_clear()


worker_ready.connect(start_outbox, weak=False)
worker_shutdown.connect(stop_outbox, weak=False)
worker_process_init.connect(reset_connections, weak=False)
