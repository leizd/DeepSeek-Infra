# Rust Core Migration Roadmap

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


This document records the incremental path to stable `4.0.0`, promoted from the validated `4.0.0-rc.2` Python-first hybrid candidate after its observation period. The promotion does not change runtime ownership or defaults.

## Goal

DeepSeek Infra 4.0 stabilizes selected optional Rust infrastructure paths while preserving Python as the default, authoritative runtime for product integration, local-first UX, document/media tooling, protocols, persistence, and ecosystem-heavy features.

The target is not a full rewrite. The target is a hybrid architecture:

- Python remains the orchestration and product-integration layer.
- Rust becomes the stable core for high-concurrency, protocol-heavy, and security-sensitive infrastructure.
- CI enforces quality gates across both Python and Rust code.

## Release Target

DeepSeek Infra 4.0.0 release requirements:

1. Core infrastructure Rust workspace is present and CI-gated.
2. At least three Rust-backed infrastructure components are production-ready.
3. Python backend coverage gate is raised from 80% toward 95%.
4. Rust core coverage is measured and gated.
5. Existing security, eval, Docker, release-readiness, and docs checks continue to pass.
6. The implementation status matrix clearly identifies which modules are Python-backed, Rust-backed, or hybrid.

## Non-goals

The 3.0.x to 4.0.0 migration should avoid these traps:

- No full repository rewrite in one pass.
- No forced rewrite of document-generation and media parsing paths where Python has stronger ecosystem support.
- No removal of FastAPI routes until Rust replacements are tested behind stable compatibility boundaries.
- No coverage-number chasing without meaningful edge-case tests.

## Architecture Direction

```text
DeepSeek Infra 3.0.x
  Python FastAPI app
  Python Agent / RAG / MCP / Tool Runtime / Workspace
  Python tests with 80% coverage gate

DeepSeek Infra 4.0.0
  Python product layer + Rust infrastructure core
  Rust gateway / protocol / policy / retrieval components
  Python + Rust CI quality gates
  Backend core coverage target: >=95%
```

Recommended Rust workspace layout:

```text
rust/
  Cargo.toml
  crates/
    deepseek-core/
    deepseek-gateway/
    deepseek-mcp/
    deepseek-policy/
    deepseek-rag/
```

## 3.0.x Milestones

### 3.0.1 — Roadmap and CI foundation

Scope:

- Add this roadmap.
- Add initial Rust workspace skeleton.
- Add CI checks for Rust formatting, linting, and tests.
- Keep all existing Python behavior unchanged.

Quality gates:

- `cargo fmt --check`
- `cargo clippy --all-targets --all-features -- -D warnings`
- `cargo test --all`
- Existing Python CI remains unchanged.

### 3.0.2 — Rust protocol model foundation

Scope:

- Introduce shared Rust types for stable protocol payloads.
- Start with pure data-model crates before moving runtime logic.
- Cover JSON serialization/deserialization compatibility with fixtures.

Candidate crates:

- `deepseek-core`: common error, ID, timestamp, event, and JSON helpers.
- `deepseek-mcp`: MCP JSON-RPC request/response/tool schema types.
- `deepseek-a2a` or module under `deepseek-core`: Agent Card and task lifecycle types.

Quality gates:

- Round-trip fixture tests for protocol payloads.
- Invalid-payload tests.
- Snapshot tests where useful.

### 3.0.3 — Rust Gateway sidecar MVP

Scope:

- Add a Rust gateway sidecar that can run independently.
- Keep Python FastAPI as the default entrypoint.
- Implement the smallest useful API surface first.

Initial endpoints:

- `GET /healthz`
- `GET /v1/models`
- `POST /v1/chat/completions`

Rust stack suggestion:

- `tokio`
- `axum`
- `serde`
- `tower`
- `tower-http`
- `tracing`

Note: `reqwest` is intentionally deferred until the sidecar needs real upstream proxying (3.0.4 or later).

