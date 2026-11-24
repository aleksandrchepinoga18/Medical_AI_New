"""Check committed Git trees and tracked edits without printing matched content."""

import argparse
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

PATTERNS = {
    "private key": re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "provider token": re.compile(
        rb"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,})"
    ),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{12,}"),
    "credential in URI": re.compile(rb"://[^\s/'\"<>:@]+:[^\s/'\"<>@\x24{}]+@"),
    "possible patient identifier": re.compile(
        rb"(?i)\b(?:medical_record_number|patient_id|mrn)\s*[\"']?\s*[:=]\s*"
        rb"[\"']?[0-9][0-9 -]{5,}[0-9]"
    ),
}
SECRET_NAME = (
    rb"[A-Z0-9_]*(?:PASSWORD|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET_KEY|CLIENT_SECRET|"
    rb"SECRET_ACCESS_KEY|GIGACHAT_CREDENTIALS)"
)
ASSIGNMENT = re.compile(
    rb"""(?im)\b""" + SECRET_NAME + rb"""\s*=\s*["']([A-Za-z0-9_.+/=@-]{8,})["']"""
)
MAPPING = re.compile(
    rb"""(?im)["']""" + SECRET_NAME + rb"""["']\s*:\s*["']([A-Za-z0-9_.+/=@-]{8,})["']"""
)
CONFIG_ASSIGNMENT = re.compile(
    rb"""(?im)^[ \t]*""" + SECRET_NAME
    + rb"""[ \t]*[:=][ \t]*["']?([A-Za-z0-9_.+/=@-]{8,})["']?[ \t\r]*(?:#.*)?$"""
)
# Exact reviewed unit-test literals for rejected credentials/configurations.
TEST_LITERALS = {
    "backend/tests/test_auth_integration.py": {
        "262ca93c6d454a50666ac983591aeb0e8bd9fa1f0068165b5c6c5d740ad48247",
    },
    "backend/tests/test_document_storage.py": {
        "01a41b00181dfd884d6a60d422ebc1437447bdfc2caae634adceefc2ccb189b2",
        "a99fc647fe178c2d63c4c7773d785fcc80c20c4f97facb252c64f46cb7e9bd49",
    },
    "backend/tests/test_llm_gateway.py": {
        "6fb8981faad51e0eb027f26c5868df546699d4e618fafe837384758befe0d2d9",
    },
}
LOCAL_PATH = re.compile(
    r"(?i)(?:^|/)(?:evaluation-reports|mlruns|mlartifacts|backups|data|scratch|secrets|"
    r"\.codex|\.agents|\.aws|\.ssh|\.venv(?:-[^/]+)?|tmp|temp)/"
    r"|(?:^|/)(?:mintty\.[^/]+\.png|stage\d+-test-config\.json|id_(?:rsa|dsa|ecdsa|ed25519))$"
    r"|\.(?:key|pem|pfx|p12|ppk|crt|cer|sqlite\d*|dump|bak|log|tmp|pdf|docx|dcm)$"
)


def git(*args):
    return subprocess.check_output(["git", *args])


def display(path):
    if any(pattern.search(path) for pattern in PATTERNS.values()):
        return "<redacted path>"
    return ascii(path.decode("utf-8", errors="replace"))


def forbidden_path(path):
    name = path.rsplit("/", 1)[-1].lower()
    env = name == ".env" or name.startswith(".env.")
    return (
        (env and not name.endswith(".example"))
        or name.startswith("credentials")
        or bool(LOCAL_PATH.search(path))
    )


def content_findings(data, path):
    test_literals = TEST_LITERALS.get(path, ())
    for rule, pattern in PATTERNS.items():
        if any(sha256(match[0]).hexdigest() not in test_literals
               for match in pattern.finditer(data)):
            yield rule
    matches = [*ASSIGNMENT.finditer(data), *MAPPING.finditer(data)]
    file = Path(path)
    if file.suffix.lower() in {".yaml", ".yml"} or file.name.lower().startswith(".env"):
        matches.extend(CONFIG_ASSIGNMENT.finditer(data))
    for match in matches:
        if sha256(match[1]).hexdigest() in test_literals:
            continue
        value = match[1].lower()
        if (
            value.startswith((b"change_me", b"change-me", b"replace_me", b"replace-me"))
            and file.name.endswith(".example")
        ):
            continue
        if (
            (value == b"synthetic"
             or value.startswith((b"synthetic-", b"fixture-", b"fake-", b"mock-", b"unused-")))
            and ("/tests/" in path or "/evaluation/" in path or path.startswith(".github/")
                 or re.fullmatch(r"infrastructure/compose\.[\w-]*test[\w-]*\.yaml", path))
        ):
            continue
        yield "possible hardcoded credential"
        break


def audit(history, worktree):
    seen = set()
    entries = {}
    flagged = set()

    def report(rule, path, revision):
        if (rule, path) not in flagged:
            flagged.add((rule, path))
            print(f"{revision} {rule}: {display(path)}")

    revisions = git("rev-list", "--all", "HEAD").splitlines() if history else [b"HEAD"]
    for revision in revisions:
        commit = revision.decode("ascii")
        message = git("show", "-s", "--format=%B", commit)
        for rule in content_findings(message, ""):
            report(rule, b"<commit message>", commit[:12])
        for row in git("ls-tree", "-rz", commit).split(b"\0"):
            if not row:
                continue
            meta, path = row.split(b"\t", 1)
            _, kind, oid = meta.split()
            if kind != b"blob" or (oid, path) in seen:
                continue
            seen.add((oid, path))
            if forbidden_path(path.decode("utf-8", errors="replace")):
                report("local or sensitive file type", path, commit[:12])
            entries.setdefault(oid, []).append((path, commit[:12]))

    with subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    ) as reader:
        for oid, occurrences in entries.items():
            reader.stdin.write(oid + b"\n")
            reader.stdin.flush()
            header = reader.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError("missing audit blob")
            data = reader.stdout.read(int(header[2]))
            if reader.stdout.read(1) != b"\n":
                raise ValueError("incomplete audit blob")
            for path, commit in occurrences:
                for rule in content_findings(data, path.decode("utf-8", errors="replace")):
                    report(rule, path, commit)
        reader.stdin.close()
        if reader.wait():
            raise ValueError("could not read audit blobs")

    if worktree:
        for path in sorted(set(git("ls-files", "-z").split(b"\0")) - {b""}):
            name = path.decode("utf-8", errors="replace")
            if forbidden_path(name):
                report("local or sensitive file type", path, "worktree")
            file = Path(name)
            if file.is_file() and not file.is_symlink():
                for rule in content_findings(file.read_bytes(), name):
                    report(rule, path, "worktree")
    print(f"Scanned {len(revisions)} tree(s), {len(entries)} blob(s). Findings: {len(flagged)}.")
    return int(bool(flagged))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Scan commits reachable from refs")
    parser.add_argument("--worktree", action="store_true", help="Also scan tracked working files")
    args = parser.parse_args()
    try:
        return audit(args.history, args.worktree)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("Git audit could not read all selected files or revisions.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
