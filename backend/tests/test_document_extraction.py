from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pymupdf
import pytest
from docx import Document
from fastapi import HTTPException

from app.core.config import Settings
from app.documents.extract import ExtractionError, TextBuilder, extract_docx, extract_pdf
from app.documents.validation import inspect_upload


def test_pdf_page_locators_limits_and_scan_state(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Synthetic page one")
        document.new_page()
        document.save(path)
    result = extract_pdf(path, 2, 1000)
    assert result["status"] == "needs_ocr"
    assert "Synthetic page one" in result["text"]
    locator = result["locators"][0]
    assert locator["kind"] == "page" and locator["index"] == 1
    with pytest.raises(ExtractionError, match="PAGE_LIMIT"):
        extract_pdf(path, 1, 1000)
    with pytest.raises(ExtractionError, match="TEXT_LIMIT"):
        extract_pdf(path, 2, 3)


def test_docx_preserves_block_order_and_limits(tmp_path: Path) -> None:
    document = Document()
    document.add_paragraph("Synthetic first")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Synthetic table"
    document.add_paragraph("Synthetic last")
    path = tmp_path / "synthetic.docx"
    document.save(path)
    result = extract_docx(path, 1000, 1000000)
    assert result["text"] == "Synthetic first\nSynthetic table\nSynthetic last"
    assert result["status"] == "ready"
    with pytest.raises(ExtractionError, match="ARCHIVE_LIMIT"):
        extract_docx(path, 1000, 10)


def test_normalized_locator_offsets() -> None:
    builder = TextBuilder(100)
    builder.add(" e\u0301\x00 ", "paragraph", 1)
    builder.add(" second ", "paragraph", 2)
    result = builder.result()
    for locator in builder.locators:
        assert result["text"][locator["start"] : locator["end"]] in {"é", "second"}


def test_format_check_and_filename_safety() -> None:
    settings = Settings(redis_password="synthetic-unused")
    metadata = inspect_upload(BytesIO(b"%PDF-synthetic"), "../../unsafe\x00.pdf", settings)
    assert metadata.filename == "unsafe.pdf"
    with pytest.raises(HTTPException) as error:
        inspect_upload(BytesIO(b"not PDF"), "document.pdf", settings)
    assert error.value.status_code == 415
    zipped = BytesIO()
    with ZipFile(zipped, "w") as archive:
        archive.writestr("unexpected.txt", "synthetic")
    with pytest.raises(HTTPException):
        inspect_upload(zipped, "document.docx", settings)
