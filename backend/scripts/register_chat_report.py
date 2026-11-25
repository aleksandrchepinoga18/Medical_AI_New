"""Import a complete, text-free Stage 6.6 GigaChat report into local MLflow."""

import argparse
import json
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any

import httpx

from evaluation.chat_metrics import first_relevant_rank, retrieval_scores, summarize
from evaluation.extraction_tracking import register

DATASET = Path(__file__).parents[1] / "evaluation" / "chat_benchmark.json"
MAX_REPORT_BYTES = 1_000_000
EXPERIMENT = "medical-ai-evaluation"
IDENTITY = {
    "dataset_version": "6.6.2", "commit": "42375cb", "mode": "live",
    "provider": "gigachat", "model": "GigaChat-3-Ultra", "retrieval_backend": "qdrant",
    "pubmed_backend": "fixed", "selected_cases": None, "checkpoint_version": 1,
}
TOOLS = {
    "get_documents", "search_documents", "analyze_measurements", "search_medical_knowledge",
    "search_pubmed", "unknown",
}
ERRORS = set("""
    agent_context_limit agent_data_unavailable agent_no_answer agent_round_limit
    agent_repeated_tool agent_sources_missing agent_tool_limit cancelled closed
    external_consent_required gigachat_timeout gigachat_unavailable gigachat_not_configured
    gigachat_tls_config gigachat_http_401 gigachat_http_403 gigachat_http_429
    internal_error interrupted invalid_arguments invalid_response output_limit
    pubmed_not_requested pubmed_query_blocked pubmed_unavailable repeated_tool result_too_large
    retrieval_unavailable knowledge_unavailable timeout tools_unsupported unavailable
    oauth_timeout oauth_tls oauth_http_400 oauth_http_401 oauth_http_403 oauth_http_429
    provider_disabled other_error
""".split())
OUTCOMES = {"correct", "partial", "unsupported", "no_answer", "technical_error"}
NOTES = {
    "Deterministic checks cover declared facts and citations, not clinical correctness.",
    "PubMed uses fixed replay in all modes; personal retrieval uses fixed candidate lists.",
    "Qdrant scores apply only to the small benchmark corpus when real retrieval is selected.",
    "Offline model timings and fixed retrieval timings are fixture timings.",
    "Live model aggregates exclude injected model output and consent-only checks.",
    "Investigate failed critical checks before interpreting quality.",
    "Inspect the setup of unverified security probes.",
    "Inspect LLM request phases and answer coverage before comparison.",
    "Inspect the tool result budget for the affected scenarios.",
    "Check retrieval availability before interpreting ranking metrics.",
}


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid_chat_report")


def record(value: Any, required: str, optional: str = "") -> dict[str, Any]:
    require(type(value) is dict)
    keys = set(required.split())
    require(keys <= value.keys() <= keys | set(optional.split()))
    return value


def items(value: Any) -> list[Any]:
    require(type(value) is list)
    return value


def member(value: Any, allowed: set[str], *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is str and value in allowed)


def count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


def duration(value: Any) -> bool:
    return type(value) in (float, int) and 0 <= value <= 2**53 - 1 and isfinite(value)


def same(actual: Any, expected: Any) -> bool:
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
        expected, sort_keys=True, allow_nan=False,
    )


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def validate_trace(row: dict[str, Any]) -> None:
    for tool in items(row["tools"]):
        record(tool, "name error ms round")
        require(member(tool["name"], TOOLS) and member(tool["error"], ERRORS, nullable=True))
        require(duration(tool["ms"]) and (tool["round"] is None or count(tool["round"])))
    for request in items(row["llm_requests"]):
        record(
            request, "phase round context_bytes offered_tools tools received_text first_event_ms "
            "error ms", "finish_reason timeout_origin timeout_kind",
        )
        require(member(request["phase"], {"tool_round", "grounded_answer", "fallback_answer"}))
        require(member(request["error"], ERRORS, nullable=True))
        require(request["round"] is None or count(request["round"]))
        require(count(request["context_bytes"]) and duration(request["ms"]))
        require(request["first_event_ms"] is None or duration(request["first_event_ms"]))
        require(type(request["received_text"]) is bool)
        require(all(member(name, TOOLS) for field in ("offered_tools", "tools")
                    for name in items(request[field])))
        for key, allowed in {
            "finish_reason": {"stop", "length", "function_call", "other"},
            "timeout_origin": {"provider_request", "scenario_deadline"},
            "timeout_kind": {"connect", "read", "write", "pool", "http", "deadline"},
        }.items():
            if key in request:
                require(member(request[key], allowed))


