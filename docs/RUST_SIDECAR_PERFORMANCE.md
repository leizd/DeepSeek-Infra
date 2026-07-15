# Rust Sidecar Release Performance and Observability

Applicable version: v3.10.0.

## Decision summary

DeepSeek Infra 3.10.0 adds no Rust delegate and changes no ownership boundary. It extends the 3.9.0 JSON-versus-compact-binary benchmark with Python-owned SQLite JSON/BLOB fetch, validation, direct BLOB assembly, mixed-row, full shadow integration, and database-size layers. Python remains the default and authoritative runtime, every Rust flag remains disabled by default, the vector transport still defaults to JSON, default Compose remains Python-only, full defensive Python parity and fallback remain in place, and persistence remains Python-owned.

The measurements are evidence, not an enablement decision. Compact binary materially reduces serialization and warmed HTTP cost for the measured dense medium/large vectors, but it is larger than JSON for the tiny tie-heavy input and full Python validation remains mandatory. The current data is therefore insufficient to enable Rust or select binary automatically or by default.

## Audit findings

- Gateway, MCP, Tool Policy, and RAG Python clients used the standard-library `urllib` path and created a fresh HTTP connection for every delegation. The clients now share a bounded, process-local `http.client` pool while retaining per-component timeouts and all connection, timeout, HTTP, empty-body, malformed-response, and parity-divergence fallback behavior.
- The pool is thread-safe, can be reset by tests or operators, does not retain request bodies or user headers, is cleared after fork/PID change, closes at process exit, and never forwards caller Authorization or API-key headers to preparation endpoints.
- The Rust Dockerfile already used `cargo build --release --locked` and copied `target/release/deepseek-gateway`. The former 3.7.0 document-preparation benchmark could target a debug sidecar and did not separate startup, warm HTTP, pure core, and full integration costs; it is historical evidence only.
- The sidecar had tracing but no Prometheus endpoint before 3.8.0. Version 3.8.0 added `GET /metrics` on the same listener; 3.9.0 reused it and added only a fixed-label vector transport counter. Version 3.10.0 changes no Rust endpoint or metric label.
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
  --evidence-out docs/evidence/rust-sidecar-performance-v3.10.0.json
