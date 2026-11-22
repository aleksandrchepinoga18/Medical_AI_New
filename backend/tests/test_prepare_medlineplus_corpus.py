import hashlib
import os
from datetime import date
from html import escape
from pathlib import Path

import httpx
import pytest
from docx import Document

from scripts.import_medical_knowledge import Manifest
from scripts.prepare_medlineplus_corpus import (
    SOURCES,
    Block,
    PreparationError,
    extract_article,
    fetch,
    prepare,
)


def page(title: str) -> bytes:
    return f"""<!doctype html>
    <html><body>
      <header role="banner"><h1>Site navigation</h1></header>
      <nav><p>Navigation</p></nav>
      <main>
        <header><h1>{escape(title)}</h1></header>
        <h2>What is a synthetic test?</h2>
        <p>Synthetic <strong>article</strong> text.<img alt="Image credit" src="/fake"></p>
        <aside><p>Sidebar</p></aside>
        <div class="advertisement"><p>Advertising</p></div>
        <section data-rights="separate license">
          <h2>What should I know?</h2><p>Protected passage</p>
        </section>
        <h3>Synthetic subheading</h3>
        <ul><li>First item<ol><li>Nested item</li></ol></li></ul>
        <div id="references"><h2>References</h2><ol><li>Third-party citation</li></ol></div>
        <h2>What happens next?</h2><p>Outside the article</p>
      </main>
      <footer><p>Last updated <time>October 15, 2024</time></p></footer>
    </body></html>""".encode()


def test_extracts_article_structure_and_excludes_other_content() -> None:
    source = SOURCES[0]
    article = extract_article(page(source.title), source.title)
    assert article.title == source.title
    assert article.updated == date(2024, 10, 15)
    assert article.blocks == (
        Block("Heading 1", "What is a synthetic test?"),
        Block("Normal", "Synthetic article text."),
        Block("Heading 2", "Synthetic subheading"),
        Block("List Bullet", "First item"),
        Block("List Number", "Nested item"),
    )
    ambiguous = page(source.title).replace(
        b"Synthetic <strong>article</strong> text.", b"Copyrighted.",
    )
    with pytest.raises(PreparationError, match="rights notice"):
        extract_article(ambiguous, source.title)


def test_does_not_request_other_urls_or_follow_redirects() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.org/"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(PreparationError, match="approved"):
            fetch(client, "https://medlineplus.gov/lab-tests/unapproved/")
        with pytest.raises(httpx.HTTPStatusError):
            fetch(client, SOURCES[0].url)
    assert requests == [SOURCES[0].url]


def test_manifest_and_idempotent_documents(tmp_path: Path) -> None:
    by_url = {source.url: source for source in SOURCES}

    def handler(request: httpx.Request) -> httpx.Response:
        source = by_url[str(request.url)]
        return httpx.Response(
            200, content=page(source.title), headers={"Content-Type": "text/html; charset=utf-8"},
        )

    output = tmp_path / "corpus"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        prepare(output, client)
        manifest_path = output / "manifest.json"
        manifest = Manifest.model_validate_json(manifest_path.read_bytes())
        assert len(manifest.items) == len(SOURCES) == 7
        assert len(list(output.glob("*.docx"))) == 7
        for source, item in zip(SOURCES, manifest.items, strict=True):
            path = output / item.file
            assert str(item.metadata.url) == source.url
            assert item.metadata.update_date == date(2024, 10, 15)
            assert item.metadata.version == "2024-10-15"
            assert item.metadata.publication_date is None
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item.sha256
            paragraphs = [paragraph.text for paragraph in Document(path).paragraphs]
            assert paragraphs[0] == source.title
            assert f"Original URL: {source.url}" in paragraphs
            assert "Source: MedlinePlus Medical Tests, National Library of Medicine." in paragraphs
            os.utime(path, (1_700_000_000, 1_700_000_000))
        times = {path.name: path.stat().st_mtime_ns for path in output.iterdir()}
        prepare(output, client)
        assert {path.name: path.stat().st_mtime_ns for path in output.iterdir()} == times
