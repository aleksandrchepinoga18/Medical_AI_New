"""Real PostgreSQL/Qdrant/Ollama/Redis/Celery, synthetic standalone Compose only."""

import json
import math
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from uuid import UUID, uuid4, uuid5

import pymupdf
import pytest
from alembic.config import Config
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.auth.models import User
from app.db import database_url, get_db, get_settings
from app.documents.extract import extract_docx, extract_pdf
from app.documents.models import Document, DocumentIngestionJob, DocumentTextVersion
from app.main import app
from app.rag import bm25_index
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError, generation_filter
from app.rag.bm25_index import backfill
from app.rag.evaluation import metrics
from app.rag.hybrid import QdrantHybrid, bm25
from app.rag.indexing import dispatch_index_once, process_index
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import enqueue, request_operation, search

pytestmark = pytest.mark.integration
PAST = datetime(2000, 1, 1, tzinfo=UTC)


def embedding_model_digest(embeddings: OllamaEmbeddings) -> str:
    model = embeddings.settings.rag_embedding_model
    try:
        response = embeddings.http.request("GET", "/api/tags")
    except RagError as error:
        pytest.fail(f"Cannot read Ollama /api/tags at http://ollama:11434 ({error.code})")
    models = response.get("models") if isinstance(response, dict) else None
    if not isinstance(models, list):
        pytest.fail("Ollama /api/tags response has no model list")
    for entry in models:
        if isinstance(entry, dict) and entry.get("name") == model:
            digest = entry.get("digest")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    pytest.fail(f"Ollama at http://ollama:11434 did not report {model} with a SHA-256 digest")


@pytest.fixture(scope="module")
def environment():
    if os.environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use standalone compose.rag-test.yaml")
    get_settings.cache_clear()
    settings = get_settings()
    if (
        settings.postgres_host != "postgres"
        or settings.postgres_db != "medical_ai_rag_test"
        or settings.rag_collection != "medical_ai_personal_test"
        or settings.rag_qdrant_url != "http://qdrant:6333"
        or settings.rag_ollama_url != "http://ollama:11434"
    ):
        pytest.fail("Refusing RAG integration tests outside synthetic environment")
    embeddings = OllamaEmbeddings(settings)
    embedding_model_digest(embeddings)
    try:
        embeddings.embed(["Synthetic RAG integration readiness check."])
    except RagError as error:
        pytest.fail(f"Ollama cannot embed with {settings.rag_embedding_model} ({error.code})")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url(settings), hide_parameters=True)

    def database() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    yield engine, settings, embeddings, QdrantVectors(settings)
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def headers(client):
    return {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": client.get("/auth/csrf").json()["csrf_token"],
    }


@pytest.fixture
def client(environment):
    with TestClient(app, client=(uuid4().hex, 50000)) as client:
        response = client.post(
            "/auth/register",
            headers=headers(client),
            json={
                "email": f"{uuid4()}@example.org",
                "password": "synthetic-rag-password",
            },
        )
        assert response.status_code == 201
        yield client


def seed(
    engine,
    owner=None,
    value="Synthetic blue train departs from platform seven.",
    schedule=True,
    *,
    format_="pdf",
    locators=None,
):
    with Session(engine) as db:
        if owner is None:
            owner = uuid4()
            db.add(
                User(
                    id=owner,
                    email=f"{owner}@example.org",
                    password_hash="synthetic",
                    created_at=datetime.now(UTC),
                )
            )
            db.flush()
        document = Document(
            owner_id=owner,
            original_filename=f"synthetic.{format_}",
            format=format_,
            size_bytes=100,
            sha256="0" * 64,
            object_key=f"synthetic/{uuid4()}",
            status="ready",
        )
        db.add(document)
        db.flush()
        version = DocumentTextVersion(
            document_id=document.id,
            version=1,
            normalized_text=value,
            locators=(
                [{"kind": "page", "index": 1, "start": 0, "end": len(value)}]
                if locators is None
                else locators
            ),
            extractor_version="synthetic-stage4",
        )
        db.add(version)
        db.flush()
        if schedule:
            enqueue(db, document, version)
        job = db.scalar(select(RagIndexJob).where(RagIndexJob.text_version_id == version.id))
        result = owner, document.id, version.id, job.id if job else None
        db.commit()
        return result


