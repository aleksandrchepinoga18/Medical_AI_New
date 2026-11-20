"""Resolve owned documents and read their current extracted text."""

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.chat.evidence import (
    MISSING,
    DocumentReference,
    EvidenceHit,
    Measurement,
    Overview,
    document_section,
    evidence_statements,
    indicator_names,
    measurement,
    normalized,
)
from app.chat.lab_tables import has_table_header, split_cells, table_measurements
from app.documents.models import Document, DocumentTextVersion


@dataclass(frozen=True)
class Extraction:
    document: Document
    version: DocumentTextVersion


@dataclass(frozen=True)
class OverviewContext:
    answer: str | None
    hits: list[EvidenceHit]
    notice: str = ""
    partial: bool = False
    supporting_hits: list[EvidenceHit] | None = None


_CLINICAL = re.compile(
    r"\b(?:заключени\w*|анамнез\w*|диагноз\w*|симптом\w*|результат\w*|"
    r"обследован\w*|исследован\w*|описание|образец|findings|impression|diagnosis|"
    r"specimen|assessment|results?)\b"
)
_ADMINISTRATIVE = re.compile(
    r"\b(?:огрн|инн|кпп|телефон|факс|адрес|лицензи\w*|регистратур\w*|"
    r"медицинский центр|больниц\w*|hospital|clinic|address|phone|fax|registration)\b"
)


