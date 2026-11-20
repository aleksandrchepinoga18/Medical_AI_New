import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from evaluation.extraction_fixtures import Evidence, load_fixtures, render
from evaluation.extraction_metrics import count_matches, score
from evaluation.local_extraction import flat_text, native_tables
from scripts import evaluate_document_extraction as benchmark


def row(**updates: object) -> Evidence:
    return Evidence(
        name="Glucose", value="<0,10", unit="µg/L", reference="3,9–5,5", date=None,
        page=1, quote="Glucose | <0,10 | µg/L | 3,9–5,5", bbox=(10, 20, 110, 40),
    ).model_copy(update=updates)


def test_fixed_corpus_and_raster_pages(tmp_path: Path) -> None:
    fixtures = load_fixtures()
    assert len(fixtures) == 8
    for fixture in fixtures:
        data, records = render(fixture)
        assert data == render(fixture)[0]
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            assert len(pdf) == len(fixture.pages)
            assert all(record.page is not None and record.page <= len(pdf) for record in records)
            for page, spec in zip(pdf, fixture.pages, strict=True):
                text = page.get_text()
                if spec.layout == "scan":
                    assert not text.strip()
                if spec.layout == "mixed":
                    assert "СИНТЕТИЧЕСКИЙ" in text
                    assert spec.rows[0][0] not in text
                if spec.layout in {"scan", "mixed"}:
                    assert page.get_images()
        path = tmp_path / f"{fixture.id}.pdf"
        path.write_bytes(data)
        baseline, raw_extraction = flat_text(path)
        tables, _ = native_tables(path)
        if fixture.id == "01-grid":
            assert "Гемоглобин" in str(raw_extraction["text"])
            assert count_matches(records, tables)["strict_correct"] == len(records)
            assert all(record.bbox is not None for record in tables)
        if fixture.id in {"06-scan", "08-no-measurements"}:
            assert baseline == tables == []


def test_micro_sign_survives_pdf_encoding_and_both_extractors(tmp_path: Path) -> None:
    fixture = next(fixture for fixture in load_fixtures() if fixture.id == "02-borderless")
    data, truth = render(fixture)
    path = tmp_path / "02-borderless.pdf"
    path.write_bytes(data)
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        assert "µg/L" in page.get_text()
        assert page.search_for("µg/L")
    baseline, _ = flat_text(path)
    tables, _ = native_tables(path)
    for records in (truth, baseline, tables):
        assert [record.unit for record in records if record.name == "Ferritin"] == ["µg/L"]


def test_metrics_keep_comparators_and_count_duplicates_and_wrong_pages() -> None:
    truth = row()
    normalized_number = truth.model_copy(update={"value": "<0.10", "page": 2})
    counts = count_matches([truth], [normalized_number, normalized_number])
    metrics = score(counts)
    strict = metrics["strict_exact_match"]
    normalized = metrics["normalized_exact_match"]
    assert strict["precision"] == strict["recall"] == strict["f1"] == 0
    assert normalized["precision"] == 0.5
    assert normalized["recall"] == 1
    assert normalized["f1"] == 2 / 3
    assert metrics["numeric_accuracy"] == metrics["unit_accuracy"] == 1
    assert metrics["page_accuracy"] == metrics["bbox_accuracy_when_claimed"] == 0
    assert strict["false_positive_records"] == 2
    assert metrics["unmatched_measurements"] == 1

    wrong_comparator = truth.model_copy(update={"value": ">0,10"})
    metrics = score(count_matches([truth], [wrong_comparator]))
    assert metrics["numeric_accuracy"] == 0

    metrics = score(count_matches([truth], [truth, truth]))
    strict = metrics["strict_exact_match"]
    assert strict["precision"] == 0.5
    assert strict["recall"] == metrics["page_accuracy"] == 1
    assert strict["f1"] == 2 / 3


