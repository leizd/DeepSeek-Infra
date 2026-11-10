# Workspace backup / DR / resilience HTTP surface — migration plan v1

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


Status: **planning only. NOT_READY.** No route in this surface has been
qualified on the native edge beyond the families already listed in
`migration-matrix.md` (backup mirror, replication, retirement, run, drill,
backup policies/targets served by Go for the control-plane subsets).

Source of the inventory: the 2026-11-07 recursive Python-route audit
(209 app routes; 116 without an explicit native route at the time of the
audit), captured in `artifacts/route-gap-audit.json`. This plan covers the
~100 `/api/workspace/*` routes; the remaining mismatches for already-migrated
routes are listed at the end.

## Why this surface is a workstream, not a slice

Every family below reads or writes at least one durable store, and the store is
currently Python-authoritative:

| Family | Python modules (infra/workspace/) | Durable stores |
| :--- | :--- | :--- |
| policies / targets | `backup_targets`, `backup_policies`, `backup_target_s3` | `.backup-policies/`, `.backup-targets/` (control plane → Go) |
| catalog | `backup_catalog`, `backup_scrub` | `.backup-catalog/`, `.backup-index/` (data plane → Rust) |
| retention | `backup_retention`, `backup_authority_retention` | `.backup-retention/`, `.backup-authority-retention/` (Rust) |
| capacity / budget | `backup_capacity`, `backup_transfer_budget` | derived from index + targets (read) |
| restores | `backup_restore`, `backup_remote_restore`, `restore_apply` | `.restore-staging/`, `.backup-spool/` (Rust + worker) |
| disaster recovery | `backup_dr`, `backup_recovery_drill`, `backup_dr_readiness`, `backup_replication` | `.backup-dr/`, `.backup-replication/`, `.backup-continuity/` |
| resilience | `resilience_*` (risk, SLO, waves, capacity, cost, optimizer) | `.resilience-*/` (Rust) |
| exports / artifacts / provenance | `workspace/exports`, `workspace/artifacts`, `workspace/provenance`, `projects` | `.projects/`, `.generated/`-adjacent (Rust data) |

Because of the one-authoritative-writer rule, no family can be switched on
until (a) the Rust data-domain cutover declares its stores and (b) the Go
control plane is authoritative for the control subset of that family. Both have
export/import + fencing + restore/rollback evidence requirements, so this plan
orders slices so each one either reads data it does not write (parity-testable
now) or is fully behind a cutover gate.

## Slice order

Each slice is one reviewable commit/branch with a Python-oracle integration
test, a `rust-cloud.yml` run at the exact source, and a prior-failure record if
a cloud run fails. Slices do not start until the previous one is qualified,
unless they are read-only and disjoint.

### S0 — prerequisites (no HTTP route)

1. **DONE — the stores are declared.** Fifteen workspace backup/restore/DR and
   resilience stores are now `python -> rust` at 4.9.4 in
   `release/native_runtime_ownership_v1.json` with `durable_store: rust_data`.
   Each carries the fields a cutover actually needs, all measured from the modules
   rather than guessed: `storePath` (the real dot-directory, with a second
   directory where the store owns two), `writerEntryPoints` (the functions that can
   write it) and `cutoverGate` (the ordering constraint that makes the transfer
   safe). `RUST_DATA_DOMAINS` in `deepseek_infra/infra/native_runtime/authority.py`
   lists the same fifteen, so the contract and the mechanical gate cannot drift.
2. **DONE — the writers are gated, and the gate is proven.** 81 declared write
   entry points now call `assert_python_writer_allowed("<store>")`, installed by
   `scripts/native_store_writer_gate.py` (idempotent; `--check` is the CI form).
   The gate is inert under the default runtime mode and raises
   `PythonWriterMechanicallyDeniedError` under `python_disabled`, both verified.
   The tool refuses to gate a function listed in any frozen-AST compatibility
   test, because those tests compare full `ast.dump` bodies against the 4.8.0
   commit — `backup_replication_store` is therefore gated at its `_atomic_write`
   choke point instead of at nine frozen job/phase functions.
