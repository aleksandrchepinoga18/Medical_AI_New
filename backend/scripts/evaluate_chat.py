"""Run the versioned chat benchmark on the isolated synthetic RAG test stack."""

import argparse
import csv
import hashlib
import json
import re
from asyncio import run
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.chat.agent import MAX_CALLS, MAX_ROUNDS
from app.core.config import Settings
from app.documents.gigachat import GigaChatExtraction, gigachat_tls_context
from app.llm.gateway import GatewayError
from app.llm.gigachat import CONNECT_TIMEOUT_SECONDS
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
GIGACHAT_PREFLIGHT_ERRORS = {
    "gigachat_credentials_missing": "pass GIGACHAT_CREDENTIALS for this run",
    "gigachat_ca_missing": "mount the PEM and set GIGACHAT_CA_BUNDLE",
    "gigachat_ca_invalid": "GIGACHAT_CA_BUNDLE cannot be loaded as a trusted CA bundle",
    "oauth_transport": "check live Evaluation egress, DNS and outbound TCP port 9443",
    "oauth_timeout": "the OAuth endpoint did not respond within the preflight timeout",
    "oauth_tls": "TLS verification failed for the OAuth endpoint",
    "oauth_invalid_response": "the OAuth endpoint did not return a valid token response",
    "oauth_expired_token": "the OAuth endpoint returned an expired token",
}


async def check_gigachat_oauth(settings: Settings) -> None:
    try:
        verify = gigachat_tls_context(settings.gigachat_ca_bundle)
    except (OSError, ValueError):
        raise ValueError("gigachat_ca_invalid") from None
    timeout = min(10, settings.generation_timeout)
    async with httpx.AsyncClient(
        verify=verify, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_SECONDS),
    ) as client:
        auth = GigaChatExtraction(
            client, settings.gigachat_credentials, settings.gigachat_model, "schema",
            scope=settings.gigachat_scope,
        )
        try:
            await auth.headers(timeout=timeout)
        except GatewayError as exc:
            code = str(exc)
            if code not in GIGACHAT_PREFLIGHT_ERRORS and not re.fullmatch(
                r"oauth_http_[1-5][0-9]{2}", code,
            ):
                code = "oauth_invalid_response"
            raise ValueError(code) from None


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
            "fallback", "error", "failed_critical_checks", "unverified_critical_checks",
        ])
        for row in rows:
            answer = row["answer"]
            writer.writerow([
                row["id"], row["llm_backend"], answer["status"], row["routing_ok"],
                answer["citations_valid"], answer["numeric_hits"], answer["numeric_total"],
                row["llm_calls"], len(row["tools"]), round(row["wall_ms"], 2),
                row["fallback"], row["error"],
                ",".join(check for check, passed in row["checks"].items() if passed is False),
                ",".join(check for check, passed in row["checks"].items() if passed is None),
            ])


def print_progress(index: int, total: int, row: dict[str, Any]) -> None:
    status = row["error"] or row["answer"]["status"]
    if row["checks"].get("consent_denied"):
        status = "consent_denied"
    print(f"[{index}/{total}] {row['id']} status={status} duration={row['wall_ms'] / 1000:.2f}s",
          flush=True)


