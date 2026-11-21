import json
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from app.chat.agent import AgentEvidence, document_contents
from app.chat.document_context import (
    Extraction,
    excerpt_payload,
    legacy_deviations,
    overview_context,
    page_measurements,
    section_excerpt,
)
from app.chat.evidence import (
    CONFLICT,
    MISSING,
    direct_indicators,
    document_reference,
    evidence_statements,
    indicator_names,
    measurement,
    needs_documents,
    overview_kind,
    select_evidence,
)
from app.chat.lab_tables import table_measurements
from app.chat.structured_context import measurement_mode
from app.documents.extract import extract_pdf
from app.documents.extraction_data import ExtractionResult
from app.documents.models import Document, DocumentTextVersion
from app.documents.providers import local_report
from evaluation.extraction_fixtures import load_fixtures, render


def test_document_tool_packs_all_five_confirmed_measurements_when_they_fit():
    fixture = next(item for item in load_fixtures() if item.id == "04-multipage")
    text = "Синтетическая таблица."
    rows = []
    for page, spec in enumerate(fixture.pages, 1):
        for index, (name, value, unit, reference, when) in enumerate(spec.rows, 1):
            rows.append({
                "name": name, "value": value, "unit": unit, "reference": reference, "date": when,
                "confirmation": "confirmed",
                "source": {"page": page, "bbox": [10, index * 20, 100, index * 20 + 10]},
            })
    doc = Document(id=uuid4(), original_filename="04-multipage.pdf", document_number=6)
    version = DocumentTextVersion(
        id=uuid4(), normalized_text=f"{text}\n{text}",
        locators=[
            {"kind": "page", "index": 1, "start": 0, "end": len(text)},
            {"kind": "page", "index": 2, "start": len(text) + 1, "end": 2 * len(text) + 1},
        ],
        structured_result={"schema_version": 1, "local": {
            "provider": "pymupdf_tables", "provider_version": "synthetic", "measurements": rows,
        }},
    )
    result = document_contents([Extraction(doc, version)], 1100)
    assert sum("measurement" in hit for hit in result.hits) == 5
    rendered = AgentEvidence().render(result, 1100)
    assert len(rendered.encode()) <= 1100
    payload = json.loads(rendered)
    assert "error" not in payload
    assert {source["page"] for source in payload["sources"]} == {1, 2}


def hit(text, *, page=1, filename="synthetic.pdf"):
    url = f"/documents/synthetic#source-page-{page}"
    return {
        "text": text,
        "filename": filename,
        "source_url": url,
        "score": 1.0,
        "chunk_start": 0,
        "sources": [{"kind": "page", "index": page, "start": 0, "end": len(text), "url": url}],
    }


@pytest.mark.parametrize(
    ("query", "personal"),
    [
        ("Как работает паровая турбина?", False),
        ("Как измеряют калий в анализе крови?", False),
        ("Что обычно указывают в медицинской выписке?", False),
        ("What is normally included in the report?", False),
        ("What is HbA1c and why is it measured?", False),
        ("Почему у меня болит голова?", False),
        ("Какой у меня был калий?", True),
        ("Что в прикреплённой выписке?", True),
        ("Объясни результаты моего анализа", True),
        ("Что в выписке про ночной кашель?", True),
        ("What was my latest TSH level?", True),
        ("Summarize report.pdf", True),
    ],
)
def test_personal_routing(query, personal):
    assert needs_documents(query) is personal


def test_overview_and_document_reference_are_separate_from_exact_facts():
    query = "Перечисли все показатели из извлечённого текста 101.02.030.PDF."
    assert needs_documents(query)
    reference, question = document_reference(query)
    assert reference == "101.02.030.pdf"
    assert overview_kind(question) == "indicators"
    assert overview_kind("Какой калий в этом PDF?") is None
    assert overview_kind("Кратко перескажи извлечённый текст") == "summary"
    assert not needs_documents("Какие показатели бывают в общем анализе крови?")
    assert document_reference('Summarize "my report.pdf"')[0] == "my report.pdf"
    assert document_reference("Сравни a.pdf и b.pdf")[0] == ""
    assert document_reference("Что такое a.pdf.bak?")[0] is None


