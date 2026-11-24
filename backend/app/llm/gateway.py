"""Provider boundary. Never log prompts, responses or upstream errors."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings

T = TypeVar("T", bound=BaseModel)
LLMProvider = Literal["ollama", "gigachat"]
ProviderStatus = Literal["ready", "not_configured", "unavailable"]
logger = logging.getLogger("uvicorn.error").getChild("ollama")
generation_log_id: ContextVar[str] = ContextVar("llm_generation_id", default="-")


async def response_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    """Bound NDJSON records and accept the final record without a newline."""
    buffer = b""
    async for block in response.aiter_bytes():
        buffer += block
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if len(line) > 262144:
                raise GatewayError("invalid_response")
            if line.strip():
                yield line
        if len(buffer) > 262144:
            raise GatewayError("invalid_response")
    if buffer.strip():
        yield buffer


class GatewayError(Exception):
    """Safe machine-readable error; no upstream payload."""

    def __init__(self, code: str, *, timeout_kind: str | None = None) -> None:
        super().__init__(code)
        self.timeout_kind = timeout_kind


def http_timeout_kind(error: TimeoutError | httpx.TimeoutException) -> str:
    if isinstance(error, httpx.TimeoutException):
        return {
            httpx.ConnectTimeout: "connect", httpx.ReadTimeout: "read",
            httpx.WriteTimeout: "write", httpx.PoolTimeout: "pool",
        }.get(type(error), "http")
    return "deadline"


class _StructuredOutputLimit(Exception):
    """The provider reached num_predict after yielding its last structured chunk."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    state_id: str | None = None

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"function": {"name": self.name, "arguments": self.arguments}}
        if self.state_id is not None:
            message["state_id"] = self.state_id
        return message


class Provider(Protocol):
    def stream(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None
    ) -> AsyncIterator[str]: ...

    def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[str | ToolCall, None]: ...


class Ollama:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings, self.transport = settings, transport

    async def stream(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None
    ) -> AsyncIterator[str]:
        async for event in self._events(messages, schema=schema):
            if not isinstance(event, str):
                raise GatewayError("unexpected_tool_call")
            yield event

    def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[str | ToolCall, None]:
        return self._events(messages, tools=tools)

    async def _events(
        self, messages: list[dict[str, Any]], *, schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[str | ToolCall, None]:
        # Settings permits only Docker Ollama or explicitly selected host Ollama.
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_predict": self.settings.llm_max_tokens,
                "num_ctx": self.settings.llm_context_tokens,
                "temperature": 0.2,
            },
        }
        if schema is not None:
            payload["format"] = schema
        if tools is not None:
            payload["tools"] = tools
        size = 0
        started = perf_counter()
        logger.info(
            "ollama_request generation=%s provider=ollama model=%s "
            "num_ctx=%d num_predict=%d messages=%d input_bytes=%d",
            generation_log_id.get(), self.settings.llm_model,
            self.settings.llm_context_tokens, self.settings.llm_max_tokens, len(messages),
            sum(len(message["content"].encode()) for message in messages),
        )
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.settings.llm_timeout_seconds, connect=5),
                ) as client:
                    async with client.stream(
                        "POST",
                        self.settings.llm_ollama_url + "/api/chat",
                        json=payload,
                    ) as response:
                        logger.info(
                            "ollama_headers status=%d duration_ms=%.0f",
                            response.status_code, (perf_counter() - started) * 1000,
                        )
                        if response.status_code != 200:
                            if tools and response.status_code == 400:
                                raise GatewayError("tools_unsupported")
                            raise GatewayError("unavailable")
                        async for line in response_lines(response):
                            item = json.loads(line)
                            if not isinstance(item, dict) or "error" in item:
                                raise GatewayError("invalid_response")
                            message = item.get("message", {})
                            if not isinstance(message, dict):
                                raise GatewayError("invalid_response")
                            content = message.get("content", "")
                            if not isinstance(content, str):
                                raise GatewayError("invalid_response")
                            size += len(content)
                            if size > 64000:
                                raise GatewayError("output_limit")
                            if content:
                                yield content
                            calls = message.get("tool_calls", [])
                            if not isinstance(calls, list):
                                raise GatewayError("invalid_response")
                            for call in calls:
                                function = call.get("function") if isinstance(call, dict) else None
                                if not isinstance(function, dict):
                                    raise GatewayError("invalid_response")
                                name, args = function.get("name"), function.get("arguments")
                                if not isinstance(name, str) or not isinstance(args, dict):
                                    raise GatewayError("invalid_response")
                                yield ToolCall(name, args)
                            if item.get("done") is True:
                                reason = item.get("done_reason")
                                safe_reason = reason if reason in ("stop", "length") else "other"
                                prompt_tokens, output_tokens = (
                                    value if type(value) is int and value >= 0 else None
                                    for value in (
                                        item.get("prompt_eval_count"), item.get("eval_count"),
                                    )
                                )
                                logger.info(
                                    "ollama_stream_done generation=%s reason=%s prompt_tokens=%s "
                                    "output_tokens=%s duration_ms=%.0f",
                                    generation_log_id.get(), safe_reason, prompt_tokens,
                                    output_tokens,
                                    (perf_counter() - started) * 1000,
                                )
                                if schema is not None and reason == "length":
                                    raise _StructuredOutputLimit
                                return
                        logger.warning("ollama_stream_eof duration_ms=%.0f", (
                            perf_counter() - started
                        ) * 1000)
                        raise GatewayError("interrupted")
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("ollama_timeout duration_ms=%.0f", (perf_counter() - started) * 1000)
            raise GatewayError("timeout") from None
        except httpx.HTTPError:
            logger.warning("ollama_transport_error duration_ms=%.0f", (
                perf_counter() - started
            ) * 1000)
            raise GatewayError("unavailable") from None
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("invalid_response") from None


