# Release Evidence Index

Applicable version: v4.0.0.

This index binds the stable 4.0.0 metadata-only promotion to evidence regenerated for the promotion commit and, for exact-merge-only container identity, to the subsequent main-commit publication run. Every versioned JSON below records `version=4.0.0`, its exact validation `commit`, and `status=PASS`. The `v4.0.0-rc.1` and `v4.0.0-rc.2` files remain unchanged historical evidence; stable evidence is newly generated and never obtained by renaming an RC file.

## Runtime and product evidence

| Capability | Evidence | Reproduction |
| --- | --- | --- |
| Personal AI Runtime GA | `docs/evidence/ga-v4.0.0.json` | `python scripts/smoke_ga.py --offline --out docs/evidence/ga-v4.0.0.json` |
| Workspace Core | `docs/evidence/workspace-v4.0.0.json` | `python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v4.0.0.json` |
| Media Layer | `docs/evidence/media-v4.0.0.json`; `evals/reports/media-v4.0.0.json` | `scripts/smoke_media.py`; `run_media_eval.py` |
| Browser Control | `docs/evidence/browser-v4.0.0.json`; `evals/reports/browser-v4.0.0.json` | `scripts/smoke_browser.py`; `run_browser_eval.py` |
| Automation Runtime | `docs/evidence/automation-v4.0.0.json`; `evals/reports/automation-v4.0.0.json` | `scripts/smoke_automation.py`; `run_automation_eval.py` |
| Skill System | `docs/evidence/skills-v4.0.0.json` | `scripts/smoke_skills.py` |
| Skill Workbench UI | `docs/evidence/skills-ui-v4.0.0.json` | `scripts/smoke_skills_ui.py` |
| Skill Builder | `docs/evidence/skill-builder-v4.0.0.json` | `scripts/smoke_skill_builder.py` |
| Skill Packs | `docs/evidence/skill-packs-v4.0.0.json` | `scripts/smoke_skill_packs.py` |
| Skill Eval Dashboard | `docs/evidence/skill-eval-dashboard-v4.0.0.json`; `evals/reports/skills-v4.0.0.json` | `scripts/smoke_skill_eval_dashboard.py` |
| Skill Versioning | `docs/evidence/skill-versioning-v4.0.0.json` | `scripts/smoke_skill_versioning.py` |
| Skill Analytics | `docs/evidence/skill-analytics-v4.0.0.json` | `scripts/smoke_skill_analytics.py` |
| Skill Security | `docs/evidence/skill-security-v4.0.0.json` | `scripts/smoke_skill_security.py` |
| Skill Catalog | `docs/evidence/skill-catalog-v4.0.0.json` | `scripts/smoke_skill_catalog.py` |
| Edge Router stabilization | `docs/evidence/edge-router-v4.0.0.json` | `scripts/smoke_edge_router.py` |
| Context Taint | `docs/evidence/context-taint-v4.0.0.json` | `scripts/smoke_context_taint.py` |
| Semantic Cache benchmark | `docs/evidence/semantic-cache-onnx-v4.0.0.json`; `docs/RUST_CANDIDATE_AUDIT_3_4.md` | `benchmarks/bench_semantic_cache.py` |
| Semantic Cache binary embeddings | `docs/SEMANTIC_CACHE_BINARY_EMBEDDINGS.md`; Rust performance evidence | `tests/test_semantic_cache_binary_embeddings.py`; `tests/test_semantic_cache_embedding_migration.py`; direct BLOB assembly remains opt-in |

## Hybrid runtime evidence

