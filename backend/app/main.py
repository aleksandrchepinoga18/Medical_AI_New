"""HTTP entry point: liveness, authentication and protected document ingestion."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.auth.routes import router
from app.documents.routes import router as documents_router

app = FastAPI(title="Medical AI", version="0.1.0", docs_url=None, redoc_url=None)
app.include_router(router)
app.include_router(documents_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo password inputs in validation errors.
    return JSONResponse(status_code=422, content={"detail": "Invalid request"})


@app.get("/health/live", include_in_schema=False)
def liveness() -> dict[str, str]:
    """Process liveness only, not a claim about dependency readiness."""
    return {"status": "ok"}
