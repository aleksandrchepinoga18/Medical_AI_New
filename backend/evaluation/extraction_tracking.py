"""Optional aggregate metrics in the existing local MLflow tracking service."""

from time import time

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


def register(
    tracking_uri: str, summaries: dict[str, dict[str, object]], params: dict[str, str],
) -> str:
    url = httpx.URL(tracking_uri)
    if (
        url.scheme not in {"http", "https"}
        or url.host not in {"localhost", "127.0.0.1", "::1"}
        or url.userinfo or url.path != "/" or url.query or url.fragment
    ):
        raise ValueError("invalid_mlflow_uri")
    with httpx.Client(
        base_url=str(url).rstrip("/") + "/api/2.0/mlflow/",
        timeout=10, trust_env=False, follow_redirects=False,
    ) as client:
        def experiment() -> httpx.Response:
            return client.get("experiments/get-by-name", params={"experiment_name": EXPERIMENT})

        lookup = experiment()
        if lookup.status_code == 404:
            created = client.post("experiments/create", json={"name": EXPERIMENT})
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
            json={"experiment_id": experiment_id, "run_name": "synthetic PDFs", "start_time": now},
        )
        response.raise_for_status()
        run_id: str = response.json()["run"]["info"]["run_id"]
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
            status = "FINISHED"
        finally:
            client.post(
                "runs/update",
                json={"run_id": run_id, "status": status, "end_time": int(time() * 1000)},
            ).raise_for_status()
        return run_id
