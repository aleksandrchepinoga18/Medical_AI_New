"""Conservative checks on model prose. Source text remains the evidence."""

import re
from typing import Any

from app.chat.evidence import indicator_names, normalized
from app.chat.medical_sources import verified_answer_refs

_LABEL = re.compile(r"\\?\[(S[0-9]+)\\?\]")
_DOCUMENT_URL = re.compile(r"(?<![\w/])/(?:api/)?documents/[^\s\])<>]+", re.I)
_DOCUMENT_NUMBER = re.compile(
    r"(?:(?:\b(?:документ\w*|выписк\w*|отч[её]т\w*|document)\s+)"
    r"(?:(?:№|#|номер)\s*)?|№\s*)(\d+)",
    re.I,
)
_SENTENCE = re.compile(r"\n+|(?<=[.!?])\s+")
_QUANTITY = re.compile(
    r"(?<![\w.,])([+<>≤≥~-]?\d+(?:[.,]\d+)?"
    r"(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?)"
    r"\s*(%|[a-zа-яµμ]{1,12}\s*/\s*[a-zа-я]{1,10}|mmhg|мм\s*рт\.?\s*ст\.?)",
    re.I,
)
_NUMBER = re.compile(r"(?<![\w.,])[+<>≤≥~-]?\d+(?:[.,]\d+)?")
_RANGE = re.compile(r"\d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}\.\d{2}\.\d{4}\b")
_THRESHOLD = re.compile(r"референс|порог|норм[аыеу]|диапазон|cutoff|threshold|reference", re.I)
_CALCULATED = re.compile(r"разниц|изменени|динамик|вырос|снизил|difference|change", re.I)
_CLINICAL = re.compile(
    r"\b(?:у (?:вас|пациента)\s+(?:точно\s+|подтвержд[её]н\w*\s+)?"
    r"(?:диабет|анеми|гипертензи|гипотиреоз|диагноз|болезнь|рак|инфаркт)\w*"
    r"|(?:подтвержда\w*|доказыва\w*|исключа\w*)\s+диагноз"
    r"|принимайте|отмените|начните\s+при[её]м|назначаю|(?:необходимо|следует)\s+принимать"
    r"|гарантиру\w*\s+(?:лечение|излечение)|(?:снижа\w*|повыша\w*)\s+риск"
    r"|эффективност\w*\s+лечени\w*|you have|start taking|reduces?\s+(?:the\s+)?risk)\b",
    re.I,
)
_RESEARCH = re.compile(
    r"\b(?:pubmed|pubmed central|pmc\d+|pmid|doi|исследовани\w*|стать\w*|"
    r"публикаци\w*|автор\w*|аннотаци\w*|полный текст|study|article|abstract)\b",
    re.I,
)
_RESEARCH_CLAIM = re.compile(
    r"показа\w*|выяви\w*|доказа\w*|подтверди\w*|результат\w*|found|showed|proved|result", re.I,
)
_AVERAGE_GLUCOSE = re.compile(
    r"\b(?:average(?:\s+[\w-]+){0,5}\s+(?:glucose|sugar)"
    r"|средн\w*(?:\s+[\w-]+){0,5}\s+(?:глюкоз\w*|сахар\w*))\b",
)
_PERIOD_QUESTION = re.compile(r"период|сколько времени|сколько месяц|how long|period")
_MONTH_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
}
_MONTH_NUMBER = r"(?:\d{1,2}|" + "|".join(_MONTH_NUMBERS) + r")"
_APPROXIMATE = r"(?:about|roughly|approximately|примерно|около|порядка|приблизительно)"
_DURATION = (
    rf"(?:{_APPROXIMATE}\s+)?(?P<low>{_MONTH_NUMBER})"
    rf"(?:\s*(?:[-–—]|\bto\b|\bдо\b)\s*(?P<high>{_MONTH_NUMBER}))?"
    r"\s+(?P<unit>months?|weeks?|years?|days?|месяц(?:а|ев)?"
    r"|недел[яиюь]|год(?:а|ов)?|лет|д(?:ень|ня|ней))\b"
)
_BARE_DURATION = re.compile(_DURATION)
_PAST_DURATION = re.compile(
    r"(?<!\w)(?:(?:over|during|in)\s+(?:the\s+)?(?:past|last|previous)"
    r"|(?:past|last|previous)|за(?:\s+(?:последн\w*|прошл\w*|предыдущ\w*))?"
    r"|(?:в течение|на протяжении)\s+(?:последн\w*|прошл\w*)|последн\w*|прошл\w*)\s+"
    + _DURATION,
)
LIMITATION = "Часть медицинских утверждений не подтверждается доступными фрагментами."


