"""Route personal questions and verify evidence before giving it to the chat."""

import re
import unicodedata
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal, NamedTuple
from uuid import UUID

EvidenceHit = dict[str, Any]
Fact = tuple[str | tuple[str, str], Decimal | None, str | None, date | None]
Overview = Literal["indicators", "summary", "results", "comparison"]
DocumentReference = UUID | str | tuple[int, ...] | None

_PERSONAL = re.compile(
    r"\b(?:(?:мой|мои|мое(?:го|му)?|моя|мою|мо[еи][мйх]|моими|my)\s+"
    r"(?:(?:последн\w*|last|latest|lab)\s+)?"
    r"(?:анализ\w*|результат\w*|выписк\w*|отчет\w*|заключени\w*|документ\w*|"
    r"файл\w*|уровень|reports?|results?|tests?|files?|documents?|levels?)|загруженн\w*|"
    r"прикрепл[её]нн\w*|приложенн\w*|в (?:этом|этих) (?:документ\w*|отчет\w*|"
    r"заключении|файл\w*|анализ\w*|результат\w*|pdf|docx)|в этой выписке|"
    r"по (?:этой выписке|этому заключению|этому отчету)|"
    r"(?:что|указан\w*|написан\w*|сказан\w*) в (?:документе|выписке|отчете|pdf|docx)|"
    r"uploaded|attached|(?:in|from) (?:this|these) (?:report|document|file|results)|"
    r"what does the (?:report|document|file) say|сравни\w* (?:документы|отчеты)|"
    r"(?:в|из|по) извлеченн\w* текст\w*|(?:in|from) the extracted text)\b"
)
_EXACT_INTENT = re.compile(
    r"\b(?:сколько|когда|како[ей] дат\w*|дат[аеуы]|числ\w*|значени\w*|уров\w*|"
    r"концентрац\w*|доз[аыеу]\w*|мутаци\w*|вариант\w*|ген[аеуы]?|"
    r"how much|how many|when|date|value|level|concentration|mutation|variant|gene)\b"
)
_ASKS_DATE = re.compile(r"\b(?:когда|как\w* дат\w*|дат[аеуы]|when|date)\b")
_DOCUMENT_DATE = re.compile(
    r"\b(?:какая дата|какой датой|датирован\w*|дата (?:моего )?(?:анализа|отчета|забора)|"
    r"когда (?:выполнен\w*|сделан\w*|проведен\w*) (?:анализ|отчет)|"
    r"date of (?:my |the )?(?:analysis|report|test|sample)|"
    r"(?:документ\w*|отчет\w*) (?:за|от)|(?:documents?|reports?) (?:dated|from|on))\b"
)
_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})(?!\d)")
_DATE_LIKE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
_NUMBER = r"[+-]?\d+(?:[.,]\d+)?"
_QUESTION_NUMBER = re.compile(rf"(?<![\w.]){_NUMBER}(?![\d.,]\d|\w)")
_GENE = re.compile(
    r"\b(?:BRCA[12]|BRAF|MAP2K1|EGFR|KRAS|NRAS|TP53|PTEN|ALK|ERBB2|APOE|CFTR|MTHFR|HFE)\b",
    re.I,
)
_VARIANT = re.compile(
    r"\b(?:[cgnrm]\.\d+(?:[_+-]\d+)*(?:[ACGT]+>[ACGT]+|(?:delins|del|dup|ins)[ACGT]*)"
    r"|(?:p\.)?[ACDEFGHIKLMNPQRSTVWY]\d+[ACDEFGHIKLMNPQRSTVWY*]"
    r"|p\.[a-z]{3}\d+[a-z]{3})(?!\w)",
    re.I,
)
_INDICATORS = re.compile(
    r"(?<!\w)(?:(?P<hba1c>hba1c|гликированн\w* гемоглобин\w*|glycated hemoglobin|"
    r"(?:гемоглобин\w*|hemoglobin) a1[cс])|(?P<hgb>hgb|гемоглобин\w*|hemoglobin)|"
    r"(?P<tsh>ттг|tsh|тиреотропн\w* гормон\w*|thyroid-stimulating hormone)|"
    r"(?P<potassium>кали[йяюеи]|potassium)|(?P<sodium>натри[йяюеи]|sodium)|"
    r"(?P<calcium>кальци[йяюеи]|calcium)|(?P<magnesium>магни[йяюеи]|magnesium)|"
    r"(?P<fasting_glucose>глюкоз\w* натощак|fasting (?:blood )?glucose)|"
    r"(?P<glucose>глюкоз\w*|(?:blood )?glucose)|(?P<creatinine>креатинин\w*|creatinine)|"
    r"(?P<ferritin>ферритин\w*|ferritin)|(?P<crp>срб|crp|[сc]-реактивн\w* бел\w*|"
    r"c-reactive protein)|(?P<wbc>лейкоцит(?:ы|ов|ам|ами|ах|а|у|ом|е)?|leukocytes|wbc)|"
    r"(?P<plt>тромбоцит(?:ы|ов|ам|ами|ах|а|у|ом|е)?|platelets|plt)|"
    r"(?P<ldl>холестерин\w* лпнп|лпнп(?:[- ]холестерин\w*)?|ldl(?:[- ]cholesterol)?)|"
    r"(?P<hdl>холестерин\w* лпвп|лпвп(?:[- ]холестерин\w*)?|hdl(?:[- ]cholesterol)?)|"
    r"(?P<cholesterol>общ\w* холестерин\w*|холестерин\w*|(?:total )?cholesterol)|"
    r"(?P<alt>алт|alt)|(?P<ast>аст|ast))(?!\w)"
)
_INDICATOR_LABELS = {
    "hba1c": "HbA1c",
    "hgb": "Гемоглобин",
    "tsh": "ТТГ",
    "potassium": "Калий",
    "sodium": "Натрий",
    "calcium": "Кальций",
    "magnesium": "Магний",
    "fasting_glucose": "Глюкоза натощак",
    "glucose": "Глюкоза",
    "creatinine": "Креатинин",
    "ferritin": "Ферритин",
    "crp": "С-реактивный белок",
    "wbc": "Лейкоциты",
    "plt": "Тромбоциты",
    "ldl": "Холестерин ЛПНП",
    "hdl": "Холестерин ЛПВП",
    "cholesterol": "Холестерин",
    "alt": "АЛТ",
    "ast": "АСТ",
}
_UNIT_ALIASES = {
    "mmol/l": ("ммоль/л",),
    "umol/l": ("µmol/l", "μmol/l", "мкмоль/л"),
    "mg/l": ("мг/л",),
    "mg/dl": ("мг/дл",),
    "g/l": ("г/л",),
    "g/dl": ("г/дл",),
    "ng/ml": ("нг/мл",),
    "miu/l": ("мме/л",),
    "µiu/ml": ("μiu/ml", "uiu/ml", "мкме/мл"),
    "iu/l": ("ме/л", "ед/л", "u/l"),
    "10^9/l": ("10^9/л", "x10^9/л", "×10^9/л", "x10^9/l", "×10^9/l"),
    "%": (),
}
_UNITS = {alias: unit for unit, aliases in _UNIT_ALIASES.items() for alias in (unit, *aliases)}
_UNIT_NAMES = "|".join(re.escape(unit).replace("/", r"\s*/\s*") for unit in _UNITS)
_UNIT = re.compile(rf"(?<![^\W\d])(?<!/)(?:{_UNIT_NAMES})(?![\w/])")
_MEASUREMENT = re.compile(rf"[\s:=|—–]+(?P<value>{_NUMBER})\s*\|?\s*(?P<unit>{_UNIT_NAMES})")
_QUANTITY_SUFFIX = re.compile(r"^\s*([a-zа-яµμ][a-zа-яµμ0-9/^]*)\b")
_AFTER_QUANTITY = {"а", "в", "за", "и", "или", "на", "от", "по", "and", "for", "in", "on", "or"}
_DATE_LABEL = r"(?:дата(?: (?:анализа|отчета|забора))?|(?:(?:report|sample|test) )?date)\s*[:=]?\s*"
_DATE_FIELD = re.compile(rf"(?:{_DATE_LABEL})?{_DATE.pattern}")
_DATED_TAIL = re.compile(rf"\s+\((?:{_DATE_LABEL})?{_DATE.pattern}\)$")
_REFERENCE_TAIL = re.compile(
    r"\s*\((?:референс\w*|норма|reference(?: range)?)\b[^()]*\)$"
)
_REFERENCE_INTERVAL = re.compile(
    rf"\s*(?:\(|\|\s*)(?:референс\w*(?: значени\w*| интервал| диапазон)?|"
    rf"норма|reference(?: range| interval)?)"
    rf"\s*:?\s*(?P<low>{_NUMBER})\s*[-–—]\s*(?P<high>{_NUMBER})"
    rf"(?:\s*(?P<unit>{_UNIT_NAMES}))?\s*(?:\))?$"
)
_DATE_KINDS = {
    "sample": re.compile(r"\b(?:дат[ауые] забора|sample date|date of (?:(?:my|the) )?sample)\b"),
    "report": re.compile(
        r"\b(?:дат[ауые] (?:моего )?отчета|report date|date of (?:(?:my|the) )?report)\b"
    ),
    "test": re.compile(
        r"\b(?:дат[ауые] (?:моего )?анализа|test date|date of (?:(?:my|the) )?test)\b"
    ),
}
_STATEMENT = re.compile(r"(?P<text>[^\n;]*?)(?:[.!?](?=\s|$)|[\n;]+|\Z)")
_FILENAME = re.compile(
    r"""["«]([^"»\n]+\.(?:pdf|docx))["»]|([\w.-]+\.(?:pdf|docx))(?![\w-]|\.[\w.-])""",
    re.I,
)
_DOCUMENT_LINK = re.compile(
    r"(?:https?://[^/\s]+)?/documents/"
    r"([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})(?![\w/-])(?:#[\w:-]+)?",
    re.I,
)
_NUMBERED_DOCUMENT = re.compile(
    r"\b(?:документ\w*|анализ\w*|файл\w*)\s+"
    r"(?:№\s*|номер\s+)?(?P<numbers>[0-9]{1,19}(?![\w/%-]|,\d|\.[\w.-])"
    rf"(?!\s*(?:{_UNIT_NAMES})(?![\w/])|\s+года?\b)"
    r"(?:\s*(?:,|и\s+|and\s+)\s*(?:№\s*|номер\s+)?"
    r"[0-9]{1,19}(?![\w/%-]|,\d|\.[\w.-]))*)",
    re.I,
)
_LIST_INDICATORS = re.compile(
    r"\b(?:(?:перечисл\w*|список|перечень|какие) (?:все )?(?:лабораторн\w* )?показател\w*|"
    r"(?:list|which|what) (?:lab )?(?:markers|indicators|analytes))\b"
)
_SUMMARY = re.compile(
    r"\b(?:резюм\w*|сделай кратк\w* обзор|кратко перескажи|"
    r"что в (?:документе|анализе|файле)|"
    r"о чем (?:(?:этот|мой) )?(?:документ|файл|извлеченный текст)|summari[sz]\w*|overview)\b"
)
_WORDS = re.compile(r"[^\W\d_]{4,}")
_GENERIC = set(
    "скажи расскажи покажи объясни найди документ документы результаты указано указаны сказано "
    "написано информация говорится выписка выписке анализ анализе анализах данных "
    "пожалуйста какое какие какой какая было были this that about tell show find "
    "please report document analysis results information according says what does".split()
)