Quality gates:

- Unit tests for routing and payload validation.
- Streaming requests are explicitly rejected with a structured error in the MVP.
- Compatibility smoke test against the existing Python gateway contract.

### 3.0.4 — Rust MCP handler MVP

Scope:

- Move MCP JSON-RPC validation and core dispatch boundaries into Rust.
- Keep Python tool implementations available through a bridge.
- Focus on strict schema validation and deterministic errors.

Quality gates:

- Valid MCP request fixtures.
- Invalid method / invalid params / malformed JSON tests.
- Tool-list compatibility smoke test.

### 3.0.5 — Rust Tool Policy core

Scope:

- Move security-sensitive policy checks into Rust.
- Start with deterministic pure functions.

Candidate checks:

- URL allow/deny logic.
- SSRF/private-host blocking.
- Path normalization and path traversal prevention.
- Capability/risk-level matching.
- Audit event schema validation.

Quality gates:

- Adversarial URL tests.
- Windows and POSIX path traversal tests.
- Permission matrix tests.
- Regression corpus for known bypass patterns.

### 3.0.6 — Rust RAG hot-path MVP

Scope:

- Move pure retrieval hot paths into Rust.
- Keep Python document parsing and media extraction unless a Rust replacement is clearly better.

Candidate functions:

- Chunk metadata normalization.
- Query token normalization.
- Chunk scoring.
- Citation locator formatting.
- Index metadata validation.

Quality gates:

- Golden tests against Python output.
- Unicode/CJK query tests.
- Empty and malformed chunk tests.
- Large-input performance tests.

## 3.1.x Milestones

### 3.1.0 — Hybrid runtime integration foundation

Scope:

- Make Rust services discoverable from the Python app behind feature flags.
- Add runtime health status for Rust-backed components (`/api/rust/status`).
- Document fallback behavior and non-goals.

Feature flags (default disabled):

- `DEEPSEEK_RUST_GATEWAY=0` (configurable via `DEEPSEEK_RUST_GATEWAY_URL`)
- `DEEPSEEK_RUST_MCP=0`
- `DEEPSEEK_RUST_POLICY=0`
- `DEEPSEEK_RUST_RAG=0`

Implementation:

- `deepseek_infra/infra/rust_core/config.py`: flag parsing and Gateway URL.
- `deepseek_infra/infra/rust_core/registry.py`: component registry and status aggregation.
- `deepseek_infra/infra/rust_core/health.py`: HTTP health probe for the Rust Gateway sidecar.
- `deepseek_infra/web/routes/status.py`: `GET /api/rust/status` (read-only, auth-gated).

Quality gates:

- Default-disabled flag tests.
- Mocked Gateway health success / failure tests.
- Route registration and auth tests.
- No coverage gate increase; no breaking changes to existing Python routes.

Non-goals:

- Does not enable Rust components by default.
- Does not forward Python `/v1/chat/completions` to Rust.
- Does not Dockerize or auto-start Rust sidecars.
- Does not expose Python-to-Rust policy or RAG calls yet.

### 3.1.1 — Rust Gateway opt-in proxy integration

Scope:

- Route `/v1/chat/completions` and `/v1/models` to the Rust Gateway sidecar when `DEEPSEEK_RUST_GATEWAY=1`.
- Keep the existing Python path as the default and as a fallback.
- Preserve auth headers when forwarding.

Feature flags:

- `DEEPSEEK_RUST_GATEWAY=0` (default)
- `DEEPSEEK_RUST_GATEWAY_URL=http://127.0.0.1:8787`
- `DEEPSEEK_RUST_GATEWAY_FALLBACK=1` (default)
- `DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS=3000` (default)

Implementation:

- `deepseek_infra/infra/rust_core/gateway_client.py`: HTTP proxy client for Rust Gateway.
- `deepseek_infra/web/routes/chat.py`: opt-in routing for `/v1/chat/completions` and `/v1/models` with Python fallback.

