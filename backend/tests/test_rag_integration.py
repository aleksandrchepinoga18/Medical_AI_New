"""Real PostgreSQL/Qdrant/Ollama/Redis/Celery, synthetic standalone Compose only."""

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.auth.models import User
from app.db import database_url, get_db, get_settings
from app.documents.models import Document, DocumentIngestionJob, DocumentTextVersion
from app.main import app
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError
from app.rag.evaluation import metrics
from app.rag.indexing import dispatch_index_once, process_index
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import enqueue, request_operation, search

pytestmark = pytest.mark.integration
PAST = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def environment():
    if os.environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use standalone compose.rag-test.yaml")
    get_settings.cache_clear()
    settings = get_settings()
    if (
        settings.postgres_db != "medical_ai_rag_test"
        or settings.rag_collection != "medical_ai_personal_test"
        or settings.rag_qdrant_url != "http://qdrant:6333"
        or settings.rag_ollama_url != "http://ollama:11434"
    ):
        pytest.fail("Refusing RAG integration tests outside synthetic environment")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url(settings), hide_parameters=True)

    def database() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    yield engine, settings, OllamaEmbeddings(settings), QdrantVectors(settings)
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
            original_filename="synthetic.pdf",
            format="pdf",
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
            locators=[{"kind": "page", "index": 1, "start": 0, "end": len(value)}],
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
    models = environment[2].http.request("GET", "/api/tags")["models"]
    digest = next(
        model["digest"] for model in models if model["name"] == environment[1].rag_embedding_model
    )
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
