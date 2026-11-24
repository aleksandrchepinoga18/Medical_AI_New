"""GigaChat v1 SSE adapter for the common Gateway and tool loop."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from time import perf_counter
from typing import Any

import httpx

from app.core.config import Settings
from app.documents.gigachat import API, GigaChatExtraction, gigachat_tls_context
from app.llm.gateway import (
    GatewayError,
    ToolCall,
    _StructuredOutputLimit,
    generation_log_id,
    http_timeout_kind,
    response_lines,
)

logger = logging.getLogger("uvicorn.error").getChild("gigachat")
CONNECT_TIMEOUT_SECONDS = 5


def function_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """Render the function wire schema. ToolArguments still validates all constraints."""
    kind = schema.get("type")
    if kind not in ("object", "array", "string", "integer", "number", "boolean"):
        raise GatewayError("gigachat_unsupported_function_schema")
    parameters = {key: schema[key] for key in ("type", "description", "enum") if key in schema}
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or any(
            not isinstance(value, dict) for value in properties.values()
        ):
            raise GatewayError("gigachat_unsupported_function_schema")
        parameters["properties"] = {
            name: function_parameters(value) for name, value in properties.items()
        }
        if "required" in schema:
            parameters["required"] = list(schema["required"])
    if kind == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise GatewayError("gigachat_unsupported_function_schema")
        parameters["items"] = function_parameters(items)
    return parameters


def gigachat_functions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    functions = [tool["function"] for tool in tools]
    return [
        {
            "name": function["name"], "description": function["description"],
            "parameters": function_parameters(function["parameters"]),
        }
        for function in functions
    ]


def validation_reason(body: object) -> str:
    """Classify a rejection without copying any upstream values into the log."""
    if not isinstance(body, dict):
        return "unknown"
    detail = body.get("detail")
    messages = [
        value.casefold() for value in (body.get("message"), detail) if isinstance(value, str)
    ]
    errors = body.get("errors")
    if isinstance(errors, list):
        for error in errors:
            description = error.get("description") if isinstance(error, dict) else None
            if isinstance(description, str):
                messages.append(description.casefold())
    categories = (
        ("system_message_order", ("system message must be the first", "multiple system messages")),
        ("model_functions_unsupported", ("model does not support functions",)),
        ("function_selection", ("explicit_call", "function name must exist")),
        ("function_state", ("functions_state_id",)),
        ("generation_parameters", ("max_tokens", "temperature", "top_p")),
        ("function_schema", (
            "schema", "properties.", "field 'properties", "function parameters", "functions",
            "function is invalid", "property ",
        )),
    )
    for reason, markers in categories:
        if any(marker in message for message in messages for marker in markers):
            if reason == "function_schema":
                for keyword in (
                    "additionalproperties", "maximum", "minimum", "maxitems", "minitems",
                    "maxlength", "minlength", "title", "default",
                ):
                    if any(keyword in message for message in messages):
                        return f"function_schema_{keyword}"
            return reason
    if isinstance(detail, list):
        for item in detail:
            path = item.get("loc") if isinstance(item, dict) else None
            if not isinstance(path, list):
                continue
            if "functions" in path:
                return "function_schema"
            if "functions_state_id" in path:
                return "function_state"
            if "messages" in path:
                return "messages"
            if any(field in path for field in ("max_tokens", "temperature", "top_p")):
                return "generation_parameters"
    return "unknown"


async def read_validation_reason(response: httpx.Response) -> str:
    body = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 8192:
                    return "unknown"
                body.extend(chunk)
        return validation_reason(json.loads(body))
    except (ValueError, TimeoutError, httpx.HTTPError):
        return "unknown"


def gigachat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "system" and index != 0:
            raise GatewayError("gigachat_invalid_message_order")
        item = {"role": role, "content": message["content"]}
        if role == "tool":
            item.update(role="function", name=message["tool_name"])
        elif calls := message.get("tool_calls"):
            # The v1 protocol generates one function call per assistant message.
            if len(calls) != 1 or not calls[0].get("state_id"):
                raise GatewayError("gigachat_invalid_tool_state")
            item.update(
                function_call=calls[0]["function"], functions_state_id=calls[0]["state_id"],
            )
        converted.append(item)
    return converted


class GigaChat:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.token = ""
        self.expires_at = 0.0

    async def stream(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        async with aclosing(self.events(messages, schema=schema)) as events:
            async for event in events:
                if not isinstance(event, str):
                    raise GatewayError("unexpected_tool_call")
                yield event

    def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[str | ToolCall, None]:
        return self.events(messages, tools=tools)

    async def events(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str | ToolCall, None]:
        settings = self.settings
        if not settings.gigachat_credentials.get_secret_value().strip():
            raise GatewayError("gigachat_not_configured")
        timeout = settings.llm_gigachat_timeout_seconds
        payload: dict[str, Any] = {
            "model": settings.gigachat_model, "messages": gigachat_messages(messages),
            "stream": True, "max_tokens": settings.llm_gigachat_max_tokens,
            "temperature": 0.2, "function_call": "none",
        }
        if tools:
            payload.update(function_call="auto", functions=gigachat_functions(tools))
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "schema": schema, "strict": True}
        try:
            verify = gigachat_tls_context(settings.gigachat_ca_bundle)
        except OSError:
            raise GatewayError("gigachat_tls_config") from None
        started = perf_counter()
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    verify=verify, transport=self.transport, trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_SECONDS),
                ) as client:
                    # Use the existing OAuth implementation without uploading a file.
                    auth = GigaChatExtraction(
                        client, settings.gigachat_credentials, settings.gigachat_model,
                        "schema", scope=settings.gigachat_scope,
                    )
                    auth.access_token, auth.expires_at = self.token, self.expires_at
                    for attempt in range(2):
                        headers = await auth.headers(timeout=timeout)
                        self.token, self.expires_at = auth.access_token, auth.expires_at
                        headers["Accept"] = "text/event-stream"
                        logger.info(
                            "gigachat_request generation=%s provider=gigachat model=%s messages=%d",
                            generation_log_id.get(), settings.gigachat_model, len(messages),
                        )
                        async with client.stream(
                            "POST", f"{API}/chat/completions", headers=headers, json=payload,
                        ) as response:
                            if response.status_code == 401 and attempt == 0:
                                auth.expires_at = self.expires_at = 0
                                continue
                            if response.status_code == 422:
                                logger.warning(
                                    "gigachat_validation_rejected generation=%s reason=%s "
                                    "messages=%d functions=%d function_call=%s max_tokens=%d",
                                    generation_log_id.get(), await read_validation_reason(response),
                                    len(messages), len(tools or []), payload["function_call"],
                                    settings.llm_gigachat_max_tokens,
                                )
                            if response.status_code != 200:
                                raise GatewayError(f"gigachat_http_{response.status_code}")
                            async with aclosing(self.read_events(
                                response, started, structured=schema is not None,
                            )) as events:
                                async for event in events:
                                    yield event
                            return
        except (TimeoutError, httpx.TimeoutException) as error:
            raise GatewayError("gigachat_timeout", timeout_kind=http_timeout_kind(error)) from None
        except httpx.HTTPError:
            raise GatewayError("gigachat_transport") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError("gigachat_invalid_response") from None

    @staticmethod
    async def read_events(
        response: httpx.Response, started: float, *, structured: bool,
    ) -> AsyncGenerator[str | ToolCall, None]:
        usage: dict[str, Any] = {}
        reason = None
        function = None
        state_id = None
        size = 0
        async for line in response_lines(response):
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                if reason not in ("stop", "length", "function_call"):
                    raise GatewayError("gigachat_interrupted")
                prompt, completion, total = (
                    value if type(value) is int and value >= 0 else None
                    for value in (
                        usage.get("prompt_tokens"), usage.get("completion_tokens"),
                        usage.get("total_tokens"),
                    )
                )
                logger.info(
                    "gigachat_stream_done generation=%s reason=%s prompt_tokens=%s "
                    "completion_tokens=%s total_tokens=%s duration_ms=%.0f",
                    generation_log_id.get(), reason, prompt, completion, total,
                    (perf_counter() - started) * 1000,
                )
                if reason == "function_call":
                    if function is None or not state_id:
                        raise GatewayError("gigachat_invalid_tool_state")
                    yield ToolCall(function["name"], function["arguments"], state_id)
                if reason == "length" and structured:
                    raise _StructuredOutputLimit
                return
            item = json.loads(data)
            if not isinstance(item, dict) or "error" in item:
                raise GatewayError("gigachat_invalid_response")
            if isinstance(item.get("usage"), dict):
                usage = item["usage"]
            choices = item.get("choices", [])
            if not isinstance(choices, list):
                raise GatewayError("gigachat_invalid_response")
            for choice in choices:
                if (finish := choice.get("finish_reason")) is not None:
                    reason = finish
                    if reason == "blacklist":
                        raise GatewayError("gigachat_filtered")
                    if reason == "error":
                        raise GatewayError("gigachat_invalid_arguments")
                delta = choice.get("delta", {})
                if delta.get("functions_state_id") is not None:
                    state_id = delta["functions_state_id"]
                    if not isinstance(state_id, str) or len(state_id) > 200:
                        raise GatewayError("gigachat_invalid_tool_state")
                if delta.get("function_call") is not None:
                    function = delta["function_call"]
                    if (
                        not isinstance(function, dict) or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), dict)
                    ):
                        raise GatewayError("gigachat_invalid_arguments")
                content = delta.get("content")
                if content is None:
                    continue
                if not isinstance(content, str):
                    raise GatewayError("gigachat_invalid_response")
                size += len(content)
                if size > 64000:
                    raise GatewayError("output_limit")
                if content:
                    yield content
        raise GatewayError("gigachat_interrupted")