Quality gates:

- Disabled-path test: still uses Python implementation.
- Enabled-path test: forwards to Rust Gateway.
- Failure-path tests: fallback to Python when enabled, structured error when disabled.
- Timeout test: short timeout triggers fallback.
- Streaming stays on Python path.
- Auth header preservation test.

Non-goals:

- Does not enable Rust Gateway by default.
- Does not route MCP, Policy, or RAG to Rust.
- Does not Dockerize or auto-start the sidecar.
- Does not raise coverage gate.

### 3.1.2 — Rust MCP opt-in route integration

Scope:

- Expose `POST /mcp` on the Rust Gateway sidecar backed by `deepseek-mcp`.
- Route Python `/mcp` JSON-RPC requests to the Rust sidecar when `DEEPSEEK_RUST_MCP=1`.
- Keep Python MCP as the default and as a fallback.

Feature flags:

- `DEEPSEEK_RUST_MCP=0` (default)
- `DEEPSEEK_RUST_MCP_FALLBACK=1` (default)
- `DEEPSEEK_RUST_MCP_TIMEOUT_MS=3000` (default)

Implementation:

- `rust/crates/deepseek-gateway/src/lib.rs`: add `POST /mcp` route.
- `rust/crates/deepseek-mcp/src/lib.rs`: re-export `handle_mcp_message`.
- `deepseek_infra/infra/rust_core/mcp_client.py`: HTTP proxy client.
- `deepseek_infra/web/routes/mcp.py`: opt-in delegation with fallback.

Quality gates:

- Disabled-path test: still uses Python MCP.
- Enabled-path tests: initialize, tools/list, tools/call forwarded to Rust.
- Failure-path tests: fallback to Python when enabled, structured error when disabled.
- Invalid payload returns structured error.
- Auth header preservation test.

Non-goals:

- Does not enable Rust MCP by default.
- Does not bridge Python tool execution into Rust yet.
- Does not Dockerize or auto-start the sidecar.
- Does not raise coverage gate.

### 3.1.3 — Rust Policy opt-in integration

Scope:

- Expose policy decision endpoints on the Rust Gateway sidecar backed by `deepseek-policy`.
- Delegate compatible URL, path, and capability checks from Python tool execution to Rust when `DEEPSEEK_RUST_POLICY=1`.
- Keep Python Tool Policy as the default and as a fallback.

Feature flags:

- `DEEPSEEK_RUST_POLICY=0` (default)
- `DEEPSEEK_RUST_POLICY_FALLBACK=1` (default)
- `DEEPSEEK_RUST_POLICY_TIMEOUT_MS=3000` (default)

Implementation:

- `rust/crates/deepseek-gateway/src/lib.rs`: add `POST /policy/url`, `/policy/path`, `/policy/capability`.
- `deepseek_infra/infra/rust_core/policy_client.py`: HTTP proxy client.
- `deepseek_infra/infra/tool_runtime/tools.py`: `execute_tool_call` Rust Policy boundary.

Quality gates:

- Disabled-path test: still uses Python policy.
- Enabled-path tests: safe URL allowed, private URL denied, path traversal denied, missing capability denied.
- Failure-path tests: fallback to Python when enabled, structured error when disabled.
- Rust sidecar endpoint tests for each policy guard.

Non-goals:

- Does not enable Rust Policy by default.
- Does not replace Python Tool Runtime or execute tools through Rust.
- Does not Dockerize or auto-start the sidecar.
- Does not raise coverage gate.

### 3.1.4 — Rust RAG opt-in integration

Scope:

- Expose RAG hot-path endpoints on the Rust Gateway sidecar backed by `deepseek-rag`.
- Delegate compatible query normalization, chunk scoring, and citation formatting from Python RAG retrieval to Rust when `DEEPSEEK_RUST_RAG=1`.
- Keep Python RAG as the default and as a fallback.

Feature flags:

- `DEEPSEEK_RUST_RAG=0` (default)
- `DEEPSEEK_RUST_RAG_FALLBACK=1` (default)
- `DEEPSEEK_RUST_RAG_TIMEOUT_MS=3000` (default)

Implementation:

- `rust/crates/deepseek-gateway/src/lib.rs`: add `POST /rag/query/normalize`, `/rag/chunks/score`, `/rag/citation/format`, `/rag/index/validate`.
- `deepseek_infra/infra/rust_core/rag_client.py`: HTTP proxy client.
- `deepseek_infra/infra/rag/local_rag.py`: `_search_db` query normalization / chunk scoring boundary and `chunk_lineage` citation formatting.

Quality gates:

- Disabled-path test: still uses Python RAG.
- Enabled-path tests: query normalization, chunk scoring, and citation formatting delegated to Rust.
- Failure-path tests: fallback to Python when Rust RAG is enabled but unreachable.
- CJK query preservation test.
- Rust sidecar endpoint tests for each RAG hot path.

Non-goals:

- Does not enable Rust RAG by default.
- Does not replace Python document parsing.
- Does not call embeddings from Rust.
- Does not use vector databases from Rust.
- Does not modify Docker.
- Does not raise coverage gate.

### 3.1.5 — Coverage uplift phase 1

Status: **in progress** (3.1.5). Raised coverage gate from 80% to 82%.

Scope:

- Harden the 3.1.x hybrid Rust runtime integration with fallback, timeout, malformed-response, and configuration-combination tests.
- Raise Python coverage gate from 80% to 82%.

Quality gates:

- All Rust feature flags have parsing tests for enabled/disabled variants.
- Gateway / MCP / Policy / RAG clients have tests for timeout, connection error, invalid JSON, unexpected status code, and missing expected fields.
- Fallback behavior tests cover unreachable sidecar, non-2xx responses, malformed policy decisions, and invalid timeout values.
- Hybrid combination tests cover all flags enabled and all flags disabled.
- Full test suite passes at or above the new 82% coverage gate with margin.

Non-goals:

- Does not add new Rust runtime features.
- Does not enable Rust components by default.
- Does not modify Docker.
- Does not replace Python runtime paths.
- Does not raise coverage gate to 85% yet.

### 3.1.6 — Release readiness documentation

Status: **in progress** (3.1.6).

Scope:

- Document how to operate the 3.1.x hybrid Rust runtime: sidecar startup, feature flags, fallback behavior, troubleshooting, rollback, and verification commands.
- Add a release-readiness checklist that gates the 3.1.x line without enabling Rust components by default.
- Update the implementation status matrix and README to point to the new runbook and checklist.

Deliverables:

- `docs/RUST_HYBRID_RUNTIME_RUNBOOK.md`: operational runbook for Gateway / MCP / Policy / RAG feature flags, fallback behavior, common errors, rollback, and verification commands.
- `docs/RELEASE_READINESS_3_1_X.md`: CI gates, runtime gates, release evidence, rollback checklist, and sign-off criteria for 3.1.x.
- `docs/IMPLEMENTATION_STATUS.md`: link to runbook/checklist (release version header remains v3.0.1).
- `README.md`: link to the runbook from the Rust Gateway section.
- `CHANGELOG.md`: 3.1.6 release entry.

Quality gates:

- All CI gates continue to pass (ruff, mypy, pytest --cov --cov-fail-under=82, cargo fmt, cargo clippy, cargo test, node --check, docs link check, security scans).
- All offline eval gates continue to pass with `--strict`.
- Runtime gates documented in `RELEASE_READINESS_3_1_X.md` have been executed manually or via the release-readiness job:
  - All Rust flags disabled.
  - Each Rust flag enabled individually.
  - All Rust flags enabled together.
  - Sidecar unavailable fallback.
  - Rust policy deny blocks unsafe tool call.
  - RAG CJK query preserved.

Non-goals:

- Does not enable Rust components by default.
- Does not modify Docker or packaging.
- Does not raise the Python coverage gate above 82%.
- Does not add 4.0.0 breaking changes.

### 3.1.7 — Pre-4.0 quality baseline

Status: **in progress** (3.1.7).

Scope:

- Audit the current state of the hybrid Rust runtime and document the gap to 4.0.0.
- Provide status matrices for Rust core, Python integration, coverage, and CI/release gates.
- List known gaps and propose a conservative 3.2.x milestone sequence.

Deliverables:

- `docs/PRE_4_0_QUALITY_BASELINE.md`: baseline audit with matrices, coverage status, gaps, and recommended 3.2.x milestones.
- `docs/RUST_MIGRATION_ROADMAP.md`: updated 3.1.7 section and 3.2.x placeholder.
- `CHANGELOG.md`: 3.1.7 entry.

Quality gates:

- All CI and release gates continue to pass unchanged.
- No coverage or eval regressions.
- Docs link check passes.

Non-goals:

- Does not add runtime features.
- Does not enable Rust components by default.
- Does not raise coverage gates.
- Does not declare 4.0.0 ready.

### 3.2.x — Coverage and parity work

Stable path (3.10.0 capabilities qualified in rc.2 and carried into 4.0.0):

- 3.2.0: Python coverage gate raised from 82% to 85%; full suite measured at 85.559% with no runtime or default-enable changes.
- 3.2.1: Multi-stage non-root Rust sidecar image, independent Compose file, offline endpoint smoke, and dedicated Docker CI job; still opt-in and separate from the default Python image.
- 3.2.2: Test-only hybrid Compose stack and offline E2E smoke covering all four Python-to-Rust delegates plus sidecar-loss fallback; defaults remain unchanged.
- 3.2.3: Stable Policy decision codes and identifiers, trace-preserving redacted audits, explicit backend failure modes, and execution-blocking tests; Rust remains opt-in.
- 3.2.4: Shared 38-case deterministic RAG corpus, strict live-sidecar parity gate, stable validation categories, and machine-readable difference reports; Rust remains opt-in.
- 3.2.5: Machine-readable 4.0 RC blocker matrix, owner sign-off checklist, JSON/terminal readiness report, and branch-aware CI enforcement. At that milestone, the decision was **NOT READY FOR 4.0.0-rc.1** because measured Python coverage was 85.63% versus 95.00% and architecture decisions were open.
- 3.3.0: ADR-0040 approves a Python-first hybrid 4.0 architecture: an empty Rust default-on set, Python-only default deployment, Python fallback through 4.x, Python-owned Gateway streaming and real MCP tool execution, plus Rust-owned MCP validation/routing. Architecture blockers are resolved; measured Python coverage remains the sole RC blocker.
- 3.3.1: Branch-aware, risk-weighted failure coverage raises measured Python coverage to a conservative 90.52% across two consecutive full runs and the CI gate to 90%. Branch coverage is recorded without a separate threshold; the 95% RC measured target remains unchanged and is the sole readiness blocker.
- 3.3.2: High-value failure tests raise combined statement-and-branch coverage to 95.3428% and 95.3396% across consecutive full runs and promote the CI gate to 95%. HIGH-risk debt decreases, coverage omit rules remain unchanged, and strict readiness reports READY without creating an RC tag.
- 3.4.0: The semantic-cache batch vector scan moves into the existing opt-in Rust RAG delegate through `POST /rag/vectors/rank`, with stable first-match tie behavior, strict Python response validation, diagnostics, and Python fallback. Cache storage and policy remain Python-owned.
- 3.5.0: Deterministic non-streaming Gateway request preparation moves behind the existing opt-in Gateway delegate through `POST /gateway/request/prepare`. A 68-case live-sidecar parity gate proves normalized request and stable error-category parity. Python retains credentials, provider routing, upstream HTTP, streaming, retries, cache policy, context injection, real tool execution, and tracing lifecycle.
- 3.6.0: Deterministic MCP envelope and method preparation moves behind the existing opt-in MCP delegate through `POST /mcp/request/prepare`. A 105-case live-sidecar parity gate proves normalized request/notification/response descriptors and stable error-category parity. Python retains transport, sessions, authentication, runtime capability decisions, registries, tool execution, resource/prompt loading, cancellation, scheduling, tracing, credentials, and business state.
- 3.7.0: Deterministic normalization and chunking of text already parsed by Python moves behind the independent default-disabled `DEEPSEEK_RUST_RAG_DOCUMENT_PREP` delegate through `POST /rag/documents/prepare`. A 125-case live-sidecar gate proves exact chunks, Unicode character offsets, overlap, BLAKE2b-96 hashes, chunk IDs, metadata boundaries, and stable errors. Python retains uploads, paths, parsing/OCR, embeddings, persistence, indexes, scheduling, authorization, retrieval, and context assembly.
- 3.8.0: No delegate is added. All five existing delegate families gain a locked release-mode layered benchmark, bounded persistent Python HTTP connections, per-layer timing, fixed-label metrics, and safe correlation tracing. Absolute public-runner latency is informational; semantic parity, zero error/fallback, redaction, connection lifecycle, and complexity contracts are merge gates. Python-first defaults and ownership remain unchanged.
- 3.9.0: The existing vector-ranking delegate gains an explicit compact little-endian `f64` endpoint beside compatible JSON. A 110-valid/16-malformed live-sidecar gate, fixed 24-byte response, checked bounds, direct Python fallback without JSON retry, and extended JSON/binary benchmark prove the contract. JSON remains the default; full Python parity and ownership are unchanged.
- 3.10.0: Python-owned semantic-cache storage retains the six-decimal JSON column and dual-writes the same normalized values as `f64le-v1` BLOBs. The binary path assembles the unchanged 3.9.0 request directly from valid SQLite BLOBs, handles mixed/legacy/corrupt rows per row, keeps complete Python parity, and provides an explicit batched/resumable migration tool. Startup never performs a full-table rewrite; Rust still does not read SQLite.

