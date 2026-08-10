# Release Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.4.13。

Use this checklist before tagging a release.

1. Bump the version across README badge, CHANGELOG, `settings.app_version`, Docker tag, Android `versionName` / `versionCode`, docs headers, evidence paths and eval report paths.
2. Run `python scripts/check_release_version.py`.
3. Run `python scripts/smoke_release.py --offline`.
4. Run `python scripts/preflight_release.py --version 4.4.13 --ga`.
5. Verify Edge Router stabilization evidence with `python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v4.4.13.json`.
6. Verify Context Taint evidence with `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v4.4.13.json`.
7. Verify encoding sanity: confirm `docs_encoding_sanity` is PASS and spot-check `rg -n "锟斤拷|鈥|鏋|杩|\\uFFFD|\\?\\?\\?" Dockerfile README.md CHANGELOG.md docs .github scripts deepseek_infra`.
8. Run `npm ci --prefix stateless-mcp` and `npm run check --prefix stateless-mcp`.
9. With Docker available, run `npm run smoke:failover --prefix stateless-mcp`; confirm round robin, owner loss, retry, lease recovery and idempotency all report `PASS`.
10. Verify the CI workflow triggered after a push, pull request or manual `workflow_dispatch` run, including `stateless-mcp`, `docker`, and `stateless-mcp-failover`.
11. Build both images with `docker build -t deepseek-infra:4.4.13 .` and `docker build -f stateless-mcp/Dockerfile -t deepseek-stateless-mcp:4.4.13 .`; validate both Compose files.
12. Run `pytest tests/test_backup_crypto.py tests/test_workspace_backups.py tests/test_web_workspace_routes.py` and confirm locked inspect, wrong-secret rejection, no plaintext metadata leakage, encrypted Safety Backup inheritance and coverage-policy contracts.
13. Run `cargo test --locked --manifest-path rust/Cargo.toml -p backup-crypto -p deepseek-backup`; confirm age round trips/tamper rejection plus exact v2/v3 chunk coverage and digest contracts. A missing native linker is a local environment failure, not a PASS.
14. Run the 4.4.4 governance contracts and the 4.4.5 fenced-commit contracts: `pytest tests/test_backup_governance_contracts.py tests/test_backup_fenced_commit_contracts.py tests/test_backup_lease_guard.py tests/test_backup_commit_markers.py tests/test_backup_writer_lease.py tests/test_backup_reconcile.py tests/test_backup_catalog_projection.py tests/test_backup_target_lineage.py tests/test_backup_retention_cas.py tests/test_backup_mirror_generation.py tests/test_backup_mirror_variants.py`.
15. Run frontend check: `npm run check --prefix frontend` (mirror leader-election unit tests included).
16. Verify an external Contributor backup with `coveragePolicy=strict`; confirm its JSONL contains no Redis URL/token/lease/fencing state and restored queued/running tasks remain `interrupted`.
17. Run `pytest tests/test_backup_4411_contracts.py tests/test_backup_4410_contracts.py tests/test_backup_448_contracts.py tests/test_backup_packed_delta_contracts.py` and confirm v2-v5 restore compatibility, Index v3 delta growth/current-head invariants, shared File Versions, packed/standalone thresholds, Pack/Range/File/Merkle verification, four-handle LRU, GC/compaction safety and 100k-file scale.
18. Run `cargo fmt --all -- --check`, `cargo clippy --locked --workspace --all-targets -- -D warnings` and `cargo test --locked --workspace`; confirm the persistent `deepseek-backup scan-batch --workers` pool streams bounded, parity-checked responses in a Rust-capable CI environment. A missing local MSVC linker is not a PASS.
19. Verify the dedicated `packed-delta-s3-e2e` CI job used the pinned MinIO service and produced `docs/evidence/packed-delta-s3-v4.4.13.json` from a real HTTP Full→v5 Multipart restart→Restore run. Do not claim PASS from a local skipped test.
20. Run `pip-audit -r requirements.txt -r requirements-dev.txt -r requirements-browser.txt -r requirements-s3-e2e.txt` so the CI-only S3 dependency is included in the dependency gate.
21. Verify the projected-recovery and production-remote-restore contracts: `pytest tests/test_backup_projection.py tests/test_backup_projected_materialize.py tests/test_backup_remote_restore_projection.py tests/test_backup_remote_restore_projection_e2e.py` and confirm selection freeze + `409 restore-selection-mismatch`, output/support separation, full logical-chain Merkle verification, whole-age-object reporting, hold lifecycle and unselected-contributor isolation.
22. Verify the production remote restore E2E in the `packed-delta-s3-e2e` CI job produced `docs/evidence/packed-delta-s3-v4.4.13.json` using the real Rust Age helper (built in that job) through Executor → S3 → Receipt → Restore → Federated Commit/Complete; do not claim PASS from a locally skipped test.
23. Generate the release zip, manifest and SHA-256 checksum with `python scripts/release.py --clean-workspace --version 4.4.13`; confirm both platform helpers (`backup-crypto` and `deepseek-backup`) are bundled.

The stateless MCP Redis volume remains operational state and is never copied directly. Preserve it explicitly, or configure the external Contributor to include a portable logical snapshot in Workspace Backup.