def reserve(environment, job_id):
    engine, settings, _, _ = environment
    with Session(engine) as db:
        db.get(RagIndexJob, job_id).available_at = PAST
        db.commit()
    deliveries = []
    assert dispatch_index_once(engine, lambda *args: deliveries.append(args), settings)
    assert deliveries and deliveries[0][0] == job_id
    return deliveries[0][1]


def index(environment, job_id):
    token = reserve(environment, job_id)
    engine, settings, embeddings, vectors = environment
    process_index(job_id, token, engine, settings, embeddings, vectors)
    with Session(engine) as db:
        job = db.get(RagIndexJob, job_id)
        assert job.status in {"ready", "deleted"}, job.error_code
    return token


def query(environment, owner, value="morning blue train platform"):
    engine, settings, embeddings, vectors = environment
    with Session(engine) as db:
        return search(db, owner, value, 5, settings, embeddings, vectors)


def test_real_worker_idempotency_and_owner_isolation(environment, client):
    from app.workers.celery_app import celery_app

    engine, settings, _, _ = environment
    owner = UUID(client.get("/auth/me").json()["id"])
    _, document_id, version_id, job_id = seed(engine, owner)
    token = reserve(environment, job_id)
    for _ in range(2):
        celery_app.send_task(
            "rag.index",
            args=[str(job_id), str(token)],
            queue="document_indexing",
            ignore_result=True,
        )
    deadline = time.monotonic() + settings.rag_index_seconds + 90
    while time.monotonic() < deadline:
        with Session(engine) as db:
            job = db.get(RagIndexJob, job_id)
            if job.status == "ready":
                break
            assert job.status != "failed", job.error_code
        dispatch_index_once(
            engine,
            lambda job, delivery: celery_app.send_task(
                "rag.index",
                args=[str(job), str(delivery)],
                queue="document_indexing",
                ignore_result=True,
            ),
            settings,
        )
        time.sleep(0.5)
    else:
        pytest.fail("Real Celery indexing did not complete")
    # Run duplicate directly too; publication must remain unchanged.
    process_index(job_id, token, *environment)
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(RagChunk)
                .where(RagChunk.text_version_id == version_id)
            )
            == 1
        )
    hits = query(environment, owner)
    assert hits and hits[0]["document_id"] == str(document_id)
    assert hits[0]["sources"][0]["index"] == 1
    assert query(environment, uuid4()) == []
    with TestClient(app) as stranger:
        assert stranger.get(f"/rag/documents/{document_id}/index").status_code == 401
    other_owner, other_document, _, other_job = seed(engine)
    index(environment, other_job)
    assert all(hit["document_id"] != str(other_document) for hit in query(environment, owner))
    assert all(hit["document_id"] != str(document_id) for hit in query(environment, other_owner))
    assert client.get(f"/rag/documents/{other_document}/index").status_code == 404
    for method in (client.post, client.delete):
        assert (
            method(f"/rag/documents/{other_document}/index", headers=headers(client)).status_code
            == 404
        )
        assert method(f"/rag/documents/{document_id}/index").status_code == 403
    assert client.post("/rag/search", json={"query": "train"}).status_code == 403
    assert (
        client.post(
            "/rag/search",
            headers=headers(client),
            json={
                "query": "train",
                "user_id": str(other_owner),
            },
        ).status_code
        == 422
    )
    response = client.post("/rag/search", headers=headers(client), json={"query": "train"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


def test_first_index_can_be_scheduled_through_api(environment, client):
    engine = environment[0]
    owner = UUID(client.get("/auth/me").json()["id"])
    _, document_id, version_id, _ = seed(engine, owner, schedule=False)
    path = f"/rag/documents/{document_id}/index"
    assert client.get(path).json()["status"] == "not_indexed"
    assert client.post(path, headers=headers(client)).status_code == 202
    assert client.post(path, headers=headers(client)).status_code == 409
    with Session(engine) as db:
        job = db.scalar(select(RagIndexJob).where(RagIndexJob.text_version_id == version_id))
        job_id = job.id
    index(environment, job_id)
    assert client.get(path).json()["status"] == "ready"


def test_forged_payload_cannot_bypass_sql_owner_validation(environment, monkeypatch):
    engine, _, _, vectors = environment
    owner, _, _, job_id = seed(engine)
    index(environment, job_id)
    _, foreign_document, foreign_version, foreign_job = seed(engine)
    index(environment, foreign_job)
    with Session(engine) as db:
        generation = db.get(RagIndexJob, job_id).generation
        foreign_chunk = db.scalar(
            select(RagChunk).where(RagChunk.text_version_id == foreign_version)
        )
        chunk_id = foreign_chunk.id
    forged = [
        {
            "id": str(uuid5(chunk_id, str(generation))),
            "score": 1.0,
            "payload": {
                "user_id": str(owner),
                "generation": str(generation),
                "chunk_id": str(chunk_id),
                "document_id": str(foreign_document),
                "text_version_id": str(foreign_version),
            },
        },
    ]
    monkeypatch.setattr(vectors, "query", lambda *args: forged)
    assert query(environment, owner) == []


def test_reindex_delete_and_current_text_version(environment, client):
    engine, _, _, vectors = environment
    owner = UUID(client.get("/auth/me").json()["id"])
    _, document_id, _, job_id = seed(engine, owner)
    first = index(environment, job_id)
    path = f"/rag/documents/{document_id}/index"
    assert client.post(path, headers=headers(client)).status_code == 202
    assert query(environment, owner) == []  # Old generation hidden immediately.
    second = index(environment, job_id)
    assert second != first
    assert len(query(environment, owner)) == 1
    with Session(engine) as db:
        previous = db.get(RagIndexJob, job_id).generation
        document = db.get(Document, document_id)
        version = DocumentTextVersion(
            document_id=document_id,
            version=2,
            normalized_text="Synthetic museum is closed on Monday.",
            locators=[],
            extractor_version="synthetic-new-version",
        )
        db.add(version)
        db.flush()
        enqueue(db, document, version)
        new_job = db.scalar(select(RagIndexJob).where(RagIndexJob.text_version_id == version.id))
        new_id = new_job.id
        db.commit()
    assert query(environment, owner) == []  # Latest text not yet indexed.
    index(environment, new_id)
    hits = query(environment, owner, "museum Monday")
    assert len(hits) == 1 and "museum" in hits[0]["text"]
    old = vectors.http.request(
        "POST",
        vectors.path + "/points/scroll",
        {
            "filter": {"must": [{"key": "generation", "match": {"value": str(previous)}}]},
            "limit": 10,
        },
    )
    assert old["result"]["points"] == []
    assert client.delete(path, headers=headers(client)).status_code == 202
    assert query(environment, owner) == []
    index(environment, new_id)
    assert client.get(path).json()["status"] == "deleted"
    removed = vectors.http.request(
        "POST",
        vectors.path + "/points/scroll",
        {
            "filter": {
                "must": [
                    {"key": "user_id", "match": {"value": str(owner)}},
                    {"key": "document_id", "match": {"value": str(document_id)}},
                ],
            },
            "limit": 10,
        },
    )
    assert removed["result"]["points"] == []
    with Session(engine) as db:
        assert db.get(Document, document_id).status == "ready"
        assert (
            db.scalar(
                select(func.count())
                .select_from(DocumentTextVersion)
                .where(DocumentTextVersion.document_id == document_id)
            )
            == 2
        )
    assert client.post(path, headers=headers(client)).status_code == 202
    index(environment, new_id)
    assert len(query(environment, owner)) == 1


def test_dependency_failure_recovery_lost_delivery_and_stale_token(environment, monkeypatch):
    engine, settings, embeddings, vectors = environment
    owner, document_id, _, job_id = seed(engine)
    token = reserve(environment, job_id)

    def unavailable(*args):
        raise RagError("HTTP_503")

    with monkeypatch.context() as patch:
        patch.setattr(embeddings, "embed", unavailable)
        process_index(job_id, token, *environment)
    with Session(engine) as db:
        assert db.get(Document, document_id).status == "ready"
        assert db.get(RagIndexJob, job_id).status == "pending"
    lost_token = reserve(environment, job_id)
    with Session(engine) as db:
        db.get(RagIndexJob, job_id).lease_expires_at = PAST
        db.commit()
    new_token = reserve(environment, job_id)
    assert new_token != lost_token
    process_index(job_id, lost_token, *environment)
    with monkeypatch.context() as patch:
        patch.setattr(vectors, "upsert", unavailable)
        process_index(job_id, new_token, *environment)
    assert query(environment, owner) == []
    index(environment, job_id)
    assert len(query(environment, owner)) == 1
    assert settings.rag_max_attempts == 5


def test_broker_failure_and_late_attempt_cannot_publish(environment, monkeypatch):
    engine, settings, _, vectors = environment
    owner, document_id, _, job_id = seed(engine)

    def broker_unavailable(*args):
        raise RuntimeError("synthetic broker failure")

    assert dispatch_index_once(engine, broker_unavailable, settings)
    with Session(engine) as db:
        job = db.get(RagIndexJob, job_id)
        assert job.status == "pending" and job.error_code == "INDEX_QUEUE_UNAVAILABLE"
    token = reserve(environment, job_id)
    original = vectors.upsert

    def supersede(*args):
        original(*args)
        with Session(engine) as db:
            request_operation(db, db.get(RagIndexJob, job_id), "index")

    with monkeypatch.context() as patch:
        patch.setattr(vectors, "upsert", supersede)
        process_index(job_id, token, *environment)
    assert query(environment, owner) == []
    index(environment, job_id)
    assert len(query(environment, owner)) == 1
    with Session(engine) as db:
        db.get(Document, document_id).status = "processing"
        db.commit()
    assert query(environment, owner) == []
    with Session(engine) as db:
        db.get(Document, document_id).status = "ready"
        db.commit()


def test_advisory_lock_prevents_parallel_processing(environment):
    engine = environment[0]
    _, _, _, job_id = seed(engine)
    token = reserve(environment, job_id)
    key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        connection.commit()
        try:
            process_index(job_id, token, *environment)
            with Session(engine) as db:
                assert db.get(RagIndexJob, job_id).status == "published"
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()
    process_index(job_id, token, *environment)
    with Session(engine) as db:
        assert db.get(RagIndexJob, job_id).status == "ready"


def test_extraction_enqueues_index_in_same_transaction(environment, monkeypatch):
    from app.documents import processing

    engine, settings, _, _ = environment
    owner, document_id, _, original_job = seed(engine)
    index(environment, original_job)
    with Session(engine) as db:
        document = db.get(Document, document_id)
        document.status = "queued"
        ingestion = DocumentIngestionJob(document_id=document_id, version=2, available_at=PAST)
        db.add(ingestion)
        db.commit()
        ingestion_id = ingestion.id
    monkeypatch.setattr(
        processing,
        "extract_original",
        lambda *args: processing.ExtractionResult(
            text="Synthetic next extracted version",
            locators=[],
        ),
    )
    processing.process_job(ingestion_id, engine, None, settings)
    with Session(engine) as db:
        version = db.scalar(
            select(DocumentTextVersion).where(
                DocumentTextVersion.document_id == document_id,
                DocumentTextVersion.version == 2,
            )
        )
        assert version is not None
        job = db.scalar(select(RagIndexJob).where(RagIndexJob.text_version_id == version.id))
        assert job is not None and job.status == "pending"
        job_id = job.id
        assert db.get(Document, document_id).status == "ready"
    assert query(environment, owner) == []
    index(environment, job_id)


def test_synthetic_retrieval_evaluation(environment):
    engine = environment[0]
    dataset = json.loads(
        (Path(__file__).parents[1] / "evaluation/personal_rag.json").read_text(encoding="utf-8")
    )
    owner = None
    labels = {}
    for item in dataset["documents"]:
        owner, document, _, job_id = seed(engine, owner, item["text"])
        labels[str(document)] = item["id"]
        index(environment, job_id)
    scores = []
    for example in dataset["queries"]:
        hits = query(environment, owner, example["query"])
        scores.append(
            metrics(
                [labels[hit["document_id"]] for hit in hits],
                example["relevance"],
                dataset["k"],
            )
        )
    summary = {
        name: sum(score[name] for score in scores) / len(scores)
        for name in ("recall", "mrr", "ndcg")
    }
    digest = embedding_model_digest(environment[2])
    print(
        json.dumps(
            {
                "dataset": dataset["version"],
                "k": dataset["k"],
                "model": environment[1].rag_embedding_model,
                "digest": digest,
                "dimensions": environment[1].rag_embedding_dimensions,
                **summary,
            },
            sort_keys=True,
        )
    )
    assert summary["recall"] >= 0.8
    assert summary["mrr"] >= 0.7
    assert summary["ndcg"] >= 0.7


def test_hybrid_backfill_isolation_fallback_and_generation_lifecycle(environment, monkeypatch):
    engine, settings, embeddings, vectors = environment
    hybrid = settings.model_copy(update={"rag_retrieval_mode": "hybrid"})
    hybrid_environment = engine, hybrid, embeddings, QdrantVectors(hybrid)
    index_v2 = QdrantHybrid(vectors)
    term = "ZX-761"
    owner, document, version, job_id = seed(engine, value=f"Synthetic panel {term}.")
    generation = index(environment, job_id)
    _, foreign_document, _, foreign_job = seed(engine, value=f"Foreign panel {term}.")
    foreign_generation = index(environment, foreign_job)
    legacy_path = vectors.path + "_bm25_v1"
    vectors.http.request(
        "PUT", legacy_path, {"vectors": {}, "sparse_vectors": {"bm25": {"modifier": "idf"}}}
    )
    with Session(engine) as db:
        db.add(
            RagChunk(
                id=uuid5(version, "chars-v1:100:0:0:10"),
                user_id=owner,
                document_id=document,
                text_version_id=version,
                ordinal=0,
                start_char=0,
                end_char=10,
                text="Prior layout",
                sources=[],
            )
        )
        db.commit()
    old_hits = vectors.query(owner, embeddings.embed([term])[0], [str(generation)], 5)
    legacy_point = {
        "id": old_hits[0]["id"],
        "vector": {"bm25": {"indices": [42], "values": [1]}},
        "payload": old_hits[0]["payload"],
    }
    vectors.http.request("PUT", legacy_path + "/points?wait=true", {"points": [legacy_point]})

    assert query(hybrid_environment, owner, term) == query(environment, owner, term)
    with monkeypatch.context() as mock:
        mock.setattr(OllamaEmbeddings, "embed", lambda *_: pytest.fail("Backfill re-embedded"))
        assert backfill(engine, settings, vectors) == backfill(engine, settings, vectors)
    generations = [str(generation), str(foreign_generation)]
    assert index_v2.count(owner, generations) == 1
    sparse_hits = index_v2.http.request(
        "POST",
        index_v2.path + "/points/query",
        {
            "query": bm25(term),
            "using": "bm25",
            "filter": generation_filter(settings, owner, generations),
            "with_payload": True,
        },
    )["result"]["points"]
    assert [hit["payload"]["document_id"] for hit in sparse_hits] == [str(document)]
    get_points = {"ids": [sparse_hits[0]["id"]], "with_vector": True}
    assert vectors.http.request("POST", legacy_path + "/points", get_points)["result"]
    old = vectors.http.request("POST", vectors.path + "/points", get_points)["result"][0]
    new = index_v2.http.request("POST", index_v2.path + "/points", get_points)["result"][0]
    assert new["vector"]["dense"] == pytest.approx(old["vector"], abs=1e-6)
    assert new["vector"]["bm25"]["indices"]
    with Session(engine) as db:
        hits = search(db, owner, term, 5, *hybrid_environment[1:], require_hybrid=True)
    assert len(hits) == 1 and hits[0]["document_id"] == str(document)
    assert hits[0]["score"] == pytest.approx(1.0)  # First place in each Qdrant prefetch.
    assert hits[0]["sources"][0]["url"].endswith("#source-page-1")
    assert hits[0]["text_version_id"] == str(version)

    def fail(*args):
        raise RagError("HTTP_503")

    _, _, _, new_job = seed(engine, owner, "Separate synthetic report")
    with monkeypatch.context() as mock:
        mock.setattr(bm25_index, "sync_generation", fail)
        new_generation = index(hybrid_environment, new_job)
    assert query(hybrid_environment, owner, term) == query(environment, owner, term)
    with Session(engine) as db, pytest.raises(RagError, match="HYBRID_COVERAGE_INCOMPLETE"):
        search(db, owner, term, 5, *hybrid_environment[1:], require_hybrid=True)
    backfill(engine, settings, vectors)
    assert index_v2.count(owner, [str(generation), str(new_generation)]) == 2

    with monkeypatch.context() as mock:
        mock.setattr(QdrantHybrid, "query_hybrid", fail)
        assert query(hybrid_environment, owner, term) == query(environment, owner, term)
    with Session(engine) as db:
        request_operation(db, db.get(RagIndexJob, job_id), "index")
    assert str(document) not in {
        hit["document_id"] for hit in query(hybrid_environment, owner, term)
    }
    replacement = index(environment, job_id)  # Worker still mirrors while retrieval is dense.
    assert index_v2.count(owner, [str(generation)]) == 0
    assert index_v2.count(owner, [str(replacement)]) == 1
    with Session(engine) as db:
        request_operation(db, db.get(RagIndexJob, job_id), "delete")
    assert str(document) not in {
        hit["document_id"] for hit in query(hybrid_environment, owner, term)
    }
    index(environment, job_id)
    backfill(engine, settings, vectors)
    assert index_v2.count(owner, [str(replacement)]) == 0
    assert vectors.http.request("POST", legacy_path + "/points", get_points)["result"] == []
    assert str(foreign_document) not in {
        hit["document_id"] for hit in query(hybrid_environment, owner, term)
    }


def test_hybrid_rejects_forged_lexical_point_after_rechecking_postgresql(environment, monkeypatch):
    engine, settings, embeddings, vectors = environment
    hybrid = settings.model_copy(update={"rag_retrieval_mode": "hybrid"})
    owner, _, _, job_id = seed(engine)
    generation = index(environment, job_id)
    _, foreign_document, foreign_version, foreign_job = seed(engine)
    index(environment, foreign_job)
    with Session(engine) as db:
        chunk_id = db.scalar(select(RagChunk.id).where(RagChunk.text_version_id == foreign_version))
    forged = {
        "id": str(uuid5(chunk_id, str(generation))),
        "score": 100,
        "payload": {
            "user_id": str(owner),
            "generation": str(generation),
            "chunk_id": str(chunk_id),
            "document_id": str(foreign_document),
            "text_version_id": str(foreign_version),
        },
    }
    legitimate = vectors.query(owner, embeddings.embed(["synthetic"])[0], [str(generation)], 5)
    monkeypatch.setattr(QdrantHybrid, "query_hybrid", lambda *args: [forged, *legitimate])
    hits = query((engine, hybrid, embeddings, vectors), owner)
    assert len(hits) == 1 and hits[0]["chunk_id"] == legitimate[0]["payload"]["chunk_id"]


def extract_synthetic_report(report, directory):
    path = directory / f"{report['id']}.{report['format']}"
    if report["format"] == "pdf":
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), report["text"])
            pdf.save(path)
        result = extract_pdf(path, 5, 10000)
    else:
        word = WordDocument()
        for line in report["text"].splitlines():
            word.add_paragraph(line)
        word.save(path)
        result = extract_docx(path, 10000, 1000000)
    assert result["status"] == "ready"
    return result


