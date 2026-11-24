import json
from hashlib import sha256

import pytest

from evaluation.chat_metrics import summarize
from evaluation.extraction_tracking import local_tracking_url
from scripts import register_chat_report as importer


def test_import_validates_and_registers_read_only_report(monkeypatch, tmp_path):
    checks = {"no_abstract": None, "no_foreign": None, "no_sources": None}
    dataset = {
        "version": "6.6.2", "documents": [], "corpus": [], "publications": [],
        "cases": [{
            "id": "pubmed-pmid", "facts": [], "required_sources": [], "checks": list(checks),
        }],
    }
    dataset_path = tmp_path / "dataset.json"
    dataset_raw = json.dumps(dataset).encode()
    dataset_path.write_bytes(dataset_raw)
    monkeypatch.setattr(importer, "DATASET", dataset_path)
    row = {
        "id": "pubmed-pmid", "llm_backend": "live", "error": "gigachat_unavailable",
        "llm_calls": 0, "llm_ms": 0.0, "wall_ms": 1.0, "checks": checks, "tools": [],
        "llm_requests": [], "sources": [], "missing_tools": [], "routing_ok": False,
        "mixed": False, "extra_pubmed": False, "context_error": False, "fallback": None,
        "answer": {
            "status": "technical_error", "fact_hits": 0, "fact_total": 0,
            "numeric_hits": 0, "numeric_total": 0, "required_source_hits": 0,
            "required_source_total": 0, "citations_valid": True, "extra_warning": False,
        },
    }
    report = {
        "dataset_version": "6.6.2", "dataset_hash": sha256(dataset_raw).hexdigest(),
        "commit": "42375cb", "mode": "live", "provider": "gigachat", "model": "GigaChat-3-Ultra",
        "retrieval_backend": "qdrant", "pubmed_backend": "fixed", "scenarios_expected": 1,
        "selected_cases": None, "checkpoint_version": 1, "legacy_scenarios": 1,
        "gigachat_connect_timeout_seconds": 5,
        "model_config": {
            "context_tokens": 16384, "output_tokens": 2048, "timeout_seconds": 180,
            "max_rounds": 5, "max_calls": 6,
        },
        "retrieval_config": {
            "embedding_model": "bge-m3:567m", "embedding_digest": "", "embedding_dimensions": 1024,
        },
        "complete": True, "cases": [row], "summary": summarize([row], live=True),
        "limitations": [], "recommendations": [], "mlflow": {"status": "not_requested"},
    }
    path = tmp_path / "report.json"
    raw = json.dumps(report, indent=2).encode()
    path.write_bytes(raw)
    registered = []
    uri = "http://mlflow:5000"

    def register(tracking_uri, summaries, params, **options):
        assert tracking_uri == uri and options["allow_tracking_service"] is True
        assert options["experiment_name"] == "medical-ai-evaluation"
        assert options["artifact_json"] == raw
        assert params["complete"] == "true" and params["security_unverified"] == "3"
        assert params["technical_errors"] == "1" and params["has_technical_errors"] == "true"
        assert params["validation_scope"] == "synthetic_benchmark"
        assert summaries["evaluation"] == report["summary"]
        registered.append(True)
        return "fixture-run"

    monkeypatch.setattr(importer, "register", register)
    assert importer.register_report(path, uri) == "fixture-run"
    assert path.read_bytes() == raw

    for invalid in (
        {**report, "complete": False},
        {**report, "recommendations": ["Authorization: Bearer fake-token"]},
        {**report, "cases": [{**row, "answer": {**row["answer"], "text": "Fake patient data"}}]},
        {**report, "cases": [{**row, "sources": ["unexpected-source-id"]}]},
        {**report, "summary": {**report["summary"], "scenarios": 999}},
    ):
        path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError, match="^invalid_chat_report$"):
            importer.register_report(path, uri)
    assert registered == [True]


def test_tracking_service_host_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="^invalid_mlflow_uri$"):
        local_tracking_url("http://mlflow:5000")
    url = local_tracking_url("http://mlflow:5000", allow_tracking_service=True)
    assert url.host == "mlflow" and url.port == 5000
    for uri in ("http://mlflow:5001", "http://token@mlflow:5000", "http://external.invalid:5000"):
        with pytest.raises(ValueError, match="^invalid_mlflow_uri$"):
            local_tracking_url(uri, allow_tracking_service=True)
