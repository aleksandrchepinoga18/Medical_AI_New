"""Prepare the seven MedlinePlus Medical Tests texts and an import manifest."""

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
from docx import Document
from lxml import html
from lxml.etree import LxmlError

from app.knowledge.schemas import SourceUpload
from scripts.import_medical_knowledge import Manifest, ManifestItem

BASE_URL = "https://medlineplus.gov/lab-tests/"
TERMS_URL = "https://www.medlineplus.gov/about/using/usingcontent/"
NLM = "National Library of Medicine"
MAX_HTML_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Source:
    slug: str
    title: str
    specialty: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}{self.slug}/"


SOURCES = (
    Source("complete-blood-count-cbc", "Complete Blood Count (CBC)", "Общий анализ крови"),
    Source(
        "comprehensive-metabolic-panel-cmp", "Comprehensive Metabolic Panel (CMP)",
        "Биохимический анализ крови",
    ),
    Source("liver-function-tests", "Liver Function Tests", "Функция печени"),
    Source(
        "estimated-glomerular-filtration-rate-egfr-and-gfr-tests",
        "Estimated Glomerular Filtration Rate (eGFR) and GFR Tests", "Функция почек",
    ),
    Source("electrolyte-panel", "Electrolyte Panel", "Электролиты"),
    Source("cholesterol-levels", "Cholesterol Levels", "Липидный профиль"),
    Source("hemoglobin-a1c-hba1c-test", "Hemoglobin A1C (HbA1c) Test", "Углеводный обмен"),
)
ALLOWED_URLS = frozenset(source.url for source in SOURCES)

CONTENT_BLOCKS = {"p", "h2", "h3", "h4", "h5", "h6", "li"}
EXCLUDED_TAGS = {
    "aside", "audio", "blockquote", "button", "canvas", "figure", "figcaption", "footer", "form",
    "iframe", "img", "nav", "noscript", "picture", "script", "style", "svg", "video",
}
EXCLUDED_LABEL = re.compile(
    r"(?:^|[\s-])(?:advertis\w*|ads?|breadcrumb\w*|cookie\w*|copyright\w*|credits?|"
    r"disclaimer\w*|footnotes?|menu\w*|navigation|related\w*|rights\w*|share\w*|social\w*|"
    r"sponsor\w*|third-?party\w*|toolbar\w*|references?)(?:$|[\s-])",
    re.I,
)
RIGHTS_NOTICE = re.compile(
    r"©|\ball rights reserved\b|\bcopyright(?:ed)?\b|"
    r"\b(?:used|reproduced|adapted) with permission\b|"
    r"\bthird[- ]party (?:content|material)\b|\bA\.D\.A\.M\.",
    re.I,
)
HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.I)
SECTION = re.compile(
    r"^(?:What|Why|How|When|Will|Can|Are|Is|Do|Does|Who|Should|Where|Which|"
    r"Understanding|About)\b",
    re.I,
)
REFERENCES = re.compile(r"^(?:References|Sources|Bibliography)\s*:?$", re.I)
UPDATED = re.compile(
    r"Last updated\s*:?\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\.?$", re.I,
)
MONTHS = {
    name: number for number, name in enumerate((
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ), 1)
}


class PreparationError(Exception):
    pass


@dataclass(frozen=True)
class Block:
    style: str
    text: str


@dataclass(frozen=True)
class Article:
    title: str
    updated: date
    blocks: tuple[Block, ...]


def normalized(text: str) -> str:
    return " ".join(text.split())


def excluded(element: html.HtmlElement) -> bool:
    labels = " ".join(
        element.get(attr, "")
        for attr in ("id", "class", "aria-label", "data-copyright", "data-rights")
    ).replace("_", "-")
    role = element.get("role", "").lower()
    return (
        element.tag in EXCLUDED_TAGS or "hidden" in element.attrib
        or element.get("aria-hidden", "").lower() == "true"
        or role in {"navigation", "complementary", "banner", "contentinfo"}
        or "data-copyright" in element.attrib or "data-rights" in element.attrib
        or bool(EXCLUDED_LABEL.search(labels))
        or bool(HIDDEN_STYLE.search(element.get("style", "")))
    )


def visible_text(element: html.HtmlElement) -> str:
    pieces = [element.text or ""]
    for child in element:
        if child.tag in {"br", "p", "div", "ol", "ul"}:
            pieces.append(" ")
        if child.tag not in {"ol", "ul"} and not excluded(child):
            pieces.append(visible_text(child))
        if child.tag in {"p", "div"}:
            pieces.append(" ")
        pieces.append(child.tail or "")
    return "".join(pieces)


def last_updated(root: html.HtmlElement) -> date:
    values = set()
    for node in root.xpath("//p|//div|//span|//time|//small|//li"):
        text = normalized(" ".join(node.itertext()))
        if match := UPDATED.fullmatch(text):
            month, day, year = match.groups()
            try:
                values.add(date(int(year), MONTHS[month.lower()], int(day)))
            except (KeyError, ValueError) as error:
                raise PreparationError("Invalid MedlinePlus update date") from error
    if len(values) != 1:
        raise PreparationError("Expected one unambiguous MedlinePlus update date")
    return values.pop()


