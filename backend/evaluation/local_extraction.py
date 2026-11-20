"""Compare existing flat-text extraction against PyMuPDF's table detector."""

from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

import pymupdf

from app.chat.lab_tables import split_cells, table_measurements
from app.documents.extract import extract_pdf
from app.documents.extraction_data import Evidence
from app.documents.tables import header_positions, page_measurements, table_record


class TextLocator(TypedDict):
    start: int
    end: int
    index: int


def flat_text(path: Path) -> tuple[list[Evidence], dict[str, object]]:
    extraction = extract_pdf(path, max_pages=200, max_chars=1_000_000)
    found = []
    full_text = cast(str, extraction["text"])
    for source in cast(list[TextLocator], extraction["locators"]):
        text = full_text[source["start"] : source["end"]]
        accepted = Counter(line for line, _ in table_measurements(text))
        columns: dict[str, int] = {}
        for raw in text.splitlines():
            cells = split_cells(raw)
            header = header_positions(cells)
            if header:
                columns = header
                continue
            line = raw.strip()
            if columns and accepted[line]:
                accepted[line] -= 1
                record = table_record(cells, columns, source["index"], line)
                if record is not None:
                    found.append(record)
    return found, extraction


def native_tables(path: Path) -> tuple[list[Evidence], dict[str, int]]:
    found: list[Evidence] = []
    counts = {"lines": 0, "text": 0, "pages_without_records": 0}
    with pymupdf.open(path) as pdf:
        for page in pdf:
            records, strategy = page_measurements(page)
            counts[strategy or "pages_without_records"] += 1
            found.extend(records)
    return found, counts
