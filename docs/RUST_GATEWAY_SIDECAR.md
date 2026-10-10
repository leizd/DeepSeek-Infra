# Rust Gateway Sidecar

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


从 3.0.3 起，仓库内新增一个可独立运行的 Rust Gateway sidecar：

3.4.0 extended the existing opt-in RAG delegate with `POST /rag/vectors/rank`. Semantic-cache candidate vectors can be ranked in Rust while cache storage, expiry, scoping, thresholds, and exact-match handling stay in Python. Invalid or unavailable sidecar responses fall back to the original Python scan. The selection rationale and deferred candidates are documented in [the 3.4.0 Rust candidate audit](RUST_CANDIDATE_AUDIT_3_4.md).

3.5.0 added optional deterministic Gateway request preparation at `POST /gateway/request/prepare`. When `DEEPSEEK_RUST_GATEWAY=1`, Python assembles a credential-free non-streaming upstream body, Rust validates and normalizes it, Python defensively validates the response, and Python then performs the real provider call. Streaming and `/v1/models` remain Python-owned. The shared 68-case corpus, fallback rules, stable error codes, and non-goals are documented in [GATEWAY_REQUEST_PREPARATION_PARITY.md](GATEWAY_REQUEST_PREPARATION_PARITY.md).

3.6.0 adds optional deterministic MCP protocol preparation at `POST /mcp/request/prepare`. When `DEEPSEEK_RUST_MCP=1`, Python prepares the request locally, asks Rust for the same normalized descriptor, adopts only a contract-identical Python-owned result, and then routes or executes through the existing Python MCP server. The shared 105-case corpus proves request, notification, response, initialize, tools, resources, prompts, size/depth, Unicode, and stable error-category parity. Rust never receives caller credentials, logs full params or tool arguments, or executes a tool. See [MCP_PROTOCOL_PREPARATION_PARITY.md](MCP_PROTOCOL_PREPARATION_PARITY.md).

3.7.0 adds optional deterministic RAG document preparation at `POST /rag/documents/prepare`. When `DEEPSEEK_RUST_RAG_DOCUMENT_PREP=1`, Python first parses the uploaded file and computes the local preparation contract, sends only parsed text plus allowlisted metadata and chunk configuration to Rust, adopts only an exact defensively validated result, and then performs embeddings, persistence, indexing, and retrieval in Python. The 125-case shared corpus proves Unicode character offsets, exact chunks, overlap, hashes, IDs, metadata boundaries, stable errors, and sidecar-loss fallback. Rust never receives paths, raw file bytes, credentials, or index ownership. See [RAG_DOCUMENT_PREPARATION_PARITY.md](RAG_DOCUMENT_PREPARATION_PARITY.md).

3.8.0 added no delegate. It made every existing delegate measurable in a locked release build, reused bounded process-local HTTP connections, separated Python preparation/serialization/transport/Rust processing/Python validation/total time, and exposed low-cardinality sidecar metrics on the existing listener. The benchmark keeps cold start separate from warmed HTTP and full integration, reports slower Rust cases, and treats public-runner latency as informational. See [RUST_SIDECAR_PERFORMANCE.md](RUST_SIDECAR_PERFORMANCE.md).

3.9.0 added `POST /rag/vectors/rank-binary` beside the fully compatible JSON endpoint. The fixed little-endian `f64` contract uses strict checked length/bounds validation and a fixed 24-byte response. Enable it only with `DEEPSEEK_RUST_RAG=1` plus `DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=binary`; the default and invalid-value fallback are `json`, there is no `auto` mode, a binary failure goes directly to Python without a second JSON sidecar request, and the complete Python parity scan remains mandatory. The 110-valid/16-malformed release-sidecar corpus is documented in [RAG_VECTOR_BINARY_TRANSPORT.md](RAG_VECTOR_BINARY_TRANSPORT.md).

3.10.0 removes the JSON decode and candidate list-of-lists rebuild from that binary path when cache rows contain valid `f64le-v1` BLOBs. New rows dual-write JSON and BLOB from one rounded vector; legacy or corrupt rows fall back only for that row, and one lookup still makes at most one Rust binary request before direct Python fallback. Use `python scripts/migrate_semantic_cache_embeddings.py --dry-run` to inspect an old database and add `--write --batch-size 100 --verify` for an explicit resumable backfill. The JSON column is never removed, so rollback to an older version remains readable. See [SEMANTIC_CACHE_BINARY_EMBEDDINGS.md](SEMANTIC_CACHE_BINARY_EMBEDDINGS.md).

```text
cd rust
cargo run -p deepseek-gateway
```

3.2.1 也提供独立、可选的 Docker 部署，不会改变默认 Python Compose：

```bash
docker compose -f docker-compose.rust.yml up --build -d
python scripts/smoke_rust_sidecar.py
```

若要与 Python 服务同时启动，显式叠加两个 Compose 文件；只有在 `.env` 中把对应 `DEEPSEEK_RUST_*` flag 设为 `1`，Python 才会连接 sidecar：

```bash
docker compose -f docker-compose.yml -f docker-compose.rust.yml up --build -d
```

Rust Policy 仍默认关闭。显式启用时，`DEEPSEEK_RUST_POLICY_FAILURE_MODE` 支持 `fallback`（默认，回到 Python Policy）、`deny`（后端异常时拒绝）和 `error`（返回结构化 503）；策略拒绝会携带稳定 `code`、`decision_id` 和 `trace_id`，审计日志不会记录凭据、查询参数或完整工作区路径。

默认监听 `127.0.0.1:8787`，提供：

- `GET /healthz` — 健康探针
- `GET /v1/models` — OpenAI-compatible 模型列表
- `POST /v1/chat/completions` — 请求校验 + 确定性本地 stub 响应

```bash
curl http://127.0.0.1:8787/healthz

curl http://127.0.0.1:8787/v1/models

curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}]}'
```

3.0.3 设计原则：**先让 Rust Gateway 能独立站起来**，暂时不替换 Python FastAPI 网关、不转发真实 DeepSeek API、不实现流式（`stream: true` 会返回结构化错误）。后续小版本会逐步接入 upstream proxy、MCP 处理、Tool Policy 等能力。

> 完整运维说明（启动 sidecar、feature flags、fallback、排错、回滚、验证命令）见 [RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md)；当前 3.2.x 门禁见 [RELEASE_READINESS_3_1_X.md](RELEASE_READINESS_3_1_X.md)，4.0 RC blocker matrix 见 [4_0_RC_READINESS.md](4_0_RC_READINESS.md)。

详见 [RUST_MIGRATION_ROADMAP.md](RUST_MIGRATION_ROADMAP.md)。