def current_extractions(
    user_id: UUID,
) -> Select[tuple[Document, DocumentTextVersion]]:
    latest = (
        select(func.max(DocumentTextVersion.version))
        .where(DocumentTextVersion.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    return (
        select(Document, DocumentTextVersion)
        .join(DocumentTextVersion, DocumentTextVersion.document_id == Document.id)
        .where(
            Document.owner_id == user_id,
            or_(
                Document.status == "ready",
                (Document.status == "needs_ocr")
                & DocumentTextVersion.structured_result.is_not(None),
                (Document.status == "failed") & (Document.error_code == "ORIGINAL_MISSING"),
            ),
            DocumentTextVersion.version == latest,
            or_(
                DocumentTextVersion.normalized_text != "",
                DocumentTextVersion.structured_result.is_not(None),
            ),
        )
    )


def resolve_documents(
    db: Session, user_id: UUID, reference: DocumentReference, *, all_structured: bool = False,
    allow_multiple: bool = False,
) -> tuple[list[Extraction], str | None]:
    query = current_extractions(user_id)
    if all_structured and reference is None:
        structured_result = db.execute(
            query.where(DocumentTextVersion.structured_result.is_not(None))
            .order_by(Document.document_number)
        )
        structured_rows = structured_result.all()
        extractions = [Extraction(doc, version) for doc, version in structured_rows]
        return extractions, None if extractions else "Нет документов с сохранёнными измерениями."
    if reference == "":
        return [], "Вставьте одну ссылку на карточку или укажите номера документов."
    if isinstance(reference, UUID):
        query = query.where(Document.id == reference)
    elif isinstance(reference, tuple):
        query = query.where(Document.document_number.in_(reference))
    elif isinstance(reference, str):
        mention = (
            rf"(?:^|[^[:alnum:]_.-]){re.escape(reference)}"
            r"(?![[:alnum:]_-]|\.[[:alnum:]_.-])"
        )
        query = query.where(
            or_(
                func.lower(Document.original_filename) == reference,
                DocumentTextVersion.normalized_text.bool_op("~*")(mention),
            )
        )
    choices = db.execute(
        query.with_only_columns(
            Document.id, Document.document_number, Document.original_filename
        ).order_by(Document.document_number).limit(11)
    ).all()
    if isinstance(reference, tuple):
        found = {number for _, number, _ in choices}
        missing = [number for number in reference if number not in found]
        if missing:
            registered = set(db.scalars(
                select(Document.document_number).where(
                    Document.owner_id == user_id, Document.document_number.in_(missing)
                )
            ))
            return [], " ".join(
                f"Для документа № {number} актуальный извлечённый текст недоступен."
                if number in registered else f"Документ № {number} не найден."
                for number in missing
            )
    elif len(choices) != 1 and not (choices and reference is None and allow_multiple):
        if not choices:
            return [], MISSING
        labels = [f"№ {number} — {name}" for _, number, name in choices[:10]]
        if len(choices) > 10:
            labels.append("Другие документы доступны в личном кабинете.")
        return [], "Уточните документ по номеру:\n" + "\n".join(labels)
    ids = [doc_id for doc_id, _, _ in choices]
    db.execute(
        select(Document.id)
        .where(Document.owner_id == user_id, Document.id.in_(ids))
        .order_by(Document.id)
        .with_for_update(read=True)
    )
    matching_result = db.execute(query.where(Document.id.in_(ids)))
    matching_rows = matching_result.all()
    extractions = [Extraction(doc, version) for doc, version in matching_rows]
    if len(extractions) != len(ids):
        return [], MISSING
    if isinstance(reference, tuple):
        by_number = {item.document.document_number: item for item in extractions}
        return [by_number[number] for number in reference], None
    return extractions, None


def extracted_pages(extraction: Extraction) -> Iterator[EvidenceHit]:
    document, version = extraction.document, extraction.version
    for locator in version.locators:
        start, end = locator["start"], locator["end"]
        if not 0 <= start < end <= len(version.normalized_text):
            continue
        url = f"/documents/{document.id}#source-{locator['kind']}-{locator['index']}"
        yield {
            **extraction_hit(extraction, url, version.normalized_text[start:end]),
            "chunk_start": start,
            "sources": [{**locator, "url": url}],
        }


def section_excerpt(extraction: Extraction, section: str) -> EvidenceHit | None:
    for page in extracted_pages(extraction):
        lines = page["text"].splitlines()
        for index, line in enumerate(lines):
            heading, _, inline = line.partition(":")
            if document_section(heading, heading=True) != section:
                continue
            body = inline.strip()
            if not body:
                following: list[str] = []
                for item in lines[index + 1:]:
                    if document_section(item.partition(":")[0], heading=True):
                        break
                    if item.strip():
                        following.append(item.strip())
                    elif following:
                        break
                body = " ".join(following)
            if normalized(body).rstrip(".") in {"", "нет", "отсутствует", "не указано", "-", "—"}:
                continue
            excerpt = body if len(body) <= 600 else body[:599].rstrip() + "…"
            return {**page, "text": excerpt}
    return None


def extraction_hit(extraction: Extraction, url: str, text: str) -> EvidenceHit:
    document = extraction.document
    return {
        "text": text,
        "document_id": str(document.id),
        "document_number": document.document_number,
        "filename": document.original_filename,
        "source_url": url,
        "sources": [],
        "text_version_id": str(extraction.version.id),
        "extraction_only": True,
        "has_structured": extraction.version.structured_result is not None,
    }


def excerpt_payload(hit: EvidenceHit) -> dict[str, int | str]:
    fragment: dict[str, int | str] = {
        "document_number": hit["document_number"],
        "text": hit["text"],
    }
    if hit["sources"]:
        source = hit["sources"][0]
        key = "page" if source["kind"] == "page" else "block"
        fragment[key] = source["index"] if key == "page" else f"{source['kind']}:{source['index']}"
    return fragment


def excerpt_size(hit: EvidenceHit) -> int:
    return len(json.dumps(excerpt_payload(hit), ensure_ascii=False).encode()) + 2


def page_measurements(page: EvidenceHit) -> Iterator[tuple[EvidenceHit, Measurement]]:
    if not isinstance(page.get("chunk_start"), int) or not page.get("sources"):
        return
    text = page["text"]
    for line, table_result in table_measurements(text):
        yield {**page, "text": line}, table_result
    headed_table = has_table_header(text)
    for statement in evidence_statements(page):
        raw = statement["text"]
        # Once a header supplies the column roles, don't guess their order from a regex.
        if headed_table and len(split_cells(raw)) > 1:
            continue
        statement_result = measurement(raw)
        if statement_result is not None:
            yield statement, statement_result


def ranked_fragments(
    pages: list[EvidenceHit], repeated: Counter[str]
) -> list[tuple[int, EvidenceHit]]:
    candidates: list[tuple[int, int, EvidenceHit]] = []
    for page in pages:
        structured = (
            set() if page.get("has_structured")
            else {row["text"] for row, _ in page_measurements(page)}
        )
        per_priority: Counter[int] = Counter()
        for raw in page["text"].splitlines():
            line = raw.strip()
            if not line:
                continue
            wording = normalized(line)
            if line in structured:
                priority = 3
            elif _ADMINISTRATIVE.search(wording):
                priority = -1
            elif indicator_names(line) or _CLINICAL.search(wording):
                priority = 2
            elif repeated[wording] > 1:
                priority = -1
            else:
                priority = 1
            candidates.append((priority, per_priority[priority], {**page, "text": line}))
            per_priority[priority] += 1
    # For equal priority, take one candidate from each page before taking a second.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [(priority, hit) for priority, _, hit in candidates]


def description_context(
    extractions: list[Extraction], budget: int, notices: list[str]
) -> OverviewContext:
    hits: list[EvidenceHit] = []
    omitted = False
    by_document: list[tuple[int, list[EvidenceHit]]] = []
    repeated: Counter[str] = Counter()
    for extraction in extractions:
        full_text = extraction.version.normalized_text
        number = extraction.document.document_number
        if not full_text.strip():
            return OverviewContext(f"Актуальный извлечённый текст документа № {number} пуст.", [])
        pages = sorted(extracted_pages(extraction), key=lambda page: page["chunk_start"])
        if not pages:
            url = f"/documents/{extraction.document.id}#extracted-text"
            pages = [extraction_hit(extraction, url, full_text)]
        else:
            end = 0
            for page in pages:
                start = page["chunk_start"]
                omitted |= bool(full_text[end:start].strip())
                end = max(end, start + len(page["text"]))
            omitted |= bool(full_text[end:].strip())
        by_document.append((number, pages))
        for page in pages:
            repeated.update({
                normalized(line.strip()) for line in page["text"].splitlines() if line.strip()
            })
    for number, pages in by_document:
        remaining = budget // len(extractions)
        start_count = len(hits)
        candidates = ranked_fragments(pages, repeated)
        substantive = any(priority > 0 for priority, _ in candidates)
        for priority, fragment in candidates:
            if priority < 0 and substantive:
                omitted = True
                continue
            # Count serialized UTF-8 bytes, including escaping and array punctuation.
            cost = excerpt_size(fragment)
            if cost > remaining:
                omitted = True
                continue
            hits.append(fragment)
            remaining -= cost
        if len(hits) == start_count and candidates:
            _, fragment = candidates[0]
            reserved = excerpt_size({**fragment, "text": ""})
            text = fragment["text"].encode()[: max(0, remaining - reserved)].decode(errors="ignore")
            while text and excerpt_size({**fragment, "text": text}) > remaining:
                text = text[: len(text) // 2]
            if text.strip():
                hits.append({**fragment, "text": text.rstrip()})
        if len(hits) == start_count:
            return OverviewContext(
                f"Для документа № {number} нет доступных выдержек в пределах контекста.", []
            )
    if omitted:
        notices.append("Частичный обзор. В контекст вошла только часть извлечённого текста.")
    return OverviewContext(None, hits, "\n".join(notices), omitted)


def indicator_context(extraction: Extraction) -> OverviewContext:
    confirmed: dict[str, EvidenceHit] = {}
    mentioned: dict[str, EvidenceHit] = {}
    for page in extracted_pages(extraction):
        for record, observation in page_measurements(page):
            labels_by_indicator = dict(indicator_names(observation.label))
            label = labels_by_indicator.get(observation.indicator, observation.label)
            confirmed.setdefault(observation.indicator, {**record, "text": label})
        for statement in evidence_statements(page):
            for key, name in indicator_names(statement["text"]):
                mentioned.setdefault(key, {**statement, "text": name})
    groups: list[str] = []
    unconfirmed = [hit for key, hit in mentioned.items() if key not in confirmed]
    if confirmed:
        confirmed_labels = ", ".join(hit["text"] for hit in confirmed.values())
        groups.append(f"Показатели с подтверждённым числом и единицей: {confirmed_labels}")
    else:
        groups.append("Подтверждённых записей с числом и единицей не найдено.")
    if unconfirmed:
        unconfirmed_labels = ", ".join(hit["text"] for hit in unconfirmed)
        groups.append(f"Упоминания без подтверждённого измерения: {unconfirmed_labels}")
    return OverviewContext("\n".join(groups), [*confirmed.values(), *unconfirmed])


def legacy_deviations(extraction: Extraction, question: str, budget: int) -> OverviewContext:
    targets = {key for key, _ in indicator_names(question)}
    outside: dict[str, EvidenceHit] = {}
    inside: list[EvidenceHit] = []
    for page in extracted_pages(extraction):
        for hit, result in page_measurements(page):
            if targets and result.indicator not in targets:
                continue
            if result.status == "в пределах указанного интервала":
                inside.append(hit)
            elif result.status in ("ниже указанного интервала", "выше указанного интервала"):
                text = f"{hit['text']} — {result.status}"
                outside.setdefault(text, {**hit, "text": text})
    if not outside:
        if inside:
            return OverviewContext(
                "В сопоставимых записях отклонений не найдено.", [], supporting_hits=inside,
            )
        return OverviewContext("Нет подтверждённых значений с сопоставимым референсом.", [])
    hits: list[EvidenceHit] = []
    for text, hit in outside.items():
        if len(text) <= budget:
            hits.append(hit)
            budget -= len(text)
    answer = "\n".join(f"• {hit['text']}" for hit in hits)
    return OverviewContext(answer or "Подтверждённые отклонения не поместились в ответ.", hits)


def overview_context(
    extractions: list[Extraction], kind: Overview, budget: int, description_budget: int
) -> OverviewContext:
    if kind == "comparison" and len(extractions) < 2:
        return OverviewContext("Укажите номера как минимум двух документов для сравнения.", [])
    if kind == "summary":
        return description_context(extractions, description_budget, [])
    if kind == "indicators":
        return indicator_context(extractions[0])

    groups: list[str] = []
    missing_measurements: list[str] = []
    displayed_by_doc: list[dict[str, set[Measurement]]] = []
    available_by_doc: list[dict[str, set[Measurement]]] = []
    hits: list[EvidenceHit] = []
    for extraction in extractions:
        records: dict[str, tuple[EvidenceHit, Measurement]] = {}
        for page in extracted_pages(extraction):
            for statement, result in page_measurements(page):
                line = f"{statement['text']} — {result.status}"
                records.setdefault(line, ({**statement, "text": line}, result))
        # Allocate a separate share to each document so the first cannot crowd out the rest.
        remaining = budget // len(extractions)
        displayed: list[str] = []
        displayed_values: dict[str, set[Measurement]] = {}
        available: dict[str, set[Measurement]] = {}
        complete_counts: dict[str, int] = {}
        for line, (record, result) in records.items():
            available.setdefault(result.indicator, set()).add(result)
            complete_counts[result.indicator] = complete_counts.get(result.indicator, 0) + 1
            if len(line) <= remaining:
                hits.append(record)
                displayed.append(f"• {line}")
                displayed_values.setdefault(result.indicator, set()).add(result)
                remaining -= len(line)
        displayed_by_doc.append({
            key: values for key, values in displayed_values.items() if complete_counts[key] == 1
        })
        available_by_doc.append(available)
        number = extraction.document.document_number
        if not records:
            missing_measurements.append(f"Документ № {number}: числовые измерения не подтверждены.")
        header = f"Документ № {number}"
        if len(displayed) < len(records):
            header += f" (частично: {len(displayed)} из {len(records)} записей)"
        groups.append(
            header + "\n" + ("\n".join(displayed) or "Подтверждённых измерений в выдаче нет.")
        )
    if missing_measurements or not hits:
        notices = missing_measurements or ["Числовые записи не поместились в бюджет контекста."]
        if kind == "comparison":
            notices.append("Сравнение по содержанию. Числовое сопоставление не выполнено.")
        description = description_context(extractions, description_budget, notices)
        if kind == "comparison" and hits:
            report = "\n\n".join(groups)
            if description.answer is not None:
                return OverviewContext(f"{description.answer}\n\n{report}", hits)
            return OverviewContext(
                None, description.hits, f"{description.notice}\n\n{report}",
                description.partial, hits,
            )
        return description
    if kind == "comparison" and len(extractions) == 2:
        first, second = displayed_by_doc
        compared: list[str] = []
        missing: list[str] = []
        incompatible: list[str] = []
        left, right = available_by_doc
        for key in dict.fromkeys((*left, *right)):
            sample = next(iter(left.get(key) or right[key]))
            if key not in left or key not in right:
                number = extractions[0 if key not in left else 1].document.document_number
                missing.append(f"• {sample.label}: нет подтверждённого измерения в № {number}")
            elif key not in first or key not in second:
                incompatible.append(f"• {sample.label}: нет однозначной пары в пределах выдачи")
            elif not next(iter(first[key])).unit or not next(iter(second[key])).unit:
                incompatible.append(f"• {sample.label}: единицы не подтверждены")
            elif next(iter(first[key])).unit != next(iter(second[key])).unit:
                incompatible.append(f"• {sample.label}: разные единицы")
        for key, values in first.items():
            if key in second:
                a, b = next(iter(values)), next(iter(second[key]))
                if a.unit and a.unit == b.unit:
                    compared.append(
                        f"• {a.label}: {a.number} → {b.number} {a.unit} (по порядку документов)"
                    )
        if compared:
            groups.append("Сопоставимые пары измерений:\n" + "\n".join(compared))
        else:
            groups.append("Сопоставимые пары измерений не найдены.")
        if missing or incompatible:
            groups.append("Отсутствующие или несопоставимые показатели:\n" + "\n".join(
                missing + incompatible
            ))
    return OverviewContext("\n\n".join(groups), hits)