def test_hybrid_mixed_language_terms_and_cross_language_retrieval(environment, tmp_path):
    engine, settings, _, vectors = environment
    assert vectors.http.request("GET", "/")["version"] == "1.15.5"
    settings = settings.model_copy(
        update={
            "rag_collection": f"medical_ai_personal_test_ru_en_{uuid4().hex}",
            "rag_retrieval_mode": "hybrid",
            # Keep these jobs out of backfills against other synthetic collections.
            "rag_chunk_chars": settings.rag_chunk_chars + 1,
        }
    )
    vectors = QdrantVectors(settings)
    embeddings = OllamaEmbeddings(settings)
    hybrid = QdrantHybrid(vectors)
    hybrid.ensure()
    reports = {
        "glucose": (
            "Учебный отчёт. Glycated hemoglobin (HbA1c): average blood glucose "
            "over the past three months."
        ),
        "thyroid": "Анализ щитовидной железы. Thyroid-stimulating hormone (TSH).",
        "genetics": "Синтетическая панель: BRCA1, BRAF V600E, MAP2K1 и c.68_69delAG.",
    }
    owner = None
    documents = {}
    generations = []
    for label, content in reports.items():
        result = extract_synthetic_report(
            {"id": label, "format": "docx", "text": content},
            tmp_path,
        )
        assert result["text"].strip() == content
        owner, document, _, job_id = seed(
            engine, owner, result["text"], format_="docx", locators=result["locators"]
        )
        documents[label] = str(document)
        generations.append(str(index((engine, settings, embeddings, vectors), job_id)))
    scoped = generation_filter(settings, owner, generations)

    def lexical(query_text):
        return hybrid.http.request(
            "POST",
            hybrid.path + "/points/query",
            {
                "query": bm25(query_text),
                "using": "bm25",
                "filter": scoped,
                "with_payload": True,
            },
        )["result"]["points"]

    for term, label in (
        ("Find hba1c", "glucose"),
        ("Где указан TSH?", "thyroid"),
        ("BRCA1", "genetics"),
        ("BRAF V600E", "genetics"),
        ("MAP2K1", "genetics"),
        ("c.68_69delAG", "genetics"),
        ("щитовидной", "thyroid"),
    ):
        hits = lexical(term)
        assert hits and hits[0]["payload"]["document_id"] == documents[label], term
    assert lexical("c.68_69delAC") == []
    assert lexical("68_69delAG") == []

    russian_query = "Где указан гликированный гемоглобин?"
    assert lexical(russian_query) == []
    dense = vectors.query(owner, embeddings.embed([russian_query])[0], generations, 3)
    assert dense[0]["payload"]["document_id"] == documents["glucose"]
    for query_text in ("Find HbA1c", russian_query):
        with Session(engine) as db:
            hits = search(
                db, owner, query_text, 3, settings, embeddings, vectors, require_hybrid=True
            )
        assert hits[0]["document_id"] == documents["glucose"]
        assert "(HbA1c)" in hits[0]["text"] and hits[0]["sources"]


