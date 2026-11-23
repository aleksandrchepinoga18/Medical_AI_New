from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.knowledge import pubmed
from app.knowledge.schemas import PubMedSearch

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
