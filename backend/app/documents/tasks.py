from uuid import UUID

from app.db import get_engine, get_settings
from app.documents.processing import process_job
from app.integrations.object_storage import ObjectStorage


def process_document_task(job_id: str) -> None:
    settings = get_settings()
    process_job(UUID(job_id), get_engine(), ObjectStorage.from_settings(settings), settings)
