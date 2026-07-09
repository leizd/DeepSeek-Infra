# Rust Core Migration Roadmap

This document defines the incremental path from the current 3.0.x line to a 4.0.0 release focused on Rust-backed infrastructure and higher test coverage.

## Goal

DeepSeek Infra 4.0.0 should migrate selected core infrastructure paths to Rust while preserving Python for product integration, local-first UX, document/media tooling, and ecosystem-heavy features.

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

Planned themes (prioritized at 3.1.7, not committed):

- 3.2.0: Python coverage uplift toward 85%.
- 3.2.1: Rust sidecar Docker profile (still opt-in).
- 3.2.2: End-to-end hybrid runtime smoke tests.
- 3.2.3: Policy deny/audit hardening and parity tests.
- 3.2.4: RAG parity tests against Python path.
- 3.2.5: 4.0.0 release candidate checklist.

See [PRE_4_0_QUALITY_BASELINE.md](PRE_4_0_QUALITY_BASELINE.md) for details.

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