3. **DONE — the state can move and the move can be undone.**
   `scripts/native_workspace_store_cutover.py` implements `export`, `verify`,
   `snapshot`, `import` and `rollback` over an explicit path, with a
   content-addressed inventory (path, kind, size, SHA-256 per entry), a
   store-level `entryDigest`, and an `exportDigest` that covers the manifest
   itself. Import refuses a non-empty destination, so the operation is a transfer
   of authority rather than a merge; rollback restores a verified snapshot. A
   named lock file fences the whole operation. All of it is offline and takes
   explicit paths — nothing here reads a production store.
4. **NOT DONE — the Go control authority path.** Policies and targets still need
   the `control-authority-v1` checkpoint plus `DEEPSEEKD_CONTROL_AUTHORITY` promoted
   per domain, with takeover/lease rules and export/import evidence. Recorded in
   `continuation.md` as the next S0 item.
5. Evidence so far: `tests/test_native_workspace_store_cutover.py` (10 tests) drives
   real stores — the catalog JSONL through `backup_catalog.append_receipt`, the
   retention policy files through `backup_retention.put_retention_policy`, and a
   WAL-mode SQLite database built with `resilience_capacity_history._SCHEMA` and
   queried again after import — through the full round trip, plus the tamper,
   changed-source, non-empty-destination and rollback refusals. No production data
   was touched. Cloud qualification of the exact commit is recorded in the slice
   artifact directory.

### S1 — pure control reads (parity-testable with current ownership)

- `GET /api/workspace/backup-target-capabilities`
- `GET /api/workspace/backups/capabilities`
- `GET /api/workspace/backup-capacity/summary`
- `GET /api/workspace/backup-transfer-budget`
- `POST /api/workspace/authority/retention/plan` (dry-run; no mutation)
- `POST /api/workspace/retention/preview` (dry-run; no mutation)

Question to resolve first for each: which runtime owns the *answer* (Go control
vs Rust data vs Python store during transition). Where the answer is a constant
or a capability of the serving runtime (target capabilities), port only after
deciding the deployed Python value is not authoritative. Oracle test:
default-mode Python for the read, native response compared byte-for-byte;
store read-only, no writes.

### S2 — catalog + index (Rust data)

- `GET /api/workspace/backup-catalog`
- `POST|DELETE /api/workspace/backup-catalog/{backup_id}/pin`
- `POST /api/workspace/backups/{backup_id}/scrub`
- `POST /api/workspace/backups/{backup_id}/verify-unlock`

Verification: Python writes a fixture catalog in a temp root; native reads it
under default mode (read-only) and returns the same bytes; pin/unpin is behind
the Rust data cutover and is tested in `python_disabled` mode against the
fixture plus a Python-side read-back that is *not* authoritative (documented).

### S3 — retention (Rust data)

- `POST /api/workspace/retention/apply`
- `GET|POST /api/workspace/authority/retention/policy`
- `POST /api/workspace/authority/retention/explain`
- `POST /api/workspace/authority/retention/compact`
- `GET /api/workspace/authority/history-snapshot`

Verification: dry-run parity for plan/preview/explain; apply/compact behind the
cutover gate with before/after store snapshots and rollback.

### S4 — policies + targets (Go control plane)

- `GET|POST /api/workspace/backup-policies`, `PATCH|DELETE .../{policy_id}`,
  `POST .../{policy_id}/promote-primary`, `GET .../{policy_id}/continuity`,
  `POST .../{policy_id}/run`
- `GET|POST /api/workspace/backup-targets`, `POST .../register-new`,
  `POST .../{target_id}/probe|adopt|drain|activate|drain/cancel`,
  `GET .../{target_id}/drain`, `DELETE .../{target_id}`
- `GET|POST /api/workspace/backup-rebalances`
- `POST /api/workspace/backups/recovery-identities`

Verification: Go handler parity against the Python control responses under the
control-authority gateway (fenced), plus refusal tests without authority.
Frontend e2e for the backup settings pages after the control subset is wired.

