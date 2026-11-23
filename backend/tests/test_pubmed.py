from datetime import date
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.chat.agent import execute_tool
from app.core.config import Settings
from app.knowledge import pubmed
from app.knowledge.routes import search_pubmed
from app.knowledge.schemas import PubMedSearch
from app.llm.gateway import ToolCall

ARTICLE_XML = """<PubmedArticle>
<MedlineCitation><PMID>12345678</PMID><Article>
  <Journal><JournalIssue><PubDate><Year>2024</Year><Month>Feb</Month><Day>29</Day>
  </PubDate></JournalIssue><Title>Synthetic journal</Title></Journal>
  <ArticleTitle>A <i>synthetic</i> article</ArticleTitle>
  <Abstract><AbstractText Label="METHODS">Sample <b>text</b>.</AbstractText>
  <AbstractText>Another section.</AbstractText></Abstract>
  <AuthorList><Author><LastName>Example</LastName><ForeName>Ada</ForeName></Author>
  <Author><CollectiveName>Synthetic group</CollectiveName></Author></AuthorList>
  <Language>eng</Language>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.99999/Sample</ArticleId>
<ArticleId IdType="pmc">PMC12345678</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>"""


@pytest.mark.parametrize(("query", "term", "doi"), [
    ("hypertension", "hypertension", None),
    ("12345678", "12345678[pmid]", None),
    ("PMID: 12345678", "12345678[pmid]", None),
    ("doi:10.99999/sample", '"10.99999/sample"[aid]', "10.99999/sample"),
    ("https://doi.org/10.99999%2FSample", '"10.99999/Sample"[aid]', "10.99999/sample"),
])
def test_search_term(query, term, doi):
    assert pubmed.search_term(query) == (term, doi)


