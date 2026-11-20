"""Local parser and authorized Ultra extraction behind one document interface."""

import asyncio
import os
import ssl
import subprocess
import sys
from collections import Counter, defaultdict
from importlib.metadata import version
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol

import httpx

from app.core.config import Settings
from app.documents.extraction_data import (
    FIELDS,
    Evidence,
    ExtractionReport,
    ExtractionResult,
    ProviderName,
    StoredMeasurement,
    StructuredExtraction,
    TableSource,
    normalized_key,
    record_key,
)
from app.documents.gigachat import (
    UPLOAD_BYTES,
    GigaChatExtraction,
    GigaChatResult,
    error_category,
    gigachat_tls_context,
)
from app.documents.schemas import ExternalAuthorization


class ProcessingError(Exception):
    def __init__(self, code: str, pending_file_id: str | None = None) -> None:
        super().__init__(code)
        self.pending_file_id = pending_file_id


class ExtractionProvider(Protocol):
    def extract(self, source: Path, format_: str, deadline: float) -> ExtractionResult: ...


def row_source(row: Evidence) -> TableSource | None:
    if row.page is None or row.bbox is None or not all(map(isfinite, row.bbox)):
        return None
    x0, y0, x1, y1 = row.bbox
    if x0 >= x1 or y0 >= y1:
        return None
    return TableSource(page=row.page, bbox=row.bbox)


def local_report(rows: list[Evidence], truncated: bool) -> ExtractionReport:
    measurements = []
    for row in rows:
        source = row_source(row)
        measurements.append(StoredMeasurement(
            **row.model_dump(include=set(FIELDS)),
            source=source,
            reconstructed_row=row.quote,
            confirmation="confirmed" if source is not None else "unverified",
        ))
    return ExtractionReport(
        provider="pymupdf_tables", provider_version=version("pymupdf"),
        measurements=measurements, truncated=truncated,
    )


def confirm_sources(
    rows: list[Evidence], local: list[Evidence], *, truncated: bool,
) -> list[StoredMeasurement]:
    candidates: dict[tuple[str | None, ...], list[Evidence]] = defaultdict(list)
    for row in local:
        candidates[normalized_key(record_key(row))].append(row)
    keys = [normalized_key(record_key(row)) for row in rows]
    repeats = Counter(keys)
    result = []
    for row, key in zip(rows, keys, strict=True):
        matches = candidates[key]
        source = None
        confirmation: Literal["confirmed", "ambiguous", "unverified"]
        if len(matches) > 1 or repeats[key] > 1:
            confirmation = "ambiguous"
        else:
            if matches and not truncated and matches[0].page == row.page:
                source = row_source(matches[0])
            confirmation = "confirmed" if source is not None else "unverified"
        result.append(StoredMeasurement(
            **row.model_dump(include=set(FIELDS)),
            reported_page=row.page,
            reconstructed_row=row.quote,
            source=source,
            confirmation=confirmation,
        ))
    return result


class LocalExtraction:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract(self, source: Path, format_: str, deadline: float) -> ExtractionResult:
        settings = self.settings
        output = source.with_name("result.json")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProcessingError("PROCESSING_TIMEOUT")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.documents.extract",
                    str(source),
                    format_,
                    str(output),
                    str(settings.documents_max_pages),
                    str(settings.documents_max_text_chars),
                    str(settings.documents_max_unpacked_bytes),
                    str(settings.documents_processing_seconds),
                    str(os.getpid()),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    **{
                        key: os.environ[key]
                        for key in ("SystemRoot", "WINDIR")
                        if key in os.environ
                    },
                },
            )
        except subprocess.TimeoutExpired:
            raise ProcessingError("PROCESSING_TIMEOUT") from None
        if result.returncode or not output.exists():
            raise ProcessingError("PARSER_FAILED")
        if output.stat().st_size > settings.documents_max_text_chars * 12 + 2000000:
            raise ProcessingError("TEXT_LIMIT")
        extracted = ExtractionResult.model_validate_json(output.read_text(encoding="utf-8"))
        if len(extracted.text) > settings.documents_max_text_chars:
            raise ProcessingError("TEXT_LIMIT")
        if extracted.error_code:
            raise ProcessingError(extracted.error_code)
        if format_ == "pdf":
            extracted.structured = StructuredExtraction(
                local=local_report(extracted.table_rows, extracted.tables_truncated),
            )
        return extracted


class UltraExtraction:
    def __init__(
        self, settings: Settings, upload_name: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.upload_name = upload_name
        self.transport = transport

    def extract(self, source: Path, format_: str, deadline: float) -> ExtractionResult:
        if format_ != "pdf" or source.stat().st_size > UPLOAD_BYTES:
            raise ProcessingError("EXTERNAL_UNSUPPORTED_FILE")
        if not self.settings.gigachat_credentials.get_secret_value().strip():
            raise ProcessingError("EXTERNAL_NOT_CONFIGURED")
        try:
            tls = gigachat_tls_context(self.settings.gigachat_ca_bundle)
        except (OSError, ValueError):
            raise ProcessingError("EXTERNAL_TLS_CONFIG") from None
        local = LocalExtraction(self.settings).extract(source, format_, deadline)
        pdf = source.read_bytes()
        remote = asyncio.run(self.request(pdf, deadline, tls))
        if remote.deletion == "unknown":
            raise ProcessingError("EXTERNAL_UPLOAD_UNCONFIRMED")
        if remote.deletion == "failed":
            raise ProcessingError("EXTERNAL_CLEANUP_UNCONFIRMED", remote.pending_file_id)
        if remote.error:
            code = {
                "timeout": "EXTERNAL_TIMEOUT",
                "tls": "EXTERNAL_TLS_ERROR",
                "network": "EXTERNAL_NETWORK_ERROR",
                "authorization": "EXTERNAL_AUTH_ERROR",
            }.get(error_category(remote.error), "EXTERNAL_API_ERROR")
            raise ProcessingError(code)
        model, _, build = (remote.reported_model or "").partition(":")
        if model != self.settings.gigachat_model:
            raise ProcessingError("EXTERNAL_MODEL_MISMATCH")
        assert local.structured is not None
        local.structured.external = ExtractionReport(
            provider="gigachat_ultra", model=model, provider_version=build or "unknown",
            measurements=confirm_sources(
                remote.measurements, local.table_rows, truncated=local.tables_truncated,
            ),
        )
        return local

    async def request(self, pdf: bytes, deadline: float, tls: ssl.SSLContext) -> GigaChatResult:
        async with httpx.AsyncClient(
            verify=tls, transport=self.transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(60, connect=5),
        ) as client:
            adapter = GigaChatExtraction(
                client, self.settings.gigachat_credentials, self.settings.gigachat_model,
                "schema", scope=self.settings.gigachat_scope,
            )
            return await adapter.extract(pdf, deadline=deadline, upload_name=self.upload_name)


def extraction_provider(
    name: ProviderName, settings: Settings, *,
    authorization: ExternalAuthorization | None = None, upload_name: str | None = None,
) -> ExtractionProvider:
    if name == "pymupdf_tables":
        return LocalExtraction(settings)
    if settings.documents_extraction_provider != name:
        raise ProcessingError("EXTERNAL_DISABLED")
    if authorization is None or not authorization.consent or authorization.provider != name:
        raise ProcessingError("EXTERNAL_NOT_AUTHORIZED")
    return UltraExtraction(settings, upload_name)
