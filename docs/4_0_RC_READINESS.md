# 4.0.0 RC Readiness

This checklist determines whether the repository may create `4.0.0-rc.1`. It does not create a tag, change Rust defaults, or declare the release candidate ready.

> **Current decision: NOT READY FOR 4.0.0-rc.1.** The hybrid runtime quality gates are established, but measured Python coverage is 85.63% against the explicit 95.00% RC target, and five runtime architecture decisions or capabilities remain unresolved.

The machine-readable source of truth is [`release/4_0_rc_requirements.json`](../release/4_0_rc_requirements.json). Run the checker in report-only mode during normal development:

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
| Current Python coverage gate | Python Runtime | PASS: 85% | `pyproject.toml` keeps `fail_under = 85`. |
| Python measured coverage | Python Runtime | **BLOCK: 85.63% < 95.00%** | A full reproducible suite reports at least 95.00%; the RC target must not be lowered to the current gate. |
| Rust fmt / clippy / tests | Rust Core | PASS | `ci / rust` is green. |
| Rust sidecar Docker smoke | Rust Core | PASS | `ci / rust-docker` is green. |
| Hybrid runtime E2E and fallback | Hybrid Runtime | PASS | `ci / hybrid-runtime-e2e` is green. |
| Rust/Python RAG parity | RAG | PASS: 38/38 | `ci / rag-parity` is green and the full deterministic fixture remains present. |
| Policy deny / audit contract | Tool Runtime Security | PASS | Stable deny identifiers, redaction, failure modes, and no-execution tests pass. |
| Release preflight | Release Engineering | PASS | `ci / release-readiness` is green. |
| Default Python behavior unchanged | Runtime Architecture | PASS | `.env.example` keeps all four Rust delegates at `0`; default Compose remains Python-only. |
| Rollback path | Release Engineering | PASS | The runbook documents all-flags-off rollback and hybrid tests exercise fallback. |

### Decision and capability blockers

| Requirement | Owner | Current status | Exit condition |
| --- | --- | --- | --- |
| Rust default-on component set | Runtime Architecture | **BLOCK: no approved decision** | Approve and record which of Gateway, MCP, Policy, and RAG are default-on for 4.0. |
| Sidecar in default deployment | Release Engineering | **BLOCK: no approved decision** | Approve default packaging and lifecycle behavior, or explicitly approve an opt-in 4.0 design. |
| Python fallback lifecycle | Runtime Architecture | **BLOCK: no approved decision** | Record support duration, compatibility guarantees, and removal criteria. |
| Gateway streaming path | Gateway | **BLOCK: incomplete** | Implement Rust streaming or explicitly approve Python streaming as the 4.0 architecture. |
| MCP real tool bridge | MCP | **BLOCK: incomplete** | Bridge real tool execution through Rust or explicitly approve the split design for 4.0. |

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
| Gateway | Models and non-streaming chat delegation | Streaming still uses Python | Do not enable by default yet | Pending |
| MCP | JSON-RPC initialize, list, and deterministic calls | No real Python tool execution bridge | Do not enable by default yet | Pending |
| Policy | Stable deny/audit contract and explicit backend failure modes | Broader Python/Rust policy corpus can still grow | First default-on candidate, subject to a separate approval | Pending |
| RAG | Deterministic hot-path parity at 38/38 | Embedding and vector database work remains in Python | Keep opt-in | Pending |

This document records recommendations only. This 3.2.5 change does not modify `.env.example`, default Compose, or runtime flag defaults.

## Sign-off

Before creating `4.0.0-rc.1`, the accountable owners must sign the following on the release change:

- [ ] Release Engineering: all required CI jobs and release preflight are green on the exact release commit.
- [ ] Python Runtime: measured full-suite coverage is at least 95.00%.
- [ ] Rust Core: workspace, Docker smoke, hybrid E2E, and parity jobs are green.
- [ ] Tool Runtime Security: deny/audit contracts remain green and no denied tool implementation executes.
- [ ] Runtime Architecture: default-on components, default sidecar deployment, and Python fallback lifecycle are approved.
- [ ] Gateway: streaming ownership for 4.0 is approved.
- [ ] MCP: real tool bridge ownership for 4.0 is approved.
- [ ] Release Engineering: rollback is rehearsed and `--strict` exits zero.

Until every blocking requirement is satisfied, the decision remains **NOT READY FOR 4.0.0-rc.1**.

## Related documents

- [Pre-4.0 Quality Baseline](PRE_4_0_QUALITY_BASELINE.md)
- [3.1.x / 3.2.x Release Readiness](RELEASE_READINESS_3_1_X.md)
- [Rust Hybrid Runtime Runbook](RUST_HYBRID_RUNTIME_RUNBOOK.md)
- [Rust Migration Roadmap](RUST_MIGRATION_ROADMAP.md)
- [RAG Parity Baseline](RAG_PARITY_BASELINE.md)