MISSING = "Подтверждения в документах не найдены. Уточните показатель, дату или документ."
CONFLICT = "В найденных фрагментах есть разные результаты. Уточните дату или документ."


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("−", "-").replace("ё", "е")


def needs_documents(question: str) -> bool:
    query = normalized(question)
    return bool(
        _PERSONAL.search(query)
        or _FILENAME.search(question)
        or _DOCUMENT_LINK.search(question)
        or _NUMBERED_DOCUMENT.search(question)
        or re.fullmatch(r"\s*(?:а (?:что )?у меня|what about mine)[?!.\s]*", query)
        or (
            re.search(r"\b(?:у меня|мой|мои|моего|моей|my)\b", query)
            and (_INDICATORS.search(query) or _GENE.search(query) or _VARIANT.search(query))
        )
    )


def document_reference(question: str) -> tuple[DocumentReference, str]:
    links = {UUID(match[1]) for match in _DOCUMENT_LINK.finditer(question)}
    if len(links) > 1:
        return "", question
    if links:
        return links.pop(), _FILENAME.sub(" ", _DOCUMENT_LINK.sub(" ", question))
    numbered = list(_NUMBERED_DOCUMENT.finditer(question))
    if numbered:
        numbers = tuple(dict.fromkeys(
            int(number) for match in numbered for number in re.findall(r"[0-9]+", match["numbers"])
        ))
        if len(numbers) > 5 or any(number > 2**63 - 1 for number in numbers):
            return "", question
        return numbers, _NUMBERED_DOCUMENT.sub(" ", question)
    references = list(_FILENAME.finditer(question))
    if not references:
        return None, question
    names = {(match[1] or match[2]).lower() for match in references}
    if len(names) != 1:
        return "", question
    return names.pop(), _FILENAME.sub(" ", question)


