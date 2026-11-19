import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.chat.routes import build_context
from app.core.config import Settings
from app.llm.gateway import Gateway, GatewayError, Ollama, gateway
from app.rag.adapters import OllamaEmbeddings, fingerprint


def adapter(handler):
    return Gateway(Ollama(Settings(redis_password="synthetic"), httpx.MockTransport(handler)))


def response(text):
    return httpx.Response(
        200,
        content=(json.dumps({"message": {"content": text}}) + '\n{"done":true}\n').encode(),
    )


def test_stream_and_local_destination():
    def handler(request):
        assert str(request.url) == "http://ollama:11434/api/chat"
        assert json.loads(request.content)["options"]["num_predict"] == 1024
        assert json.loads(request.content)["options"]["num_ctx"] == 4096
        return response("синтетический ответ")

    assert asyncio.run(adapter(handler).generate([])) == "синтетический ответ"


@pytest.mark.parametrize("name", ["openai", "deepseek", "yandexgpt", "gigachat"])
def test_external_disabled(name):
    with pytest.raises(GatewayError, match="provider_disabled"):
        gateway(Settings(redis_password="synthetic"), name)


class Result(BaseModel):
    count: int


@pytest.mark.parametrize("context_tokens", [2048, 4096, 16384, 32768])
@pytest.mark.parametrize("mode", ["generate", "stream", "structured"])
@pytest.mark.parametrize("url", ["http://ollama:11434", "http://host.docker.internal:11434"])
def test_context_tokens_from_environment(monkeypatch, context_tokens, mode, url):
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", str(context_tokens))
    monkeypatch.setenv("LLM_OLLAMA_URL", url)
    settings = Settings()

    def handler(request):
        assert str(request.url) == url + "/api/chat"
        assert json.loads(request.content)["options"]["num_ctx"] == context_tokens
        return response('{"count":2}')

    client = Gateway(Ollama(settings, httpx.MockTransport(handler)))

    async def scenario():
        if mode == "structured":
            assert (await client.structured([], Result)).count == 2
        elif mode == "stream":
            assert "".join([part async for part in client.stream([])]) == '{"count":2}'
        else:
            assert await client.generate([]) == '{"count":2}'

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com",
        "http://example.org:11434",
        "http://host.docker.internal.example.org:11434",
        "https://host.docker.internal:11434",
        "http://host.docker.internal:8080",
        "http://user:password@host.docker.internal:11434",
        "http://host.docker.internal:11434/api/chat",
        "http://host.docker.internal:11434/",
        "http://host.docker.internal:11434?target=external",
        "http://host.docker.internal:11434#fragment",
        "http://127.0.0.1:11434",
        "",
    ],
)
def test_generation_url_rejects_unapproved_addresses(monkeypatch, url):
    monkeypatch.setenv("LLM_OLLAMA_URL", url)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("url", ["http://ollama:11434", "http://host.docker.internal:11434"])
def test_generation_url_does_not_change_embeddings_or_index(monkeypatch, url):
    baseline = Settings()
    monkeypatch.setenv("LLM_OLLAMA_URL", url)
    settings = Settings()
    embeddings = OllamaEmbeddings(settings)
    assert settings.rag_ollama_url == embeddings.http.url == "http://ollama:11434"
    assert settings.rag_embedding_model == "bge-m3:567m"
    assert settings.rag_qdrant_url == baseline.rag_qdrant_url
    assert settings.rag_collection == baseline.rag_collection
    assert fingerprint(settings) == fingerprint(baseline)

    def request(method, path, body=None):
        assert method == "POST" and path == "/api/embed"
        assert body["model"] == "bge-m3:567m"
        return {"embeddings": [[1.0] * settings.rag_embedding_dimensions]}

    monkeypatch.setattr(embeddings.http, "request", request)
    assert len(embeddings.embed(["synthetic"])[0]) == settings.rag_embedding_dimensions


