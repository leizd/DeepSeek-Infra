# Release Checklist

适用版本：v2.9.1。

Use this checklist before tagging a release.

1. Bump the version across README badge, CHANGELOG, `settings.app_version`, Docker tag, Android `versionName` / `versionCode`, docs headers, evidence paths and eval report paths.
2. Run `python scripts/smoke_release.py --offline`.
3. Run `python scripts/preflight_release.py --version 2.9.1`.
4. Verify Edge Router stabilization evidence with `python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v2.9.1.json`.
5. Verify Context Taint evidence with `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v2.9.1.json`.
6. Verify encoding sanity: confirm `docs_encoding_sanity` is PASS and spot-check `rg -n "锟斤拷|鈥|鏋|杩|\\uFFFD|\\?\\?\\?" Dockerfile README.md CHANGELOG.md docs .github scripts deepseek_infra`.
7. Verify the CI workflow triggered after a push, pull request or manual `workflow_dispatch` run.
8. Build the Docker image with `docker build -t deepseek-infra:2.9.1 .`.
9. Generate the release zip, manifest and SHA-256 checksum with `python scripts/release.py --clean-workspace --version 2.9.1`.
