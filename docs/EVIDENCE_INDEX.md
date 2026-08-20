# Release Evidence Index

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Applicable version: v4.5.8.

## 4.5.8 development evidence contract

4.5.8 converges Repair, Replication, Rebalance, Drain, Retirement, Capacity,
and Transfer QoS into a durable Storage Control Plane. Local tests and legacy
contract runners validate deterministic behavior, but they cannot satisfy the
new real-infrastructure Gate. `scripts/run_storage_control_plane_minio_e2e.py`
is owned by the `storage-control-plane-minio-e2e` exact-merge CI producer and
requires three independent MinIO endpoints, boto3, `S3TargetStore`, production
Scheduler/BackupExecutor/Maintenance Supervisor paths, and the real randomized
Age helper. Missing prerequisites produce a FAIL report; fake S3, stub crypto,
or a previous-version artifact cannot be relabelled as PASS.

The typed source of truth is
`deepseek_infra/infra/diagnostics/evidence_inventory.py`. Exact-merge reports are
generated, provenance-validated, and assembled by their owning CI producers;
they are not committed or claimed as locally verified before that workflow
succeeds. For development setup and quality gates, see [AGENTS.md](../AGENTS.md).

## Required Evidence inventory

- `docs/evidence/headless-mcp-bridge.json`
- `docs/evidence/a2a-external-peer.json`
- `docs/evidence/ga-v4.5.8.json`
- `docs/evidence/workspace-v4.5.8.json`
- `docs/evidence/edge-router-v4.5.8.json`
- `docs/evidence/media-v4.5.8.json`
- `docs/evidence/browser-v4.5.8.json`
- `docs/evidence/automation-v4.5.8.json`
- `docs/evidence/skills-v4.5.8.json`
- `docs/evidence/skills-ui-v4.5.8.json`
- `docs/evidence/skill-builder-v4.5.8.json`
- `docs/evidence/skill-packs-v4.5.8.json`
- `docs/evidence/skill-eval-dashboard-v4.5.8.json`
- `docs/evidence/skill-versioning-v4.5.8.json`
- `docs/evidence/skill-analytics-v4.5.8.json`
- `docs/evidence/skill-security-v4.5.8.json`
- `docs/evidence/skill-catalog-v4.5.8.json`
- `docs/evidence/context-taint-v4.5.8.json`
- `docs/evidence/semantic-cache-onnx-v4.5.8.json`
- `docs/evidence/upgrade-rollback-v4.5.8.json`
- `docs/evidence/protocol-contract-v4.5.8.json`
- `docs/evidence/frontend-bundle-v4.5.8.json`
- `docs/evidence/frontend-browser-v4.5.8.json`
- `evals/reports/latest.json`
- `evals/reports/agent-latest.json`
- `evals/reports/baseline-compare-latest.json`
- `evals/reports/security-latest.json`
- `evals/reports/skills-v4.5.8.json`
- `evals/reports/media-v4.5.8.json`
- `evals/reports/browser-v4.5.8.json`
- `evals/reports/automation-v4.5.8.json`
- `docs/evidence/rust-sidecar-image-v4.5.8.json`
- `docs/evidence/hybrid-runtime-e2e-v4.5.8.json`
- `docs/evidence/gateway-request-parity-v4.5.8.json`
- `docs/evidence/mcp-protocol-parity-v4.5.8.json`
- `docs/evidence/rag-parity-v4.5.8.json`
- `docs/evidence/rag-document-preparation-parity-v4.5.8.json`
- `docs/evidence/rag-vector-binary-parity-v4.5.8.json`
- `docs/evidence/rust-coverage-v4.5.8.json`
- `docs/evidence/rust-sidecar-performance-v4.5.8.json`
- `docs/evidence/packed-delta-s3-v4.5.8.json`
- `docs/evidence/object-set-s3-v4.5.8.json`
- `docs/evidence/recovery-faults-v4.5.8.json`
- `docs/evidence/replica-healing-s3-v4.5.8.json`
- `docs/evidence/storage-control-plane-minio-v4.5.8.json`

## Historical 4.4.15 evidence contract

