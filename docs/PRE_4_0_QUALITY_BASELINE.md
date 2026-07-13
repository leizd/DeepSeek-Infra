# Pre-4.0 Quality Baseline

This document tracks where the project stands after the 3.4.0 semantic-cache vector-ranking update, built on the 3.3.2 95% coverage and RC rehearsal milestone, and how far it is from the 4.0.0 goals defined in [RUST_MIGRATION_ROADMAP.md](RUST_MIGRATION_ROADMAP.md).

> **Purpose**: know and enforce the gap. The [4.0 RC readiness matrix](4_0_RC_READINESS.md) now reports **READY FOR 4.0.0-rc.1** after the measured suite cleared the 95% RC target twice with a 0.30-point safety margin. This rehearsal does not create an RC tag.

---

## Current quality milestone: 3.4.0

At the end of 3.4.0:

- All Rust components remain **default-disabled**.
- The hybrid runtime has a complete operational runbook ([RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md)) and a release-readiness checklist ([RELEASE_READINESS_3_1_X.md](RELEASE_READINESS_3_1_X.md)).
- Python CI gates pass at the inherited 95% coverage gate after two 3.3.2 full-suite statement-and-branch runs measured 95.3428% and 95.3396%.
- Rust CI gates pass (`cargo fmt`, `cargo clippy -D warnings`, `cargo test`).
- Offline eval gates pass with `--strict`.
- The Rust sidecar has a standalone multi-stage Docker image, optional Compose file, container health check, and offline smoke test.
- A test-only Compose overlay starts Python and Rust together with all four Rust flags enabled, then verifies Python-to-Rust delegation and Python fallback after stopping the sidecar.
- Rust Policy decisions carry stable codes, decision and trace identifiers, capability/risk context, and structured redacted audit fields.
- Policy backend failures have explicit `fallback`, `deny`, and `error` behavior; all deny/error paths stop tool execution.
- A shared 38-case fixture proves Python/Rust parity for normalization, full Top-K ordering, tie-breaks, scores, citations, and index validation.
- The independent `rag-parity` CI job runs against a live Rust sidecar and uploads a machine-readable difference report.
- A machine-readable RC requirements manifest classifies quality blockers, architecture decision blockers, and non-blocking recommendations with explicit owners and evidence.
- [ADR-0040](adr/ADR-0040-hybrid-runtime-architecture.md) approves a Python-first hybrid 4.0 architecture: no Rust delegate is default-on, default deployment remains Python-only, and Python fallback is guaranteed throughout 4.x.
- Gateway streaming and real MCP tool execution remain Python-owned by design; Rust owns models/non-streaming chat delegation and MCP JSON-RPC validation/routing respectively.
- The machine-readable architecture contract resolves all five architecture blockers from real decision fields, including the intentionally empty Rust default-on set.
- Normal PRs and `main` generate the RC report without permanent failure; `release/*` and `rc/*` branches run the same checker in strict mode.
- The default Docker deployment still builds and runs only the Python service.
- Semantic-cache vector ranking can use the existing opt-in Rust RAG delegate, with strict response validation, backend diagnostics, and Python fallback.
- Python coverage is conservatively recorded as **95.33%**, above the explicit 4.0 RC target of **95.00%**.

---

## Rust core status matrix

| Component | Rust crate | Sidecar routes | Python opt-in integration | Default-on in 3.1.x | Gap before 4.0.0 |
| --- | --- | --- | --- | --- | --- |
| **Gateway** | `deepseek-gateway` | `GET /healthz`, `GET /v1/models`, `POST /v1/chat/completions` (non-streaming) | `DEEPSEEK_RUST_GATEWAY=1` proxies non-streaming chat and model list | ❌ | Streaming chat still uses Python; needs default-on decision and packaging |
| **MCP** | `deepseek-mcp` | `POST /mcp` | `DEEPSEEK_RUST_MCP=1` delegates JSON-RPC handling | ❌ | No real Python tool execution bridge into Rust; default-on not decided |
| **Policy** | `deepseek-policy` | `POST /policy/url`, `/policy/path`, `/policy/capability` | `DEEPSEEK_RUST_POLICY=1` delegates structured, audited URL/path/capability checks | ❌ | Python Tool Policy remains the fallback; default enforcement not decided |
| **RAG** | `deepseek-rag` | `POST /rag/query/normalize`, `/rag/chunks/score`, `/rag/vectors/rank`, `/rag/citation/format`, `/rag/index/validate` | `DEEPSEEK_RUST_RAG=1` delegates hot paths and semantic-cache batch vector ranking | ❌ | Deterministic hot-path parity is proven; embedding and vector DB access still live in Python |