@pytest.mark.parametrize(
    ("question", "kind", "numbers"),
    [
        ("Перечисли показатели из документа № 1", "indicators", (1,)),
        ("Объясни результаты документа № 2", "results", (2,)),
        ("Сравни документы № 1 и № 2", "comparison", (1, 2)),
        ("Объясни документ № 2", "summary", (2,)),
        ("Проанализируй документ № 2", "summary", (2,)),
        ("Проанализируй результаты документа № 2", "results", (2,)),
    ],
)
def test_numbered_overview_requests_route_separately_from_exact_lookup(question, kind, numbers):
    assert needs_documents(question)
    assert overview_kind(question) == kind
    assert document_reference(question)[0] == numbers


def test_deviations_take_priority_over_indicator_inventory():
    question = "Какие показатели документа №3 выходят за пределы нормы?"
    assert overview_kind(question) == "indicators"
    assert measurement_mode(question, overview_kind(question)) == "deviations"


@pytest.mark.parametrize("form", ["гемоглобин", "гемоглобина", "гемоглобином", "гемоглобине"])
def test_direct_measurement_reuses_indicator_aliases(form):
    _, question = document_reference(f"Какой уровень {form} в документе №3?")
    assert direct_indicators(question) == {"hgb"}
    assert not direct_indicators(question + " 21.11.2026")
    assert not direct_indicators("Что сказано о гемоглобинурии?")


def test_named_measurement_explanation_resolves_multiple_declined_indicators():
    question = "Что означают показатели гемоглобина, лейкоцитов и глюкозы в документе №3?"
    reference, remainder = document_reference(question)
    assert reference == (3,) and overview_kind(question) == "results"
    assert direct_indicators(remainder) == {"hgb", "wbc", "glucose"}


def test_grid_pdf_confirms_measurements_and_has_no_conclusion(tmp_path):
    fixture = next(item for item in load_fixtures() if item.id == "01-grid")
    pdf, _ = render(fixture)
    path = tmp_path / "01-grid.pdf"
    path.write_bytes(pdf)
    extracted = ExtractionResult.model_validate_json(json.dumps(
        extract_pdf(path, 2, 10000, include_tables=True),
    ))
    extraction = Extraction(
        Document(id=uuid4(), document_number=3, original_filename=path.name),
        DocumentTextVersion(
            id=uuid4(), normalized_text=extracted.text, locators=extracted.locators,
        ),
    )
    assert section_excerpt(extraction, "conclusion") is None
    rows = local_report(extracted.table_rows, extracted.tables_truncated).measurements
    assert [row.name for row in rows] == ["Гемоглобин", "Лейкоциты", "Глюкоза"]


def test_legacy_deviations_reuses_text_measurements():
    text = "Гемоглобин: 132 г/л (референс 120–160).\nГлюкоза: 6,1 ммоль/л (референс 3,9–5,5)."
    extraction = Extraction(
        Document(id=uuid4(), document_number=3, original_filename="synthetic.pdf"),
        DocumentTextVersion(
            id=uuid4(), normalized_text=text,
            locators=[{"kind": "page", "index": 1, "start": 0, "end": len(text)}],
        ),
    )
    deviations = legacy_deviations(extraction, "Какие показатели выходят за норму?", 1000)
    assert deviations.answer and "6,1" in deviations.answer and "132" not in deviations.answer
    normal = legacy_deviations(extraction, "Гемоглобин вне нормы?", 1000)
    assert normal.answer == "В сопоставимых записях отклонений не найдено."
    assert normal.supporting_hits


