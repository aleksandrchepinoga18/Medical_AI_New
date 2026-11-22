"""Explicit one-time provisioning only; never called by Compose startup."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def run_mc(*arguments: str) -> str:
    result = subprocess.run(["mc", *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        # SDK/CLI errors can contain credentials, endpoints and server responses.
        raise RuntimeError("MinIO operation failed; inspect configuration privately")
    return result.stdout


def listed_values(output: str) -> set[str]:
    """mc list emits one JSON object per line; reject unexpected/error records."""
    values: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.add(value.rstrip("/"))
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)

    for line in output.splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or record.get("status") != "success":
            raise RuntimeError("Unexpected MinIO listing; no provisioning performed")
        collect(record)
    return values


def main() -> None:
    if sys.argv[1:] not in (["provision"], ["provision-knowledge"]):
        raise RuntimeError("Explicit 'provision' or 'provision-knowledge' argument required")
    knowledge = sys.argv[1] == "provision-knowledge"
    bucket = os.environ.get("DOCUMENTS_S3_BUCKET", "medical-ai-documents")
    access = os.environ.get("DOCUMENTS_S3_ACCESS_KEY", "")
    secret = os.environ.get("DOCUMENTS_S3_SECRET_KEY", "")
    root = os.environ["MINIO_ROOT_USER"]
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
        or bucket == os.environ.get("MLFLOW_ARTIFACT_BUCKET", "mlflow-artifacts")
        or not re.fullmatch(r"[a-zA-Z0-9_-]{3,64}", access)
        or access in {root, os.environ.get("MLFLOW_S3_ACCESS_KEY", "")}
        or len(secret) < 16
        or secret.startswith("CHANGE_ME")
        or secret == os.environ["MINIO_ROOT_PASSWORD"]
        or secret == os.environ.get("MLFLOW_S3_SECRET_KEY")
    ):
        raise RuntimeError("Configure a separate document bucket and dedicated credentials")
    os.umask(0o077)
    # Credentials stay in the subprocess environment, not alias commands or printed output.
    os.environ["MC_HOST_storage"] = (
        f"http://{quote(root, safe='')}:{quote(os.environ['MINIO_ROOT_PASSWORD'], safe='')}"
        "@minio:9000"
    )
    os.environ["MC_CONFIG_DIR"] = "/tmp/mc-documents"
    policy_name = "medical-ai-documents-storage"
    buckets = listed_values(run_mc("ls", "--json", "storage"))
    users = listed_values(run_mc("admin", "user", "list", "--json", "storage"))
    policies = listed_values(run_mc("admin", "policy", "list", "--json", "storage"))
    if knowledge:
        if bucket not in buckets or access not in users or policy_name not in policies:
            raise RuntimeError("Provision document storage first")
        policy_name = "medical-ai-knowledge-storage"
        if policy_name in policies:
            raise RuntimeError("Knowledge policy already exists; refusing to overwrite")
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [f"arn:aws:s3:::{bucket}/knowledge/*"],
            }],
        }
        path = Path("/tmp/knowledge-policy.json")
        path.write_text(json.dumps(policy), encoding="utf-8")
        run_mc("admin", "policy", "create", "storage", policy_name, str(path))
        run_mc("admin", "policy", "attach", "storage", policy_name, "--user", access)
        print("Knowledge prefix policy attached to the document storage account.")
        return
    if bucket in buckets or access in users or policy_name in policies:
        raise RuntimeError("Document bucket, user or policy already exists; refusing to overwrite")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [f"arn:aws:s3:::{bucket}/users/*"],
            },
        ],
    }
    path = Path("/tmp/documents-policy.json")
    path.write_text(json.dumps(policy), encoding="utf-8")
    # New buckets are private by default; do not modify any existing bucket policy.
    run_mc("mb", f"storage/{bucket}")
    run_mc("admin", "policy", "create", "storage", policy_name, str(path))
    run_mc("admin", "user", "add", "storage", access, secret)
    run_mc("admin", "policy", "attach", "storage", policy_name, "--user", access)
    print("Document bucket and dedicated scoped account provisioned; MLflow unchanged.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Document provisioning stopped. Check settings and resource conflicts privately. "
            "Partial new resources may remain; do not retry blindly or delete existing data.",
            file=sys.stderr,
        )
        sys.exit(1)