Legend:

- **MVP** ✅ = Rust crate exists and sidecar endpoints are implemented.
- **Opt-in integration** ✅ = Python app can delegate to Rust behind a feature flag with fallback.
- **Default-on** ❌ = Rust component is not enabled by default and cannot yet replace Python path unconditionally.

---

## Python integration status matrix

| Python boundary | Location | Rust flag | Fallback path | Tests | Docs |
| --- | --- | --- | --- | --- | --- |
| Gateway chat/models | `deepseek_infra/web/routes/chat.py` | `DEEPSEEK_RUST_GATEWAY` | Python `openai_api.py` | ✅ `tests/test_rust_gateway_proxy.py` | ✅ Runbook |
| MCP JSON-RPC | `deepseek_infra/web/routes/mcp.py` | `DEEPSEEK_RUST_MCP` | Python `infra/mcp/server.py` | ✅ `tests/test_rust_mcp_proxy.py` | ✅ Runbook |
| Tool policy guards | `deepseek_infra/infra/tool_runtime/tools.py` | `DEEPSEEK_RUST_POLICY` | Python `tool_policy.py` | ✅ `tests/test_rust_policy_integration.py`, `test_rust_policy_audit.py`, `test_rust_policy_fail_modes.py` | ✅ Runbook |
| RAG hot paths | `deepseek_infra/infra/rag/local_rag.py` | `DEEPSEEK_RUST_RAG` | Python RAG functions | ✅ `tests/test_rust_rag_integration.py`, `test_rust_rag_parity_contract.py` | ✅ [Parity baseline](RAG_PARITY_BASELINE.md) |
| Semantic-cache vector ranking | `deepseek_infra/infra/gateway/semantic_cache.py` | `DEEPSEEK_RUST_RAG` | Python `cosine_similarity` scan | ✅ `tests/test_observability_semantic_cache.py`, `test_rust_core_clients.py` | ✅ [3.4.0 candidate audit](RUST_CANDIDATE_AUDIT_3_4.md) |
| Feature flags / config | `deepseek_infra/infra/rust_core/config.py` | all | n/a | ✅ `tests/test_hybrid_runtime_hardening.py` | ✅ Runbook |
| Health / status | `deepseek_infra/infra/rust_core/health.py` / `registry.py` | n/a | n/a | ✅ | ✅ `GET /api/rust/status` |

---

## Coverage status

### Python

| Metric | Current | 4.0.0 target | Gap |
| --- | --- | --- | --- |
| Coverage gate | **95%** | ~95% | Cleared |
| Measured coverage (full suite) | **95.33%** | ~95% | Cleared with 0.30-point rehearsal margin |

The gate was raised from 82% to 85% in 3.2.0, to 90% in 3.3.1, and to 95% in 3.3.2. The final uplift targets real failure behavior in DeepSeek streaming, RAG/files, launcher credentials, agent persistence/concurrency, Skills security/versioning, Browser/OCR/media, MCP, and workspace persistence. Coverage omit rules remain unchanged, and HIGH-risk coverage debt is lower than the 3.3.1 baseline.

### Rust

| Metric | Current | 4.0.0 target | Gap |
| --- | --- | --- | --- |
| Coverage gate | **Not set** | ~95% | **Unknown / not measured** |
| Measured coverage | Not measured | ~95% | Needs a coverage tool wired into `cargo test` and CI |

Rust coverage is currently not measured or gated. Before 4.0.0, the Rust workspace needs a coverage tool (e.g., `cargo-llvm-cov` or `tarpaulin`) and a CI gate.

---

## CI / release gate status

