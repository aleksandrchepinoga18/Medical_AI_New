"""Read-only public evidence and the boundary for PubMed requests from chat."""

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.evidence import needs_documents, normalized
from app.core.config import Settings
from app.knowledge import pubmed
from app.knowledge.models import MedicalChunk, MedicalSource, MedicalVersion
from app.knowledge.schemas import PubMedHit
from app.knowledge.service import eligible

# Outbound topics are application constants. The model can select concepts found
# in the current question, but cannot send arbitrary text or interpolate values.
PUBLIC_TOPICS = {
    "hypertension": r"гипертензи\w*|повышенн\w* давлени\w*",
    "blood pressure": r"артериальн\w* давлени\w*",
    "diabetes": r"диабет\w*",
    "type 2 diabetes": r"диабет\w* (?:2|второго) тип\w*",
    "hba1c": r"гликированн\w* гемоглобин\w*",
    "glucose": r"глюкоз\w*",
    "hemoglobin": r"гемоглобин\w*",
    "anemia": r"анеми\w*",
    "iron deficiency": r"дефицит\w* железа",
    "ferritin": r"ферритин\w*",
    "complete blood count": r"общ\w* анализ\w* крови|оак",
    "cholesterol": r"холестерин\w*",
    "ldl cholesterol": r"лпнп|ldl",
    "triglycerides": r"триглицерид\w*",
    "creatinine": r"креатинин\w*",
    "glomerular filtration rate": r"скф|egfr|клубочков\w* фильтраци\w*",
    "chronic kidney disease": r"хбп|хроническ\w* болезн\w* почек",
    "c-reactive protein": r"срб|crp|с-реактивн\w* бел\w*",
    "thyroid stimulating hormone": r"ттг|tsh|тиреотропн\w* гормон\w*",
    "hypothyroidism": r"гипотиреоз\w*",
    "vitamin d": r"витамин\w* [dд]",
    "vitamin b12": r"витамин\w* [bв]12|кобаламин\w*",
    "alanine aminotransferase": r"алт|alt|аланинаминотрансфераз\w*",
    "aspartate aminotransferase": r"аст|ast|аспартатаминотрансфераз\w*",
    "potassium": r"кали[йяюем]\w*",
    "albumin": r"альбумин\w*",
    "migraine": r"мигрен\w*",
    "asthma": r"астм\w*",
    "obesity": r"ожирени\w*",
    "heart failure": r"сердечн\w* недостаточност\w*",
    "cardiovascular disease": r"сердечно-сосудист\w* заболевани\w*",
    "systematic review": r"систематическ\w* обзор\w*",
    "meta-analysis": r"мета[- ]?анализ\w*",
    "randomized controlled trial": r"рандомизированн\w* исследовани\w*|рки",
}
_TOPICS = {
    topic: re.compile(rf"(?<!\w)(?:{re.escape(topic)}s?|{aliases})(?!\w)", re.I)
    for topic, aliases in PUBLIC_TOPICS.items()
}
_DOI_VALUE = r"10\.[0-9]{4,9}/[A-Za-z0-9][A-Za-z0-9._/:;()\-]{0,119}"
_DOI_INPUT = re.compile(
    rf"(?:doi(?:\s*:\s*|\s+)|https?://(?:dx\.)?doi\.org/)?({_DOI_VALUE})", re.I,
)
_QUESTION_DOI = re.compile(rf"(?<!\w)({_DOI_VALUE})(?![^\s,!?\]\}}])", re.I)
_PUBLICATION_REQUEST = re.compile(
    r"\b(?:doi|pubmed|стать\w*|публикаци\w*|journal|publication|article)\b", re.I,
)
_QUESTION_PMID = re.compile(
    r"\b(?:pmid|pubmed)(?:\s*[:№#]\s*|\s+)([1-9][0-9]{0,9})(?![\w-]|\.[0-9])",
    re.I,
)
_RESEARCH_REQUEST = re.compile(
    r"\b(?:pubmed|пабмед|pmid|doi|стат(?:ья|ью|ьи|ей|ьям|ьях)|публикаци\w*|"
    r"(?:какие|есть(?: ли)?|найди|покажи|приведи|подбери|новые|последни\w*|свежие|"
    r"что (?:говорят|показывают))\s+(?:есть\s+)?исследовани\w*|"
    r"(?:научн\w*|клиническ\w*|рандомизированн\w*)\s+исследовани\w*|"
    r"(?:научн\w*|клиническ\w*)\s+доказательств\w*|"
    r"(?:найди|приведи|какие(?: есть)?)\s+доказательств\w*|"
    r"(?:из|согласно|по данным)\s+исследований|"
    r"мета[- ]?анализ\w*|систематическ\w*\s+обзор\w*|"
    r"stud(?:y|ies)|research|papers?|publications?|articles?|scientific evidence)\b"
    r"|^исследования\b",
)
_DECLINED_RESEARCH = re.compile(
    r"\b(?:без|не\s+(?:ищи|нужны|нужна|нужно|нужен))\s+(?:поиска\s+)?"
    r"(?:(?:новых|научн\w*|клиническ\w*)\s+)?"
    r"(?:pubmed|пабмед|стат\w*|исследовани\w*|публикаци\w*|доказательств\w*)\b",
)
_REFERENCE_QUESTION = re.compile(
    r"^(?:пожалуйста[ ,]+)?(?:что (?:такое|показыва\w*|измеря\w*|означа\w*|"
    r"отража\w*|определя\w*|значит|входит в)|за (?:какой период|сколько времени)|"
    r"какие (?:вещества|показатели|клетки)\b|объясни(?:те)?|what (?:is|are|does)|how long)\b"
)
_REFERENCE_TOPIC = re.compile(
    r"\b(?:электролит\w*|печеночн\w* проб\w*|cbc|electrolytes?|liver (?:function )?tests?)\b"
)
_PERSONAL_OR_CALCULATION = re.compile(
    r"\b(?:мой|моя|мои|моего|моей|моем|моих|моим|мою|у меня|у пациента|my|mine|"
    r"(?:в|из|по) (?:этом |этом же |предыдущем |предыдущему )?(?:документ\w*|файл\w*|"
    r"выписк\w*)|вычисл\w*|посчита\w*|рассчита\w*|calculate|compute)\b"
    r"|\d\s*[+×*/=]\s*\d|\d\s*(?:%|(?:mg|mmol)/[dl]+\b|(?:мг|ммоль)/[дл]+\b)"
)
_AMBIGUOUS_TOPICS = {"alanine aminotransferase", "aspartate aminotransferase"}
_CLINICAL_CONTEXT = re.compile(r"\b(?:анализ\w*|кров\w*|печен\w*|алт|аст|blood|liver|test)\b")
_URL_IN_ANSWER = re.compile(r"(?:https?://|www\.)[^\s)\]<>]+", re.I)
_MARKDOWN_URL = re.compile(
    r"(?<!\[)\[+((?:\[S[0-9]+\]|[^\[\]\r\n])+)"
    r"(?:\]\((?:https?://|www\.|/(?:api/)?documents/)[^\s()\[\]<>]+\))+",
    re.I,
)
_SOURCE_ID = re.compile(r"S[0-9]+")
_ID_IN_ANSWER = re.compile(
    rf"\b(?:PMID\s*[:№#]?\s*([0-9]+)|(?:DOI\s*[:№#]?\s*)?({_DOI_VALUE}))", re.I,
)


