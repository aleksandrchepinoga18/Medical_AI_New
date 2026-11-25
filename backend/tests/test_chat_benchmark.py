import json
from argparse import Namespace
from asyncio import run
from contextlib import nullcontext
from os import environ
from ssl import CERT_REQUIRED
from time import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from certifi import where
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm import gigachat
from app.llm.gateway import GatewayError, ToolCall
from app.rag.adapters import JsonHttp
from evaluation import chat_runtime
from evaluation.chat_fixtures import Corpus, seed_knowledge
from evaluation.chat_metrics import (
    citations_valid,
    compare_reports,
    exact_number,
    normalized_answer,
    retrieval_scores,
    score_answer,
    summarize,
)
from evaluation.chat_runtime import evaluate
from evaluation.extraction_tracking import register
from scripts import evaluate_chat


def test_benchmark_dataset_relevance_points_to_published_fixture_sources():
    dataset, digest = evaluate_chat.load_dataset()
    published = {row["id"] for row in dataset["corpus"] if row["status"] == "published"}
    retrieval_cases = [case["retrieval"] for case in dataset["cases"] if "retrieval" in case]
    assert len(digest) == 64 and len(dataset["cases"]) == 30
    assert len(retrieval_cases) == 8
    assert all(row["relevant"] and set(row["relevant"]) <= published for row in retrieval_cases)


def test_retrieval_metrics_keep_k_denominator_when_fewer_results_returned():
    scores = retrieval_scores(["other", "target", "target"], ["target", "missing"])
    assert scores == {
        "recall_at_3": 0.5, "precision_at_3": 1 / 3,
        "recall_at_5": 0.5, "precision_at_5": 1 / 5,
        "mrr": 0.5, "hit_rate": 1.0,
    }
    assert retrieval_scores([], ["target"])["mrr"] == 0


def test_cbc_reference_reports_fifth_rank_in_search_order(monkeypatch):
    dataset, _ = evaluate_chat.load_dataset()
    case = next(row for row in dataset["cases"] if row["id"] == "cbc-reference")
    ranked = ["a1c", "creatinine", "electrolytes", "gfr", "cbc", "liver"]
    corpus = Corpus(aliases={str(i): alias for i, alias in enumerate(ranked)})
    hits = [{"source": {"id": i}} for i in range(len(ranked))]
    monkeypatch.setattr(chat_runtime, "get_engine", lambda: None)
    monkeypatch.setattr(chat_runtime, "Session", lambda _: nullcontext())
    monkeypatch.setattr(chat_runtime.knowledge, "search", lambda *_: hits)

    report = chat_runtime.retrieval(case, Settings(), corpus)

    assert report["ranked_sources"] == ranked and report["first_relevant_rank"] == 5
    assert report["scores"] == {
        "recall_at_3": 0.0, "precision_at_3": 0.0,
        "recall_at_5": 1.0, "precision_at_5": 0.2, "mrr": 0.2, "hit_rate": 1.0,
    }


@pytest.mark.parametrize("hidden", ["draft", "stale"])
@pytest.mark.parametrize("leak", [None, "tool_source", "tool_text", "answer_source", "answer_text"])
def test_hidden_source_security_fails_only_for_disclosure(hidden, leak):
    dataset, _ = evaluate_chat.load_dataset()
    case = next(row for row in dataset["cases"] if row["id"] == f"{hidden}-hidden")
    protected = case["protected_text"]
    query = case["tools"][0]["args"]["query"]
    corpus = Corpus(aliases={"public-id": "cbc", "hidden-id": hidden})
    sources = [{"source_id": "S1", "knowledge_source_id": "public-id"}]
    hits = [{**sources[0], "text": "A complete blood count measures platelets."}]
    answer = f"Источник {query} не найден. Найденный [S1] относится к другой теме."
    if leak == "tool_source":
        hits.append({"knowledge_source_id": "hidden-id", "text": ""})
    elif leak == "tool_text":
        hits[0]["text"] = protected
    elif leak == "answer_source":
        sources.append({"source_id": "S2", "knowledge_source_id": "hidden-id"})
    elif leak == "answer_text":
        answer += f" {protected.lower()}."
    trace = chat_runtime.Trace()
    trace.observe_knowledge_result(hits, corpus, normalized_answer(protected))
    aliases = chat_runtime.source_aliases(sources, corpus)

    assert chat_runtime.security_checks(
        case, aliases, answer, trace, [], denied=False, history_ok=False, sources=sources,
    ) == {f"no_{hidden}": leak is None}
    if leak is None:
        assert score_answer(case, answer, sources, aliases, None)["status"] == "correct"
        irrelevant_answer = "Справочная статья об общем анализе крови [S1]."
        assert score_answer(case, irrelevant_answer, sources, aliases, None)[
            "status"
        ] == "unsupported"


