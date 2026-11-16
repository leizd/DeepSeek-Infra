# Handoff — native Rust/Go runtime migration (2026-11-16)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**Overall status: ACTIVE / NOT_READY.** The whole-product migration is not complete.
This document records what is actually qualified, where the evidence is, and what
remains. Nothing below should be read as completion of the objective.

## 1. Where the work is

| Item | Value |
| :--- | :--- |
| Active worktree | `D:\deepseek-worktrees\tip20261106` |
| Branch | `codex/native-s0-store-ownership-20261109` |
| Local HEAD | `588a485c54309ad572b58ea8c6e2fa266fda22ff` |
| Remote HEAD | `d8ea1d5fd20ca145680f9112ee7309eb9be5e575` (identical tree) |
| Tree | `4a06cabba67114e3d54d299851d5b838af7eaa1e` |
| Root checkout | `D:\deepseek` — branch `codex/native-runtime-pr/20261014`, HEAD `b0316d09`, **~184 preserved dirty/untracked user files; do not touch** |
| Evidence (outside repo) | `D:/deepseek-worktree-evidence/` |

`artifacts/` is gitignored, which is why evidence is mirrored outside the repo.
Downloaded artifact trees inside `artifacts/` make `mypy .` fail with duplicate module
names, so keep them out.

**Push mechanics.** Git HTTPS transport is broken on this host
(`schannel: failed to receive handshake`). All pushes go through the GitHub API
(blob → tree → commit → ref PATCH); see the push helper in section 8. Remote branch
heads are API-created commits with different SHAs but identical trees to local HEADs —
always match by tree, never by local SHA. `gh` must run with
`PYTHONIOENCODING=utf-8` and `encoding="utf-8", errors="replace"`, otherwise API
responses fail to decode as `gbk`.

## 2. Current verified state

