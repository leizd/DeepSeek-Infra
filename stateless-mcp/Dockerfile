FROM rust:1.85-bookworm AS build

WORKDIR /src
COPY rust/Cargo.toml rust/Cargo.lock rust/
COPY rust/crates rust/crates
WORKDIR /src/rust
RUN cargo build --release --locked -p deepseek-stateless-mcp

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /var/lib/deepseek --create-home deepseek

COPY --from=build /src/rust/target/release/deepseek-stateless-mcp /usr/local/bin/deepseek-stateless-mcp

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8010 \
    MCP_WORKSPACE_ROOT=/workspace

WORKDIR /workspace
USER deepseek
EXPOSE 8010

CMD ["deepseek-stateless-mcp"]
