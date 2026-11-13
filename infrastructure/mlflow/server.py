"""Build connection URLs without shell expansion or plaintext URI in Compose."""

import os

from sqlalchemy import URL

backend_uri = URL.create(
    "postgresql+psycopg2",
    username=os.environ["MLFLOW_DB_USER"],
    password=os.environ["MLFLOW_DB_PASSWORD"],
    host="mlflow-db",
    port=5432,
    database=os.environ["MLFLOW_DB"],
).render_as_string(hide_password=False)

# MLflow handles schema initialization when the user starts this service.
# For existing stores, back up and explicitly run `mlflow db upgrade` before upgrades.
os.environ["MLFLOW_BACKEND_STORE_URI"] = backend_uri
os.environ["MLFLOW_REGISTRY_STORE_URI"] = backend_uri
os.execvp(
    "mlflow",
    [
        "mlflow", "server",
        "--host", "0.0.0.0",
        "--port", "5000",
        "--serve-artifacts",
        "--artifacts-destination",
        f"s3://{os.environ['MLFLOW_ARTIFACT_BUCKET']}/artifacts",
        "--default-artifact-root", "mlflow-artifacts:/",
    ],
)