| Contract | Evidence | Reproduction and frozen count |
| --- | --- | --- |
| Gateway request preparation | `docs/evidence/gateway-request-parity-v4.0.0.json`; `docs/GATEWAY_REQUEST_PREPARATION_PARITY.md` | `fixtures/gateway/request_preparation_cases.json`; `scripts/check_gateway_request_parity.py`; 68/68 |
| MCP protocol preparation | `docs/evidence/mcp-protocol-parity-v4.0.0.json`; `docs/MCP_PROTOCOL_PREPARATION_PARITY.md` | `fixtures/mcp/protocol_preparation_cases.json`; `scripts/check_mcp_protocol_parity.py`; 105/105 |
| RAG parity | `docs/evidence/rag-parity-v4.0.0.json`; `docs/RAG_PARITY_BASELINE.md` | `scripts/check_rag_parity.py`; 38/38 |
| RAG document preparation | `docs/evidence/rag-document-preparation-parity-v4.0.0.json`; `docs/RAG_DOCUMENT_PREPARATION_PARITY.md` | `fixtures/rag/document_preparation_cases.json`; `scripts/check_rag_document_preparation_parity.py`; 125/125 |
| RAG vector compact binary parity | `docs/evidence/rag-vector-binary-parity-v4.0.0.json`; `docs/RAG_VECTOR_BINARY_TRANSPORT.md` | `scripts/check_rag_vector_binary_parity.py`; 110 valid + 16 malformed |
| Rust sidecar performance | `docs/evidence/rust-sidecar-performance-v4.0.0.json`; `docs/RUST_SIDECAR_PERFORMANCE.md` | `scripts/run_rust_sidecar_benchmarks.py` |
| Rust workspace coverage | `docs/evidence/rust-coverage-v4.0.0.json`; `artifacts/rust-coverage.json`; `artifacts/rust-coverage.lcov` | `scripts/run_rust_coverage.py`; line coverage must be at least 80% |
| Rust sidecar image | `docs/evidence/rust-sidecar-image-v4.0.0.json` | exact image tag, immutable identity, and digest from CI |
| Hybrid E2E and sidecar loss | `docs/evidence/hybrid-runtime-e2e-v4.0.0.json` | `scripts/smoke_hybrid_runtime.py` |
| Upgrade and rollback | `docs/evidence/upgrade-rollback-v4.0.0.json`; `docs/UPGRADING_TO_4_0.md` | `tests/test_4_0_upgrade_contract.py` |
| Protocol freeze | `docs/evidence/protocol-contract-v4.0.0.json`; `release/4_0_protocol_contract.json` | `tests/test_4_0_protocol_contract.py` |

The binary request and response magic values remain `DSVRNK01` and `DSVRSP01`. Rust never reads SQLite, and binary failure falls directly to the Python ranking path without a second JSON Rust request.

## Evaluation reports

| Gate | Evidence |
| --- | --- |
| RAG and Tool eval | `evals/reports/latest.json` |
| Agent eval | `evals/reports/agent-latest.json` |
| Baseline comparison | `evals/reports/baseline-compare-latest.json` |
| Security corpus | `evals/reports/security-latest.json` |
| Skill eval | `evals/reports/skills-v4.0.0.json` |
| Media eval | `evals/reports/media-v4.0.0.json` |
| Browser eval | `evals/reports/browser-v4.0.0.json` |
| Automation eval | `evals/reports/automation-v4.0.0.json` |

## Compatibility and historical evidence

- Headless MCP bridge: `docs/evidence/headless-mcp-bridge.json`, generated by `scripts/smoke_mcp_headless_bridge.py`.
- External A2A peer: `docs/evidence/a2a-external-peer.json`, generated by `scripts/smoke_a2a_external_peer.py`.
- Third-party A2A ecosystem: `docs/evidence/a2a-third-party-peer.json`.
- GUI MCP interoperability records include Claude Desktop and Cursor.
- `v4.0.0-rc.1` and `v4.0.0-rc.2` release notes and JSON evidence are retained unchanged as historical qualification material.

## Release gates and publication artifacts

- Python statement and branch coverage: CI gate 95%; exact stable-promotion evidence must be at least 95.20%. Compact proof is `docs/evidence/python-coverage-stability-v4.0.0.json`.
- Rust line coverage: at least 80%, blocking in `rust-coverage`.
- Required CI includes test, security, eval, Docker, Rust Docker, hybrid-runtime-e2e, all five parity/performance jobs, rust-coverage, release-readiness, docs, and Rust.
- RC strict readiness remains historical qualification proof: `python scripts/check_4_0_rc_readiness.py --requirements release/4_0_rc_requirements.json --strict`.
- Stable preflight: `python scripts/preflight_release.py --version 4.0.0 --ga`.
- Promotion-PR dry-run: `python scripts/release.py --version 4.0.0 --coverage-gate 95% --dry-run`.
- Exact-merge publication produces `dist/deepseek-infra-4.0.0.zip`, `.zip.sha256`, and `.zip.manifest.json`, then verifies them before creating `v4.0.0` and the non-prerelease GitHub Release.
