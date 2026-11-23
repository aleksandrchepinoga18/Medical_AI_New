"""Run isolated evaluation turns through the production chat and auth routes."""

import json
import logging
from asyncio import CancelledError
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import ExitStack, aclosing
from os import environ
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat import agent, medical_sources, routes
from app.chat.models import Conversation, Message
from app.core.config import Settings
from app.db import get_engine, get_settings
from app.knowledge import pubmed
from app.knowledge import service as knowledge
from app.knowledge.models import MedicalChunk, MedicalSource, MedicalVersion
from app.llm.gateway import Gateway, GatewayError, ToolCall, gateway, provider_status
from app.main import app
from app.rag.adapters import OllamaEmbeddings, QdrantVectors, RagError
from evaluation.chat_fixtures import (
    Corpus,
    EvaluationVectors,
    fixed_knowledge_ranking,
    pubmed_client,
    seed_knowledge,
    seed_personal,
)
from evaluation.chat_metrics import (
    first_relevant_rank,
    normalized_answer,
    retrieval_scores,
    score_answer,
)

CHAT_LOG = logging.getLogger("uvicorn.error").getChild("chat")
GIGACHAT_LOG = logging.getLogger("uvicorn.error").getChild("gigachat")
SAFE_ERRORS = {
    "agent_context_limit", "agent_data_unavailable", "agent_no_answer", "agent_round_limit",
    "agent_repeated_tool", "agent_sources_missing", "agent_tool_limit",
    "external_consent_required", "gigachat_timeout", "gigachat_unavailable", "internal_error",
    "interrupted", "invalid_arguments", "invalid_response", "output_limit",
    "pubmed_not_requested", "pubmed_query_blocked", "pubmed_unavailable", "repeated_tool",
    "result_too_large",
    "retrieval_unavailable", "knowledge_unavailable", "timeout", "tools_unsupported", "unavailable",
}


def safe_code(code: Any) -> str:
    return code if isinstance(code, str) and code in SAFE_ERRORS else "other_error"


