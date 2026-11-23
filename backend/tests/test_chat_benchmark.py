from argparse import Namespace
from asyncio import run
from contextlib import nullcontext
from os import environ
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
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


def test_dry_run_does_not_invoke_evaluation_or_mlflow(monkeypatch):
    settings = SimpleNamespace(
        generation_model="qwen2.5:7b-instruct", generation_context=4096,
        generation_output=1024, generation_timeout=180,
    )
    monkeypatch.setattr(evaluate_chat, "isolated_settings", lambda *_args, **_kwargs: settings)

    def unexpected(*_args, **_kwargs):
        pytest.fail("dry-run called evaluation or tracking")

    monkeypatch.setattr(evaluate_chat, "evaluate", unexpected)
    monkeypatch.setattr(evaluate_chat, "register", unexpected)
    result = evaluate_chat.safe_report(Namespace(
        dry_run=True, provider="ollama", mode="offline", retrieval="fixed",
        commit="6a1953f", mlflow_uri="http://localhost:5000", case=["cbc-reference"],
    ))
    assert result["state"] == "configuration_checked"
    assert result["scenarios_expected"] == 1 and result["selected_cases"] == ["cbc-reference"]
    assert "summary" not in result and "cases" not in result


def test_evaluation_reports_llm_phase_and_safe_errors(capsys):
    async def failing_final(_messages):
        yield "SYNTHETIC-PRIVATE"
        raise GatewayError("timeout")

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
    assert request["error"] == "timeout" and request["received_text"]
    assert request["timeout_origin"] == "provider_request"
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
    dataset["cases"] = [row for row in dataset["cases"] if row["id"] == "history-recheck"]
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