See [PRE_4_0_QUALITY_BASELINE.md](PRE_4_0_QUALITY_BASELINE.md) for the quality baseline and [4_0_RC_READINESS.md](4_0_RC_READINESS.md) for the current blocker matrix.

### 3.4.0 — Semantic-cache vector ranking (completed)

Selection rationale:

- The candidate scan is pure CPU work with a small JSON contract and no filesystem, database, network, or mutable runtime ownership.
- Its O(candidates x dimensions) loop benefits from Rust most as cache size or embedding dimensions grow.
- Exact-match handling, SQLite queries, TTL, namespaces, thresholds, and hit mutation stay in Python, limiting the blast radius.
- The existing `DEEPSEEK_RUST_RAG` flag and fallback contract can be reused without adding a new default-on surface.

See [RUST_CANDIDATE_AUDIT_3_4.md](RUST_CANDIDATE_AUDIT_3_4.md) for the evaluated alternatives and non-goals.

### 3.5.0 — Gateway request preparation (completed)

- The boundary is pure input-to-output work and reuses the existing sidecar and `DEEPSEEK_RUST_GATEWAY` flag.
- Rust returns a normalized request or a stable validation code; Python never parses natural-language text to determine an error category.
- Python validates every successful Rust result against its safe normalized contract before any upstream HTTP call.
- Backend failures fall back to Python when configured, while deterministic user-input errors remain input errors.
- The default runtime, default Compose file, streaming owner, provider owner, credential owner, and Python fallback contract do not change.

See [GATEWAY_REQUEST_PREPARATION_PARITY.md](GATEWAY_REQUEST_PREPARATION_PARITY.md) for the shared corpus, diagnostics, fallback rules, and non-goals.

### 3.6.0 — MCP protocol preparation (completed)