| Gate | Status | Notes |
| --- | --- | --- |
| `ruff check .` | ✅ Green | Minimal rule set by design. |
| `mypy .` | ✅ Green | `ignore_missing_imports=true`. |
| `pytest --cov --cov-fail-under=95` | ✅ Green | 3.4.0 inherits the 3.3.2 evidence of 95.3428% and 95.3396% across two consecutive full runs; branch coverage is recorded without a separate gate. |
| `cargo fmt --check` | ✅ Green | Rust workspace. |
| `cargo clippy --all-targets --all-features -- -D warnings` | ✅ Green | No warnings. |
| `cargo test --all` | ✅ Green | Rust crate tests. |
| Rust sidecar Docker build + smoke | ✅ CI gate | Independent job; does not alter the Python image. |
| Hybrid runtime E2E + fallback | ✅ CI gate | Gateway, MCP, Policy, and RAG over the live Compose network. |
| Rust/Python RAG parity | ✅ CI gate | 38 deterministic cases against a live Rust sidecar; JSON report uploaded. |
| `node --check ...` | ✅ Green | Listed JS files. |
| `python scripts/check_doc_links.py` | ✅ Green | Internal doc links. |
| `pip-audit`, `bandit`, `detect-secrets` | ✅ Green | Security scan. |
| Offline eval suite `--strict` | ✅ Green | RAG, Tool, Injection, Agent, Security corpus. |
| `scripts/preflight_release.py --version 3.4.0 --ga` | ✅ Green | Release readiness. |

---

## Known gaps before 4.0.0

1. **Python coverage**: the 95% gate and measured RC target are cleared. Continue tracking risk-weighted debt so HIGH-risk gaps do not regress.
2. **Rust coverage**: Not measured or gated.
3. **Rust default-on**: ADR-0040 approves an empty default-on set for 4.0; future promotion requires a separate decision and matching evidence.
4. **Default packaging**: ADR-0040 approves Python-only default deployment; the Rust sidecar remains optional and separate from the default Python image and single-file exe build.
5. **Policy parity**: Stable Rust deny codes, trace correlation, redacted audits, and fail modes are proven; broader corpus-level Rust/Python rule parity is still required before default enforcement.
6. **RAG parity**: Deterministic normalization, scoring/order, citation, and validation parity is proven; embedding, vector database, and corpus-scale performance parity remain open.
7. **Gateway streaming**: Streaming chat completions remain Python-owned for 4.0 by ADR-0040; this is an explicit boundary, not a completed Rust path.
8. **Rust-side error contracts**: Policy decisions are stable contracts; Gateway, MCP, and RAG error shapes still need the same treatment.

---

## Recommended 3.2.x milestones

These completed milestones keep the project on the conservative path toward 4.0.0 without turning readiness evidence into an early release declaration.

### 3.2.0 — Coverage uplift to 85% (completed)

- Achieved: raised the Python coverage gate from 82% to 85% with 85.559% measured coverage.
- Added failure and boundary coverage for Rust clients, Local RAG, tool execution/policy, web routes, core config, MCP, browser downloads, and launcher paths.
- Non-goals preserved: no runtime features, Rust default-on changes, Docker sidecar packaging, or 4.0.0 release candidate work.

### 3.2.1 — Rust sidecar Docker profile (completed)

- Added a multi-stage, non-root image containing only the Rust Gateway binary and health-check dependency.
- Added an independent Compose file, offline six-endpoint smoke test, static deployment contract tests, and a dedicated CI job.
- Preserved the default Python Compose deployment, default-disabled Rust flags, 85% Python coverage gate, and pre-4.0 status.

### 3.2.2 — End-to-end hybrid runtime smoke tests (completed)

- Added a test-only Compose overlay that starts Python and Rust on one container network with all four Rust flags enabled.
- Added an offline smoke that verifies Rust status, Gateway and MCP proxying, Tool Policy denial, and RAG normalization/ranking/citation through Python boundaries.
- Stops the Rust sidecar mid-run and verifies Gateway, MCP, Policy, and RAG fall back to Python without an application crash or unstructured 500.

### 3.2.3 — Policy deny / audit hardening (completed)

- **Completed in 3.2.3**: added stable deny codes, decision/trace identifiers, capability/risk context, and redacted structured audit logs.
- Added explicit `fallback`, `deny`, and `error` backend failure modes while preserving Python Policy as the default fallback.
- Proved with spies that Rust denial and backend fail-closed modes never invoke network, file-write, or execution helpers.
- Extended the live hybrid smoke to preserve the Rust decision code and identifier through the Python tool boundary.

### 3.2.4 — RAG parity tests against Python path (completed)

- Added 38 implementation-independent expected cases for query normalization, complete Top-K ordering/scores, citation formatting, and index validation.
- Added a strict live-sidecar parity runner with `1e-6` score tolerance, stable error categories, first-divergence output, and JSON report artifacts.
- Preserved CJK, mixed-language, punctuation, emoji, stable tie-break, and existing citation-format behavior.
- Kept embedding/vector retrieval and performance benchmarking outside this deterministic test-only milestone.

