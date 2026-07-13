# 4.0.0 RC Readiness

This checklist determines whether the repository may create `4.0.0-rc.1`. It does not create a tag or change Rust defaults.

> **Current decision: READY FOR 4.0.0-rc.1.** ADR-0040 resolves all five runtime architecture blockers with an approved Python-first hybrid design. Two consecutive full suites measured 95.3428% and 95.3396%, clearing the explicit 95.00% RC target with the required 0.30-point safety margin. This rehearsal does not create an RC tag.

The machine-readable requirements source is [`release/4_0_rc_requirements.json`](../release/4_0_rc_requirements.json), and the approved architecture contract is [`release/4_0_runtime_decision.json`](../release/4_0_runtime_decision.json). Run the checker in report-only mode during normal development:

```bash
python scripts/check_4_0_rc_readiness.py \
  --requirements release/4_0_rc_requirements.json \
  --report-only \
  --json-out artifacts/4-0-rc-readiness.json
```

Only an actual `release/*` or `rc/*` branch should use the blocking mode:

```bash
python scripts/check_4_0_rc_readiness.py \
  --requirements release/4_0_rc_requirements.json \
  --strict
```

## Blocker matrix

### Quality blockers

| Requirement | Owner | Current status | Evidence / exit condition |
| --- | --- | --- | --- |
| All required CI jobs green | Release Engineering | PASS on merged baseline; evaluated live in CI | All jobs listed in the requirements manifest return `success` or an intentional `skipped` result. |
| Current Python coverage gate | Python Runtime | PASS: 95% | `pyproject.toml` sets `fail_under = 95`; statement and branch coverage are measured together, with no separate branch threshold in 3.4.0. |
| Python measured coverage | Python Runtime | **PASS: 95.33% >= 95.00%** | Consecutive full runs measured 95.3428% and 95.3396%, both above the 95.30% rehearsal floor. |
| Rust fmt / clippy / tests | Rust Core | PASS | `ci / rust` is green. |
| Rust sidecar Docker smoke | Rust Core | PASS | `ci / rust-docker` is green. |
| Hybrid runtime E2E and fallback | Hybrid Runtime | PASS | `ci / hybrid-runtime-e2e` is green. |
| Rust/Python RAG parity | RAG | PASS: 38/38 | `ci / rag-parity` is green and the full deterministic fixture remains present. |
| Policy deny / audit contract | Tool Runtime Security | PASS | Stable deny identifiers, redaction, failure modes, and no-execution tests pass. |
| Release preflight | Release Engineering | PASS | `ci / release-readiness` is green. |
| Default Python behavior unchanged | Runtime Architecture | PASS | `.env.example` keeps all four Rust delegates at `0`; default Compose remains Python-only. |
| Rollback path | Release Engineering | PASS | The runbook documents all-flags-off rollback and hybrid tests exercise fallback. |

### Architecture decisions

| Requirement | Owner | Current status | Exit condition |
| --- | --- | --- | --- |
| Rust default-on component set | Runtime Architecture | **PASS: approved empty set** | ADR-0040 explicitly keeps Gateway, MCP, Policy, and RAG opt-in. Empty is a valid decision value. |
| Sidecar in default deployment | Release Engineering | **PASS: Python-only default** | The Rust sidecar remains an optional Compose deployment. |
| Python fallback lifecycle | Runtime Architecture | **PASS: supported through 4.x** | Removal may not be considered before 5.0.0. |
| Gateway streaming path | Gateway | **PASS: Python-owned for 4.0** | Rust continues to handle models and opt-in non-streaming chat; no Rust streaming implementation is claimed. |
| MCP real tool bridge | MCP | **PASS: Python-owned execution for 4.0** | Rust validates and routes JSON-RPC; Python Tool Runtime executes real tools. No Rust tool bridge is claimed. |

### Non-blocking recommendations

- Expand the deterministic RAG and Policy parity corpora.
- Record hybrid latency and throughput benchmarks.
- Add Rust coverage measurement and a data-backed target.
- Measure and optimize the Rust sidecar image.
- Evaluate persistent policy audit storage after the logging contract stabilizes.

These items remain visible in the generated JSON report but do not independently block an RC.

## Default-on decision matrix

| Component | Proven Rust surface | Remaining gap | Current recommendation | Decision |
| --- | --- | --- | --- | --- |
| Gateway | Models and non-streaming chat delegation | Streaming remains in Python by design | Keep opt-in | Approved: opt-in |
| MCP | JSON-RPC initialize, validation, and routing | Real tool execution remains in Python by design | Keep opt-in | Approved: opt-in |
| Policy | Stable deny/audit contract and explicit backend failure modes | Broader Python/Rust policy corpus can still grow | Keep opt-in | Approved: opt-in |
| RAG | Deterministic hot-path parity at 38/38 plus 3.4.0 semantic-cache vector ranking | Embedding and vector database work remains in Python | Keep opt-in | Approved: opt-in |

ADR-0040 is an approved architecture contract, not a runtime-default change. The 3.3.2 coverage uplift and the 3.4.0 vector-ranking delegate do not modify runtime defaults; specifically, 3.4.0 does not modify `.env.example`, default Compose, or the decision file.

## Sign-off

Before creating `4.0.0-rc.1`, the accountable owners must sign the following on the release change:

- [x] Release Engineering: all required CI jobs and release preflight are green on the rehearsal commit.
- [x] Python Runtime: two consecutive measured full-suite runs exceed 95.30%.
- [x] Rust Core: workspace, Docker smoke, hybrid E2E, and parity jobs are green.
- [x] Tool Runtime Security: deny/audit contracts remain green and no denied tool implementation executes.
- [x] Runtime Architecture: default-on components, default sidecar deployment, and Python fallback lifecycle are approved by ADR-0040.
- [x] Gateway: Python streaming ownership for 4.0 is approved by ADR-0040.
- [x] MCP: Python real-tool execution and Rust protocol ownership for 4.0 are approved by ADR-0040.
- [x] Release Engineering: rollback remains documented and `--strict` exits zero on an `rc/*` rehearsal branch.

The repository is **READY FOR 4.0.0-rc.1**, but this milestone intentionally stops before version bump, evidence freeze, checksums, tag creation, and release notes.

## Related documents

- [Pre-4.0 Quality Baseline](PRE_4_0_QUALITY_BASELINE.md)
- [ADR-0040: Python-first hybrid runtime architecture](adr/ADR-0040-hybrid-runtime-architecture.md)
- [3.1.x / 3.2.x Release Readiness](RELEASE_READINESS_3_1_X.md)
- [Rust Hybrid Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [RAG Parity Baseline](RAG_PARITY_BASELINE.md)