def source_labels(text: str) -> set[str]:
    return {f"[{match[1]}]" for match in _LABEL.finditer(text)}


def reference_text(text: str, sources: list[dict[str, Any]]) -> str:
    labels = {f"[{source['source_id']}]" for source in sources if source.get("source_id")}
    own_urls = {source["source_url"] for source in sources if source.get("document_id")}
    own_numbers = {str(source.get("document_number")) for source in sources
                   if source.get("document_id")}
    text = verified_answer_refs(text, sources)
    text = _DOCUMENT_URL.sub(
        lambda m: m[0] if m[0].rstrip(".,") in own_urls else "", text,
    )
    text = _DOCUMENT_NUMBER.sub(lambda m: m[0] if m[1] in own_numbers else "", text)
    return _LABEL.sub(lambda m: f"[{m[1]}]" if f"[{m[1]}]" in labels else "", text)


def numeric_text(value: str) -> str:
    value = re.sub(r"(?<=\d),(?=\d)", ".", normalized(value))
    value = re.sub(r"\s*([/–—%-])\s*", r"\1", value)
    return " ".join(value.split())


def has_hba1c(text: str) -> bool:
    return any(name == "hba1c" for name, _ in indicator_names(text))


def month_bounds(match: re.Match[str]) -> tuple[int, int]:
    low, high = match["low"], match["high"] or match["low"]
    return (
        int(low) if low.isdecimal() else _MONTH_NUMBERS[low],
        int(high) if high.isdecimal() else _MONTH_NUMBERS[high],
    )


def hba1c_periods(evidence: list[dict[str, Any]]) -> dict[tuple[int, int], str]:
    periods: dict[tuple[int, int], str] = {}
    for hit in evidence:
        if hit.get("kind") != "medical_knowledge" or not hit.get("source_id"):
            continue
        title_matches = has_hba1c(hit.get("filename", ""))
        for excerpt in _SENTENCE.split(hit["text"]):
            excerpt = normalized(excerpt)
            if not _AVERAGE_GLUCOSE.search(excerpt) or not (title_matches or has_hba1c(excerpt)):
                continue
            for match in _PAST_DURATION.finditer(excerpt):
                low, high = month_bounds(match)
                if match["unit"].startswith(("month", "месяц")) and 0 < low <= high:
                    periods.setdefault((low, high), hit["source_id"])
    return periods


def review_hba1c_period(
    sentence: str, candidates: list[dict[str, Any]], question: str,
) -> tuple[str, bool, bool]:
    statement = normalized(_LABEL.sub("", sentence)).strip(" .!?*")
    if not has_hba1c(f"{statement} {question}"):
        return sentence, True, False
    matches = list(_PAST_DURATION.finditer(statement))
    if not matches and has_hba1c(question) and _PERIOD_QUESTION.search(normalized(question)):
        if bare := _BARE_DURATION.fullmatch(statement):
            matches = [bare]
    if not matches:
        if _AVERAGE_GLUCOSE.search(statement) and _BARE_DURATION.search(statement):
            return sentence, False, False
        return sentence, True, False
    # A short answer such as «За последние три месяца» can refer to the question.
    short = len(matches) == 1 and not (
        statement[:matches[0].start()] + statement[matches[0].end():]
    ).strip(" .!?*")
    if not _AVERAGE_GLUCOSE.search(statement) and not (short and has_hba1c(question)):
        return sentence, False, False
    periods = hba1c_periods(candidates)
    approximate = re.search(_APPROXIMATE, statement) is not None
    for match in matches:
        if not match["unit"].startswith(("month", "месяц")):
            return sentence, False, False
        bounds = month_bounds(match)
        if bounds not in periods and not (approximate and bounds[0] == bounds[1] and any(
            high - low == 1 and bounds[0] in (low, high) for low, high in periods
        )):
            return sentence, False, False
    remainder = "" if short else _PAST_DURATION.sub(" ", statement)
    if _BARE_DURATION.search(remainder):
        return sentence, False, False
    return remainder, True, True


