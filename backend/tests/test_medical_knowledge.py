"""Synthetic knowledge lifecycle with real PostgreSQL, Qdrant and BGE-M3."""

import hashlib
import json
from uuid import UUID, uuid4

import pymupdf
import pytest
import test_document_storage as document_storage
import test_rag_integration as rag_integration
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import KNOWLEDGE_COLLECTION, Settings
from app.documents.models import Document
from app.integrations.object_storage import ObjectStorage
from app.knowledge import pubmed, routes
from app.knowledge.models import KnowledgeAdmin, MedicalChunk, MedicalSource, MedicalVersion
from app.knowledge.schemas import PubMedHit, SourceMetadata, SourceUpload
from app.knowledge.storage import KnowledgeStorage, knowledge_key
from app.knowledge.vectors import KnowledgeVectors
from app.main import app
from app.rag.adapters import RagError

REGISTRY = "/knowledge/sources"

# Expose the original fixtures under the names pytest uses to resolve dependencies.
environment = rag_integration.environment
client = rag_integration.client


def pdf(value: str) -> bytes:
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), value)
        return document.tobytes()


def multipart(metadata, original):
    return {
        "data": {"metadata": json.dumps(metadata)},
        "files": {"file": ("synthetic.pdf", original, "application/pdf")},
    }


def synthetic_metadata(identity):
    return {
        "title": f"Synthetic guideline {identity}",
        "material_type": "guideline",
        "author": "Synthetic author",
        "publisher": "Synthetic publisher",
        "url": f"https://example.org/knowledge/{identity}#page=1",
        "doi": f"10.99999/{identity.upper()}",
        "publication_date": "2025-01-01",
        "update_date": "2025-02-01",
        "specialty": "Synthetic specialty",
        "language": "en",
        "jurisdiction": "Synthetic jurisdiction",
        "version": "1",
    }


def test_knowledge_input_and_storage_scope():
    metadata = synthetic_metadata(uuid4().hex)
    for unexpected in ("document_id", "object_key", "owner_id"):
        with pytest.raises(ValidationError):
            SourceUpload.model_validate({**metadata, unexpected: str(uuid4())})
    with pytest.raises(ValidationError):
        SourceUpload.model_validate({**metadata, "url": "https://user@example.org/guide"})
    with pytest.raises(ValidationError):
        Settings.model_validate({
            "redis_password": "synthetic-test-password", "rag_collection": KNOWLEDGE_COLLECTION,
        })
    transport = document_storage.FakeClient()
    personal = ObjectStorage(document_storage.settings(), transport)
    with pytest.raises(ValueError, match="Invalid document object key"):
        with personal.open_original(uuid4(), uuid4(), knowledge_key(uuid4(), uuid4())):
            pytest.fail("A personal read accepted a knowledge object key")
    assert transport.response is None


