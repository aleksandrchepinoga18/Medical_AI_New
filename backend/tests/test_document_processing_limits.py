import hashlib
import subprocess
from contextlib import contextmanager
from io import BytesIO
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.documents.models import Document
from app.documents.processing import ProcessingError, extract_original


class SyntheticStorage:
    @contextmanager
    def open_original(self, *args):
        with BytesIO(b"%PDF-synthetic-only") as source:
            yield source


def document() -> Document:
    payload = b"%PDF-synthetic-only"
    return Document(
        id=uuid4(),
        owner_id=uuid4(),
        object_key="unused-by-synthetic-storage",
        format="pdf",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_parser_timeout_is_bounded_and_has_no_captured_document_output(monkeypatch) -> None:
    settings = Settings(redis_password="synthetic-unused")
    calls = []

    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("synthetic parser", 1)

    monkeypatch.setattr("app.documents.processing.subprocess.run", timeout)
    with pytest.raises(ProcessingError, match="PROCESSING_TIMEOUT"):
        extract_original(document(), SyntheticStorage(), settings)
    assert 0 < calls[0]["timeout"] <= settings.documents_processing_seconds
    assert calls[0]["stdout"] == subprocess.DEVNULL
    assert calls[0]["stderr"] == subprocess.DEVNULL
    sensitive = {"POSTGRES_PASSWORD", "REDIS_PASSWORD", "DOCUMENTS_S3_SECRET_KEY"}
    assert not sensitive & calls[0]["env"].keys()


def test_checksum_mismatch_never_reaches_parser(monkeypatch) -> None:
    original = document()
    original.sha256 = "0" * 64
    calls = []
    monkeypatch.setattr(
        "app.documents.processing.subprocess.run", lambda *args, **kwargs: calls.append(True)
    )
    with pytest.raises(ProcessingError, match="CHECKSUM_MISMATCH"):
        extract_original(original, SyntheticStorage(), Settings(redis_password="synthetic-unused"))
    assert not calls
