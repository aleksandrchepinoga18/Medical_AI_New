"""Exercise the running dev backend through its HTTP API using a synthetic PDF."""

import hashlib
import json
import sys
from typing import Any
from uuid import UUID, uuid4

import httpx
import pymupdf

from app.db import get_settings


class SmokeError(Exception):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def request(
    client: httpx.Client, method: str, path: str, expected: int = 200, **kwargs: Any,
) -> Any:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.RequestError as error:
        raise SmokeError(f"{method} {path}: {type(error).__name__}") from None
    check(
        response.status_code == expected,
        f"{method} {path}: HTTP {response.status_code}, expected {expected}",
    )
    if expected == 204:
        return
    return response.json()


def unpublish(client: httpx.Client, source_path: str, version_id: str) -> None:
    for attempt in range(2):
        try:
            result = request(
                client, "PUT", f"{source_path}/status",
                json={"version_id": version_id, "status": "outdated"},
            )
            check(result["current_version"]["status"] == "outdated", "Expected outdated status")
            return
        except (SmokeError, KeyError, TypeError, ValueError):
            if attempt == 1:
                raise SmokeError(
                    f"Cleanup could not be confirmed. Set {source_path} version {version_id} "
                    "to outdated via its status endpoint.",
                ) from None


def smoke(client: httpx.Client, search_max_k: int, index_timeout: int) -> None:
    marker = f"syntheticsmoke{uuid4().hex[:12]}"
    text = f"Synthetic reference sheet. Test marker {marker}."
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), text)
        pdf = document.tobytes()
    checksum = hashlib.sha256(pdf).hexdigest()
    metadata = {
        "title": f"Synthetic smoke check {marker}",
        "material_type": "synthetic test",
        "author": "Local smoke script",
        "publisher": "Local dev smoke check",
        "url": f"https://example.org/knowledge-smoke/{marker}",
        "specialty": "Synthetic",
        "language": "en",
        "jurisdiction": "Synthetic",
        "version": "1",
    }
    print(f"Generated one-page synthetic PDF: {marker}")
    created = request(
        client, "POST", "/knowledge/sources", expected=201,
        data={"metadata": json.dumps(metadata)},
        files={"file": ("synthetic-knowledge.pdf", pdf, "application/pdf")},
    )
    source_id = str(UUID(created["id"]))
    version = created["current_version"]
    version_id = str(UUID(version["id"]))
    source_path = f"/knowledge/sources/{source_id}"
    query = {"query": marker, "k": min(10, search_max_k)}
    try:
        print(f"Created source {source_id}, version {version_id}")
        check(version["status"] == "uploaded", "Expected uploaded status")
        check(version["sha256"] == checksum, "Uploaded SHA-256 did not match the generated PDF")
        verified = request(
            client, "PUT", f"{source_path}/status",
            json={
                "version_id": version_id,
                "status": "verified",
                "review_revision": version["review_revision"],
                "verified_sha256": checksum,
                "review_note": "Generated locally for this smoke check. Synthetic text only.",
            },
        )
        check(verified["current_version"]["status"] == "verified", "Expected verified status")
        indexed = request(
            client, "POST", f"{source_path}/index",
            json={"version_id": version_id}, timeout=index_timeout,
        )
        chunk_count = indexed["current_version"]["chunk_count"]
        check(chunk_count > 0, "Indexing returned no chunks")
        print(f"MinIO write and read validated. Indexed {chunk_count} chunk(s).")
        published = request(
            client, "PUT", f"{source_path}/status",
            json={"version_id": version_id, "status": "published"},
        )
        check(published["current_version"]["status"] == "published", "Expected published status")
        found = request(client, "POST", "/knowledge/search", json=query)
        hits = [item for item in found["items"] if item["source"]["id"] == source_id]
        check(hits, "Hybrid Search did not return the synthetic source")
        for hit in hits:
            source = hit["source"]
            check(
                source["version_id"] == version_id and all(
                    source[key] == metadata[key] for key in ("title", "url", "publisher", "version")
                ),
                "Hybrid Search returned incorrect source metadata",
            )
        check(
            any(
                marker in hit["text"]
                and any(
                    locator["kind"] == "page"
                    and locator["index"] == 1
                    and locator["start"] < hit["end_char"]
                    and locator["end"] > hit["start_char"]
                    for locator in hit["locators"]
                )
                for hit in hits
            ),
            "Hybrid Search did not return the marker with a page 1 locator",
        )
        print("Hybrid Search returned the expected source, version, marker and page 1 locator.")
    finally:
        unpublish(client, source_path, version_id)
        print(f"Source {source_id} is outdated.")

    remaining = request(client, "POST", "/knowledge/search", json=query)
    check(
        all(item["source"]["id"] != source_id for item in remaining["items"]),
        "The outdated source is still in Hybrid Search results",
    )
    print("The synthetic source is absent from Hybrid Search after unpublishing.")


def main() -> None:
    email = sys.stdin.readline().rstrip("\n")
    password = sys.stdin.readline().rstrip("\n")
    check(email and password, "Provide the existing administrator's credentials via stdin")
    settings = get_settings()
    # The client points only at the running backend, with redirects and ambient proxies disabled.
    with httpx.Client(
        base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False,
        headers={"Origin": settings.auth_origin}, timeout=2 * settings.rag_http_timeout + 30,
    ) as client:
        csrf = request(client, "GET", "/auth/csrf")["csrf_token"]
        # Send the session cookies explicitly on this internal loopback connection.
        client.headers["Cookie"] = f"medical_csrf={csrf}"
        request(
            client, "POST", "/auth/login", json={"email": email, "password": password},
            headers={"X-CSRF-Token": csrf},
        )
        csrf = client.cookies.get("medical_csrf")
        session = client.cookies.get("medical_session")
        check(csrf and session, "Login did not issue session cookies")
        client.headers["Cookie"] = f"medical_session={session}; medical_csrf={csrf}"
        client.headers["X-CSRF-Token"] = csrf
        try:
            request(client, "GET", "/knowledge/sources", params={"limit": 1})
            print("Administrator session verified.")
            smoke(
                client, settings.rag_search_max_k,
                settings.documents_processing_seconds + settings.rag_index_seconds
                + 3 * settings.rag_http_timeout + 30,
            )
        finally:
            request(client, "POST", "/auth/logout", expected=204)
    print("PASS: medical knowledge smoke check.")


if __name__ == "__main__":
    try:
        main()
    except SmokeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, TypeError, ValueError):
        print("FAIL: Unexpected API response or local configuration.", file=sys.stderr)
        sys.exit(1)