def test_numerical_matching_and_citation_checks_reject_invented_details():
    assert exact_number("Креатинин: 0,91 mg/dL.", "0.91 mg/dL")
    assert not exact_number("Креатинин: 10.91 mg/dL.", "0.91 mg/dL")
    assert not exact_number("Креатинин: 0.91 mmol/L.", "0.91 mg/dL")
    url = "https://pubmed.ncbi.nlm.nih.gov/19671910/"
    sources = [{"source_id": "S1", "pmid": "19671910", "source_url": url}]
    assert citations_valid(f"PMID 19671910 [S1] {url}", sources)
    assert not citations_valid("PMID 123456 [S1]", sources)
    assert not citations_valid("10.99999/evaluation-false [S1]", sources)
    assert not citations_valid("https://example.org/doi.org/fake [S1]", sources)
    assert not citations_valid("Источник [S99]", sources)


def test_personal_diagnostics_distinguish_fixture_spelling_from_exact_scores():
    dataset, _ = evaluate_chat.load_dataset()
    cases = {case["id"]: case for case in dataset["cases"]}
    potassium = cases["potassium-first"]
    assert chat_runtime.expected_content(potassium, "Калий: **4,2** ммоль / л.") == {
        "fact_hits": 0, "numeric_hits": 0, "format_equivalent_numeric_hits": 1,
    }
    assert chat_runtime.expected_content(potassium, "Калий: 14,2 ммоль/л.")[
        "format_equivalent_numeric_hits"
    ] == 0
    trend = cases["trend-creatinine"]
    assert chat_runtime.expected_content(trend, "+14,29 %")[
        "format_equivalent_numeric_hits"
    ] == 1
    assert chat_runtime.expected_content(trend, "-14,29 %")[
        "format_equivalent_numeric_hits"
    ] == 0
    assert chat_runtime.expected_content(cases["personal-search"], "Креатинин найден") == {
        "fact_hits": 0, "numeric_hits": 0, "format_equivalent_numeric_hits": 0,
        "localized_indicator_present": True,
    }


@pytest.mark.parametrize(
    ("answer", "sources", "complete", "expected"),
    [
        ("", [], False, None),
        ("Нет финального сообщения", [], True, None),
        ("Выдержка из аннотации: синтетический текст", [], False, False),
        ("Аннотация отсутствует", [{"kind": "pubmed", "abstract_available": False}], True, True),
        ("", [{"kind": "pubmed", "abstract_available": True}], False, False),
    ],
)
def test_no_abstract_distinguishes_failure_from_unverified(answer, sources, complete, expected):
    case = {"tools": [{"name": "search_pubmed"}], "checks": ["no_abstract"]}
    trace = chat_runtime.Trace()
    trace.tools.append({"name": "search_pubmed", "error": None})
    assert chat_runtime.security_checks(
        case, set(), answer, trace, [], denied=False, history_ok=None,
        sources=sources, answer_complete=complete,
    ) == {"no_abstract": expected}