### S5 — restores (Rust data + worker fencing)

- `POST /api/workspace/restores/from-target[/preview]`
- `GET /api/workspace/restores`, `GET|DELETE .../{restore_id}`
- `POST .../{restore_id}/fetch|preflight|materialize|prepare|apply|commit|complete|pause|resume|abort|unlock`
- `PUT .../{restore_id}/secret|frontend-prepared`
- `POST /api/workspace/restores/inspect|cleanup`
- `GET /api/workspace/backups/{backup_id}/download`

Verification: fixture backup repositories (three-MinIO topology later), full
create→fetch→materialize→apply→commit→restore path under cutover, plus
process-kill/restart, stale-epoch, unknown-effect and recovery cases.

### S6 — disaster recovery + replication reads

- `GET /api/workspace/disaster-recovery/status|replication|slo`
- `POST /api/workspace/disaster-recovery/audit[/resume]|plan|failover/{restore_id}`
- `POST /api/workspace/disaster-recovery/drills[/run|schedule/{policy_id}]`
- `GET /api/workspace/disaster-recovery/drills/{restore_id}`

Verification: Python-oracle dry-run parity for status/plan; failover and drills
only after S5, with provider-backed takeover and proof verification evidence.

### S7 — resilience

- `GET /api/workspace/resilience/{snapshot,readiness,forecast,capacity-forecast,journal,optimizer,waves}`
- `POST /api/workspace/resilience/{assess,plan,explain,simulate,whatif,execute,waves/admit,waves/run}`
- `GET|POST /api/workspace/resilience/coordination-plan`

Verification: pure scoring/plan parity against Python fixtures first; persistence
and execution behind the Rust cutover with journal verification.

### S8 — workspace artifacts / exports / projects

- `GET /api/workspace/artifacts/{artifact_id}/download`
- `POST /api/workspace/exports`, `GET /api/workspace/exports/{export_id}/download`
- `GET /api/workspace/projects/{project_id}/provenance`
- `GET|POST /api/workspace/backups`, `GET|DELETE /api/workspace/backups/{backup_id}`,
  `.../finalize`, `PUT .../frontend-state|secret`

Verification: generated-export bytes and disposition headers byte-compared with
the oracle; provenance graph compared on Python-written project fixtures.

## Already-migrated routes with remaining gaps (separate track)

- `/api/agent-runs/{run_id}/{action}`: native has explicit plan/cancel/resume/rerun
  routes; an unknown action still falls through to the Go 501 instead of the
  original `404 Unsupported Agent run action`.
- `/api/automation` root GET/POST (12 actions incl. runner/scheduler) is the
  next self-contained candidate: registry reads/writes can reuse
  `automation_definition_routes` helpers; `run/rerun/list_runs/simulate/run_due`
  need the runner/scheduler port.
- `/api/media/{media_id}/process` is served by Go; confirm the Rust edge
  forwards it before claiming parity.
- `/api/edge/reload|route-preview`, `/api/mcp/external/tools`, `/api/rag/*`
  writes, `/api/semantic-cache*`, `/api/traces*`, `/api/compress-context`,
  `/api/share-target`, `/share-target` each need their owning store/engine
  migration first; track them in `todo.md`.

## Verification rules for every slice

1. Python-oracle test in default mode for read-only behavior; byte-compare
   status/body/content-type where applicable.
2. Writes only behind the ownership gate; test the refusal in default mode and
   the successful write in `python_disabled` mode against isolated fixtures.
3. Data cutover slices must ship export/import/integrity/fencing/restore/rollback
   evidence on isolated copies; never touch real production data in tests.
4. Fault matrix: process-kill/restart, timeout, lease loss, stale epoch,
   unknown remote effect, recovery/takeover. Unknown effects stay
   `EFFECT_UNKNOWN`.
5. Cloud evidence: exact commit, `rust-cloud.yml` (and the media/windows
   workflows where relevant), report/log artifacts only, prior-failure records
   when a run fails.
6. Record every slice in `migration-matrix.md` with the honest status string and
   the exact commit that was qualified.
