# Release Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.4.3。

Use this checklist before tagging a release.

1. Bump the version across README badge, CHANGELOG, `settings.app_version`, Docker tag, Android `versionName` / `versionCode`, docs headers, evidence paths and eval report paths.
2. Run `python scripts/smoke_release.py --offline`.
3. Run `python scripts/preflight_release.py --version 4.4.3 --ga`.
4. Verify Edge Router stabilization evidence with `python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v4.4.3.json`.
5. Verify Context Taint evidence with `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v4.4.3.json`.
6. Verify encoding sanity: confirm `docs_encoding_sanity` is PASS and spot-check `rg -n "锟斤拷|鈥|鏋|杩|\\uFFFD|\\?\\?\\?" Dockerfile README.md CHANGELOG.md docs .github scripts deepseek_infra`.
7. Run `npm ci --prefix stateless-mcp` and `npm run check --prefix stateless-mcp`.
8. With Docker available, run `npm run smoke:failover --prefix stateless-mcp`; confirm round robin, owner loss, retry, lease recovery and idempotency all report `PASS`.
9. Verify the CI workflow triggered after a push, pull request or manual `workflow_dispatch` run, including `stateless-mcp`, `docker`, and `stateless-mcp-failover`.
10. Build both images with `docker build -t deepseek-infra:4.4.3 .` and `docker build -f stateless-mcp/Dockerfile -t deepseek-stateless-mcp:4.4.3 .`; validate both Compose files.
11. Run `pytest tests/test_backup_crypto.py tests/test_workspace_backups.py tests/test_web_workspace_routes.py` and confirm locked inspect, wrong-secret rejection, no plaintext metadata leakage, encrypted Safety Backup inheritance and coverage-policy contracts.
12. Run `cargo test --locked --manifest-path rust/Cargo.toml -p backup-crypto`; confirm passphrase/X25519 round trips and tamper rejection. A missing native linker is a local environment failure, not a PASS.
13. Verify an external Contributor backup with `coveragePolicy=strict`; confirm its JSONL contains no Redis URL/token/lease/fencing state and restored queued/running tasks remain `interrupted`.
14. Generate the release zip, manifest and SHA-256 checksum with `python scripts/release.py --clean-workspace --version 4.4.3`; confirm the platform `backup-crypto` helper is bundled.

The stateless MCP Redis volume remains operational state and is never copied directly. Preserve it explicitly, or configure the 4.4.3 external Contributor to include a portable logical snapshot in Workspace Backup.