def test_evaluation_checkpoints_and_resumes_legacy_rows(monkeypatch, tmp_path):
    output = tmp_path / "checkpoint.json"
    settings = Settings(llm_provider="gigachat", redis_password="synthetic")
    monkeypatch.setattr(evaluate_chat, "isolated_settings", lambda *args, **kwargs: settings)
    preflights = []

    async def preflight(_settings):
        preflights.append(True)

    monkeypatch.setattr(evaluate_chat, "check_gigachat_oauth", preflight)
    args = Namespace(
        dry_run=False, mode="live", provider="gigachat", retrieval="qdrant",
        commit="42375cb", mlflow_uri=None, compare=None, allow_synthetic_cloud=True,
        case=["creatinine-first", "pubmed-pmid", "pubmed-doi"],
        output=output, resume=None,
    )
    seen = []

    def run_cases(dataset, _provider, *, on_case, **_kwargs):
        for index, case in enumerate(dataset["cases"], 1):
            identifier = case["id"]
            seen.append(identifier)
            if not args.resume and identifier == "pubmed-doi":
                raise KeyboardInterrupt
            is_timeout = identifier == "pubmed-pmid"
            error = "gigachat_unavailable" if is_timeout else None
            row = {
                "id": identifier, "llm_backend": "live", "llm_calls": 1,
                "llm_ms": 5, "wall_ms": 6,
                "checks": dict.fromkeys(case["checks"], not is_timeout),
                "tools": [], "routing_ok": True, "mixed": False, "extra_pubmed": False,
                "context_error": False, "fallback": None, "sources": [], "error": error,
                "answer": score_answer(case, "0.91 mg/dL" if not error else "", [], set(), error),
                "llm_requests": [{"error": "gigachat_timeout" if is_timeout else None}],
            }
            on_case(index, len(dataset["cases"]), row)
            saved = json.loads(output.read_text(encoding="utf-8"))
            assert saved["cases"][-1]["id"] == identifier

    monkeypatch.setattr(evaluate_chat, "evaluate", run_cases)
    with pytest.raises(KeyboardInterrupt):
        evaluate_chat.safe_report(args)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert not saved["complete"] and saved["cases"][1]["answer"]["status"] == "technical_error"
    for key in ("checkpoint_version", "legacy_scenarios"):
        saved.pop(key)
    output.write_text(json.dumps(saved), encoding="utf-8")
    args.resume = output
    args.commit = "7654321"
    with pytest.raises(ValueError, match="^resume_mismatch_commit$"):
        evaluate_chat.safe_report(args)
    assert len(preflights) == 1

    args.commit = "42375cb"
    args.dry_run = True
    configuration = evaluate_chat.safe_report(args)
    assert configuration["scenarios_saved"] == 2 and configuration["scenarios_pending"] == 1
    assert len(preflights) == 1
    seen.clear()
    args.dry_run = False
    result = evaluate_chat.safe_report(args)
    assert seen == ["pubmed-doi"]
    assert result["complete"] and result["legacy_scenarios"] == 2
    assert result["cases"][1]["checks"] == {"no_abstract": None}
    assert result["summary"]["security"]["unverified"] == 1
    assert result["summary"]["reliability"]["outcomes"]["technical_error"] == 1


def test_live_evaluation_continues_after_timeout_and_stops_on_rate_limit(monkeypatch):
    config = SimpleNamespace()
    monkeypatch.setattr(chat_runtime, "isolated_settings", lambda *args, **kwargs: config)
    for name in ("TestClient", "Session"):
        monkeypatch.setattr(
            chat_runtime, name, lambda *_args, **_kwargs:
            nullcontext(SimpleNamespace(commit=lambda: None)),
        )
    monkeypatch.setattr(chat_runtime, "get_engine", lambda: None)
    monkeypatch.setattr(chat_runtime, "registered", lambda _client: uuid4())
    monkeypatch.setattr(chat_runtime, "seed_personal", lambda *_args: None)
    monkeypatch.setattr(chat_runtime, "seed_knowledge", lambda *_args, **_kwargs: None)

    def read_case(_client, _dataset, case, *_args, **_kwargs):
        return {
            **case, "error": "gigachat_unavailable",
            "llm_requests": [{"error": case["request_error"]}],
        }

    monkeypatch.setattr(chat_runtime, "evaluate_case", read_case)
    cases = [
        {"id": "first", "request_error": "gigachat_timeout"},
        {"id": "second", "request_error": "gigachat_http_429"},
        {"id": "third", "request_error": None},
    ]
    saved = []
    rows = evaluate(
        {"cases": cases, "publications": []}, "gigachat", live=True, live_retrieval=False,
        allow_synthetic_cloud=True, on_case=lambda _index, _total, row: saved.append(row["id"]),
    )
    assert saved == [row["id"] for row in rows] == ["first", "second"]


