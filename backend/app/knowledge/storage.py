from collections.abc import Iterator
from contextlib import AbstractContextManager
from uuid import UUID

from app.integrations.object_storage import (
    CHUNK_BYTES,
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

    def stream_version(self, source_id: UUID, version_id: UUID) -> Iterator[bytes]:
        with self.open_version(source_id, version_id) as response:
            while chunk := response.read(CHUNK_BYTES):
                yield chunk
