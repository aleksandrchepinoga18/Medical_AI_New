"""Compare existing flat-text extraction against PyMuPDF's table detector."""

import re
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

import pymupdf

from app.chat.lab_tables import column_positions, split_cells, table_measurements
from app.documents.extract import extract_pdf
from evaluation.extraction_fixtures import Evidence

NUMBER = re.compile(r"([<>≤≥]?)\s*([+-]?[0-9]+(?:[.,][0-9]+)?)")


class TextLocator(TypedDict):
    start: int
    end: int
    index: int


def header_positions(cells: list[str]) -> dict[str, int]:
    positions = column_positions(cells)
    if positions:
        dates = [
            index for index, cell in enumerate(cells)
            if cell.casefold() in {"дата", "date"}
        ]
        if len(dates) == 1:
            positions["date"] = dates[0]
    return positions


def table_record(
    cells: list[str], columns: dict[str, int], page: int, quote: str
) -> Evidence | None:
    if max(columns.values()) >= len(cells):
        return None
    fields = {key: cells[index] or None for key, index in columns.items()}
    name, value = fields["name"], fields["value"]
    if not name or not value or not NUMBER.fullmatch(value):
        return None
    return Evidence(
        name=name, value=value, unit=fields["unit"], reference=fields.get("reference"),
        date=fields.get("date"), page=page, quote=quote,
    )


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
            records: list[Evidence] = []
            for strategy in ("lines", "text"):
                for table in page.find_tables(strategy=strategy).tables:
                    columns: dict[str, int] = {}
                    table_records = []
                    for geometry, row in zip(table.rows, table.extract(), strict=True):
                        cells = [" ".join((cell or "").split()) for cell in row]
                        header = header_positions(cells)
                        if header:
                            columns = header
                        elif columns:
                            quote = " | ".join(cell for cell in cells if cell)
                            record = table_record(cells, columns, page.number + 1, quote)
                            if record is not None:
                                x0, y0, x1, y1 = geometry.bbox
                                record.bbox = (x0, y0, x1, y1)
                                table_records.append(record)
                    if table_records:
                        counts[strategy] += 1
                        records.extend(table_records)
                if records:
                    break
            if not records:
                counts["pages_without_records"] += 1
            found.extend(records)
    return found, counts
