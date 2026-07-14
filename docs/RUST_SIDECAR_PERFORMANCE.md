# Rust Sidecar Release Performance and Observability

Applicable version: v3.9.0.

## Decision summary

DeepSeek Infra 3.9.0 adds no Rust delegate and changes no ownership boundary. It extends the 3.8.0 reproducible release-mode benchmark with an explicit JSON-versus-compact-binary comparison for the existing vector-ranking delegate. Python remains the default and authoritative runtime, every Rust flag remains disabled by default, the vector transport still defaults to JSON, default Compose remains Python-only, full defensive Python parity and fallback remain in place, and persistence remains Python-owned.

The measurements are evidence, not an enablement decision. Compact binary materially reduces serialization and warmed HTTP cost for the measured dense medium/large vectors, but it is larger than JSON for the tiny tie-heavy input and full Python validation remains mandatory. The current data is therefore insufficient to enable Rust or select binary automatically or by default.

## Audit findings

- Gateway, MCP, Tool Policy, and RAG Python clients used the standard-library `urllib` path and created a fresh HTTP connection for every delegation. The clients now share a bounded, process-local `http.client` pool while retaining per-component timeouts and all connection, timeout, HTTP, empty-body, malformed-response, and parity-divergence fallback behavior.
- The pool is thread-safe, can be reset by tests or operators, does not retain request bodies or user headers, is cleared after fork/PID change, closes at process exit, and never forwards caller Authorization or API-key headers to preparation endpoints.
- The Rust Dockerfile already used `cargo build --release --locked` and copied `target/release/deepseek-gateway`. The former 3.7.0 document-preparation benchmark could target a debug sidecar and did not separate startup, warm HTTP, pure core, and full integration costs; it is historical evidence only.
- The sidecar had tracing but no Prometheus endpoint before 3.8.0. Version 3.8.0 added `GET /metrics` on the same listener; 3.9.0 reuses it and adds only a fixed-label vector transport counter.
- Existing endpoint defenses remain in force: Gateway request preparation rejects bodies above 16,000,000 bytes; MCP preparation rejects bodies above 2,000,000 bytes and JSON depth above 32; document preparation rejects bodies above 40,000,000 bytes, JSON depth above 24, and document text above 8,000,000 characters. Size checks run before JSON parsing where applicable.

## Measured endpoints and components

| Delegate family | Real endpoint | Metric component |
| --- | --- | --- |
| Gateway request preparation | `POST /gateway/request/prepare` | `gateway_prepare` |
| MCP protocol preparation | `POST /mcp/request/prepare` | `mcp_prepare` |
| Tool Policy evaluation | `POST /policy/url`, `POST /policy/path`, `POST /policy/capability` | `policy_url`, `policy_path`, `policy_capability` |
| RAG vector ranking | `POST /rag/vectors/rank` (JSON), `POST /rag/vectors/rank-binary` (compact binary) | `rag_vector_rank` |
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
  --evidence-out docs/evidence/rust-sidecar-performance-v3.9.0.json
