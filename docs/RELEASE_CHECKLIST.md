# Release Checklist

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：从仓库根目录 `VERSION` 派生（当前以 `cat VERSION` 为准）。

Use this checklist before tagging a release. Prefer `RELEASE_VERSION=$(cat VERSION)`
(or PowerShell `Get-Content VERSION`) everywhere — never hand-copy a stale semver.

```bash
# POSIX / Git Bash / CI
export RELEASE_VERSION="$(cat VERSION)"

# PowerShell
# $env:RELEASE_VERSION = (Get-Content -Raw VERSION).Trim()
```

1. Bump the version across every surface checked by `python scripts/check_release_version.py --require-release-note`.
2. Run `python scripts/check_release_version.py --require-release-note --strict-branch`.
3. Run `python scripts/smoke_release.py --offline`.
4. Run `python scripts/preflight_release.py --version "$RELEASE_VERSION" --ga`.
5. Verify Edge Router stabilization evidence with `python scripts/smoke_edge_router.py --offline --out "docs/evidence/edge-router-v${RELEASE_VERSION}.json"`.
6. Verify Context Taint evidence with `python scripts/smoke_context_taint.py --offline --out "docs/evidence/context-taint-v${RELEASE_VERSION}.json"`.
7. Verify encoding sanity: confirm `docs_encoding_sanity` is PASS and spot-check `rg -n "锟斤拷|鈥|鏋|杩|\uFFFD|\?\?\?" Dockerfile README.md CHANGELOG.md docs .github scripts deepseek_infra`.
8. Run `npm ci --prefix stateless-mcp` and `npm run check --prefix stateless-mcp`.
9. With Docker available, run `npm run smoke:failover --prefix stateless-mcp`; confirm round robin, owner loss, retry, lease recovery and idempotency all report `PASS`.
10. Verify the CI workflow triggered after a push, pull request or manual `workflow_dispatch` run, including `stateless-mcp`, `docker`, and `stateless-mcp-failover`. **Do not merge until every required check is green.**
11. Build both images with `docker build -t "deepseek-infra:${RELEASE_VERSION}" .` and `docker build -f stateless-mcp/Dockerfile -t "deepseek-stateless-mcp:${RELEASE_VERSION}" .`; validate both Compose files.
12. Run `pytest tests/test_backup_crypto.py tests/test_workspace_backups.py tests/test_web_workspace_routes.py` and confirm locked inspect, wrong-secret rejection, no plaintext metadata leakage, encrypted Safety Backup inheritance and coverage-policy contracts.
13. Run `cargo test --locked --manifest-path rust/Cargo.toml -p backup-crypto -p deepseek-backup`; confirm age round trips/tamper rejection plus exact v2/v3 chunk coverage and digest contracts. A missing native linker is a local environment failure, not a PASS.
14. Run the governance and fenced-commit contracts: `pytest tests/test_backup_governance_contracts.py tests/test_backup_fenced_commit_contracts.py tests/test_backup_lease_guard.py tests/test_backup_commit_markers.py tests/test_backup_writer_lease.py tests/test_backup_reconcile.py tests/test_backup_catalog_projection.py tests/test_backup_target_lineage.py tests/test_backup_retention_cas.py tests/test_backup_mirror_generation.py tests/test_backup_mirror_variants.py`.
15. Run frontend check: `npm run check --prefix frontend` (mirror leader-election unit tests included).
16. Verify an external Contributor backup with `coveragePolicy=strict`; confirm its JSONL contains no Redis URL/token/lease/fencing state and restored queued/running tasks remain `interrupted`.
17. Run `pytest tests/test_backup_4411_contracts.py tests/test_backup_4410_contracts.py tests/test_backup_448_contracts.py tests/test_backup_packed_delta_contracts.py` and confirm v2-v5 restore compatibility, Index v3 delta growth/current-head invariants, shared File Versions, packed/standalone thresholds, Pack/Range/File/Merkle verification, four-handle LRU, GC/compaction safety and 100k-file scale.
18. Run `cargo fmt --all -- --check`, `cargo clippy --locked --workspace --all-targets -- -D warnings` and `cargo test --locked --workspace`; confirm the persistent `deepseek-backup scan-batch --workers` pool streams bounded, parity-checked responses in a Rust-capable CI environment. A missing local MSVC linker is not a PASS.
19. Verify the dedicated `packed-delta-s3-e2e` CI job used the pinned MinIO service and produced `docs/evidence/packed-delta-s3-v${RELEASE_VERSION}.json` from a real HTTP Full→v5 Multipart restart→Restore run. Do not claim PASS from a local skipped test.
20. Run `pip-audit -r requirements.txt -r requirements-dev.txt -r requirements-browser.txt -r requirements-s3-e2e.txt` so the CI-only S3 dependency is included in the dependency gate.
21. Verify the projected-recovery and production-remote-restore contracts: `pytest tests/test_backup_projection.py tests/test_backup_projected_materialize.py tests/test_backup_remote_restore_projection.py tests/test_backup_remote_restore_projection_e2e.py` and confirm selection freeze + `409 restore-selection-mismatch`, output/support separation, full logical-chain Merkle verification, whole-age-object reporting, hold lifecycle and unselected-contributor isolation.
22. Verify the production remote restore E2E in the `packed-delta-s3-e2e` CI job produced `docs/evidence/packed-delta-s3-v${RELEASE_VERSION}.json` using the real Rust Age helper (built in that job) through Executor → S3 → Receipt → Restore → Federated Commit/Complete; do not claim PASS from a locally skipped test.
23. Generate the release zip, manifest and SHA-256 checksum with `python scripts/release.py --clean-workspace --version "$RELEASE_VERSION"`; confirm both platform helpers (`backup-crypto` and `deepseek-backup`) are bundled.
24. Confirm Python coverage is `>=95%` on the 3.10 / 3.11 / 3.12 matrix (`pytest --cov --cov-fail-under=95`) before tagging.

