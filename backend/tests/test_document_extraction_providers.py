from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.documents.extraction_data import Evidence
from app.documents.providers import (
    LocalExtraction,
    ProcessingError,
    UltraExtraction,
    confirm_sources,
    extraction_provider,
)
from app.documents.schemas import ExternalAuthorization, TextVersionView
from evaluation.extraction_fixtures import load_fixtures, render


def test_default_local_provider_preserves_text_and_anchors_rows(tmp_path: Path) -> None:
    spec = load_fixtures()[0]
    payload, expected = render(spec)
    path = tmp_path / "original"
    path.write_bytes(payload)
    settings = Settings(redis_password="synthetic")
    assert settings.llm_provider == "ollama"
    provider = extraction_provider(settings.documents_extraction_provider, settings)
    assert isinstance(provider, LocalExtraction)
    result = provider.extract(path, "pdf", monotonic() + 30)
    assert result.status == "ready" and "Гемоглобин" in result.text
    assert result.locators[0]["index"] == 1
    assert result.structured and result.structured.local
    local = result.structured.local
    assert local.provider == "pymupdf_tables" and local.provider_version == "1.26.6"
    assert [(row.name, row.value) for row in local.measurements] == [
        (row.name, row.value) for row in expected
    ]
    assert all(row.source and row.source.page == 1 for row in local.measurements)


def test_external_selection_requires_opt_in_and_authorization() -> None:
    basic = Settings(redis_password="synthetic")
    enabled = basic.model_copy(update={"documents_extraction_provider": "gigachat_ultra"})
    auth = ExternalAuthorization(
        provider="gigachat_ultra", consent=True, sha256="a" * 64, text_version=1,
        owner_id=uuid4(), consented_at=datetime.now(UTC),
    )
    with pytest.raises(ProcessingError, match="EXTERNAL_DISABLED"):
        extraction_provider("gigachat_ultra", basic, authorization=auth)
    with pytest.raises(ProcessingError, match="EXTERNAL_NOT_AUTHORIZED"):
        extraction_provider("gigachat_ultra", enabled)
    denied = auth.model_copy(update={"consent": False})
    with pytest.raises(ProcessingError, match="EXTERNAL_NOT_AUTHORIZED"):
        extraction_provider("gigachat_ultra", enabled, authorization=denied)
    provider = extraction_provider("gigachat_ultra", enabled, authorization=auth)
    assert isinstance(provider, UltraExtraction)
    assert isinstance(extraction_provider("pymupdf_tables", enabled), LocalExtraction)


def test_confirm_sources_requires_unique_full_row_and_matching_page() -> None:
    local = Evidence(
        name="Glucose", value="<0,10", unit="mmol/L", reference="3,9–5,5",
        date=None, page=1, quote="synthetic table row", bbox=(10.0, 20.0, 50.0, 38.0),
    )
    remote = local.model_copy(update={"bbox": None, "reference": "3,9-5,5"})
    result, = confirm_sources([remote], [local], truncated=False)
    assert result.confirmation == "confirmed" and result.source is not None
    assert result.source.page == 1 and result.source.bbox == local.bbox
    assert result.reference == "3,9-5,5" and result.reconstructed_row == remote.quote
    for claimed in (
        remote.model_copy(update={"page": 2}),
        remote.model_copy(update={"date": "2026-10-01"}),
    ):
        result, = confirm_sources([claimed], [local], truncated=False)
        assert result.confirmation == "unverified" and result.source is None
    unverified, = confirm_sources([remote], [], truncated=False)
    assert unverified.confirmation == "unverified" and unverified.reported_page == 1
    incomplete, = confirm_sources([remote], [local], truncated=True)
    assert incomplete.confirmation == "unverified"
    duplicate_rows = [local, local.model_copy(update={"page": 2})]
    ambiguous, = confirm_sources([remote], duplicate_rows, truncated=False)
    assert ambiguous.confirmation == "ambiguous" and ambiguous.source is None


def test_legacy_version_view_keeps_existing_text_and_locators() -> None:
    old = TextVersionView.model_validate({
        "version": 1, "normalized_text": "synthetic", "extractor_version": "pdf:1.26.6:stage3",
        "locators": [{"kind": "page", "index": 1, "start": 0, "end": 9}],
    })
    assert old.structured_result is None
    assert old.normalized_text == "synthetic" and old.locators[0]["index"] == 1
