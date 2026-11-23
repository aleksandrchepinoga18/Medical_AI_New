"""Run the versioned chat benchmark on the isolated synthetic RAG test stack."""

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.chat.agent import MAX_CALLS, MAX_ROUNDS
from app.rag.adapters import RagError
from evaluation.chat_metrics import compare_reports, summarize
from evaluation.chat_runtime import SAFE_ERRORS, evaluate, isolated_settings
from evaluation.extraction_tracking import local_tracking_url, register

DATASET = Path(__file__).parents[1] / "evaluation" / "chat_benchmark.json"
LIMITATIONS = [
    "Deterministic checks cover declared facts and citations, not clinical correctness.",
    "PubMed uses fixed replay in all modes; personal retrieval uses fixed candidate lists.",
    "Qdrant scores apply only to the small benchmark corpus when real retrieval is selected.",
    "Offline model timings and fixed retrieval timings are fixture timings.",
    "Live model aggregates exclude injected model output and consent-only checks.",
]


def load_dataset() -> tuple[dict[str, Any], str]:
    raw = DATASET.read_bytes()
    dataset = json.loads(raw)
    cases = dataset["cases"]
    corpus = {item["id"]: item for item in dataset["corpus"]}
    published = {alias for alias, item in corpus.items() if item["status"] == "published"}
    ids = {
        item["id"] for family in ("documents", "corpus", "publications") for item in dataset[family]
    }
    if not 25 <= len(cases) <= 40 or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("invalid_dataset_cases")
    for case in cases:
        if not set(case["required_sources"]) <= ids:
            raise ValueError("invalid_dataset_sources")
        if case.get("retrieval"):
            relevant = set(case["retrieval"]["relevant"])
            if not relevant or not relevant <= published:
                raise ValueError("invalid_dataset_relevance")
        if "hidden_source" in case:
            hidden = case["hidden_source"]
            protected = case.get("protected_text", "").strip().casefold()
            if (
                hidden not in corpus or hidden in published or not protected
                or protected not in corpus[hidden]["text"].casefold()
                or protected in case["question"].casefold()
                or f"no_{hidden}" not in case["checks"]
                or "no_sources" in case["checks"]
            ):
                raise ValueError("invalid_dataset_hidden_source")
    return dataset, hashlib.sha256(raw).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            "scenario", "llm_backend", "answer_status", "routing_ok", "citations_valid",
            "numeric_hits", "numeric_total", "llm_calls", "tool_calls", "wall_ms",
            "fallback", "error", "failed_critical_checks",
        ])
        for row in rows:
            answer = row["answer"]
            writer.writerow([
                row["id"], row["llm_backend"], answer["status"], row["routing_ok"],
                answer["citations_valid"], answer["numeric_hits"], answer["numeric_total"],
                row["llm_calls"], len(row["tools"]), round(row["wall_ms"], 2),
                row["fallback"], row["error"],
                ",".join(check for check, passed in row["checks"].items() if not passed),
            ])


def safe_report(args: argparse.Namespace) -> dict[str, Any]:
    dataset, dataset_hash = load_dataset()
    if args.mlflow_uri:
        local_tracking_url(args.mlflow_uri, allow_docker_host=True)
    live = args.mode == "live"
    live_retrieval = args.retrieval == "qdrant"
    settings = isolated_settings(args.provider, live=live, live_retrieval=live_retrieval)
    metadata: dict[str, Any] = {
        "dataset_version": dataset["version"], "dataset_hash": dataset_hash,
        "commit": args.commit, "mode": args.mode, "provider": args.provider,
        "model": settings.generation_model, "retrieval_backend": args.retrieval,
        "pubmed_backend": "fixed", "scenarios_expected": len(dataset["cases"]),
        "retrieval_config": {
            "embedding_model": settings.rag_embedding_model if live_retrieval else "fixed",
            "embedding_digest": settings.rag_embedding_digest if live_retrieval else "fixed",
            "embedding_dimensions": settings.rag_embedding_dimensions if live_retrieval else None,
        },
        "model_config": {
            "context_tokens": settings.generation_context,
            "output_tokens": settings.generation_output,
            "timeout_seconds": settings.generation_timeout, "max_rounds": MAX_ROUNDS,
            "max_calls": MAX_CALLS,
        },
    }
    if args.dry_run:
        return {**metadata, "state": "configuration_checked"}
    previous = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
    rows = evaluate(
        dataset, args.provider, live=live, live_retrieval=live_retrieval,
        allow_synthetic_cloud=args.allow_synthetic_cloud,
    )
    summary = summarize(rows, live=live)
    complete = len(rows) == len(dataset["cases"])
    recommendations = []
    if not complete:
        recommendations.append("Check the selected provider credentials or connectivity.")
    if summary["security"]["failed"]:
        recommendations.append("Investigate failed critical checks before interpreting quality.")
    if summary["agent"]["context_errors"]:
        recommendations.append("Inspect the tool result budget for the affected scenarios.")
    if not summary["retrieval"]["scored"]:
        recommendations.append("Check retrieval availability before interpreting ranking metrics.")
    report = {
        **metadata, "complete": complete, "summary": summary, "cases": rows,
        "limitations": LIMITATIONS, "recommendations": recommendations,
    }
    tracking: dict[str, Any] = {"status": "not_requested"}
    if args.mlflow_uri:
        public_params = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
            for key, value in metadata.items()
        }
        public_params["complete"] = str(complete)
        try:
            run_id = register(
                args.mlflow_uri, {"evaluation": summary}, public_params,
                experiment_name="medical-ai-evaluation",
                run_name=f"{args.mode} {args.provider}",
                artifact_json=json.dumps(report, ensure_ascii=False).encode(),
                allow_docker_host=True,
            )
        except (ValueError, httpx.HTTPError, KeyError, TypeError):
            tracking = {"status": "failed"}
        else:
            tracking = {"status": "registered", "run_id": run_id}
    report["mlflow"] = tracking
    if previous is not None:
        report["comparison"] = compare_reports(report, previous)
    return report