def extract_article(raw: bytes, expected_title: str) -> Article:
    try:
        root = html.fromstring(
            raw, parser=html.HTMLParser(no_network=True, remove_comments=True, encoding="utf-8"),
        )
    except LxmlError as error:
        raise PreparationError("Invalid HTML response") from error
    headings = [
        node for node in root.xpath("//h1")
        if normalized(visible_text(node)).casefold() == expected_title.casefold()
        and not excluded(node) and not any(excluded(parent) for parent in node.iterancestors())
    ]
    if len(headings) != 1:
        raise PreparationError("Expected one title matching the approved MedlinePlus source")
    title_node = headings[0]
    title = normalized(visible_text(title_node))
    region = next(
        (
            node for node in title_node.iterancestors()
            if node.tag in {"main", "body"} or node.get("role") == "main"
        ),
        root,
    )
    blocks: list[Block] = []
    past_title = in_section = found_end = False
    for node in region.iter():
        if node is title_node:
            past_title = True
            continue
        if not past_title or node.tag not in CONTENT_BLOCKS:
            continue
        if blocks and node.tag in {"h2", "h3"} and REFERENCES.fullmatch(
            normalized(node.text_content())
        ):
            found_end = True
            break
        if excluded(node) or any(excluded(parent) for parent in node.iterancestors()):
            continue
        text = normalized(visible_text(node))
        if node.tag == "h2":
            in_section = bool(SECTION.match(text))
        if not in_section or not text:
            continue
        if RIGHTS_NOTICE.search(text):
            raise PreparationError("Unscoped third-party rights notice in article text")
        if node.tag == "p" and next(node.iterancestors("li"), None) is not None:
            continue
        if node.tag == "li":
            parent_list = next(node.iterancestors("ul", "ol"), None)
            style = (
                "List Number" if parent_list is not None and parent_list.tag == "ol"
                else "List Bullet"
            )
        elif node.tag.startswith("h"):
            style = f"Heading {int(node.tag[1]) - 1}"
        else:
            style = "Normal"
        blocks.append(Block(style, text))
    if not found_end or not any(
        block.style in {"Normal", "List Bullet", "List Number"} for block in blocks
    ):
        raise PreparationError("Expected an article body followed by a References section")
    return Article(title, last_updated(root), tuple(blocks))


def fetch(client: httpx.Client, url: str) -> bytes:
    if url not in ALLOWED_URLS:
        raise PreparationError("URL is outside the seven approved MedlinePlus pages")
    with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "text/html":
            raise PreparationError("Expected a MedlinePlus HTML page")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(content) + len(chunk) > MAX_HTML_BYTES:
                raise PreparationError("MedlinePlus response exceeds the size limit")
            content.extend(chunk)
        return bytes(content)


def render_docx(source: Source, article: Article) -> bytes:
    document = Document()
    document.core_properties.title = article.title
    document.core_properties.author = NLM
    document.core_properties.created = document.core_properties.modified = datetime(
        article.updated.year, article.updated.month, article.updated.day, tzinfo=UTC,
    )
    document.add_heading(article.title, level=0)
    document.add_paragraph(f"Source: MedlinePlus Medical Tests, {NLM}.")
    document.add_paragraph(f"Original URL: {source.url}")
    document.add_paragraph(f"Last updated by MedlinePlus: {article.updated.isoformat()}")
    document.add_paragraph(f"Reuse policy: {TERMS_URL}")
    for block in article.blocks:
        document.add_paragraph(block.text, style=block.style)
    output = BytesIO()
    document.save(output)
    stable = BytesIO()
    # DOCX is a ZIP package. Repack with fixed entry timestamps for stable checksums.
    with ZipFile(output) as original, ZipFile(stable, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(original.namelist()):
            archive.writestr(ZipInfo(name), original.read(name), ZIP_DEFLATED)
    return stable.getvalue()


def manifest_item(source: Source, article: Article, content: bytes) -> ManifestItem:
    updated = article.updated.isoformat()
    metadata = SourceUpload.model_validate({
        "title": article.title, "material_type": "Laboratory test information",
        "author": NLM, "publisher": NLM, "url": source.url, "update_date": updated,
        "specialty": source.specialty, "language": "en", "jurisdiction": "US", "version": updated,
    })
    return ManifestItem(
        file=f"{source.slug}.docx", sha256=hashlib.sha256(content).hexdigest(), metadata=metadata,
    )


def write_if_changed(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(data)
        temp.chmod(0o644)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def prepare(output: Path, client: httpx.Client) -> Manifest:
    output = output.resolve()
    if any((directory / ".git").exists() for directory in (output, *output.parents)):
        raise PreparationError("The corpus output must be outside a Git repository")
    documents = []
    for source in SOURCES:
        try:
            article = extract_article(fetch(client, source.url), source.title)
            content = render_docx(source, article)
            documents.append((manifest_item(source, article, content), content))
        except (PreparationError, httpx.HTTPError, ValueError) as error:
            raise PreparationError(f"{source.slug}: {error}") from error
    manifest = Manifest(items=[item for item, _ in documents])
    output.mkdir(parents=True, exist_ok=True)
    for item, content in documents:
        write_if_changed(output / item.file, content)
    write_if_changed(output / "manifest.json", (manifest.model_dump_json(indent=2) + "\n").encode())
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            manifest = prepare(args.output, client)
    except (OSError, PreparationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"Prepared {len(manifest.items)} DOCX files and manifest.json in {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
