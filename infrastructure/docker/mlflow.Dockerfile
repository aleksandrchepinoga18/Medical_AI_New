FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DO_NOT_TRACK=1
WORKDIR /app
COPY infrastructure/mlflow/ ./
RUN if [ -f requirements.lock ]; then pip install --require-hashes -r requirements.lock; \
    else pip install -r requirements.txt; fi \
    && useradd --uid 10001 --create-home mlflow
USER mlflow
CMD ["python", "server.py"]
