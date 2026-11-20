"""The document extraction wire format and the evidence stored with a text version."""

import re
import unicodedata
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

FIELDS = ("name", "value", "unit", "reference", "date")
BBox = tuple[float, float, float, float]
ProviderName = Literal["pymupdf_tables", "gigachat_ultra"]
SIGNED_DECIMAL = r"[+\-−]?[0-9]+(?:[.,][0-9]+)?"
SCALAR = re.compile(rf"([<>≤≥]?)\s*({SIGNED_DECIMAL})")
RANGE = re.compile(rf"({SIGNED_DECIMAL})\s*[-–—‐‑‒−]\s*({SIGNED_DECIMAL})")
SUPERSCRIPT = re.compile(r"(?<=[0-9a-zA-Zа-яА-Я)])[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
EXPONENT_CHARS = str.maketrans("⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹", "+-0123456789")


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=240)
    value: str = Field(min_length=1, max_length=80)
    unit: str | None = Field(max_length=80)
    reference: str | None = Field(max_length=160)
    date: str | None = Field(max_length=60)
    page: int | None = Field(ge=1)
    quote: str | None = Field(max_length=1000)


def safe_source_strings(row: Measurement) -> bool:
    for field in (*FIELDS, "quote"):
        value = getattr(row, field)
        if value is not None and any(
            char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            return False
    return bool(row.name.strip() and row.value.strip())


class Measurements(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    measurements: list[Measurement] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_source_strings(self) -> Self:
        if not all(map(safe_source_strings, self.measurements)):
            raise ValueError("invalid source strings")
        return self


class Evidence(Measurement):
    bbox: BBox | None = None


class TableSource(BaseModel):
    page: int
    bbox: BBox


class StoredMeasurement(BaseModel):
    name: str
    value: str
    unit: str | None
    reference: str | None
    date: str | None
    reported_page: int | None = None
    reconstructed_row: str | None = None
    source: TableSource | None = None
    confirmation: Literal["confirmed", "ambiguous", "unverified"]


class ExtractionReport(BaseModel):
    provider: ProviderName
    provider_version: str
    model: str | None = None
    measurements: list[StoredMeasurement]
    truncated: bool = False


class StructuredExtraction(BaseModel):
    schema_version: Literal[1] = 1
    local: ExtractionReport | None = None
    external: ExtractionReport | None = None


class ExtractionResult(BaseModel):
    text: str = ""
    locators: list[dict[str, str | int]] = Field(default_factory=list)
    status: Literal["ready", "needs_ocr"] = "ready"
    error_code: str | None = None
    table_rows: list[Evidence] = Field(default_factory=list)
    tables_truncated: bool = False
    structured: StructuredExtraction | None = None


def strict_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFC", value).split()) or None


def decimal_text(value: str) -> str:
    value = value.replace("−", "-")
    whole, comma, fraction = value.partition(",")
    digits = whole.lstrip("+-")
    if comma and not (len(fraction) == 3 and 1 <= len(digits) <= 3 and digits[0] != "0"):
        return f"{whole}.{fraction}"
    return value


def numeric(value: str) -> str | None:
    match = SCALAR.fullmatch(value)
    if match is None:
        return None
    operator, number = match.groups()
    return operator + decimal_text(number)


def normalize_field(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if field == "unit":
        value = value.replace("µ", "μ")
        value = SUPERSCRIPT.sub(lambda match: "^" + match[0].translate(EXPONENT_CHARS), value)
        return re.sub(r"\s*([/^·])\s*", r"\1", value)
    if field == "reference" and (interval := RANGE.fullmatch(value)):
        low, high = interval.groups()
        return f"{decimal_text(low)}-{decimal_text(high)}"
    if field in {"value", "reference"}:
        return numeric(value) or value
    return value


def record_key(record: Measurement) -> tuple[str | None, ...]:
    return tuple(strict_text(getattr(record, field)) for field in FIELDS)


def normalized_key(key: tuple[str | None, ...]) -> tuple[str | None, ...]:
    return tuple(normalize_field(field, value) for field, value in zip(FIELDS, key, strict=True))
