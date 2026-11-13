FROM python:3.12.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/ ./

FROM base AS development
RUN if [ -f requirements-dev.lock ]; then \
      pip install --require-hashes -r requirements-dev.lock && pip install --no-deps .; \
    else pip install '.[dev]'; fi \
    && useradd --uid 10001 --create-home app && chown -R app:app /app
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
RUN if [ -f requirements.lock ]; then \
      pip install --require-hashes -r requirements.lock && pip install --no-deps .; \
    else pip install .; fi \
    && useradd --uid 10001 --create-home app
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