def public_topics(question: str) -> tuple[str, ...]:
    question = normalized(question)
    return tuple(topic for topic, pattern in _TOPICS.items() if pattern.search(question))


def medical_reference_query(question: str) -> str | None:
    """Select only explicit general reference questions for a local lookup."""
    query = question.strip()
    text = normalized(query)
    if (
        len(query) > 400 or not _REFERENCE_QUESTION.search(text)
        or needs_documents(query) or _PERSONAL_OR_CALCULATION.search(text)
    ):
        return None
    topics = set(public_topics(text))
    if topics - _AMBIGUOUS_TOPICS or _REFERENCE_TOPIC.search(text):
        return query
    if topics and _CLINICAL_CONTEXT.search(text):
        return query
    return None


def requests_pubmed(question: str) -> bool:
    question = _DECLINED_RESEARCH.sub(" ", normalized(question)).strip()
    return bool(
        pubmed.PMID.fullmatch(question) or _DOI_INPUT.fullmatch(question)
        or _RESEARCH_REQUEST.search(question)
    )


def pubmed_query(query: str, question: str) -> str | None:
    """Return only vetted topic strings or publication IDs explicitly supplied this turn."""
    term = " ".join(query.casefold().split())
    if match := _DOI_INPUT.fullmatch(term):
        doi = match[1]
        supplied = _DOI_INPUT.fullmatch(question.strip())
        if supplied and supplied[1].casefold() == doi:
            return f"doi:{doi}"
        candidates = _QUESTION_DOI.findall(question.casefold())
        if _PUBLICATION_REQUEST.search(question) and any(
            doi in (value, value.rstrip(".")) for value in candidates
        ):
            return f"doi:{doi}"
        return None
    pmid = re.sub(r"^(?:pmid|pubmed)(?:\s*[:№#]\s*|\s+)", "", term)
    pmid = re.sub(r"\s*\[pmid\]$", "", pmid)
    if pubmed.PMID.fullmatch(pmid):
        if pmid == question.strip() or pmid in _QUESTION_PMID.findall(question):
            return f"PMID:{pmid}"
        return None
    allowed = public_topics(question)
    terms = list(dict.fromkeys(term.split(" and ")))
    if 1 <= len(terms) <= 3 and all(topic in allowed for topic in terms):
        return " AND ".join(terms)
    return None


