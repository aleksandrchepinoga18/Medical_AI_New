"""Optional aggregate metrics in the existing local MLflow tracking service."""

from time import time
from urllib.parse import quote, urlsplit

import httpx

EXPERIMENT = "medical-ai-synthetic-document-extraction"


def numeric_metrics(summary: dict[str, object], prefix: str = "") -> dict[str, int | float]:
    metrics: dict[str, int | float] = {}
    for key, value in summary.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            metrics.update(numeric_metrics(value, f"{name}."))
        elif type(value) in (int, float):
            metrics[name] = value
    return metrics


def local_tracking_url(
    tracking_uri: str, *, allow_docker_host: bool = False, allow_tracking_service: bool = False,
) -> httpx.URL:
    url = httpx.URL(tracking_uri)
    allowed_host = url.host in {"localhost", "127.0.0.1", "::1"} or (
        allow_docker_host and url.host == "host.docker.internal" and url.port == 5000
    ) or (
        allow_tracking_service and url.host == "mlflow" and url.port == 5000
    )
    if (
        url.scheme not in {"http", "https"} or not allowed_host
        or url.userinfo or url.path != "/" or url.query or url.fragment
    ):
        raise ValueError("invalid_mlflow_uri")
    return url


def register(
    tracking_uri: str, summaries: dict[str, dict[str, object]], params: dict[str, str],
    *, experiment_name: str = EXPERIMENT, run_name: str = "synthetic PDFs",
    artifact_json: bytes | None = None, allow_docker_host: bool = False,
    allow_tracking_service: bool = False,
) -> str:
    url = local_tracking_url(
        tracking_uri, allow_docker_host=allow_docker_host,
        allow_tracking_service=allow_tracking_service,
    )
    with httpx.Client(
        base_url=str(url).rstrip("/") + "/api/2.0/mlflow/",
        timeout=10, trust_env=False, follow_redirects=False,
    ) as client:
        def experiment() -> httpx.Response:
            return client.get(
                "experiments/get-by-name", params={"experiment_name": experiment_name},
            )

        lookup = experiment()
        if lookup.status_code == 404:
            created = client.post("experiments/create", json={"name": experiment_name})
            if created.status_code in (400, 409) and (
                created.json().get("error_code") == "RESOURCE_ALREADY_EXISTS"
            ):
                lookup = experiment()
                lookup.raise_for_status()
                experiment_id = lookup.json()["experiment"]["experiment_id"]
            else:
                created.raise_for_status()
                experiment_id = created.json()["experiment_id"]
        else:
            lookup.raise_for_status()
            experiment_id = lookup.json()["experiment"]["experiment_id"]
        now = int(time() * 1000)
        response = client.post(
            "runs/create",
            json={"experiment_id": experiment_id, "run_name": run_name, "start_time": now},
        )
        response.raise_for_status()
        info = response.json()["run"]["info"]
        run_id: str = info["run_id"]
        metrics = [
            {"key": f"{method}.{key}", "value": value, "timestamp": now, "step": 0}
            for method, summary in summaries.items()
            for key, value in numeric_metrics(summary).items()
        ]
        status = "FAILED"
        try:
            logged = client.post(
                "runs/log-batch",
                json={
                    "run_id": run_id,
                    "metrics": metrics,
                    "params": [{"key": key, "value": value} for key, value in params.items()],
                },
            )
            logged.raise_for_status()
            if artifact_json is not None:
                root = urlsplit(info["artifact_uri"])
                if root.scheme != "mlflow-artifacts" or root.netloc or ".." in root.path.split("/"):
                    raise ValueError("mlflow_artifact_proxy_required")
                path = quote(root.path.strip("/"), safe="/")
                artifact_url = url.copy_with(
                    path=f"/api/2.0/mlflow-artifacts/artifacts/{path}/evaluation.json",
                )
                client.put(artifact_url, content=artifact_json, headers={
                    "Content-Type": "application/json",
                }).raise_for_status()
            status = "FINISHED"
        finally:
            client.post(
                "runs/update",
                json={"run_id": run_id, "status": status, "end_time": int(time() * 1000)},
            ).raise_for_status()
        return run_id
