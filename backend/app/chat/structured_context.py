"""Build numerical answers from saved records and their verified table sources."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from app.chat.document_context import Extraction, OverviewContext, extracted_pages, overview_context
from app.chat.evidence import EvidenceHit, Overview, indicator_key, indicator_names, normalized
from app.documents.extraction_data import (
    FIELDS,
    RANGE,
    SCALAR,
    StoredMeasurement,
    StructuredExtraction,
    normalize_field,
    strict_text,
)

Mode = Literal["results", "comparison", "deviations", "trend", "change"]
ReferencePosition = Literal["ниже", "выше", "в интервале"]
_NAME_WORD = re.compile(r"[^\W_]+")
_CASE_ENDINGS = (
    "иями", "ями", "ами", "ией", "ием", "ого", "ему", "ыми", "ими", "ах", "ях", "ам", "ям",
    "ом", "ем", "ов", "ев", "ой", "ей", "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю",
    "а", "я", "у", "ю", "е", "ы", "и", "о", "ь", "й",
)


def measurement_mode(question: str, overview: Overview | None) -> Mode | None:
    query = " ".join(normalized(question).split())
    if re.search(r"\b(?:динамик\w*|тренд\w*|trend)\b", query):
        return "trend"
    if re.search(
        r"\b(?:изменил\w*|изменени\w*|разниц\w*|chang(?:e[ds]?|ing)|difference)\b", query,
    ):
        return "change"
    if re.search(
        r"\b(?:отклонени\w*|отклоня\w*|за (?:предел\w* )?норм\w*|вне норм\w*|out of range)\b",
        query,
    ):
        return "deviations"
    if overview == "results" or overview == "comparison":
        return overview
    return None


def structured_digest(data: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def analyte(name: str) -> str:
    return indicator_key(name) or comparable_name(name)


def comparable_name(name: str) -> str:
    return normalized(" ".join(name.split()))


def case_forms(word: str) -> set[str]:
    return {word} | {
        word[:-len(ending)] for ending in _CASE_ENDINGS
        if len(word) >= len(ending) + 4 and word.endswith(ending)
    }


def series_targets(question: str, names: Iterable[str]) -> dict[str, str]:
    targets = dict(indicator_names(question))
    words = [case_forms(word) for word in _NAME_WORD.findall(normalized(question))]
    for name in names:
        if indicator_key(name) is not None:
            continue
        terms = [case_forms(word) for word in _NAME_WORD.findall(normalized(name))]
        if terms and any(
            all(
                term & word
                for term, word in zip(terms, words[start:start + len(terms)], strict=True)
            )
            for start in range(len(words) - len(terms) + 1)
        ):
            targets.setdefault(analyte(name), name)
    return targets


def key_for(row: StoredMeasurement) -> tuple[str | None, ...]:
    return tuple(normalize_field(field, strict_text(getattr(row, field))) for field in FIELDS)


def scalar(raw: str) -> tuple[str, Decimal] | None:
    match = SCALAR.fullmatch(raw.strip())
    if not match:
        return None
    operator, number = match.groups()
    # The same three trailing digits may denote a thousands group.
    if re.fullmatch(r"[+\-−]?[1-9][0-9]{0,2}[.,][0-9]{3}", number):
        return None
    return operator, Decimal(number.replace("−", "-").replace(",", "."))


def deviation(row: StoredMeasurement) -> ReferencePosition | None:
    value = scalar(row.value)
    if not value or value[0] or not row.unit or not row.reference:
        return None
    number = value[1]
    reference = row.reference.strip()
    if interval := RANGE.fullmatch(reference):
        low, high = map(scalar, interval.groups())
        if low and high and low[1] <= high[1]:
            return "ниже" if number < low[1] else "выше" if number > high[1] else "в интервале"
    if limit := scalar(reference):
        operator, boundary = limit
        if operator in ("<", "≤"):
            return "выше" if number > boundary or (number == boundary and operator == "<") else (
                "в интервале"
            )
        if operator in (">", "≥"):
            return "ниже" if number < boundary or (number == boundary and operator == ">") else (
                "в интервале"
            )
    return None


def observed_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for format_ in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.strip(), format_).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Record:
    document_number: int
    row: StoredMeasurement
    hit: EvidenceHit

    @property
    def unit(self) -> str | None:
        return normalize_field("unit", " ".join(self.row.unit.split())) if self.row.unit else None


def show(row: StoredMeasurement) -> str:
    value = f"{row.name}: {row.value}" + (f" {row.unit}" if row.unit else "")
    if row.date:
        value += f" ({row.date})"
    if row.reference:
        value += f", референс {row.reference}"
    status = deviation(row)
    if status:
        value += f" — {status} указанного референса" if status != "в интервале" else (
            " — в указанном референсе"
        )
    return value


def saved_records(
    extraction: Extraction, stored: StructuredExtraction, targets: set[str],
) -> tuple[list[Record], Counter[str]]:
    pages = {
        page["sources"][0]["index"]: page for page in extracted_pages(extraction)
        if page["sources"][0]["kind"] == "page"
    }
    data = extraction.version.structured_result
    assert data is not None
    digest = structured_digest(data)
    number = extraction.document.document_number
    seen = set()
    records = []
    warnings: Counter[str] = Counter()
    for report in (stored.local, stored.external):
        if report is None:
            continue
        for row in report.measurements:
            if targets and analyte(row.name) not in targets:
                continue
            page = (
                pages.get(row.source.page)
                if row.source is not None and row.confirmation == "confirmed" else None
            )
            if page is None:
                warnings["ambiguous" if row.confirmation == "ambiguous" else "unverified"] += 1
                continue
            assert row.source is not None
            key = (row.source.page, row.source.bbox, key_for(row))
            if key in seen:
                continue
            seen.add(key)
            records.append(Record(number, row, {
                **page, "text": show(row), "structured_digest": digest,
                "measurement": {
                    **row.model_dump(include=set(FIELDS)), "position": deviation(row),
                },
            }))
    return records, warnings


def comparison(
    all_records: list[Record], visible: list[Record], numbers: list[int],
) -> list[str]:
    grouped: dict[str, dict[int, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for record in all_records:
        grouped[analyte(record.row.name)][record.document_number].append(record)
    visible_ids = {id(record) for record in visible}
    lines = []
    for by_doc in grouped.values():
        if not all(len(by_doc.get(number, [])) == 1 for number in numbers):
            continue
        first, second = (by_doc[number][0] for number in numbers)
        if id(first) not in visible_ids or id(second) not in visible_ids:
            continue
        a, b = scalar(first.row.value), scalar(second.row.value)
        if not first.unit or first.unit != second.unit or not a or not b or a[0] or b[0]:
            continue
        lines.append(
            f"• {first.row.name}: {first.row.value} → {second.row.value} "
            f"{first.row.unit} (№ {numbers[0]} → № {numbers[1]}, разница {b[1] - a[1]:+f})"
        )
    return lines


def trend(records: list[Record], *, partial: bool) -> list[str]:
    dated: dict[date, list[Record]] = defaultdict(list)
    for record in records:
        when = observed_date(record.row.date)
        if when:
            dated[when].append(record)
    result = []
    for when, items in sorted(dated.items()):
        if len(items) != 1:
            result.append(f"• {when}: несколько измерений, однозначная точка отсутствует.")
            continue
        item = items[0]
        result.append(f"• {when} · документ № {item.document_number} · {show(item.row)}")
    if len(dated) < 2:
        result.append("Недостаточно измерений для расчёта динамики.")
    if not partial and len(dated) >= 2 and all(len(items) == 1 for items in dated.values()):
        first, last = dated[min(dated)][0], dated[max(dated)][0]
        a, b = scalar(first.row.value), scalar(last.row.value)
        units = {items[0].unit for items in dated.values()}
        if len(units) == 1 and None not in units and a and b and not a[0] and not b[0]:
            difference = b[1] - a[1]
            result.append(
                f"Изменение между первой и последней датой: {difference:+f} {first.row.unit}."
            )
            if a[1] != 0:
                percent = difference / a[1] * 100
                result.append(f"Процентное изменение: {percent:+.2f}%.")
            else:
                result.append("Процентное изменение не определено при исходном значении 0.")
    return result


def structured_context(
    extractions: list[Extraction], mode: Mode, question: str, budget: int,
    *, targets: set[str] | None = None, earlier_questions: Sequence[str] = (),
    indicators: Sequence[str] = (), confirmed_only: bool = False,
) -> OverviewContext | None:
    if not any(item.version.structured_result is not None for item in extractions):
        return None
    if mode == "comparison" and len(extractions) != 2:
        return OverviewContext("Укажите номера двух документов для сравнения.", [])
    decoded = []
    for item in extractions:
        data = item.version.structured_result
        try:
            stored = StructuredExtraction.model_validate(data) if data is not None else None
        except ValidationError:
            stored = None
        decoded.append((item, stored))
    time_series = mode in ("trend", "change")
    labels = {analyte(name): name for name in indicators}
    if labels:
        targets = set(labels)
    elif time_series:
        names = dict.fromkeys(
            row.name for _, stored in decoded if stored is not None
            for report in (stored.local, stored.external) if report is not None
            for row in report.measurements
        )
        for candidate in (question, *earlier_questions):
            labels = series_targets(candidate, names)
            if labels:
                break
        if not labels:
            if mode == "trend":
                return OverviewContext("Для динамики укажите показатель.", [])
            return None
        targets = set(labels)
    elif targets is None:
        targets = {key for key, _ in indicator_names(question)} if mode == "deviations" else set()
    all_records: list[Record] = []
    visible: list[Record] = []
    groups: list[str] = []
    legacy_hits: list[EvidenceHit] = []
    supporting_hits: list[EvidenceHit] = []
    partial = False
    share = budget // len(extractions) if mode == "comparison" else budget
    for item, stored in decoded:
        data = item.version.structured_result
        number = item.document.document_number
        if data is None:
            if not confirmed_only and mode in ("comparison", "results", "deviations"):
                legacy = overview_context([item], "results", min(share, budget), 0)
                if legacy.answer and legacy.hits:
                    groups.append(legacy.answer)
                    legacy_hits.extend(legacy.hits)
                    budget = max(0, budget - sum(len(hit["text"]) for hit in legacy.hits))
                    continue
            groups.append(f"Документ № {number}: структурированные измерения ещё не сохранены.")
            continue
        if stored is None:
            groups.append(f"Документ № {number}: структурированные измерения недоступны.")
            continue
        records, warnings = saved_records(item, stored, targets)
        partial |= any(report is not None and report.truncated for report in (
            stored.local, stored.external,
        ))
        all_records.extend(records)
        statuses = [deviation(record.row) for record in records] if mode == "deviations" else []
        lines = []
        remaining = min(share, budget)
        for index, record in enumerate(records):
            if statuses and statuses[index] not in ("ниже", "выше"):
                continue
            if time_series and observed_date(record.row.date) is None:
                continue
            cost = len(record.hit["text"])
            if cost > remaining:
                partial = True
                continue
            budget -= cost
            remaining -= cost
            visible.append(record)
            lines.append(f"• {record.hit['text']}")
        if not time_series:
            empty = "Подтверждённые измерения не поместились в ответ." if records else (
                "Подтверждённых измерений нет."
            )
            if mode == "deviations":
                if any(status in ("ниже", "выше") for status in statuses):
                    empty = "Подтверждённые отклонения не поместились в ответ."
                elif "в интервале" in statuses:
                    empty = "В сопоставимых записях отклонений не найдено."
                    supporting_hits.extend(
                        record.hit for record, status in zip(records, statuses, strict=True)
                        if status == "в интервале"
                    )
                else:
                    empty = "Нет подтверждённых значений с сопоставимым референсом."
                if None in statuses and any(status is not None for status in statuses):
                    if not lines:
                        lines.append(empty)
                    lines.append("Часть измерений не имеет сопоставимого референса.")
            groups.append(f"Документ № {number}\n" + ("\n".join(lines) or empty))
        if warnings:
            groups.append(
                f"Документ № {number}: не проверено — {warnings['unverified']}, "
                f"неоднозначно — {warnings['ambiguous']}."
            )
    if mode == "comparison":
        numbers = [item.document.document_number for item in extractions]
        pairs = comparison(all_records, visible, numbers)
        groups.append("\n".join(["Сопоставимые измерения", *pairs]) if pairs else (
            "Однозначные пары с одинаковыми единицами не найдены."
        ))
    if time_series:
        series: dict[str, list[Record]] = defaultdict(list)
        for record in visible:
            series[analyte(record.row.name)].append(record)
        for key, label in labels.items():
            if points := series.get(key):
                groups.append("\n".join([points[0].row.name, *trend(points, partial=partial)]))
            else:
                groups.append(f"{label}: подтверждённых измерений с датой нет в ответе.")
    if partial:
        groups.append("Часть измерений могла быть пропущена при извлечении или подготовке ответа.")
    hits = [record.hit for record in visible] + legacy_hits
    return OverviewContext(
        "\n\n".join(groups), hits, partial=partial, supporting_hits=supporting_hits or None,
    )