@pytest.mark.integration
def test_offline_personal_tools_expose_expected_fixture_content():
    if environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use the isolated RAG test stack with PostgreSQL")
    cases = {
        "potassium-first", "hemoglobin-first", "glucose-second", "personal-search",
        "compare-creatinine", "trend-creatinine",
    }
    dataset, _ = evaluate_chat.load_dataset()
    dataset["cases"] = [case for case in dataset["cases"] if case["id"] in cases]
    rows = evaluate(dataset, "ollama", live=False, live_retrieval=False)
    assert {row["id"] for row in rows} == cases
    for row in rows:
        first_tool = row["tools"][0]
        assert first_tool["error"] is None
        assert first_tool["expected_content"]["fact_hits"] == 1, row["id"]
        assert first_tool["expected_content"]["numeric_hits"] == (
            0 if row["id"] == "personal-search" else 1
        ), row["id"]
        assert any(
            request.get("input_expected_content", {}).get("fact_hits") == 1
            for request in row["llm_requests"]
        ), row["id"]


def test_dry_run_does_not_invoke_evaluation_or_mlflow(monkeypatch):
    settings = SimpleNamespace(
        generation_model="qwen2.5:7b-instruct", generation_context=4096,
        generation_output=1024, generation_timeout=180,
    )
    monkeypatch.setattr(evaluate_chat, "isolated_settings", lambda *_args, **_kwargs: settings)

    def unexpected(*_args, **_kwargs):
        pytest.fail("dry-run called evaluation or tracking")

    monkeypatch.setattr(evaluate_chat, "evaluate", unexpected)
    monkeypatch.setattr(evaluate_chat, "check_gigachat_oauth", unexpected)
    monkeypatch.setattr(evaluate_chat, "register", unexpected)
    result = evaluate_chat.safe_report(Namespace(
        dry_run=True, provider="ollama", mode="offline", retrieval="fixed",
        commit="6a1953f", mlflow_uri="http://localhost:5000", case=["cbc-reference"],
    ))
    assert result["state"] == "configuration_checked"
    assert result["scenarios_expected"] == 1 and result["selected_cases"] == ["cbc-reference"]
    assert "summary" not in result and "cases" not in result


