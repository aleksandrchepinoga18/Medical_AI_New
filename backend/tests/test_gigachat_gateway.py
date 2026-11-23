import asyncio
import json
from time import time

import httpx
import pytest

from app.chat.agent import tool_definitions
from app.chat.routes import AGENT_SYSTEM
from app.core.config import Settings
from app.llm.gateway import Gateway, GatewayError, ToolCall, gateway
from app.llm.gigachat import GigaChat

TOOLS = [{"type": "function", "function": {
    "name": "get_test_code",
    "description": "Read a synthetic code",
    "parameters": {"type": "object", "properties": {"number": {"type": "integer"}}},
}}]


def settings():
    return Settings(
        redis_password="synthetic", llm_provider="gigachat", gigachat_credentials="synthetic",
    )


def token(value="mock-token"):
    return httpx.Response(200, json={"access_token": value, "expires_at": int(time()) + 1800})


def event(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def sse(*events, done=True):
    lines = [f"data: {json.dumps(item)}\n\n" for item in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, content="".join(lines), headers={"content-type": "text/event-stream"},
    )


def test_cloud_factory_requires_config_and_explicit_opt_in():
    with pytest.raises(GatewayError, match="provider_disabled"):
        gateway(settings())
    with pytest.raises(GatewayError, match="provider_disabled"):
        gateway(Settings(redis_password="synthetic"), provider="gigachat", external_consent=True)
    assert isinstance(gateway(settings(), external_consent=True).provider, GigaChat)
    local_default = settings().model_copy(update={"llm_provider": "ollama"})
    with pytest.raises(GatewayError, match="provider_disabled"):
        gateway(local_default, provider="gigachat")
    assert isinstance(
        gateway(local_default, provider="gigachat", external_consent=True).provider, GigaChat,
    )


def test_function_calls_sse_round_trip_and_oauth_reuse():
    requests = []

    def respond(request):
        assert request.url.scheme == "https"
        if request.url.path.endswith("/oauth"):
            assert request.url.host == "ngw.devices.sberbank.ru"
            assert request.headers["Authorization"] == "Basic synthetic"
            assert request.headers["RqUID"]
            assert request.content == b"scope=GIGACHAT_API_PERS"
            requests.append("oauth")
            return token("round-trip-token")
        assert str(request.url) == "https://api.giga.chat/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer round-trip-token"
        payload = json.loads(request.content)
        assert payload["model"] == "GigaChat-3-Ultra" and payload["stream"] is True
        assert payload["max_tokens"] == 2048 and "options" not in payload
        assert payload["function_call"] == "auto" and "response_format" not in payload
        assert payload["functions"] == [TOOLS[0]["function"]]
        requests.append("chat")
        if len(requests) == 2:
            return sse(event({
                "function_call": {"name": "get_test_code", "arguments": {"number": 6}},
                "functions_state_id": "synthetic-state",
            }, "function_call"))
        assert payload["messages"][-2:] == [
            {
                "role": "assistant", "content": "",
                "function_call": {"name": "get_test_code", "arguments": {"number": 6}},
                "functions_state_id": "synthetic-state",
            },
            {"role": "function", "name": "get_test_code", "content": '{"code":68427}'},
        ]
        return sse(event({"content": "Код "}), event({"content": "68427"}, "stop"))

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))

    async def dialogue():
        messages = [{"role": "user", "content": "Синтетический запрос"}]
        (call,) = [part async for part in model.tool_stream(messages, TOOLS)]
        assert call == ToolCall("get_test_code", {"number": 6}, "synthetic-state")
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [call.message()]},
            {"role": "tool", "tool_name": "get_test_code", "content": '{"code":68427}'},
        ]
        assert [part async for part in model.tool_stream(messages, TOOLS)] == ["Код ", "68427"]

    asyncio.run(dialogue())
    assert requests == ["oauth", "chat", "chat"]


