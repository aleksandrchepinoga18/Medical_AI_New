"""Infrastructure HTTP entry point; business routes are added in later stages."""

from fastapi import FastAPI

app = FastAPI(title="Medical AI", version="0.1.0", docs_url=None, redoc_url=None)


@app.get("/health/live", include_in_schema=False)
def liveness() -> dict[str, str]:
    """Process liveness only, not a claim about dependency readiness."""
    return {"status": "ok"}
