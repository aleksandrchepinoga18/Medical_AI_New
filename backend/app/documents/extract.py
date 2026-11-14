"""Isolated parser process: no network clients, no medical text in stdout/stderr."""

import json
import os
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile


class ExtractionError(Exception):
    pass


@dataclass
class TextBuilder:
    maximum: int
    parts: list[str] = field(default_factory=list)
    locators: list[dict[str, str | int]] = field(default_factory=list)
    length: int = 0

    def add(self, value: str, kind: str, index: int) -> None:
        value = unicodedata.normalize("NFC", value.replace("\x00", "")).strip()
        if not value:
            return
        separator = 1 if self.parts else 0
        if self.length + separator + len(value) > self.maximum or len(self.locators) >= 10000:
            raise ExtractionError("TEXT_LIMIT")
        start = self.length + separator
        self.parts.append(value)
        self.length = start + len(value)
        self.locators.append({"kind": kind, "index": index, "start": start, "end": self.length})

    def result(self, *, needs_ocr: bool = False) -> dict[str, object]:
        return {
            "text": "\n".join(self.parts),
            "locators": self.locators,
            "status": "needs_ocr" if needs_ocr else "ready",
        }


def extract_pdf(path: Path, max_pages: int, max_chars: int) -> dict[str, object]:
    import pymupdf

    builder = TextBuilder(max_chars)
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ExtractionError("PASSWORD_PROTECTED")
        if document.page_count > max_pages:
            raise ExtractionError("PAGE_LIMIT")
        empty_page = False
        for number, page in enumerate(document):
            value = page.get_text("text", sort=True)
            if not isinstance(value, str):
                raise ExtractionError("INVALID_DOCUMENT")
            empty_page = empty_page or not value.strip()
            builder.add(value, "page", number + 1)
    # Without OCR a blank or scanned page cannot be claimed as fully processed.
    return builder.result(needs_ocr=empty_page or not builder.parts)


def extract_docx(path: Path, max_chars: int, max_unpacked: int) -> dict[str, object]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(item.file_size for item in entries) > max_unpacked:
            raise ExtractionError("ARCHIVE_LIMIT")
        if not {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist()):
            raise ExtractionError("INVALID_DOCUMENT")
        for item in entries:
            if item.flag_bits & 1 or "vbaproject" in item.filename.lower():
                raise ExtractionError("UNSUPPORTED_DOCUMENT")
            if item.filename.lower().endswith((".xml", ".rels")):
                value = archive.read(item).upper()
                if b"<!DOCTYPE" in value or b"<!ENTITY" in value:
                    raise ExtractionError("UNSUPPORTED_DOCUMENT")
    builder = TextBuilder(max_chars)
    document = Document(str(path))
    # OOXML order, not separate paragraphs/tables lists. No guessed DOCX page numbers.
    for index, block in enumerate(document.iter_inner_content(), 1):
        if isinstance(block, Paragraph):
            builder.add(block.text, "paragraph", index)
        elif isinstance(block, Table):
            for row_index, row in enumerate(block.rows, 1):
                builder.add("\t".join(cell.text for cell in row.cells), f"table:{index}", row_index)
    if not builder.parts:
        raise ExtractionError("NO_TEXT")
    return builder.result()


def main() -> None:
    source, format_, output, pages, chars, unpacked, seconds, parent_pid = sys.argv[1:]
    if sys.platform.startswith("linux"):
        import ctypes
        import signal

        # Kill the parser if Celery's hard timeout/crash kills its parent process.
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError("Cannot bind parser lifetime to worker")
        if os.getppid() != int(parent_pid):
            os._exit(1)
    if sys.platform != "win32":
        import resource

        memory = 768 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (int(seconds), int(seconds) + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        output_limit = int(chars) * 12 + 2000000
        resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit, output_limit))
    try:
        if format_ == "pdf":
            result = extract_pdf(Path(source), int(pages), int(chars))
        elif format_ == "docx":
            result = extract_docx(Path(source), int(chars), int(unpacked))
        else:
            raise ExtractionError("UNSUPPORTED_DOCUMENT")
    except ExtractionError as error:
        result = {"error_code": str(error)}
    except MemoryError:
        result = {"error_code": "RESOURCE_LIMIT"}
    except Exception:
        result = {"error_code": "INVALID_DOCUMENT"}
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