@pytest.mark.integration
def test_pubmed_citation_is_saved_without_a_file_or_publication(environment, client, monkeypatch):
    engine, _, _, _ = environment
    admin_id = UUID(client.get("/auth/me").json()["id"])
    pmid = str(uuid4().int % 10**18 + 10**18)
    citation = PubMedHit(
        title="Synthetic article", authors=["Synthetic author"], journal="Synthetic journal",
        year=2024, publication_date=None, pmid=pmid, doi=f"10.99999/{pmid}",
        pmcid=f"PMC{pmid}", language="eng", url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        abstract="Synthetic abstract", full_text_url=None, publisher_url=None,
    )
    monkeypatch.setattr(pubmed, "lookup", lambda *_: citation)
    monkeypatch.setattr(pubmed, "search", lambda *_: [citation])
    transport = document_storage.FakeClient()
    storage = KnowledgeStorage(document_storage.settings(), transport)
    monkeypatch.setattr(routes, "get_storage", lambda: storage)
    csrf = rag_integration.headers(client)
    search = "/knowledge/pubmed/search"
    save = "/knowledge/pubmed/save"
    query, selection = {"query": "synthetic"}, {"pmid": pmid}
    assert client.post(search, headers=csrf, json=query).status_code == 403
    assert client.post(save, headers=csrf, json=selection).status_code == 403
    with Session(engine) as db:
        db.add(KnowledgeAdmin(user_id=admin_id))
        db.commit()
    assert client.post(save, json=selection).status_code == 403
    result = client.post(search, headers=csrf, json=query)
    assert result.status_code == 200
    assert result.json()["items"][0]["abstract"] == citation.abstract

    saved = client.post(save, headers=csrf, json=selection)
    assert saved.status_code == 201, saved.text
    row = saved.json()
    assert row["current_version"] is None
    assert row["verification_status"] == "pending"
    assert row["pubmed_citation"]["pmcid"] == citation.pmcid
    assert "abstract" not in row["pubmed_citation"]
    assert transport.objects == {}
    assert client.post(save, headers=csrf, json=selection).status_code == 409
    after = None
    while True:
        page = client.get(REGISTRY, params={"after": after} if after else None).json()
        if row in page["items"]:
            break
        after = page["next_after"]
        assert after

    path = f"{REGISTRY}/{row['id']}"
    assert client.post(
        f"{path}/index", headers=csrf, json={"version_id": str(uuid4())},
    ).status_code == 409
    metadata = {key: row[key] for key in SourceMetadata.model_fields} | {"version": "1"}
    original = pdf(f"Synthetic article {pmid}.")
    assert client.put(
        f"{path}/version", headers=csrf, **multipart(metadata, original),
    ).status_code == 422
    assert transport.objects == {}
    upload = client.put(
        f"{path}/version", headers=csrf,
        **multipart({**metadata, "rights_confirmed": True}, original),
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["current_version"]["status"] == "uploaded"
    assert upload.json()["pubmed_citation"] == row["pubmed_citation"]


@pytest.mark.integration
def test_medical_knowledge_lifecycle(environment, client, monkeypatch):
    engine, settings, _, personal_vectors = environment
    admin_id = UUID(client.get("/auth/me").json()["id"])
    transport = document_storage.FakeClient()
    storage_settings = document_storage.settings()
    storage = KnowledgeStorage(storage_settings, transport)
    monkeypatch.setattr(routes, "get_storage", lambda: storage)
    identity = uuid4().hex
    metadata = synthetic_metadata(identity)
    synthetic_text = f"Synthetic chart zebra{identity[:12]} for version one."
    original = pdf(synthetic_text)
    with TestClient(app) as anonymous:
        assert anonymous.get("/knowledge/access").status_code == 401
        assert anonymous.get(REGISTRY).status_code == 401
        assert anonymous.post(
            "/knowledge/search", json={"query": synthetic_text},
        ).status_code == 401
    assert client.get("/knowledge/access").status_code == 403
    assert client.get(REGISTRY).status_code == 403
    csrf = rag_integration.headers(client)
    assert client.post(REGISTRY, headers=csrf, **multipart(metadata, original)).status_code == 403

    with Session(engine) as db:
        db.add(KnowledgeAdmin(user_id=admin_id))
        db.commit()
    access = client.get("/knowledge/access")
    assert access.status_code == 204
    assert access.headers["Cache-Control"] == "no-store"
    assert client.post(REGISTRY, **multipart(metadata, original)).status_code == 403
    result = client.post(REGISTRY, headers=csrf, **multipart(metadata, original))
    assert result.status_code == 201, result.text
    source = result.json()
    source_id = UUID(source["id"])
    version = source["current_version"]
    version_id = UUID(version["id"])
    path = f"{REGISTRY}/{source_id}"
    original_path = f"{path}/versions/{version_id}/original"
    download = client.get(original_path)
    assert download.status_code == 200
    assert download.content == original
    assert download.headers["Content-Disposition"].startswith("attachment;")
    assert download.headers["Cache-Control"] == "no-store"
    assert transport.response and transport.response.closed and transport.response.released
    assert client.get(f"{REGISTRY}/{uuid4()}/versions/{version_id}/original").status_code == 404
    assert source["verification_status"] == "pending"
    assert source["url"] == metadata["url"].split("#")[0]
    assert source["doi"] == metadata["doi"].lower()
    assert version["status"] == "uploaded"
    assert transport.objects == {
        (storage_settings.documents_s3_bucket, knowledge_key(source_id, version_id)): original,
    }
    after = None
    while True:
        registry = client.get(REGISTRY, params={"after": after} if after else None)
        assert registry.status_code == 200
        if any(item["id"] == source["id"] for item in registry.json()["items"]):
            break
        after = registry.json()["next_after"]
        assert after
    duplicate = {**metadata, "url": f"https://example.org/copy/{identity}", "doi": source["doi"]}
    assert client.post(REGISTRY, headers=csrf, **multipart(metadata, original)).status_code == 409
    assert client.post(REGISTRY, headers=csrf, **multipart(duplicate, original)).status_code == 409
    assert len(transport.objects) == 1

    def status(client_, target, **extra):
        return client_.put(
            f"{path}/status", headers=rag_integration.headers(client_),
            json={"status": target, "version_id": version["id"], **extra},
        )

    def index_current(client_):
        return client_.post(
            f"{path}/index",
            headers=rag_integration.headers(client_),
            json={"version_id": version["id"]},
        )

    def results(client_, query=synthetic_text):
        response = client_.post(
            "/knowledge/search", headers=rag_integration.headers(client_), json={"query": query},
        )
        assert response.status_code == 200, response.text
        return [hit for hit in response.json()["items"] if hit["source"]["id"] == str(source_id)]

    assert status(client, "published").status_code == 409
    assert index_current(client).status_code == 409
    assert results(client) == []
    review = {
        "verified_sha256": hashlib.sha256(original).hexdigest(),
        "review_note": "Matched the synthetic publisher and original.",
        "review_revision": version["review_revision"],
    }
    assert status(client, "verified", **{**review, "verified_sha256": "0" * 64}).status_code == 422
    initial_review = review.copy()
    edited_metadata = {key: value for key, value in metadata.items() if key != "version"}
    edited_metadata["title"] = "Corrected synthetic title"
    edited = client.put(f"{path}/metadata", headers=csrf, json=edited_metadata)
    assert edited.status_code == 200
    review["review_revision"] = edited.json()["current_version"]["review_revision"]
    assert status(client, "verified", **initial_review).status_code == 409
    assert status(client, "verified", **review).status_code == 200
    assert status(client, "published").status_code == 409
    write_batch = KnowledgeVectors.write_batch

    def interrupted_write(self, *args):
        write_batch(self, *args)
        raise RagError("SYNTHETIC_INTERRUPTION")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(KnowledgeVectors, "write_batch", interrupted_write)
        assert index_current(client).status_code == 503
    with Session(engine) as db:
        assert db.get(MedicalVersion, version_id).generation is None
    assert results(client) == []
    indexed = index_current(client)
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["current_version"]["chunk_count"] == 1
    with Session(engine) as db:
        generation = db.get(MedicalVersion, version_id).generation
    assert index_current(client).status_code == 200
    with Session(engine) as db:
        assert db.get(MedicalVersion, version_id).generation == generation
        assert db.scalar(select(func.count()).select_from(MedicalChunk).where(
            MedicalChunk.version_id == version_id,
        )) == 1
    medical_vectors = KnowledgeVectors(settings)
    assert medical_vectors.path == f"/collections/{KNOWLEDGE_COLLECTION}"
    assert medical_vectors.path != personal_vectors.path
    assert medical_vectors.count_scoped(medical_vectors.scope([str(generation)])) == 1
    assert results(client) == []

    with TestClient(app, client=(uuid4().hex, 50000)) as reader:
        assert reader.post(
            "/auth/register", headers=rag_integration.headers(reader),
            json={"email": f"{uuid4()}@example.org", "password": "synthetic-reader-password"},
        ).status_code == 201
        assert reader.get(REGISTRY).status_code == 403
        assert reader.get(original_path).status_code == 403
        assert status(reader, "published").status_code == 403
        assert index_current(reader).status_code == 403
        reader_id = UUID(reader.get("/auth/me").json()["id"])
        _, personal_id, _, personal_job = rag_integration.seed(
            engine, reader_id, "Synthetic private training text.",
        )
        rag_integration.index(environment, personal_job)
        assert status(client, "published").status_code == 200
        hits = results(reader)
        assert len(hits) == 1
        hit = hits[0]
        assert synthetic_text in hit["text"]
        assert hit["locators"][0]["kind"] == "page"
        assert hit["locators"][0]["index"] == 1
        assert hit["source"]["version_id"] == str(version_id)
        personal = reader.post(
            "/rag/search", headers=rag_integration.headers(reader), json={"query": synthetic_text},
        )
        assert personal.status_code == 200
        assert {item["document_id"] for item in personal.json()["items"]} == {str(personal_id)}
        assert "Synthetic private training text" not in str(hits)
        second_original = pdf(f"Synthetic revised chart zebra{identity[:12]}.")
        with Session(engine) as db:
            db.get(Document, personal_id).sha256 = hashlib.sha256(second_original).hexdigest()
            db.commit()
        second = {**edited_metadata, "version": "2"}
        assert client.put(
            f"{path}/version", headers=csrf, **multipart(second, second_original),
        ).status_code == 409
        assert len(results(reader)) == 1
        assert status(client, "outdated").status_code == 200
        assert results(reader) == []

        new_original = pdf(f"Synthetic new guideline llama{identity[:12]}.")
        replaced = client.put(f"{path}/version", headers=csrf, **multipart(second, new_original))
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["id"] == str(source_id)
        assert replaced.json()["current_version"]["version"] == "2"
        assert replaced.json()["current_version"]["status"] == "uploaded"
        assert status(client, "verified", **review).status_code == 409
        assert results(reader) == []
        retry = client.put(f"{path}/version", headers=csrf, **multipart(second, new_original))
        assert retry.json() == replaced.json()
        assert len(transport.objects) == 2
        assert client.get(original_path).status_code == 404
        version = replaced.json()["current_version"]
        new_review = {
            "version_id": version["id"], "review_revision": version["review_revision"],
            "verified_sha256": version["sha256"],
        }
        publish_path = f"{path}/review-publish"
        assert client.post(publish_path, json=new_review).status_code == 403
        assert reader.post(
            publish_path, headers=rag_integration.headers(reader), json=new_review,
        ).status_code == 403
        assert client.post(
            publish_path, headers=csrf, json={**new_review, "verified_sha256": "0" * 64},
        ).status_code == 422
        with monkeypatch.context() as interrupted:
            interrupted.setattr(KnowledgeVectors, "write_batch", interrupted_write)
            assert client.post(publish_path, headers=csrf, json=new_review).status_code == 503
        with Session(engine) as db:
            saved = db.get(MedicalVersion, UUID(version["id"]))
            assert saved.status == "verified"
            assert saved.reviewed_by == admin_id
            assert saved.generation is None
        assert results(reader) == []
        published = client.post(publish_path, headers=csrf, json=new_review)
        assert published.status_code == 200, published.text
        assert published.json()["current_version"]["status"] == "published"
        assert client.post(publish_path, headers=csrf, json=new_review).json() == published.json()
        new_hits = results(reader, f"Synthetic new guideline llama{identity[:12]}.")
        assert [item["source"]["version_id"] for item in new_hits] == [version["id"]]
        assert status(client, "outdated").status_code == 200
        assert client.post(publish_path, headers=csrf, json=new_review).status_code == 409
        assert status(client, "rejected").status_code == 200
        assert results(reader) == []
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(MedicalSource).where(
            MedicalSource.doi == source["doi"],
        )) == 1
        versions = list(db.scalars(select(MedicalVersion).where(
            MedicalVersion.source_id == source_id,
        ).order_by(MedicalVersion.version)))
        assert [(row.version, row.is_current, row.status) for row in versions] == [
            ("1", False, "outdated"), ("2", True, "rejected"),
        ]