class Gateway:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        return self.provider.stream(messages)

    def tool_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> AsyncGenerator[str | ToolCall, None]:
        return self.provider.tool_stream(messages, tools)

    async def generate(self, messages: list[dict[str, str]]) -> str:
        return "".join([part async for part in self.stream(messages)])

    async def structured(self, messages: list[dict[str, str]], model: type[T]) -> T:
        parts: list[str] = []
        reached_limit = False
        try:
            async for part in self.provider.stream(messages, model.model_json_schema()):
                parts.append(part)
        except _StructuredOutputLimit:
            reached_limit = True
        try:
            return model.model_validate_json("".join(parts))
        except ValidationError as error:
            truncated = reached_limit and any(
                item["type"] == "json_invalid" for item in error.errors(include_input=False)
            )
            code = "structured_output_truncated" if truncated else "invalid_structure"
            raise GatewayError(code) from None


def provider_status(settings: Settings, provider: LLMProvider) -> ProviderStatus:
    """Check local configuration without contacting either provider."""
    if provider == "ollama":
        return "ready" if settings.llm_model.strip() else "not_configured"
    if not settings.gigachat_credentials.get_secret_value().strip():
        return "not_configured"
    if settings.gigachat_ca_bundle:
        from app.documents.gigachat import gigachat_tls_context

        try:
            gigachat_tls_context(settings.gigachat_ca_bundle)
        except (OSError, ValueError):
            return "unavailable"
    return "ready"


def gateway(
    settings: Settings, provider: str | None = None, *, external_consent: bool = False,
) -> Gateway:
    selected = settings.llm_provider if provider is None else provider
    if selected == "ollama":
        return Gateway(Ollama(settings))
    if (
        selected == "gigachat" and external_consent
        and provider_status(settings, "gigachat") == "ready"
    ):
        from app.llm.gigachat import GigaChat

        return Gateway(GigaChat(settings))
    raise GatewayError("provider_disabled")
