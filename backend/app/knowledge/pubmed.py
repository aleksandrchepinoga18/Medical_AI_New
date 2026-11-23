import re
from calendar import month_abbr
from datetime import date
from threading import Lock
from time import monotonic, sleep
from typing import Literal
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx
from fastapi import HTTPException

from app.knowledge.schemas import PubMedHit, PubMedSearch

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PMID = re.compile(r"[1-9][0-9]{0,19}")
DOI = re.compile(r'10\.\d{4,9}/[^\s"]+', re.ASCII | re.IGNORECASE)
PMCID = re.compile(r"PMC[1-9][0-9]*", re.IGNORECASE)
DOI_PREFIX = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
PMID_PREFIX = re.compile(r"^pmid\s*:\s*", re.IGNORECASE)
MAX_XML_BYTES = 5 * 1024 * 1024
MONTHS = {name.lower(): index for index, name in enumerate(month_abbr) if name}
ACCESS_FILTERS: tuple[tuple[str, Literal["link_reported", "free_link_reported"]], ...] = (
    ("full text", "link_reported"), ("free full text", "free_link_reported"),
)

_rate_lock = Lock()
_next_request = 0.0


def search_term(query: str) -> tuple[str, str | None]:
    pmid = PMID_PREFIX.sub("", query)
    if PMID.fullmatch(pmid):
        return f"{pmid}[pmid]", None
    doi = DOI_PREFIX.sub("", query)
    if query.lower().startswith(("http://", "https://")):
        doi = unquote(doi)
    if DOI.fullmatch(doi):
        return f'"{doi}"[aid]', doi.lower()
    if pmid != query or DOI_PREFIX.match(query):
        raise HTTPException(422, "Invalid PubMed identifier")
    return query, None


def node_text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def author_name(author: ET.Element) -> str:
    return node_text(author.find("CollectiveName")) or " ".join(filter(None, (
        node_text(author.find("ForeName")) or node_text(author.find("Initials")),
        node_text(author.find("LastName")),
        node_text(author.find("Suffix")),
    )))


def publication_date(*nodes: ET.Element | None) -> tuple[int | None, date | None]:
    for node in nodes:
        if node is None:
            continue
        year_text = node.findtext("Year") or node.findtext("MedlineDate") or ""
        year_match = re.search(r"\b[1-9][0-9]{3}\b", year_text)
        if year_match is None:
            continue
        year = int(year_match[0])
        month = node.findtext("Month", "").strip().lower()
        try:
            month_number = MONTHS.get(month, 0) or int(month)
            return year, date(year, month_number, int(node.findtext("Day", "")))
        except ValueError:
            return year, None
    return None, None


def parse_record(record: ET.Element) -> PubMedHit | None:
    is_book = record.tag == "PubmedBookArticle"
    if is_book:
        citation = article = record.find("BookDocument")
        journal_path = "Book/BookTitle"
        ids = record.findall("PubmedBookData/ArticleIdList/ArticleId")
    else:
        citation = record.find("MedlineCitation")
        article = record.find("MedlineCitation/Article")
        journal_path = "Journal/Title"
        ids = record.findall("PubmedData/ArticleIdList/ArticleId")
    if citation is None or article is None:
        return None
    pmid = node_text(citation.find("PMID"))
    journal = node_text(article.find(journal_path))
    title = node_text(article.find("ArticleTitle")) or (journal if is_book else "")
    if not PMID.fullmatch(pmid) or not title:
        return None
    identifiers = {entry.get("IdType"): node_text(entry) for entry in (
        article.findall("ArticleIdList/ArticleId") + ids
    )}
    doi = identifiers.get("doi") or next((
        node_text(entry) for entry in article.findall("ELocationID[@EIdType='doi']")
        if entry.get("ValidYN") != "N"
    ), "")
    doi_value = doi.lower() if DOI.fullmatch(doi) else None
    pmc = identifiers.get("pmc", "")
    pmcid = pmc.upper() if PMCID.fullmatch(pmc) else None
    date_fields = ("ContributionDate", "Book/PubDate") if is_book else (
        "Journal/JournalIssue/PubDate", "ArticleDate",
    )
    year, published = publication_date(*(article.find(field) for field in date_fields))
    author_lists = article.findall("AuthorList")
    if is_book:
        author_lists = [group for group in author_lists if group.get("Type") != "editors"]
    authors = [author_name(author) for group in author_lists for author in group.findall("Author")]
    abstract = []
    sections = article.findall("Abstract/AbstractText")
    if not sections:
        other_abstract = citation.find("OtherAbstract")
        if other_abstract is not None:
            sections = other_abstract.findall("AbstractText")
    for node in sections:
        content = node_text(node)
        if content:
            label = node.get("Label")
            abstract.append(f"{label}: {content}" if label else content)
    return PubMedHit(
        title=title, authors=list(filter(None, authors)), journal=journal or None,
        year=year, publication_date=published, pmid=pmid, doi=doi_value, pmcid=pmcid,
        language=node_text(article.find("Language")) or None,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        abstract="\n\n".join(abstract) or None,
        full_text_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else None,
        publisher_url=f"https://doi.org/{quote(doi_value, safe='/')}" if doi_value else None,
    )


