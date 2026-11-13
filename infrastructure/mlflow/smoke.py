"""Manual infrastructure acceptance check using synthetic, non-medical data only.

Not imported by the application and never run during container startup.
Creates and keeps a uniquely named experiment, run, artifact and registry version.
"""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from mlflow import MlflowClient

uri = os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
client = MlflowClient(tracking_uri=uri, registry_uri=uri)
name = f"infrastructure-smoke-{uuid4().hex}"
experiment_id = client.create_experiment(name)
run = client.create_run(experiment_id, tags={"purpose": "infrastructure-check"})
run_id = run.info.run_id
client.log_param(run_id, "synthetic", "true")
client.log_metric(run_id, "probe", 1.0)

with tempfile.TemporaryDirectory() as directory:
    probe = Path(directory) / "probe.txt"
    probe.write_text("Medical AI infrastructure probe\n", encoding="utf-8")
    client.log_artifact(run_id, str(probe))
    download_dir = Path(directory) / "download"
    download_dir.mkdir()
    downloaded = client.download_artifacts(run_id, "probe.txt", str(download_dir))
    assert Path(downloaded).read_text(encoding="utf-8") == probe.read_text(encoding="utf-8")

client.set_terminated(run_id)
stored = client.get_run(run_id)
assert stored.data.params["synthetic"] == "true"
assert stored.data.metrics["probe"] == 1.0
assert stored.info.artifact_uri.startswith("mlflow-artifacts:/")

client.create_registered_model(name, description="Synthetic infrastructure check; not a model")
# Registry version metadata can reference an artifact without loading/executing any model.
version = client.create_model_version(
    name,
    source=f"{stored.info.artifact_uri}/probe.txt",
    run_id=run_id,
    description="Synthetic artifact reference; not deployable",
)
assert client.get_model_version(name, version.version).run_id == run_id
print(f"PASS experiment={name} run={run_id} registry_version={version.version}")
print("Synthetic records kept for the manual persistence check; no real model was created.")