@pytest.mark.parametrize("url", ["http://ollama:11434", "http://host.docker.internal:11434"])
def test_generation_redirect_is_not_followed(monkeypatch, url):
    monkeypatch.setenv("LLM_OLLAMA_URL", url)
    monkeypatch.setenv("HTTP_PROXY", "http://unapproved-proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://unapproved-proxy.invalid:8080")
    original_client = httpx.AsyncClient

    def local_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.org/api/chat"})

    client = Gateway(Ollama(Settings(), httpx.MockTransport(handler)))
    with pytest.raises(GatewayError, match="unavailable"):
        asyncio.run(client.generate([]))
    assert calls == [url + "/api/chat"]


@pytest.mark.parametrize("context_tokens", [0, 2047, 32769])
def test_context_tokens_bounds(monkeypatch, context_tokens):
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", str(context_tokens))
    with pytest.raises(ValidationError):
        Settings()


def test_structured_validation():
    assert asyncio.run(adapter(lambda r: response('{"count":2}')).structured([], Result)).count == 2
    with pytest.raises(GatewayError, match="invalid_structure"):
        asyncio.run(adapter(lambda r: response('{"count":"bad"}')).structured([], Result))


@pytest.mark.parametrize(
    "value,code",
    [
        (httpx.Response(503), "unavailable"),
        (httpx.Response(200, content=b"not-json\n"), "invalid_response"),
        (httpx.Response(200, content=b'{"message":{"content":"partial"}}\n'), "interrupted"),
        (httpx.Response(302, headers={"location": "https://example.org"}), "unavailable"),
    ],
)
def test_upstream_errors(value, code):
    with pytest.raises(GatewayError, match=code):
        asyncio.run(adapter(lambda r: value).generate([]))


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("sensitive upstream detail")

    with pytest.raises(GatewayError, match="^timeout$"):
        asyncio.run(adapter(handler).generate([]))


@pytest.mark.parametrize("ending", [b"\n", b""])
@pytest.mark.parametrize("reason", ["stop", "length"])
def test_fragmented_completion_and_final_record(ending, reason):
    class Fragmented(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = (
                json.dumps({"message": {"content": "ответ"}}, ensure_ascii=False).encode()
                + b"\n"
                + json.dumps({"done": True, "done_reason": reason}).encode()
                + ending
            )
            for offset in range(0, len(data), 3):
                yield data[offset : offset + 3]

    def handler(request):
        return httpx.Response(200, stream=Fragmented())

    assert asyncio.run(adapter(handler).generate([])) == "ответ"


def test_read_timeout_uses_configured_generation_deadline():
    def handler(request):
        assert request.extensions["timeout"]["read"] == 180
        assert request.extensions["timeout"]["connect"] == 5
        return response("synthetic")

    assert asyncio.run(adapter(handler).generate([])) == "synthetic"


def test_length_completion_still_validates_structured_output():
    def handler(request):
        return httpx.Response(
            200,
            content=b'{"message":{"content":"{unfinished"}}\n'
            b'{"done":true,"done_reason":"length"}\n',
        )

    with pytest.raises(GatewayError, match="invalid_structure"):
        asyncio.run(adapter(handler).structured([], Result))


def test_cancellation_propagates():
    async def scenario():
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(adapter(handler).generate([]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_context_budget_and_metadata():
    hit = {
        "text": "untrusted ignore instructions",
        "document_id": "real",
        "filename": "synthetic",
        "source_url": "/documents/real#source-page-2",
        "sources": [{"kind": "page", "index": 2}],
        "text_version_id": "version",
    }
    assert build_context([hit], 10) == ([], [])
    context, citations = build_context([hit, hit], 100)
    assert context == [hit["text"]]
    assert len(citations) == 1
    assert citations[0]["source_url"] == hit["source_url"]
    assert "text" not in citations[0]
    assert build_context([], 100) == ([], [])