def write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    contents = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    file = NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False,
    )
    temporary = Path(file.name)
    try:
        with file:
            file.write(contents)
            file.flush()
            fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def resume_rows(
    path: Path, metadata: dict[str, Any], cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("invalid_evaluation_checkpoint") from exc
    if not isinstance(saved, dict):
        raise ValueError("invalid_evaluation_checkpoint")
    for key, value in metadata.items():
        if key not in saved or saved[key] != value:
            raise ValueError(f"resume_mismatch_{key}")
    try:
        rows = saved["cases"]
        if not isinstance(rows, list):
            raise ValueError
        prefix = [case["id"] for case in cases[:len(rows)]]
        if [row["id"] for row in rows] != prefix:
            raise ValueError
        if saved["complete"] is not (len(rows) == len(cases)):
            raise ValueError
        for case, row in zip(cases[:len(rows)], rows, strict=True):
            if set(row["checks"]) != set(case["checks"]) or any(
                value is not None and type(value) is not bool for value in row["checks"].values()
            ):
                raise ValueError
        version = saved.get("checkpoint_version")
        if version is None:
            # Legacy reports treated the absence of a streamed answer as a failed check.
            for row in rows:
                if (
                    row["error"] and not row["sources"]
                    and row["checks"].get("no_abstract") is False
                ):
                    row["checks"]["no_abstract"] = None
        elif type(version) is not int or version != 1:
            raise ValueError
        imported = saved.get("legacy_scenarios", len(rows) if version is None else 0)
        if type(imported) is not int or not 0 <= imported <= len(rows):
            raise ValueError
        summarize(rows, live=metadata["mode"] == "live")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_evaluation_checkpoint") from exc
    return rows, imported


def safe_report(args: argparse.Namespace) -> dict[str, Any]:
    dataset, dataset_hash = load_dataset()
    selected = set(getattr(args, "case", []) or [])
    if not selected <= {case["id"] for case in dataset["cases"]}:
        raise ValueError("unknown_evaluation_case")
    if selected:
        cases = [case for case in dataset["cases"] if case["id"] in selected]
        dataset = {**dataset, "cases": cases}
    if args.mlflow_uri:
        local_tracking_url(args.mlflow_uri, allow_docker_host=True)
    live = args.mode == "live"
    live_retrieval = args.retrieval == "qdrant"
    live_gigachat = live and args.provider == "gigachat"
    preflight_only = getattr(args, "preflight_only", False)
    if preflight_only and not live_gigachat:
        raise ValueError("gigachat_preflight_requires_live")
    if live_gigachat and not args.dry_run and not args.allow_synthetic_cloud:
        raise ValueError("synthetic_cloud_consent_required")
    settings = isolated_settings(args.provider, live=live, live_retrieval=live_retrieval)
    metadata: dict[str, Any] = {
        "dataset_version": dataset["version"], "dataset_hash": dataset_hash,
        "commit": args.commit, "mode": args.mode, "provider": args.provider,
        "model": settings.generation_model, "retrieval_backend": args.retrieval,
        "pubmed_backend": "fixed", "scenarios_expected": len(dataset["cases"]),
        "selected_cases": [case["id"] for case in dataset["cases"]] if selected else None,
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
    resume = getattr(args, "resume", None)
    rows, imported = resume_rows(resume, metadata, dataset["cases"]) if resume else ([], 0)
    output = getattr(args, "output", None)
    offset = len(rows)
    diagnostics = (
        {"gigachat_connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS} if live_gigachat else {}
    )
    if args.dry_run:
        return {
            **metadata, **diagnostics, "state": "configuration_checked",
            "scenarios_saved": offset, "scenarios_pending": len(dataset["cases"]) - offset,
        }
    if live_gigachat and (preflight_only or offset < len(dataset["cases"])):
        run(check_gigachat_oauth(settings))
        if preflight_only:
            return {**metadata, "state": "gigachat_oauth_checked"}
    previous = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
    total = len(dataset["cases"])

    def snapshot() -> dict[str, Any]:
        summary = summarize(rows, live=live)
        recommendations = []
        if len(rows) != total:
            recommendations.append(
                "Resume the remaining scenarios after resolving any run blocker.",
            )
        if summary["security"]["failed"]:
            recommendations.append(
                "Investigate failed critical checks before interpreting quality.",
            )
        if summary["security"]["unverified"]:
            recommendations.append("Inspect the setup of unverified security probes.")
        if summary["reliability"]["timeout_scenarios"]:
            recommendations.append(
                "Inspect LLM request phases and answer coverage before comparison.",
            )
        if summary["agent"]["context_errors"]:
            recommendations.append("Inspect the tool result budget for the affected scenarios.")
        if not summary["retrieval"]["scored"]:
            recommendations.append(
                "Check retrieval availability before interpreting ranking metrics.",
            )
        return {
            **metadata, **diagnostics,
            "checkpoint_version": 1, "legacy_scenarios": imported,
            "complete": len(rows) == total, "summary": summary, "cases": rows,
            "limitations": LIMITATIONS, "recommendations": recommendations,
        }

    def on_case(index: int, _total: int, row: dict[str, Any]) -> None:
        rows.append(row)
        if output:
            write_checkpoint(output, snapshot())
        print_progress(offset + index, total, row)

    if offset < total:
        evaluate(
            {**dataset, "cases": dataset["cases"][offset:]},
            args.provider, live=live, live_retrieval=live_retrieval,
            allow_synthetic_cloud=args.allow_synthetic_cloud, on_case=on_case,
        )
    report = snapshot()
    summary, complete = report["summary"], report["complete"]
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
    if output:
        write_checkpoint(output, report)
    return report


def print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"{report['mode']} / {report['provider']} / {report['model']}")
    print(f"Scenarios {summary['scenarios']} / {report['scenarios_expected']}")
    print("| Group | Metric | Value |\n|---|---|---|")
    for group in ("retrieval", "agent", "answers", "reliability", "security", "performance"):
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
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--dry-run", action="store_true")
    checks.add_argument("--preflight-only", action="store_true", help="Check live GigaChat OAuth")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--provider", choices=["ollama", "gigachat"], required=True)
    parser.add_argument("--retrieval", choices=["fixed", "qdrant"], default="fixed")
    parser.add_argument("--case", action="append", help="Evaluate a scenario by ID (repeatable)")
    parser.add_argument("--allow-synthetic-cloud", action="store_true")
    parser.add_argument("--commit", required=True, help="Git revision supplied by the operator")
    parser.add_argument("--output", type=Path, help="Local JSON report")
    parser.add_argument("--resume", type=Path, help="Continue from a matching JSON checkpoint")
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
    if not (args.dry_run or args.preflight_only) and args.output is None:
        parser.error("--output is required for an evaluation run")
    if args.resume and args.preflight_only:
        parser.error("--resume is incompatible with --preflight-only")
    if (
        args.resume and args.output and args.output.exists()
        and args.output.resolve() != args.resume.resolve()
    ):
        parser.error("--output must be the resume checkpoint or a new path")
    if args.csv and any(args.csv.resolve() == path.resolve()
                        for path in (args.output, args.resume) if path):
        parser.error("--csv must differ from the JSON report paths")
    try:
        report = safe_report(args)
        if args.dry_run or args.preflight_only:
            print(json.dumps(report, ensure_ascii=False))
            return 0
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
            "invalid_dataset_hidden_source", "unknown_evaluation_case",
            "gigachat_preflight_requires_live", "invalid_evaluation_checkpoint",
        }
        if code in GIGACHAT_PREFLIGHT_ERRORS:
            print(f"Evaluation blocked: {code}. {GIGACHAT_PREFLIGHT_ERRORS[code]}")
        elif re.fullmatch(r"oauth_http_[1-5][0-9]{2}", code):
            hint = ("Check the GigaChat authorization key and scope."
                    if code in {"oauth_http_400", "oauth_http_401", "oauth_http_403"}
                    else "The OAuth endpoint returned an unsuccessful HTTP status.")
            print(f"Evaluation blocked: {code}. {hint}")
        elif re.fullmatch(r"resume_mismatch_[a-z_]+", code):
            print(f"Evaluation blocked: {code}")
        else:
            print(f"Evaluation blocked: {code if code in known else type(exc).__name__}")
        return 2
    return int(
        not report["complete"] or report["summary"]["security"]["failed"] > 0
        or report["summary"]["security"]["unverified"] > 0
        or any(
            row["answer"]["status"] in {"technical_error", "no_answer"} for row in report["cases"]
        )
        or report["mlflow"]["status"] == "failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
