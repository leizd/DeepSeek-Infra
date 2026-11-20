# Outstanding work — native Rust/Go runtime migration (2026-11-20)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**Overall: ACTIVE / NOT_READY.** `current_production_authority` is still `python` and
no readiness string has moved. Read `HANDOFF.md` first for the verified state; this
file is the work queue that follows from it.

Scope reminder: the target is that server, desktop, Android, background tasks and
every production capability run on Rust/Go, with **no Python interpreter, Python
service or Python fallback in production runtime or release packages**, and no
side-channel call back into the old Python implementation.

Ordering rule: an item is only `[x]` when an observed, exact-commit result covers
it. Everything else stays `[ ]` even if the code is written.

---

## P0 — Unblock the current branch (blocking everything below)

- [ ] **Resolve the Rust coverage stage-1 number.** `scripts/run_rust_coverage.py`
      decides with `passed = float(totals["lines"]["percent"]) >= args.threshold`
      from the `cargo llvm-cov` JSON; the lcov file is only an artifact. Run
      `44735963134` at `059e3b5e` measured **80.31% (138093/171944, 2003 tests)**
      and exited 1 against the stage-1 floor of **82.0**. The "81.63%" in
      `059e3b5e`'s message is the 2026-10-08 figure `72808/89192 = 81.630640%`
      from `continuation.md`, measured on an 89,192-line source, not on today's
      171,944 lines — a stale number, not a different metric. Either justify stage 1
      with a fresh measurement at the current head or hold the ladder at 80.0.
      **Do not lower the floor to match a red run.**