def overview_kind(question: str) -> Overview | None:
    query = normalized(question)
    if re.search(r"\b(?:сравни\w*|сравнение|compare)\b", query):
        return "comparison"
    if re.search(
        r"\b(?:(?:объясни\w*|проанализируй\w*) результаты|расшифруй\w*|референс\w*|"
        r"выходят за норму|explain (?:the |my )?results|reference ranges?)\b",
        query,
    ):
        return "results"
    if _LIST_INDICATORS.search(query):
        return "indicators"
    explain_document = re.search(
        r"\b(?:объясни\w*|проанализируй\w*) (?:этот |мой )?"
        r"(?:документ\w*|анализ\w*|отчет\w*|выписк\w*)\b",
        query,
    )
    if (_SUMMARY.search(query) or explain_document) and not _EXACT_INTENT.search(query):
        return "summary"
    return None


def indicator_names(text: str) -> list[tuple[str, str]]:
    return [
        (match.lastgroup, _INDICATOR_LABELS[match.lastgroup])
        for match in _INDICATORS.finditer(normalized(text))
        if match.lastgroup is not None
    ]


def indicator_key(text: str) -> str | None:
    match = _INDICATORS.fullmatch(normalized(text))
    return match.lastgroup if match else None


def date_values(text: str) -> set[date] | None:
    found = set()
    for match in _DATE_LIKE.finditer(text):
        raw = match[0]
        if _DATE.fullmatch(raw) is None:
            return None
        if "." in raw:
            day, month, year = raw.split(".")
            raw = f"{year}-{month}-{day}"
        try:
            found.add(date.fromisoformat(raw))
        except ValueError:
            return None
    return found


