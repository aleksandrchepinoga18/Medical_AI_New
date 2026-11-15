"""Character offsets refer to the immutable normalized text from stage 3."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5


@dataclass(frozen=True)
class Chunk:
    id: UUID
    ordinal: int
    start: int
    end: int
    text: str
    sources: list[dict[str, Any]]


def chunk_text(
    version_id: UUID,
    text: str,
    locators: list[dict[str, Any]],
    size: int,
    overlap: int,
) -> list[Chunk]:
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk configuration")
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if text[start:end].strip():
            sources = [
                dict(locator)
                for locator in locators
                if isinstance(locator.get("start"), int)
                and isinstance(locator.get("end"), int)
                and locator["start"] < end
                and locator["end"] > start
            ]
            chunks.append(
                Chunk(
                    uuid5(version_id, f"chars-v1:{size}:{overlap}:{start}:{end}"),
                    len(chunks),
                    start,
                    end,
                    text[start:end],
                    sources,
                )
            )
        if end == len(text):
            break
        start = end - overlap
    return chunks