The following paths are the required CI output contract for 4.4.15. Files that depend on
exact-merge, Chromium, Rust Docker, or release packaging are produced by their owning
workflow and are not represented as locally verified until that workflow succeeds.

- `docs/evidence/headless-mcp-bridge.json`
- `docs/evidence/a2a-external-peer.json`
- `docs/evidence/ga-v4.4.15.json`
- `docs/evidence/workspace-v4.4.15.json`
- `docs/evidence/edge-router-v4.4.15.json`
- `docs/evidence/media-v4.4.15.json`
- `docs/evidence/browser-v4.4.15.json`
- `docs/evidence/automation-v4.4.15.json`
- `docs/evidence/skills-v4.4.15.json`
- `docs/evidence/skills-ui-v4.4.15.json`
- `docs/evidence/skill-builder-v4.4.15.json`
- `docs/evidence/skill-packs-v4.4.15.json`
- `docs/evidence/skill-eval-dashboard-v4.4.15.json`
- `docs/evidence/skill-versioning-v4.4.15.json`
- `docs/evidence/skill-analytics-v4.4.15.json`
- `docs/evidence/skill-security-v4.4.15.json`
- `docs/evidence/skill-catalog-v4.4.15.json`
- `docs/evidence/context-taint-v4.4.15.json`
- `docs/evidence/semantic-cache-onnx-v4.4.15.json`
- `docs/evidence/upgrade-rollback-v4.4.15.json`
- `docs/evidence/protocol-contract-v4.4.15.json`
- `docs/evidence/frontend-bundle-v4.4.15.json`
- `docs/evidence/frontend-browser-v4.4.15.json`
- `evals/reports/latest.json`
- `evals/reports/agent-latest.json`
- `evals/reports/baseline-compare-latest.json`
- `evals/reports/security-latest.json`
- `evals/reports/skills-v4.4.15.json`
- `evals/reports/media-v4.4.15.json`
- `evals/reports/browser-v4.4.15.json`
- `evals/reports/automation-v4.4.15.json`
- `docs/evidence/rust-sidecar-image-v4.4.15.json`
- `docs/evidence/hybrid-runtime-e2e-v4.4.15.json`
- `docs/evidence/gateway-request-parity-v4.4.15.json`
- `docs/evidence/mcp-protocol-parity-v4.4.15.json`
- `docs/evidence/rag-parity-v4.4.15.json`
- `docs/evidence/rag-document-preparation-parity-v4.4.15.json`
- `docs/evidence/rag-vector-binary-parity-v4.4.15.json`
- `docs/evidence/rust-coverage-v4.4.15.json`
- `docs/evidence/rust-sidecar-performance-v4.4.15.json`
- `docs/evidence/packed-delta-s3-v4.4.15.json`
- `docs/evidence/object-set-s3-v4.4.15.json`

4.4.15 is the encrypted-object-set and true-selective-fetch release. Full and Incremental snapshots share one projection pipeline; selection is validated against the verified final snapshot; adaptive deltas use bounded temporary archives and stop before encryption when oversized. New object-set lineages fetch independently encrypted controls first and only GET payload components in the Merkle-verified dependency closure. Receipt/Commit v4 exposes ciphertext digests and sizes without plaintext identity, while real process restart, holds, retention/GC and legacy Whole-Age v2-v5 compatibility remain hard gates. It retains the 4.4.13 projected-recovery contracts, 4.4.12 packed-delta index/container contracts, earlier fenced backup/restore and portability contracts, all 4.3.7 convergence contracts, and the frozen 4.0 protocol surface. The root `VERSION` file is canonical, and CI derives candidate/exact-merge evidence names and Docker tags from `RELEASE_VERSION`.

The typed source of truth is `deepseek_infra/infra/diagnostics/evidence_inventory.py`.
The current required contract is listed above. The detailed matrices below are
retained as historical ownership and reproduction references; their older
versioned paths are not 4.5.0 PASS Evidence. The optional Python stability
report remains informative.

## Historical workflow-only stateless MCP reliability gates

The stateless MCP service is validated by workflow checks rather than a committed release-evidence JSON. Do not treat these rows as exact-merge artifacts; the authoritative result is the GitHub check suite for the commit being evaluated.

