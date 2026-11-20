import asyncio
import json
from collections.abc import Callable
from time import time
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from evaluation.gigachat_extraction import (
    API,
    OAUTH_URL,
    GigaChatExtraction,
    GigaChatResult,
    ResponseMode,
)

SYNTHETIC_PDF = b"%PDF-1.7\nsynthetic mock only"
ROW = {
    "name": "Glucose",
    "value": "<0,10",
    "unit": "mmol/L",
    "reference": "3,9–5,5",
    "date": None,
    "page": 1,
    "quote": "Glucose | <0,10 | mmol/L | 3,9–5,5",
}


def success(request: httpx.Request) -> httpx.Response:
    if str(request.url) == OAUTH_URL:
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Basic synthetic-key"
        assert UUID(request.headers["RqUID"]).version == 4
        assert request.content == b"scope=GIGACHAT_API_PERS"
        return httpx.Response(200, json={
            "access_token": "mock-access", "expires_at": int((time() + 1800) * 1000),
        })
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer mock-access"
    if str(request.url) == f"{API}/files":
        assert b'name="purpose"\r\n\r\ngeneral' in request.content
        assert b"Content-Type: application/pdf" in request.content
        assert SYNTHETIC_PDF in request.content
        return httpx.Response(200, json={"id": "test-file"})
    if str(request.url) == f"{API}/chat/completions":
        body = json.loads(request.content)
        assert body["model"] == "GigaChat-3-Ultra"
        assert body["messages"][0]["attachments"] == ["test-file"]
        assert body["function_call"] == "auto"
        if "response_format" in body:
            output = body["response_format"]
            assert output["type"] == "json_schema"
            assert output["strict"] is True
            assert "$ref" not in json.dumps(output["schema"])
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps({"measurements": [ROW]}, ensure_ascii=False)},
            }],
            "model": "GigaChat-3-Ultra:3.0",
            "usage": {"prompt_tokens": 101, "completion_tokens": 49, "total_tokens": 150},
        })
    assert str(request.url) == f"{API}/files/test-file/delete"
    return httpx.Response(200, json={"id": "test-file", "deleted": True})


def run(
    handler: Callable[[httpx.Request], httpx.Response], mode: ResponseMode = "schema",
) -> tuple[GigaChatResult, list[str]]:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    async def extract() -> GigaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = GigaChatExtraction(client, SecretStr("synthetic-key"), "ultra", mode)
            return await adapter.extract(SYNTHETIC_PDF)

    return asyncio.run(extract()), calls


def test_full_file_lifecycle_and_usage() -> None:
    result, calls = run(success)
    assert calls == [
        OAUTH_URL, f"{API}/files", f"{API}/chat/completions", f"{API}/files/test-file/delete",
    ]
    assert result.error is None
    assert result.deletion == "deleted"
    assert result.pending_file_id is None
    assert result.upload_name is None
    assert result.tokens == {"prompt_tokens": 101, "completion_tokens": 49, "total_tokens": 150}
    assert result.reported_model == "GigaChat-3-Ultra:3.0"
    assert result.measurements[0].value == "<0,10"
    assert result.measurements[0].bbox is None


def test_prompt_mode_is_explicit_and_validates_fenced_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            assert "response_format" not in body
            assert "Схема JSON" in body["messages"][0]["content"]
            content = "```json\n" + json.dumps({"measurements": [ROW]}) + "\n```"
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            })
        return success(request)

    result, _ = run(handler, "prompt-json")
    assert result.error is None
    assert len(result.measurements) == 1
    assert result.tokens is None


@pytest.mark.parametrize(
    ("phase", "failure", "expected", "deletion"),
    [
        ("oauth", 401, "oauth_http_401", "not_uploaded"),
        ("oauth", "malformed", "oauth_invalid_response", "not_uploaded"),
        ("upload", 413, "upload_http_413", "not_uploaded"),
        ("upload", "timeout", "upload_timeout", "unknown"),
        ("upload", "malformed", "upload_invalid_response", "unknown"),
        ("completion", 422, "completion_http_422", "deleted"),
        ("completion", "timeout", "completion_timeout", "deleted"),
        ("completion", "malformed", "completion_invalid_response", "deleted"),
        ("completion", "invalid_structure", "completion_invalid_structure", "deleted"),
    ],
)
def test_failures_record_safe_reason_and_cleanup(
    phase: str, failure: str | int, expected: str, deletion: str,
) -> None:
    paths = {
        "oauth": "/api/v2/oauth", "upload": "/v1/files", "completion": "/v1/chat/completions",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != paths[phase]:
            return success(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream data", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="private upstream data")
        if failure == "invalid_structure":
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 42},
            })
        assert isinstance(failure, int)
        return httpx.Response(failure, text="private upstream data")

    result, calls = run(handler)
    assert result.error == expected
    assert result.deletion == deletion
    assert (f"{API}/files/test-file/delete" in calls) == (deletion == "deleted")
    assert (result.upload_name is not None) == (deletion == "unknown")
    if failure == "invalid_structure":
        assert result.tokens == {"total_tokens": 42}


@pytest.mark.parametrize("timeout", (False, True))
def test_delete_failure_retains_recovery_id(timeout: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/delete"):
            if timeout:
                raise httpx.ReadTimeout("private upstream data", request=request)
            return httpx.Response(503)
        return success(request)

    result, _ = run(handler)
    assert result.error is None
    assert result.deletion == "failed"
    assert result.cleanup_error == ("delete_timeout" if timeout else "delete_http_503")
    assert result.pending_file_id == "test-file"
    assert len(result.measurements) == 1