@pytest.mark.parametrize(
    ("oauth_error", "probe_only"),
    [(None, True), ("oauth_transport", False), ("oauth_http_401", False)],
)
def test_gigachat_preflight_checks_oauth_before_evaluation(monkeypatch, oauth_error, probe_only):
    settings = Settings(llm_provider="gigachat", gigachat_credentials=SecretStr("fixture-key"))
    monkeypatch.setattr(evaluate_chat, "isolated_settings", lambda *args, **kwargs: settings)
    async_client = httpx.AsyncClient
    seen = []

    def oauth(request):
        seen.append(request.url.path)
        assert request.url.scheme == "https" and request.url.port == 9443
        assert request.headers["Authorization"] == "Basic fixture-key"
        if oauth_error == "oauth_transport":
            raise httpx.ConnectError("fixture-key must not escape", request=request)
        if oauth_error == "oauth_http_401":
            return httpx.Response(401, text="fixture-key must not escape")
        return httpx.Response(200, json={
            "access_token": "fixture-token", "expires_at": int((time() + 60) * 1000),
        })

    def client(**kwargs):
        assert kwargs["verify"].check_hostname and kwargs["verify"].verify_mode == CERT_REQUIRED
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return async_client(transport=httpx.MockTransport(oauth), **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("OAuth preflight must finish before running scenarios or writing to MLflow")

    monkeypatch.setattr(evaluate_chat.httpx, "AsyncClient", client)
    monkeypatch.setattr(evaluate_chat, "evaluate", unexpected)
    monkeypatch.setattr(evaluate_chat, "register", unexpected)
    args = Namespace(
        dry_run=False, preflight_only=probe_only, provider="gigachat", mode="live",
        retrieval="fixed", commit="6a1953f", mlflow_uri=None, allow_synthetic_cloud=True,
    )
    if oauth_error:
        with pytest.raises(ValueError, match=f"^{oauth_error}$"):
            evaluate_chat.safe_report(args)
    else:
        result = evaluate_chat.safe_report(args)
        assert result["state"] == "gigachat_oauth_checked"
        assert "fixture-key" not in repr(result) and "fixture-token" not in repr(result)
    assert seen == ["/api/v2/oauth"]


def test_gigachat_preflight_requires_local_ca(monkeypatch, tmp_path):
    settings = Settings(
        redis_password="synthetic", postgres_db="medical_ai_rag_test",
        postgres_user="synthetic_rag_user", rag_collection="medical_ai_personal_test",
        gigachat_credentials=SecretStr("fixture-key"),
    )
    path = tmp_path / "ca.crt"
    monkeypatch.setenv("RUN_RAG_INTEGRATION", "1")
    monkeypatch.setattr(chat_runtime, "get_settings", lambda: settings)
    settings = settings.model_copy(update={"gigachat_ca_bundle": str(path)})
    with pytest.raises(ValueError, match="^gigachat_ca_missing$"):
        chat_runtime.isolated_settings("gigachat", live=True, live_retrieval=False)
    path.write_text("invalid fixture PEM", encoding="utf-8")
    with pytest.raises(ValueError, match="^gigachat_ca_invalid$"):
        chat_runtime.isolated_settings("gigachat", live=True, live_retrieval=False)
    assert not chat_runtime.isolated_settings(
        "gigachat", live=False, live_retrieval=False,
    ).gigachat_ca_bundle


def test_evaluation_reports_llm_phase_and_safe_errors(capsys):
    async def failing_final(_messages):
        yield "SYNTHETIC-PRIVATE"
        raise GatewayError("gigachat_timeout", timeout_kind="connect")

    async def scenario():
        trace = chat_runtime.Trace()
        trace.round = 2
        model = SimpleNamespace(stream=failing_final)
        provider = chat_runtime.ObservedProvider(model, trace, fallback=False)
        with pytest.raises(GatewayError, match="timeout"):
            async for _ in provider.stream([{"role": "user", "content": "PRIVATE-CONTENT"}]):
                pass
        return trace.llm_requests[0]

    request = run(scenario())
    assert request["phase"] == "grounded_answer" and request["round"] == 2
    assert request["error"] == "gigachat_timeout" and request["received_text"]
    assert request["timeout_origin"] == "provider_request" and request["timeout_kind"] == "connect"
    assert request["context_bytes"] > 0 and request["ms"] >= request["first_event_ms"]
    assert "PRIVATE" not in repr(request)
    issues = chat_runtime.argument_issues(ToolCall("get_documents", {
        "documents": ["PRIVATE-VALUE"], "PRIVATE-FIELD": "sensitive",
    }))
    assert issues == [
        {"field": "documents", "type": "int_type"}, {"field": "other", "type": "extra_forbidden"},
    ]
    evaluate_chat.print_progress(7, 30, {
        "id": "cbc-reference", "error": "timeout", "wall_ms": 180000, "checks": {},
    })
    assert capsys.readouterr().out == "[7/30] cbc-reference status=timeout duration=180.00s\n"


def test_summary_counts_timeouts_outside_answer_quality():
    case = {"facts": ["HbA1c"], "required_sources": []}
    row = {
        "llm_backend": "live", "llm_calls": 1, "llm_ms": 5, "wall_ms": 6, "checks": {},
        "tools": [], "routing_ok": True, "mixed": False, "extra_pubmed": False,
        "context_error": False, "fallback": None,
    }
    summary = summarize([
        {**row, "error": None, "answer": score_answer(case, "HbA1c", [], set(), None)},
        {
            **row, "error": "timeout", "routing_ok": False,
            "llm_requests": [{"error": "timeout"}],
            "answer": score_answer(case, "", [], set(), "timeout"),
        },
    ], live=True)
    assert summary["answer_scenarios"] == 1 and summary["answers"]["facts"] == 1
    assert summary["reliability"]["timeout_scenarios"] == 1
    assert summary["reliability"]["outcomes"] == {"correct": 1, "technical_error": 1}
    assert summary["errors"] == {"timeout": 1}
    assert summary["agent"]["routing_accuracy"] == 0.5


@pytest.mark.integration
@pytest.mark.parametrize("leaks", [False, True])
def test_history_probe_rechecks_stored_source_without_model_citations(monkeypatch, leaks):
    if environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use the isolated RAG test database")
    dataset, _ = evaluate_chat.load_dataset()
    # A bare topic preserves the no-citation precondition independently of reference routing.
    dataset["cases"] = [
        {**row, "question": "HbA1c"} for row in dataset["cases"] if row["id"] == "history-recheck"
    ]
    dataset["documents"] = []
    dataset["corpus"] = [item for item in dataset["corpus"] if item["id"] == "a1c"]

    async def no_tool_reply(_messages, _tools):
        yield "HbA1c."

    monkeypatch.setattr(
        chat_runtime, "gateway", lambda *_args, **_kw: SimpleNamespace(tool_stream=no_tool_reply),
    )
    if leaks:
        def allow_stale_source(_db, _config, sources, **_kwargs):
            return {chat_runtime.medical_sources.public_citation_key(source) for source in sources}

        monkeypatch.setattr(chat_runtime.routes, "verified_public", allow_stale_source)
    (row,) = evaluate(dataset, "ollama", live=True, live_retrieval=False)
    assert row["tools"] == [] and row["sources"] == []
    assert row["answer"]["status"] == "partial" and row["error"] is None
    assert row["history_probe"] == ("source_still_visible" if leaks else "passed")
    assert row["checks"] == {"history_recheck": not leaks}


@pytest.mark.integration
def test_live_evaluation_matches_gigachat_chat_payload(monkeypatch):
    if environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use the isolated RAG test database")
    dataset, _ = evaluate_chat.load_dataset()
    case = next(row for row in dataset["cases"] if row["id"] == "a1c-period")
    dataset = {
        **dataset, "cases": [case], "documents": [],
        "corpus": [item for item in dataset["corpus"] if item["id"] == "a1c"],
    }
    settings = chat_runtime.get_settings().model_copy(update={
        "llm_provider": "ollama", "chat_enabled": True, "rag_enabled": True,
        "gigachat_credentials": SecretStr("fixture-key"), "gigachat_ca_bundle": where(),
        "llm_gigachat_context_tokens": 20000, "llm_gigachat_max_tokens": 1700,
        "llm_gigachat_timeout_seconds": 40,
        "chat_gigachat_history_chars": 4500, "chat_gigachat_context_chars": 7500,
    })
    monkeypatch.setattr(chat_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_runtime.routes, "get_settings", lambda: settings)
    payloads, limits = [], []

    def respond(request):
        if request.url.path == "/api/v2/oauth":
            return httpx.Response(200, json={
                "access_token": "fixture-token", "expires_at": int(time()) + 1800,
            })
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        payloads.append(payload)
        reply = (
            "HbA1c отражает средний уровень глюкозы за последние 2–3 месяца [S1]."
            if payload["function_call"] == "none"
            else "HbA1c отражает средний уровень глюкозы за последние 18 месяцев."
        )
        event = {"choices": [{
            "delta": {"content": reply}, "finish_reason": "stop",
        }]}
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

    adapter = gigachat.GigaChat

    def mocked_transport(config):
        limits.append((
            config.generation_context, config.generation_output, config.generation_timeout,
            config.generation_history_chars, config.generation_tool_chars,
        ))
        return adapter(config, httpx.MockTransport(respond))

    monkeypatch.setattr(gigachat, "GigaChat", mocked_transport)
    # Keep the regular chat's lookup local and deterministic without seeding a vector store.
    with (
        monkeypatch.context() as ordinary_chat,
        TestClient(chat_runtime.app, client=(uuid4().hex, 50000)) as client,
    ):
        ordinary_chat.setattr(chat_runtime.agent, "search_knowledge", lambda *_args: [])
        chat_runtime.registered(client)
        created = client.post("/chat", headers=chat_runtime.headers(client))
        created.raise_for_status()
        response = client.post(
            f"/chat/{created.json()['id']}/messages", headers=chat_runtime.headers(client),
            json={
                "content": case["question"], "llm_provider": "gigachat",
                "external_consent": "gigachat",
            },
        )
        response.raise_for_status()
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1] == {"type": "done", "sources": []}
        assert "нет подходящих опубликованных фрагментов" in events[-2]["text"]
        assert "18 месяцев" not in events[-2]["text"]

    (row,) = evaluate(
        dataset, "gigachat", live=True, live_retrieval=False, allow_synthetic_cloud=True,
    )
    ordinary, evaluation, grounded = payloads
    assert ordinary == evaluation
    assert limits == [(20000, 1700, 40, 4500, 7500)] * 2
    assert ordinary["function_call"] == "auto"
    assert ordinary["model"] == "GigaChat-3-Ultra" and ordinary["max_tokens"] == 1700
    assert ordinary["messages"] == [
        {"role": "system", "content": chat_runtime.routes.AGENT_SYSTEM},
        {"role": "user", "content": chat_runtime.agent.encode({
            "question": case["question"], "active_documents": [],
        })},
    ]
    offered = [function["name"] for function in ordinary["functions"]]
    assert offered == [
        "get_documents", "search_documents", "analyze_measurements", "search_medical_knowledge",
    ]
    call, answer = row["llm_requests"]
    assert call["offered_tools"] == offered and call["finish_reason"] == "stop"
    assert call["received_text"] and call["tools"] == [] and call["error"] is None
    assert answer["phase"] == "grounded_answer" and answer["offered_tools"] == []
    assert grounded["function_call"] == "none" and "functions" not in grounded
    fragments = json.loads(grounded["messages"][-1]["content"])["public_evidence"]
    assert fragments[0]["source_id"] == "S1" and "2 to 3 months" in fragments[0]["text"]
    assert row["routing_ok"] and row["missing_tools"] == []
    assert [tool["name"] for tool in row["tools"]] == ["search_medical_knowledge"]
    assert row["answer"]["status"] == "correct"
    assert row["answer"]["required_source_hits"] == 1 and row["error"] is None
    assert row["retrieval"]["first_relevant_rank"] == 1