def print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"{report['mode']} / {report['provider']} / {report['model']}")
    print(f"Scenarios {summary['scenarios']} / {report['scenarios_expected']}")
    print("| Group | Metric | Value |\n|---|---|---|")
    for group in ("retrieval", "agent", "answers", "security", "performance"):
        for metric, value in summary[group].items():
            display = f"{value:.4g}" if type(value) is float else value
            print(f"| {group} | {metric} | {display} |")
    print(f"Errors {summary['errors']}")
    if comparison := report.get("comparison"):
        print(f"Comparison {comparison['status']}: {len(comparison.get('case_ids', []))} cases")
        if comparison["status"] == "ok":
            local = comparison["providers"]["ollama"]
            cloud = comparison["providers"]["gigachat"]
            print("| Metric | Ollama | GigaChat |\n|---|---|---|")
            for group, metric in (
                ("agent", "routing_accuracy"), ("answers", "numerical"),
                ("answers", "citation_validity"), ("performance", "wall_mean_ms"),
                ("performance", "llm_calls"),
            ):
                print(f"| {metric} | {local[group][metric]} | {cloud[group][metric]} |")
    if report["recommendations"]:
        print("\n".join(report["recommendations"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--provider", choices=["ollama", "gigachat"], required=True)
    parser.add_argument("--retrieval", choices=["fixed", "qdrant"], default="fixed")
    parser.add_argument("--allow-synthetic-cloud", action="store_true")
    parser.add_argument("--commit", required=True, help="Git revision supplied by the operator")
    parser.add_argument("--output", type=Path, help="Local JSON report")
    parser.add_argument("--csv", type=Path, help="Optional local CSV projection")
    parser.add_argument("--mlflow-uri", help="Opt in to local MLflow tracking")
    parser.add_argument("--compare", type=Path, help="Compare with an existing live JSON report")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", args.commit):
        parser.error("--commit must be a hexadecimal Git revision")
    if (
        not args.dry_run and args.mode == "live" and args.provider == "gigachat"
        and not args.allow_synthetic_cloud
    ):
        parser.error("live GigaChat requires --allow-synthetic-cloud")
    if not args.dry_run and args.output is None:
        parser.error("--output is required for an evaluation run")
    try:
        report = safe_report(args)
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False))
            return 0
        contents = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.output.write_text(contents, encoding="utf-8")
        if args.csv:
            write_csv(args.csv, report["cases"])
        print_summary(report)
    except (
        ValueError, KeyError, TypeError, OSError, RagError, SQLAlchemyError, httpx.HTTPError,
    ) as exc:
        # Avoid repr or upstream payloads in run output.
        code = str(exc)
        known = SAFE_ERRORS | {
            "isolated_rag_test_environment_required", "offline_qdrant_requires_bge_m3",
            "selected_provider_unavailable", "invalid_dataset_cases", "invalid_dataset_sources",
            "invalid_dataset_relevance", "invalid_mlflow_uri", "synthetic_cloud_consent_required",
        }
        print(f"Evaluation blocked: {code if code in known else type(exc).__name__}")
        return 2
    return int(
        not report["complete"] or report["summary"]["security"]["failed"] > 0
        or report["mlflow"]["status"] == "failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
