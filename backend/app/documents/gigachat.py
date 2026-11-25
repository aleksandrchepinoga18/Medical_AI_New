"""Shared GigaChat HTTP adapter for document extraction."""

import asyncio
import json
import logging
import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic, perf_counter, time
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError

from app.documents.extraction_data import Evidence, Measurement, Measurements
from app.llm.gateway import GatewayError, http_timeout_kind

logger = logging.getLogger(__name__)

MODELS = {
    "lite": "GigaChat-2",
    "pro": "GigaChat-2-Pro",
    "max": "GigaChat-2-Max",
    "ultra": "GigaChat-3-Ultra",
}
ResponseMode = Literal["schema", "prompt-json"]
DeleteStatus = Literal["not_uploaded", "unknown", "deleted", "failed"]
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API = "https://api.giga.chat/v1"
UPLOAD_BYTES = 5 * 1024 * 1024
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "precached_prompt_tokens")
PROMPT = (
    "Извлеки все лабораторные измерения из приложенного PDF. "
    "Одна запись на строку таблицы. Сохраняй исходные названия, знаки < и >, "
    "разделители, единицы, референсы и дату измерения. "
    "Не считай реквизиты, даты оформления и номера измерениями. "
    "Укажи номер страницы PDF от 1. В quote перепиши видимые ячейки в порядке "
    "слева направо, разделяя их строкой ' | '. Переносы внутри ячейки можно "
    "заменить пробелами. Пропущенные или неподтверждённые поля запиши как null. "
    "Если измерений нет, верни пустой массив measurements. Верни только JSON."
)


