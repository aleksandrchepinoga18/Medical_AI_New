"""Import local medical originals into the running backend as uploaded sources."""

import argparse
import getpass
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any, BinaryIO

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.db import get_settings
from app.documents.validation import inspect_upload
from app.integrations.object_storage import CONTENT_TYPES
from app.knowledge.schemas import SourceUpload

IDENTIFIERS = ("url", "doi", "pmid")
Identity = tuple[str, str]


class ManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    metadata: SourceUpload


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Annotated[list[ManifestItem], Field(min_length=1)]


class ImportFailure(Exception):
    pass


def import_path(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or "\\" in name or ":" in name or ".." in relative.parts:
        raise ImportFailure("Expected a relative file path inside the manifest directory")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ImportFailure("File is missing or lies outside the manifest directory")
    return path


def copy_original(path: Path, target: BinaryIO, maximum: int) -> None:
    size = 0
    with path.open("rb") as original:
        while block := original.read(65536):
            size += len(block)
            if size > maximum:
                raise ImportFailure("File exceeds the configured document size limit")
            target.write(block)
    target.seek(0)


def identities(metadata: dict[str, Any], checksum: str) -> set[Identity]:
    return {
        (field, str(metadata[field])) for field in IDENTIFIERS if metadata.get(field)
    } | {("sha256", checksum)}


def require_status(response: httpx.Response, expected: int) -> None:
    if response.status_code != expected:
        raise ImportFailure(
            f"{response.request.method} {response.request.url.path}: "
            f"HTTP {response.status_code}, expected {expected}",
        )


def csrf_headers(client: httpx.Client, email: str, password: str) -> None:
    response = client.get("/auth/csrf")
    require_status(response, 200)
    token = response.json()["csrf_token"]
    client.headers["Cookie"] = f"medical_csrf={token}"
    response = client.post(
        "/auth/login", json={"email": email, "password": password}, headers={"X-CSRF-Token": token},
    )
    require_status(response, 200)
    token = client.cookies.get("medical_csrf")
    session = client.cookies.get("medical_session")
    if not token or not session:
        raise ImportFailure("Login response did not include a session")
    # The CLI only connects to this container's loopback HTTP endpoint.
    client.headers.update({
        "Cookie": f"medical_session={session}; medical_csrf={token}",
        "X-CSRF-Token": token,
    })


class Registry:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.by_key: dict[Identity, list[dict[str, Any]]] = {}
        self.refresh()

    def add(self, row: dict[str, Any]) -> None:
        for key in identities(row, row["current_version"]["sha256"]):
            self.by_key.setdefault(key, []).append(row)

    def refresh(self) -> None:
        self.by_key.clear()
        after: str | None = None
        visited: set[str] = set()
        while True:
            params = {"limit": "100"}
            if after:
                params["after"] = after
            response = self.client.get("/knowledge/sources", params=params)
            require_status(response, 200)
            page = response.json()
            for row in page["items"]:
                self.add(row)
            after = page["next_after"]
            if after is None:
                return
            if after in visited:
                raise ImportFailure("Registry returned a repeated paging cursor")
            visited.add(after)

    def find(
        self, metadata: dict[str, Any], checksum: str, keys: set[Identity],
    ) -> dict[str, Any] | None:
        matches = {row["id"]: row for key in keys for row in self.by_key.get(key, [])}
        if not matches:
            return None
        if len(matches) == 1:
            row = next(iter(matches.values()))
            current = row["current_version"]
            if (
                current["version"] == metadata["version"] and current["sha256"] == checksum
                and all(row.get(field) == metadata[field] for field in IDENTIFIERS
                        if metadata.get(field))
            ):
                return row
        raise ImportFailure("Existing source identity or checksum conflicts with this item")

    def upload(
        self, metadata: dict[str, Any], checksum: str, keys: set[Identity],
        file: BinaryIO, filename: str, content_type: str,
    ) -> tuple[str, str]:
        existing = self.find(metadata, checksum, keys)
        if existing is not None:
            return "already_uploaded", str(existing["id"])
        response = self.client.post(
            "/knowledge/sources", data={"metadata": json.dumps(metadata)},
            files={"file": (filename, file, content_type)},
        )
        if response.status_code == 409:
            self.refresh()
            existing = self.find(metadata, checksum, keys)
            if existing is not None:
                return "already_uploaded", str(existing["id"])
        require_status(response, 201)
        row = response.json()
        self.add(row)
        version = row["current_version"]
        if version["status"] != "uploaded" or version["sha256"] != checksum:
            raise ImportFailure(
                "The API did not confirm an uploaded source with the expected SHA-256",
            )
        return "uploaded", str(row["id"])


def batch(
    manifest: Manifest, root: Path, settings: Settings, registry: Registry | None,
) -> Iterable[dict[str, Any]]:
    seen: dict[Identity, int] = {}
    for number, item in enumerate(manifest.items, 1):
        result: dict[str, Any] = {"item": number, "file": item.file}
        try:
            path = import_path(root, item.file)
            metadata = item.metadata.model_dump(mode="json")
            with SpooledTemporaryFile(max_size=1048576, mode="w+b") as original:
                copy_original(path, original, settings.documents_max_bytes)
                upload = inspect_upload(original, path.name, settings)
                if upload.sha256 != item.sha256.lower():
                    raise ImportFailure("SHA-256 does not match the manifest")
                keys = identities(metadata, upload.sha256)
                duplicates = {seen[key] for key in keys if key in seen}
                if duplicates:
                    raise ImportFailure(f"Duplicate identity or SHA-256 in item {min(duplicates)}")
                for key in keys:
                    seen[key] = number
                if registry is None:
                    result["status"] = "ready"
                else:
                    result["status"], result["source_id"] = registry.upload(
                        metadata, upload.sha256, keys, original, upload.filename,
                        CONTENT_TYPES[upload.format],
                    )
        except ImportFailure as error:
            result.update(status="failed", error=str(error))
        except HTTPException as error:
            result.update(status="failed", error=str(error.detail))
        except httpx.RequestError as error:
            result.update(status="failed", error=f"{type(error).__name__}; rerun to reconcile")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            result.update(status="failed", error=type(error).__name__)
        yield result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate local files without API access",
    )
    parser.add_argument(
        "--credentials-stdin", action="store_true", help="Read email and password lines",
    )
    args = parser.parse_args()
    report: dict[str, Any] = {"dry_run": args.dry_run, "items": []}
    rows: list[dict[str, Any]] = report["items"]
    try:
        manifest_path = args.manifest.resolve()
        manifest = Manifest.model_validate_json(manifest_path.read_bytes())
        settings = get_settings()
        if args.dry_run:
            rows.extend(batch(manifest, manifest_path.parent, settings, None))
        else:
            if args.credentials_stdin:
                email = sys.stdin.readline().rstrip("\r\n")
                password = sys.stdin.readline().rstrip("\r\n")
            elif sys.stdin.isatty():
                print("Email: ", end="", file=sys.stderr, flush=True)
                email = sys.stdin.readline().rstrip("\r\n")
                password = getpass.getpass("Password: ")
            else:
                raise ImportFailure("Use an interactive terminal or --credentials-stdin")
            if not email or not password:
                raise ImportFailure("Administrator credentials are required")
            with httpx.Client(
                base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False,
                headers={"Origin": settings.auth_origin},
                timeout=settings.documents_upload_grace_seconds + 60,
            ) as client:
                csrf_headers(client, email, password)
                try:
                    rows.extend(batch(manifest, manifest_path.parent, settings, Registry(client)))
                finally:
                    require_status(client.post("/auth/logout"), 204)
    except ValidationError as error:
        report["validation_errors"] = [
            {"location": issue["loc"], "type": issue["type"]} for issue in error.errors(
                include_input=False,
            )
        ]
    except ImportFailure as error:
        report["error"] = str(error)
    except KeyboardInterrupt:
        report["error"] = "Interrupted; rerun to reconcile with the registry"
    except (OSError, ValueError, TypeError, KeyError, httpx.RequestError) as error:
        report["error"] = type(error).__name__
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(
        "error" in report or "validation_errors" in report
        or any(row["status"] == "failed" for row in rows)
    )


if __name__ == "__main__":
    sys.exit(main())