def test_search_validates_date_range_and_limit():
    with pytest.raises(ValidationError):
        PubMedSearch(query="synthetic", date_from=date(2025, 1, 1), date_to=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        PubMedSearch(query="synthetic", limit=26)
    with pytest.raises(HTTPException) as error:
        pubmed.search_term("PMID: invalid")
    assert error.value.status_code == 422


def test_search_fetches_only_citations_and_reports_availability(monkeypatch):
    ids = "<eSearchResult><IdList><Id>12345678</Id></IdList></eSearchResult>"
    article_set = f"<PubmedArticleSet>{ARTICLE_XML}</PubmedArticleSet>"
    calls = []

    def respond(request):
        assert request.url.host == "eutils.ncbi.nlm.nih.gov"
        assert request.method == "POST"
        params = parse_qs(request.content.decode())
        assert params["db"] == ["pubmed"]
        calls.append((request.url.path.rsplit("/", 1)[-1], params))
        return httpx.Response(200, text=article_set if "id" in params else ids)

    monkeypatch.setattr(pubmed, "ncbi_client", lambda _: httpx.Client(
        base_url=pubmed.EUTILS, transport=httpx.MockTransport(respond),
    ))
    monkeypatch.setattr(pubmed, "sleep", lambda _: None)
    item, = pubmed.search(PubMedSearch(
        query="https://doi.org/10.99999/Sample", limit=1,
        date_from=date(2024, 1, 1), date_to=date(2024, 12, 31),
    ))
    assert [utility for utility, _ in calls] == [
        "esearch.fcgi", "efetch.fcgi", "esearch.fcgi", "esearch.fcgi",
    ]
    assert calls[0][1]["term"] == ['"10.99999/Sample"[aid]']
    assert calls[0][1]["datetype"] == ["pdat"]
    assert calls[0][1]["mindate"] == ["2024/01/01"]
    assert calls[0][1]["maxdate"] == ["2024/12/31"]
    assert calls[0][1]["retmax"] == ["1"]
    assert calls[1][1]["id"] == ["12345678"]
    assert calls[3][1]["term"] == ["(12345678[pmid]) AND free full text[sb]"]
    assert item.title == "A synthetic article"
    assert item.authors == ["Ada Example", "Synthetic group"]
    assert item.journal == "Synthetic journal"
    assert item.year == 2024
    assert item.publication_date == date(2024, 2, 29)
    assert item.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert item.abstract == "METHODS: Sample text.\n\nAnother section."
    assert item.doi == "10.99999/sample"
    assert item.pmcid == "PMC12345678"
    assert item.full_text_url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345678/"
    assert item.access == "free_link_reported"


def test_exact_pmid_returns_same_bibliography_in_admin_and_agent(monkeypatch):
    pmid = "19671910"
    article = ARTICLE_XML.replace("12345678", pmid)
    fetches = []

    def respond(request):
        params = parse_qs(request.content.decode())
        if request.url.path.endswith("/efetch.fcgi"):
            assert params["id"] == [pmid]
            fetches.append(params)
            return httpx.Response(200, text=f"<PubmedArticleSet>{article}</PubmedArticleSet>")
        assert params["term"][0].endswith("full text[sb]")
        return httpx.Response(200, text="<eSearchResult><IdList/></eSearchResult>")

    monkeypatch.setattr(pubmed, "ncbi_client", lambda _: httpx.Client(
        base_url=pubmed.EUTILS, transport=httpx.MockTransport(respond),
    ))
    monkeypatch.setattr(pubmed, "sleep", lambda _: None)
    settings = Settings(redis_password="synthetic")
    with Session() as db:
        admin, = search_pubmed(
            PubMedSearch(query=pmid, limit=10), Response(), db, settings,
        )["items"]
        tool = execute_tool(
            db, uuid4(), settings, ToolCall("search_pubmed", {"query": f"{pmid}[PMID]"}),
            2000, (), f"Найди публикацию PubMed по PMID {pmid}",
        )
    hit, = tool.public_hits
    assert hit["filename"] == admin.title
    assert hit["source_url"] == admin.url and hit["text"] == admin.abstract
    for field in ("pmid", "doi", "year", "journal", "authors"):
        assert hit[field] == getattr(admin, field)
    assert len(fetches) == 2 and fetches[0] == fetches[1]


def test_pmid_search_preserves_date_filter(monkeypatch):
    def respond(request):
        assert request.url.path.endswith("/esearch.fcgi")
        params = parse_qs(request.content.decode())
        assert params["term"] == ["19671910[pmid]"]
        assert params["datetype"] == ["pdat"]
        assert params["mindate"] == ["2024/01/01"]
        return httpx.Response(200, text="<eSearchResult><IdList/></eSearchResult>")

    monkeypatch.setattr(pubmed, "ncbi_client", lambda _: httpx.Client(
        base_url=pubmed.EUTILS, transport=httpx.MockTransport(respond),
    ))
    monkeypatch.setattr(pubmed, "sleep", lambda _: None)
    assert pubmed.search(PubMedSearch(
        query="19671910", date_from=date(2024, 1, 1),
    ), include_access=False) == []


@pytest.mark.parametrize(("status", "body", "category"), [
    (429, "synthetic private upstream detail", "http_status"),
    (200, "synthetic private upstream detail", "invalid_xml"),
    (200, "<PubmedArticleSet><ERROR>synthetic private</ERROR></PubmedArticleSet>",
     "upstream_xml_error"),
])
def test_exact_pmid_upstream_diagnostics_have_no_query_or_response_body(
    monkeypatch, caplog, status, body, category,
):
    monkeypatch.setattr(pubmed, "ncbi_client", lambda _: httpx.Client(
        base_url=pubmed.EUTILS,
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, text=body)),
    ))
    monkeypatch.setattr(pubmed, "sleep", lambda _: None)
    with pytest.raises(HTTPException) as error:
        pubmed.search(PubMedSearch(query="19671910"), "synthetic-contact@example.org")
    assert error.value.status_code == 503
    assert f"stage=efetch.fcgi mode=pmid category={category} http_status={status}" in caplog.text
    assert "19671910" not in caplog.text
    assert "synthetic private" not in caplog.text and "synthetic-contact" not in caplog.text


@pytest.mark.parametrize(("status", "body"), [(429, "rate limit"), (200, "malformed response")])
def test_upstream_errors_are_sanitized(monkeypatch, status, body):
    def respond(_request):
        return httpx.Response(status, text=body)

    monkeypatch.setattr(pubmed, "ncbi_client", lambda _: httpx.Client(
        base_url=pubmed.EUTILS, transport=httpx.MockTransport(respond),
    ))
    monkeypatch.setattr(pubmed, "sleep", lambda _: None)
    with pytest.raises(HTTPException) as error:
        pubmed.search(PubMedSearch(query="synthetic"))
    assert error.value.status_code == 503
    assert error.value.detail == "PubMed temporarily unavailable"
