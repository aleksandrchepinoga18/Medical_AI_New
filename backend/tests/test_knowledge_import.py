import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from pydantic import ValidationError

from app.db import get_settings
from scripts.import_medical_knowledge import Manifest, Registry, batch


def entry(path: Path, title: str) -> dict[str, Any]:
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metadata": {
            "title": title, "material_type": "synthetic test", "author": "Synthetic author",
            "publisher": "Synthetic publisher", "url": f"https://example.org/{title}",
            "specialty": "Synthetic", "language": "en", "jurisdiction": "Synthetic",
            "version": "1",
        },
    }


def test_manifest_validation_and_dry_run(tmp_path: Path) -> None:
    pdf = tmp_path / "first.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    item = entry(pdf, "first")
    with pytest.raises(ValidationError):
        Manifest.model_validate({"items": [item], "password": "synthetic"})
    with pytest.raises(ValidationError):
        Manifest.model_validate({"items": [{**item, "sha256": "wrong"}]})
    with pytest.raises(ValidationError):
        Manifest.model_validate({"items": [{**item, "metadata": {
            **item["metadata"], "version": "", "status": "published",
        }}]})
    duplicate = entry(pdf, "copy")
    invalid_checksum = {**entry(pdf, "broken"), "sha256": "0" * 64}
    escaping = {**entry(pdf, "escape"), "file": "../first.pdf"}
    manifest = Manifest.model_validate({
        "items": [item, duplicate, invalid_checksum, escaping],
    })
    results = list(batch(manifest, tmp_path, get_settings(), None))
    assert [result["status"] for result in results] == ["ready", "failed", "failed", "failed"]
    assert "Duplicate" in results[1]["error"]
    assert "SHA-256" in results[2]["error"]
    assert "relative" in results[3]["error"]


def test_batch_resumes_from_registry_without_publishing(tmp_path: Path) -> None:
    pdf = tmp_path / "first.pdf"
    pdf.write_bytes(b"%PDF-first-synthetic")
    docx = tmp_path / "second.docx"
    with ZipFile(docx, "w") as original:
        original.writestr("[Content_Types].xml", "<Types/>")
        original.writestr("word/document.xml", "<document/>")
    items = [entry(pdf, "first"), entry(docx, "second")]
    manifest = Manifest.model_validate({"items": items})
    rows: dict[str, dict[str, Any]] = {}
    posts: list[str] = []
    lost_response = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal lost_response
        assert request.url.path == "/knowledge/sources"
        if request.method == "GET":
            if rows and not request.url.params.get("after"):
                return httpx.Response(200, json={"items": [], "next_after": str(uuid4())})
            return httpx.Response(200, json={"items": list(rows.values()), "next_after": None})
        assert request.method == "POST"
        posts.append(request.method)
        body = request.read()
        item = items[1] if b"second.docx" in body else items[0]
        assert item["metadata"]["url"].encode() in body
        assert (b"word/document.xml" if item is items[1] else pdf.read_bytes()) in body
        metadata = item["metadata"]
        row = {
            **metadata, "id": str(uuid4()),
            "current_version": {
                "version": "1", "sha256": item["sha256"], "status": "uploaded",
            },
        }
        rows[metadata["url"]] = row
        if lost_response:
            lost_response = False
            raise httpx.ReadTimeout("synthetic interruption", request=request)
        return httpx.Response(201, json=row)

    with httpx.Client(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler),
    ) as http:
        first = list(batch(manifest, tmp_path, get_settings(), Registry(http)))
        second = list(batch(manifest, tmp_path, get_settings(), Registry(http)))
        conflict = Manifest.model_validate({
            "items": [{**items[0], "metadata": {**items[0]["metadata"], "version": "2"}}],
        })
        conflicting = list(batch(conflict, tmp_path, get_settings(), Registry(http)))
    assert [result["status"] for result in first] == ["failed", "uploaded"]
    assert [result["status"] for result in second] == ["already_uploaded", "already_uploaded"]
    assert conflicting[0]["status"] == "failed"
    assert "conflicts" in conflicting[0]["error"]
    assert posts == ["POST", "POST"]
    assert len(rows) == 2
