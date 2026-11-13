# Official release assets and SHA-256 values: infrastructure/minio/releases.json.
# No dependency on the unavailable minio/mc or minio/minio Docker repositories.
FROM python:3.12.12-slim-bookworm AS mc-download
ARG TARGETOS
ARG TARGETARCH
WORKDIR /download
COPY infrastructure/minio/download.py infrastructure/minio/releases.json ./
RUN python download.py mc "$TARGETOS" "$TARGETARCH" /out/mc

FROM python:3.12.12-slim-bookworm AS client
ENV MC_CONFIG_DIR=/tmp/mc
COPY --from=mc-download /out/mc /usr/local/bin/mc
ENTRYPOINT ["/usr/local/bin/mc"]
CMD ["--help"]

FROM mc-download AS server-download
RUN python download.py minio "$TARGETOS" "$TARGETARCH" /out/minio

FROM client AS server
ENV MINIO_UPDATE=off
COPY --from=server-download /out/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