- [ ] **Green `ci.yml` at `858f95ba` (or its successor).** Run
      [44737806916](https://github.com/leizd/DeepSeek-Infra/actions/runs/44737806916)
      is in flight. It contains the mypy fix for
      `tests/test_rust_coverage_floor.py` (`_ladder()` now returns
      `tuple[list[float], int]`; local mypy, ruff and 6 pytest cases are clean).
- [ ] **Retire the red runs explicitly** by recording them as prior-failure
      evidence: `44735963134` at `059e3b5e` failed `test (3.10/3.11/3.12)` on that
      mypy error and `rust-coverage` on the floor.

## P1 — Make S5–S8 actually qualified

- [ ] **Exact-commit green for the S5–S8 tree.** Runs `44687684156` (`1e90e9e3`),
      `44672165757` (`57733adc`) and `44723378106` (`5eb286c5`) were `pull_request`
      events and compiled `refs/pull/<n>/merge`, not the branch head. The last plain
      exact-commit dispatch green is `44043989640` at `28ec08c8` (S4). Candidate:
      [44733063678](https://github.com/leizd/DeepSeek-Infra/actions/runs/44733063678)
      (`push` to `main` at `cb695c67`). If inconclusive, re-dispatch with
      `workflow_dispatch` at the exact SHA.
- [ ] **Record S5–S8 in `migration-matrix.md` as qualified slices** — restores
      (`backup_restore_{read,tx,write}_routes`, `backup_remote_restore_routes`,
      `backup_archive_routes`, `backup_secret_slots`), DR
      (`backup_dr_{audit,drill_exec,failover,plan,slo,status}_routes`), resilience
      (`resilience_{action_exec,advisory,plan,read,wave,whatif}_routes`,
      `resilience_state_support`), exports/provenance, automation root, RAG actions,
      semantic cache, edge actions, external MCP — using artifact numbers, not job
      conclusions.
- [ ] **Keep the six fixed defects under regression.** They are described in
      `HANDOFF.md` §4; each already has a test, but re-verify them in the same run
      that qualifies the slices.

## P2 — Ownership inversion (the actual 5.0 blocker)

- [ ] **Go control-domain promotion** against a genuinely stopped-and-fenced Python
      source: `control-authority-v1` checkpoint, `DEEPSEEKD_CONTROL_AUTHORITY` +
      `DEEPSEEKD_INTERNAL_BEARER`, per-domain promotion artifact, takeover/lease
      rules, and export/import + rollback evidence. Go currently owns **0** of the
      67 domains.
- [ ] **Then move `current_production_authority` off `python`** in
      `release/native_runtime_ownership_v1.json`, domain by domain, with the
      promotion evidence attached.
- [ ] **Close the declared-vs-gated gap deliberately.** `RUST_DATA_DOMAINS`
      (Python side) lists 23 stores; `DECLARED_NATIVE_DATA_DOMAINS` (Rust gateway)
      lists 18. The five with no native writer are `media_library_store`,
      `backup_retirement_store`, `backup_component_cache_store`,
      `resilience_scheduler_store`, `resilience_capacity_store`. Each needs either a
      native writer or an explicit, recorded decision to leave it Python-owned —
      otherwise a `python_disabled` cutover leaves those stores unwritable.
- [ ] **Worker/fencing invariants under a real cutover**: restore/DR/resilience
      routes carrying `actionId + executionEpoch`, refusing side effects on
      lease/epoch/identity/proof failure, and keeping uncertain remote effects
      `EFFECT_UNKNOWN`.
- [ ] **Fail-closed gap still named in the matrix**: remote S3 registered-target
      catalog stores return `503 NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED`.

## P3 — Packages and zero-Python deployment

- [ ] **Android APK must carry the native binary.** Inventory records
      `android_activity` as `executor: rust_go_unpackaged`,
      `packaged_native_binary: false`. `MainActivity.startNativeServer` exists but
      the APK does not ship the binary yet.
- [ ] **Desktop (WebView2) and launcher packaging** must run the native runtime
      independently of Python. `launch.py`, `deepseek_infra/desktop_app.py`,
      `launcher/runtime.py`, `launcher/gui.py`, `launcher/mobile.py` and
      `scripts/build_exe.py` are all still Python production entries.
- [ ] **Zero-Python workloads with non-zero successful operations** for server,
      desktop, Android and background tasks, proving no production package or
      subprocess chain reaches Python.
- Note: `production_artifact_zero_python` passes for the default native **image**
      (no `python`/`python3`/`node`/`nodejs` on `PATH` after a fresh-volume start)
      and fails for the release zip (a source tree with 1029 Python files). The zip
      is a source distribution, not a runtime artifact — do not claim the gate for
      it.

## P4 — Provider, fault and evidence assembly

- [ ] **Three-MinIO and two-Fleet/four-MinIO** topologies at an exact head.
- [ ] **SIGKILL / restart / network / timeout / lease / stale-epoch / unknown-effect
      / takeover matrices** at an exact head.
- [ ] **Zero-invariant measurement against production-shaped traffic**, not the
      isolated shadow-mode process (currently 7/7 measured at 0 with 48 samples and
      96 operations, `scripts/measure_native_zero_invariants.py`).
- [ ] **Evidence Assembly** at the exact final head, then update
      `release/native_runtime_gate_evidence_v1.json` — whose 15 PASS gates are still
      bound to `d8ea1d5f` / `69a5a881` and are history, not current evidence.
- [ ] `release/native_runtime_5_0_evidence_v1.json` stays `NOT_READY` with all six
      blockers open until every gate above has exact-commit evidence. Do not edit it
      to look better.

## P5 — Route and surface completion

Verified at `858f95ba`: Python registers 177 unique `/api` verb+path pairs; Rust
matches all but `GET`/`POST /api/workspace/backup-policies`, which Go serves
(`go/internal/api/public.go:69,76`) through the Rust `/api/*path` catch-all.

- [ ] **Confirm the Go-served pair end to end** through the Rust edge with an oracle
      test (the Rust catch-all must forward both verbs with the original envelope).
- [ ] `release/production_runtime_inventory_v1.json` shows
      `http_routes_only_on_python: []` but **14 legacy Python production entries**
      remain (server, desktop, WebView, Android bridge, PyInstaller specs, backup
      worker, federation node, federation HTTP app). Those are the real remaining
      migration target, not the route count.
- [ ] **Background workers still Python** (6): `embedded_backup_worker`,
      `recovery_lease_keeper`, `gateway_scheduler_orphan_recovery`,
      `cache_cleanup_loop`, `agent_run_auto_resume`, `automation_scheduler`.

## P6 — Agent runtime / media carry-over

These are the oldest open workstream items and are still unqualified. They were
never tied to the S-slices and should not be treated as done because CI is green.

- [ ] Agent runtime: public resume/rerun/tool-continuation, retained tool results,
      model-turn and published-answer settlement are implemented in
      `deepseek_infra/infra/native_runtime/agent_runtime_admission.py`, Rust
      `agent_runtime_admission.rs` and Go `runtime_admission.go`, but the local
      evidence is Python-oracle tests that do **not** compile or run Rust/Go.
- [ ] Media: create/upload/delete/process/index mutations, durable image recovery,
      `EFFECT_UNKNOWN` recovery for an interrupted `APPLYING` operation, and the
      128 MiB library-read / 2 MiB typed-message bounds still need compatibility
      acceptance.
- [ ] Skills: read/run/mutation route end-to-end suites exist
      (`tasks/native-runtime/skills_*_e2e.py`) but have no current cloud evidence.

---

## Runs to cite when this list is re-reviewed

| Run | Revision | Event | Result |
| :--- | :--- | :--- | :--- |
| 44687684156 | `1e90e9e3` | `pull_request` (merge ref) | 40/40 success |
| 44672165757 | `57733adc` | `pull_request` (merge ref) | 40/40 success |
| 44723378106 | `5eb286c5` | `pull_request` (merge ref) | 40/40 success |
| 44043989640 | `28ec08c8` | `workflow_dispatch` | 40/40 success — last exact-commit green (S4) |
| 44735963134 | `059e3b5e` | `workflow_dispatch` | **red** — mypy + coverage floor, superseded by `858f95ba` |
| 44733063678 | `cb695c67` | `push` to `main` | in progress at the time of writing |
| 44737806916 | `858f95ba` | `workflow_dispatch` | in progress at the time of writing |