| Check | Contract | Reproduction |
| --- | --- | --- |
| `stateless-mcp` | Official TypeScript SDK build, strict typecheck, unit tests for request isolation, auth/Host, workspace containment, idempotency, lease/fencing, retry client, and telemetry | `npm ci --prefix stateless-mcp` then `npm run check --prefix stateless-mcp` |
| `docker` | Stateless MCP image builds and `docker-compose.stateless-mcp.yml` resolves in addition to the default image/Compose | `docker build -f stateless-mcp/Dockerfile -t deepseek-stateless-mcp:test .` and `docker compose -f docker-compose.stateless-mcp.yml config` |
| `stateless-mcp-failover` | Two-instance round robin, owner termination, cross-instance client retry, expired-lease recovery, and idempotent replay convergence; stale-owner fencing is fixed by the unit gate | `npm run smoke:failover --prefix stateless-mcp` |
| `backup-crypto` | Secret-slot lifecycle, age locked inspect/unlock, wrong-secret rejection, no plaintext metadata leakage, encrypted Safety Backup inheritance, strict/best-effort coverage, and plaintext v1 compatibility | `pytest tests/test_backup_crypto.py tests/test_workspace_backups.py tests/test_web_workspace_routes.py` |
| `backup-crypto-rust` | Standard age v1 passphrase/X25519 streaming round trips, tamper rejection, recipient derivation and header inspection; helper build is locked and packaged with release artifacts | `cargo test --locked --manifest-path rust/Cargo.toml -p backup-crypto` |
| `backup-governance-4.4.4` | Scheduled policies, sealed frontend mirrors, unattended age round-trip, target markers, GFS retention, catalog rebuild, unlock drill | `pytest tests/test_backup_governance_contracts.py` |
| `backup-fenced-commit-4.4.5` | One formal commit per schedule slot, expired-lease rejection, target writer lease, crash orphan invisibility, blocked/superseded phases, catalog projection CAS, target lineage, retention snapshot CAS, mirror generation/epoch/sequence fences, recipient-variant isolation, client replica/sequence | `pytest tests/test_backup_fenced_commit_contracts.py` |
| `backup-remote-target-4.4.6` | Filesystem adapter parity, S3 conditional create/CAS writer lease, stale remote writer cannot commit, multipart checksum + resume, verified spool reuse, remote single slot commit, catalog head CAS, logical trash/GC reference protection, range restore resume, no cloud credentials in persistence, remote failure never falls back local | `pytest tests/test_backup_remote_target_contracts.py` |
| `backup-incremental-4.4.9` | Verified spool survives scheduler retry, remote commit crash reconcile, remote restore resume survives restart, target-session governance, incremental put/delete with coverage-safe tombstones, Merkle chain, missing parent fail-closed, pinned descendant protects ancestors, recipient rotation / index loss force full, adaptive checkpoint, payload reference dedupe | `pytest tests/test_backup_447_contracts.py` |
| `backup-streaming-delta-4.4.10` | Public remote materialize into federated restore, bounded parent/payload reads, v2/v3 protocol compatibility, Python/Rust parity, indexed parent lookup, actual-ratio plan freeze, bounded scan concurrency and fenced multipart resume | `pytest tests/test_backup_4410_contracts.py` + `cargo test --locked --manifest-path rust/Cargo.toml -p deepseek-backup` |
| `backup-effective-dedup-4.4.11` | Effective Chunk Ref inheritance, transactional immutable maps, immediate-parent cross-file/whole-file reuse, local Bloom plus exact batched lookup, v4 Parent Ranges, immutable-parent restore, batch helper telemetry, legacy migration/GC and strict multipart convergence | `pytest tests/test_backup_4411_contracts.py tests/test_backup_448_contracts.py tests/test_backup_4410_contracts.py` + `cargo test --locked --manifest-path rust/Cargo.toml -p deepseek-backup` |
| `backup-packed-delta-4.4.13` | Index v3 delta ops/current head, shared File Versions, PackWriter/typed refs, Pack/Blob/File/Merkle checks, persistent bounded scanner, GC/compaction, 100k-file scale and real HTTP S3 multipart restart/restore | `python scripts/run_packed_delta_s3_e2e.py --out docs/evidence/packed-delta-s3-v4.4.13.json` in the dedicated MinIO CI job |
| `stateless-mcp-portability` | Generation-fenced logical JSONL, no deployment secrets, running/queued to interrupted conversion, deterministic collision remap and retry convergence | `npm run check --prefix stateless-mcp` |