def evaluation_summary(dataset, rankings, durations, k):
    queries = dataset["queries"]
    assert len(queries) == len(rankings)
    grades = [
        metrics(ranking, example["relevance"], k)
        for example, ranking in zip(queries, rankings, strict=True)
        if example["relevance"]
    ]
    negatives = [
        ranking for example, ranking in zip(queries, rankings, strict=True)
        if not example["relevance"]
    ]
    false_matches = {}
    for category in ("numeric", "date"):
        cases = [
            (ranking, set(example["decoys"]), example["relevance"])
            for example, ranking in zip(queries, rankings, strict=True)
            if example["category"] == category
        ]
        false_matches[category] = {
            "queries": len(cases),
            "decoy_top1_rate": mean(bool(set(r[:1]) & decoys) for r, decoys, _ in cases),
            "decoy_at_k_rate": mean(bool(set(r) & decoys) for r, decoys, _ in cases),
            "relevant_top1_rate": mean(
                bool(r and relevance.get(r[0], 0)) for r, _, relevance in cases
            ),
        }
    return {
        **{name: mean(score[name] for score in grades) for name in ("recall", "mrr", "ndcg")},
        "latency_ms_p50": median(durations),
        "latency_ms_p95": sorted(durations)[math.ceil(0.95 * len(durations)) - 1],
        "no_relevant_queries": len(negatives),
        "no_relevant_nonempty_rate": mean(bool(ranking) for ranking in negatives),
        "false_matches": false_matches,
        "per_query": {
            example["id"]: ranking for example, ranking in zip(queries, rankings, strict=True)
        },
    }


