from contextlib import AbstractContextManager
from uuid import UUID

from app.integrations.object_storage import (
    BinaryReader,
    ObjectResponse,
    ObjectStorage,
    StoredOriginal,
)


def knowledge_key(source_id: UUID, version_id: UUID) -> str:
    return f"knowledge/{source_id}/versions/{version_id}/original"


class KnowledgeStorage(ObjectStorage):
    def upload_version(
        self, source_id: UUID, version_id: UUID, file: BinaryReader, format_: str,
    ) -> StoredOriginal:
        return self._upload(knowledge_key(source_id, version_id), file, format_)

    def open_version(
        self, source_id: UUID, version_id: UUID,
    ) -> AbstractContextManager[ObjectResponse]:
        return self._open(knowledge_key(source_id, version_id))
