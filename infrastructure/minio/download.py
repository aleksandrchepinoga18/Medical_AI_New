"""Build-time download of pinned official MinIO binaries; standard library only.

Hashes were read from the official GitHub release asset listings, not obtained
from the download response. This file is not run by container entrypoints.
"""

import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("Usage: download.py COMPONENT TARGETOS TARGETARCH DESTINATION")
    component, target_os, architecture, destination = sys.argv[1:]
    manifest = json.loads(Path(__file__).with_name("releases.json").read_text(encoding="utf-8"))
    if target_os != "linux" or component not in manifest:
        raise SystemExit("Only pinned mc/minio Linux releases are supported")
    release = manifest[component]
    expected_hash = release["sha256"].get(architecture)
    if expected_hash is None:
        raise SystemExit(f"Unsupported architecture: {architecture}; use linux/amd64 or linux/arm64")

    version = release["version"]
    asset = f"{component}.linux-{architecture}.{version}"
    url = f"https://github.com/{release['repository']}/releases/download/{version}/{asset}"
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".partial")
    digest = hashlib.sha256()
    request = Request(url, headers={"User-Agent": "medical-ai-container-build"})
    try:
        with urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            if response.status != 200 or not response.geturl().startswith("https://"):
                raise SystemExit("Expected an HTTPS release asset response")
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != expected_hash:
            raise SystemExit(f"SHA-256 mismatch for {asset}; refusing to install")
        partial.chmod(0o755)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    print(f"Verified {asset}: sha256:{expected_hash}")


if __name__ == "__main__":
    main()
