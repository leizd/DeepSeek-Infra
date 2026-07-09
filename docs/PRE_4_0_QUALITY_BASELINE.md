# Pre-4.0 Quality Baseline

This document is an audit of where the project stands after the 3.1.6 release-readiness milestone and how far it is from the 4.0.0 goals defined in [RUST_MIGRATION_ROADMAP.md](RUST_MIGRATION_ROADMAP.md).

> **Purpose**: know the gap, not to declare 4.0.0 ready. No new runtime features are added in 3.1.7; this is a planning and transparency document.

---

## Current version status: 3.1.6

At the end of 3.1.6:

- All Rust components remain **default-disabled**.
- The hybrid runtime has a complete operational runbook ([RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md)) and a release-readiness checklist ([RELEASE_READINESS_3_1_X.md](RELEASE_READINESS_3_1_X.md)).
- Python CI gates pass at the 82% coverage gate.
- Rust CI gates pass (`cargo fmt`, `cargo clippy -D warnings`, `cargo test`).
- Offline eval gates pass with `--strict`.
- The Rust sidecar is **not** packaged or Dockerized.
- Python coverage is **not** near the 4.0.0 target of ~95%.

---

## Rust core status matrix

| Component | Rust crate | Sidecar routes | Python opt-in integration | Default-on in 3.1.x | Gap before 4.0.0 |
| --- | --- | --- | --- | --- | --- |
| **Gateway** | `deepseek-gateway` | `GET /healthz`, `GET /v1/models`, `POST /v1/chat/completions` (non-streaming) | `DEEPSEEK_RUST_GATEWAY=1` proxies non-streaming chat and model list | ❌ | Streaming chat still uses Python; needs default-on decision and packaging |
| **MCP** | `deepseek-mcp` | `POST /mcp` | `DEEPSEEK_RUST_MCP=1` delegates JSON-RPC handling | ❌ | No real Python tool execution bridge into Rust; default-on not decided |
| **Policy** | `deepseek-policy` | `POST /policy/url`, `/policy/path`, `/policy/capability` | `DEEPSEEK_RUST_POLICY=1` delegates URL/path/capability checks | ❌ | Python Tool Policy remains the fallback; default enforcement not decided |
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
| Tool policy guards | `deepseek_infra/infra/tool_runtime/tools.py` | `DEEPSEEK_RUST_POLICY` | Python `tool_policy.py` | ✅ `tests/test_rust_policy_integration.py` | ✅ Runbook |
| RAG hot paths | `deepseek_infra/infra/rag/local_rag.py` | `DEEPSEEK_RUST_RAG` | Python RAG functions | ✅ `tests/test_rust_rag_integration.py` | ✅ Runbook |
| Feature flags / config | `deepseek_infra/infra/rust_core/config.py` | all | n/a | ✅ `tests/test_hybrid_runtime_hardening.py` | ✅ Runbook |
| Health / status | `deepseek_infra/infra/rust_core/health.py` / `registry.py` | n/a | n/a | ✅ | ✅ `GET /api/rust/status` |

---

## Coverage status

### Python

| Metric | Current | 4.0.0 target | Gap |
| --- | --- | --- | --- |
| Coverage gate | **82%** | ~95% | **+13 percentage points** |
| Measured coverage (full suite) | ~82.4% | ~95% | ~+12.6 percentage points |

The 82% gate was raised from 80% in 3.1.5. The measured full-suite coverage is currently slightly above the gate, but a large portion of the codebase (document/media parsing, browser control, OCR, edge inference, skills UI, etc.) still has meaningful misses. Closing the gap to 95% will require dedicated tests for the largest remaining modules rather than chasing easy one-liners.

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
| `pytest --cov --cov-fail-under=82` | ✅ Green | Measured ~82.4%. |
| `cargo fmt --check` | ✅ Green | Rust workspace. |
| `cargo clippy --all-targets --all-features -- -D warnings` | ✅ Green | No warnings. |
| `cargo test --all` | ✅ Green | Rust crate tests. |
| `node --check ...` | ✅ Green | Listed JS files. |
| `python scripts/check_doc_links.py` | ✅ Green | Internal doc links. |
| `pip-audit`, `bandit`, `detect-secrets` | ✅ Green | Security scan. |
| Offline eval suite `--strict` | ✅ Green | RAG, Tool, Injection, Agent, Security corpus. |
| `scripts/preflight_release.py --version 3.0.1 --ga` | ✅ Green | Release readiness. |

---

## Known gaps before 4.0.0

1. **Python coverage**: 82% → ~95% is a significant climb. The biggest remaining misses are in OCR, browser control, media processing, edge inference, and skill UI/UX paths.
2. **Rust coverage**: Not measured or gated.
3. **Rust default-on**: No component is enabled by default. 4.0.0 requires a decision on which components become the primary path.
4. **Docker / packaging**: The Rust sidecar is not included in the Docker image or single-file exe build.
5. **End-to-end hybrid smoke tests**: The release-readiness job does not yet exercise all Rust flags together against a live sidecar.
6. **Policy parity**: Rust Policy deny reasons and audit logs should be compared against Python Tool Policy output.
7. **RAG parity**: Query normalization, chunk scoring, and citation formatting results need a structured comparison against the Python path.
8. **Gateway streaming**: Streaming chat completions still bypass Rust Gateway.
9. **Rust-side error contracts**: Malformed response handling is tested on the Python side, but Rust side error shapes are not yet stable contracts.

---

## Recommended 3.2.x milestones

These are proposed, not committed. They keep the project on the conservative path toward 4.0.0 without jumping the gun.

### 3.2.0 — Coverage uplift to 85%

- Target: raise Python coverage gate from 82% to 85%.
- Focus: largest remaining misses (OCR, browser, media, edge inference, skills UI).
- Non-goal: no Rust default-on changes.

### 3.2.1 — Rust sidecar Docker profile

- Add a Docker profile or service that can run the Rust sidecar alongside the Python app.
- Keep Rust components default-disabled; the profile is opt-in.
- Non-goal: do not make Rust the default Docker runtime.

### 3.2.2 — End-to-end hybrid runtime smoke tests

- Add smoke scripts that start the Rust sidecar and exercise all four flags together.
- Verify fallback when the sidecar is killed mid-run.
- Generate evidence JSON for the release-readiness job.

### 3.2.3 — Policy deny / audit hardening

- Compare Rust Policy deny decisions against Python Tool Policy for the same inputs.
- Ensure deny reasons and audit records are equivalent or better.
- Add regression tests for edge cases (private IPs, path traversal, missing capability).

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
- [ ] Does the Docker image include and run the Rust sidecar?
- [ ] Are there end-to-end hybrid smoke tests with all flags enabled?
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