def knowledge_hit(hit: dict[str, Any]) -> dict[str, Any]:
    source = hit["source"]
    return {
        "kind": "medical_knowledge",
        "knowledge_source_id": str(source["id"]),
        "version_id": str(source["version_id"]),
        "version": source["version"],
        "chunk_id": str(hit["chunk_id"]),
        "start_char": hit["start_char"],
        "end_char": hit["end_char"],
        "filename": source["title"],
        "source_url": source["url"],
        "sources": hit["locators"],
        "text": hit["text"],
    }


def publication_hit(hit: PubMedHit) -> dict[str, Any]:
    return {
        "kind": "pubmed",
        "filename": hit.title,
        "authors": hit.authors,
        "journal": hit.journal,
        "year": hit.year,
        "pmid": hit.pmid,
        "doi": hit.doi,
        "source_url": hit.url,
        "sources": [],
        "text": hit.abstract,
        "abstract_available": bool(hit.abstract),
    }


def public_citation_key(source: dict[str, Any]) -> str:
    if source["kind"] == "medical_knowledge":
        return (
            f"chunk:{source['chunk_id']}:{source['version_id']}:"
            f"{source['start_char']}:{source['end_char']}:{source['source_url']}"
        )
    return f"pmid:{source['pmid']}:{source['source_url']}"


def public_citation(source: dict[str, Any]) -> bool:
    return source.get("kind") in {"medical_knowledge", "pubmed"}


def verified_public(
    db: Session, settings: Settings, sources: list[dict[str, Any]], *, lock: bool = False,
) -> set[str]:
    knowledge = [source for source in sources if source.get("kind") == "medical_knowledge"]
    verified = {
        public_citation_key(source) for source in sources
        if source.get("kind") == "pubmed" and pubmed.PMID.fullmatch(source["pmid"])
        and source["source_url"] == f"https://pubmed.ncbi.nlm.nih.gov/{source['pmid']}/"
    }
    if not knowledge:
        return verified
    if lock:
        ids = {UUID(source["knowledge_source_id"]) for source in knowledge}
        db.execute(
            select(MedicalSource.id).where(MedicalSource.id.in_(ids))
            .order_by(MedicalSource.id).with_for_update(read=True),
        )
    chunks = {UUID(source["chunk_id"]) for source in knowledge}
    rows = db.execute(
        select(MedicalChunk, MedicalVersion, MedicalSource)
        .join(MedicalVersion, MedicalVersion.id == MedicalChunk.version_id)
        .join(MedicalSource, MedicalSource.id == MedicalVersion.source_id)
        .where(eligible(settings), MedicalChunk.id.in_(chunks)),
    )
    current = {str(chunk.id): (chunk, version, source) for chunk, version, source in rows}
    for citation in knowledge:
        row = current.get(citation["chunk_id"])
        if row is None:
            continue
        chunk, version, source = row
        if (
            citation["knowledge_source_id"] == str(source.id)
            and citation["version_id"] == str(version.id)
            and citation["source_url"] == source.url
            and citation["start_char"] == chunk.start_char
            and chunk.start_char < citation["end_char"] <= chunk.end_char
        ):
            verified.add(public_citation_key(citation))
    return verified


def plain_answer_links(text: str) -> str:
    # Chat prose is displayed as text. Source cards render vetted clickable links.
    return _MARKDOWN_URL.sub(
        lambda match: f"[{match[1]}]" if _SOURCE_ID.fullmatch(match[1]) else match[1], text,
    )


def verified_answer_refs(text: str, sources: list[dict[str, Any]]) -> str:
    urls = {source["source_url"] for source in sources}
    pmids = {source.get("pmid") for source in sources}
    dois = {source.get("doi") for source in sources}
    text = _URL_IN_ANSWER.sub(
        lambda match: match[0] if match[0].rstrip(".,") in urls else "", plain_answer_links(text),
    )
    return _ID_IN_ANSWER.sub(
        lambda match: match[0]
        if (match[1] in pmids if match[1] else match[2].rstrip(".").lower() in dois) else "",
        text,
    )
