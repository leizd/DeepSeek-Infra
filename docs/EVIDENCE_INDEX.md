# Release Evidence Index

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Applicable version: v4.3.4.

4.3.4 is the Reload Transaction Integrity and Page-Lifecycle Recovery release. Frontend producers prove that update activation runs as one serialized single-flight transaction, update checks are timeout-bounded and superseded by newer targets, deferral leaves no half-applied activation state, Composer drafts and conversation state flush on pagehide/visibilitychange/beforeunload, drafts are scoped per conversation and project, and message submission commits atomically through `tryStartMessage`/`peek`/`commit`. All 4.3.3 stable deployment discovery, waiting Worker consent, reload blocker, verified-controller and cross-tab coordination contracts remain active, together with 4.3.2 immutable build identity, 4.3.1 continuity and 4.3.0 demand-loading/budget gates. The 4.2.8 exact-merge chain remains authoritative: `evidence-context` captures one clean schema-v2 source identity, isolated producer Artifacts carry that identity, `evidence-assembly` validates ownership/completeness/PASS state/exact `GITHUB_SHA`, and `release-package` independently verifies the real ZIP. The frozen 4.0 protocol remains unchanged.

The typed source of truth is `deepseek_infra/infra/diagnostics/evidence_inventory.py`. Candidate and exact-merge entries below are all required for GA; the optional Python stability report is informative.

## Candidate tier

| Capability | Producer | Evidence / reproduction |
| --- | --- | --- |
| Headless MCP bridge | `release-readiness` | `docs/evidence/headless-mcp-bridge.json`; `scripts/smoke_mcp_headless_bridge.py` |
| A2A external peer | `release-readiness` | `docs/evidence/a2a-external-peer.json`; `scripts/smoke_a2a_external_peer.py` |
| Personal AI Runtime GA | `release-readiness` | `docs/evidence/ga-v4.3.4.json`; `scripts/smoke_ga.py` |
| Workspace Core | `release-readiness` | `docs/evidence/workspace-v4.3.4.json`; `scripts/smoke_workspace.py` |
| Edge Router stabilization | `release-readiness` | `docs/evidence/edge-router-v4.3.4.json`; `scripts/smoke_edge_router.py` |
| Media Layer | `release-readiness` / `eval` | `docs/evidence/media-v4.3.4.json`; `evals/reports/media-v4.3.4.json`; `scripts/smoke_media.py`; `run_media_eval.py` |
| Browser Control | `release-readiness` / `eval` | `docs/evidence/browser-v4.3.4.json`; `evals/reports/browser-v4.3.4.json`; `scripts/smoke_browser.py`; `run_browser_eval.py` |
| Automation Runtime | `release-readiness` / `eval` | `docs/evidence/automation-v4.3.4.json`; `evals/reports/automation-v4.3.4.json`; `scripts/smoke_automation.py`; `run_automation_eval.py` |
| Skill System | `release-readiness` | `docs/evidence/skills-v4.3.4.json`; `scripts/smoke_skills.py` |
| Skill Workbench UI | `release-readiness` | `docs/evidence/skills-ui-v4.3.4.json`; `scripts/smoke_skills_ui.py` |
| Skill Builder | `release-readiness` | `docs/evidence/skill-builder-v4.3.4.json`; `scripts/smoke_skill_builder.py` |
| Skill Packs | `release-readiness` | `docs/evidence/skill-packs-v4.3.4.json`; `scripts/smoke_skill_packs.py` |
| Skill Eval Dashboard | `release-readiness` / `eval` | `docs/evidence/skill-eval-dashboard-v4.3.4.json`; `evals/reports/skills-v4.3.4.json`; `scripts/smoke_skill_eval_dashboard.py` |
| Skill Versioning | `release-readiness` | `docs/evidence/skill-versioning-v4.3.4.json`; `scripts/smoke_skill_versioning.py` |
| Skill Analytics | `release-readiness` | `docs/evidence/skill-analytics-v4.3.4.json`; `scripts/smoke_skill_analytics.py` |
| Skill Security | `release-readiness` | `docs/evidence/skill-security-v4.3.4.json`; `scripts/smoke_skill_security.py` |
| Skill Catalog | `release-readiness` | `docs/evidence/skill-catalog-v4.3.4.json`; `scripts/smoke_skill_catalog.py` |
| Context Taint | `release-readiness` | `docs/evidence/context-taint-v4.3.4.json`; `scripts/smoke_context_taint.py` |
| Semantic Cache ONNX | `release-readiness` | `docs/evidence/semantic-cache-onnx-v4.3.4.json`; `benchmarks/bench_semantic_cache.py`; `docs/RUST_CANDIDATE_AUDIT_3_4.md` |
| Upgrade / rollback | `release-readiness` | `docs/evidence/upgrade-rollback-v4.3.4.json`; `scripts/generate_4_0_contract_evidence.py` |
| Protocol freeze | `release-readiness` | `docs/evidence/protocol-contract-v4.3.4.json`; `scripts/generate_4_0_contract_evidence.py` |
| Frontend bundle | `frontend` | `docs/evidence/frontend-bundle-v4.3.4.json`; `scripts/check_frontend_bundle.py`; Vite manifest |
| Frontend browser | `frontend-browser` | `docs/evidence/frontend-browser-v4.3.4.json`; `scripts/smoke_frontend_browser.py`; real Chromium |
| Offline eval suite | `eval` | `evals/reports/latest.json`; `evals/reports/agent-latest.json`; `evals/reports/baseline-compare-latest.json`; `evals/reports/security-latest.json` |

