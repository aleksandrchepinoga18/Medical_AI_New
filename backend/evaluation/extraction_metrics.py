"""Measure fidelity of extracted records independently of source accuracy."""

from collections import Counter
from collections.abc import Mapping
from itertools import product

import pymupdf

from app.documents.extraction_data import (
    FIELDS,
    Evidence,
    normalized_key,
    numeric,
    record_key,
    strict_text,
)


def verbatim_matches(actual: Evidence, expected: Evidence, page_text: str) -> bool:
    if not actual.quote or not expected.quote or actual.quote not in page_text:
        return False
    return strict_text(actual.quote) == strict_text(expected.quote.replace(" | ", " "))


def bbox_matches(actual: Evidence, expected: Evidence) -> bool:
    if actual.bbox is None or expected.bbox is None:
        return False
    predicted, truth = pymupdf.Rect(actual.bbox), pymupdf.Rect(expected.bbox)
    intersection = (predicted & truth).get_area()
    union = predicted.get_area() + truth.get_area() - intersection
    return union > 0 and intersection / union >= 0.35


def count_matches(
    expected: list[Evidence],
    predicted: list[Evidence],
    *,
    verbatim_pages: Mapping[int, str] | None = None,
) -> Counter[str]:
    expected_keys = [record_key(record) for record in expected]
    predicted_keys = [record_key(record) for record in predicted]
    expected_normalized = list(map(normalized_key, expected_keys))
    predicted_normalized = list(map(normalized_key, predicted_keys))
    counts: Counter[str] = Counter(expected=len(expected), predicted=len(predicted))
    for kind, wanted, found in (
        ("strict", expected_keys, predicted_keys),
        ("normalized", expected_normalized, predicted_normalized),
    ):
        counts[f"{kind}_correct"] = sum((Counter(wanted) & Counter(found)).values())
    for key in expected_keys:
        for field, value in zip(FIELDS, key, strict=True):
            if value is not None:
                counts[f"{field}_expected"] += 1

    # Compare individual fields one-to-one within an analyte. Take exact records first.
    candidates = []
    for i, j in product(range(len(expected)), range(len(predicted))):
        a, b = expected_keys[i], predicted_keys[j]
        if a[0] is not None and b[0] is not None and a[0].casefold() == b[0].casefold():
            rank = (
                a == b,
                expected_normalized[i] == predicted_normalized[j],
                a[4] is not None and a[4] == b[4],
                expected[i].page == predicted[j].page,
                sum(x is not None and x == y for x, y in zip(a, b, strict=True)),
            )
            candidates.append((rank, i, j))
    seen_expected: set[int] = set()
    seen_predicted: set[int] = set()
    for _, i, j in sorted(candidates, key=lambda candidate: candidate[0], reverse=True):
        if i in seen_expected or j in seen_predicted:
            continue
        seen_expected.add(i)
        seen_predicted.add(j)
        truth, found = expected[i], predicted[j]
        a, b = expected_keys[i], predicted_keys[j]
        for index, field in enumerate(FIELDS):
            want, got = a[index], b[index]
            if want is not None:
                counts[f"{field}_strict"] += int(want == got)
                counts[f"{field}_normalized"] += int(
                    expected_normalized[i][index] == predicted_normalized[j][index]
                )
            if want is None and got is not None:
                counts[f"{field}_unexpected"] += 1
        counts["numeric_correct"] += int(
            a[1] is not None
            and numeric(a[1]) is not None
            and expected_normalized[i][1] == predicted_normalized[j][1]
        )
        counts["paired"] += 1
        counts["page_claims"] += int(found.page is not None)
        counts["bbox_claims"] += int(found.bbox is not None)
        quote_kind = "verbatim" if verbatim_pages is not None else "reconstructed"
        counts[f"{quote_kind}_quote_claims"] += int(bool(found.quote))
        if found.page != truth.page:
            continue
        counts["page_correct"] += 1
        counts["bbox_correct"] += int(bbox_matches(found, truth))
        if verbatim_pages is not None and found.page is not None:
            counts["verbatim_quote_correct"] += int(
                verbatim_matches(found, truth, verbatim_pages.get(found.page, ""))
            )
    return counts


def score(counts: Counter[str]) -> dict[str, object]:
    def rate(correct: int, total: int) -> float | None:
        return correct / total if total else None

    expected, predicted = counts["expected"], counts["predicted"]

    def exact_match(kind: str) -> dict[str, object]:
        tp = counts[f"{kind}_correct"]
        return {
            "exact_records": tp,
            "precision": rate(tp, predicted),
            "recall": rate(tp, expected),
            "f1": rate(2 * tp, predicted + expected),
            "false_positive_records": predicted - tp,
            "false_negative_records": expected - tp,
            "field_exact_match": {
                field: {
                    "correct": counts[f"{field}_{kind}"],
                    "expected": counts[f"{field}_expected"],
                    "unexpected_on_paired": counts[f"{field}_unexpected"],
                    "accuracy": rate(counts[f"{field}_{kind}"], counts[f"{field}_expected"]),
                }
                for field in FIELDS
            },
        }

    return {
        "expected_records": expected,
        "predicted_records": predicted,
        "strict_exact_match": exact_match("strict"),
        "normalized_exact_match": exact_match("normalized"),
        "unmatched_measurements": predicted - counts["paired"],
        "numeric_accuracy": rate(counts["numeric_correct"], counts["value_expected"]),
        "unit_accuracy": rate(counts["unit_normalized"], counts["unit_expected"]),
        "reference_accuracy": rate(counts["reference_normalized"], counts["reference_expected"]),
        "page_accuracy": rate(counts["page_correct"], expected),
        "bbox_accuracy_when_claimed": rate(counts["bbox_correct"], counts["bbox_claims"]),
        "verbatim_quote_accuracy_when_claimed": rate(
            counts["verbatim_quote_correct"], counts["verbatim_quote_claims"]
        ),
        "source_counts_on_paired_records": {
            field: counts[field]
            for field in (
                "page_claims", "page_correct", "bbox_claims", "bbox_correct",
                "verbatim_quote_claims", "verbatim_quote_correct", "reconstructed_quote_claims",
            )
        },
    }
