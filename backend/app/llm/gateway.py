"""Local-only provider boundary. Never log prompts, responses or upstream errors."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


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


class Provider(Protocol):
    def stream(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None
    ) -> AsyncIterator[str]: ...


class Ollama:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings, self.transport = settings, transport

    async def stream(
        self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None
    ) -> AsyncIterator[str]:
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
        size = 0
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
                        if response.status_code != 200:
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
                            if item.get("done") is True:
                                # Reaching num_predict is a valid Ollama completion.
                                # Structured output still requires Pydantic validation.
                                reason = item.get("done_reason")
                                safe_reason = reason if reason in ("stop", "length") else "other"
                                logger.info("ollama_stream_done reason=%s", safe_reason)
                                return
                        raise GatewayError("interrupted")
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayError("timeout") from None
        except httpx.HTTPError:
            raise GatewayError("unavailable") from None
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("invalid_response") from None


class Gateway:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        return self.provider.stream(messages)

    async def generate(self, messages: list[dict[str, str]]) -> str:
        return "".join([part async for part in self.stream(messages)])

    async def structured(self, messages: list[dict[str, str]], model: type[T]) -> T:
        parts = self.provider.stream(messages, model.model_json_schema())
        text = "".join([part async for part in parts])
        try:
            return model.model_validate_json(text)
        except ValidationError:
            raise GatewayError("invalid_structure") from None


def gateway(settings: Settings, provider: str = "ollama") -> Gateway:
    # Future OpenAI/DeepSeek/YandexGPT/GigaChat adapters must implement Provider.
    # No registry, SDK, credentials, fallback or external network path exists today.
    if provider != "ollama":
        raise GatewayError("provider_disabled")
    return Gateway(Ollama(settings))