def validate_case(row: Any, case: dict[str, Any], aliases: set[str]) -> None:
    record(
        row, "id answer routing_ok missing_tools mixed extra_pubmed tools fallback context_error "
        "llm_calls llm_ms wall_ms llm_requests llm_backend error sources checks",
        "retrieval retrieval_quality history_probe",
    )
    require(row["id"] == case["id"])
    require(member(row["llm_backend"], {"live", "fixture", "none"}))
    require(all(member(row[key], ERRORS, nullable=True) for key in ("error", "fallback")))
    require(all(type(row[key]) is bool for key in (
        "routing_ok", "mixed", "extra_pubmed", "context_error",
    )))
    require(count(row["llm_calls"]) and row["llm_calls"] == len(items(row["llm_requests"])))
    require(duration(row["llm_ms"]) and duration(row["wall_ms"]))
    require(all(member(name, TOOLS) for name in items(row["missing_tools"])))
    require(all(member(alias, aliases) for alias in items(row["sources"])))
    checks = record(row["checks"], " ".join(case.get("checks", [])))
    require(all(value is None or type(value) is bool for value in checks.values()))

    answer = record(
        row["answer"], "status fact_hits fact_total numeric_hits numeric_total "
        "required_source_hits required_source_total citations_valid extra_warning",
    )
    require(member(answer["status"], OUTCOMES))
    require((answer["status"] == "technical_error") == (row["error"] is not None))
    require(type(answer["citations_valid"]) is bool and type(answer["extra_warning"]) is bool)
    for key, total in (
        ("fact", len(case["facts"])), ("numeric", len(case.get("numeric", []))),
        ("required_source", len(case["required_sources"])),
    ):
        actual, hits = answer[f"{key}_total"], answer[f"{key}_hits"]
        require(count(actual) and actual == total and count(hits) and hits <= total)

    validate_trace(row)
    require(("retrieval" in row) == ("retrieval" in case))
    if "retrieval" in row:
        retrieval = record(row["retrieval"], "scores error ms ranked_sources first_relevant_rank")
        require(member(retrieval["error"], ERRORS, nullable=True) and duration(retrieval["ms"]))
        ranked = items(retrieval["ranked_sources"])
        require(all(member(alias, aliases) for alias in ranked) and len(set(ranked)) == len(ranked))
        relevant = case["retrieval"]["relevant"]
        require(same(retrieval["first_relevant_rank"], first_relevant_rank(ranked, set(relevant))))
        expected = None if retrieval["error"] else retrieval_scores(ranked, relevant)
        require(same(retrieval["scores"], expected))
    if "retrieval_quality" in row:
        quality = record(row["retrieval_quality"], "irrelevant_sources")
        require(all(member(alias, aliases) for alias in items(quality["irrelevant_sources"])))
    if "history_probe" in row:
        require(member(row["history_probe"], {
            "passed", "source_still_visible",
            "fixture_missing_reference", "fixture_missing_message",
        }))


def validate_report(raw: bytes) -> dict[str, Any]:
    require(len(raw) <= MAX_REPORT_BYTES)
    report = record(
        json.loads(raw, object_pairs_hook=reject_duplicate_keys),
        " ".join(IDENTITY) + " dataset_hash scenarios_expected retrieval_config model_config "
        "gigachat_connect_timeout_seconds legacy_scenarios complete summary cases limitations "
        "recommendations mlflow",
    )
    require(all(same(report[key], value) for key, value in IDENTITY.items()))
    dataset_raw = DATASET.read_bytes()
    dataset = json.loads(dataset_raw)
    require(report["dataset_version"] == dataset["version"])
    require(report["dataset_hash"] == sha256(dataset_raw).hexdigest())
    require(report["complete"] is True)
    require(same(report["retrieval_config"], {
        "embedding_model": "bge-m3:567m", "embedding_digest": "", "embedding_dimensions": 1024,
    }))
    require(same(report["model_config"], {
        "context_tokens": 16384, "output_tokens": 2048, "timeout_seconds": 180,
        "max_rounds": 5, "max_calls": 6,
    }))
    require(same(report["gigachat_connect_timeout_seconds"], 5))
    require(same(report["mlflow"], {"status": "not_requested"}))
    require(all(member(note, NOTES) for key in ("limitations", "recommendations")
                for note in items(report[key])))
    rows = items(report["cases"])
    require(len(rows) == len(dataset["cases"]) and same(report["scenarios_expected"], len(rows)))
    require(count(report["legacy_scenarios"]) and report["legacy_scenarios"] <= len(rows))
    aliases = {"unknown"} | {
        entry["id"]
        for family in ("documents", "corpus", "publications")
        for entry in dataset[family]
    }
    for row, case in zip(rows, dataset["cases"], strict=True):
        validate_case(row, case, aliases)
    require(same(report["summary"], summarize(rows, live=True)))
    return report


def register_report(path: Path, uri: str) -> str:
    with path.open("rb") as file:
        raw = file.read(MAX_REPORT_BYTES + 1)
    report = validate_report(raw)
    summary = report["summary"]
    technical_errors = sum(row["answer"]["status"] == "technical_error" for row in report["cases"])
    params = {
        "dataset_version": report["dataset_version"], "dataset_hash": report["dataset_hash"],
        "commit": report["commit"], "mode": report["mode"], "provider": report["provider"],
        "model": report["model"], "retrieval_backend": report["retrieval_backend"],
        "complete": "true", "technical_errors": str(technical_errors),
        "has_technical_errors": str(bool(technical_errors)).lower(),
        "security_unverified": str(summary["security"]["unverified"]),
        "validation_scope": "synthetic_benchmark",
    }
    return register(
        uri, {"evaluation": summary}, params, experiment_name=EXPERIMENT,
        run_name="live gigachat (imported)", artifact_json=raw, allow_tracking_service=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mlflow-uri", required=True)
    args = parser.parse_args()
    try:
        run_id = register_report(args.report, args.mlflow_uri)
    except (ValueError, KeyError, TypeError, OSError, RecursionError, httpx.HTTPError):
        print("Import failed. Check the Stage 6.6 report and the local MLflow endpoint.")
        return 2
    print(f"MLflow run_id={run_id}. Imported synthetic benchmark report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
