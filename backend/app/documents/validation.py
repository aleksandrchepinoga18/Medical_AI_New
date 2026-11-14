import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException

from app.core.config import Settings


@dataclass(frozen=True)
class UploadMetadata:
    filename: str
    format: str
    size: int
    sha256: str


def inspect_upload(source: BinaryIO, filename: str, settings: Settings) -> UploadMetadata:
    safe_name = PurePosixPath(filename.replace("\\", "/")).name
    safe_name = "".join(c for c in safe_name if not unicodedata.category(c).startswith("C"))
    safe_name = safe_name.strip()
    suffix = PurePosixPath(safe_name).suffix.lower()
    if not safe_name or suffix not in {".pdf", ".docx"}:
        raise HTTPException(415, "Only PDF and DOCX are supported")
    if len(safe_name) > 255:
        safe_name = safe_name[: 255 - len(suffix)] + suffix
    source.seek(0)
    header = source.read(1024)
    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise HTTPException(415, "File contents do not match PDF")
    if suffix == ".docx":
        try:
            with ZipFile(source) as archive:
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "word/document.xml"} <= names:
                    raise HTTPException(415, "File contents do not match DOCX")
        except BadZipFile:
            raise HTTPException(415, "Invalid DOCX") from None
    source.seek(0)
    size = 0
    checksum = hashlib.sha256()
    while chunk := source.read(65536):
        size += len(chunk)
        if size > settings.documents_max_bytes:
            raise HTTPException(413, "File is too large")
        checksum.update(chunk)
    if not size:
        raise HTTPException(422, "Empty document")
    source.seek(0)
    return UploadMetadata(safe_name, suffix[1:], size, checksum.hexdigest())