def test_indicator_inventory_distinguishes_mentions_from_results():
    assert indicator_names("Раздел о тромбоцитопении") == []
    assert indicator_names("Работа с тромбоцитами") == [("plt", "Тромбоциты")]
    text = "Работа с тромбоцитами.\nPotassium: 4.5 mmol/L."
    version = DocumentTextVersion(
        id=uuid4(), normalized_text=text,
        locators=[{"kind": "page", "index": 1, "start": 0, "end": len(text)}],
    )
    extraction = Extraction(
        Document(id=uuid4(), document_number=1, original_filename="synthetic.pdf"), version
    )
    overview = overview_context([extraction], "indicators", 1000, 1000)
    assert overview.answer == (
        "Показатели с подтверждённым числом и единицей: Калий\n"
        "Упоминания без подтверждённого измерения: Тромбоциты"
    )
    assert [hit["text"] for hit in overview.hits] == ["Калий", "Тромбоциты"]


def test_large_text_without_locators_uses_a_labeled_partial_description():
    extraction = Extraction(
        Document(id=uuid4(), document_number=2, original_filename="synthetic.pdf"),
        DocumentTextVersion(id=uuid4(), normalized_text="Учебное описание. " * 200, locators=[]),
    )
    overview = overview_context([extraction], "results", 1000, 128)
    assert overview.answer is None
    assert "Частичный обзор" in overview.notice
    assert "числовые измерения не подтверждены" in overview.notice
    (fragment,) = overview.hits
    assert len(fragment["text"].encode()) < 128
    assert "chunk_start" not in fragment
    assert fragment["sources"] == []
    assert fragment["source_url"] == f"/documents/{extraction.document.id}#extracted-text"
    assert excerpt_payload(fragment) == {"document_number": 2, "text": fragment["text"]}


def test_unpositioned_extraction_cannot_supply_a_page_or_measurement():
    fragment = {
        "text": "Test | Result | Unit\nPotassium | 4.3 | mmol/L\nКалий: 4,3 ммоль/л.",
        "extraction_only": True,
        "source_url": "/documents/synthetic#extracted-text",
        "sources": [],
    }
    assert evidence_statements(fragment) == []
    assert list(page_measurements(fragment)) == []


def test_ru_en_explicit_columns_and_ambiguous_rows():
    ru = (
        "Показатель\tРезультат\tЕд. изм.\tРеференсные значения\n"
        "Калий\t5,8\tммоль/л\t3,5–5,1\n"
        "Витамин D\t21\tнг/мл\t20–50\n"
        "Ферритин\t30\t\t20–50"
    )
    rows = list(table_measurements(ru))
    assert len(rows) == 2
    assert rows[0][1].status == "выше указанного интервала"
    assert rows[1][1].label == "Витамин D"
    en = "Test | Result | Unit | Reference\nPotassium | 4.3 | mmol/L | 3.5-5.1"
    (_, english), = table_measurements(en)
    assert english.indicator == rows[0][1].indicator
    assert english.unit == rows[0][1].unit
    assert not list(table_measurements("Potassium  4.3  mmol/L  3.5-5.1"))
    mismatched = en.replace("3.5-5.1", "3.5-5.1 mg/L")
    (_, observation), = table_measurements(mismatched)
    assert "не подтверждён" in observation.status


def test_real_pdf_long_shared_header_and_lab_columns_on_last_page(tmp_path: Path):
    def report(number: int, value: str) -> Extraction:
        path = tmp_path / f"report-{number}.pdf"
        with pymupdf.open() as pdf:
            cover = pdf.new_page()
            for line in range(35):
                cover.insert_text((40, 40 + line * 15), f"Synthetic Hospital address line {line}")
            page = pdf.new_page()
            for y, row in (
                (650, ("Test", "Result", "Unit", "Reference")),
                (675, ("Hemoglobin", value, "g/L", "120-160")),
            ):
                for x, cell in zip((40, 210, 310, 425), row, strict=True):
                    page.insert_text((x, y), cell)
            pdf.save(path)
        result = extract_pdf(path, 2, 10000)
        assert result["status"] == "ready"
        return Extraction(
            Document(id=uuid4(), document_number=number, original_filename=path.name),
            DocumentTextVersion(
                id=uuid4(), normalized_text=result["text"], locators=result["locators"]
            ),
        )

    first, second = report(1, "140"), report(2, "168")
    overview = overview_context([first], "summary", 1000, 180)
    assert overview.answer is None and overview.partial
    fragment = excerpt_payload(overview.hits[0])
    assert fragment["page"] == 2 and fragment["document_number"] == 1
    assert "Hemoglobin" in fragment["text"] and "Hospital" not in fragment["text"]

    comparison = overview_context([first, second], "comparison", 2000, 180)
    assert comparison.answer is not None
    assert "140 → 168 g/l" in comparison.answer
    assert "выше указанного интервала" in comparison.answer
    assert "Hospital" not in comparison.answer
    assert {
        (hit["document_number"], hit["sources"][0]["index"]) for hit in comparison.hits
    } == {(1, 2), (2, 2)}


