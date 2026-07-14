# Rust Sidecar Release Performance and Observability

Applicable version: v3.8.0.

## Decision summary

DeepSeek Infra 3.8.0 adds no Rust delegate and changes no ownership boundary. It establishes a reproducible release-mode benchmark and low-cardinality sidecar observability for the five existing delegate families. Python remains the default and authoritative runtime, every Rust flag remains disabled by default, default Compose remains Python-only, defensive Python parity checks and fallback remain in place, and persistence remains Python-owned.

The measurements are evidence, not an enablement decision. In the audited workloads, pure Rust can reduce compute time for selected document-preparation and policy cases, while HTTP/JSON and Python defensive validation dominate many small requests. Vector ranking also depends on workload size and dimensionality; Rust is not assumed to win. The current data is therefore insufficient to enable any Rust delegate by default.

## Audit findings

- Gateway, MCP, Tool Policy, and RAG Python clients used the standard-library `urllib` path and created a fresh HTTP connection for every delegation. The clients now share a bounded, process-local `http.client` pool while retaining per-component timeouts and all connection, timeout, HTTP, empty-body, malformed-response, and parity-divergence fallback behavior.
- The pool is thread-safe, can be reset by tests or operators, does not retain request bodies or user headers, is cleared after fork/PID change, closes at process exit, and never forwards caller Authorization or API-key headers to preparation endpoints.
- The Rust Dockerfile already used `cargo build --release --locked` and copied `target/release/deepseek-gateway`. The former 3.7.0 document-preparation benchmark could target a debug sidecar and did not separate startup, warm HTTP, pure core, and full integration costs; it is historical evidence only.
- The sidecar had tracing but no Prometheus endpoint. Version 3.8.0 extends the same listener with `GET /metrics`; it does not create a second metrics service.
- Existing endpoint defenses remain in force: Gateway request preparation rejects bodies above 16,000,000 bytes; MCP preparation rejects bodies above 2,000,000 bytes and JSON depth above 32; document preparation rejects bodies above 40,000,000 bytes, JSON depth above 24, and document text above 8,000,000 characters. Size checks run before JSON parsing where applicable.

## Measured endpoints and components

| Delegate family | Real endpoint | Metric component |
| --- | --- | --- |
| Gateway request preparation | `POST /gateway/request/prepare` | `gateway_prepare` |
| MCP protocol preparation | `POST /mcp/request/prepare` | `mcp_prepare` |
| Tool Policy evaluation | `POST /policy/url`, `POST /policy/path`, `POST /policy/capability` | `policy_url`, `policy_path`, `policy_capability` |
| RAG vector ranking | `POST /rag/vectors/rank` | `rag_vector_rank` |
| RAG document preparation | `POST /rag/documents/prepare` | `rag_document_prepare` |

## Release benchmark contract

The formal build is exactly:

```bash
cargo build \
  --release \
  --locked \
  --manifest-path rust/Cargo.toml \
  -p deepseek-gateway
```

Run the bounded suite with:

```bash
python scripts/run_rust_sidecar_benchmarks.py \
  --iterations 5 \
  --warmups 2 \
  --concurrency 1,8,32 \
  --artifact-out artifacts/rust-sidecar-performance.json \
  --evidence-out docs/evidence/rust-sidecar-performance-v3.8.0.json
```

The report records Rust profile and version, target triple, Python version, operating system, logical CPU count, commit SHA, warmups, iterations, concurrency, input/output sizes, requests per second, median, p95, p99, minimum, maximum, errors, fallbacks, and observable connection counts. Repository evidence removes host-specific detail and never stores prompts, messages, tool arguments, document text, URLs, paths, credentials, tokens, or user metadata.

Each scenario is measured as four independent layers:

1. `pythonBaseline`: direct authoritative Python implementation, without HTTP.
2. `pureRustCore`: the release helper invokes the relevant Rust core function without Axum, TCP, or JSON transport timing.
3. `releaseSidecarHttp`: a healthy, warmed release sidecar over a pre-established persistent connection; process startup is excluded.
4. `fullPythonIntegration`: the real Python delegate client, including defensive equality/parity validation.

Cold process launch, health readiness, and the first request are reported separately under `coldStart` and are never averaged into warm results. Warmups are executed but excluded from all summary statistics. The suite starts one sidecar per run, enforces hard timeouts and bounded iteration/concurrency values, and fails its contract checks on missing delegates, semantic divergence, errors, fallbacks, or sensitive report content.

The 26 scenarios cover minimal/multi-turn/tools-heavy/multipart/invalid Gateway inputs; initialize, tools/list, small/large tools/call, and invalid JSON-RPC MCP inputs; allow/deny URL, path, and capability policy inputs; four vector scales including ties; and small, medium, large, high-overlap, CJK-heavy, and non-BMP-heavy document inputs. Representative scenarios also run at concurrency 1, 8, and 32.

## Timing diagnostics

Every Python delegate exposes these observational fields without including them in semantic parity:

```text
pythonPreparationUs
serializationUs
transportUs
rustProcessingUs
pythonValidationUs
totalDelegateUs
```

An unavailable measurement is `null`. `rustProcessingUs` comes from a sidecar response header and is treated only as telemetry: Python never uses it for security, fallback, routing, parity, or business decisions.

## Metrics and safe tracing

`GET /metrics` exports only bounded labels:

```text
requests_total{component,outcome}
request_duration_seconds{component}
request_payload_bytes{component}
response_payload_bytes{component}
backend_errors_total{component,reason}
```

Components are restricted to the seven values in the endpoint table. Outcomes are `success`, `client_error`, or `server_error`; backend reasons are `timeout`, `unavailable`, `malformed_response`, or `internal`. Model names, MCP methods, tool names, document IDs, URLs/hosts, paths, request IDs, raw errors, and user input are never metric labels.

Sidecar logs may contain only component, payload/response byte counts, duration, bounded outcome/error code, and a system-generated or strictly validated correlation ID. They do not contain prompts, messages, tool arguments, document/chunk text, URL queries, file paths, Authorization, API keys, tokens, or user metadata. The default release filter is `deepseek_gateway=info`.

## CI policy and interpretation

The `rust-sidecar-performance` CI job builds and runs the release binary and publishes complete machine-local results. It strictly gates schema, delegate coverage, semantic parity, zero errors/fallbacks, persistent-sidecar use, report redaction, and bounded complexity behavior. Absolute cross-run latency is informational because public runners are not stable performance hosts. Only deterministic complexity ratios use deliberately wide thresholds; vector ranking must remain consistent with `O(candidates × dimensions)`, and document preparation must avoid obvious quadratic growth or overlap loops.

The committed evidence is [rust-sidecar-performance-v3.8.0.json](evidence/rust-sidecar-performance-v3.8.0.json). Its per-scenario values, including slower Rust cases, are authoritative. A final five-delegate median/p95 summary is added to this document from that same run during release evidence refresh.

## What this milestone does not prove

- It does not prove that Rust is faster on every scenario or machine.
- It does not prove that sidecar HTTP delegation is worthwhile for small payloads.
- It does not justify default enablement, default sidecar deployment, removal of Python validation/fallback, or an ownership migration.
- It does not cover Gateway streaming/upstream HTTP, MCP transport/tool execution, file reading/OCR/embeddings, SQLite/index writes, or Python-owned persistence.
- It does not create a 4.0 RC, a 4.0.0 stable release, a tag, or a GitHub Release.