def test_sse_round_trip_with_the_real_agent_functions():
    tools = tool_definitions()
    original = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    requests = []

    def respond(request):
        if request.url.path.endswith("/oauth"):
            return token()
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["function_call"] == "auto" and payload["stream"] is True
        assert payload["max_tokens"] == 2048 and payload["temperature"] == 0.2
        assert "options" not in payload and "response_format" not in payload
        functions = {item["name"]: item["parameters"] for item in payload["functions"]}
        assert set(functions) == {
            "get_documents", "search_documents", "analyze_measurements",
            "search_medical_knowledge", "search_pubmed",
        }
        assert set(functions["get_documents"]["properties"]) == {"documents", "after"}
        assert set(functions["search_documents"]["properties"]) == {"query", "documents"}
        assert set(functions["analyze_measurements"]["properties"]) == {
            "operation", "documents", "indicators",
        }
        assert functions["search_documents"]["required"] == ["query"]
        assert set(functions["analyze_measurements"]["required"]) == {"operation", "documents"}
        assert "required" not in functions["get_documents"]
        for name in ("search_medical_knowledge", "search_pubmed"):
            assert functions[name]["required"] == ["query"]
            assert functions[name]["properties"]["query"]["type"] == "string"
        for name, parameters in functions.items():
            assert parameters["type"] == "object"
            assert set(parameters) <= {"type", "properties", "required"}
            if "documents" in parameters["properties"]:
                assert parameters["properties"]["documents"]["items"]["type"] == "integer"
            for field, property_schema in parameters["properties"].items():
                assert set(property_schema) <= {"type", "description", "items", "enum"}
                source = original[name]["properties"][field]
                assert source["description"].strip()
                assert property_schema["description"] == source["description"]
                if "items" in source:
                    assert source["items"]["description"].strip()
                    assert property_schema["items"] == {
                        "type": source["items"]["type"],
                        "description": source["items"]["description"],
                    }
        analysis = functions["analyze_measurements"]["properties"]
        assert analysis["indicators"]["items"]["type"] == "string"
        assert set(analysis["operation"]["enum"]) == {
            "results", "trend", "comparison", "deviations",
        }
        assert all(
            value in analysis["operation"]["description"] for value in analysis["operation"]["enum"]
        )
        assert "maximum" in tools[0]["function"]["parameters"]["properties"]["documents"]["items"]

        assert payload["messages"][0] == {"role": "system", "content": AGENT_SYSTEM}
        roles = [message["role"] for message in payload["messages"]]
        if len(requests) == 1:
            assert roles == ["system", "user"]
            return sse(event({
                "function_call": {"name": "get_documents", "arguments": {"documents": [6]}},
                "functions_state_id": "synthetic-agent-state",
            }, "function_call"))
        assert roles == ["system", "user", "assistant", "function"]
        assert payload["messages"][-2]["functions_state_id"] == "synthetic-agent-state"
        assert payload["messages"][-1] == {
            "role": "function", "name": "get_documents", "content": '{"synthetic":true}',
        }
        return sse(event({"content": "Данные приняты."}, "stop"))

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))

    async def dialogue():
        messages = [
            {"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": json.dumps({
                "question": "Объясни документ №6", "requested_documents": [6],
            }, ensure_ascii=False)},
        ]
        (call,) = [part async for part in model.tool_stream(messages, tools)]
        assert call == ToolCall("get_documents", {"documents": [6]}, "synthetic-agent-state")
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [call.message()]},
            {"role": "tool", "tool_name": call.name, "content": '{"synthetic":true}'},
        ])
        assert [part async for part in model.tool_stream(messages, tools)] == ["Данные приняты."]

    asyncio.run(dialogue())
    assert len(requests) == 2


def test_422_diagnostic_does_not_log_echoed_values(caplog):
    caplog.set_level("WARNING", logger="uvicorn.error.gigachat")
    secret_input = "synthetic private clinical text"
    reflected_token = "synthetic reflected credential"

    def respond(request):
        if request.url.path.endswith("/oauth"):
            return token()
        return httpx.Response(422, json={
            "message": f"Type properties.documents.items.type is wrong: {reflected_token}",
            "detail": [{"loc": ["body", "functions", 0], "input": secret_input}],
        })

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))

    async def fetch():
        return [part async for part in model.tool_stream(
            [{"role": "user", "content": secret_input}], tool_definitions(),
        )]

    with pytest.raises(GatewayError, match="^gigachat_http_422$"):
        asyncio.run(fetch())
    assert "gigachat_validation_rejected" in caplog.text
    assert "reason=function_schema messages=1 functions=5 function_call=auto" in caplog.text
    assert secret_input not in caplog.text and reflected_token not in caplog.text


def test_rejects_system_message_out_of_order_before_http():
    def respond(_request):
        pytest.fail("Malformed message order reached HTTP")

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))
    with pytest.raises(GatewayError, match="^gigachat_invalid_message_order$"):
        asyncio.run(model.generate([
            {"role": "user", "content": "synthetic"},
            {"role": "system", "content": AGENT_SYSTEM},
        ]))


def test_text_stream_refreshes_token_once_on_401():
    attempts = []

    def respond(request):
        if request.url.path.endswith("/oauth"):
            attempts.append("oauth")
            return token(str(len(attempts)))
        attempts.append("chat")
        if len(attempts) == 2:
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer 3"
        payload = json.loads(request.content)
        assert payload["function_call"] == "none" and "functions" not in payload
        return sse(event({"content": "ответ"}, "stop"))

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))
    assert asyncio.run(model.generate([{"role": "user", "content": "synthetic"}])) == "ответ"
    assert attempts == ["oauth", "chat", "oauth", "chat"]


@pytest.mark.parametrize(("reply", "code"), [
    (sse(event({"content": "partial"}, "stop"), done=False), "gigachat_interrupted"),
    (sse(event({"content": ""}, "blacklist")), "gigachat_filtered"),
    (sse(event({"function_call": {"name": "x", "arguments": {}}}, "function_call")),
     "gigachat_invalid_tool_state"),
    (httpx.Response(307, headers={"location": "https://example.org/"}), "gigachat_http_307"),
])
def test_gateway_rejects_incomplete_or_untrusted_streams(reply, code):
    def respond(request):
        if request.url.path.endswith("/oauth"):
            return token()
        assert request.url.host == "api.giga.chat"
        return reply

    model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))

    async def fetch():
        return [part async for part in model.tool_stream([], TOOLS)]

    with pytest.raises(GatewayError, match=f"^{code}$"):
        asyncio.run(fetch())


def test_cancellation_stops_cloud_request():
    async def scenario():
        started = asyncio.Event()

        async def respond(request):
            if request.url.path.endswith("/oauth"):
                return token()
            started.set()
            await asyncio.Event().wait()

        model = Gateway(GigaChat(settings(), httpx.MockTransport(respond)))
        task = asyncio.create_task(model.generate([]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