@pytest.mark.parametrize(
    ("question", "reference"),
    [
        ("Что в документе № 1?", (1,)),
        ("Перечисли показатели анализа 2.", (2,)),
        ("Объясни результаты документа номер 3", (3,)),
        ("Сравни документы № 1 и № 2", (1, 2)),
        ("Какой гемоглобин указан в анализе № 5?", (5,)),
        ("Документ за 2025-02-11, страница № 3, значение 5,2 ммоль/л", None),
        ("Анализ 12.02.2025", None),
        ("В анализе 5 ммоль/л", None),
        ("Анализ 2025 года", None),
    ],
)
def test_document_numbers_are_scoped_to_named_document_phrases(question, reference):
    assert document_reference(question)[0] == reference


def test_reference_comparison_checks_value_and_unit_on_the_same_record():
    assert measurement("Калий: 2,5 ммоль/л (норма 3,5–5,1)").status == (
        "ниже указанного интервала"
    )
    assert measurement("Potassium: 5.4 mmol/L (reference 3.5–5.1)").status == (
        "выше указанного интервала"
    )
    assert measurement("Калий: 4,5 ммоль/л (норма 3,5–5,1 мг/л)").status == (
        "сопоставимый референсный интервал не подтверждён"
    )
    assert measurement("Референс: 3,5–5,1 ммоль/л") is None


def test_exact_value_requires_indicator_unit_and_date_in_same_statement():
    question = "В моём анализе калий 5.2 mmol/L от 11.02.2025?"
    correct = hit("Калий | 5,2 | ммоль/л | 2025-02-11.", page=2)
    other_indicator = hit("Глюкоза: 5,2 ммоль/л | 2025-02-11.")
    other_date = hit("Калий: 5,2 ммоль/л | 2025-02-12.")
    separate = hit("Калий: 5,2 ммоль/л.\nДата анализа: 2025-02-11.")
    answer, sources = select_evidence(question, [other_indicator, other_date, separate, correct])
    assert answer == "В документе указано: Калий | 5,2 | ммоль/л | 2025-02-11"
    assert [item["source_url"] for item in sources] == [correct["source_url"]]
    assert select_evidence(question, [other_indicator, other_date, separate]) == (MISSING, [])


@pytest.mark.parametrize(
    "text",
    [
        "Глюкоза: 5,2 ммоль/л. Калий: 4,8 ммоль/л.",
        "Калий: 5,2 мг/л.",
        "Калий: 4,8 ммоль/л (референс 3,5–5,2).",
        "Калий: <5,2 ммоль/л.",
        "Калий\nГлюкоза: 5,2 ммоль/л.",
        "Калий не измерялся. 5,2 ммоль/л.",
        "Натрий: 52 ммоль/л.",
    ],
)
def test_similar_or_unbound_measurement_is_not_evidence(text):
    assert select_evidence("В моих анализах калий 5,2 ммоль/л?", [hit(text)]) == (MISSING, [])


def test_conflicting_values_are_reported_even_if_one_matches_question():
    first = hit("Калий: 5,2 ммоль/л.", page=1)
    second = hit("Калий: 5,3 ммоль/л.", page=2)
    assert select_evidence("Калий 5,2 в моих анализах?", [first, second]) == (CONFLICT, [])
    assert select_evidence("Мой калий 5,2 см?", [first]) == (MISSING, [])
    assert select_evidence("Мой калий >5,2 ммоль/л?", [first]) == (MISSING, [])


