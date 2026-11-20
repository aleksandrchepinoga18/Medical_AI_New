"""Run the synthetic PDF extraction benchmark from the backend directory."""

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

import httpx
from pydantic import SecretStr, TypeAdapter

from app.documents.extract import ExtractionError
from app.documents.gigachat import (
    MODELS,
    PROMPT,
    GigaChatExtraction,
    GigaChatResult,
    gigachat_tls_context,
)
from evaluation.extraction_fixtures import CORPUS_VERSION, MANIFEST, Evidence, load_fixtures, render
from evaluation.extraction_metrics import count_matches, score
from evaluation.extraction_tracking import register
from evaluation.local_extraction import flat_text, native_tables

REPORT_VERSION = 2
RECORDS = TypeAdapter(list[Evidence])
LEGACY_METRICS = {
    "exact_records", "precision", "recall", "f1", "false_positive_records",
    "false_negative_records", "field_exact_match", "source_accuracy", "quote_accuracy_when_claimed",
}


def verbatim_pages(details: dict[str, Any] | None) -> dict[int, str]:
    if details is None:
        return {}
    text = details["text"]
    return {
        item["index"]: text[item["start"] : item["end"]]
        for item in details["locators"]
        if item["kind"] == "page"
    }


@dataclass
class Method:
    counts: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)
    deletion: Counter[str] = field(default_factory=Counter)
    tokens: Counter[str] = field(default_factory=Counter)
    usage_reports: int = 0

    def add(
        self,
        truth: list[Evidence],
        records: list[Evidence],
        *,
        latency: float | None,
        error: str | None,
        details: dict[str, Any] | None = None,
        verbatim: dict[int, str] | None = None,
    ) -> dict[str, object]:
        counts = count_matches(truth, records, verbatim_pages=verbatim)
        self.counts.update(counts)
        if latency is not None:
            self.latencies.append(latency)
        if error is not None:
            self.errors[error] += 1
        return {
            "metrics": score(counts),
            "measurements": [record.model_dump(mode="json") for record in records],
            "latency_ms": latency,
            "error": error,
            "details": details,
        }

    def summary(self) -> dict[str, object]:
        return {
            **score(self.counts),
            "error_counts": dict(self.errors),
            "documents_with_errors": sum(self.errors.values()),
            "latency_samples": len(self.latencies),
            "latency_median_ms": median(self.latencies) if self.latencies else None,
            "latency_max_ms": max(self.latencies, default=None),
            "total_tokens": self.tokens.get("total_tokens"),
            "token_usage": dict(self.tokens) or None,
            "requests_with_reported_usage": self.usage_reports,
            "deletion_statuses": dict(self.deletion),
        }


def save_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def rescore(input_path: Path, output_path: Path) -> None:
    raw = input_path.read_bytes()
    report = json.loads(raw)
    if report["schema_version"] not in {1, REPORT_VERSION} or not report["documents"]:
        raise ValueError("Unsupported or empty benchmark report")
    totals: dict[str, Counter[str]] = {}
    for document in report["documents"]:
        truth = RECORDS.validate_json(json.dumps(document["expected"]))
        for method, result in document["results"].items():
            found = RECORDS.validate_json(json.dumps(result["measurements"]))
            pages = verbatim_pages(result["details"]) if method == "pymupdf_text" else None
            counts = count_matches(truth, found, verbatim_pages=pages)
            result["metrics"] = score(counts)
            totals.setdefault(method, Counter()).update(counts)
    for method, counts in totals.items():
        previous = report["summary"].get(method, {})
        report["summary"][method] = {
            **{key: value for key, value in previous.items() if key not in LEGACY_METRICS},
            **score(counts),
        }
    report.setdefault("corpus_version", 1)
    report["schema_version"] = REPORT_VERSION
    report["rescored_from_sha256"] = hashlib.sha256(raw).hexdigest()
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    with output_path.open("x", encoding="utf-8") as file:
        file.write(output)


def local_result(
    method: Method,
    extractor: Callable[[Path], tuple[list[Evidence], dict[str, Any]]],
    path: Path,
    truth: list[Evidence],
    *,
    may_quote_source_text: bool = False,
) -> dict[str, object]:
    start = perf_counter()
    records, details, error = [], None, None
    try:
        records, details = extractor(path)
    except (ExtractionError, RuntimeError, ValueError, OSError):
        error = "local_extraction_failed"
    return method.add(
        truth, records, details=details, latency=(perf_counter() - start) * 1000, error=error,
        verbatim=verbatim_pages(details) if may_quote_source_text else None,
    )


