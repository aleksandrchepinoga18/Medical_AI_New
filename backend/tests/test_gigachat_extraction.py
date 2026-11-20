import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from pathlib import Path
from time import monotonic, time
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.documents.gigachat import (
    API,
    OAUTH_URL,
    GigaChatExtraction,
    GigaChatResult,
    ResponseMode,
    gigachat_tls_context,
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


def stage_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, str | float | None]]:
    records = [r for r in caplog.records if r.name == "app.documents.gigachat"]
    events: list[dict[str, str | float | None]] = [
        json.loads(record.getMessage()) for record in records
    ]
    for event in events:
        assert set(event) == {
            "event", "stage", "status", "duration_ms", "error_category", "error_code",
        }
        duration = event["duration_ms"]
        assert event["event"] == "gigachat_extraction"
        assert isinstance(duration, (int, float)) and duration >= 0
    for private in ("synthetic-key", "mock-access", "test-file", "Glucose", "private upstream"):
        assert private not in caplog.text
    return events


def test_ca_bundle_adds_trust_and_preserves_tls_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_ca, extra_ca, *_ = ssl.create_default_context().get_ca_certs(binary_form=True)
    default_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    default_context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(default_ca))
    bundle = tmp_path / "extra.pem"
    bundle.write_text(ssl.DER_cert_to_PEM_cert(extra_ca), encoding="ascii")
    monkeypatch.setattr(ssl, "create_default_context", lambda: default_context)
    context = gigachat_tls_context(str(bundle))
    assert set(context.get_ca_certs(binary_form=True)) == {default_ca, extra_ca}
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_full_file_lifecycle_and_usage(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.documents.gigachat")
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
    events = stage_events(caplog)
    assert [event["stage"] for event in events] == ["oauth", "upload", "completion", "delete"]
    assert all(
        event["status"] == "succeeded" and event["error_category"] is None for event in events
    )


def test_exhausted_processing_budget_does_not_send() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url.host}")

    async def extract() -> GigaChatResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            adapter = GigaChatExtraction(client, SecretStr("synthetic-key"), "ultra", "schema")
            # The remaining window belongs to cleanup, so no attempt can start.
            return await adapter.extract(SYNTHETIC_PDF, deadline=monotonic() + 1)

    result = asyncio.run(extract())
    assert result.error == "oauth_timeout"
    assert result.deletion == "not_uploaded"


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
    ("phase", "failure", "expected", "deletion", "category"),
    [
        ("oauth", 401, "oauth_http_401", "not_uploaded", "authorization"),
        ("oauth", "tls", "oauth_tls", "not_uploaded", "tls"),
        ("oauth", "connect", "oauth_transport", "not_uploaded", "network"),
        ("oauth", "malformed", "oauth_invalid_response", "not_uploaded", "api_response"),
        ("upload", 413, "upload_http_413", "not_uploaded", "api_response"),
        ("upload", "timeout", "upload_timeout", "unknown", "timeout"),
        ("upload", "malformed", "upload_invalid_response", "unknown", "api_response"),
        ("completion", 422, "completion_http_422", "deleted", "api_response"),
        ("completion", "timeout", "completion_timeout", "deleted", "timeout"),
        ("completion", "malformed", "completion_invalid_response", "deleted", "api_response"),
        (
            "completion", "invalid_structure", "completion_invalid_structure",
            "deleted", "api_response",
        ),
        ("completion", "length", "completion_unfinished_length", "deleted", "api_response"),
    ],
)
def test_failures_record_safe_reason_and_cleanup(
    phase: str, failure: str | int, expected: str, deletion: str, category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.documents.gigachat")
    paths = {
        "oauth": "/api/v2/oauth", "upload": "/v1/files", "completion": "/v1/chat/completions",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != paths[phase]:
            return success(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream data", request=request)
        if failure == "tls":
            tls = ssl.SSLCertVerificationError("private upstream data")
            transport = OSError("transport wrapper")
            transport.__cause__ = tls
            raise httpx.ConnectError("private upstream data", request=request) from transport
        if failure == "connect":
            raise httpx.ConnectError("private upstream data", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="private upstream data")
        if failure == "invalid_structure":
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 42},
            })
        if failure == "length":
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"completion_tokens": 80},
            })
        assert isinstance(failure, int)
        return httpx.Response(failure, text="private upstream data")

    result, calls = run(handler)
    assert result.error == expected
    assert result.deletion == deletion
    assert (f"{API}/files/test-file/delete" in calls) == (deletion == "deleted")
    assert (result.upload_name is not None) == (deletion == "unknown")
    event, = stage_events(caplog)
    assert event["stage"] == phase and event["status"] == "failed"
    assert event["error_category"] == category and event["error_code"] == expected
    if phase == "oauth":
        assert calls == [OAUTH_URL]
    if failure == "invalid_structure":
        assert result.tokens == {"total_tokens": 42}
    if failure == "length":
        assert result.tokens == {"completion_tokens": 80}


@pytest.mark.parametrize("timeout", (False, True))
def test_delete_failure_retains_recovery_id(
    timeout: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.documents.gigachat")
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
    event, = stage_events(caplog)
    assert event["stage"] == "delete" and event["status"] == "failed"
    assert event["error_category"] == ("timeout" if timeout else "api_response")