def unit_key(unit: str) -> str:
    return _UNITS["".join(unit.split())]


def date_kinds(text: str) -> set[str]:
    return {kind for kind, pattern in _DATE_KINDS.items() if pattern.search(text)}


def topic_pairs(text: str) -> set[tuple[str, str]]:
    return set(pairwise(word for word in _WORDS.findall(text) if word not in _GENERIC))


def evidence_statements(hit: EvidenceHit) -> list[EvidenceHit]:
    """Every citation points to the block or page containing the whole statement."""
    offset = hit.get("chunk_start")
    if not isinstance(offset, int) or not hit.get("sources"):
        return []
    statements = []
    text = hit["text"]
    for match in _STATEMENT.finditer(text):
        raw = match["text"]
        excerpt = raw.strip()
        if not excerpt:
            continue
        start = offset + match.start() + len(raw) - len(raw.lstrip())
        end = start + len(excerpt)
        # Skip statements that might have been cut at a chunk boundary.
        covering = [
            source
            for source in hit["sources"]
            if source["start"] <= start < end <= source["end"]
            and (match.start() != 0 or source["start"] == offset)
            and (match.end() != match.end("text") or source["end"] <= offset + len(text))
        ]
        if covering:
            statements.append(
                {**hit, "text": excerpt, "sources": covering, "source_url": covering[0]["url"]}
            )
    return statements


def exact_facts(text: str, indicator: str | None, genetics: bool) -> Fact | None:
    if indicator:
        text = _REFERENCE_TAIL.sub("", text)
    fields = re.split(r"[|\t]", text)
    dated = date_values(text)
    if dated is None or len(dated) > 1:
        return None
    observed_date = next(iter(dated), None)
    if len(fields) > 1 and _DATE_FIELD.fullmatch(fields[0].strip()):
        fields = fields[1:]
    if len(fields) > 1 and _DATE_FIELD.fullmatch(fields[-1].strip()):
        fields = fields[:-1]
    text = "|".join(fields).strip()
    dated_tail = _DATED_TAIL.search(text)
    if dated_tail:
        text = text[: dated_tail.start()]
    if indicator:
        marker = _INDICATORS.match(text)
        if marker is None or marker.lastgroup != indicator:
            return None
        tail = text[marker.end() :]
        alias = re.match(r"\s*\(([^()]+)\)", tail)
        if alias:
            additional = _INDICATORS.fullmatch(alias[1])
            if additional is None or additional.lastgroup != indicator:
                return None
            tail = tail[alias.end() :]
        value = _MEASUREMENT.fullmatch(tail)
        if value is not None:
            number = Decimal(value["value"].replace(",", "."))
            return indicator, number, unit_key(value["unit"]), observed_date
    elif genetics:
        gene = _GENE.match(text)
        if gene is not None:
            variant = _VARIANT.fullmatch(text[gene.end() :].lstrip(" :=|\t"))
            if variant is not None:
                return (gene[0], variant[0]), None, None, observed_date
    elif observed_date and re.match(_DATE_LABEL, text) and _DATE_FIELD.fullmatch(text):
        return "date", None, None, observed_date
    return None


class Measurement(NamedTuple):
    indicator: str
    label: str
    number: Decimal
    unit: str
    status: str