### 3.2.5 — 4.0 RC readiness checklist (completed)

- Added an owner-tagged, machine-readable blocker matrix and a human sign-off document without creating an RC.
- Added terminal and JSON reporting with an honest `NOT READY` decision while coverage and architecture blockers remain.
- Added report-only CI behavior for normal development and strict blocking behavior for `release/*` and `rc/*` branches.
- Preserved the 85% current coverage gate, 95% RC target, Python default runtime, opt-in Rust flags, and existing Docker defaults.

### 3.3.0 — 4.0 runtime architecture decision (completed)

- Approved ADR-0040 and a machine-readable Python-first hybrid runtime contract.
- Resolved all five architecture blockers without claiming Rust Gateway streaming or a real Rust-to-Python MCP tool bridge.
- Kept all Rust delegates opt-in, default Compose Python-only, and Python fallback supported throughout 4.x with removal not before 5.0.0.
- Preserved the 95% RC measured-coverage target; 85.63% measured coverage remains the sole readiness blocker.

### 3.3.1 — Risk-weighted Python coverage uplift (completed)

- Enabled branch measurement and added a machine-readable coverage-debt report without introducing a separate branch gate.
- Added deterministic failure and boundary tests for Browser, OCR/media, edge inference, Skills, Automation, files, launchers, DeepSeek networking, A2A, and Agent run persistence.
- Raised measured coverage to a conservative 90.52% across two consecutive full runs and the Python CI/preflight/release-manifest gate from 85% to 90%.
- Preserved the 95% RC measured target, approved hybrid architecture contract, Python-only default deployment, and four default-disabled Rust delegates.

### 3.3.2 — 95% coverage and RC readiness rehearsal (completed)

- Added high-value failure coverage for DeepSeek streaming/retry/cache behavior, RAG and file corruption/atomic writes, launcher credentials, agent cancellation/concurrency/persistence, Skills security/versioning, Browser/OCR/media, MCP, and workspace persistence.
- Measured 95.3428% and 95.3396% across two consecutive full runs, reduced HIGH-risk debt, preserved coverage omit rules, and raised the Python gate from 90% to 95%.
- Cleared strict 4.0 RC readiness while preserving ADR-0040, Python-only defaults, four opt-in Rust delegates, and Python fallback through 4.x.
- Did not create an RC tag; version bump, evidence freeze, checksum, tag, and release notes remain a separate change.

### 3.4.0 — Rust semantic-cache vector ranking (completed)

- Added a pure Rust vector-ranking primitive and sidecar endpoint with stable first-match tie behavior.
- Integrated semantic-cache ranking behind the existing default-disabled `DEEPSEEK_RUST_RAG` flag.
- Kept cache storage, filtering, TTL, exact matches, thresholds, and mutation in Python.
- Added strict response validation, backend diagnostics, endpoint smoke coverage, and Python fallback tests.

---

## Sign-off questions for 4.0.0

Before scheduling 4.0.0, the following should be answered with evidence:

- [x] Which Rust components are default-on, and which remain opt-in? ADR-0040 approves an empty default-on set.
- [x] Is Python coverage at or near 95%? Two consecutive full suites exceeded 95.30%.
- [ ] Is Rust coverage measured and at or near 95%?
- [x] Does the default release deployment include and coordinate the Rust sidecar by design? ADR-0040 explicitly approves Python-only default deployment.
- [x] Are there end-to-end hybrid smoke tests with all flags enabled?
- [ ] Is Rust Policy parity proven against Python Tool Policy?
- [x] Is deterministic Rust RAG hot-path parity proven against Python RAG?
- [ ] Are embedding, vector database, and performance parity proven?
- [x] Does streaming Gateway use Rust or stay on Python by design? It stays on Python for 4.0.
- [x] Does MCP execute real tools in Rust or Python by design? Python Tool Runtime executes them; Rust validates and routes JSON-RPC.

Architecture and Python coverage questions are now answered. The repository is ready to prepare an RC in a separate, evidence-freezing release change.

---

## Related documents

- [4.0 RC Readiness](4_0_RC_READINESS.md)
- [ADR-0040: Python-first hybrid runtime architecture](adr/ADR-0040-hybrid-runtime-architecture.md)
- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [Hybrid Rust Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [RAG Parity Baseline](RAG_PARITY_BASELINE.md)
- [Release Readiness 3.1.x](RELEASE_READINESS_3_1_X.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)
- [CHANGELOG.md](../CHANGELOG.md)