- The boundary is pure input JSON to a normalized protocol descriptor or stable error and reuses the existing sidecar and default-disabled `DEEPSEEK_RUST_MCP` flag.
- Python computes the local result before calling Rust and accepts only a JSON-serializable, contract-identical result whose routing owner remains Python.
- Backend failures and semantic divergence use the local Python result; deterministic user protocol errors preserve their stable category and are never reported as backend fallback.
- Rust validates only established Python MCP methods and preserves the repository's single-message, non-batch behavior. It never interprets tool arguments, receives credentials, loads resources/prompts, or executes tools.
- The 105-case shared corpus, Rust unit tests, Python defensive/fallback tests, Docker smoke, and hybrid E2E prove protocol parity, argument preservation, Python-only execution, and sidecar-loss recovery.
- The default runtime, default Compose file, Python fallback, and all transport/session/execution ownership remain unchanged.

See [MCP_PROTOCOL_PREPARATION_PARITY.md](MCP_PROTOCOL_PREPARATION_PARITY.md) for the shared corpus, stable error mapping, redacted diagnostics, fallback rules, and non-goals.

### 3.7.0 — RAG document preparation (completed)

- The boundary is parsed text plus allowlisted metadata and chunk configuration to a normalized document/chunk descriptor or stable error; it performs no file, database, index, network, model, or embedding I/O.
- Python computes the local contract before calling Rust and verifies the document ID, metadata, contiguous indexes, unique IDs, character offsets, text ranges, overlap, hashes, and full semantics before adopting a Rust result.
- The established Python algorithm remains the specification: CRLF/CR normalization, per-line trailing whitespace removal, paragraph/newline-aware 6,000-character windows, 400-character overlap, stripped chunk bodies, Python Unicode character offsets, BLAKE2b-96 lineage hashes, and deterministic IDs.
- Backend failures or malformed/divergent results use Python; deterministic input/configuration errors retain stable categories. Diagnostics and parity reports exclude document/chunk text, paths, bytes, credentials, and private metadata.
- The existing hybrid E2E proves real Python parsing, Rust preparation, Python persistence/readback, payload isolation, sidecar-loss fallback, and identical semantic chunk fingerprints.
- The independent flag, all other Rust delegates, and the sidecar deployment remain default-disabled. This is not Rust RAG ingestion and does not move any file, OCR, embedding, persistence, index, retrieval, or transaction ownership.

See [RAG_DOCUMENT_PREPARATION_PARITY.md](RAG_DOCUMENT_PREPARATION_PARITY.md) for the 125-case contract, offset/hash/ID semantics, fallback rules, redacted diagnostics, benchmark, and non-goals.

### 3.8.0 — Rust sidecar release performance and observability (completed)

- The benchmark uses `cargo build --release --locked --manifest-path rust/Cargo.toml -p deepseek-gateway` and records profile/toolchain/target/Python/OS/CPU/commit/warmup/iteration/concurrency provenance.
- Python baseline, pure Rust core, warm sidecar HTTP, cold start, and full Python integration are separate. The report retains slower Rust scenarios and never treats sidecar timing as a security or business input.
- Gateway, MCP, Policy, and RAG clients reuse bounded process-local standard-library connections with component timeouts, close/reset hooks, fork/PID invalidation, no caller credentials, and unchanged fallback behavior.
- `GET /metrics` extends the existing sidecar listener with seven fixed components and allowlisted outcomes/reasons. Metrics and tracing exclude models, methods, tools, document IDs, URLs, paths, raw errors, request IDs as labels, payloads, and credentials.
- No Rust delegate, ownership transfer, default flag, default Compose service, or fallback removal is part of this milestone.

See [RUST_SIDECAR_PERFORMANCE.md](RUST_SIDECAR_PERFORMANCE.md) for the audit, measurement contract, evidence, observability allowlists, and interpretation limits.

### 3.9.0 — Vector-ranking compact binary transport (completed)

