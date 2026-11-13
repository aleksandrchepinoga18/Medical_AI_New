"""Celery configuration; no business tasks are registered yet."""

from celery import Celery

from app.core.config import Settings

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
)
