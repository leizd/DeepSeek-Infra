FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
RUN test -f /build/static/ui/index.html

FROM rust:1.85-bookworm AS builder

WORKDIR /app
COPY rust ./rust
COPY proto ./proto

RUN cargo build \
    --locked \
    --manifest-path rust/Cargo.toml \
    --release \
    -p deepseek-gateway \
    -p deepseek-worker

FROM debian:bookworm-slim AS runtime

ARG VCS_REF=unknown
LABEL org.opencontainers.image.version="4.8.0" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.description="DeepSeek Infra native Rust runtime binaries"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home deepseek

WORKDIR /app

FROM runtime AS worker

LABEL org.opencontainers.image.title="DeepSeek Infra Rust Worker"

COPY --from=builder \
    /app/rust/target/release/deepseek-worker \
    /usr/local/bin/deepseek-worker

ENV DEEPSEEK_WORKER_LISTEN=127.0.0.1:50052

USER deepseek

CMD ["deepseek-worker"]

FROM runtime AS gateway

LABEL org.opencontainers.image.title="DeepSeek Infra Rust Gateway"

COPY --from=builder \
    /app/rust/target/release/deepseek-gateway \
    /usr/local/bin/deepseek-gateway
COPY static ./static
COPY --from=frontend-builder /build/static/ui ./static/ui
RUN test -f /app/static/ui/index.html

ENV GATEWAY_BIND_ADDR=0.0.0.0:8787 \
    DEEPSEEK_INFRA_STATIC_DIR=/app/static

USER deepseek

EXPOSE 8787

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8787/healthz"]

CMD ["deepseek-gateway"]
