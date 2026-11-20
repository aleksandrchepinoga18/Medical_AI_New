"""GigaChat file lifecycle for the isolated synthetic benchmark."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from time import perf_counter, time
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError

from app.llm.gateway import GatewayError
from evaluation.extraction_fixtures import Evidence, Measurement, Measurements

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
    "Извлеки все лабораторные измерения из приложенного синтетического PDF. "
    "Одна запись на строку таблицы. Сохраняй исходные названия, знаки < и >, "
    "разделители, единицы, референсы и дату измерения. "
    "Не считай реквизиты, даты оформления и номера измерениями. "
    "Укажи номер страницы PDF от 1. В quote перепиши видимые ячейки в порядке "
    "слева направо, разделяя их строкой ' | '. Переносы внутри ячейки можно "
    "заменить пробелами. Пропущенные или неподтверждённые поля запиши как null. "
    "Если измерений нет, верни пустой массив measurements. Верни только JSON."
)


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
        request_seconds: float = 60,
        cleanup_seconds: float = 15,
    ) -> None:
        self.client = client
        self.credentials = credentials
        self.model = MODELS[model]
        self.mode = mode
        self.request_seconds = request_seconds
        self.cleanup_seconds = cleanup_seconds
        self.access_token = ""
        self.expires_at = 0.0

    async def request(
        self, phase: str, url: str, *, timeout: float, **kwargs: Any
    ) -> httpx.Response:
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.post(url, **kwargs)
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayError(f"{phase}_timeout") from None
        except httpx.HTTPError:
            raise GatewayError(f"{phase}_transport") from None
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
            response = await self.request(
                "oauth", OAUTH_URL, timeout=timeout,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {self.credentials.get_secret_value()}",
                    "RqUID": str(uuid4()),
                },
                data={"scope": "GIGACHAT_API_PERS"},
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
        content = PROMPT
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

    async def extract(self, pdf: bytes) -> GigaChatResult:
        result = GigaChatResult()
        if not pdf.startswith(b"%PDF-") or len(pdf) > UPLOAD_BYTES:
            result.error = "invalid_fixture"
            return result
        start = perf_counter()
        file_id = None
        try:
            headers = await self.headers(timeout=self.request_seconds)
            result.upload_name = f"synthetic-benchmark-{uuid4().hex}.pdf"
            result.deletion = "unknown"
            upload = await self.request(
                "upload", f"{API}/files", timeout=self.request_seconds, headers=headers,
                data={"purpose": "general"},
                files={"file": (result.upload_name, pdf, "application/pdf")},
            )
            candidate = self.object(upload, "upload").get("id")
            if not isinstance(candidate, str) or not FILE_ID.fullmatch(candidate):
                raise GatewayError("upload_invalid_response")
            file_id = candidate
            completion = await self.request(
                "completion", f"{API}/chat/completions",
                timeout=self.request_seconds, headers=headers, json=self.payload(file_id),
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
                    headers = await self.headers(timeout=self.cleanup_seconds)
                    deleted = await self.request(
                        "delete", f"{API}/files/{file_id}/delete",
                        timeout=self.cleanup_seconds, headers=headers,
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
