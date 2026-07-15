# Hybrid Rust Runtime Runbook

This runbook covers day-to-day operation of the stable DeepSeek Infra 4.0.0 Python-first hybrid runtime: how to start or containerize the optional Rust sidecar, verify the complete hybrid system, enable individual delegates, understand fallback behavior, troubleshoot failures, and roll back to the Python-only runtime.

> **Scope**: every Rust component and the binary vector path remain explicit opt-ins. Python is default and authoritative, default Compose is Python-only, and fallback is supported throughout 4.x. Python owns SQLite, uploads, paths, parsing, OCR, embeddings, indexes, retrieval, authorization, upstream HTTP/credentials/retries, Gateway streaming, MCP transports/sessions/tools, tracing, and business state. Binary failures fall directly to Python without retrying the JSON Rust endpoint. `v4.0.0-rc.1` is superseded and historical; Rust-primary is not enabled. See [docs/ARCHITECTURE.md](ARCHITECTURE.md).

---

## Table of Contents

1. [Default behavior](#default-behavior)
2. [Starting the Rust Gateway sidecar](#starting-the-rust-gateway-sidecar)
3. [Optional Docker deployment](#optional-docker-deployment)
4. [Hybrid runtime E2E smoke](#hybrid-runtime-e2e-smoke)
5. [Feature flags](#feature-flags)
6. [Fallback behavior](#fallback-behavior)
7. [Common errors and troubleshooting](#common-errors-and-troubleshooting)
8. [Rollback](#rollback)
9. [Verification commands](#verification-commands)

---

## Default behavior

Out of the box, all Rust components are **disabled**:

```bash
DEEPSEEK_RUST_GATEWAY=0
DEEPSEEK_RUST_MCP=0
DEEPSEEK_RUST_POLICY=0
DEEPSEEK_RUST_RAG=0
DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json
DEEPSEEK_RUST_RAG_DOCUMENT_PREP=0
```

When a component is disabled, the Python FastAPI runtime handles the request exactly as it did in 3.0.x. No Rust sidecar needs to be running for the application to work.

The Rust sidecar and feature flags are discovered through `GET /api/rust/status` (see [Verification commands](#verification-commands)).

---

## Starting the Rust Gateway sidecar

The Rust Gateway sidecar is a single binary that hosts the Gateway, MCP, Policy, and RAG endpoints. It is built from the workspace under `rust/`.

```bash
cd rust
cargo run -p deepseek-gateway
```

By default it listens on `http://127.0.0.1:8787`. You can point the Python app at a different URL:

```bash
DEEPSEEK_RUST_GATEWAY_URL=http://127.0.0.1:8787
```

The sidecar exposes the following endpoints used by the Python app:

| Endpoint | Used by | Purpose |
| --- | --- | --- |
| `GET /healthz` | Health probe | Liveness check for the sidecar |
| `GET /metrics` | Prometheus scraper | Bounded release observability on the existing listener |
| `POST /gateway/request/prepare` | `DEEPSEEK_RUST_GATEWAY=1` | Credential-free deterministic non-streaming request preparation |
| `POST /mcp/request/prepare` | `DEEPSEEK_RUST_MCP=1` | Deterministic MCP JSON-RPC preparation; routing owner remains Python |
| `POST /policy/url` | `DEEPSEEK_RUST_POLICY=1` | URL allow/deny decision |
| `POST /policy/path` | `DEEPSEEK_RUST_POLICY=1` | Path traversal guard decision |
| `POST /policy/capability` | `DEEPSEEK_RUST_POLICY=1` | Capability/risk decision |
| `POST /rag/query/normalize` | `DEEPSEEK_RUST_RAG=1` | Query normalization |
| `POST /rag/chunks/score` | `DEEPSEEK_RUST_RAG=1` | Chunk scoring/ranking |
| `POST /rag/vectors/rank` | `DEEPSEEK_RUST_RAG=1` | Semantic-cache batch vector ranking |
| `POST /rag/vectors/rank-binary` | `DEEPSEEK_RUST_RAG=1` plus explicit binary transport | Compact little-endian `f64` vector ranking; errors remain structured JSON |
| `POST /rag/citation/format` | `DEEPSEEK_RUST_RAG=1` | Citation string formatting |
| `POST /rag/index/validate` | `DEEPSEEK_RUST_RAG=1` | Index metadata validation |
| `POST /rag/documents/prepare` | `DEEPSEEK_RUST_RAG_DOCUMENT_PREP=1` | Normalize/chunk text already parsed by Python; no file I/O or persistence |

> **Note**: Streaming, model listing, provider routing, upstream HTTP, credentials, retry/backoff, MCP transports/sessions, tool execution, resources/prompts, file parsing/OCR, embeddings, persistence/indexes, retrieval, and tracing lifecycle always stay on the Python path. Rust MCP never executes tools and Rust RAG never reads files or writes an index.

For release-equivalent local runs, build the exact locked binary instead of using `cargo run`:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml -p deepseek-gateway
rust/target/release/deepseek-gateway
```

Python reuses a bounded process-local connection pool (`DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS`, default 32; hard maximum 128) and caps buffered responses (`DEEPSEEK_RUST_SIDECAR_MAX_RESPONSE_BYTES`, default 16 MiB; hard maximum 64 MiB). Component-specific request timeouts are unchanged. Tests and launchers may call the transport reset hook; a fork or PID change always discards inherited connections.

Each delegate reports `pythonPreparationUs`, `serializationUs`, `transportUs`, `rustProcessingUs`, `pythonValidationUs`, and `totalDelegateUs`. A field is `null` when it cannot be measured accurately. `rustProcessingUs` is observational sidecar telemetry and is never trusted for parity, security, routing, fallback, or business logic.

---

## Optional Docker deployment

3.2.1 adds a multi-stage, non-root Rust image. It contains only the compiled `deepseek-gateway` binary and its runtime health-check dependency; it does not install or copy the Python application.

Build and start only the sidecar:

```bash
docker compose -f docker-compose.rust.yml up --build -d
python scripts/smoke_rust_sidecar.py --base-url http://127.0.0.1:8787
```

Start the normal Python service plus the optional sidecar:

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.rust.yml up --build -d
```

This starts both containers but still leaves Python on every request path because `.env.example` sets all Rust flags to `0`. To opt in, set the required flags in `.env` and use the Compose service URL:

```bash
DEEPSEEK_RUST_GATEWAY=1
DEEPSEEK_RUST_MCP=1
DEEPSEEK_RUST_POLICY=1
DEEPSEEK_RUST_RAG=1
DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json
DEEPSEEK_RUST_RAG_DOCUMENT_PREP=1
DEEPSEEK_RUST_GATEWAY_URL=http://rust-gateway:8787
```

These settings are deployment choices, not defaults. A plain `docker compose up -d` continues to build and run only the Python service.

---

## Hybrid runtime E2E smoke

The 3.2.2 test overlay is for CI and local verification only. It enables all Rust delegates, keeps all Python fallbacks enabled, and disables local token auth inside the loopback-only test deployment.

```bash
cp .env.example .env
docker compose \
  -f docker-compose.yml \
  -f docker-compose.hybrid-test.yml \
  up --detach --build

python scripts/smoke_hybrid_runtime.py

docker compose \
  -f docker-compose.yml \
  -f docker-compose.hybrid-test.yml \
  down --volumes --remove-orphans
```

The smoke first checks the healthy system through Python boundaries: Rust status, Gateway request preparation followed by Python HTTP to an offline upstream stub, MCP initialize/list/call after Rust preparation with Python-owned tool results, stable invalid MCP error categories, Tool Policy private-URL denial, RAG CJK normalization/exact ranking/citation, and a real Python file-ingestion call whose parsed text is prepared in Rust and persisted/read in Python. Its semantic-cache probe creates a fresh SQLite database, writes multiple dual-format rows through the production store path, adds a legacy JSON-only row and a corrupt-BLOB/valid-JSON row, then performs one mixed lookup. It proves exactly one binary request, no JSON Rust request, direct BLOB assembly, fixed storage diagnostics, and Rust/Python parity without external models. It then stops `rust-gateway` and proves every path—including the same mixed lookup—falls back to Python with no second sidecar attempt and identical results. The script is intentionally destructive to the test sidecar, so do not run it against a shared deployment.

No real API key, external model call, or external network service is required. The test overlay injects a fake key and points Python at a deterministic local upstream stub. Safe response diagnostics prove Rust preparation on the healthy path and Python preparation after sidecar loss.

---

## Feature flags

Each Rust component has its own opt-in flag. All flags accept the same truthy/falsy values:

- **Enabled**: `1`, `true`, `yes`, `on`
- **Disabled**: `0`, `false`, `no`, `off`

### Component flags

| Flag | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_RUST_GATEWAY` | `0` | Delegate deterministic non-streaming request preparation to Rust |
| `DEEPSEEK_RUST_MCP` | `0` | Compare Python's local MCP preparation with Rust, then continue routing/execution in Python |
| `DEEPSEEK_RUST_POLICY` | `0` | Delegate URL/path/capability policy checks to Rust |
| `DEEPSEEK_RUST_RAG` | `0` | Delegate query normalization, chunk scoring, semantic-cache vector ranking, citation formatting, and index validation to Rust |
| `DEEPSEEK_RUST_RAG_DOCUMENT_PREP` | `0` | Compare Python's local preparation with Rust for text already parsed by Python; Python still persists/indexes chunks |

### Configuration flags

| Flag | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_RUST_GATEWAY_URL` | `http://127.0.0.1:8787` | Base URL of the Rust Gateway sidecar |
| `DEEPSEEK_RUST_GATEWAY_FALLBACK` | `1` | Fall back to Python Gateway if Rust fails |
| `DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS` | `3000` | Gateway request-preparation timeout in milliseconds |
| `DEEPSEEK_RUST_MCP_FALLBACK` | `1` | Legacy compatibility setting; 3.6.0 always uses the already-computed Python result for Rust backend failure or divergence |
| `DEEPSEEK_RUST_MCP_TIMEOUT_MS` | `3000` | MCP preparation timeout in milliseconds |
| `DEEPSEEK_RUST_POLICY_FAILURE_MODE` | `fallback` | Policy backend failure behavior: `fallback`, `deny`, or `error` |
| `DEEPSEEK_RUST_POLICY_FALLBACK` | unset | Legacy compatibility switch; `0` maps to `deny`, otherwise `fallback` |
| `DEEPSEEK_RUST_POLICY_TIMEOUT_MS` | `3000` | Policy proxy timeout in milliseconds |
| `DEEPSEEK_RUST_RAG_FALLBACK` | `1` | Fall back to Python RAG if Rust fails |
| `DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT` | `json` | `json` or explicit `binary`; invalid values fail closed to JSON and there is no `auto` mode |
| `DEEPSEEK_RUST_RAG_TIMEOUT_MS` | `3000` | RAG proxy timeout in milliseconds |

### Example: enable only the Gateway

```bash
cd rust
cargo run -p deepseek-gateway &

DEEPSEEK_RUST_GATEWAY=1 \
DEEPSEEK_RUST_MCP=0 \
DEEPSEEK_RUST_POLICY=0 \
DEEPSEEK_RUST_RAG=0 \
DEEPSEEK_RUST_RAG_DOCUMENT_PREP=0 \
python -m deepseek_infra.app
```

### Example: enable everything (all flags on)

```bash
cd rust
cargo run -p deepseek-gateway &

DEEPSEEK_RUST_GATEWAY=1 \
DEEPSEEK_RUST_MCP=1 \
DEEPSEEK_RUST_POLICY=1 \
DEEPSEEK_RUST_RAG=1 \
DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json \
DEEPSEEK_RUST_RAG_DOCUMENT_PREP=1 \
python -m deepseek_infra.app
```

### Example: explicitly enable compact binary vector ranking

```bash
DEEPSEEK_RUST_RAG=1 \
DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=binary \
python -m deepseek_infra.app
```

The media type is `application/vnd.deepseek.vector-rank.v1+octet-stream`. See [RAG_VECTOR_BINARY_TRANSPORT.md](RAG_VECTOR_BINARY_TRANSPORT.md) for the fixed wire layout, bounds, stable errors, 110-case parity corpus, and size/performance evidence.

---

## Fallback behavior

Every Rust component has a Python equivalent. Gateway and existing RAG hot paths retain their corresponding `*_FALLBACK` switches; MCP and RAG document preparation always compute the Python result first and use it for any Rust backend failure, malformed response, sensitive-field injection, changed contract, or semantic divergence. Policy uses `DEEPSEEK_RUST_POLICY_FAILURE_MODE` so backend failure can fall back, deny, or return an error explicitly.

| Component | Default recovery | Fail-closed behavior |
| --- | --- | --- |
| **Gateway preparation** | Python prepares the same request, then continues normal Python execution | Returns `502 Bad Gateway` with `UPSTREAM_FAILURE` |
| **MCP preparation** | Use the already-computed Python protocol result, then route/execute in Python | User protocol errors remain their stable JSON-RPC/internal category and are never disguised as fallback |
| **Policy** | `fallback`: Python Tool Policy re-evaluates the call | `deny`: block execution; `error`: return a structured 503 |
| **RAG** | Python RAG hot-path runs locally; binary failure does not retry the JSON sidecar | The Rust result is ignored and Python continues |
| **RAG document preparation** | Use the already-computed Python chunks, then embed/persist/index in Python | Invalid user/config input retains its stable category; malformed Rust output never reaches persistence |

A "Rust call failure" includes any of these conditions:

- Sidecar is not running or unreachable
- TCP/HTTP connection timeout (exceeds `*_TIMEOUT_MS`)
- Non-2xx HTTP response from the sidecar
- Malformed JSON or missing expected fields in the response
- Invalid timeout value in the environment variable (falls back to the default `3000` ms)

For binary vector ranking this also includes wrong Content-Type, empty/non-24-byte response, wrong magic/reserved field, out-of-range index, non-finite similarity, or index/similarity divergence from the complete Python ranking. These conditions perform no second Rust JSON request. Diagnostics contain only encoding, dimensions/counts, scalar/payload sizes, fixed error category, timing, and system correlation ID; they never contain vector values.

For MCP, an empty body, non-object response, missing contract fields, unknown message type, unsafe routing owner, non-serializable value, changed `tools/call` arguments, or any Python/Rust semantic difference is also a backend failure. Logs contain only bounded component, message/request-ID type, payload/response size, runtime/outcome, fallback reason, duration, and system correlation ID; they never contain the MCP method, full params, tool arguments, credentials, prompt content, or local paths.

For RAG document preparation, invalid/missing document or chunk fields, offset drift, text/range mismatch, duplicate IDs, invalid hashes, metadata expansion, a changed document ID, sensitive fields, or any semantic difference are backend failures. Logs contain only bounded component, counts, payload/response sizes, runtime/outcome, fallback reason, duration, and system correlation ID; they never contain a document ID/hash, document/chunk text, filenames, paths, raw bytes, credentials, or private metadata.

> **Default is safe**: Rust Policy defaults to `fallback`, and the Python Tool Policy is attached even to bare tool calls after a Rust backend failure. A sidecar outage therefore cannot silently skip policy evaluation.

### Rust Policy failure modes

| Mode | Sidecar failure behavior | Tool execution |
| --- | --- | --- |
| `fallback` | Re-evaluate with Python Tool Policy | Runs only if Python Policy allows it |
| `deny` | Return `policy_backend_unavailable` as a structured denial | Never runs |
| `error` | Return `policy_backend_unavailable` with `status: 503` | Never runs |

An invalid mode falls back to `fallback`. `DEEPSEEK_RUST_POLICY_FALLBACK=0` remains supported as a compatibility alias for `deny`.

---

## Common errors and troubleshooting

### Sidecar unreachable

**Symptom**: `GET /api/rust/status` shows `gateway.healthy: false`, or requests return a `502` / `UPSTREAM_FAILURE` when fallback is disabled.

**Diagnosis**:

```bash
curl http://127.0.0.1:8787/healthz
```

If this fails, the sidecar is not running or is listening on a different address.

**Remediation**:

```bash
cd rust
cargo run -p deepseek-gateway
```

Verify `DEEPSEEK_RUST_GATEWAY_URL` matches the sidecar's actual bind address.

### Timeout

**Symptom**: Requests to Rust-enabled paths intermittently fall back to Python, or return `502` when fallback is disabled.

**Diagnosis**: Check logs for timeout messages and compare against the configured `*_TIMEOUT_MS`.

**Remediation**:

- Increase the timeout for the affected component:

  ```bash
  DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS=5000
  DEEPSEEK_RUST_MCP_TIMEOUT_MS=5000
  DEEPSEEK_RUST_POLICY_TIMEOUT_MS=5000
  DEEPSEEK_RUST_RAG_TIMEOUT_MS=5000
  ```

- Ensure the sidecar is not overloaded or blocked by a firewall.

### Malformed response

**Symptom**: Rust returns `200` but the Python app ignores the body and falls back.

**Diagnosis**: Call the sidecar endpoint directly and inspect the JSON shape. For example, for RAG query normalization:

```bash
curl -X POST http://127.0.0.1:8787/rag/query/normalize \
  -H "Content-Type: application/json" \
  -d '{"query":"hello world"}'
```

Expected: `{"normalized": "..."}`.

**Remediation**: Update the Rust sidecar to the same version/commit as the Python app; the protocol models must match.

### Non-2xx response

**Symptom**: `502` from Python, or sidecar returns `400` / `500` directly.

**Diagnosis**: Hit the sidecar directly to see the raw error:

```bash
curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-pro","messages":[]}'
```

**Remediation**: Fix the payload or the sidecar implementation. If the sidecar is not yet production-ready for a given path, disable that component's flag or leave fallback enabled.

### Rust policy deny

**Symptom**: A tool call is blocked with a reason mentioning `Rust Policy` or `PolicyDecision`.

**Diagnosis**: The request was delegated to Rust Policy and the sidecar returned `allowed: false`. The tool response includes a stable `code`, `decision_id`, and the correlated `trace_id`.

Example decision:

```json
{
  "allowed": false,
  "code": "private_network_blocked",
  "reason": "private network addresses are not allowed",
  "decision_id": "pd_...",
  "trace_id": "trace_...",
  "capability": "NetworkFetch",
  "risk_level": "High"
}
```

Each allow, deny, and backend-failure outcome emits a `tool_policy_decision` structured log. URL targets keep only scheme, host, port, and path; credentials, query values, authorization values, complete tool arguments, and workspace roots are omitted or redacted. No audit database is introduced in 3.2.3.

Stable codes in this milestone are `unsupported_scheme`, `localhost_blocked`, `private_network_blocked`, `link_local_blocked`, `path_traversal`, `protected_path`, `missing_capability`, `risk_limit_exceeded`, `invalid_policy_request`, and `policy_backend_unavailable`.

**Remediation**:

- Verify the tool call is safe (URL, path, capability).
- If the deny is unexpected, disable Rust Policy to confirm Python Tool Policy behavior:

  ```bash
  DEEPSEEK_RUST_POLICY=0
  ```

- If Python allows the same call, the Rust Policy rule may need tuning; report it with the stable code and decision identifier, never with secrets or complete sensitive arguments.

---

## Rollback

To immediately return to the pure Python 3.0.x runtime:

1. Set all Rust flags to disabled:

   ```bash
   export DEEPSEEK_RUST_GATEWAY=0
   export DEEPSEEK_RUST_MCP=0
   export DEEPSEEK_RUST_POLICY=0
   export DEEPSEEK_RUST_RAG=0
   export DEEPSEEK_RUST_RAG_DOCUMENT_PREP=0
   ```

2. Restart the Python process (or launcher) so all workers pick up the new environment.

3. Confirm via `GET /api/rust/status` that all `enabled` flags are `false`.

No data migration is required to upgrade or roll back: Rust components are stateless proxies and delegates, the legacy JSON embedding remains present, and all persistent state lives in the Python runtime directories. New semantic-cache rows dual-write JSON and `f64le-v1` BLOBs automatically. To backfill old rows explicitly, first run `python scripts/migrate_semantic_cache_embeddings.py --dry-run --database <path>`, then use `--write --batch-size 100 --verify` during a maintenance window. The migration is batched and resumable; it never deletes JSON. See [SEMANTIC_CACHE_BINARY_EMBEDDINGS.md](SEMANTIC_CACHE_BINARY_EMBEDDINGS.md).

For Docker deployments, disable the flags first and then remove the optional sidecar:

```bash
docker compose -f docker-compose.rust.yml down
```

---

## Verification commands

### Standalone sidecar smoke

```bash
python scripts/smoke_rust_sidecar.py --base-url http://127.0.0.1:8787
```

The smoke is offline and requires no model or API key. It checks `/healthz`, `/v1/models`, non-streaming chat shape, MCP initialize, localhost policy denial, and CJK RAG normalization.

### Full hybrid runtime smoke

```bash
python scripts/smoke_hybrid_runtime.py
```

This command expects the Compose stack from [Hybrid runtime E2E smoke](#hybrid-runtime-e2e-smoke) and stops the Rust sidecar while testing fallback. Use `--keep-sidecar` to run only the healthy-path checks.

### Hybrid runtime status

```bash
curl http://127.0.0.1:8000/api/rust/status \
  -H "Authorization: Bearer $(cat .auth-token)"
```

Expected response shape:

```json
{
  "enabled": {
    "gateway": true,
    "mcp": false,
    "policy": false,
    "rag": false
  },
  "components": {
    "gateway": {
      "enabled": true,
      "url": "http://127.0.0.1:8787",
      "healthy": true
    }
  }
}
```

### Gateway path

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $(cat .auth-token)"
```

`GET /v1/models` is always served by Python. With `DEEPSEEK_RUST_GATEWAY=1` and a healthy sidecar, only credential-free preparation of non-streaming upstream request bodies is delegated to Rust.

### MCP path

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(cat .auth-token)" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

With `DEEPSEEK_RUST_MCP=1`, Python prepares the message locally, compares it with Rust `/mcp/request/prepare`, and adopts only a contract-identical descriptor whose owner is still Python. The existing Python MCP server then performs routing and any real tool execution exactly once. With the flag off, the same Python preparation and execution path runs without contacting Rust.

### RAG path

Run the offline RAG demo, which exercises query normalization and chunk scoring:

```bash
python examples/local_rag_demo.py
```

With `DEEPSEEK_RUST_RAG=1`, the hot paths are delegated to the Rust sidecar. With the flag off, Python RAG handles them.

With the independent `DEEPSEEK_RUST_RAG_DOCUMENT_PREP=1`, the upload path parses files in Python and sends only parsed text, allowlisted metadata, and the current chunk configuration to `/rag/documents/prepare`. Python adopts only a contract-identical result and continues to compute embeddings, persist chunks, and update indexes. Verify the deterministic boundary with:

```bash
python scripts/check_rag_document_preparation_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report docs/evidence/rag-document-preparation-parity-v4.0.0.json
```

Alternatively, verify end-to-end RAG via the evaluation harness:

```bash
python evals/runners/run_rag_eval.py
```

---

## Related documents

- [Release readiness checklist](RELEASE_READINESS_3_1_X.md)
- [Rust migration roadmap](RUST_MIGRATION_ROADMAP.md)
- [Implementation status](IMPLEMENTATION_STATUS.md)
- [Rust sidecar performance and observability](RUST_SIDECAR_PERFORMANCE.md)
- [RAG document preparation parity](RAG_DOCUMENT_PREPARATION_PARITY.md)