def measurement(statement: str) -> Measurement | None:
    text = normalized(statement)
    marker = _INDICATORS.search(text)
    if marker is None or marker.lastgroup is None:
        return None
    interval = _REFERENCE_INTERVAL.search(text)
    value_text = text[: interval.start()] if interval else text
    fact = exact_facts(value_text, marker.lastgroup, False)
    if fact is None or fact[1] is None or fact[2] is None:
        return None
    number, unit = fact[1], fact[2]
    pending = "сопоставимый референсный интервал не подтверждён"
    if interval is None:
        return Measurement(marker.lastgroup, marker[0], number, unit, pending)
    reference_unit = interval["unit"]
    low = Decimal(interval["low"].replace(",", "."))
    high = Decimal(interval["high"].replace(",", "."))
    if low > high or (reference_unit is not None and unit_key(reference_unit) != unit):
        status = pending
    elif number < low:
        status = "ниже указанного интервала"
    elif number > high:
        status = "выше указанного интервала"
    else:
        status = "в пределах указанного интервала"
    return Measurement(marker.lastgroup, marker[0], number, unit, status)


def select_evidence(question: str, hits: list[EvidenceHit]) -> tuple[str | None, list[EvidenceHit]]:
    """Return a fixed factual answer or supported excerpts for Qwen."""
    query = normalized(question)
    indicators = {match.lastgroup for match in _INDICATORS.finditer(query)}
    genes = set(_GENE.findall(query))
    without_indicators = _INDICATORS.sub(" ", query)
    variants = set(_VARIANT.findall(without_indicators))
    dates = date_values(query)
    if dates is None:
        return MISSING, []
    units = {unit_key(match[0]) for match in _UNIT.finditer(query)}
    kinds = date_kinds(query)
    numeric_query = _UNIT.sub(" ", _VARIANT.sub(" ", _DATE.sub(" ", without_indicators)))
    if re.search(r"\d\s*[-–—]\s*\d|\d[eE][+-]?\d|(?<![\w\d])[.,]\d", numeric_query):
        return MISSING, []
    number_matches = list(_QUESTION_NUMBER.finditer(numeric_query))
    for match in number_matches:
        suffix = _QUANTITY_SUFFIX.match(numeric_query[match.end() :])
        if suffix and suffix[1] not in _AFTER_QUANTITY:
            return MISSING, []
    values = {Decimal(match[0].replace(",", ".")) for match in number_matches}
    if any(len(scope) > 1 for scope in (indicators, genes, variants, dates, units, values, kinds)):
        return MISSING, []
    indicator = next(iter(indicators), None)
    genetics = bool(genes or variants)
    if genetics and (indicator or not genes):
        return MISSING, []
    if genetics and re.search(r"\b(?:[cgnrmp]\.\d|rs\d)", numeric_query):
        return MISSING, []
    exact = bool(indicator or genetics or dates or units or values or _EXACT_INTENT.search(query))
    statements = [statement for hit in hits for statement in evidence_statements(hit)]
    if not exact:
        topic = topic_pairs(_PERSONAL.sub(" ", query))
        relevant = [
            hit for hit in statements if topic.intersection(topic_pairs(normalized(hit["text"])))
        ]
        return (None, relevant) if relevant else (MISSING, [])
    if (units or values) and not indicator:
        return MISSING, []
    if not (indicator or genetics or _DOCUMENT_DATE.search(query)):
        return MISSING, []
    if re.search(r"(?:[<>≤≥≈]=?\s*[+-]?\d|\b(?:выше|ниже|больше|меньше|over|under)\s+\d)", query):
        return MISSING, []
    needs_date = bool(dates or _ASKS_DATE.search(query))
    facts: dict[Fact, EvidenceHit] = {}
    for statement in statements:
        fact = exact_facts(normalized(statement["text"]), indicator, genetics)
        if fact is None:
            continue
        identity, number, unit, observed_date = fact
        if kinds and date_kinds(normalized(statement["text"])) != kinds:
            continue
        if (needs_date and observed_date is None) or (dates and observed_date not in dates):
            continue
        if genetics:
            assert isinstance(identity, tuple)
            gene, variant = identity
            if gene not in genes:
                continue
        facts.setdefault(fact, statement)
    if len(facts) > 1:
        return CONFLICT, []
    for (_, number, unit, _), statement in facts.items():
        if (values and number not in values) or (units and unit not in units):
            break
        if variants and variants.isdisjoint(_VARIANT.findall(normalized(statement["text"]))):
            break
        return f"В документе указано: {statement['text']}", [statement]
    return MISSING, []
