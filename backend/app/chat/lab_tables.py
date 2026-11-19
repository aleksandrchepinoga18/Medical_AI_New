"""Recognize explicit lab columns in already extracted PDF text."""

import re
from collections.abc import Iterator
from decimal import Decimal

from app.chat.evidence import Measurement, indicator_key, normalized, unit_key

_COLUMNS = {
    "name": {
        "показатель", "исследование", "наименование", "наименование исследования",
        "analyte", "test", "test name", "parameter",
    },
    "value": {"результат", "значение", "result", "value"},
    "unit": {"ед. изм", "единица измерения", "единицы измерения", "единицы", "unit", "units"},
    "reference": {
        "референс", "референсные значения", "референсный интервал", "норма",
        "reference", "reference range", "reference interval",
    },
}
_DELIMITER = re.compile(r"\t|\||[ \u00a0]{2,}")
_NUMBER = r"[+-]?\d+(?:[.,]\d+)?"
_VALUE = re.compile(_NUMBER)
_RANGE = re.compile(
    rf"(?P<low>{_NUMBER})\s*[-–—]\s*(?P<high>{_NUMBER})(?:\s+(?P<unit>\S.*))?"
)
_UNIT_TEXT = re.compile(r"[\w µμ%/^*·×().-]{1,32}")


def column_positions(cells: list[str]) -> dict[str, int]:
    if len(cells) < 3:
        return {}
    labels = [" ".join(normalized(cell).split()).rstrip(".:") for cell in cells]
    positions: dict[str, int] = {}
    for role, aliases in _COLUMNS.items():
        matches = [index for index, label in enumerate(labels) if label in aliases]
        if len(matches) > 1:
            return {}
        if matches:
            positions[role] = matches[0]
    return positions if {"name", "value", "unit"} <= positions.keys() else {}


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in _DELIMITER.split(line.strip(" \u00a0"))]


def has_table_header(text: str) -> bool:
    return any(column_positions(split_cells(line)) for line in text.splitlines())


def recognized_unit(text: str) -> str:
    try:
        return unit_key(normalized(text))
    except KeyError:
        return ""


def table_measurements(text: str) -> Iterator[tuple[str, Measurement]]:
    """Trust each value only within a complete row under an unambiguous header."""
    columns: dict[str, int] = {}
    width = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cells = split_cells(raw)
        header = column_positions(cells)
        if header:
            columns, width = header, len(cells)
            continue
        if not columns:
            continue
        if len(cells) != width:
            columns = {}
            continue
        label, raw_value, raw_unit = (cells[columns[role]] for role in ("name", "value", "unit"))
        value, unit_label = normalized(raw_value), normalized(raw_unit)
        if (
            not (label and len(label) <= 120 and any(char.isalpha() for char in label))
            or not _VALUE.fullmatch(value)
            or not _UNIT_TEXT.fullmatch(unit_label)
            or not any(char.isalpha() or char == "%" for char in unit_label)
        ):
            columns = {}
            continue
        number = Decimal(value.replace(",", "."))
        unit = recognized_unit(raw_unit)
        status = "сопоставимый референсный интервал не подтверждён"
        reference = cells[columns["reference"]] if "reference" in columns else ""
        interval = _RANGE.fullmatch(normalized(reference))
        if unit and interval and (
            interval["unit"] is None or recognized_unit(interval["unit"]) == unit
        ):
            low = Decimal(interval["low"].replace(",", "."))
            high = Decimal(interval["high"].replace(",", "."))
            if low <= high:
                if number < low:
                    status = "ниже указанного интервала"
                elif number > high:
                    status = "выше указанного интервала"
                else:
                    status = "в пределах указанного интервала"
        indicator = indicator_key(label) or "table:" + " ".join(normalized(label).split())
        yield line, Measurement(indicator, label, number, unit, status)
