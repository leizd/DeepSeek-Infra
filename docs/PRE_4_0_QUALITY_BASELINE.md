# Pre-4.0 Quality Baseline

This document tracks where the project stands after the 3.2.3 Rust Policy deny/audit hardening milestone and how far it is from the 4.0.0 goals defined in [RUST_MIGRATION_ROADMAP.md](RUST_MIGRATION_ROADMAP.md).

> **Purpose**: know the gap, not to declare 4.0.0 ready. 3.2.3 makes Rust Policy deny and backend-failure behavior traceable and fail-safe; it does not enable Rust by default, replace Python Policy, add persistent audit storage, or raise the coverage gate.

---

## Current quality milestone: 3.2.3

At the end of 3.2.3:

- All Rust components remain **default-disabled**.
- The hybrid runtime has a complete operational runbook ([RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md)) and a release-readiness checklist ([RELEASE_READINESS_3_1_X.md](RELEASE_READINESS_3_1_X.md)).
- Python CI gates pass at the 85% coverage gate with 85.60% measured full-suite coverage.
- Rust CI gates pass (`cargo fmt`, `cargo clippy -D warnings`, `cargo test`).
- Offline eval gates pass with `--strict`.
- The Rust sidecar has a standalone multi-stage Docker image, optional Compose file, container health check, and offline smoke test.
- A test-only Compose overlay starts Python and Rust together with all four Rust flags enabled, then verifies Python-to-Rust delegation and Python fallback after stopping the sidecar.
- Rust Policy decisions carry stable codes, decision and trace identifiers, capability/risk context, and structured redacted audit fields.
- Policy backend failures have explicit `fallback`, `deny`, and `error` behavior; all deny/error paths stop tool execution.
- The default Docker deployment still builds and runs only the Python service.
- Python coverage is **not** near the 4.0.0 target of ~95%.

---

## Rust core status matrix

| Component | Rust crate | Sidecar routes | Python opt-in integration | Default-on in 3.1.x | Gap before 4.0.0 |
| --- | --- | --- | --- | --- | --- |
| **Gateway** | `deepseek-gateway` | `GET /healthz`, `GET /v1/models`, `POST /v1/chat/completions` (non-streaming) | `DEEPSEEK_RUST_GATEWAY=1` proxies non-streaming chat and model list | ❌ | Streaming chat still uses Python; needs default-on decision and packaging |
| **MCP** | `deepseek-mcp` | `POST /mcp` | `DEEPSEEK_RUST_MCP=1` delegates JSON-RPC handling | ❌ | No real Python tool execution bridge into Rust; default-on not decided |
| **Policy** | `deepseek-policy` | `POST /policy/url`, `/policy/path`, `/policy/capability` | `DEEPSEEK_RUST_POLICY=1` delegates structured, audited URL/path/capability checks | ❌ | Python Tool Policy remains the fallback; default enforcement not decided |
| **RAG** | `deepseek-rag` | `POST /rag/query/normalize`, `/rag/chunks/score`, `/rag/citation/format`, `/rag/index/validate` | `DEEPSEEK_RUST_RAG=1` delegates hot paths | ❌ | Embedding and vector DB access still live in Python; full parity not proven |

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
| RAG hot paths | `deepseek_infra/infra/rag/local_rag.py` | `DEEPSEEK_RUST_RAG` | Python RAG functions | ✅ `tests/test_rust_rag_integration.py` | ✅ Runbook |
| Feature flags / config | `deepseek_infra/infra/rust_core/config.py` | all | n/a | ✅ `tests/test_hybrid_runtime_hardening.py` | ✅ Runbook |
| Health / status | `deepseek_infra/infra/rust_core/health.py` / `registry.py` | n/a | n/a | ✅ | ✅ `GET /api/rust/status` |

---

## Coverage status

### Python

| Metric | Current | 4.0.0 target | Gap |
| --- | --- | --- | --- |
| Coverage gate | **85%** | ~95% | **+10 percentage points** |
| Measured coverage (full suite) | **85.60%** | ~95% | ~+9.40 percentage points |