```

The report records Rust profile and version, target triple, Python version, operating system, logical CPU count, commit SHA, warmups, iterations, concurrency, input/output sizes, requests per second, median, p95, p99, minimum, maximum, errors, fallbacks, and observable connection counts. Repository evidence removes host-specific detail and never stores prompts, messages, tool arguments, document text, URLs, paths, credentials, tokens, or user metadata.

Every delegate scenario retains four independent layers:

1. `pythonBaseline`: direct authoritative Python implementation, without HTTP.
2. `pureRustCore`: the release helper invokes the relevant Rust core function without Axum, TCP, or JSON transport timing.
3. `releaseSidecarHttp`: a healthy, warmed release sidecar over a pre-established persistent connection; process startup is excluded.
4. `fullPythonIntegration`: the real Python delegate client, including defensive equality/parity validation.

The vector scenarios additionally report `pythonDirect`, `pureRustCore`, `jsonSerialization`, `binarySerialization`, `warmJsonHttp`, `warmBinaryHttp`, `fullJsonIntegration`, and `fullBinaryIntegration`. Version 3.10.0 also reports `sqliteJsonFetch`, `legacyJsonDecode`, `listBinaryAssembly`, `sqliteBlobFetch`, `blobValidation`, `directBlobAssembly`, `warmBinaryHttpFromLists`, `warmBinaryHttpFromBlobs`, `fullShadowIntegrationFromJson`, `fullShadowIntegrationFromBlobs`, `pythonDirectFromJson`, and `pythonDirectFromBlobArrays`. Fetch, decode, validation, assembly, transport, Rust processing, Python validation, and total medians/p95s remain separate.

Strict gates require list and BLOB encoders to produce byte-for-byte identical requests, semantic parity for every row, zero errors, zero unexpected fallbacks, no vector content in the report, no JSON decode or candidate list-of-lists in the direct path, and faster direct BLOB assembly than legacy JSON decode plus list assembly in the same large `1000 × 1536` run. Absolute latency and speedup ratios remain informational. The report records JSON-only and dual-write SQLite bytes and their delta rather than hiding storage overhead.

Cold process launch, health readiness, and the first request are reported separately under `coldStart` and are never averaged into warm results. Warmups are executed but excluded from all summary statistics. The suite starts one sidecar per run, enforces hard timeouts and bounded iteration/concurrency values, and fails its contract checks on missing delegates, semantic divergence, errors, fallbacks, or sensitive report content.

The 26 scenarios cover minimal/multi-turn/tools-heavy/multipart/invalid Gateway inputs; initialize, tools/list, small/large tools/call, and invalid JSON-RPC MCP inputs; allow/deny URL, path, and capability policy inputs; four vector scales including ties; and small, medium, large, high-overlap, CJK-heavy, and non-BMP-heavy document inputs. Representative scenarios also run at concurrency 1, 8, and 32.

## Historical committed 3.9.0 vector transport result

The committed Windows x86_64 evidence uses Rust 1.96.1, Python 3.13.5, 20 logical CPUs, five measured iterations, two excluded warmups, and concurrency 1/8/32. It records the exact measured commit and full machine details; this document intentionally avoids treating those absolute values as cross-machine gates. Every row below had semantic parity, zero errors, and zero fallbacks. Times are median/p95 microseconds.

| Scenario | JSON bytes → binary bytes | JSON serialization | Binary serialization | Warm JSON HTTP | Warm binary HTTP | Full JSON | Full binary |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 × 384 | 61,212 → 52,240 | 3,335 / 3,565 | 1,033 / 1,267 | 3,286 / 3,413 | 1,411 / 1,716 | 3,184 / 3,839 | 2,200 / 2,300 |
| 128 × 768 | 927,803 → 792,592 | 36,863 / 40,098 | 14,038 / 15,622 | 46,302 / 64,966 | 15,637 / 15,877 | 56,710 / 60,660 | 32,573 / 39,759 |
| 1000 × 1536 | 14,399,713 → 12,300,304 | 1,305,382 / 1,399,884 | 579,717 / 612,676 | 1,431,674 / 1,524,849 | 689,536 / 717,219 | 1,791,355 / 1,919,616 | 1,035,570 / 1,078,017 |
| tie-heavy (3 × 16) | 288 → 528 | 46 / 49 | 48 / 55 | 1,484 / 1,805 | 1,448 / 1,664 | 1,656 / 1,771 | 1,765 / 1,815 |

The result is deliberately not uniformly favorable:

- The three dense scale scenarios reduced request bytes by about 14.6%, and binary serialization, warm HTTP, and full integration were lower on this machine.
- The tiny tie-heavy request demonstrates the opposite size result: fixed-width binary was 528 bytes versus 288 bytes for sparse JSON, serialization was slightly slower (48 µs versus 46 µs), and full binary integration was slower (1,765 µs versus 1,656 µs). This is direct evidence against automatic selection.
- At 1000 × 1536, pure Rust core remained faster than Python direct (75,564 µs versus 168,363 µs), and binary reduced the measured full integration from 1,791,355 µs to 1,035,570 µs. The full path still computes Python parity; that cost is an intentional safety requirement.
- Binary is not compression. Dense six-decimal JSON shrinks by about 14.6%, not by an order of magnitude, and results will vary with number formatting and vector sparsity.
- Cold startup remained separate (397,497 µs process launch, 572,211 µs health readiness, 2,083 µs first request) and contributes to no warm value.

These 3.9.0 local results justified an explicit binary option for large vector ranking. They did not justify Rust default enablement, a binary default, `auto` selection, sampled parity, or removing JSON/fallback.

## 3.10.0 semantic-cache storage comparison

The 3.10.0 evidence adds the requested `16 × 384`, `128 × 768`, `1000 × 1536`, and mixed BLOB/legacy scenarios. Each scenario compares SQLite JSON fetch/decode/list assembly with SQLite BLOB fetch/validation/direct assembly, warmed binary HTTP from lists and BLOBs, full shadow integration from both representations, and direct Python ranking from decoded JSON versus BLOB-backed arrays. The `databaseBytes` block reports the JSON-only database, dual-write database, byte increase, and percent increase.

The committed evidence file is the source of truth for measured values. It retains slower cases and storage overhead, and it does not set an absolute millisecond gate on public runners.

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

The committed 3.10.0 evidence is [rust-sidecar-performance-v3.10.0.json](evidence/rust-sidecar-performance-v3.10.0.json). Its per-scenario values, including slower paths and database overhead, are authoritative. The earlier compact-transport baseline remains preserved as [rust-sidecar-performance-v3.9.0.json](evidence/rust-sidecar-performance-v3.9.0.json).

## What this milestone does not prove

- It does not prove that Rust is faster on every scenario or machine.
- It does not prove that sidecar HTTP delegation is worthwhile for small payloads.
- It does not prove binary is smaller or faster for sparse/tiny inputs, or justify automatic transport selection.
- It does not justify default enablement, default sidecar deployment, removal of Python validation/fallback, or an ownership migration.
- It does not cover Gateway streaming/upstream HTTP, MCP transport/tool execution, file reading/OCR/embeddings, SQLite/index writes, or Python-owned persistence.
- It does not create a 4.0 RC, a 4.0.0 stable release, a tag, or a GitHub Release.