def argument_issues(call: ToolCall) -> list[dict[str, str]]:
    schema, _ = agent.TOOLS[call.name]
    try:
        schema.model_validate(call.arguments)
    except ValidationError as exc:
        return [
            {
                "field": issue["loc"][0]
                if issue["loc"] and issue["loc"][0] in schema.model_fields else "other",
                "type": issue["type"],
            }
            for issue in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
    return []


class Trace(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.tools: list[dict[str, Any]] = []
        self.tool_ms: dict[str, list[float]] = {}
        self.argument_issues: dict[str, list[dict[str, str]]] = {}
        self.llm_calls = 0
        self.llm_ms = 0.0
        self.llm_requests: list[dict[str, Any]] = []
        self.round: int | None = None
        self.active_tool: tuple[str, int | None, float] | None = None
        self.timed_out_tool: dict[str, Any] | None = None
        self.fallback: str | None = None
        self.knowledge_sources: set[str] = set()
        self.exposed_protected_text = False

    def observe_knowledge_result(
        self, hits: list[dict[str, Any]], corpus: Corpus, protected_text: str,
    ) -> None:
        self.knowledge_sources.update(source_aliases(hits, corpus))
        self.exposed_protected_text |= any(
            protected_text in normalized_answer(hit.get("text", "")) for hit in hits
        )

    def emit(self, record: logging.LogRecord) -> None:
        if not isinstance(record.args, tuple):
            return
        if record.msg.startswith("chat_agent_round "):
            self.round = record.args[3]
        elif record.msg.startswith("chat_agent_tool "):
            _, name, status, code, *_ = record.args
            timings = self.tool_ms.get(name, [])
            elapsed = timings.pop(0) if timings else 0.0
            self.tools.append({
                "name": name if name in agent.TOOLS else "unknown",
                "error": safe_code(code) if status == "error" else None,
                "ms": elapsed, "round": self.round,
            })
            if code == "invalid_arguments" and name in self.argument_issues:
                self.tools[-1]["argument_issues"] = self.argument_issues.pop(name)
        elif record.msg.startswith("chat_agent_fallback "):
            self.fallback = safe_code(record.args[1])
            if self.fallback == "timeout":
                self.note_tool_timeout()
        elif record.msg.startswith("gigachat_stream_done ") and self.llm_requests:
            reason = record.args[1]
            self.llm_requests[-1]["finish_reason"] = (
                reason if reason in {"stop", "length", "function_call"} else "other"
            )

    def note_tool_timeout(self) -> None:
        if (active_tool := self.active_tool) and self.timed_out_tool is None:
            name, round_number, started = active_tool
            self.timed_out_tool = {
                "name": name, "round": round_number,
                "ms": (perf_counter() - started) * 1000, "error": "timeout",
            }


class FixedProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.pending = iter(case["tools"])

    async def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[ToolCall | str, None]:
        if call := next(self.pending, None):
            if any(schema["function"]["name"] == call["name"] for schema in tools):
                yield ToolCall(call["name"], call["args"], "fixture-state")
                return
        if self.case.get("fault") == "unsupported":
            yield "Креатинин 99.99 mg/dL [S99]. DOI 10.99999/fabricated."
            return
        for message in reversed(messages):
            if message["role"] == "tool":
                result = json.loads(message["content"])
                if result.get("result"):
                    yield str(result["result"])
                    return
                excerpts = [f"{hit['text']} [{hit['id']}]"
                            for hit in result.get("sources", []) if hit.get("text")]
                if excerpts:
                    yield "\n".join(excerpts)
                    return
        yield "Синтетический ответ."

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncGenerator[str, None]:
        payload = json.loads(messages[-1]["content"])
        excerpts = [
            f"{entry['text']} [{entry['source_id']}]"
            for entry in payload.get("public_evidence", [])
            if entry["kind"] == "medical_knowledge" and entry.get("text")
        ]
        yield "\n".join(excerpts) or "Синтетический ответ."


class ObservedProvider:
    def __init__(self, model: Gateway | FixedProvider, trace: Trace, *, fallback: bool) -> None:
        self.model, self.trace, self.fallback = model, trace, fallback

    async def measure(
        self, events: AsyncIterator[ToolCall | str], messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[ToolCall | str, None]:
        phase = "fallback_answer" if self.fallback else "tool_round" if tools else "grounded_answer"
        context = agent.encode({"messages": messages, "tools": tools or []})
        request: dict[str, Any] = {
            "phase": phase, "round": None if self.fallback else self.trace.round,
            "context_bytes": len(context.encode()),
            "offered_tools": [tool["function"]["name"] for tool in tools or []],
            "tools": [], "received_text": False, "first_event_ms": None, "error": None,
        }
        self.trace.llm_requests.append(request)
        started = perf_counter()
        self.trace.llm_calls += 1
        try:
            async for event in events:
                if request["first_event_ms"] is None:
                    request["first_event_ms"] = (perf_counter() - started) * 1000
                if isinstance(event, ToolCall):
                    name = event.name if event.name in agent.TOOLS else "unknown"
                    request["tools"].append(name)
                elif event:
                    request["received_text"] = True
                yield event
        except GatewayError as exc:
            request["error"] = safe_code(str(exc))
            if request["error"] in {"timeout", "gigachat_timeout"}:
                request["timeout_origin"] = "provider_request"
            raise
        except CancelledError:
            request["error"] = "cancelled"
            raise
        except GeneratorExit:
            request["error"] = "closed"
            raise
        finally:
            request["ms"] = (perf_counter() - started) * 1000
            self.trace.llm_ms += request["ms"]

    async def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[ToolCall | str, None]:
        async with (
            aclosing(self.model.tool_stream(messages, tools)) as events,
            aclosing(self.measure(events, messages, tools)) as measured,
        ):
            async for event in measured:
                yield event

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncGenerator[str, None]:
        async with aclosing(self.measure(self.model.stream(messages), messages)) as measured:
            async for event in measured:
                assert isinstance(event, str)
                yield event


def isolated_settings(provider: str, *, live: bool, live_retrieval: bool) -> Settings:
    settings = get_settings()
    if (
        environ.get("RUN_RAG_INTEGRATION") != "1"
        or settings.app_env != "development" or settings.postgres_host != "postgres"
        or settings.postgres_db != "medical_ai_rag_test"
        or settings.postgres_user != "synthetic_rag_user"
        or settings.rag_collection != "medical_ai_personal_test"
        or settings.rag_qdrant_url != "http://qdrant:6333"
        or settings.rag_ollama_url != "http://ollama:11434"
        or settings.auth_origin != "http://localhost:3000" or settings.auth_cookie_secure
    ):
        raise ValueError("isolated_rag_test_environment_required")
    if live_retrieval and not live and settings.rag_embedding_model.split(":", 1)[0] != "bge-m3":
        raise ValueError("offline_qdrant_requires_bge_m3")
    settings = settings.model_copy(update={
        "llm_provider": provider, "chat_enabled": True, "rag_enabled": True,
        "rag_retrieval_mode": "dense",
        **({"gigachat_credentials": SecretStr("offline-fixture"), "gigachat_ca_bundle": ""}
           if not live else {}),
    })
    if live and provider == "gigachat":
        if not settings.gigachat_credentials.get_secret_value().strip():
            raise ValueError("gigachat_credentials_missing")
        if not settings.gigachat_ca_bundle or not Path(settings.gigachat_ca_bundle).is_file():
            raise ValueError("gigachat_ca_missing")
        if provider_status(settings, "gigachat") != "ready":
            raise ValueError("gigachat_ca_invalid")
    elif provider_status(settings, settings.llm_provider) != "ready":
        raise ValueError("selected_provider_unavailable")
    return settings


def headers(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/csrf")
    response.raise_for_status()
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": response.json()["csrf_token"]}


def registered(client: TestClient) -> UUID:
    response = client.post("/auth/register", headers=headers(client), json={
        "email": f"benchmark-{uuid4().hex}@example.org", "password": "synthetic-evaluation-654",
    })
    response.raise_for_status()
    return UUID(response.json()["id"])


def source_aliases(sources: list[dict[str, Any]], corpus: Corpus) -> set[str]:
    return {
        corpus.aliases.get(str(source.get("knowledge_source_id") or source.get("document_id")
                               or source.get("pmid")), "unknown")
        for source in sources
    }


def security_checks(
    case: dict[str, Any], aliases: set[str], answer: str, trace: Trace, queries: list[str],
    *, denied: bool, history_ok: bool | None, sources: list[dict[str, Any]],
) -> dict[str, bool | None]:
    ncbi_fields = [parse_qs(query) for query in queries]
    checks = {
        "no_pubmed": not queries and not any(t["name"] == "search_pubmed" for t in trace.tools),
        "no_sources": not sources,
        "no_foreign": "foreign" not in aliases and "9.99" not in normalized_answer(answer),
        "consent_denied": denied and trace.llm_calls == 0,
        "pubmed_privacy": any(fields.get("term") == ["hba1c"] for fields in ncbi_fields)
        and all(
            set(fields) <= {"db", "tool", "retmode", "sort", "retmax", "term", "id"}
            and fields.get("id", ["90000001"]) == ["90000001"]
            and fields.get("term", ["hba1c"]) == ["hba1c"]
            for fields in ncbi_fields
        ) and all("SYNTHETIC-PRIVATE" not in query for query in queries),
        "no_abstract": bool(sources)
        and all(source.get("abstract_available") is False for source in sources)
        and "Аннотация отсутствует" in answer and "Выдержка из аннотации" not in answer,
        "history_recheck": history_ok,
        "unsupported_redacted": (
            "не подтверждается доступными фрагментами" in answer
            and "99.99" not in normalized_answer(answer) and "S99" not in answer
            and "fabricated" not in answer
        ),
    }
    if hidden := case.get("hidden_source"):
        checks[f"no_{hidden}"] = (
            hidden not in aliases
            and hidden not in trace.knowledge_sources
            and not trace.exposed_protected_text
            and normalized_answer(case["protected_text"]) not in normalized_answer(answer)
        )
    return {name: checks[name] for name in case.get("checks", [])}


def check_history(
    client: TestClient, owner: UUID, corpus: Corpus,
) -> str:
    identifier = corpus.versions["a1c"]
    with Session(get_engine()) as db:
        version = db.get(MedicalVersion, identifier)
        assert version is not None
        source = db.get(MedicalSource, version.source_id)
        chunk = db.scalars(select(MedicalChunk).where(
            MedicalChunk.version_id == identifier,
        )).one()
        assert source is not None
        citation = medical_sources.knowledge_hit({
            "chunk_id": chunk.id, "start_char": chunk.start_char, "end_char": chunk.end_char,
            "text": chunk.text, "locators": chunk.locators,
            "source": {
                "id": source.id, "version_id": version.id, "version": version.version,
                "title": source.title, "url": source.url,
            },
        })
        citation.pop("text")
        citation["source_id"] = "S1"
        conversation = Conversation(user_id=owner)
        db.add(conversation)
        db.flush()
        message = Message(
            conversation_id=conversation.id, role="assistant", sources=[citation],
            content=f"Синтетический ответ [S1] {source.url}",
        )
        db.add(message)
        db.flush()
        path = f"/chat/{conversation.id}"
        message_id = str(message.id)
        published_status = version.status
        db.commit()

    def saved_message() -> dict[str, Any]:
        history = client.get(path)
        history.raise_for_status()
        return next(
            (row for row in history.json()["messages"] if row["id"] == message_id), {},
        )

    baseline = saved_message()
    url = citation["source_url"]
    if baseline.get("sources") != [citation] or not all(
        reference in baseline["content"] for reference in ("[S1]", url)
    ):
        return "fixture_missing_reference"

    with Session(get_engine()) as db:
        version = db.get(MedicalVersion, identifier)
        assert version is not None
        version.status = "outdated"
        db.commit()
    try:
        saved = saved_message()
        if not saved:
            return "fixture_missing_message"
        if saved["sources"] or any(ref in saved["content"] for ref in ("[S1]", url)):
            return "source_still_visible"
        return "passed"
    finally:
        with Session(get_engine()) as db:
            version = db.get(MedicalVersion, identifier)
            assert version is not None
            version.status = published_status
            db.commit()


def evaluate_case(
    client: TestClient, dataset: dict[str, Any], case: dict[str, Any], corpus: Corpus,
    settings: Settings, *, live: bool, owner: UUID,
) -> dict[str, Any]:
    trace = Trace()
    queries: list[str] = []
    execute = agent.execute_tool
    live_call = live and case.get("fault") != "unsupported"
    protected_text = normalized_answer(case.get("protected_text", ""))

    def observed_tool(*args: Any, **kwargs: Any) -> agent.ToolResult:
        call = args[3]
        started = perf_counter()
        name = call.name if call.name in agent.TOOLS else "unknown"
        trace.active_tool = (name, trace.round, started)
        try:
            result = execute(*args, **kwargs)
            if protected_text and call.name == "search_medical_knowledge":
                trace.observe_knowledge_result(result.public_hits, corpus, protected_text)
            if result.data.get("error") == "invalid_arguments" and call.name in agent.TOOLS:
                trace.argument_issues[call.name] = argument_issues(call)
            return result
        finally:
            trace.active_tool = None
            timings = trace.tool_ms.setdefault(call.name, [])
            timings.append((perf_counter() - started) * 1000)

    def provider(
        config: Settings, *, external_consent: bool = False, fallback: bool = False,
    ) -> ObservedProvider:
        if config.llm_provider == "gigachat" and not external_consent:
            raise AssertionError("cloud_call_without_consent")
        model = (
            gateway(config, external_consent=external_consent) if live_call else FixedProvider(case)
        )
        return ObservedProvider(model, trace, fallback=fallback)

    def fallback_provider(
        config: Settings, *, external_consent: bool = False,
    ) -> ObservedProvider:
        return provider(config, external_consent=external_consent, fallback=True)

    consent_case = "consent_denied" in case.get("checks", [])
    with ExitStack() as stack:
        stack.enter_context(patch.object(agent, "execute_tool", observed_tool))
        stack.enter_context(patch.object(routes, "agent_gateway", provider))
        stack.enter_context(patch.object(routes, "gateway", fallback_provider))
        stack.enter_context(patch.object(pubmed, "sleep", lambda _: None))
        stack.enter_context(patch.object(pubmed, "ncbi_client", lambda _: pubmed_client(
            dataset["publications"], queries, outage=case.get("fault") == "pubmed",
        )))
        CHAT_LOG.addHandler(trace)
        stack.callback(CHAT_LOG.removeHandler, trace)
        if live_call and settings.llm_provider == "gigachat":
            stack.callback(GIGACHAT_LOG.setLevel, GIGACHAT_LOG.level)
            GIGACHAT_LOG.setLevel(logging.INFO)
            GIGACHAT_LOG.addHandler(trace)
            stack.callback(GIGACHAT_LOG.removeHandler, trace)
        created = client.post("/chat", headers=headers(client))
        created.raise_for_status()
        path = f"/chat/{created.json()['id']}"
        data = {"content": case["question"], "llm_provider": (
            "gigachat" if consent_case else settings.llm_provider
        )}
        if settings.llm_provider == "gigachat" and not consent_case:
            data["external_consent"] = "gigachat"
        csrf = headers(client)
        started = perf_counter()
        response = client.post(f"{path}/messages", headers=csrf, json=data)
        elapsed = (perf_counter() - started) * 1000

    answer, sources, error = "", [], None
    denied = False
    if not response.is_success:
        detail = response.json().get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        denied = (
            consent_case and response.status_code == 403 and code == "external_consent_required"
        )
        if not denied:
            error = safe_code(code)
    else:
        complete = False
        for line in response.text.splitlines():
            event = json.loads(line)
            if event["type"] == "delta":
                answer += event["text"]
            if event["type"] == "error":
                error = safe_code(event["code"])
            if event["type"] == "done":
                sources, complete = event["sources"], True
        if not complete and not error:
            error = "other_error"

    aliases = source_aliases(sources, corpus)
    expected = {step["name"] for step in case["tools"]}
    actual = {call["name"] for call in trace.tools}
    history_probe = None
    history_ok = None
    if "history_recheck" in case.get("checks", []):
        history_probe = check_history(client, owner, corpus)
        if history_probe in {"passed", "source_still_visible"}:
            history_ok = history_probe == "passed"
    if "timeout" in {error, trace.fallback}:
        trace.note_tool_timeout()
        for request in trace.llm_requests:
            if request["error"] == "cancelled":
                request["error"] = "timeout"
                request["timeout_origin"] = "scenario_deadline"
    row = {
        "id": case["id"], "answer": score_answer(case, answer, sources, aliases, error),
        "routing_ok": expected == actual,
        "missing_tools": sorted(expected - actual),
        "mixed": {"search_pubmed", "search_medical_knowledge"} <= expected,
        "extra_pubmed": "search_pubmed" in actual - expected,
        "tools": trace.tools, "fallback": trace.fallback,
        "context_error": (
            "agent_context_limit" in {error, trace.fallback}
            or any(tool["error"] == "result_too_large" for tool in trace.tools)
        ),
        "llm_calls": trace.llm_calls, "llm_ms": trace.llm_ms, "wall_ms": elapsed,
        "llm_requests": trace.llm_requests,
        "llm_backend": "none" if consent_case else "live" if live_call else "fixture",
        "error": error, "sources": sorted(aliases),
        "checks": security_checks(
            case, aliases, answer, trace, queries, denied=denied, history_ok=history_ok,
            sources=sources,
        ),
    }
    if history_probe:
        row["history_probe"] = history_probe
    if trace.timed_out_tool:
        row["timed_out_tool"] = trace.timed_out_tool
    if protected_text:
        published = {item["id"] for item in dataset["corpus"] if item["status"] == "published"}
        row["retrieval_quality"] = {
            "irrelevant_sources": sorted((trace.knowledge_sources | aliases) & published),
        }
    return row


def retrieval(case: dict[str, Any], settings: Settings, corpus: Corpus) -> dict[str, Any]:
    query = case["retrieval"]
    started = perf_counter()
    error = None
    scores = None
    ranked: list[str] = []
    with Session(get_engine()) as db:
        scope_size = max(5, len(corpus.medical_points))
        try:
            hits = knowledge.search(db, settings, query["query"], scope_size)
        except RagError as exc:
            error = safe_code(str(exc))
        else:
            ranked = list(dict.fromkeys(corpus.aliases[str(hit["source"]["id"])] for hit in hits))
            scores = retrieval_scores(ranked, query["relevant"])
    return {
        "scores": scores, "error": error, "ms": (perf_counter() - started) * 1000,
        "ranked_sources": ranked, "first_relevant_rank": first_relevant_rank(
            ranked, set(query["relevant"]),
        ),
    }


def evaluate(
    dataset: dict[str, Any], provider: str, *, live: bool, live_retrieval: bool,
    allow_synthetic_cloud: bool = False,
    on_case: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if live and provider == "gigachat" and not allow_synthetic_cloud:
        raise ValueError("synthetic_cloud_consent_required")
    settings = isolated_settings(provider, live=live, live_retrieval=live_retrieval)
    corpus = Corpus()
    original_eligible = knowledge.eligible

    def eligible(config: Settings) -> Any:
        return original_eligible(config) & MedicalVersion.source_id.in_(corpus.medical_ids.values())

    with ExitStack() as stack:
        stack.enter_context(patch.object(routes, "get_settings", lambda: settings))
        stack.enter_context(patch.object(knowledge, "eligible", eligible))
        stack.enter_context(patch.object(medical_sources, "eligible", eligible))
        stack.enter_context(patch.object(knowledge, "KnowledgeVectors", EvaluationVectors))
        stack.enter_context(patch.object(
            QdrantVectors, "query", lambda *_args: corpus.personal_points,
        ))
        if not live_retrieval:
            stack.enter_context(patch.object(
                OllamaEmbeddings, "embed", lambda _self, texts: [[0.0] for _ in texts],
            ))
            stack.enter_context(patch.object(
                EvaluationVectors, "query_scoped", lambda _self, _vector, query, _scope, _count, k:
                fixed_knowledge_ranking(corpus, dataset, query)[:k],
            ))
        client = stack.enter_context(TestClient(app, client=(uuid4().hex, 50000)))
        other = stack.enter_context(TestClient(app, client=(uuid4().hex, 50000)))
        owners = (registered(client), registered(other))
        with Session(get_engine()) as db:
            seed_personal(db, dataset, owners, settings, corpus)
            seed_knowledge(db, dataset, owners[0], settings, corpus, live_retrieval=live_retrieval)
            db.commit()
        for record in dataset["publications"]:
            corpus.aliases[record["pmid"]] = record["id"]
        level = CHAT_LOG.level
        CHAT_LOG.setLevel(logging.INFO)
        stack.callback(CHAT_LOG.setLevel, level)
        results = []
        for index, case in enumerate(dataset["cases"], 1):
            row = evaluate_case(client, dataset, case, corpus, settings, live=live, owner=owners[0])
            if "retrieval" in case:
                row["retrieval"] = retrieval(case, settings, corpus)
            results.append(row)
            if on_case:
                on_case(index, len(dataset["cases"]), row)
            if live and provider == "gigachat" and row["error"] == "gigachat_unavailable":
                break
        return results