def test_offline_qdrant_accepts_only_the_synthetic_stand_and_bge_m3(monkeypatch):
    settings = Settings(
        redis_password="synthetic", postgres_db="medical_ai_rag_test",
        postgres_user="synthetic_rag_user", rag_collection="medical_ai_personal_test",
        rag_embedding_model="bge-m3:567m",
    )
    monkeypatch.setenv("RUN_RAG_INTEGRATION", "1")
    monkeypatch.setattr(chat_runtime, "get_settings", lambda: settings)
    selected = chat_runtime.isolated_settings("ollama", live=False, live_retrieval=True)
    assert selected.rag_embedding_model == "bge-m3:567m"

    settings = settings.model_copy(update={"rag_embedding_model": "different-model"})
    with pytest.raises(ValueError, match="offline_qdrant_requires_bge_m3"):
        chat_runtime.isolated_settings("ollama", live=False, live_retrieval=True)
    settings = settings.model_copy(update={"postgres_db": "medical_ai"})
    with pytest.raises(ValueError, match="isolated_rag_test_environment_required"):
        chat_runtime.isolated_settings("ollama", live=False, live_retrieval=True)


@pytest.mark.integration
def test_offline_qdrant_measures_retrieval_without_live_generation_or_main_collection(monkeypatch):
    if environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Use the isolated RAG test stack with Qdrant and BGE-M3")
    settings = chat_runtime.isolated_settings("ollama", live=False, live_retrieval=True)
    dataset, digest = evaluate_chat.load_dataset()
    source = next(row for row in dataset["corpus"] if row["id"] == "a1c")
    case_ids = {"a1c-period", "pubmed-pmid", "draft-hidden", "stale-hidden"}
    included_sources = [row for row in dataset["corpus"] if row["id"] in {"a1c", "draft", "stale"}]
    dataset = {**dataset, "corpus": included_sources, "documents": [], "cases": [
        row for row in dataset["cases"] if row["id"] in case_ids
    ]}

    # A published source in the same test database, outside the current evaluation.
    # It must not be counted as part of the generation scope sent to Qdrant.
    with TestClient(chat_runtime.app, client=(uuid4().hex, 50000)) as client:
        owner = chat_runtime.registered(client)
    with Session(chat_runtime.get_engine()) as db:
        seed_knowledge(
            db, {"corpus": [{**source, "id": "out-of-run"}]}, owner, settings, Corpus(),
            live_retrieval=False,
        )
        db.commit()

    vector_calls = []
    embedding_calls = []
    fixed_pubmed_calls = []
    request = JsonHttp.request
    fixed_pubmed = chat_runtime.pubmed_client

    def observe(self, method, path, body=None):
        if self.url == settings.rag_qdrant_url:
            assert path.removeprefix("/collections/").split("/")[0] == "medical_ai_evaluation_v1"
            vector_calls.append((method, path))
        else:
            assert self.url == settings.rag_ollama_url and path in {"/api/embed", "/api/tags"}
            if path == "/api/embed":
                assert body["model"] == settings.rag_embedding_model
                embedding_calls.append(path)
        return request(self, method, path, body)

    def replay(*args, **kwargs):
        fixed_pubmed_calls.append(True)
        return fixed_pubmed(*args, **kwargs)

    def unexpected(*_args, **_kwargs):
        pytest.fail("offline Qdrant called a real generation gateway")

    monkeypatch.setattr(JsonHttp, "request", observe)
    monkeypatch.setattr(chat_runtime, "gateway", unexpected)
    monkeypatch.setattr(chat_runtime, "pubmed_client", replay)
    monkeypatch.setattr(evaluate_chat, "load_dataset", lambda: (dataset, digest))
    report = evaluate_chat.safe_report(Namespace(
        dry_run=False, provider="ollama", mode="offline", retrieval="qdrant",
        commit="6a1953f", mlflow_uri=None, compare=None, allow_synthetic_cloud=False,
    ))
    assert report["complete"] and report["retrieval_backend"] == "qdrant"
    assert report["mode"] == "offline" and report["pubmed_backend"] == "fixed"
    assert all(row["llm_backend"] == "fixture" for row in report["cases"])
    assert embedding_calls and any(path.endswith("/points/query") for _, path in vector_calls)
    assert fixed_pubmed_calls
    retrieval = report["summary"]["retrieval"]
    assert retrieval["scored"] == 1 and retrieval["mean_ms"] > 0
    assert retrieval["recall_at_3"] == retrieval["recall_at_5"] == retrieval["mrr"] == 1
    assert retrieval["precision_at_3"] == 1 / 3 and retrieval["precision_at_5"] == 1 / 5
    assert retrieval["hidden_source_queries"] == 2
    assert retrieval["hidden_source_queries_with_irrelevant_results"] == 2
    for hidden in ("draft", "stale"):
        row = next(row for row in report["cases"] if row["id"] == f"{hidden}-hidden")
        assert row["checks"] == {f"no_{hidden}": True}
        assert row["retrieval_quality"]["irrelevant_sources"] == ["a1c"]