def numeric_claim_supported(
    sentence: str, candidates: list[dict[str, Any]], calculation: str,
) -> bool:
    statement = numeric_text(_LABEL.sub("", sentence))
    names = {name for name, _ in indicator_names(statement)}
    quantities = {numeric_text(match[0]) for match in _QUANTITY.finditer(statement)}
    ranges = {numeric_text(match[0]) for match in _RANGE.finditer(_DATE.sub("", statement))}
    threshold = bool(_THRESHOLD.search(statement) and _NUMBER.search(statement))
    if not (quantities or ranges or threshold or names and _NUMBER.search(statement)):
        return True
    if not quantities and not ranges:
        quantities = set(_NUMBER.findall(_DATE.sub("", _DOCUMENT_NUMBER.sub("", statement))))
    if not quantities and not ranges:
        return False
    is_delta = _CALCULATED.search(statement) or any(
        value.endswith("%") and value[0] in "+-" for value in quantities
    )
    if is_delta:
        for line in calculation.splitlines():
            data = numeric_text(line)
            if not _CALCULATED.search(data) and "%" not in data:
                continue
            row_names = {key for key, _ in indicator_names(data)}
            if names and row_names and names - row_names:
                continue
            numbers = set(_NUMBER.findall(data))
            if all(value in numbers or (
                (match := _QUANTITY.fullmatch(value)) is not None
                and match[1] in numbers and match[2] in data
            ) for value in quantities | ranges):
                return True
        return False
    for hit in candidates:
        for excerpt in _SENTENCE.split(hit["text"]):
            data = numeric_text(excerpt)
            if names - {name for name, _ in indicator_names(data)}:
                continue
            if threshold and not _THRESHOLD.search(data):
                continue
            if all(re.search(rf"(?<![\w.+<>≤≥-]){re.escape(value)}(?!\w|[.,]\d)", data)
                   for value in quantities | ranges):
                return True
    return False


def review_prose(
    text: str, sources: list[dict[str, Any]], evidence: list[dict[str, Any]],
    calculation: str = "", *, pubmed_attempted: bool = False, question: str = "",
) -> str:
    publications = {
        f"[{source['source_id']}]" for source in sources if source.get("kind") == "pubmed"
    }
    accepted: list[str] = []
    removed = False
    accepted_period = False
    start = 0
    for boundary in (*_SENTENCE.finditer(text), None):
        end = boundary.start() if boundary else len(text)
        sentence = text[start:end]
        start = boundary.end() if boundary else end
        labels = source_labels(sentence)
        candidates = [hit for hit in evidence
                      if not labels or f"[{hit.get('source_id')}]" in labels]
        research = _RESEARCH.search(sentence) and (
            pubmed_attempted or _RESEARCH_CLAIM.search(sentence)
        )
        remainder, period_supported, is_period = review_hba1c_period(sentence, candidates, question)
        if (_CLINICAL.search(sentence) or labels & publications
                or research or not period_supported
                or not numeric_claim_supported(remainder, candidates, calculation)):
            removed = True
            continue
        accepted_period |= is_period
        accepted.append(sentence + (boundary[0] if boundary else ""))
    prose = reference_text("".join(accepted), sources)
    asks_for_period = has_hba1c(question) and _PERIOD_QUESTION.search(normalized(question))
    if not accepted_period and asks_for_period:
        periods = hba1c_periods(evidence)
        if periods and max(low for low, _ in periods) <= min(high for _, high in periods):
            (low, high), label = next(iter(periods.items()))
            value = str(low) if low == high else f"{low}–{high}"
            unit = "месяц" if high == 1 else "месяца" if high in (2, 3, 4) else "месяцев"
            fact = f"HbA1c отражает средний уровень глюкозы за последние {value} {unit} [{label}]."
            prose = "\n\n".join(filter(None, (prose.strip(), fact)))
    if not removed:
        return prose
    return "\n\n".join(filter(None, (prose.strip(), LIMITATION)))


def publication_answer(
    citations: list[dict[str, Any]], excerpts: dict[str, str],
) -> str:
    blocks = []
    for source in citations:
        if source.get("kind") != "pubmed":
            continue
        label = source["source_id"]
        details = [f"PMID {source['pmid']}"]
        details.extend(str(source[key]) for key in ("journal", "year") if source.get(key))
        if source.get("doi"):
            details.append(f"DOI {source['doi']}")
        authors = ", ".join(source.get("authors", []))
        lines = [f"[{label}] {source['filename']}", " · ".join(details)]
        if authors:
            lines.append(authors)
        lines.append(source["source_url"])
        if excerpt := excerpts.get(label):
            lines.extend(("Выдержка из аннотации:", excerpt))
        elif source.get("abstract_available"):
            lines.append("Аннотация есть в PubMed. Её текст не вошёл в контекст ответа.")
        else:
            lines.append("Аннотация отсутствует в записи PubMed.")
        lines.append("Полный текст статьи не проверялся.")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