## Historical candidate tier

| Capability | Producer | Evidence / reproduction |
| --- | --- | --- |
| Headless MCP bridge | `release-readiness` | `docs/evidence/headless-mcp-bridge.json`; `scripts/smoke_mcp_headless_bridge.py` |
| A2A external peer | `release-readiness` | `docs/evidence/a2a-external-peer.json`; `scripts/smoke_a2a_external_peer.py` |
| Personal AI Runtime GA | `release-readiness` | `docs/evidence/ga-v4.5.6.json`; `scripts/smoke_ga.py` |
| Workspace Core | `release-readiness` | `docs/evidence/workspace-v4.5.6.json`; `scripts/smoke_workspace.py` |
| Edge Router stabilization | `release-readiness` | `docs/evidence/edge-router-v4.5.6.json`; `scripts/smoke_edge_router.py` |
| Media Layer | `release-readiness` / `eval` | `docs/evidence/media-v4.5.6.json`; `evals/reports/media-v4.5.6.json`; `scripts/smoke_media.py`; `run_media_eval.py` |
| Browser Control | `release-readiness` / `eval` | `docs/evidence/browser-v4.5.6.json`; `evals/reports/browser-v4.5.6.json`; `scripts/smoke_browser.py`; `run_browser_eval.py` |
| Automation Runtime | `release-readiness` / `eval` | `docs/evidence/automation-v4.5.6.json`; `evals/reports/automation-v4.5.6.json`; `scripts/smoke_automation.py`; `run_automation_eval.py` |
| Skill System | `release-readiness` | `docs/evidence/skills-v4.5.6.json`; `scripts/smoke_skills.py` |
| Skill Workbench UI | `release-readiness` | `docs/evidence/skills-ui-v4.5.6.json`; `scripts/smoke_skills_ui.py` |
| Skill Builder | `release-readiness` | `docs/evidence/skill-builder-v4.5.6.json`; `scripts/smoke_skill_builder.py` |
| Skill Packs | `release-readiness` | `docs/evidence/skill-packs-v4.5.6.json`; `scripts/smoke_skill_packs.py` |
| Skill Eval Dashboard | `release-readiness` / `eval` | `docs/evidence/skill-eval-dashboard-v4.5.6.json`; `evals/reports/skills-v4.5.6.json`; `scripts/smoke_skill_eval_dashboard.py` |
| Skill Versioning | `release-readiness` | `docs/evidence/skill-versioning-v4.5.6.json`; `scripts/smoke_skill_versioning.py` |
| Skill Analytics | `release-readiness` | `docs/evidence/skill-analytics-v4.5.6.json`; `scripts/smoke_skill_analytics.py` |
| Skill Security | `release-readiness` | `docs/evidence/skill-security-v4.5.6.json`; `scripts/smoke_skill_security.py` |
| Skill Catalog | `release-readiness` | `docs/evidence/skill-catalog-v4.5.6.json`; `scripts/smoke_skill_catalog.py` |
| Context Taint | `release-readiness` | `docs/evidence/context-taint-v4.5.6.json`; `scripts/smoke_context_taint.py` |
| Semantic Cache ONNX | `release-readiness` | `docs/evidence/semantic-cache-onnx-v4.5.6.json`; `benchmarks/bench_semantic_cache.py`; `docs/RUST_CANDIDATE_AUDIT_3_4.md` |
| Upgrade / rollback | `release-readiness` | `docs/evidence/upgrade-rollback-v4.5.6.json`; `scripts/generate_4_0_contract_evidence.py` |
| Protocol freeze | `release-readiness` | `docs/evidence/protocol-contract-v4.5.6.json`; `scripts/generate_4_0_contract_evidence.py` |
| Frontend bundle | `frontend` | `docs/evidence/frontend-bundle-v4.5.6.json`; `scripts/check_frontend_bundle.py`; Vite manifest |
| Frontend browser | `frontend-browser` | `docs/evidence/frontend-browser-v4.5.6.json`; `scripts/smoke_frontend_browser.py`; real Chromium |
| Offline eval suite | `eval` | `evals/reports/latest.json`; `evals/reports/agent-latest.json`; `evals/reports/baseline-compare-latest.json`; `evals/reports/security-latest.json` |

