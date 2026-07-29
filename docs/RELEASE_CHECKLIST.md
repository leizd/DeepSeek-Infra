# Release Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.4.1。

Use this checklist before tagging a release.

1. Bump the version across README badge, CHANGELOG, `settings.app_version`, Docker tag, Android `versionName` / `versionCode`, docs headers, evidence paths and eval report paths.
2. Run `python scripts/smoke_release.py --offline`.
3. Run `python scripts/preflight_release.py --version 4.4.1 --ga`.
4. Verify Edge Router stabilization evidence with `python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v4.4.1.json`.
5. Verify Context Taint evidence with `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v4.4.1.json`.
6. Verify encoding sanity: confirm `docs_encoding_sanity` is PASS and spot-check `rg -n "锟斤拷|鈥|鏋|杩|\\uFFFD|\\?\\?\\?" Dockerfile README.md CHANGELOG.md docs .github scripts deepseek_infra`.
7. Run `npm ci --prefix stateless-mcp` and `npm run check --prefix stateless-mcp`.
8. With Docker available, run `npm run smoke:failover --prefix stateless-mcp`; confirm round robin, owner loss, retry, lease recovery and idempotency all report `PASS`.
9. Verify the CI workflow triggered after a push, pull request or manual `workflow_dispatch` run, including `stateless-mcp`, `docker`, and `stateless-mcp-failover`.
10. Build both images with `docker build -t deepseek-infra:4.4.1 .` and `docker build -f stateless-mcp/Dockerfile -t deepseek-stateless-mcp:4.4.1 .`; validate both Compose files.
11. Generate the release zip, manifest and SHA-256 checksum with `python scripts/release.py --clean-workspace --version 4.4.1`.

The stateless MCP Redis volume is operational state, not part of the release archive or default `/data` backup. Preserve or destroy it explicitly according to the deployment policy.
