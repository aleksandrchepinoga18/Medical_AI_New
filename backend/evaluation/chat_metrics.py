"""Deterministic scores over projected, text-free chat benchmark results."""

import re
from collections import Counter
from statistics import mean
from typing import Any

_CITATION = re.compile(r"\[(S\d+)\]")
_PMID = re.compile(r"\bPMID\s*[:№#]?\s*(\d+)", re.I)
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>\])]+", re.I)
_URL = re.compile(r"https?://[^\s<>\])]+|/documents/[^\s<>\])]+", re.I)
_NOTICE = re.compile(r"не подтверждается доступными фрагментами|временно недоступен", re.I)


def ratio(numerator: int | float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def first_relevant_rank(ranked: list[str], relevant: set[str]) -> int | None:
    return next((position for position, alias in enumerate(ranked, 1) if alias in relevant), None)


def retrieval_scores(ranked: list[str], relevant: list[str]) -> dict[str, float]:
    gold = set(relevant)
    if not gold:
        raise ValueError("retrieval_ground_truth_missing")
    ranked = list(dict.fromkeys(ranked))
    scores = {}
    for k in (3, 5):
        hits = len(gold.intersection(ranked[:k]))
        scores.update({f"recall_at_{k}": hits / len(gold), f"precision_at_{k}": hits / k})
    rank = first_relevant_rank(ranked, gold)
    return {**scores, "mrr": 1 / rank if rank else 0.0, "hit_rate": float(bool(rank))}


def normalized_answer(value: str) -> str:
    value = re.sub(r"(?<=\d),(?=\d)", ".", value.casefold())
    for original in ("—", "−"):
        value = value.replace(original, "–" if original == "—" else "-")
    return " ".join(value.split())


def fact_present(answer: str, alternatives: str) -> bool:
    return any(normalized_answer(option) in answer for option in alternatives.split("|"))


def exact_number(answer: str, expected: str) -> bool:
    pattern = re.escape(normalized_answer(expected))
    return bool(re.search(rf"(?<![\w.+-]){pattern}(?!\w|[.,]\d)", normalized_answer(answer)))


def equivalent_number(answer: str, expected: str) -> bool:
    """Recognize formatting variants for diagnostics of the personal fixtures."""
    answer = normalized_answer(answer).replace("**", "")
    answer = re.sub(r"\s*/\s*", "/", answer)
    answer = re.sub(r"\s+%", "%", answer)
    for localized, original in (("ммоль/л", "mmol/l"), ("г/л", "g/l"), ("мг/дл", "mg/dl")):
        answer = answer.replace(localized, original)
    return exact_number(answer, expected)


def citations_valid(text: str, sources: list[dict[str, Any]]) -> bool:
    labels = {source.get("source_id") for source in sources}
    pmids = {source.get("pmid") for source in sources}
    dois = {source["doi"].casefold() for source in sources if source.get("doi")}
    urls = {source.get("source_url") for source in sources} | {
        f"https://doi.org/{doi}" for doi in dois
    }
    return (
        set(_CITATION.findall(text)) <= labels and set(_PMID.findall(text)) <= pmids
        and {doi.rstrip(".,").casefold() for doi in _DOI.findall(text)} <= dois
        and {url.rstrip(".,") for url in _URL.findall(text)} <= urls
    )


def score_answer(
    case: dict[str, Any], text: str, sources: list[dict[str, Any]], aliases: set[str],
    error: str | None,
) -> dict[str, Any]:
    answer = normalized_answer(text)
    facts = [fact_present(answer, fact) for fact in case["facts"]]
    numbers = [exact_number(answer, value) for value in case.get("numeric", [])]
    required = set(case["required_sources"])
    source_count = len(aliases & required)
    reference_ok = citations_valid(text, sources) and "unknown" not in aliases
    expectation = bool(case.get("fault") or case.get("expected_empty"))
    if error:
        status = "technical_error"
    elif not answer:
        status = "no_answer"
    elif not reference_ok or case.get("fault") == "unsupported" and "99.99" in answer:
        status = "unsupported"
    elif all(facts) and all(numbers) and source_count == len(required):
        status = "correct"
    elif any(facts) or any(numbers) or source_count:
        status = "partial"
    else:
        status = "unsupported"
    return {
        "status": status, "fact_hits": sum(facts), "fact_total": len(facts),
        "numeric_hits": sum(numbers), "numeric_total": len(numbers),
        "required_source_hits": source_count, "required_source_total": len(required),
        "citations_valid": reference_ok,
        "extra_warning": bool(_NOTICE.search(text) and not expectation),
    }


def summarize(rows: list[dict[str, Any]], *, live: bool = False) -> dict[str, Any]:
    ordinary = [row for row in rows if "consent_denied" not in row["checks"]
                and (not live or row["llm_backend"] == "live")]
    retrieval = [row["retrieval"] for row in rows if row.get("retrieval")]
    hidden_queries = [row["retrieval_quality"] for row in rows if "retrieval_quality" in row]
    answers = [row["answer"] for row in ordinary]
    completed = [
        row for row in ordinary
        if row["answer"]["status"] not in {"no_answer", "technical_error"}
    ]
    quality = [row["answer"] for row in completed]
    timeouts = {"timeout", "gigachat_timeout", "oauth_timeout"}
    llm_errors = Counter(
        request["error"] for row in ordinary for request in row.get("llm_requests", [])
        if request["error"]
    )
    timed_out = sum(
        row["error"] in timeouts or row["fallback"] in timeouts
        or any(request["error"] in timeouts for request in row.get("llm_requests", []))
        for row in ordinary
    )
    tools = [tool for row in ordinary for tool in row["tools"]]
    critical = [passed for row in rows for passed in row["checks"].values()]
    tool_errors = Counter(tool["error"] for tool in tools if tool["error"])
    error_counts = Counter(row["error"] for row in rows if row["error"])
    error_counts.update(tool_errors)
    error_counts.update(row["error"] for row in retrieval if row["error"])
    repetitions = [row["wall_ms"] for row in ordinary]
    timing = [tool["ms"] for tool in tools]

    def micro(kind: str) -> float | None:
        return ratio(sum(ans[f"{kind}_hits"] for ans in quality),
                     sum(ans[f"{kind}_total"] for ans in quality))

    scores = [row["scores"] for row in retrieval if row.get("scores")]
    return {
        "scenarios": len(rows), "answer_scenarios": len(quality),
        "retrieval": {
            "queries": len(retrieval), "scored": len(scores),
            "hidden_source_queries": len(hidden_queries),
            "hidden_source_queries_with_irrelevant_results": sum(
                bool(quality["irrelevant_sources"]) for quality in hidden_queries
            ),
            **({key: mean(score[key] for score in scores) for key in scores[0]} if scores else {}),
            "mean_ms": mean(row["ms"] for row in retrieval) if retrieval else None,
        },
        "agent": {
            "routing_accuracy": ratio(sum(row["routing_ok"] for row in ordinary), len(ordinary)),
            "tool_success": ratio(sum(not tool["error"] for tool in tools), len(tools)),
            "tool_calls": len(tools),
            "tool_errors": dict(tool_errors),
            "calls_per_question": ratio(len(tools), len(ordinary)),
            "unrequested_pubmed_rate": ratio(sum(row["extra_pubmed"] for row in ordinary),
                                            len(ordinary)),
            "mixed_routing_accuracy": ratio(
                sum(row["routing_ok"] for row in ordinary if row["mixed"]),
                sum(row["mixed"] for row in ordinary),
            ),
            "fallback_rate": ratio(sum(bool(row["fallback"]) for row in ordinary), len(ordinary)),
            "context_errors": sum(row["context_error"] for row in ordinary),
        },
        "answers": {
            "statuses": dict(Counter(answer["status"] for answer in quality)),
            "facts": micro("fact"), "numerical": micro("numeric"),
            "required_sources": micro("required_source"),
            "citation_validity": ratio(
                sum(ans["citations_valid"] for ans in quality), len(quality),
            ),
            "extra_warning_rate": ratio(
                sum(ans["extra_warning"] for ans in quality), len(quality),
            ),
        },
        "reliability": {
            "scenarios_attempted": len(ordinary),
            "outcomes": dict(Counter(answer["status"] for answer in answers)),
            "completed_answers": len(quality),
            "fallback_answers": sum(bool(row["fallback"]) for row in completed),
            "timeout_scenarios": timed_out,
            "llm_request_errors": dict(llm_errors),
            "technical_error_rate": ratio(
                sum(ans["status"] == "technical_error" for ans in answers), len(answers),
            ),
        },
        "security": {
            "checks": len(critical), "passed": critical.count(True),
            "failed": critical.count(False),
            "unverified": critical.count(None),
        },
        "performance": {
            "wall_mean_ms": mean(repetitions) if repetitions else None,
            "tool_mean_ms": mean(timing) if timing else None,
            "llm_calls": sum(row["llm_calls"] for row in ordinary),
            "llm_mean_ms": ratio(
                sum(row["llm_ms"] for row in ordinary), sum(row["llm_calls"] for row in ordinary),
            ),
        },
        "errors": dict(error_counts),
    }


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    compatible = (
        current.get("mode") == previous.get("mode") == "live"
        and {current.get("provider"), previous.get("provider")} == {"ollama", "gigachat"}
        and current.get("dataset_hash") == previous.get("dataset_hash")
        and current.get("commit") == previous.get("commit")
        and current.get("retrieval_backend") == previous.get("retrieval_backend")
        and current.get("retrieval_config") == previous.get("retrieval_config")
        and current.get("selected_cases") == previous.get("selected_cases")
        and current.get("complete") and previous.get("complete")
    )
    if not compatible:
        return {"status": "incompatible_runs"}
    indexes = [
        {row["id"]: row for row in report["cases"]
         if row["llm_backend"] == "live" and row["llm_calls"] > 0
         and not row["fallback"] and not row["error"]}
        for report in (current, previous)
    ]
    ids = sorted(indexes[0].keys() & indexes[1].keys())
    if not ids:
        return {"status": "no_comparable_cases"}
    return {
        "status": "ok", "case_ids": ids,
        "providers": {
            report["provider"]: summarize([indexed[key] for key in ids], live=True)
            for report, indexed in zip((current, previous), indexes, strict=True)
        },
    }