## Historical exact-merge tier

| Contract | Producer | Evidence / reproduction |
| --- | --- | --- |
| Rust sidecar image | `rust-docker` | `docs/evidence/rust-sidecar-image-v4.5.6.json`; immutable digest from the exact merge job |
| Hybrid runtime E2E | `hybrid-runtime-e2e` | `docs/evidence/hybrid-runtime-e2e-v4.5.6.json`; `scripts/smoke_hybrid_runtime.py` |
| Gateway request preparation | `gateway-request-parity` | `docs/evidence/gateway-request-parity-v4.5.6.json`; `docs/GATEWAY_REQUEST_PREPARATION_PARITY.md`; `fixtures/gateway/request_preparation_cases.json`; `scripts/check_gateway_request_parity.py` |
| MCP protocol preparation | `mcp-protocol-parity` | `docs/evidence/mcp-protocol-parity-v4.5.6.json`; `docs/MCP_PROTOCOL_PREPARATION_PARITY.md`; `scripts/check_mcp_protocol_parity.py` |
| RAG parity | `rag-parity` | `docs/evidence/rag-parity-v4.5.6.json`; `docs/RAG_PARITY_BASELINE.md`; `scripts/check_rag_parity.py` |
| RAG document preparation | `rag-document-preparation-parity` | `docs/evidence/rag-document-preparation-parity-v4.5.6.json`; `docs/RAG_DOCUMENT_PREPARATION_PARITY.md`; `fixtures/rag/document_preparation_cases.json`; `scripts/check_rag_document_preparation_parity.py` |
| RAG vector binary transport | `rag-vector-binary-parity` | `docs/evidence/rag-vector-binary-parity-v4.5.6.json`; `scripts/check_rag_vector_binary_parity.py`; `docs/RAG_VECTOR_BINARY_TRANSPORT.md` |
| Rust coverage | `rust-coverage` | `docs/evidence/rust-coverage-v4.5.6.json`; `scripts/run_rust_coverage.py`; line coverage >= 80% |
| Rust sidecar performance | `rust-sidecar-performance` | `docs/evidence/rust-sidecar-performance-v4.5.6.json`; `scripts/run_rust_sidecar_benchmarks.py`; `docs/RUST_SIDECAR_PERFORMANCE.md` |
| Packed Delta real S3 | `packed-delta-s3-e2e` | `docs/evidence/packed-delta-s3-v4.5.6.json`; pinned MinIO HTTP service; 100k-file scale + Multipart restart + v5 byte-exact restore |

## Historical optional and frozen compatibility evidence

- Optional test-producer report: `docs/evidence/python-coverage-stability-v4.4.13.json`.
- Python-owned semantic-cache binary embeddings remain covered by `docs/SEMANTIC_CACHE_BINARY_EMBEDDINGS.md`, `tests/test_semantic_cache_binary_embeddings.py` and `tests/test_semantic_cache_embedding_migration.py`, including direct BLOB assembly.
- GUI interoperability remains documented for Claude Desktop and Cursor. Third-party A2A ecosystem checks remain optional compatibility submissions and are not silently promoted into the GA inventory.

## Historical assembly and package outputs

- Source context: `docs/evidence/evidence-source-context-v4.4.13.json`.
- Manifest: `docs/evidence/evidence-manifest-v4.4.13.json`.
- Detached manifest checksum: `docs/evidence/evidence-manifest-v4.4.13.json.sha256`.
- Final CI Artifact: `release-evidence-v4.4.13`.
- Release archive: `dist/deepseek-infra-4.4.13.zip`, its `.sha256`, `.manifest.json` and final preflight report.

No 4.4.13 Evidence is generated or committed from a dirty working tree. Formal PASS claims belong to the exact-merge CI assembly and its downloadable artifacts.