async def compare(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    fixtures = load_fixtures()
    if args.fixture:
        fixtures = [fixture for fixture in fixtures if fixture.id == args.fixture]
        if not fixtures:
            parser.error("--fixture must be an ID from document_extraction.json")
    credentials = ""
    context = None
    if args.gigachat_model:
        credentials = os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
        if not credentials:
            parser.error("set GIGACHAT_CREDENTIALS before requesting GigaChat")
        try:
            context = gigachat_tls_context(os.environ.get("GIGACHAT_CA_BUNDLE"))
        except (OSError, ValueError):
            parser.error("could not load GigaChat CA certificates")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except OSError:
        parser.error("--output-dir must be a new writable directory")
    report_path = args.output_dir / "report.json"
    methods = {"pymupdf_text": Method(), "pymupdf_tables": Method()}
    if args.gigachat_model:
        methods["gigachat"] = Method()
    report: dict[str, Any] = {
        "schema_version": REPORT_VERSION,
        "corpus_version": CORPUS_VERSION,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "fixtures": [fixture.id for fixture in fixtures],
        "pymupdf_version": version("pymupdf"),
        "gigachat_model": MODELS.get(args.gigachat_model),
        "gigachat_response_mode": args.gigachat_mode if args.gigachat_model else None,
        "mlflow": {"status": "not_requested" if args.mlflow_uri is None else "pending"},
        "documents": [],
    }
    remote_stopped = False
    async with httpx.AsyncClient(
        verify=context if context is not None else True,
        timeout=httpx.Timeout(60, connect=5), trust_env=False, follow_redirects=False,
    ) as client:
        adapter = (
            GigaChatExtraction(
                client, SecretStr(credentials), args.gigachat_model, args.gigachat_mode,
                prompt=PROMPT.replace("приложенного PDF", "приложенного синтетического PDF", 1),
            )
            if args.gigachat_model else None
        )
        for fixture in fixtures:
            pdf, truth = render(fixture)
            path = args.output_dir / f"{fixture.id}.pdf"
            path.write_bytes(pdf)
            results = {
                "pymupdf_text": local_result(
                    methods["pymupdf_text"], flat_text, path, truth, may_quote_source_text=True,
                ),
                "pymupdf_tables": local_result(
                    methods["pymupdf_tables"], native_tables, path, truth,
                ),
            }
            if adapter is not None:
                method = methods["gigachat"]
                if remote_stopped:
                    results["gigachat"] = method.add(
                        truth, [], latency=None, error="skipped_after_remote_failure",
                    )
                else:
                    remote: GigaChatResult = await adapter.extract(pdf)
                    method.deletion[remote.deletion] += 1
                    if remote.tokens is not None:
                        method.tokens.update(remote.tokens)
                        method.usage_reports += 1
                    details = asdict(remote)
                    for key in ("measurements", "latency_ms", "error"):
                        del details[key]
                    results["gigachat"] = method.add(
                        truth, remote.measurements, latency=remote.latency_ms,
                        error=remote.error, details=details,
                    )
                    remote_stopped = remote.deletion in {"unknown", "failed"} or bool(
                        remote.error and remote.error.startswith("oauth_")
                    )
            report["documents"].append({
                "id": fixture.id,
                "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                "layouts": [page.layout for page in fixture.pages],
                "expected": [row.model_dump(mode="json") for row in truth],
                "results": results,
            })
            report["summary"] = {key: method.summary() for key, method in methods.items()}
            save_report(report_path, report)

    failed = any(method.errors for method in methods.values()) or remote_stopped
    if args.mlflow_uri is not None:
        params = {
            "pymupdf_version": report["pymupdf_version"],
            "corpus_version": str(report["corpus_version"]),
            "manifest_sha256": report["manifest_sha256"],
            "fixtures": ",".join(report["fixtures"]),
        }
        if args.gigachat_model:
            params["gigachat_model"] = report["gigachat_model"]
            params["gigachat_response_mode"] = args.gigachat_mode
        try:
            run_id = register(args.mlflow_uri, report["summary"], params)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            report["mlflow"] = {"status": "failed"}
            failed = True
        else:
            report["mlflow"] = {"status": "registered", "run_id": run_id}
        save_report(report_path, report)
    print(f"Report: {report_path}")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path)
    mode.add_argument("--rescore", type=Path, help="Recalculate metrics from an earlier report")
    parser.add_argument("--report-output", type=Path, help="New JSON file for --rescore")
    parser.add_argument("--fixture", help="Run just this synthetic fixture ID")
    parser.add_argument("--gigachat-model", choices=tuple(MODELS))
    parser.add_argument("--gigachat-mode", choices=("schema", "prompt-json"))
    parser.add_argument("--allow-synthetic-upload", action="store_true")
    parser.add_argument("--mlflow-uri", help="Optional existing loopback tracking server URL")
    args = parser.parse_args()
    if args.rescore is not None:
        if args.report_output is None or any((
            args.fixture, args.gigachat_model, args.gigachat_mode,
            args.allow_synthetic_upload, args.mlflow_uri,
        )):
            parser.error("--rescore requires --report-output and accepts no extraction options")
        try:
            rescore(args.rescore, args.report_output)
        except FileExistsError:
            parser.error("--report-output must be a new file")
        except OSError:
            parser.error("could not read the input report or write the output report")
        except (ValueError, KeyError, TypeError, AttributeError):
            parser.error("invalid or unsupported benchmark report")
        print(f"Report: {args.report_output}")
        return 0
    if args.report_output is not None:
        parser.error("--report-output requires --rescore")
    args.gigachat_mode = args.gigachat_mode or "schema"
    if bool(args.gigachat_model) != args.allow_synthetic_upload:
        parser.error("--gigachat-model and --allow-synthetic-upload must be used together")
    return asyncio.run(compare(args, parser))


if __name__ == "__main__":
    raise SystemExit(main())
