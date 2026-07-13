# Hybrid Rust Runtime Runbook

This runbook covers day-to-day operation of the DeepSeek Infra 3.6.0 hybrid Rust runtime: how to start or containerize the Rust sidecar, verify the complete hybrid system, enable individual components, understand fallback behavior, troubleshoot common failures, and roll back to the Python runtime.

> **Scope**: every Rust component remains opt-in. 3.6.0 adds deterministic, credential-free MCP protocol preparation; Python still owns transports, authentication, sessions, capabilities, registries, tools, resources, prompts, tracing, and business state. The default Python image, default-disabled Rust flags, Python fallback, and `docker compose up` behavior are unchanged. The published `v4.0.0-rc.1` remains a historical architecture preview, not the active stable line.

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
| `POST /gateway/request/prepare` | `DEEPSEEK_RUST_GATEWAY=1` | Credential-free deterministic non-streaming request preparation |
| `POST /mcp/request/prepare` | `DEEPSEEK_RUST_MCP=1` | Deterministic MCP JSON-RPC preparation; routing owner remains Python |
| `POST /policy/url` | `DEEPSEEK_RUST_POLICY=1` | URL allow/deny decision |
| `POST /policy/path` | `DEEPSEEK_RUST_POLICY=1` | Path traversal guard decision |
| `POST /policy/capability` | `DEEPSEEK_RUST_POLICY=1` | Capability/risk decision |
| `POST /rag/query/normalize` | `DEEPSEEK_RUST_RAG=1` | Query normalization |
| `POST /rag/chunks/score` | `DEEPSEEK_RUST_RAG=1` | Chunk scoring/ranking |
| `POST /rag/vectors/rank` | `DEEPSEEK_RUST_RAG=1` | Semantic-cache batch vector ranking |
| `POST /rag/citation/format` | `DEEPSEEK_RUST_RAG=1` | Citation string formatting |
| `POST /rag/index/validate` | `DEEPSEEK_RUST_RAG=1` | Index metadata validation |

> **Note**: Streaming, model listing, provider routing, upstream HTTP, credentials, retry/backoff, MCP transports/sessions, tool execution, resources/prompts, and tracing lifecycle always stay on the Python path. Rust MCP never executes tools.

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

The smoke first checks the healthy system through Python boundaries: Rust status, Gateway request preparation followed by Python HTTP to an offline upstream stub, MCP initialize/list/call after Rust preparation with Python-owned tool results, stable invalid MCP error categories, Tool Policy private-URL denial, and RAG CJK normalization/exact ranking/citation. It then stops `rust-gateway` and proves all four paths fall back to Python, including the same MCP requests and error category. The script is intentionally destructive to the test sidecar, so do not run it against a shared deployment.

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
| `DEEPSEEK_RUST_RAG_TIMEOUT_MS` | `3000` | RAG proxy timeout in milliseconds |

### Example: enable only the Gateway

```bash
cd rust
cargo run -p deepseek-gateway &

DEEPSEEK_RUST_GATEWAY=1 \
DEEPSEEK_RUST_MCP=0 \
DEEPSEEK_RUST_POLICY=0 \
DEEPSEEK_RUST_RAG=0 \
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
python -m deepseek_infra.app
```

---

## Fallback behavior

Every Rust component has a Python equivalent. Gateway and RAG retain their corresponding `*_FALLBACK` switches; MCP always computes the Python preparation result first and uses it for any Rust backend failure, malformed response, sensitive-field injection, changed tool arguments, routing-owner change, or semantic divergence. Policy uses `DEEPSEEK_RUST_POLICY_FAILURE_MODE` so backend failure can fall back, deny, or return an error explicitly.

| Component | Default recovery | Fail-closed behavior |
| --- | --- | --- |
| **Gateway preparation** | Python prepares the same request, then continues normal Python execution | Returns `502 Bad Gateway` with `UPSTREAM_FAILURE` |
| **MCP preparation** | Use the already-computed Python protocol result, then route/execute in Python | User protocol errors remain their stable JSON-RPC/internal category and are never disguised as fallback |
| **Policy** | `fallback`: Python Tool Policy re-evaluates the call | `deny`: block execution; `error`: return a structured 503 |
| **RAG** | Python RAG hot-path runs locally | The Rust result is ignored and Python continues |

A "Rust call failure" includes any of these conditions:

- Sidecar is not running or unreachable
- TCP/HTTP connection timeout (exceeds `*_TIMEOUT_MS`)
- Non-2xx HTTP response from the sidecar
- Malformed JSON or missing expected fields in the response
- Invalid timeout value in the environment variable (falls back to the default `3000` ms)

For MCP, an empty body, non-object response, missing contract fields, unknown message type, unsafe routing owner, non-serializable value, changed `tools/call` arguments, or any Python/Rust semantic difference is also a backend failure. Diagnostics contain only method, message type, request ID type, payload size, runtime, fallback state/reason, and latency; they never contain full params, tool arguments, credentials, prompt content, or local paths.

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
   ```

2. Restart the Python process (or launcher) so all workers pick up the new environment.

3. Confirm via `GET /api/rust/status` that all `enabled` flags are `false`.

No data migration is required: Rust components are stateless proxies and delegates; all persistent state lives in the Python runtime directories.

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

Alternatively, verify end-to-end RAG via the evaluation harness:

```bash
python evals/runners/run_rag_eval.py
```

---

## Related documents

- [Release readiness checklist](RELEASE_READINESS_3_1_X.md)
- [Rust migration roadmap](RUST_MIGRATION_ROADMAP.md)
- [Implementation status](IMPLEMENTATION_STATUS.md)