@pytest.mark.parametrize(
    ("field", "original", "alternate", "matches"),
    [
        ("reference", "120–160", "120-160", True),
        ("reference", "4,0–9,0", "4,0-9,0", True),
        ("reference", "3,9–5,5", "3,9-5,5", True),
        ("reference", " −1,20 — -0,50 ", "-1.20--0.50", True),
        ("reference", "-1,20–0,50", "1.20-0.50", False),
        ("reference", "<5,0", "5.0", False),
        ("reference", "3,9 mmol/L", "3.9 mmol/L", False),
        ("unit", "µg/L", "μg / L", True),
        ("unit", "µg/L", "g/L", False),
        ("unit", "µg/L", "\x00g/L", False),
        ("unit", "10⁹/л", "10^9/л", True),
        ("unit", "10^9/л", "10^12/л", False),
        ("unit", "mg/L FEU", "mg/LFEU", False),
        ("value", "<0,10", "< 0.10", True),
        ("value", "<0,10", "0,10", False),
        ("value", "0,10", "0.1", False),
        ("value", "-0,10", "0.10", False),
        ("value", "0,123", "0.123", True),
        ("value", "1,234", "1.234", False),
        ("value", "1 234,56", "1234.56", False),
    ],
)
def test_normalization_only_accepts_safe_equivalences(
    field: str, original: str, alternate: str, matches: bool,
) -> None:
    counts = count_matches([row(**{field: original})], [row(**{field: alternate})])
    assert counts["strict_correct"] == 0
    assert counts["normalized_correct"] == counts[f"{field}_normalized"] == int(matches)


def test_verbatim_quote_requires_source_text_and_correct_page() -> None:
    truth = row()
    actual = "Glucose    <0,10    µg/L    3,9–5,5"
    pages = {1: f"Heading\n{actual}\nFooter", 2: actual}
    typed_quote = row(quote="Glucose <0,10 µg/L 3,9–5,5")
    retyped = count_matches([truth], [typed_quote], verbatim_pages=pages)
    assert retyped["verbatim_quote_correct"] == 0
    matched = count_matches([truth], [row(quote=actual)], verbatim_pages=pages)
    assert matched["verbatim_quote_correct"] == matched["page_correct"] == 1
    assert matched["bbox_correct"] == 1
    wrong_page = count_matches(
        [truth], [row(quote=actual, page=2)], verbatim_pages=pages,
    )
    assert wrong_page["verbatim_quote_correct"] == wrong_page["bbox_correct"] == 0
    rebuilt = count_matches([truth], [row(quote=actual)])
    assert rebuilt["normalized_correct"] == rebuilt["reconstructed_quote_claims"] == 1
    assert rebuilt["verbatim_quote_correct"] == rebuilt["verbatim_quote_claims"] == 0


def test_rescore_saved_v1_report_creates_new_file_without_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = [
        row(name=f"Analyte {index}", reference=reference).model_dump(mode="json")
        for index, reference in enumerate(("120–160", "4,0–9,0", "3,9–5,5"))
    ]
    found = [{**record, "reference": record["reference"].replace("–", "-")} for record in expected]
    result = {
        "metrics": {"f1": 0}, "measurements": found, "latency_ms": 13010, "error": None,
        "details": {"tokens": {"total_tokens": 2626}, "deletion": "deleted"},
    }
    old = {
        "schema_version": 1, "manifest_sha256": "original-manifest", "fixtures": ["01-grid"],
        "documents": [{
            "id": "01-grid", "pdf_sha256": "original-pdf", "expected": expected,
            "results": {"gigachat": result},
        }],
        "summary": {"gigachat": {"f1": 0, "total_tokens": 2626}},
    }
    input_path, output_path = tmp_path / "report.json", tmp_path / "rescored.json"
    original = json.dumps(old).encode()
    input_path.write_bytes(original)

    def no_extraction(*args: object) -> None:
        pytest.fail("rescoring attempted to run extraction")

    monkeypatch.setattr(benchmark, "compare", no_extraction)
    monkeypatch.setattr(
        "sys.argv",
        ["benchmark", "--rescore", str(input_path), "--report-output", str(output_path)],
    )
    assert benchmark.main() == 0
    assert input_path.read_bytes() == original
    updated = json.loads(output_path.read_bytes())
    assert updated["schema_version"] == 2
    assert updated["corpus_version"] == 1
    assert updated["manifest_sha256"] == "original-manifest"
    assert updated["rescored_from_sha256"] == hashlib.sha256(original).hexdigest()
    document = updated["documents"][0]
    assert document["pdf_sha256"] == "original-pdf"
    assert document["expected"] == expected
    assert document["results"]["gigachat"]["measurements"] == found
    assert document["results"]["gigachat"]["details"] == result["details"]
    for metrics in (updated["summary"]["gigachat"], document["results"]["gigachat"]["metrics"]):
        assert "f1" not in metrics
        assert metrics["strict_exact_match"]["f1"] == 0
        assert metrics["normalized_exact_match"]["f1"] == 1
        assert metrics["normalized_exact_match"]["field_exact_match"]["reference"]["accuracy"] == 1
    with pytest.raises(FileExistsError):
        benchmark.rescore(input_path, output_path)
    with pytest.raises(FileExistsError):
        benchmark.rescore(input_path, input_path)
    assert input_path.read_bytes() == original