```

The report records Rust profile and version, target triple, Python version, operating system, logical CPU count, commit SHA, warmups, iterations, concurrency, input/output sizes, requests per second, median, p95, p99, minimum, maximum, errors, fallbacks, and observable connection counts. Repository evidence removes host-specific detail and never stores prompts, messages, tool arguments, document text, URLs, paths, credentials, tokens, or user metadata.

Every delegate scenario retains four independent layers:

1. `pythonBaseline`: direct authoritative Python implementation, without HTTP.
2. `pureRustCore`: the release helper invokes the relevant Rust core function without Axum, TCP, or JSON transport timing.
3. `releaseSidecarHttp`: a healthy, warmed release sidecar over a pre-established persistent connection; process startup is excluded.
4. `fullPythonIntegration`: the real Python delegate client, including defensive equality/parity validation.

The four vector scenarios additionally report `pythonDirect`, `pureRustCore`, `jsonSerialization`, `binarySerialization`, `warmJsonHttp`, `warmBinaryHttp`, `fullJsonIntegration`, and `fullBinaryIntegration`. Serialization/transport/Rust processing/full medians and p95s remain separate. Binary success responses must be exactly 24 bytes, semantic parity must pass, all errors/fallbacks must be zero, and the equivalent 1000 × 1536 binary request must be smaller than JSON.

Cold process launch, health readiness, and the first request are reported separately under `coldStart` and are never averaged into warm results. Warmups are executed but excluded from all summary statistics. The suite starts one sidecar per run, enforces hard timeouts and bounded iteration/concurrency values, and fails its contract checks on missing delegates, semantic divergence, errors, fallbacks, or sensitive report content.

The 26 scenarios cover minimal/multi-turn/tools-heavy/multipart/invalid Gateway inputs; initialize, tools/list, small/large tools/call, and invalid JSON-RPC MCP inputs; allow/deny URL, path, and capability policy inputs; four vector scales including ties; and small, medium, large, high-overlap, CJK-heavy, and non-BMP-heavy document inputs. Representative scenarios also run at concurrency 1, 8, and 32.

## Committed 3.9.0 vector transport result

The committed Windows x86_64 evidence uses Rust 1.96.1, Python 3.13.5, 20 logical CPUs, five measured iterations, two excluded warmups, and concurrency 1/8/32. It records the exact measured commit and full machine details; this document intentionally avoids treating those absolute values as cross-machine gates. Every row below had semantic parity, zero errors, and zero fallbacks. Times are median/p95 microseconds.

| Scenario | JSON bytes → binary bytes | JSON serialization | Binary serialization | Warm JSON HTTP | Warm binary HTTP | Full JSON | Full binary |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 × 384 | 61,212 → 52,240 | 5,064 / 5,266 | 1,853 / 2,117 | 7,115 / 7,355 | 3,098 / 3,275 | 9,719 / 10,490 | 5,238 / 5,513 |
| 128 × 768 | 927,803 → 792,592 | 75,363 / 83,002 | 28,165 / 31,429 | 95,193 / 131,860 | 34,849 / 46,807 | 132,055 / 139,518 | 59,289 / 76,164 |
| 1000 × 1536 | 14,399,713 → 12,300,304 | 1,239,560 / 1,324,321 | 641,243 / 660,238 | 1,453,591 / 1,514,543 | 601,468 / 758,639 | 689,275 / 768,267 | 372,460 / 410,224 |
| tie-heavy (3 × 16) | 288 → 528 | 6 / 6 | 7 / 7 | 757 / 891 | 283 / 447 | 1,033 / 1,227 | 911 / 1,168 |

The result is deliberately not uniformly favorable:

- The three dense scale scenarios reduced request bytes by about 14.6%, and binary serialization, warm HTTP, and full integration were lower on this machine.
- The tiny tie-heavy request demonstrates the opposite size result: fixed-width binary was 528 bytes versus 288 bytes for sparse JSON, and serialization was slightly slower (7 µs versus 6 µs). This is direct evidence against automatic selection.
- At 1000 × 1536, pure Rust core remained much faster than Python direct (42,489 µs versus 406,347 µs), and binary reduced the measured full integration to 372,460 µs. The full path still computes Python parity; that cost is an intentional safety requirement.
- Binary is not compression. Dense six-decimal JSON shrinks by about 14.6%, not by an order of magnitude, and results will vary with number formatting and vector sparsity.
- Cold startup remained separate (18,917 µs process launch, 143,433 µs health readiness, 25,048 µs first request) and contributes to no warm value.

These local results justify an explicit binary option for large vector ranking. They do not justify Rust default enablement, a binary default, `auto` selection, sampled parity, or removing JSON/fallback.

## Timing diagnostics

Every Python delegate exposes these observational fields without including them in semantic parity:

```text
pythonPreparationUs
serializationUs
transportUs
rustProcessingUs
pythonValidationUs
totalDelegateUs
transportEncoding
requestPayloadBytes
responsePayloadBytes
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
vector_rank_transport_total{encoding,outcome}
```

Components are restricted to the seven values in the endpoint table. Transport encoding is restricted to `json|binary`; outcomes are `success`, `client_error`, or `server_error`; backend reasons are `timeout`, `unavailable`, `malformed_response`, or `internal`. Dimensions, candidate counts, model names, MCP methods, tool names, document IDs, URLs/hosts, paths, request IDs, raw errors, vector values, and user input are never metric labels.

Sidecar logs may contain only component, payload/response byte counts, duration, bounded outcome/error code, and a system-generated or strictly validated correlation ID. They do not contain prompts, messages, tool arguments, document/chunk text, URL queries, file paths, Authorization, API keys, tokens, or user metadata. The default release filter is `deepseek_gateway=info`.

## CI policy and interpretation

The `rust-sidecar-performance` CI job builds and runs the release binary and publishes complete machine-local results. It strictly gates schema, delegate/transport layers, semantic parity, zero errors/fallbacks, fixed 24-byte binary responses, large-payload reduction, persistent-sidecar use, report redaction, and bounded complexity behavior. Absolute cross-run latency is informational because public runners are not stable performance hosts. Only deterministic complexity ratios use deliberately wide thresholds; vector ranking/encoding must remain consistent with scalar count, and document preparation must avoid obvious quadratic growth or overlap loops.

The committed evidence is [rust-sidecar-performance-v3.9.0.json](evidence/rust-sidecar-performance-v3.9.0.json). Its per-scenario values, including slower Rust cases, are authoritative.

## What this milestone does not prove

- It does not prove that Rust is faster on every scenario or machine.
- It does not prove that sidecar HTTP delegation is worthwhile for small payloads.
- It does not prove binary is smaller or faster for sparse/tiny inputs, or justify automatic transport selection.
- It does not justify default enablement, default sidecar deployment, removal of Python validation/fallback, or an ownership migration.
- It does not cover Gateway streaming/upstream HTTP, MCP transport/tool execution, file reading/OCR/embeddings, SQLite/index writes, or Python-owned persistence.
- It does not create a 4.0 RC, a 4.0.0 stable release, a tag, or a GitHub Release.