def request_xml(
    client: httpx.Client, utility: str, params: dict[str, str], root_tag: str,
) -> ET.Element:
    global _next_request
    with _rate_lock:
        sleep(max(0, _next_request - monotonic()))
        _next_request = monotonic() + 0.35
    try:
        with client.stream("POST", utility, data={
            "db": "pubmed", "retmode": "xml", "tool": "medical_ai_new", **params,
        }) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_XML_BYTES:
                    raise ValueError("PubMed response exceeds size limit")
        root = ET.fromstring(data)
        if root.tag != root_tag or root.find("ERROR") is not None:
            raise ValueError("Unexpected PubMed response")
        return root
    except (httpx.HTTPError, ET.ParseError, ValueError):
        raise HTTPException(503, "PubMed temporarily unavailable") from None


def search_ids(client: httpx.Client, params: dict[str, str]) -> list[str]:
    root = request_xml(client, "esearch.fcgi", params, "eSearchResult")
    return [pmid for node in root.findall("IdList/Id") if PMID.fullmatch(pmid := node_text(node))]


def fetch_records(client: httpx.Client, pmids: list[str]) -> list[PubMedHit]:
    root = request_xml(client, "efetch.fcgi", {"id": ",".join(pmids)}, "PubmedArticleSet")
    by_id = {}
    for record in root:
        if record.tag in {"PubmedArticle", "PubmedBookArticle"} and (entry := parse_record(record)):
            by_id[entry.pmid] = entry
    return [by_id[pmid] for pmid in dict.fromkeys(pmids) if pmid in by_id]


def annotate_access(client: httpx.Client, entries: list[PubMedHit]) -> None:
    if not entries:
        return
    term = " OR ".join(f"{entry.pmid}[pmid]" for entry in entries)
    params = {"retmax": str(len(entries))}
    for filter_name, access in ACCESS_FILTERS:
        try:
            matched = set(search_ids(client, {**params, "term": f"({term}) AND {filter_name}[sb]"}))
        except HTTPException:
            continue
        for entry in entries:
            if entry.pmid in matched:
                entry.access = access
                entry.full_text_url = entry.full_text_url or f"{entry.url}#linkout"


def search(
    data: PubMedSearch, contact_email: str = "", *, include_access: bool = True,
) -> list[PubMedHit]:
    term, doi = search_term(data.query)
    params = {"term": term, "retmax": str(data.limit), "sort": "relevance"}
    if data.date_from or data.date_to:
        params.update(
            datetype="pdat",
            mindate=(data.date_from or date.min).isoformat().replace("-", "/"),
            maxdate=(data.date_to or date.max).isoformat().replace("-", "/"),
        )
    with ncbi_client(contact_email) as client:
        ids = search_ids(client, params)[:data.limit]
        entries = fetch_records(client, ids) if ids else []
        if doi is not None:
            entries = [entry for entry in entries if entry.doi == doi]
        if include_access:
            annotate_access(client, entries)
        return entries


def ncbi_client(contact_email: str) -> httpx.Client:
    return httpx.Client(
        base_url=EUTILS, timeout=8.0, params={"email": contact_email} if contact_email else None,
    )


def lookup(pmid: str, contact_email: str = "") -> PubMedHit:
    with ncbi_client(contact_email) as client:
        entries = fetch_records(client, [pmid])
    if not entries:
        raise HTTPException(404, "PubMed publication not found")
    return entries[0]
