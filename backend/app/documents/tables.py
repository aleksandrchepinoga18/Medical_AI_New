"""Extract structured rows using PyMuPDF's native table geometry."""

import re

import pymupdf
from pydantic import ValidationError

from app.chat.lab_tables import column_positions
from app.documents.extraction_data import Evidence, safe_source_strings

NUMBER = re.compile(r"([<>≤≥]?)\s*([+-]?[0-9]+(?:[.,][0-9]+)?)")


def header_positions(cells: list[str]) -> dict[str, int]:
    headings = [" ".join(cell.split()) for cell in cells]
    positions = column_positions(headings)
    if positions:
        dates = [
            index for index, cell in enumerate(headings)
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
    try:
        record = Evidence(
            name=name, value=value, unit=fields["unit"], reference=fields.get("reference"),
            date=fields.get("date"), page=page, quote=quote,
        )
        return record if safe_source_strings(record) else None
    except ValidationError:
        return None


def page_measurements(page: pymupdf.Page) -> tuple[list[Evidence], str | None]:
    for strategy in ("lines", "text"):
        records = []
        for table in page.find_tables(strategy=strategy).tables:
            columns: dict[str, int] = {}
            for geometry, row in zip(table.rows, table.extract(), strict=True):
                cells = [(cell or "").strip() for cell in row]
                header = header_positions(cells)
                if header:
                    columns = header
                elif columns:
                    quote = " | ".join(cell for cell in cells if cell)
                    record = table_record(cells, columns, page.number + 1, quote)
                    if record is not None:
                        x0, y0, x1, y1 = geometry.bbox
                        record.bbox = (x0, y0, x1, y1)
                        records.append(record)
        if records:
            return records, strategy
    return [], None