def test_date_must_be_labeled_and_valid():
    question = "Какая дата в моём отчёте?"
    assert select_evidence(question, [hit("Число в сноске: 11.02.2025.")]) == (MISSING, [])
    assert select_evidence(question, [hit("Дата отчёта: 31.02.2025.")]) == (MISSING, [])
    answer, sources = select_evidence(question, [hit("Дата отчёта: 11.02.2025.")])
    assert answer == "В документе указано: Дата отчёта: 11.02.2025"
    assert len(sources) == 1
    assert select_evidence("Когда измерили мой калий?", [hit("Калий: 5 ммоль/л.")]) == (MISSING, [])
    assert select_evidence(
        "Какая дата забора в моём анализе?", [hit("Дата отчёта: 11.02.2025.")]
    ) == (MISSING, [])


def test_indicator_aliases_and_subtypes():
    hba1c = hit("Hemoglobin A1c (HbA1c): 5.8%.")
    answer, sources = select_evidence("Мой гликированный гемоглобин 5,8 %?", [hba1c])
    assert answer == "В документе указано: Hemoglobin A1c (HbA1c): 5.8%"
    assert len(sources) == 1
    assert select_evidence("Какой гемоглобин A1c в моём отчёте?", [hba1c])[0] == answer
    total = hit("Холестерин: 5,8 ммоль/л.")
    assert select_evidence("В моём отчёте холестерин ЛПНП 5,8 ммоль/л?", [total]) == (MISSING, [])
    potassium = hit("Калий: 5,2 ммоль/л (референс 3,5–5,1).")
    assert select_evidence("Какой у меня калий 5,2 ммоль/л?", [potassium])[1]


def test_gene_and_variant_must_belong_to_the_same_statement():
    question = "В моём документе BRCA1 c.68_69delAG?"
    unrelated = hit("BRCA1 c.68_69delAC; BRCA2 c.68_69delAG.")
    assert select_evidence(question, [unrelated]) == (MISSING, [])
    matching = hit("BRCA1 c.68_69delAG.", page=2)
    assert select_evidence("В отчёте PALB2 c.68_69delAG?", [matching]) == (MISSING, [])
    assert select_evidence(question, [matching, unrelated]) == (CONFLICT, [])
    answer, sources = select_evidence(question, [matching])
    assert answer == "В документе указано: BRCA1 c.68_69delAG"
    assert sources[0]["source_url"] == matching["source_url"]


def test_citation_points_to_the_page_with_the_whole_statement():
    first = "Не относящийся к вопросу фрагмент."
    second = "TSH: 2.1 mIU/L."
    candidate = hit(first + "\n" + second)
    candidate["sources"][0]["end"] = len(first)
    candidate["sources"].append(
        {
            "kind": "page",
            "index": 2,
            "start": len(first) + 1,
            "end": len(candidate["text"]),
            "url": "/documents/synthetic#source-page-2",
        }
    )
    _, sources = select_evidence("Какой ТТГ в моём отчёте?", [candidate])
    assert sources[0]["source_url"].endswith("#source-page-2")
    assert [source["index"] for source in sources[0]["sources"]] == [2]
    truncated = {**hit("TSH: 2.1 mIU/L"), "chunk_start": 1}
    assert select_evidence("Какой ТТГ в моём отчёте?", [truncated]) == (MISSING, [])


def test_narrative_requires_an_excerpt_with_topic_words():
    question = "Что в моей выписке сказано про ночной кашель?"
    other = hit("Контрольная запись про давление. Ночной режим сна. Сухой кашель.")
    relevant = hit("Ночной кашель отмечен в учебной записи.", page=2)
    assert select_evidence(question, [other]) == (MISSING, [])
    answer, sources = select_evidence(question, [other, relevant])
    assert answer is None
    assert [item["source_url"] for item in sources] == [relevant["source_url"]]