The gate was raised from 82% to 85% in 3.2.0 after the measured full-suite coverage reached 85.559%; the 3.2.3 suite measures 85.60%. The uplift emphasizes Rust client failures, RAG and tool-policy edges, route/config/launcher paths, MCP execution, and isolated browser downloads. OCR, browser controller, edge inference, media processing, and several skills paths still have meaningful misses, so the next climb should remain test-led and incremental.

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
| `pytest --cov --cov-fail-under=85` | ✅ Green | 3.2.3 measured 85.60% with 1,795 tests and 58 subtests passing. |
| `cargo fmt --check` | ✅ Green | Rust workspace. |
| `cargo clippy --all-targets --all-features -- -D warnings` | ✅ Green | No warnings. |
| `cargo test --all` | ✅ Green | Rust crate tests. |
| Rust sidecar Docker build + smoke | ✅ CI gate | Independent job; does not alter the Python image. |
| Hybrid runtime E2E + fallback | ✅ CI gate | Gateway, MCP, Policy, and RAG over the live Compose network. |
| `node --check ...` | ✅ Green | Listed JS files. |
| `python scripts/check_doc_links.py` | ✅ Green | Internal doc links. |
| `pip-audit`, `bandit`, `detect-secrets` | ✅ Green | Security scan. |
| Offline eval suite `--strict` | ✅ Green | RAG, Tool, Injection, Agent, Security corpus. |
| `scripts/preflight_release.py --version 3.0.1 --ga` | ✅ Green | Release readiness. |

---

## Known gaps before 4.0.0

1. **Python coverage**: 85% → ~95% remains a significant climb. The biggest remaining misses are in OCR, browser control, media processing, edge inference, and skill UI/UX paths.
2. **Rust coverage**: Not measured or gated.
3. **Rust default-on**: No component is enabled by default. 4.0.0 requires a decision on which components become the primary path.
4. **Default packaging**: The Rust sidecar has an optional standalone image, but it is not bundled into the default Python image or single-file exe build.
5. **Policy parity**: Stable Rust deny codes, trace correlation, redacted audits, and fail modes are proven; broader corpus-level Rust/Python rule parity is still required before default enforcement.
6. **RAG parity**: Query normalization, chunk scoring, and citation formatting need broader corpus-level comparison beyond the E2E contract cases.
7. **Gateway streaming**: Streaming chat completions still bypass Rust Gateway.
8. **Rust-side error contracts**: Policy decisions are stable contracts; Gateway, MCP, and RAG error shapes still need the same treatment.

---

## Recommended 3.2.x milestones

These are proposed, not committed. They keep the project on the conservative path toward 4.0.0 without jumping the gun.

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

### 3.2.4 — RAG parity tests against Python path

- Structured comparison of Rust vs. Python for query normalization, chunk scoring, and citation formatting.
- Preserve CJK and Unicode behavior.
- Add performance regression checks.

### 3.2.5 — Release candidate checklist

- Define a 4.0.0 release candidate checklist.
- Decide which Rust components become default-on and which remain opt-in.
- Run the full release-readiness, eval, and security corpus gates with Rust enabled.

---

## Sign-off questions for 4.0.0

Before scheduling 4.0.0, the following should be answered with evidence:

- [ ] Which Rust components are default-on, and which remain opt-in?
- [ ] Is Python coverage at or near 95%?
- [ ] Is Rust coverage measured and at or near 95%?
- [ ] Does the default release deployment include and coordinate the Rust sidecar by design?
- [x] Are there end-to-end hybrid smoke tests with all flags enabled?
- [ ] Is Rust Policy parity proven against Python Tool Policy?
- [ ] Is Rust RAG parity proven against Python RAG?
- [ ] Does streaming Gateway use Rust or stay on Python by design?

Until these are answered, the project remains in the 3.1.x / 3.2.x line.

---

## Related documents

- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [Hybrid Rust Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [Release Readiness 3.1.x](RELEASE_READINESS_3_1_X.md)
- [Implementation Status](IMPLEMENTATION_STATUS.md)
- [CHANGELOG.md](../CHANGELOG.md)