## 4.8.0 Federation qualification addendum

1. Confirm the Gate A history precedes every Federation write path: immutable
   schedule digest, terminal/running rewrite rejection, renewable schedule/wave
   leases, lease-loss fencing, and real two-process takeover with higher
   schedule/wave/action epochs and one underlying provider effect.
2. Confirm Fleet A and B use independent ROOT directories, Authority stores,
   federation roots/signers, SQLite journals, HTTP processes, storage targets,
   and long-lived storage principals. Attempt cross-auth in both directions and
   require provider denial.
3. Verify operators pinned each exact federation root fingerprint and
   provider/region/jurisdiction/siteClass metadata before activation. TOFU,
   same-Fleet identity, root collision, stale signer sequence, expired signer,
   and revoked signer must fail closed.
4. Verify signed readiness binds the complete canonical snapshot rather than only
   `riskDigest`; verify challenge nonce replay, wrong Fleet IDs, future timestamp,
   expiry, and revoked identity rejection.
5. Verify Receiver-signed ingress grants bind source/destination, transfer,
   policy, backup, object-set digest, prefix, max bytes, nonce, and expiry. Sender
   process/config/Evidence must contain no Receiver long-lived storage credential.
6. Verify same transfer ID/same digest resumes, same ID/different digest fails,
   and every partition or unknown remote outcome performs GET/reconcile before
   write retry.
7. Verify the Federation scenario uses four logical real MinIO targets A1/A2/B1/B2
   across two independent Fleet processes. The shared producer may start a fifth
   legacy target, but it must not be counted as a federated replica.
8. SIGKILL Receiver during a real component upload, restart a new PID with the
   same durable state, resume the same transfer ID, and prove exactly one Receipt
   v4/Commit v4 effect. Replay the old grant, tamper the replica attestation, and
   revoke the peer; all must fail closed.
9. Verify local `minCommittedCopies` and `minFailureDomains` are unchanged before/
   after Federation. No remote copy may authorize promotion, policy mutation,
   local pruning, or delete.
10. Run the recovery-capable production restore into an isolated workspace using
    an Age identity provisioned outside Federation. Require exact transfer/backup/
    object-set/Receipt/Commit/workspace binding and successful cleanup; confirm no
    Age private identity crosses config, HTTP, journal, or proof boundaries.
11. Download and independently validate the exact CI artifacts with
    `scripts/validate_evidence_proof.py`:
    `federation-trust-proof-v${RELEASE_VERSION}.json`,
    `federated-replica-proof-v${RELEASE_VERSION}.json`, and
    `federated-dr-proof-v${RELEASE_VERSION}.json`. Match each report path,
    scenario, SHA-256, and byte size before Evidence Assembly.
12. Re-run frozen compatibility checks for `object-set-v1`, Receipt v4, Commit v4,
    FastCDC v3, Projection semantics, randomized Age, `control-authority-v1`,
    AuthorityCheckpoint v1, `dr-readiness-proof-v1`, the `evidence-proof-v2`
    envelope, and `predictive-planning-proof-v1`. No new Federation document may
    be represented as a storage wire revision.
13. Do not tag or call 4.8.0 release-ready from local MinIO success. Require the
    final PR head/merge SHA's Python 3.10/3.11/3.12, frontend, eval, security,
    five-MinIO producer, exact Evidence Assembly, RC readiness, and package gates.

The stateless MCP Redis volume remains operational state and is never copied directly. Preserve it explicitly, or configure the external Contributor to include a portable logical snapshot in Workspace Backup.
