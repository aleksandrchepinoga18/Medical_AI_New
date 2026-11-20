"""Render the fixed benchmark corpus and retain ground truth from its layout."""

import json
from pathlib import Path
from typing import Literal

import pymupdf
from pydantic import BaseModel, Field

from app.documents.extraction_data import Evidence as Evidence

MANIFEST = Path(__file__).with_name("document_extraction.json")
CORPUS_VERSION = 2
HEADERS = {
    "ru": ("Показатель", "Результат", "Единицы", "Референс", "Дата"),
    "en": ("Analyte", "Result", "Units", "Reference", "Date"),
}
COLUMNS = (42, 242, 334, 440, 559, 672)


class FixturePage(BaseModel):
    layout: Literal["grid", "borderless", "wrapped", "long_header", "scan", "mixed", "plain"]
    language: Literal["ru", "en"]
    rows: list[tuple[str, str, str, str | None, str | None]]


class Fixture(BaseModel):
    id: str = Field(pattern=r"^[0-9]{2}-[a-z-]+$")
    pages: list[FixturePage] = Field(min_length=1)


def load_fixtures() -> list[Fixture]:
    manifest = json.loads(MANIFEST.read_bytes())
    fixtures = [Fixture.model_validate(item) for item in manifest["documents"]]
    if manifest["version"] != CORPUS_VERSION:
        raise ValueError("Invalid benchmark manifest")
    if len(fixtures) != 8 or len({item.id for item in fixtures}) != 8:
        raise ValueError("Expected eight distinct benchmark PDFs")
    return fixtures


def write_text(page: pymupdf.Page, x: float, y: float, text: str, size: int = 11) -> None:
    # Droid Sans Fallback has no U+00B5 glyph. WinAnsi Helvetica preserves it.
    fontname = "helv" if "µ" in text and all(ord(char) < 256 for char in text) else "BenchmarkFont"
    page.insert_text((x, y), text, fontname=fontname, fontsize=size, lineheight=1.15)


def heading(page: pymupdf.Page, spec: FixturePage, number: int) -> None:
    write_text(page, 42, 48, "SYNTHETIC FIXTURE / СИНТЕТИЧЕСКИЙ ОБРАЗЕЦ", 13)
    if spec.layout in {"long_header", "plain"}:
        lines = (
            "Учебная лаборатория. Бланк № 4202 от 2026-10-01",
            "Лицензия DEMO 77-01-2048, корпус 3, кабинет 205.",
            "Регистратура: внутренний номер 120–160. Код подразделения 5,20.",
            "Справочная информация о форме. Образец заполнения № 901.",
            "Контроль печати 99,9 %. Режим работы 08:00–18:00.",
            "Копия № 2. Идентификатор шаблона 2026. Ревизия 4.0.",
        )
        for index, line in enumerate(lines):
            write_text(page, 42, 80 + index * 17, line)
    if spec.layout == "plain":
        write_text(page, 42, 208, "В этом уведомлении результаты анализов отсутствуют.")
    write_text(page, 42, 850, f"Page / Страница {number}")


def draw_table(page: pymupdf.Page, spec: FixturePage, number: int) -> list[Evidence]:
    if not spec.rows:
        return []
    has_dates = any(row[4] is not None for row in spec.rows)
    headers = HEADERS[spec.language][: 5 if has_dates else 4]
    edges = COLUMNS[: len(headers) + 1]
    top = 224 if spec.layout == "long_header" else 100
    height = 38 if spec.layout == "wrapped" else 26
    bottom = top + height * (len(spec.rows) + 1)
    if spec.layout != "borderless":
        for x in edges:
            page.draw_line((x, top), (x, bottom), width=0.6)
        for y in range(top, bottom + 1, height):
            page.draw_line((edges[0], y), (edges[-1], y), width=0.6)
    for x, label in zip(edges[:-1], headers, strict=True):
        write_text(page, x + 7, top + 17, label)
    evidence = []
    for index, row in enumerate(spec.rows, 1):
        y = top + index * height
        for x, value in zip(edges[:-1], row[: len(headers)], strict=True):
            if value is not None:
                write_text(page, x + 7, y + 17, value)
        evidence.append(
            Evidence(
                name=row[0],
                value=row[1],
                unit=row[2],
                reference=row[3],
                date=row[4],
                page=number,
                quote=" | ".join(value for value in row if value is not None),
                bbox=(float(edges[0]), float(y), float(edges[-1]), float(y + height)),
            )
        )
    return evidence


def render(fixture: Fixture) -> tuple[bytes, list[Evidence]]:
    font = pymupdf.Font("cjk").buffer
    evidence = []
    with pymupdf.open() as pdf:
        for number, spec in enumerate(fixture.pages, 1):
            target = pdf.new_page(width=720, height=880)
            if spec.layout in {"scan", "mixed"}:
                with pymupdf.open() as raster:
                    page = raster.new_page(width=720, height=880)
                    page.insert_font(fontname="BenchmarkFont", fontbuffer=font)
                    if spec.layout == "scan":
                        heading(page, spec, number)
                        clip = page.rect
                    else:
                        target.insert_font(fontname="BenchmarkFont", fontbuffer=font)
                        heading(target, spec, number)
                        clip = pymupdf.Rect(32, 90, 682, 100 + (len(spec.rows) + 1) * 26 + 10)
                    evidence.extend(draw_table(page, spec, number))
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip)
                    target.insert_image(clip, pixmap=pixmap)
            else:
                target.insert_font(fontname="BenchmarkFont", fontbuffer=font)
                heading(target, spec, number)
                evidence.extend(draw_table(target, spec, number))
        return pdf.tobytes(deflate=True, garbage=4, no_new_id=True), evidence
