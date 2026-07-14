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

## Committed release result

The committed Windows x86_64 evidence was produced from commit `7d52af19005d4d05d70af5cbc764cf2355782f5d` with Rust 1.96.1, Python 3.13.5, 20 logical CPUs, five measured iterations, two excluded warmups, and concurrency 1/8/32. The table uses the first required scenario for each delegate family and reports median/p95 in microseconds; every shown layer had zero errors and the full integration layer had zero fallbacks.

| Delegate / representative scenario | Python baseline median / p95 | Pure Rust median / p95 | Warm HTTP median / p95 | Full integration median / p95 |
| --- | ---: | ---: | ---: | ---: |
| Gateway / minimal request | 18.8 / 25.94 | 7.0 / 18.04 | 263.2 / 296.24 | 331.3 / 379.16 |
| MCP / initialize | 21.3 / 28.68 | 9.8 / 18.92 | 359.1 / 572.70 | 421.8 / 663.94 |
| Policy / safe public URL | 10.7 / 16.92 | 4.2 / 12.62 | 362.2 / 408.24 | 764.0 / 1003.28 |
| Vector ranking / 16 × 384 | 321.8 / 328.94 | 114.3 / 159.30 | 1010.7 / 1261.34 | 1471.0 / 1679.66 |
| Document preparation / small | 263.8 / 466.62 | 33.7 / 53.96 | 2128.2 / 2477.26 | 2593.0 / 2728.72 |

Cold startup was separate: process launch 7,127 µs, health readiness 93,782 µs, and first request 948 µs. None of these values contributes to the warm table.

The full evidence is deliberately not uniformly favorable to delegation:

- Pure Rust was faster than direct Python in 25 of 26 scenarios on this machine. The exception was Gateway tools-heavy preparation (159.6 µs Rust vs 132.9 µs Python), small enough that the difference should not drive architecture.
- Warm sidecar HTTP beat direct Python only for MCP large nested arguments (8,346.1 µs vs 10,275.0 µs). For every Gateway, Policy, vector-ranking, and document-preparation scenario, TCP/HTTP/JSON overhead erased the core benefit.
- Full Python-to-Rust integration remained slower for every valid representative workload because Python still computes or validates the authoritative result. That cost is intentional defense, not benchmark noise to remove. The invalid Gateway result was locally rejected before delegation and its tiny timing difference is not evidence of Rust benefit.
- Large vector ranking showed a strong pure-core difference (43,769.8 µs Rust vs 349,158.7 µs Python at 1000 × 1536), but warm HTTP still measured 493,128.7 µs and full defensive integration 821,399.9 µs. Payload serialization, transfer, and Python parity therefore remain decisive.

These measurements do not make any current delegate worth enabling by default. They identify where batching or a future lower-overhead boundary might be investigated, while proving that the current HTTP boundary is mostly an observability/optionality mechanism rather than a general latency win.

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

The committed evidence is [rust-sidecar-performance-v3.8.0.json](evidence/rust-sidecar-performance-v3.8.0.json). Its per-scenario values, including slower Rust cases, are authoritative.

## What this milestone does not prove

- It does not prove that Rust is faster on every scenario or machine.
- It does not prove that sidecar HTTP delegation is worthwhile for small payloads.
- It does not justify default enablement, default sidecar deployment, removal of Python validation/fallback, or an ownership migration.
- It does not cover Gateway streaming/upstream HTTP, MCP transport/tool execution, file reading/OCR/embeddings, SQLite/index writes, or Python-owned persistence.
- It does not create a 4.0 RC, a 4.0.0 stable release, a tag, or a GitHub Release.
