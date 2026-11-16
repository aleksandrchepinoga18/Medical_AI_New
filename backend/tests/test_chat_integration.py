"""Real disposable PostgreSQL; fake local LLM/RAG, no working documents."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

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
from app.llm.gateway import Gateway, GatewayError, Ollama
from app.main import app

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
    monkeypatch.setattr(routes, "search", lambda *args: [])

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
