# Production image: Rust public listener, Go control plane, Rust worker.
# The browser UI is the Vite build. This image does not install a Python
# interpreter or the stateless MCP Node server.
# Build locally: docker build -t deepseek-infra:4.8.0 .
FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
RUN test -f /build/static/ui/index.html

FROM rust:1.85-bookworm AS rust-builder

WORKDIR /app
COPY rust ./rust
COPY proto ./proto
COPY VERSION ./VERSION
RUN cargo build \
    --locked \
    --manifest-path rust/Cargo.toml \
    --release \
    -p deepseek-gateway \
    -p deepseek-worker

FROM golang:1.27.1-bookworm AS go-builder

WORKDIR /src
COPY go/go.mod go/go.sum ./
RUN go mod download
COPY go/ ./
RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o /out/deepseekd ./cmd/deepseekd
RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o /out/deepseek-launch ./cmd/deepseek-launch

FROM debian:bookworm-slim

WORKDIR /app

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="DeepSeek Infra" \
      org.opencontainers.image.version="4.8.0" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.description="Rust and Go production runtime"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 deepseek

COPY --from=rust-builder /app/rust/target/release/deepseek-gateway /usr/local/bin/deepseek-gateway
COPY --from=rust-builder /app/rust/target/release/deepseek-worker /usr/local/bin/deepseek-worker
COPY --from=go-builder /out/deepseekd /usr/local/bin/deepseekd
COPY --from=go-builder /out/deepseek-launch /usr/local/bin/deepseek-launch
COPY packaging/native/entrypoint.sh /usr/local/bin/deepseek-infra-entrypoint
COPY static /app/static
COPY --from=frontend-builder /build/static/ui ./static/ui
RUN test -f /app/static/ui/index.html \
    && chmod 755 /usr/local/bin/deepseek-infra-entrypoint \
    && mkdir -p /data/go-control /data/worker \
    && chown -R deepseek:deepseek /data

ENV DEEPSEEK_INFRA_ROOT=/data \
    DEEPSEEK_INFRA_STATIC_DIR=/app/static \
    GATEWAY_BIND_ADDR=0.0.0.0:8000 \
    GO_CONTROL_ADDR=http://127.0.0.1:8090 \
    DEEPSEEKD_MODE=authoritative \
    DEEPSEEKD_LISTEN=127.0.0.1:8090 \
    DEEPSEEKD_PRODUCTION_STORE=/data/go-control \
    DEEPSEEK_WORKER_LISTEN=127.0.0.1:50052

VOLUME ["/data"]
USER deepseek
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8000/healthz"]

ENTRYPOINT ["/usr/local/bin/deepseek-infra-entrypoint"]
