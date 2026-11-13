FROM qdrant/qdrant:v1.15.5
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl --fail --silent http://127.0.0.1:6333/readyz || exit 1
