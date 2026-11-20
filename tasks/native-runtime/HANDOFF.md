# Handoff — native Rust/Go runtime migration (2026-11-20)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**Overall status: ACTIVE / NOT_READY.** The whole-product migration is not complete
and no readiness string has been changed. This document records only what was
observed, where the evidence is, and what is still open.

Every number below was read from a workflow job log or an uploaded artifact — not
from a run's summary conclusion.

## 1. Where the work is

| Item | Value |
| :--- | :--- |
| Active worktree | `D:\deepseek-worktrees\tip20261106` |
| Branch | `codex/rust-coverage-stage1-20261120` |
| Local HEAD | `858f95badddd4fea3c3f61fd4f3e4a14e7914e76` (2026-11-20 20:39) |
| Local tree | `409c2da024ae2e54ea892839d044099c547223ad` |
| Worktree state | clean except these two docs — `HANDOFF.md` and `todo.md` are rewritten and uncommitted |
| `origin/main` | `cb695c67` (PR #200 merge, 2026-11-20 20:06) |
| Ahead of main | 2 commits (`059e3b5e`, `858f95ba`) — coverage stage 1, not merged |
| Root checkout `D:\deepseek` | branch `codex/native-runtime-pr/20261014`, HEAD `b0316d09`, **~190 deliberately preserved dirty/untracked files; do not touch** |
| Release version | `VERSION` = 4.8.0 |
| Evidence (outside repo) | `D:/deepseek-worktree-evidence/`, plus committed `release/native-runtime-evidence-5.0/` |

`artifacts/` is gitignored, which is why evidence is mirrored outside the repo.
Downloaded artifact trees placed inside `artifacts/` make `mypy .` fail with
duplicate module names; keep them out.

**Push mechanics.** Git HTTPS transport is broken on this host
(`schannel: failed to receive handshake`). All pushes replay local commits through
the GitHub API (blob → tree → commit → ref PATCH), asserting tree equality at each
step. Remote commits therefore have different SHAs with identical trees — match by
tree, never by local SHA. `gh` must run with `PYTHONIOENCODING=utf-8` and
`encoding="utf-8", errors="replace"`, otherwise API responses fail to decode as
`gbk`.

## 2. What is actually verified

### 2.1 The runs

| Workflow | Run | Revision | Result |
| :--- | :--- | :--- | :--- |
| `ci.yml` | [44687684156](https://github.com/leizd/DeepSeek-Infra/actions/runs/44687684156) | `1e90e9e3` | 40 jobs, 40 success, 0 failure, 0 skipped |
| `Native Android cloud verification` | [44687684163](https://github.com/leizd/DeepSeek-Infra/actions/runs/44687684163) | `1e90e9e3` | 6 jobs, 6 success |
| `ci.yml` | [44672165757](https://github.com/leizd/DeepSeek-Infra/actions/runs/44672165757) | `57733adc` | 40 jobs, 40 success (green before the diagnostic cleanup) |
| `ci.yml` | [44723378106](https://github.com/leizd/DeepSeek-Infra/actions/runs/44723378106) | `5eb286c5` | 40 jobs, 40 success (PR #200, on `main`) |
| `ci.yml` | [44043989640](https://github.com/leizd/DeepSeek-Infra/actions/runs/44043989640) | `28ec08c8` | 40 jobs, 40 success — last **exact-commit** dispatch green, S4 |

**Read this nuance before claiming an exact-commit qualification.** Runs
`44687684156`, `44672165757` and `44723378106` were triggered by `pull_request`,
so the workflow compiled `refs/pull/<n>/merge`, not the branch head. Their reported
`head_sha` is the PR head (`1e90e9e3`, `57733adc`, `5eb286c5`), but the compiled and
tested tree is the merge with `main` at that moment. The last run that compiled a
plain commit rather than a merge ref is `44043989640` at `28ec08c8` (S4).
An exact-commit green for the S5–S8 tree is still outstanding — see §3.

A completed `ci.yml` run enumerates 40 jobs; 38 are declared in the workflow
because `test` contributes three matrix instances (3.10 / 3.11 / 3.12). While
upstream gates are still running, only 37 are enumerated — `evidence-assembly`,
`rc-readiness` and `release-package` are created once their `needs` settle.

### 2.2 Measurements, read from the job logs

From `ci.yml` run `44687684156`:

| Measurement | Value | Floor |
| :--- | :--- | :--- |
| Go statement coverage (default) | 95.140136% (17482/18375) | 95.0 |
| Go statement coverage (Android) | 95.131727% (17802/18713) | 95.0 |
| Rust line coverage | 80.27% (137932/171837), `1992` tests | 80.0 |
| Python coverage | 95.62% total, statements 96.92%, branches 91.79% | 95.0 |
| Fuzz executions | 9,134,153 + 17,694,322 + 3,656,882 = **30,485,357**, 0 findings | — |
| 4.0 RC readiness | `Decision: READY FOR 4.0.0-rc.2` | — |
| Evidence assembly | `release-evidence-v4.8.0.zip` uploaded (artifact `14610886213`) | — |
| Go race | `native-go-race` success; slowest corpus job 3925 s | — |

`native-go-race` and `native-go-android` both passed, so the `RACE_COMMAND_FAILED`
failures of 2026-11-20 02:42–12:06 are closed for this tree.

### 2.3 What that does *not* qualify

- `release/native_runtime_gate_evidence_v1.json` still reads `status: PARTIAL`,
  `readiness: NOT_READY`, 15/15 gates PASS — but those gate artifacts are bound to
  `d8ea1d5f` / `69a5a881`, not to any current head. They are history, not current
  evidence.
- `release/native_runtime_5_0_evidence_v1.json` is `NOT_READY` **by design**:
  `release: 5.0.0` against `VERSION` 4.8.0 means its `DELIVERED` path can never
  validate here (`VERSION_MISMATCH`), its `assessment_scope` says "repository
  snapshot only", and its empty `measurements`/`gates` and `exact_head: null` are
  intentional. **Do not "fix" it.**
- A green CI means the repository is self-consistent under its current gates. It is
  not release evidence, and it is not production authority.

## 3. In flight right now (2026-11-20 ~20:55 Asia/Shanghai)

| Run | Revision | State | What it tells us |
| :--- | :--- | :--- | :--- |
| [44733063678](https://github.com/leizd/DeepSeek-Infra/actions/runs/44733063678) | `cb695c67` | in progress (`push` to `main`) | the exact-commit green that S5–S8 still lacks |
| [44735963134](https://github.com/leizd/DeepSeek-Infra/actions/runs/44735963134) | `059e3b5e` | in progress, **already red** | superseded — its mypy failure is fixed in `858f95ba` |
| [44737806916](https://github.com/leizd/DeepSeek-Infra/actions/runs/44737806916) | `858f95ba` | in progress | whether coverage stage 1 can be made green |

Two independent reds on `059e3b5e`, both real:

1. **mypy — fixed in `858f95ba`.** `tests/test_rust_coverage_floor.py` annotated
   `_ladder()` as `dict[str, float]` while returning a list and an index, so mypy
   emitted 11 `"Value of type float is not indexable"` errors and `test (3.10)`,
   `test (3.11)`, `test (3.12)` failed before pytest ran. Now
   `tuple[list[float], int]`; local `mypy`, `ruff` and
   `pytest tests/test_rust_coverage_floor.py` (6 passed) are clean.
2. **rust-coverage — open, and the cause is now identified.** The job measured
   **80.31% (138093/171944, 2003 tests)** and exited 1 against the stage-1 floor of
   **82.0**. The gate compares `totals["lines"]["percent"]` from the
   `cargo llvm-cov` JSON (`scripts/run_rust_coverage.py`:
   `passed = float(line_metric["percent"]) >= args.threshold`), and that is the only
   percentage it ever computes — the lcov file is written as an artifact and never
   parsed for the decision.

   The "81.63%" that commit `059e3b5e` used to justify stage 1 does not come from
   this gate. It is the 2026-10-08 figure `72808/89192 = 81.630640%` recorded in
   `continuation.md` (lines 4256, 4349, 4465) for a different, much smaller source
   (89,192 lines versus today's 171,944). So stage 1 was set from a stale number on
   a different tree, not from a different metric basis.

   Consequence: the measured value at this head is 80.31%, which is above the
   original 80.0 floor and below the 82.0 stage. Either the ladder's first step has
   to be justified by a fresh measurement at the current head, or stage 1 waits.
   **Do not lower the floor to match a red run** — the floor exists to be earned.

## 4. What changed since the 2026-11-17 S4 checkpoint

**S5–S8 were implemented and, for the first time, actually executed.** They had
never run: `rust` failed to compile because `backup_policy_run_routes.rs` passed an
owned `ContributorSpec` where `collect_contributor_files` takes a reference
(`E0308`), and a crate that does not compile produces no test results. Every S5–S8
test sat below that error, invisible.

28 new gateway route modules landed since `28ec08c8` (each with a matching
integration test): `backup_archive_routes`, `backup_capacity_routes`,
`backup_dr_{audit,drill_exec,failover,plan,slo,status}_routes`,
`backup_policy_run_routes`, `backup_remote_restore_routes`,
`backup_restore_{read,tx,write}_routes`, `backup_secret_slots`,
`workspace_{export,provenance}_routes`, `resilience_{action_exec,advisory,plan,
read,wave,whatif}_routes`, `resilience_state_support`, `automation_root_routes`,
`edge_action_routes`, `external_mcp_routes`, `rag_action_routes`,
`semantic_cache_routes`.

Of those 28 files, 25 are route modules that register their own `router()` and each
carries a matching integration test in `rust/crates/deepseek-gateway/tests/`; the
other three are shared support (`backup_secret_slots`,
`resilience_action_exec`, `resilience_state_support`) consumed by the route modules.
The gateway test directory now holds **83** test files.

Removing the compile error exposed **six real implementation defects**, all fixed:

1. `hex_digest` (hex-encode only) used as a digest in five places, so `commitHash`,
   `receiptDigest`, `slotDigest`, `runPlanDigest` and the control
   `checksums.sha256` were hex dumps and no commit marker could validate.
2. A projected restore plan hashed `extracted/manifest.json` from disk, but the
   projection branch only writes `projection.output_files`; the oracle passes the
   manifest in memory.
3. A repeated risk snapshot incremented `observationCount`; the oracle returns the
   stored row when the snapshot key matches.
4. `value_str(Null)` produced the literal `"None"`, adding a phantom target id that
   emptied the capacity snapshot and failed the wave admit closed.
5. The persisted capacity observation was only consulted inside the
   `quotaBytes` / `kind == "s3"` branch, while the oracle reads it first.
6. `TestSettlementEntryPointsRefuseAMismatchedPhase` called `t.Skipf` when its
   fixture lacked a plan-phase action, so none of its three assertions ran; the race
   gate correctly reported `UNREVIEWED_TEST_SKIP`.

**Merged to `main`:** PR [#199](https://github.com/leizd/DeepSeek-Infra/pull/199)
("accumulate 399 migration commits and unblock the gateway compile", 806 files,
+235878/−2682) at `940c72dc`; PR [#200](https://github.com/leizd/DeepSeek-Infra/pull/200)
(Gradle User Home cache, so Maven Central 429s stop failing the APK build) at
`cb695c67`.

**Rust coverage floor is being raised 80 → 95 in five committed stages.**
`scripts/run_rust_coverage.py` now holds `RUST_COVERAGE_STAGE_FLOORS = (82.0, 85.0,
88.0, 91.0, 95.0)` and `RUST_COVERAGE_CURRENT_FLOOR` selects the enforced stage;
`ci.yml` no longer passes `--threshold`, so the workflow cannot drift from the
script. Stage 1 raised the DOCX extractor (`docx_text.rs`, previously 0 tests over
1335 lines) by 11 tests. The 4.0 RC requirement deliberately stays at 80.0 because
it is checked against the frozen `docs/evidence/rust-coverage-v4.0.0-rc.2.json`
measurement. See §3 for the open stage-1 number.

## 5. Ownership and route parity

`release/native_runtime_ownership_v1.json`: 67 domains, `current_production_authority`
= `python`, untouched.

| current_owner | domains |
| :--- | :--- |
| python | 54 |
| rust | 12 |
| typescript | 1 (`browser_ui`, whose target is also typescript) |

Target owners: 43 rust, 21 go, 2 python (`offline_eval_oracle`,
`migration_release_tooling`), 1 typescript. **Go currently owns 0 domains** — the
whole control plane is still Python, and that is what `CONTROL_CUTOVER_INCOMPLETE`
means.

Native store declarations:

- `RUST_DATA_DOMAINS` in `deepseek_infra/infra/native_runtime/authority.py`: **23**
  stores, each with `assert_python_writer_allowed(...)` at its Python write
  entry points — **121** call sites across `deepseek_infra/` at this head, covering
  26 distinct names (the 23 Rust data domains plus the Go control names
  `a2a_task_lifecycle`, `agent_run` and `scheduler`).
  `scripts/native_store_writer_gate.py --check` reports "would gate 0 entry points",
  i.e. every declared writer already carries its guard.
- `DECLARED_NATIVE_DATA_DOMAINS` in `rust/crates/deepseek-gateway/src/lib.rs`:
  **18**. The gateway list is deliberately a *subset*: the five stores with no
  native writer yet — `media_library_store`, `backup_retirement_store`,
  `backup_component_cache_store`, `resilience_scheduler_store`,
  `resilience_capacity_store` — are gated in Python but not declared Rust-side, and
  no Rust route references them. The in-repo test asserts subset direction (a
  gateway domain must never invent ownership the contract has not declared).
- `GO_CONTROL_DOMAINS`: 29.

Route parity, counted from registrations (`@router.<verb>(...)` in
`deepseek_infra/web/`, `.route("...")` in `rust/crates/deepseek-gateway/src/`,
`any(...)` expanded to all verbs):

- Python registers 177 unique `/api` verb+path pairs.
- Rust matches all but two: `GET /api/workspace/backup-policies` and
  `POST /api/workspace/backup-policies`. These are served by Go
  (`go/internal/api/public.go:69,76`) through the Rust `/api/*path` catch-all
  reverse proxy.
- `release/production_runtime_inventory_v1.json`: `http_routes_only_on_python` is
  now `[]`, and 14 legacy Python production entries remain (server, desktop
  launcher, WebView, Android bridge, PyInstaller specs, backup worker, federation
  node).

## 6. What remains

### 6.1 The six release blockers, all open

`release/native_runtime_5_0_evidence_v1.json` keeps all six:
`PUBLIC_EDGE_AUTHORITY_INCOMPLETE`, `CONTROL_CUTOVER_INCOMPLETE`,
`EFFECT_RECONCILIATION_UNPROVEN`, `PROVIDER_EVIDENCE_MISSING`,
`EXACT_HEAD_CI_MISSING`, `ZERO_PYTHON_MEASUREMENTS_MISSING`.

### 6.2 Ownership inversion

- Go control-domain promotion against a genuinely stopped-and-fenced Python source,
  with `control-authority-v1` checkpoint, takeover/lease rules and export/import
  evidence. The S0 store-level tooling already exists
  (`scripts/native_workspace_store_cutover.py`: export/verify/snapshot/import/
  rollback over explicit paths); the Go control path is what is missing.
- Only then move `current_production_authority` off `python`.

### 6.3 Packages and zero-Python deployment

- Android: `MainActivity.java` starts a native server but the inventory records
  `executor: rust_go_unpackaged` with `packaged_native_binary: false` — **the APK
  does not yet carry the native binary**.
- Desktop (WebView2) and launcher/PyInstaller packaging still execute Python.
- `production_artifact_zero_python` passes for the default native **image**
  (no `python`/`python3`/`node`/`nodejs` on `PATH` after a fresh-volume start) and
  **not** for the release zip, which is a source tree containing 1029 Python files.
- Zero-Python clean-environment workloads with non-zero successful operations are
  still unmeasured.

### 6.4 Provider and fault evidence

Three-MinIO, two-Fleet/four-MinIO, and process-kill/takeover workloads have no
evidence bound to a current head. The seven zero invariants were measured at 0
against an isolated shadow-mode process, not production traffic.

## 7. Hard rules that were learned the hard way

Each of these cost at least one CI round or produced a wrong report.

1. **Read the artifact, not the conclusion.** Three times in this workstream a green
   job measured nothing: a hard-coded `informationalOnly: true`, a coverage figure
   that moved on the clock, and a YAML `#` comment that swallowed every flag after
   it.
2. **A `#` inside a `>-` block is not a YAML comment** — it is folded into the shell
   command and truncates everything after it. `tests/test_rust_sidecar_latency_gate.py`
   asserts on the *parsed* command.
3. **Pin `eol=lf` on any file whose digest is recorded.** With CRLF in the worktree
   and LF in the repository, a recorded SHA-256 matches on exactly one platform.
4. **Fixed sleeps are not synchronization.** `time.Sleep(4300ms)` silently became
   load-dependent; it is a condition wait now.
5. **A shared fixture needs a lock** if a driver goroutine writes it while the test
   reads it, otherwise `-race` is entitled to fail it.
6. **Do not lower a coverage floor to make a gate pass.** Both Android coverage
   flakes were traced to one statement each and fixed at the source.
7. **Do not claim a PASS you did not observe.** Cancelled runs, superseded commits,
   network failures and merge-ref runs all look like results if you only read the
   summary.
8. **A crate that does not compile silently deletes every test below the error.**
   Six real defects hid behind one `E0308` for weeks; check `rust` compiles before
   reasoning about test counts.

## 8. Known limitations of this host

- **Cargo cannot build locally.** `cargo check` fails in dependency build scripts;
  Rust is compile-checked only in CI. Mitigation: tests such as
  `tests/test_native_workspace_store_cutover.py` assert Rust fixed-size-array
  lengths, turning a class of `E0308` into a local test failure.
- **The Go race detector cannot run locally** (`0xc0000139`, missing runtime DLL,
  reproduced on an untouched package). `-race` is cloud-only.
- **Docker, real MinIO topologies and provider workloads are cloud-only.**
- Local gates that work: `ruff`, `mypy`, `scripts/update_docs_language_nav.py
  --check` (240 files), `scripts/check_doc_links.py`,
  `scripts/check_release_version.py`, `scripts/native_runtime_contract.py --check`,
  `scripts/production_runtime_inventory.py --check`,
  `scripts/native_store_writer_gate.py --check`, `gofmt`, `go vet`, `go build`,
  `go test` (non-race), `rustfmt`, `cargo metadata --locked --offline`, pytest
  subsets.
- Known pre-existing local failures (reproduced on a clean tree, not caused by this
  work): `tests/test_backup_456_coverage_booster.py::test_rebalance_policy_replicas_with_draining_and_diversity`,
  `tests/test_backup_480_real_two_fleet_four_minio_e2e.py` (needs `link.exe`),
  `tests/test_server_integration.py`, five `tests/test_native_media_*_fixture.py`
  suites, `go/internal/store TestNativeOriginWALMarkerRejectsCommittedForeignSchemaWithoutWrites`
  (Windows temp-dir lock), `go/internal/launch TestManagedProfileRejectsRedirectedPrivateDirectories`
  (symlink privilege).

## 9. Next executable tasks

1. **Land the coverage stage 1 decision.** Either resolve the basis mismatch in §3
   and re-measure, or keep the enforced floor at 80.0 and defer stage 1 until the
   measured value clears it. Do not lower the stage to match a red.
2. **Get an exact-commit green for the S5–S8 tree.** `44733063678` (`push` to
   `main` at `cb695c67`) is the first candidate; re-dispatch with
   `workflow_dispatch` at the exact SHA if it is inconclusive.
3. **Record S5–S8 in `migration-matrix.md` as qualified slices** once that run is
   green, with the artifact numbers rather than the job conclusions.
4. **Go control-domain promotion** against a stopped-and-fenced Python source, then
   move `current_production_authority` off `python`.
5. **Android packaging**: put the native binary inside the APK and flip
   `packaged_native_binary` with evidence.
6. **Provider/fault evidence** at an exact head: three-MinIO, two-Fleet/four-MinIO,
   SIGKILL/takeover.
7. **Zero-Python workloads**, then Evidence Assembly.

## 10. Where to read further

| File | Contents |
| :--- | :--- |
| `tasks/native-runtime/continuation.md` | Chronological checkpoint log, newest first (13633 lines) |
| `tasks/native-runtime/migration-matrix.md` | Per-domain parity audit and slice-by-slice qualification (3383 lines) |
| `tasks/native-runtime/todo.md` | Outstanding work, by workstream |
| `tasks/native-runtime/workspace-backup-dr-migration-plan.md` | S0–S8 slice plan and verification rules |
| `tasks/native-runtime/CONTINUATION-PROMPT.md` | Self-contained prompt for the next agent |
| `release/native_runtime_ownership_v1.json` | 67 domains, `current_production_authority: python` |
| `release/native_runtime_gate_evidence_v1.json` | 15 gates + 7 invariants, bound to `d8ea1d5f` / `69a5a881` |
| `release/native_runtime_5_0_evidence_v1.json` | Release assessment (`NOT_READY`, by design) |