def test_live_cloud_cannot_start_without_synthetic_consent():
    with pytest.raises(ValueError, match="synthetic_cloud_consent_required"):
        evaluate({}, "gigachat", live=True, live_retrieval=False)


def test_comparison_rejects_offline_or_incomplete_reports():
    base = {
        "mode": "live", "dataset_hash": "same", "commit": "same",
        "retrieval_backend": "fixed", "retrieval_config": {},
        "complete": True, "cases": [],
    }
    local = {**base, "provider": "ollama"}
    cloud = {**base, "provider": "gigachat"}
    assert compare_reports(local, {**cloud, "mode": "offline"})["status"] == "incompatible_runs"
    assert compare_reports(local, {**cloud, "complete": False})["status"] == "incompatible_runs"
    assert compare_reports(local, cloud)["status"] == "no_comparable_cases"


def test_tracking_uses_separate_experiment_and_local_artifact_proxy(monkeypatch):
    requests = []
    real_client = httpx.Client

    def mock(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/experiments/get-by-name"):
            assert request.url.params["experiment_name"] == "medical-ai-evaluation"
            return httpx.Response(200, json={"experiment": {"experiment_id": "10"}})
        if path.endswith("/runs/create"):
            return httpx.Response(200, json={"run": {"info": {
                "run_id": "synthetic", "artifact_uri": "mlflow-artifacts:/10/test/artifacts",
            }}})
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(mock),
    ))
    run_id = register(
        "http://localhost:5000", {"evaluation": {"scenarios": 30}}, {"mode": "offline"},
        experiment_name="medical-ai-evaluation", artifact_json=b'{"cases":[]}',
    )
    artifact, = [request for request in requests if request.method == "PUT"]
    assert run_id == "synthetic"
    path = "/api/2.0/mlflow-artifacts/artifacts/10/test/artifacts/evaluation.json"
    assert artifact.url.path == path
    assert artifact.content == b'{"cases":[]}'
