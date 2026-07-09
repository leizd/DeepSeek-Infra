# Hybrid Rust Runtime Runbook

This runbook covers day-to-day operation of the DeepSeek Infra 3.1.x hybrid Rust runtime: how to start the Rust sidecar, enable individual components, understand fallback behavior, troubleshoot common failures, and roll back to the Python runtime.

> **Scope**: 3.1.x only. The default behavior of 3.1.x is intentionally conservative: every Rust component is opt-in and falls back to the existing Python implementation by default. No Docker or packaging changes are required to use this runbook.

---

## Table of Contents

1. [Default behavior](#default-behavior)
2. [Starting the Rust Gateway sidecar](#starting-the-rust-gateway-sidecar)
3. [Feature flags](#feature-flags)
4. [Fallback behavior](#fallback-behavior)
5. [Common errors and troubleshooting](#common-errors-and-troubleshooting)
6. [Rollback](#rollback)
7. [Verification commands](#verification-commands)

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
| `GET /v1/models` | `DEEPSEEK_RUST_GATEWAY=1` | OpenAI-compatible model list |
| `POST /v1/chat/completions` | `DEEPSEEK_RUST_GATEWAY=1` | Non-streaming chat completion proxy |
| `POST /mcp` | `DEEPSEEK_RUST_MCP=1` | MCP JSON-RPC message handler |
| `POST /policy/url` | `DEEPSEEK_RUST_POLICY=1` | URL allow/deny decision |
| `POST /policy/path` | `DEEPSEEK_RUST_POLICY=1` | Path traversal guard decision |
| `POST /policy/capability` | `DEEPSEEK_RUST_POLICY=1` | Capability/risk decision |
| `POST /rag/query/normalize` | `DEEPSEEK_RUST_RAG=1` | Query normalization |
| `POST /rag/chunks/score` | `DEEPSEEK_RUST_RAG=1` | Chunk scoring/ranking |
| `POST /rag/citation/format` | `DEEPSEEK_RUST_RAG=1` | Citation string formatting |
| `POST /rag/index/validate` | `DEEPSEEK_RUST_RAG=1` | Index metadata validation |

> **Note**: Streaming chat requests (`stream: true`) always stay on the Python path, even when `DEEPSEEK_RUST_GATEWAY=1`.

---

## Feature flags

Each Rust component has its own opt-in flag. All flags accept the same truthy/falsy values:

- **Enabled**: `1`, `true`, `yes`, `on`
- **Disabled**: `0`, `false`, `no`, `off`

### Component flags

| Flag | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_RUST_GATEWAY` | `0` | Proxy `/v1/chat/completions` (non-streaming) and `/v1/models` to Rust |
| `DEEPSEEK_RUST_MCP` | `0` | Handle `POST /mcp` JSON-RPC in Rust |
| `DEEPSEEK_RUST_POLICY` | `0` | Delegate URL/path/capability policy checks to Rust |
| `DEEPSEEK_RUST_RAG` | `0` | Delegate query normalization, chunk scoring, citation formatting, and index validation to Rust |

### Configuration flags

| Flag | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_RUST_GATEWAY_URL` | `http://127.0.0.1:8787` | Base URL of the Rust Gateway sidecar |
| `DEEPSEEK_RUST_GATEWAY_FALLBACK` | `1` | Fall back to Python Gateway if Rust fails |
| `DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS` | `3000` | Gateway proxy timeout in milliseconds |
| `DEEPSEEK_RUST_MCP_FALLBACK` | `1` | Fall back to Python MCP if Rust fails |
| `DEEPSEEK_RUST_MCP_TIMEOUT_MS` | `3000` | MCP proxy timeout in milliseconds |
| `DEEPSEEK_RUST_POLICY_FALLBACK` | `1` | Fall back to Python Tool Policy if Rust fails |
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

Every Rust component has a Python equivalent. When a Rust flag is enabled but the Rust call fails, the Python app chooses one of the following behaviors based on the corresponding `*_FALLBACK` flag.

| Component | Fallback enabled | Fallback disabled |
| --- | --- | --- |
| **Gateway** | Python Gateway handles the request | Returns `502 Bad Gateway` with `UPSTREAM_FAILURE` |
| **MCP** | Python MCP handler processes the JSON-RPC message | Returns `502 Bad Gateway` with `UPSTREAM_FAILURE` |
| **Policy** | Python Tool Policy re-evaluates the decision | The tool call is blocked with the Rust error reason |
| **RAG** | Python RAG hot-path runs locally | The Rust result is ignored and Python continues |

A "Rust call failure" includes any of these conditions:

- Sidecar is not running or unreachable
- TCP/HTTP connection timeout (exceeds `*_TIMEOUT_MS`)
- Non-2xx HTTP response from the sidecar
- Malformed JSON or missing expected fields in the response
- Invalid timeout value in the environment variable (falls back to the default `3000` ms)

> **Default is safe**: all `*_FALLBACK` flags default to `1`, so enabling a Rust component cannot break a working 3.0.x deployment.

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

**Diagnosis**: The request was delegated to Rust Policy and the sidecar returned `Deny`.

**Remediation**:

- Verify the tool call is safe (URL, path, capability).
- If the deny is unexpected, disable Rust Policy to confirm Python Tool Policy behavior:

  ```bash
  DEEPSEEK_RUST_POLICY=0
  ```

- If Python allows the same call, the Rust Policy rule may need tuning; report it as a bug with the deny reason.

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

---

## Verification commands

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

With `DEEPSEEK_RUST_GATEWAY=1` and a healthy sidecar, the model list is served by Rust. With the flag off, it is served by Python.

### MCP path

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(cat .auth-token)" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

With `DEEPSEEK_RUST_MCP=1`, the JSON-RPC message is handled by Rust. With the flag off, it is handled by Python.

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
