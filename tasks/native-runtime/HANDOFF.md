# Handoff — native runtime migration state (2026-11-07)

**Overall status: ACTIVE / NOT_READY.** The 5.0 native Rust+Go runtime objective
is not complete; this document records exactly what is qualified, where the
evidence is, and what remains. Do not treat any route slice below as completion
of the whole objective.

## Qualified source (cloud-verified)

| Item | Value |
| :--- | :--- |
| Branch | `codex/native-gateway-download-save-20261107` |
| Commit | `9a74dc56557b474eeef2990a6d032c788ba588ee` |
| Tree | `29b0c5d4ae5eb15dea446ffa51f829730c520b08` |
| Cloud run | [rust-cloud 42346115402](https://github.com/leizd/DeepSeek-Infra/actions/runs/42346115402) — Rust checks success, Rust coverage success |
| Coverage | status PASS, 101877/122589 = 83.1045% lines, unchanged 80% floor, `sourceTreeDirty: false`, `testedRevision` = the commit above |
| Evidence | `artifacts/native-20261107-download-save-route/` (cloud-evidence.json, received-reports/, review.md, full.diff, manifest.json, local-validation.json, continuation-checkpoint.json, prior-failure-evidence.json) |

This source contains four migrated native-edge route slices, all re-qualified by
the same run:

1. **Public auth host/refusal semantics** (`auth.rs`, `openai_facade.rs` LAN
   allow list, flat `{"error","code"}` envelopes for `/api/*`, `/v1/*`, `/mcp`,
   `/a2a*`; `tests/auth_semantics.rs` 20-case Python oracle).
2. **`POST /api/fetch-url`** (`fetch_url_route.rs`): original `read_json_body`
   contract, URL coercion, `fetch_policy` reuse, non-POST routing;
   `tests/fetch_url_route.rs` 17-case oracle over a seeded cache.
3. **`POST /api/auth/logout`** (`auth_logout_route.rs`): exact expired
   `Set-Cookie` bytes and original routing; `tests/auth_logout_route.rs`.
4. **`POST /api/download-save`** (`download_save_route.rs`) plus shared
   `route_support.rs` (read_json_body, error envelopes, flat 404 / global
   OPTIONS / 405 fallbacks); `tests/download_save_route.rs` runs oracle and
   native in separate home directories.

Earlier qualified slices that stay valid as history:
`codex/native-media-multipart-process-20261107` `eec2853f` (cloud runs green on
the exact source), `codex/native-mcp-status-route-20261107` `4394329d`
(rust-cloud green), and the parent media-diagnostics chains listed in
`continuation.md`.

## Branch/lineage facts

- Root checkout `D:\deepseek` stays untouched by implementation (branch
  `codex/native-runtime-pr/20261014`, HEAD `b0316d09811d66ae3e47fbc4bbea3b4ddc43d4ca`,
  dirty/untracked user files preserved). All migration work happens in isolated
  worktrees (`D:\deepseek-worktrees\tip20261106` here; `auth-fix` for the auth
  branch).
- Validation branches are independent: `codex/native-gateway-download-save-...`
  is the currently qualified head. Do not force-push or move it; put new slices
  on new branches stacked on the qualified commit.
- Docs edits made after qualification are intentionally left uncommitted so the
  branch head stays the qualified commit; they are folded into the next slice
  commit.
- `artifacts/` is gitignored; root `D:\deepseek\artifacts\` holds mirrored
  copies so evidence is readable without the worktree.

## Remaining work (ordered)

### 1. Native edge route coverage (89 unmigrated paths)

Refreshed audit (Python 209 app routes; Rust 99 route declarations; 14 of the
103 missing paths are Go-served control/media families):

- 71 `/api/workspace/*` backup/DR/resilience routes — plan in
  `tasks/native-runtime/workspace-backup-dr-migration-plan.md`:
  S0 prerequisites (Rust data-domain declarations for backup/restore/DR stores
  + export/import/integrity/fencing/rollback tooling + Go control authority),
  S1 read-only control routes, S2 catalog/index, S3 retention, S4
  policies/targets (Go control), S5 restores, S6 disaster recovery, S7
  resilience, S8 workspace artifacts/exports/provenance.
- 7 `/api/traces*` and `/trace/{trace_id}` — trace store domain
  (`observability_trace_store` python→rust) needs its cutover plus read routes.
- 11 top-level routes: `/api/agent-runs/{run_id}/{action}` unknown-action 404
  parity, `/api/automation` root GET/POST (12 actions incl. runner/scheduler),
  `/api/compress-context`, `/api/edge/reload` and `/api/edge/route-preview`,
  `/api/mcp/external/tools`, `/api/rag/{eval,reindex,verify-citation}` writes,
  `/api/semantic-cache` and `/api/semantic-cache/status`, `/api/share-target`,
  `/share-target`.
  Route notes: `/api/media/{media_id}/process` is Go-served; confirm the Rust
  edge forwards it before claiming parity.

### 2. Ownership inversion execution

- Add the backup/restore/DR/resilience stores to `RUST_DATA_DOMAINS`
  (`deepseek_infra/infra/native_runtime/authority.py`) with their Python write
  choke points gated, and declare the store-level cutover records in
  `release/native_runtime_ownership_v1.json` (domains already exist at the
  coarse level: `backup_restore_data_movement`, `target_registry`,
  `backup_scheduler`, `dr_orchestration`, `resilience_coordinator`, ...).
- Promote the Go control plane for policies/targets/DR
  (`DEEPSEEKD_CONTROL_AUTHORITY` + `DEEPSEEKD_INTERNAL_BEARER` +
  `control-authority-v1` checkpoint, takeover/lease semantics) and keep the
  shadow reconciler green.
- Rust worker/fencing: restore/DR routes must carry `actionId + executionEpoch`,
  refuse side effects on lease/epoch/identity/proof failures, and keep
  uncertain remote effects `EFFECT_UNKNOWN`.

### 3. Platform packages and zero-Python runtime

- Desktop (WebView2), Android APK, launcher/packaging (`scripts/build_exe.py`,
  `android/`, `static/ui/` build) must run the native runtime independently.
- Zero-Python clean environment with non-zero successful workloads for server,
  desktop, Android and background tasks; prove no production package or
  subprocess chain reaches Python (inspect images/APKs/dependencies/
  subprocesses).

### 4. Verification gates

- Full 37-job `ci.yml` on the exact source (Go + Android-Go 95% coverage floors,
  Python 95%, release-version, security scans, offline evals).
- `native-windows-cloud.yml`, `native-media-development.yml`,
  `native-media-create.yml` and `rust-cloud.yml` on the exact final source.
- Real browsers, real Three-MinIO / two-Fleet / multi-MinIO topologies,
  process-kill/restart/network/timeout/lease/stale-epoch/unknown-effect/
  takeover matrices, provider/persistence evidence.
- Frozen contracts unchanged: `object-set-v1`, Receipt v4, Commit v4, FastCDC
  v3, randomized Age, `control-authority-v1`, AuthorityCheckpoint v1,
  `dr-readiness-proof-v1`, `evidence-proof-v2`, `predictive-planning-proof-v1`,
  signed Federation contracts.
- Evidence Assembly: `release/native_runtime_5_0_evidence_v1.json` stays
  `NOT_READY` until every gate above has exact-commit cloud evidence.

## Next concrete steps

1. Start S1 on top of `9a74dc56`: `/api/workspace/backup-target-capabilities`
   (decide the deployed contract for `s3TargetAvailable`; Python reports boto3
   availability, native must decide whether the answer describes the serving
   runtime), `/api/workspace/backups/capabilities`,
   `/api/workspace/backup-transfer-budget`, `/api/workspace/retention/preview`
   and `/api/workspace/authority/retention/plan` dry-run paths.
2. In parallel (read-only, disjoint): `/api/agent-runs/{run_id}/{action}`
   unknown-action 404 after the original preamble, and `/api/automation` root
   GET/POST reusing `automation_definition_routes` helpers plus runner/scheduler
   port for `run/rerun/list_runs/simulate/run_due`.
3. Then S2 catalog/index (Rust data) with fixture-backed oracle reads and
   `python_disabled` writes.
4. Keep dispatching exact-source `rust-cloud.yml` per slice; on failure record a
   prior-failure artifact and fix the first upstream failure (formatting, clippy,
   ordering or assertion) before re-running.

## Verification/CI rules in force

- Rust/Go/Docker compilation and cloud verification run only in GitHub Actions;
  a pushed dedicated branch does not auto-trigger main `ci.yml` (use
  `gh workflow run`). Never pool revisions or claim an unobserved PASS.
- Download/report artifacts only (reports, logs, source-context, coverage);
  never compiler caches, images or volumes.
- Chinese operator rules: pwsh, `rg`, bounded waits, no repeated identical
  failures, absolute paths, no sandbox escalation requests.