## Exact-merge tier

| Contract | Producer | Evidence / reproduction |
| --- | --- | --- |
| Rust sidecar image | `rust-docker` | `docs/evidence/rust-sidecar-image-v4.3.4.json`; immutable digest from the exact merge job |
| Hybrid runtime E2E | `hybrid-runtime-e2e` | `docs/evidence/hybrid-runtime-e2e-v4.3.4.json`; `scripts/smoke_hybrid_runtime.py` |
| Gateway request preparation | `gateway-request-parity` | `docs/evidence/gateway-request-parity-v4.3.4.json`; `docs/GATEWAY_REQUEST_PREPARATION_PARITY.md`; `fixtures/gateway/request_preparation_cases.json`; `scripts/check_gateway_request_parity.py` |
| MCP protocol preparation | `mcp-protocol-parity` | `docs/evidence/mcp-protocol-parity-v4.3.4.json`; `docs/MCP_PROTOCOL_PREPARATION_PARITY.md`; `scripts/check_mcp_protocol_parity.py` |
| RAG parity | `rag-parity` | `docs/evidence/rag-parity-v4.3.4.json`; `docs/RAG_PARITY_BASELINE.md`; `scripts/check_rag_parity.py` |
| RAG document preparation | `rag-document-preparation-parity` | `docs/evidence/rag-document-preparation-parity-v4.3.4.json`; `docs/RAG_DOCUMENT_PREPARATION_PARITY.md`; `fixtures/rag/document_preparation_cases.json`; `scripts/check_rag_document_preparation_parity.py` |
| RAG vector binary transport | `rag-vector-binary-parity` | `docs/evidence/rag-vector-binary-parity-v4.3.4.json`; `scripts/check_rag_vector_binary_parity.py`; `docs/RAG_VECTOR_BINARY_TRANSPORT.md` |
| Rust coverage | `rust-coverage` | `docs/evidence/rust-coverage-v4.3.4.json`; `scripts/run_rust_coverage.py`; line coverage >= 80% |
| Rust sidecar performance | `rust-sidecar-performance` | `docs/evidence/rust-sidecar-performance-v4.3.4.json`; `scripts/run_rust_sidecar_benchmarks.py`; `docs/RUST_SIDECAR_PERFORMANCE.md` |

## Optional and frozen compatibility evidence

- Optional test-producer report: `docs/evidence/python-coverage-stability-v4.3.4.json`.
- Python-owned semantic-cache binary embeddings remain covered by `docs/SEMANTIC_CACHE_BINARY_EMBEDDINGS.md`, `tests/test_semantic_cache_binary_embeddings.py` and `tests/test_semantic_cache_embedding_migration.py`, including direct BLOB assembly.
- GUI interoperability remains documented for Claude Desktop and Cursor. Third-party A2A ecosystem checks remain optional compatibility submissions and are not silently promoted into the GA inventory.

## Assembly and package outputs

- Source context: `docs/evidence/evidence-source-context-v4.3.4.json`.
- Manifest: `docs/evidence/evidence-manifest-v4.3.4.json`.
- Detached manifest checksum: `docs/evidence/evidence-manifest-v4.3.4.json.sha256`.
- Final CI Artifact: `release-evidence-v4.3.4`.
- Release archive: `dist/deepseek-infra-4.3.4.zip`, its `.sha256`, `.manifest.json` and final preflight report.

No 4.3.4 Evidence is generated or committed from a dirty working tree. Formal PASS claims belong to the exact-merge CI assembly and its downloadable artifacts.
