# 4.0.0-rc.2 Release Readiness

This checklist governs the `4.0.0-rc.2` freeze from the verified `3.10.0` hybrid runtime baseline. `v4.0.0-rc.1` is superseded and retained only as a historical architecture preview; it must not be promoted directly to stable. This PR creates neither a tag nor a GitHub Release.

> **Current decision: pending final local and GitHub CI validation.** The checker may emit `Decision: READY FOR 4.0.0-rc.2` only after every blocking requirement is supported by current-commit PASS evidence.

The machine contract is [`release/4_0_rc_requirements.json`](../release/4_0_rc_requirements.json), the approved ADR-0040 ownership decision is [`release/4_0_runtime_decision.json`](../release/4_0_runtime_decision.json), and the protocol freeze is [`release/4_0_protocol_contract.json`](../release/4_0_protocol_contract.json).

```bash
python scripts/check_4_0_rc_readiness.py --requirements release/4_0_rc_requirements.json --report-only --json-out artifacts/4-0-rc-readiness.json
python scripts/check_4_0_rc_readiness.py --requirements release/4_0_rc_requirements.json --strict
```

## Blocking matrix

| Requirement | Freeze condition |
| --- | --- |
| Python coverage | CI floor remains 95%; two complete rc.2 runs must each be at least 95.20%. |
| Rust measured line coverage | `cargo llvm-cov` measures `deepseek-core`, `deepseek-gateway`, `deepseek-mcp`, `deepseek-policy`, and `deepseek-rag`; line coverage must be at least 80% with no core exclusions. |
| Rust quality | Rust fmt, clippy with warnings denied, workspace tests, Rust Docker, and the independent `rust-coverage` job pass. |
| Parity | Gateway 68/68, MCP 105/105, RAG 38/38, RAG document 125/125, vector binary 110 valid + 16 malformed. |
| Upgrade and rollback | 3.10.0 and rc.1 upgrade, 3.10.0 rollback, Python-only startup, and sidecar-loss fallback pass without data loss. |
| Protocol freeze | All ten supported endpoints, schemas, media types, payload limits, errors, ownership, fallback, and stability classes match the frozen contract. |
| Product evidence | GA, Workspace, Media, Browser, Automation, Skills, security/catalog/builder/versioning, Edge Router, and Context Taint evidence is regenerated from the current commit. |
| Release archive | ZIP, checksum, and rich manifest agree; no credentials, user data, caches, databases, or sensitive benchmark payloads are included. |
| CI | Every job listed by `all_ci_jobs_green.required_jobs` passes on this exact branch commit. |

## Frozen architecture ownership

- Python is the default and authoritative runtime; default Compose is Python-only.
- The Rust sidecar is officially supported but optional. Every Rust delegate and binary transport remain default-off and explicit opt-ins.
- Python fallback is supported throughout 4.x and may not be removed before 5.0.0.
- Python owns Gateway streaming, upstream HTTP, credentials, retries, MCP transport/session/real tool execution, file reading, OCR, embedding, SQLite, indexes, and business state.
- Rust request preparation, MCP protocol preparation, Tool Policy evaluation, vector ranking, document preparation, and binary vector transport remain bounded optional delegates.
- Rust-primary ranking is not enabled.

## Delegate status

| Component | Proven surface | Freeze decision |
| --- | --- | --- |
| Gateway | Models and non-streaming chat delegation | Opt-in request preparation; streaming and upstream HTTP remain Python-owned. |
| MCP | JSON-RPC initialize, validation, and routing | Opt-in protocol preparation; transport, sessions, and execution remain Python-owned. |
| Policy | Stable deny/audit contract and failure modes | Opt-in evaluation with Python fallback. |
| RAG | Deterministic hot-path parity at 38/38 plus document and binary corpora | Opt-in preparation/ranking; files, embeddings, SQLite, and indexes remain Python-owned. |

## Advisory state

Expanded parity corpora and the hybrid performance benchmark are observed. Sidecar image size optimization and persistent policy audit storage remain advisory; their incomplete state must not be reported as green.

## Sign-off

- [ ] Two complete Python coverage runs are each at least 95.20%.
- [ ] Rust line coverage is at least 80% and the report covers all five crates.
- [ ] Upgrade, rollback, sidecar-loss, protocol, parity, performance, package, smoke, and preflight evidence is current-commit PASS.
- [ ] Strict readiness exits zero with `Decision: READY FOR 4.0.0-rc.2`.
- [ ] GitHub Actions is fully green on the PR head.
- [ ] No tag or GitHub Release was created.

Stable `4.0.0` remains out of scope and requires an independent promotion PR after the rc.2 observation period.