**`ci.yml` run [43816807364](https://github.com/leizd/DeepSeek-Infra/actions/runs/43816807364)
at `d8ea1d5f`: 40 jobs, 40 success, 0 failure, 0 skipped.** The local tree hashes to
the same value the run tested.

Read from the artifacts, not from the job conclusion:

| Measurement | Value |
| :--- | :--- |
| Go statement coverage (default) | 95.010592% (17043/17938), floor 95.0 |
| Go statement coverage (Android) | 95.004377% (17363/18276), floor 95.0 |
| Rust line coverage | 83.2749% (103967/124848), floor 80.0 |
| Python coverage | 95.59%, floor 95.0 |
| Fuzz executions | 27,023,603 across three libFuzzer targets, 0 findings |
| Go gate wall clock | ~70 min, down from 183 |

**Release gates: 15 of 15 have evidence.** See
`release/native_runtime_gate_evidence_v1.json` (status `PARTIAL`, readiness
`NOT_READY`, deliberately). Every gate carries an exact-head artifact whose SHA-256 is
re-verified by `tests/test_native_runtime_gate_evidence.py`.

**Zero invariants: 7 of 7 measured at 0**, with 48 samples and 96 workload operations,
by `scripts/measure_native_zero_invariants.py`. Artifact:
`release/native-runtime-evidence-5.0/zero-invariant-measurement.json`.

**Release assessment: `release/native_runtime_5_0_evidence_v1.json` stays `NOT_READY`.**
This is by design and must not be "fixed":

- It declares `release: "5.0.0"` while `VERSION` is `4.8.0`, so its `DELIVERED` path can
  never validate here (`VERSION_MISMATCH`).
- Its own `assessment_scope` says *"Repository snapshot only; this file is not release
  evidence"*; empty `measurements`/`gates` and `exact_head: null` are intentional.
- The measured results live in the gate-evidence file instead.

**Why it is still NOT_READY:** the seven zero invariants were measured against an
isolated shadow-mode process, not production traffic, and this runtime has no
production deployment. `zeroInvariantMeasurement.whatIsStillNotProven` says so in the
file. That gap is not closable by code or tests.

## 3. What this session changed

In order, newest first:

| Commit | What it did |
| :--- | :--- |
| `588a485c` | Fix `E0308`: array declared 8 but held 7 elements; adds a test asserting declared length == element count |
| `ca310ab3` | **S2-prep**: native commit-marker reader (`backup_commit.rs`) + 17-case Python parity test |
| `1e1e109c` | Record that all seven zero invariants are measured (file said 0 of 7) |
| `16e0c9e8` | Guard the fixture's `effects` map; repair a helper that had become infinite recursion |
| `23a3f2c2` | Make the agent driver test wait for state instead of a fixed 4300ms sleep |
| `4b96c8ea` | Revert `--max-parallel` to 2 after a misdiagnosis (see section 6) |
| `f6cb643a` | Overlap the race variants' two halves; use all four vCPUs |
| `05122174` | Update the foundation gate for the split Go jobs |
| `377de8c4` | Split the two Go race suites into parallel jobs (183 min → ~70 min) |
| `331317cc` | Cover the `ErrServerStopped` arm deterministically |
| `bdc85ef0`, `923eb4e9` | Green-run records |
| `e58743a8` | Pin `eol=lf` on evidence artifacts (CRLF made a digest match only on Windows) |

## 4. What remains

### 4.1 Route migration — the largest remaining block

Counted from **route registrations** — Python `@router.<verb>("...")` decorators and
Rust `.route("...")` calls — because a grep for the path string also matches test
fixtures and helper constants, which inflates the native side:

- Python registers **106** unique `/api/workspace/*` shapes; Rust registers **27**.
  **79 are not yet in Rust.**
- The same basis gives 82 Rust `/api` shapes in total (27 workspace, 55 non-workspace).
- An earlier count of 109/95/76 used bare string matching and overstated both sides;
  use the registration basis when re-auditing.
- Traces: **4 of 4 already migrated** — `GET /api/traces`, `GET /api/traces/{trace_id}`,
  `GET /api/traces/{trace_id}/export.json` (all in `trace_routes.rs`) plus the
  `/trace/{trace_id}` page route. The old "7 trace routes" note is stale. Note that
  `crates/deepseek-gateway/tests/trace_routes.rs` also contains `doesnotexist` paths;
  those are test fixtures, not routes, so count registrations rather than string
  literals when re-auditing.
- Top-level control/feature routes (`/api/automation`, `/api/compress-context`,
  `/api/semantic-cache`, `/api/rag/*` writes, …): counting `.route("...")` registrations
  in `crates/deepseek-gateway/src` gives **82** `/api` shapes — 27 workspace and **55**
  non-workspace. Count registrations, not string literals: a grep over all Rust files
  reports 72 non-workspace shapes because it also matches test fixtures and helper
  strings. The real gap needs a per-route audit against Python, not a subtraction.

Slice plan: `tasks/native-runtime/workspace-backup-dr-migration-plan.md` (S0–S8).

Qualification status, stated exactly as the matrix records it:

| Slice | Status |
| :--- | :--- |
| S1a | cloud-qualified (`e3aa9e22`) |
| S1b | cloud-qualified (`2a0f1ef9`), re-qualified by the trace run below |
| S1c (read-only traces) | cloud-qualified — `rust-cloud.yml` run 42459534356 at `aed5d99f`, Rust checks + coverage success, `sourceTreeDirty: false` |
| S2a (`GET /api/workspace/backup-catalog`) | **implemented and locally verified only — no cloud run recorded** |
| S2-prep (commit reader) | landed this session; verified by the 40/40 `ci.yml` run at `d8ea1d5f` |

Do not describe S2a as cloud-qualified without a run URL; the matrix has none for it.

**S2 still open, and each is a real dependency rather than a route stub:**

- `POST|DELETE /api/workspace/backup-catalog/{backup_id}/pin` — needs a native writer
  lease (`backup_writer_lease`, 396 lines: `O_EXCL` acquisition, fencing token, expiry,
  takeover) and `backup_publish._create_exclusive` for the event file.
- `POST /api/workspace/backups/{backup_id}/scrub` — `scrub_backup` reads every committed
  ciphertext member, rejects reparse points, compares sizes and recomputes per-member
  sha256. Needs the object layout.
- `POST /api/workspace/backups/{backup_id}/verify-unlock` — needs an
  `AGE-SECRET-KEY-` recovery identity and a real `verify_unlock_drill` against
  `RESTORE_DIR/drills`; native Age decryption and drill staging do not exist.

### 4.2 Ownership inversion

- `current_production_authority` in `release/native_runtime_ownership_v1.json` is still
  `python`.
- `backup_catalog_store` was added to `DECLARED_NATIVE_DATA_DOMAINS` (7 → 8). The gate is
  symmetric with Python's, verified across all three runtime modes: both sides may write
  under `python_authoritative` and `go_authoritative`, and both are denied under
  `python_disabled`.
- Go control authority for policies/targets/DR still needs a real promotion against a
  genuinely stopped-and-fenced Python source.

### 4.3 Packages and zero-Python deployment

- Desktop (WebView2), Android APK, launcher packaging must run the native runtime
  independently of Python.
- `production_artifact_zero_python` **passes** for the default native **image** (a
  container-side check proved no `python`/`python3`/`node`/`nodejs` on `PATH` after a
  fresh-volume start). It is **not** satisfied by the release zip, which is a
  4294-entry source tree containing 1029 Python files.
- Zero-Python clean-environment workloads with non-zero successful operations remain.

## 5. Hard rules that were learned the hard way

These are not style preferences; each one cost a CI round or a wrong report.

1. **Read the artifact, not the conclusion.** A green job has three times in this
   workstream measured nothing: a hard-coded `informationalOnly: true`, a coverage
   figure that moved on the clock, and a YAML `#` comment that swallowed every flag
   after it. Verify numbers from the report body.
2. **A `#` inside a `>-` block is not a YAML comment** — it is folded into the shell
   command and truncates everything after it. `tests/test_rust_sidecar_latency_gate.py`
   now asserts on the *parsed* command.
3. **Pin `eol=lf` on any file whose digest is recorded.** With CRLF in the worktree and
   LF in the repository, a recorded SHA-256 matches on exactly one platform.
4. **Fixed sleeps are not synchronization.** `time.Sleep(4300ms)` silently became
   load-dependent; it is now a condition wait, verified under 8 saturated cores.
5. **A test's shared fixture needs a lock** if the driver goroutine writes it and the
   test reads it — otherwise `-race` is entitled to fail it.
6. **Do not lower a coverage floor to make a gate pass.** Both Android flakes were
   traced to a single statement each and fixed at the source.
7. **Do not claim a PASS you did not observe.** Cancelled runs, superseded commits and
   network failures all look like results if you only read the summary.

## 6. Known limitations of this host

- **Cargo cannot build locally.** `cargo check` fails in dependency build scripts, so
  Rust changes are compile-checked only in CI. Mitigation: `tests/test_native_workspace_store_cutover.py`
  now asserts Rust fixed-size-array lengths, turning a class of `E0308` into a local
  test failure. Everything else Rust-related must be verified in the cloud.
- **The Go race detector cannot run locally** (`0xc0000139`, missing runtime DLL —
  reproduced on an untouched package). `-race` verification is cloud-only.
- **Docker, real MinIO topologies and provider workloads are cloud-only.**
- Local gates that *do* work: `ruff`, `mypy` (1035 files),
  `scripts/update_docs_language_nav.py --check` (239 files),
  `scripts/check_release_version.py`, `scripts/native_runtime_contract.py --check`,
  `scripts/production_runtime_inventory.py --check`,
  `scripts/native_store_writer_gate.py --check`, `gofmt`, `go vet`, `go build`,
  `go test` (non-race), `rustfmt`, `cargo metadata --locked --offline`, pytest subsets.
- Known pre-existing local failures (reproduced on a clean tree, not caused by this
  work): `tests/test_backup_456_coverage_booster.py::test_rebalance_policy_replicas_with_draining_and_diversity`,
  `tests/test_backup_480_real_two_fleet_four_minio_e2e.py` (needs `link.exe`),
  `tests/test_server_integration.py`, five `tests/test_native_media_*_fixture.py` suites,
  `go/internal/store TestNativeOriginWALMarkerRejectsCommittedForeignSchemaWithoutWrites`
  (Windows temp-dir lock), `go/internal/launch TestManagedProfileRejectsRedirectedPrivateDirectories`
  (symlink privilege).

## 7. Next executable tasks

Ordered so each is completable and verifiable on its own.

1. **Native writer lease** (`backup_writer_lease` equivalent): `O_EXCL` acquisition,
   fencing token, expiry, takeover, and the same 423 refusals. This is the other half of
   the S2 write path and is reused by every later write slice. Verify with a Python
   oracle over the lease file and a `python_disabled` write test.
2. **`pin` POST/DELETE** on top of (1) plus the commit reader already landed.
3. **`scrub`**, then **`verify-unlock`** (the latter needs native Age decryption).
4. **S3 retention** (5 routes), then S4 policies/targets (12), S5 restores (5),
   S6 DR (10), S7 resilience (18), S8 artifacts/exports.
5. **Go control-domain promotion** against a genuinely stopped-and-fenced Python source,
   then move `current_production_authority` off `python`.

## 8. Reference: the push helper

Because git transport is broken, pushes replay local commits through the API. It
asserts tree equality at each step, so a mismatch aborts instead of writing a wrong
commit:

```python
import base64, json, subprocess, time
REPO = "leizd/DeepSeek-Infra"
BRANCH = "codex/native-s0-store-ownership-20261109"

def gh(args, stdin=None, tries=8):
    last = ""
    for attempt in range(tries):
        p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           input=stdin, encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        last = (p.stderr or "")[:200]
        time.sleep(15 * (attempt + 1))
    raise SystemExit(f"FAILED {args}: {last}")

parent = json.loads(gh([f"repos/{REPO}/git/ref/heads/{BRANCH}"]))["object"]["sha"]
SHA = "<local commit to push>"
files = subprocess.run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", SHA],
                       capture_output=True, text=True, check=True).stdout.split()
entries = []
for path in files:
    content = subprocess.run(["git", "cat-file", "blob", f"{SHA}:{path}"], capture_output=True).stdout
    blob = json.loads(gh([f"repos/{REPO}/git/blobs", "--input", "-"],
        json.dumps({"content": base64.b64encode(content).decode(), "encoding": "base64"})))
    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
parent_commit = json.loads(gh([f"repos/{REPO}/git/commits/{parent}"]))
tree = json.loads(gh([f"repos/{REPO}/git/trees", "--input", "-"],
    json.dumps({"base_tree": parent_commit["tree"]["sha"], "tree": entries})))
message = subprocess.run(["git", "log", "-1", "--pretty=format:%B", SHA],
                         capture_output=True, text=True, encoding="utf-8").stdout
created = json.loads(gh([f"repos/{REPO}/git/commits", "--input", "-"],
    json.dumps({"message": message, "tree": tree["sha"], "parents": [parent]})))
json.loads(gh([f"repos/{REPO}/git/refs/heads/{BRANCH}", "-X", "PATCH", "--input", "-"],
    json.dumps({"sha": created["sha"], "force": False})))
```

Then dispatch, since a dedicated branch does not auto-trigger `ci.yml`:

```bash
gh workflow run ci.yml --repo leizd/DeepSeek-Infra --ref codex/native-s0-store-ownership-20261109
```

`native-go` is the long pole (~70 min with the split); budget a full run before reading
results. Cancel superseded runs rather than letting them complete — a cancelled run
reports `failure` for jobs that were only interrupted.

## 9. Where to read further

| File | Contents |
| :--- | :--- |
| `tasks/native-runtime/migration-matrix.md` | Per-domain parity audit and slice log (3251 lines) |
| `tasks/native-runtime/continuation.md` | Chronological checkpoint log (13590 lines) |
| `tasks/native-runtime/todo.md` | Outstanding item list (3575 lines) |
| `tasks/native-runtime/workspace-backup-dr-migration-plan.md` | S0–S8 slice plan and verification rules |
| `release/native_runtime_gate_evidence_v1.json` | 15 gates + 7 invariants, with artifacts and digests |
| `release/native_runtime_ownership_v1.json` | 67 domains, `current_production_authority: python` |
| `release/native_runtime_5_0_evidence_v1.json` | Release assessment (`NOT_READY`, by design) |
