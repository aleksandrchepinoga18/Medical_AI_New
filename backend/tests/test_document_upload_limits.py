from collections.abc import AsyncGenerator

import pytest
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect

from app.documents.routes import ClosingMultipartParser


@pytest.mark.parametrize("malformed", [False, True])
def test_partial_multipart_files_are_closed(malformed: bool) -> None:
    import asyncio

    async def stream() -> AsyncGenerator[bytes, None]:
        yield (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; '
            b'filename="synthetic.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-'
        )
        if malformed:
            raise ValueError("synthetic parser failure")
        raise MultiPartException("synthetic size limit")

    parser = ClosingMultipartParser(
        Headers({"content-type": "multipart/form-data; boundary=boundary"}), stream()
    )
    with pytest.raises((ValueError, MultiPartException)):
        asyncio.run(parser.parse())
    assert parser._files_to_close_on_error
    assert all(file.closed for file in parser._files_to_close_on_error)


def test_unauthenticated_upload_does_not_parse_body() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/documents", content=b"invalid multipart")
    assert response.status_code == 401


def test_download_disconnect_closes_original_iterator() -> None:
    import asyncio

    from app.documents.routes import OriginalResponse

    closed = []

    def chunks():
        try:
            yield b"synthetic-first"
            yield b"synthetic-second"
        finally:
            closed.append(True)

    stream = chunks()
    first = next(stream)
    response = OriginalResponse(stream, first, {})

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("synthetic disconnect")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect) as error:
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert isinstance(error.value.__context__, OSError)
    assert str(error.value.__context__) == "synthetic disconnect"
    assert closed == [True]


def test_oversized_multipart_header_is_rejected_before_staging() -> None:
    parser = ClosingMultipartParser(Headers(), empty_stream())
    parser.on_part_begin()
    with pytest.raises(MultiPartException, match="headers too large"):
        parser.on_header_value(b"x" * 16385, 0, 16385)
    assert not parser._files_to_close_on_error


async def empty_stream() -> AsyncGenerator[bytes, None]:
    yield b""