def gigachat_tls_context(ca_bundle: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_bundle:
        context.load_verify_locations(cafile=ca_bundle)
    return context


def error_category(code: str) -> str:
    suffix = code.partition("_")[2]
    if suffix in {"tls", "timeout", "interrupted"}:
        return suffix
    if suffix == "transport":
        return "network"
    if suffix in {"http_401", "http_403", "expired_token"}:
        return "authorization"
    return "api_response"


@contextmanager
def log_stage(stage: str) -> Iterator[None]:
    started = perf_counter()
    code = None
    try:
        yield
    except BaseException as error:
        code = str(error) if isinstance(error, GatewayError) else f"{stage}_interrupted"
        raise
    finally:
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        # Log only adapter-generated codes. Never include a URL, exception or HTTP payload.
        logger.log(logging.INFO if code is None else logging.WARNING, "%s", json.dumps({
            "event": "gigachat_extraction", "stage": stage,
            "status": "succeeded" if code is None else "failed",
            "duration_ms": elapsed_ms,
            "error_category": None if code is None else error_category(code),
            "error_code": code,
        }))


def is_tls_error(error: BaseException | None) -> bool:
    while error is not None:
        if isinstance(error, ssl.SSLError):
            return True
        error = error.__cause__ or error.__context__
    return False


@dataclass
class GigaChatResult:
    measurements: list[Evidence] = field(default_factory=list)
    latency_ms: float = 0.0
    reported_model: str | None = None
    tokens: dict[str, int] | None = None
    deletion: DeleteStatus = "not_uploaded"
    error: str | None = None
    cleanup_error: str | None = None
    pending_file_id: str | None = None
    upload_name: str | None = None


class GigaChatExtraction:
    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: SecretStr,
        model: str,
        mode: ResponseMode,
        *,
        scope: str = "GIGACHAT_API_PERS",
        prompt: str = PROMPT,
        request_seconds: float = 60,
        cleanup_seconds: float = 15,
    ) -> None:
        self.client = client
        self.credentials = credentials
        self.model = MODELS.get(model, model)
        self.mode = mode
        self.scope = scope
        self.prompt = prompt
        self.request_seconds = request_seconds
        self.cleanup_seconds = cleanup_seconds
        self.access_token = ""
        self.expires_at = 0.0

    async def request(
        self, phase: str, url: str, *, timeout: float, **kwargs: Any
    ) -> httpx.Response:
        if timeout <= 0:
            raise GatewayError(f"{phase}_timeout")
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.post(url, **kwargs)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise GatewayError(f"{phase}_timeout", timeout_kind=http_timeout_kind(error)) from None
        except httpx.HTTPError as error:
            failure = "tls" if is_tls_error(error) else "transport"
            raise GatewayError(f"{phase}_{failure}") from None
        if not response.is_success:
            raise GatewayError(f"{phase}_http_{response.status_code}")
        return response

    @staticmethod
    def object(response: httpx.Response, phase: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise GatewayError(f"{phase}_invalid_response") from None
        if not isinstance(body, dict):
            raise GatewayError(f"{phase}_invalid_response")
        return body

    async def headers(self, *, timeout: float) -> dict[str, str]:
        if time() + 30 >= self.expires_at:
            with log_stage("oauth"):
                response = await self.request(
                    "oauth", OAUTH_URL, timeout=timeout,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Basic {self.credentials.get_secret_value()}",
                        "RqUID": str(uuid4()),
                    },
                    data={"scope": self.scope},
                )
                body = self.object(response, "oauth")
                token, expires = body.get("access_token"), body.get("expires_at")
                if not isinstance(token, str) or not token or type(expires) is not int:
                    raise GatewayError("oauth_invalid_response")
                expires_at = expires / 1000 if expires >= 10**12 else float(expires)
                if expires_at <= time():
                    raise GatewayError("oauth_expired_token")
                self.access_token, self.expires_at = token, expires_at
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def payload(self, file_id: str) -> dict[str, Any]:
        schema = Measurements.model_json_schema()
        # Send a self-contained schema with no local references.
        del schema["$defs"]
        schema["properties"]["measurements"]["items"] = Measurement.model_json_schema()
        content = self.prompt
        if self.mode == "prompt-json":
            content += "\nСхема JSON:\n" + json.dumps(schema, ensure_ascii=False)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content, "attachments": [file_id]}],
            "function_call": "auto",
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 2500,
        }
        if self.mode == "schema":
            payload["response_format"] = {"type": "json_schema", "schema": schema, "strict": True}
        return payload

    def read_completion(self, response: httpx.Response, result: GigaChatResult) -> None:
        body = self.object(response, "completion")
        usage = body.get("usage")
        if isinstance(usage, dict):
            result.tokens = {
                key: usage[key] for key in USAGE_FIELDS
                if type(usage.get(key)) is int and usage[key] >= 0
            } or None
        model = body.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model):
            result.reported_model = model
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise GatewayError("completion_invalid_response")
        choice = choices[0]
        reason = choice.get("finish_reason")
        if reason != "stop":
            code = reason if reason in ("length", "blacklist", "function_call") else "other"
            raise GatewayError(f"completion_unfinished_{code}")
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise GatewayError("completion_invalid_response")
        content = message["content"].strip()
        if self.mode == "prompt-json" and content.startswith("```") and content.endswith("```"):
            _, _, content = content[:-3].partition("\n")
        try:
            parsed = Measurements.model_validate_json(content)
        except ValidationError:
            raise GatewayError("completion_invalid_structure") from None
        result.measurements = [
            Evidence.model_validate(row.model_dump()) for row in parsed.measurements
        ]

    async def extract(
        self, pdf: bytes, *, deadline: float | None = None, upload_name: str | None = None,
    ) -> GigaChatResult:
        result = GigaChatResult()
        if not pdf.startswith(b"%PDF-") or len(pdf) > UPLOAD_BYTES:
            result.error = "invalid_fixture"
            return result
        start = perf_counter()
        file_id = None

        def available(limit: float, cutoff: float | None) -> float:
            return limit if cutoff is None else min(limit, cutoff - monotonic())

        request_deadline = None if deadline is None else deadline - self.cleanup_seconds
        try:
            headers = await self.headers(timeout=available(self.request_seconds, request_deadline))
            with log_stage("upload"):
                upload_timeout = available(self.request_seconds, request_deadline)
                if upload_timeout <= 0:
                    raise GatewayError("upload_timeout")
                result.upload_name = upload_name or f"synthetic-benchmark-{uuid4().hex}.pdf"
                result.deletion = "unknown"
                upload = await self.request(
                    "upload", f"{API}/files", timeout=upload_timeout, headers=headers,
                    data={"purpose": "general"},
                    files={"file": (result.upload_name, pdf, "application/pdf")},
                )
                candidate = self.object(upload, "upload").get("id")
                if not isinstance(candidate, str) or not FILE_ID.fullmatch(candidate):
                    raise GatewayError("upload_invalid_response")
            file_id = candidate
            with log_stage("completion"):
                completion = await self.request(
                    "completion", f"{API}/chat/completions",
                    timeout=available(self.request_seconds, request_deadline),
                    headers=headers, json=self.payload(file_id),
                )
                self.read_completion(completion, result)
        except GatewayError as error:
            result.error = str(error)
            if result.error in {
                f"upload_http_{status}" for status in (400, 401, 403, 404, 413, 415, 422, 429)
            }:
                result.deletion = "not_uploaded"
        finally:
            if file_id is not None:
                try:
                    cleanup_deadline = monotonic() + available(self.cleanup_seconds, deadline)
                    with log_stage("delete"):
                        headers = await self.headers(
                            timeout=available(self.cleanup_seconds, cleanup_deadline),
                        )
                        deleted = await self.request(
                            "delete", f"{API}/files/{file_id}/delete",
                            timeout=available(self.cleanup_seconds, cleanup_deadline),
                            headers=headers,
                        )
                        if deleted.status_code != 200:
                            raise GatewayError("delete_unconfirmed")
                except GatewayError as error:
                    result.deletion = "failed"
                    result.cleanup_error = str(error)
                    result.pending_file_id = file_id
                else:
                    result.deletion = "deleted"
            if result.deletion in {"not_uploaded", "deleted"}:
                result.upload_name = None
            result.latency_ms = (perf_counter() - start) * 1000
        return result
