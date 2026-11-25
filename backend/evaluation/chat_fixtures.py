"""Seed only the dedicated benchmark database and replay the fixed PubMed corpus."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4, uuid5
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy.orm import Session

from app.auth.models import User
from app.core.config import Settings
from app.documents.models import Document, DocumentTextVersion
from app.knowledge import pubmed
from app.knowledge.models import MedicalChunk, MedicalSource, MedicalVersion
from app.knowledge.vectors import KNOWLEDGE_PIPELINE, KnowledgeVectors
from app.rag.adapters import OllamaEmbeddings, fingerprint
from app.rag.chunking import Chunk
from app.rag.models import RagChunk, RagIndexJob


def locator(text: str) -> list[dict[str, Any]]:
    return [{"kind": "page", "index": 1, "start": 0, "end": len(text)}]


@dataclass
class Corpus:
    aliases: dict[str, str] = field(default_factory=dict)
    medical_ids: dict[str, UUID] = field(default_factory=dict)
    versions: dict[str, UUID] = field(default_factory=dict)
    medical_points: dict[str, dict[str, Any]] = field(default_factory=dict)
    personal_points: list[dict[str, Any]] = field(default_factory=list)


class EvaluationVectors(KnowledgeVectors):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.path = "/collections/medical_ai_evaluation_v1"


def seed_personal(
    db: Session, dataset: dict[str, Any], owners: tuple[UUID, UUID], settings: Settings,
    corpus: Corpus,
) -> None:
    other = db.get(User, owners[1])
    assert other is not None
    other.last_document_number = 2
    db.flush()
    for item in dataset["documents"]:
        owner = owners[item["id"] == "foreign"]
        text = "\n".join(f"{name}: {value} {unit} ({day})."
                         for name, value, unit, day in item["rows"])
        doc_id, version_id, chunk_id, generation = (uuid4() for _ in range(4))
        doc = Document(
            id=doc_id, owner_id=owner, status="ready", format="pdf", size_bytes=100,
            original_filename=f"{item['id']}.pdf", sha256="0" * 64,
            object_key=f"synthetic/{doc_id}",
        )
        db.add(doc)
        db.flush()
        assert doc.document_number == {"doc1": 1, "doc2": 2, "foreign": 3}[item["id"]]
        rows = [
            {"name": name, "value": value, "unit": unit, "reference": None, "date": day,
             "confirmation": "confirmed",
             "source": {"page": 1, "bbox": [10, i * 20, 100, i * 20 + 10]}}
            for i, (name, value, unit, day) in enumerate(item["rows"], 1)
        ]
        db.add(DocumentTextVersion(
            id=version_id, document_id=doc_id, version=1, normalized_text=text,
            extractor_version="synthetic", locators=locator(text),
            structured_result={"schema_version": 1, "local": {
                "provider": "pymupdf_tables", "provider_version": "synthetic", "measurements": rows,
            }},
        ))
        db.flush()
        db.add_all([
            RagChunk(
                id=chunk_id, user_id=owner, document_id=doc_id, text_version_id=version_id,
                ordinal=0, start_char=0, end_char=len(text), text=text, sources=locator(text),
            ),
            RagIndexJob(
                user_id=owner, document_id=doc_id, text_version_id=version_id,
                status="ready", operation="index", generation=generation,
                fingerprint=fingerprint(settings), chunk_count=1,
            ),
        ])
        corpus.aliases[str(doc_id)] = item["id"]
        corpus.personal_points.append({
            "id": str(uuid5(chunk_id, str(generation))), "score": 1.0,
            "payload": {"user_id": str(owner), "document_id": str(doc_id),
                        "text_version_id": str(version_id), "chunk_id": str(chunk_id),
                        "generation": str(generation)},
        })


def seed_knowledge(
    db: Session, dataset: dict[str, Any], owner: UUID, settings: Settings, corpus: Corpus,
    *, live_retrieval: bool,
) -> None:
    vectors = EvaluationVectors(settings)
    if live_retrieval:
        vectors.ensure()
    marker = uuid4().hex
    for item in dataset["corpus"]:
        source_id, version_id, chunk_id, generation = (uuid4() for _ in range(4))
        text = item["text"]
        source = MedicalSource(
            id=source_id, title=item["title"], url=f"{item['url']}?evaluation={marker}",
            author="Fixture", publisher="Fixture", material_type="evaluation",
            language="en", specialty="fixture", jurisdiction="fixture",
        )
        db.add(source)
        db.flush()
        now = datetime.now(UTC)
        db.add(MedicalVersion(
            id=version_id, source_id=source_id, version="benchmark", is_current=True,
            status=item["status"], uploaded_by=owner, uploaded_at=now, reviewed_by=owner,
            reviewed_at=now, review_note="Synthetic evaluation", format="pdf", size_bytes=100,
            sha256="0" * 64, generation=generation, fingerprint=fingerprint(settings),
            chunk_count=1,
        ))
        db.flush()
        db.add(MedicalChunk(
            id=chunk_id, version_id=version_id, start_char=0, end_char=len(text), text=text,
            locators=locator(text),
        ))
        cid = item["id"]
        corpus.aliases[str(source_id)] = cid
        corpus.medical_ids[cid] = source_id
        corpus.versions[cid] = version_id
        corpus.medical_points[cid] = {
            "id": str(uuid5(chunk_id, str(generation))), "score": 1.0,
            "payload": {
                "pipeline": KNOWLEDGE_PIPELINE, "source_id": str(source_id),
                "version_id": str(version_id), "generation": str(generation),
                "chunk_id": str(chunk_id), "fingerprint": fingerprint(settings),
            },
        }
        if live_retrieval:
            chunk = Chunk(chunk_id, 0, 0, len(text), text, locator(text))
            embeddings = OllamaEmbeddings(settings).embed([text])
            vectors.write_batch(source_id, version_id, generation, [chunk], embeddings)


def fixed_knowledge_ranking(
    corpus: Corpus, dataset: dict[str, Any], query: str,
) -> list[dict[str, Any]]:
    words = set(re.findall(r"[\w-]+", query.casefold()))
    ranked = sorted(
        dataset["corpus"],
        key=lambda row: -len(words & set(row["terms"].casefold().split())),
    )
    return [corpus.medical_points[row["id"]] for row in ranked
            if words & set(row["terms"].casefold().split())]


def pubmed_response(
    request: httpx.Request, entries: list[dict[str, Any]], *, outage: bool,
) -> httpx.Response:
    if outage:
        return httpx.Response(503)
    fields = parse_qs(request.content.decode())
    if request.url.path.endswith("/esearch.fcgi"):
        term = fields["term"][0].casefold()
        root = ET.Element("eSearchResult")
        ids = ET.SubElement(root, "IdList")
        for entry in entries:
            if entry["doi"] in term or any(topic in term for topic in entry["topics"]):
                ET.SubElement(ids, "Id").text = entry["pmid"]
    else:
        requested = fields["id"][0].split(",")
        root = ET.Element("PubmedArticleSet")
        for entry in entries:
            if entry["pmid"] not in requested:
                continue
            record = ET.SubElement(root, "PubmedArticle")
            citation = ET.SubElement(record, "MedlineCitation")
            ET.SubElement(citation, "PMID").text = entry["pmid"]
            article = ET.SubElement(citation, "Article")
            ET.SubElement(article, "ArticleTitle").text = entry["title"]
            journal = ET.SubElement(article, "Journal")
            ET.SubElement(journal, "Title").text = entry["journal"]
            issue = ET.SubElement(journal, "JournalIssue")
            ET.SubElement(ET.SubElement(issue, "PubDate"), "Year").text = str(entry["year"])
            if entry["abstract"]:
                ET.SubElement(ET.SubElement(article, "Abstract"), "AbstractText").text = (
                    entry["abstract"]
                )
            identifiers = ET.SubElement(ET.SubElement(record, "PubmedData"), "ArticleIdList")
            ET.SubElement(identifiers, "ArticleId", IdType="doi").text = entry["doi"]
    return httpx.Response(200, content=ET.tostring(root, encoding="utf-8"))


def pubmed_client(
    entries: list[dict[str, Any]], queries: list[str], *, outage: bool,
) -> httpx.Client:
    def transport(request: httpx.Request) -> httpx.Response:
        queries.append(request.content.decode())
        return pubmed_response(request, entries, outage=outage)

    return httpx.Client(base_url=pubmed.EUTILS, transport=httpx.MockTransport(transport))