def test_hybrid_retrieval_evaluation(environment, tmp_path):
    engine, settings, embeddings, _ = environment
    digest = embedding_model_digest(embeddings)
    qdrant = environment[3].http.request("GET", "/")["version"]
    assert qdrant == "1.15.5"
    hybrid = settings.model_copy(
        update={
            "rag_retrieval_mode": "hybrid",
            "rag_embedding_digest": digest,
            "rag_collection": f"medical_ai_personal_test_eval_{uuid4().hex}",
        }
    )
    configs = {"dense": hybrid.model_copy(update={"rag_retrieval_mode": "dense"}), "hybrid": hybrid}
    embeddings = OllamaEmbeddings(hybrid)
    vectors = QdrantVectors(hybrid)
    dataset = json.loads(
        (Path(__file__).parents[1] / "evaluation/hybrid_rag.json").read_text(encoding="utf-8")
    )
    owners = {}
    labels = {}
    for report in dataset["documents"]:
        tenant = report["tenant"]
        extracted = extract_synthetic_report(report, tmp_path)
        owner, document, _, job_id = seed(
            engine,
            owners.get(tenant),
            extracted["text"],
            format_=report["format"],
            locators=extracted["locators"],
        )
        owners[tenant] = owner
        labels[str(document)] = report["id"], tenant
        index((engine, hybrid, embeddings, vectors), job_id)
    assert backfill(engine, hybrid, vectors)[0] == len(dataset["documents"])

    def retrieve(mode, example, k):
        tenant = example["tenant"]
        start = time.perf_counter()
        with Session(engine) as db:
            hits = search(
                db,
                owners[tenant],
                example["query"],
                k,
                configs[mode],
                embeddings,
                vectors,
                require_hybrid=(mode == "hybrid"),
            )
        elapsed = (time.perf_counter() - start) * 1000
        assert len(hits) <= k
        assert len({hit["chunk_id"] for hit in hits}) == len(hits)
        ranking = []
        for hit in hits:
            label, owner = labels[hit["document_id"]]
            assert owner == tenant and hit["sources"]
            ranking.append(label)
        return ranking, elapsed

    repeats = 3
    output = {}
    for k in (3, 5):
        rankings = {mode: [] for mode in configs}
        durations = {mode: [] for mode in configs}
        for mode in configs:
            retrieve(mode, dataset["queries"][0], k)  # Warm each path before timing.
        for repeat in range(repeats):
            for index_, example in enumerate(dataset["queries"]):
                order = ("dense", "hybrid") if (index_ + repeat) % 2 == 0 else ("hybrid", "dense")
                for mode in order:
                    ranking, elapsed = retrieve(mode, example, k)
                    durations[mode].append(elapsed)
                    if repeat == 0:
                        rankings[mode].append(ranking)
        output[k] = {
            mode: evaluation_summary(dataset, rankings[mode], durations[mode], k)
            for mode in configs
        }
    deltas = {
        k: {
            metric: by_mode["hybrid"][metric] - by_mode["dense"][metric]
            for metric in ("recall", "mrr", "ndcg", "latency_ms_p50", "latency_ms_p95")
        }
        for k, by_mode in output.items()
    }
    quality_deltas = [
        delta[metric] for delta in deltas.values() for metric in ("recall", "mrr", "ndcg")
    ]
    if max(quality_deltas) <= 0:
        verdict = "no_gain_in_ranking_metrics"
    elif min(quality_deltas) < 0:
        verdict = "mixed_ranking_results"
    else:
        verdict = "ranking_gain_on_this_fixture"
    run_config = {
        "dataset": dataset["version"],
        "model": settings.rag_embedding_model,
        "digest": digest,
        "dimensions": settings.rag_embedding_dimensions,
        "chunk_chars": settings.rag_chunk_chars,
        "chunk_overlap": settings.rag_chunk_overlap,
        "qdrant": qdrant,
        "latency_repeats": repeats,
    }
    report = {
        **run_config,
        "hybrid_index": "hybrid_v2",
        "cutoffs": output,
        "hybrid_minus_dense": deltas,
        "ranking_verdict": verdict,
    }
    baseline_path = os.environ.get("RAG_EVALUATION_BASELINE")
    if baseline_path:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        assert all(baseline.get(key) == value for key, value in run_config.items())
        assert baseline.get("hybrid_index") is None, "Expected the previous Hybrid RAG report"
        comparison = {}
        queries = dataset["queries"]
        for k, by_mode in output.items():
            previous = baseline["cutoffs"][str(k)]["hybrid"]
            previous_top1 = {
                query_id: next(iter(ranking), "")
                for query_id, ranking in previous["per_query"].items()
            }
            current = by_mode["hybrid"]
            comparison[k] = {
                **{metric: current[metric] - previous[metric] for metric in deltas[k]},
                "false_matches": {
                    category: {
                        metric: stats[metric] - previous["false_matches"][category][metric]
                        for metric in ("decoy_top1_rate", "decoy_at_k_rate")
                    }
                    for category, stats in current["false_matches"].items()
                },
                "relevant_top1_rate": {
                    category: current["false_matches"][category]["relevant_top1_rate"]
                    - mean(
                        bool(example["relevance"].get(previous_top1[example["id"]]))
                        for example in queries
                        if example["category"] == category
                    )
                    for category in ("numeric", "date")
                },
            }
        report["hybrid_minus_previous_hybrid"] = comparison
    print(json.dumps(report, sort_keys=True))
