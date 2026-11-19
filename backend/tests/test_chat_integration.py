"""Real disposable PostgreSQL; fake local LLM/RAG, no working documents."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app.auth.models import User
from app.auth.routes import current_user
from app.chat import routes
from app.chat.models import Conversation, Message
from app.db import database_url, get_db, get_settings
from app.documents.models import Document, DocumentTextVersion
from app.llm.gateway import Gateway, GatewayError, Ollama
from app.main import app
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError, fingerprint
from app.rag.hybrid import QdrantHybrid
from app.rag.models import RagChunk, RagIndexJob
from app.rag.service import search

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(monkeypatch):
    if os.environ.get("RUN_CHAT_INTEGRATION") != "1":
        pytest.skip("Use standalone compose.chat-test.yaml")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.postgres_db == "medical_ai_chat_test"
    assert settings.postgres_host == "postgres"
    assert settings.postgres_user == "synthetic_chat"
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    engine = create_engine(database_url(settings), hide_parameters=True)
    users = [
        User(
            id=uuid4(),
            email=f"{uuid4()}@example.org",
            password_hash="synthetic",
            created_at=datetime.now(UTC),
        )
        for _ in range(2)
    ]
    with Session(engine, expire_on_commit=False) as db:
        db.add_all(users)
        db.commit()

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[current_user] = lambda: users[0]
    monkeypatch.setattr(routes, "get_engine", lambda: engine)
    monkeypatch.setattr(routes, "search", lambda *args, **kwargs: [])

    class Fake:
        async def stream(self, messages):
            assert "document_excerpts" in messages[-1]["content"]
            yield "Synthetic "
            yield "answer"

    monkeypatch.setattr(routes, "gateway", lambda settings: Fake())
    try:
        with TestClient(app) as client:
            yield client, users
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()


def test_persistence_crud_and_isolation(setup):
    client, users = setup
    cid = client.post("/chat").json()["id"]
    assert client.patch(f"/chat/{cid}", json={"title": "Synthetic"}).status_code == 200
    result = client.post(f"/chat/{cid}/messages", json={"content": "Synthetic question"})
    assert '"type": "delta"' in result.text and '"type": "done"' in result.text
    history = client.get(f"/chat/{cid}").json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert all(m["status"] == "complete" for m in history)
    assert history[-1]["sources"] == []
    app.dependency_overrides[current_user] = lambda: users[1]
    assert cid not in [d["id"] for d in client.get("/chat").json()]
    for method, body in [
        ("GET", None),
        ("PATCH", {"title": "foreign"}),
        ("DELETE", None),
        ("POST", {"content": "foreign"}),
    ]:
        path = f"/chat/{cid}" + ("/messages" if method == "POST" else "")
        assert client.request(method, path, json=body).status_code == 404
    app.dependency_overrides[current_user] = lambda: users[0]
    assert client.delete(f"/chat/{cid}").status_code == 204
    assert client.get(f"/chat/{cid}").status_code == 404


def test_failed_generation_is_not_completed(setup, monkeypatch):
    client, _ = setup

    class Broken:
        async def stream(self, messages):
            yield "partial"
            raise GatewayError("timeout")

    monkeypatch.setattr(routes, "gateway", lambda settings: Broken())
    cid = client.post("/chat").json()["id"]
    result = client.post(f"/chat/{cid}/messages", json={"content": "Synthetic"})
    assert '"type": "error"' in result.text
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert len(messages) == 1 and messages[0]["status"] == "interrupted"


def test_ollama_length_completion_is_persisted_and_can_continue(setup, monkeypatch):
    client, _ = setup

    def handler(request):
        return httpx.Response(
            200,
            content=b'{"message":{"content":"Synthetic limited answer"}}\n'
            b'{"done":true,"done_reason":"length"}',
        )

    monkeypatch.setattr(
        routes,
        "gateway",
        lambda settings: Gateway(Ollama(settings, httpx.MockTransport(handler))),
    )
    cid = client.post("/chat").json()["id"]
    for _ in range(2):
        result = client.post(f"/chat/{cid}/messages", json={"content": "Synthetic"})
        events = [json.loads(line) for line in result.text.splitlines()]
        assert events[0]["type"] == "start"
        assert any(event["type"] == "delta" for event in events)
        assert events[-1]["type"] == "done"
        assert not any(event["type"] == "error" for event in events)
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert len(messages) == 4
    assert all(message["status"] == "complete" for message in messages)
    assert "Synthetic limited answer" in messages[-1]["content"]


def test_active_lease_rejects_retry_without_interrupting_pending(setup):
    client, _ = setup
    cid = client.post("/chat").json()["id"]
    with Session(routes.get_engine()) as db:
        conversation = db.get(Conversation, UUID(cid))
        assert conversation is not None
        conversation.generation = uuid4()
        conversation.lease_until = datetime.now(UTC) + timedelta(seconds=60)
        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content="Synthetic",
                status="pending",
            )
        )
        db.commit()
    result = client.post(f"/chat/{cid}/messages", json={"content": "Retry"})
    assert result.status_code == 409
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert len(messages) == 1 and messages[0]["status"] == "pending"


def send_events(client, cid, content):
    response = client.post(f"/chat/{cid}/messages", json={"content": content})
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines()]


def test_general_chat_skips_retrieval_and_keeps_streaming_history(setup, monkeypatch):
    client, _ = setup
    prompts = []

    def unexpected_search(*args, **kwargs):
        pytest.fail("General chat called personal retrieval")

    class Provider:
        async def stream(self, messages):
            prompts.append(messages)
            payload = json.loads(messages[-1]["content"])
            assert payload["personal_documents"] is False
            assert payload["document_excerpts"] == []
            yield "Первый "
            yield "ответ"

    monkeypatch.setattr(routes, "search", unexpected_search)
    monkeypatch.setattr(routes, "gateway", lambda settings: Provider())
    cid = client.post("/chat").json()["id"]
    for question in ("Как устроена турбина?", "Как работает её ротор?"):
        events = send_events(client, cid, question)
        assert [event["type"] for event in events] == ["start", "delta", "delta", "done"]
        assert events[-1]["sources"] == []
        assert "Подтверждения" not in events[1]["text"]
    assert prompts[-1][1:3] == [
        {"role": "user", "content": "Как устроена турбина?"},
        {"role": "assistant", "content": "Первый ответ"},
    ]


def publish_synthetic_report(owner, body, name, *, locators=None):
    ids = [uuid4() for _ in range(4)]
    doc_id, version_id, chunk_id, generation = ids
    locator = {"kind": "page", "index": 1, "start": 0, "end": len(body)}
    if locators is None:
        locators = [locator]
    with Session(routes.get_engine()) as db:
        db.add(
            Document(
                id=doc_id,
                owner_id=owner,
                original_filename=name,
                format="pdf",
                status="ready",
                size_bytes=100,
                sha256="0" * 64,
                object_key=f"synthetic/{doc_id}",
            )
        )
        db.flush()
        db.add(
            DocumentTextVersion(
                id=version_id,
                document_id=doc_id,
                version=1,
                normalized_text=body,
                locators=locators,
                extractor_version="synthetic",
            )
        )
        db.flush()
        db.add(
            RagChunk(
                id=chunk_id,
                user_id=owner,
                document_id=doc_id,
                text_version_id=version_id,
                ordinal=0,
                start_char=0,
                end_char=len(body),
                text=body,
                sources=locators,
            )
        )
        job = RagIndexJob(
            user_id=owner,
            document_id=doc_id,
            text_version_id=version_id,
            status="ready",
            operation="index",
            generation=generation,
            fingerprint=fingerprint(routes.get_settings()),
            chunk_count=1,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    return job_id, {
        "id": str(uuid5(chunk_id, str(generation))),
        "score": 1.0,
        "payload": {
            "user_id": str(owner),
            "document_id": str(doc_id),
            "text_version_id": str(version_id),
            "chunk_id": str(chunk_id),
            "generation": str(generation),
        },
    }


def test_exact_chat_filters_fallback_results_and_revalidates_history(setup, monkeypatch):
    client, users = setup
    owner, foreign = (user.id for user in users)
    settings = get_settings().model_copy(update={"rag_retrieval_mode": "hybrid"})
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "search", search)
    monkeypatch.setattr(OllamaEmbeddings, "embed", lambda *_: [[1.0]])

    def unavailable(*args):
        raise RagError("HTTP_503")

    def unexpected_gateway(*args):
        pytest.fail("Exact answer should not be generated by the model")

    monkeypatch.setattr(QdrantHybrid, "query_hybrid", unavailable)
    monkeypatch.setattr(routes, "gateway", unexpected_gateway)
    _, decoy = publish_synthetic_report(owner, "Глюкоза: 5,2 ммоль/л.", "decoy.pdf")
    _, wrong_tenant = publish_synthetic_report(foreign, "Калий: 5,2 ммоль/л.", "foreign.pdf")
    points = [decoy, wrong_tenant]
    monkeypatch.setattr(QdrantVectors, "query", lambda *_: points)
    cid = client.post("/chat").json()["id"]
    question = "В анализе № 1 калий 5,2 ммоль/л?"

    events = send_events(client, cid, question)
    assert [event["type"] for event in events] == ["start", "delta", "done"]
    assert "Подтверждения в документах не найдены" in events[1]["text"]
    assert events[-1]["sources"] == []

    report_job, match = publish_synthetic_report(owner, "Калий: 5,2 ммоль/л.", "match.pdf")
    points.append(match)
    events = send_events(client, cid, "В анализе № 2 калий 5,2 ммоль/л?")
    assert events[1]["text"] == "В документе указано: Калий: 5,2 ммоль/л"
    (citation,) = events[-1]["sources"]
    assert citation["document_id"] == match["payload"]["document_id"]
    assert citation["source_url"].endswith("#source-page-1")

    _, conflicting = publish_synthetic_report(
        owner, "Калий: 5,2 ммоль/л.\nКалий: 5,3 ммоль/л.", "other.pdf"
    )
    points.append(conflicting)
    events = send_events(client, cid, "В анализе № 3 калий 5,2 ммоль/л?")
    assert "разные результаты" in events[1]["text"]
    assert events[-1]["sources"] == []
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert messages[3]["sources"] == [citation]
    with Session(routes.get_engine()) as db:
        job = db.get(RagIndexJob, report_job)
        job.status = "deleted"
        job.operation = "delete"
        job.generation = None
        db.commit()
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert messages[3]["sources"] == []
    assert messages[-1]["status"] == "complete"


def test_personal_narrative_streams_only_relevant_context_and_handles_outage(setup, monkeypatch):
    client, users = setup
    owner = users[0].id
    _, decoy = publish_synthetic_report(
        owner, "Ночной режим сна. Сухой кашель.", "decoy.pdf"
    )
    _, relevant = publish_synthetic_report(
        owner, "Ночной кашель отмечен в учебной записи.", "match.pdf"
    )
    monkeypatch.setattr(routes, "search", search)
    monkeypatch.setattr(OllamaEmbeddings, "embed", lambda *_: [[1.0]])
    monkeypatch.setattr(QdrantVectors, "query", lambda *_: [decoy, relevant])

    class Provider:
        async def stream(self, messages):
            data = json.loads(messages[-1]["content"])
            assert data["personal_documents"] is True
            assert data["document_excerpts"] == ["Ночной кашель отмечен в учебной записи"]
            yield "В выписке "
            yield "упомянут ночной кашель."

    monkeypatch.setattr(routes, "gateway", lambda settings: Provider())
    cid = client.post("/chat").json()["id"]
    events = send_events(client, cid, "Что в моей выписке match.pdf сказано про ночной кашель?")
    assert [event["type"] for event in events] == ["start", "delta", "delta", "done"]
    assert [source["filename"] for source in events[-1]["sources"]] == ["match.pdf"]

    def unavailable(*args):
        raise RagError("HTTP_503")

    monkeypatch.setattr(QdrantVectors, "query", unavailable)
    response = client.post(
        f"/chat/{cid}/messages", json={"content": "Что в моей выписке match.pdf про ночной кашель?"}
    )
    assert response.status_code == 503
    messages = client.get(f"/chat/{cid}").json()["messages"]
    assert messages[-1]["status"] == "failed"
    assert messages[-2]["sources"][0]["filename"] == "match.pdf"


def test_indicator_overview_resolves_a_text_mention_without_using_vector_or_original(
    setup, monkeypatch
):
    client, users = setup
    owner, foreign = (user.id for user in users)
    ref = "101.02.030.pdf"
    _, match = publish_synthetic_report(
        owner, f"Вложение: {ref}.\nКалий: 5,2 ммоль/л.\nГлюкоза: 5,8 ммоль/л.", "upload.pdf"
    )
    publish_synthetic_report(owner, f"Копия: {ref}.bak.\nТТГ: 2 mIU/L.", "other.pdf")
    publish_synthetic_report(foreign, "ТТГ: 3 mIU/L.", ref)

    def unexpected(*args, **kwargs):
        pytest.fail("Indicator overview should use the authorized extraction directly")

    monkeypatch.setattr(routes, "search", unexpected)
    monkeypatch.setattr(routes, "gateway", unexpected)
    cid = client.post("/chat").json()["id"]
    question = f"Перечисли показатели из извлечённого текста {ref}"
    events = send_events(client, cid, question)
    assert events[1]["text"].endswith("Калий, Глюкоза")
    (citation,) = events[-1]["sources"]
    assert citation["filename"] == "upload.pdf"
    assert citation["document_id"] == match["payload"]["document_id"]
    assert citation["source_url"].endswith("#source-page-1")

    version_doc = UUID(match["payload"]["document_id"])
    with Session(routes.get_engine()) as db:
        doc = db.get(Document, version_doc)
        doc.status = "failed"
        doc.error_code = "ORIGINAL_MISSING"
        db.commit()
    events = send_events(client, cid, "Перечисли показатели анализа 1")
    assert events[1]["text"].endswith("Калий, Глюкоза")
    assert events[-1]["sources"][0]["document_number"] == 1
    with Session(routes.get_engine()) as db:
        db.add(
            DocumentTextVersion(
                document_id=version_doc,
                version=2,
                normalized_text="Новая версия.",
                locators=[],
                extractor_version="synthetic",
            )
        )
        db.commit()
    events = send_events(client, cid, question)
    assert "Подтверждения" in events[1]["text"]
    assert events[-1]["sources"] == []


def test_overview_requires_document_selection_when_reference_is_ambiguous(setup, monkeypatch):
    client, users = setup
    owner = users[0].id
    ref = "case-note.pdf"
    _, named = publish_synthetic_report(owner, "Калий: 5,2 ммоль/л.", ref)
    publish_synthetic_report(owner, f"Вложение: {ref}.\nГлюкоза: 5,8 ммоль/л.", "upload.pdf")

    def unexpected(*args, **kwargs):
        pytest.fail("An ambiguous overview must ask for clarification")

    monkeypatch.setattr(routes, "search", unexpected)
    monkeypatch.setattr(routes, "gateway", unexpected)
    cid = client.post("/chat").json()["id"]
    for scope in (ref, "в моих документах"):
        events = send_events(client, cid, f"Перечисли показатели {scope}")
        assert events[1]["text"].startswith("Уточните документ")
        assert events[-1]["sources"] == []
    link = "/documents/" + named["payload"]["document_id"]
    events = send_events(client, cid, f"Перечисли показатели {link}")
    assert events[1]["text"].endswith("Калий")
    assert events[-1]["sources"][0]["filename"] == ref


def test_summary_uses_current_extraction_for_a_registered_filename(setup, monkeypatch):
    client, users = setup
    publish_synthetic_report(users[0].id, "Пример учебного документа.", "my report.pdf")

    def unexpected(*args, **kwargs):
        pytest.fail("Summary should read the current extraction directly")

    class Provider:
        async def stream(self, messages):
            payload = json.loads(messages[-1]["content"])
            assert payload["document_context"] == {
                "fragments": [
                    {"document_number": 1, "page": 1, "text": "Пример учебного документа."}
                ],
                "partial": False,
            }
            yield "Краткое "
            yield "резюме."

    monkeypatch.setattr(routes, "search", unexpected)
    monkeypatch.setattr(routes, "gateway", lambda _: Provider())
    cid = client.post("/chat").json()["id"]
    events = send_events(client, cid, 'Summarize "my report.pdf"')
    assert [event["type"] for event in events] == ["start", "delta", "delta", "done"]
    assert events[-1]["sources"][0]["filename"] == "my report.pdf"


def test_exact_question_with_text_reference_keeps_search_in_resolved_generation(setup, monkeypatch):
    client, users = setup
    owner = users[0].id
    ref = "linked-note.pdf"
    _, match = publish_synthetic_report(
        owner, f"Упоминание {ref}.\nКалий: 5,2 ммоль/л.", "upload.pdf"
    )
    publish_synthetic_report(owner, "Калий: 6,2 ммоль/л.", "other.pdf")

    def dense_query(self, user_id, vector, generations, k):
        assert user_id == owner
        assert generations == [match["payload"]["generation"]]
        return [match]

    monkeypatch.setattr(routes, "search", search)
    monkeypatch.setattr(OllamaEmbeddings, "embed", lambda *_: [[1.0]])
    monkeypatch.setattr(QdrantVectors, "query", dense_query)
    cid = client.post("/chat").json()["id"]
    events = send_events(client, cid, f"Какой калий в {ref}?")
    assert events[1]["text"] == "В документе указано: Калий: 5,2 ммоль/л"
    assert events[-1]["sources"][0]["filename"] == "upload.pdf"


def test_numbered_comparison_explanation_and_missing_document(setup, monkeypatch):
    client, users = setup
    owner, foreign = (user.id for user in users)
    publish_synthetic_report(
        owner, "Калий: 5,8 ммоль/л (референс 3,5–5,1).", "first.pdf"
    )
    publish_synthetic_report(
        owner, "Potassium: 4.5 mmol/L (reference range 3.5–5.1).\nTSH: 2 mIU/L.", "second.pdf"
    )
    for _ in range(3):
        publish_synthetic_report(foreign, "Гемоглобин: 144 г/л.", "foreign.pdf")

    def unexpected(*args, **kwargs):
        pytest.fail("Report comparison and extraction should use the current text directly")

    monkeypatch.setattr(routes, "search", unexpected)
    monkeypatch.setattr(routes, "gateway", unexpected)
    cid = client.post("/chat").json()["id"]
    events = send_events(client, cid, "Сравни документы № 1 и № 2")
    assert "выше указанного" in events[1]["text"] and "в пределах" in events[1]["text"]
    assert "интервал не подтверждён" in events[1]["text"]
    assert {source["document_number"] for source in events[-1]["sources"]} == {1, 2}
    events = send_events(client, cid, "Объясни результаты документа номер 2")
    assert [source["document_number"] for source in events[-1]["sources"]] == [2]
    assert "выше указанного" not in events[1]["text"]
    events = send_events(client, cid, "Сравни документы № 1 и № 3")
    assert "№ 3" in events[1]["text"] and events[-1]["sources"] == []
    assert "Гемоглобин" not in events[1]["text"]


def test_reported_numbered_prompts_use_text_when_measurements_are_absent(setup, monkeypatch):
    client, users = setup
    owner, foreign = (user.id for user in users)
    first_text = "В учебной заметке описана работа с тромбоцитами."
    second_pages = [
        "На первой странице описана подготовка образца.",
        "На второй странице описан порядок передачи.",
    ]
    first_end = len(second_pages[0])
    second_text = "\n".join(second_pages)
    _, first = publish_synthetic_report(owner, first_text, "first.pdf")
    _, second = publish_synthetic_report(
        owner,
        second_text,
        "second.pdf",
        locators=[
            {"kind": "page", "index": 1, "start": 0, "end": first_end},
            {"kind": "page", "index": 2, "start": first_end + 1, "end": len(second_text)},
        ],
    )
    publish_synthetic_report(foreign, "Посторонняя запись.", "foreign.pdf")

    def unexpected(*args, **kwargs):
        pytest.fail("Document overviews should read owned, current PostgreSQL extractions")

    prompts = []

    class Provider:
        async def stream(self, messages):
            prompts.append(messages)
            if len(prompts) == 3:
                yield "Документ № 2 не "
                yield "предоставлен в данном запросе."
            elif len(prompts) == 4:
                yield "Гемоглобин: 999 г/л."
            else:
                yield "Описание подтверждённых выдержек."

    monkeypatch.setattr(routes, "search", unexpected)
    monkeypatch.setattr(routes, "gateway", lambda _: Provider())
    cid = client.post("/chat").json()["id"]
    listed = send_events(client, cid, "Перечисли показатели из документа № 1")
    assert "Упоминания без подтверждённого измерения: Тромбоциты" in listed[1]["text"]
    assert "тромбоцитами" not in listed[1]["text"] and not prompts
    assert listed[-1]["sources"][0]["document_id"] == first["payload"]["document_id"]

    explained = send_events(client, cid, "Объясни результаты документа № 2")
    assert "числовые измерения не подтверждены" in explained[1]["text"]
    assert explained[-1]["type"] == "done"
    second_fragments = [
        {"document_number": 2, "page": index, "text": page}
        for index, page in enumerate(second_pages, 1)
    ]
    explained_context = json.loads(prompts[0][-1]["content"])["document_context"]
    assert explained_context["fragments"] == second_fragments
    assert "Не сообщай медицинские числа" in prompts[0][0]["content"]
    assert {source["source_url"] for source in explained[-1]["sources"]} == {
        f"/documents/{second['payload']['document_id']}#source-page-1",
        f"/documents/{second['payload']['document_id']}#source-page-2",
    }

    compared = send_events(client, cid, "Сравни документы № 1 и № 2")
    assert "Числовое сопоставление не выполнено" in compared[1]["text"]
    assert json.loads(prompts[1][-1]["content"])["document_context"]["fragments"] == [
        {"document_number": 1, "page": 1, "text": first_text},
        *second_fragments,
    ]
    assert {source["document_number"] for source in compared[-1]["sources"]} == {1, 2}
    generic = send_events(client, cid, "Объясни документ № 2")
    assert generic[-1]["type"] == "done"
    generic_context = json.loads(prompts[2][-1]["content"])["document_context"]
    assert generic_context["fragments"] == second_fragments
    reply = "".join(event.get("text", "") for event in generic)
    assert "не предоставлен" not in reply
    assert all(page in reply for page in second_pages)
    invented = send_events(client, cid, "Проанализируй документ № 2")
    reply = "".join(event.get("text", "") for event in invented)
    assert "999" not in reply and second_pages[0] in reply