- `POST /rag/vectors/rank-binary` uses a fixed little-endian `f64` request and 24-byte response while `POST /rag/vectors/rank` remains fully compatible JSON.
- The decoder validates media type, magic, checked size arithmetic, dimensions/candidates/scalar budgets, exact body length, trailing bytes, and finite values before ranking; it borrows the validated body and never materializes a candidate matrix.
- `DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT` accepts explicit `json|binary`, defaults/fails closed to JSON, and has no `auto` mode. A binary backend/protocol/parity failure makes no second Rust request and returns directly to the Python ranking.
- Python still scans the full candidate set, requires exact best index/first-match semantics, applies the existing similarity tolerance, and remains authoritative for cache policy, storage, thresholds, retrieval, and persistence.
- The release benchmark reports JSON/binary serialization, warmed HTTP, Rust processing, full integration, body sizes, and bounded concurrency without an absolute public-runner latency gate. Dense large requests shrink about 14.6%; tiny sparse JSON can be smaller, so no default or automatic selection follows.

See [RAG_VECTOR_BINARY_TRANSPORT.md](RAG_VECTOR_BINARY_TRANSPORT.md) for the wire contract, bounds, stable errors, corpus, fallback proof, evidence, and non-goals.

### 3.10.0 — Semantic-cache binary embedding storage and direct payload assembly (completed)

- `semantic_cache_items.embedding TEXT NOT NULL` remains the rollback-compatible source contract. New nullable `embedding_blob` plus dimensions/format metadata are added idempotently without scanning or rewriting rows at startup.
- Every new write rounds once to the established six-decimal semantics, then derives JSON and `f64le-v1` BLOB representations from that same normalized vector. The only non-empty format is `f64le-v1`.
- Valid BLOB candidates feed the unchanged `DSVRNK01` request through a single final allocation and `memoryview` copies. Mixed rows decode only the legacy/invalid rows and still make at most one binary sidecar call.
- Missing, unknown, truncated, oversized, dimension-mismatched, non-finite, or otherwise unusable BLOBs fall back to the same row's JSON. If both representations are bad, the row keeps the established corrupt-record behavior. Diagnostics contain counts, never vector contents.
- `scripts/migrate_semantic_cache_embeddings.py` is explicit, dry-run by default, batched, repeatable, resumable, and preserves every legacy JSON value and all unrelated columns. Old databases do not require migration and old versions can read newly written rows through the retained JSON column.
- Complete Python authoritative parity, sidecar-loss fallback, JSON transport behavior, Rust flags, Python-only default Compose, and ownership boundaries remain unchanged. Rust-primary, automatic transport selection, `f32`, compression, startup backfill, and Rust SQLite access remain out of scope.

See [SEMANTIC_CACHE_BINARY_EMBEDDINGS.md](SEMANTIC_CACHE_BINARY_EMBEDDINGS.md) for the storage, migration, corruption, downgrade, diagnostics, and operational contracts.

## Testing Priorities

Coverage work should prioritize meaningful behavior rather than easy lines:

- Streaming cancellation and reconnect behavior.
- Gateway retry, fallback, backpressure, and dead-letter handling.
- Agent DAG failure, resume, rerun, and synthesis behavior.
- MCP and A2A invalid payloads and protocol compatibility.
- Tool policy SSRF, private address blocking, path traversal, and sensitive write protection.
- RAG chunk selection, citation stability, malformed files, and CJK text.
- File upload limits, archive bombs, and parser failure modes.
- Cache cleanup, runtime roots, and local-first data isolation.

## Suggested PR Strategy

Keep migration PRs small:

1. One crate or boundary per PR.
2. Tests before integration.
3. Feature flag before default enablement.
4. Compatibility fixtures before behavior changes.
5. Release evidence before version bump.

## Status Labels

Use these labels in issues and PRs:

- `rust-core`
- `coverage`
- `gateway`
- `mcp`
- `policy`
- `rag`
- `release-readiness`
- `4.0.0`
