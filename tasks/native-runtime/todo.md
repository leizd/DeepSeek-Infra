# 4.8.1 Todo — Native Runtime Contract Freeze

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

## Active work — 2026-10-10

- [x] Clear the seven user-authorized Rust compilation/dependency cache roots,
  record actual free-space growth and verify source/index/report preservation.
  Continue Rust compilation in the cloud; retain toolchains/SDK/runtime data.
- [x] Prepare a repository Go producer/process probe against actual real-UTC
  ControlRPC and lease stores; planning/tasks preflight passes with full process
  case code compiled. Private environment/public binding/TLS fixture tests pass.
- [ ] Obtain authorization for the concrete follow-up cloud source upload and
  run actual production Rust custody/TLS/restart/expiry signing cases. Current
  producer tests do not qualify those cases or provider effects.
- [x] Complete Go `agent-grant-current-v2`: all 10 gates pass, including default
  and Android race. Exact coverage is 10205/10742 = 95.000931% and
  10031/10552 = 95.062547%; the 95% floor and frozen/current Go hashes are
  unchanged. Keep the final receipt; the owned container and tmpfs are released.
- [x] Add typed Rust Agent execution signing with an independently renewed Go
  claim, exact local epoch admission, deadline bounds and immutable retry bytes.
  Go verifies the distinct signature and immutable scope; 145 Worker tests,
  strict Worker Clippy and current signing regressions pass. Lease RPC fixtures
  do not qualify the actual Go producer or provider effects.
- [x] Qualify the current signing/disk-guard Rust inputs in the user-authorized
  cloud snapshot `291092e9210570416036d4a7292f53498a8572d8`: strict Clippy,
  1414 passing tests/one existing Redis ignore, all 15 crates, 1415 inventory
  entries and **81.797900%** line coverage at the unchanged 80% floor.
  Run `38034954077` passes; `rust-cloud-preview/cloud-evidence.json` records
  source bindings and small reports without downloading compiler caches.
- [ ] Complete the real-time production Go/Rust signer probe over TLS. Current
  default/Android Go 95% gates already pass; the new cloud process cases remain
  pending. Preserve each complete receipt as proof of its exact original inputs.
- [x] Exercise actual Go Agent claim/renewal with default real UTC time and
  authenticated loopback RPC in an isolated fresh store. Planning/tasks,
  replay, rebinding, cancellation and expiry of the writer while its action
  remains live pass (`agent-real-utc-probe-green-v4`). This is producer proof;
  Rust signing, TLS, artifact custody and provider effects remain unqualified.
- [x] Admit native Agent execution with a separate action fence, both live Go
  domain authorities, immutable request/plan binding and mandatory run reservation.
  Register typed ClaimExecution/RenewExecution on the actual authenticated service.
- [x] Reject lossy UTF-8 identities before creating a replay digest. Preserve raw
  caller input while defaulting a bounded lease; verify normal planning admission,
  cancellation/plan-change renewal denial, reconciliation takeover, transaction
  rollback, SQLite write failures and journal-scope/epoch replay refusal.
  Current focused Store/API regressions: `agent-execution-go-replay-scope-v17` PASS.
- [x] Finish the current complete default/Android Go gates. `full-v1` failed
  94.665665% coverage and `full-v2` failed 94.973222%; rounding to 95.0% is not a
  pass. `agent-execution-go-full-v3` is the new frozen-input batch. Keep the
  original 95% floor and qualify both race suites; preserve all failed receipts.
  All ten checks now pass, including both race suites: default **95.020201%**,
  Android **95.073185%**. All 374 Go/Proto inputs match; session 81813 is terminal
  and its owned container is removed without a cache archive (`full-v3.json`).
- [x] Qualify the current full Rust workspace after body/read and additive Proto
  changes: strict Clippy, 1409 passing tests/one original ignore, all 15 crates,
  1410 inventory entries and complete LCOV at **81.773383%**, original 80% floor.
  All 3673 input bytes remain unchanged; `agent-execution-rust-full-v3` passes.
  Preserve the two offline-oracle image failures; their repair changes only the
  verification environment. The final RAM container is removed, no cache archive.
- [x] Run the actual Go/Rust lease/public-read product probe only after
  the complete Go receipt passes and its Go/Proto hashes match the current inputs.
  Label v1 passes actual admission/replay/renewal/terminal refusal, body reopen,
  public detail/cursor/NDJSON and authentication. Its fresh-empty fixture uses
  clock 1000; provider effects and nonempty handoff remain unqualified. Both
  product containers are removed without an archive; session 11640 is terminal.
- [ ] Implement signed Rust effect authority and actual Agent node/provider/tool
  execution, durable dispatch/outcomes, heartbeat and uncertainty reconciliation.
  Execution claims and body/event reads alone do not qualify these paths.

## Previous Agent metadata qualification — 2026-10-09

- [x] Implement the actual Go Agent metadata event writer: transactional cursor,
  projected DAG, immutable history, replay receipt, and denial of generic bypass.
- [x] Register the authenticated typed Agent metadata gRPC service; validate
  metadata fence equality against the live domain epoch inside the write transaction.
- [x] Verify SQLite persistence/reopen, rollback, pagination, request replay,
  actual RPC authentication, shadow/writer/fence denial and malformed requests.
- [x] Match 62 original-reference histories at all 1000 checkpoints; retain
  long tasks and remove untouched nodes after plan edits.
- [x] Complete all ten default/Android Go gates for the preceding metadata input, including both race
  suites and original 95% coverage floors, on unchanged Go input `6830e490faeb`.
  Verify source receipts and owned-container/RAM cleanup (`agent-events-go-full-v2`).
- [x] Finish current full Rust qualification after the additive Agent Proto change
  and new body storage (`agent-execution-rust-full-v3`, 81.773383%, 1409/one ignore).
- [ ] Complete Rust event/request artifact custody, public Agent routes and
  real fenced task effects/reconciliation, nonempty legacy Agent migration and
  complete restart/resume/rerun/cancellation workflows. Artifact references and
  metadata-only successes do not qualify these workloads. **NOT_READY**.

Current Rust prefix parity correction preserves the first four distinct recognized
planner roles even after long invalid/duplicate prefixes. A real public-HTTP
regression first returns four fallback roles instead of the reference two; after
the production fix all ten real Go/Rust controlled-provider scenarios pass.
Current fmt and strict all-target/all-feature Clippy pass. Complete all-feature
tests pass **1394**, with one original ignored test. The first snapshot omitted
the gitignored real Vite bundle and failed `production_contract`; its receipt
remains. A genuine build from unchanged frontend inputs adds 110 files (3,336,573
bytes), and the original unmodified test then passes. Rust source bytes are
unchanged by this topology repair. Default intermediates occupy 3,669,757,952
RAM bytes. After sharing the cfg(test)-only queue lock between Hub and HTTP
router tests, all 198 Gateway unit tests and current strict Clippy pass. The
original complete 80% coverage gate passes **74329/91151 (81.544909%)**,
with all 15 crates, 1395 inventory entries, 1394 passing tests and one original
ignored test, and complete LCOV. Input `7f89c13480e1` changes only test
synchronization from the preceding snapshot; every frozen fixture and test
assertion is unchanged. The 12 GiB RAM disk/14 GiB memory/zero-swap verification
container is removed, with its 9,221,550,080 bytes of intermediates/source copy
released and no cache archive. The initial 10 GiB capacity refusal and subsequent
A2A concurrency failure remain recorded. Proof:
`artifacts/native-20261009-agent-prefix-qualify-v3.json` and its coverage/cleanup
receipts.
Proof: `artifacts/native-20261009-agent-prefix-full-test-ui.json` and
`native-20261009-agent-prefix-freeze-v3.json`. No remote provider, durable Agent
product, zero-Python production, device or exact-head release proof is claimed.


- [x] Retain Agent metadata approval/orphan/resume/rerun states and snapshot
  updates with durable retry/reopen tests and no execution-epoch escalation.
  Two Go regressions and the complete Windows Store suite pass. Connect the
  real Python Agent writer and enabled auto-resume to existing ownership denial
  before filesystem/worker mutation; 54 writer/execution-entry/reference tests,
  Ruff and local Mypy pass. Reads and reference modes retain their behavior.
  Review: `artifacts/native-20261009-agent-durable-control-review.md`.
- [x] Finish the current 349-file Go complete default/Android test/race/95%
  qualification using executable RAM. Format/modules/default vet/test/race and
  coverage **9587/10076 (95.146884%)** pass; Android vet/test/race and coverage
  **9413/9886 (95.215456%)** pass. The verification container is removed and its
  991,043,584-byte RAM cache released. Preserve the initial logging-argument
  failure. Android-tag validation does not qualify an APK or device.
- [x] Finish per-build compiler/linker scratch, registry/download and Cargo
  metadata capacity protection. Disable implicit toolchain installation in child
  environments; explicitly opted-in installation is monitored throughout metadata
  and compilation. All 64 related regressions pass without skips, plus Ruff/Mypy.
  Full workspace fmt/check/strict Clippy and two all-feature test runs pass:
  1394 passed/one original ignored in each run; repeat reuses all 599 artifacts.
  RAM cache stays at 7951 files/4,176,035,014 logical bytes, with no scratch
  directories left. Approved exact-list cleanup removes 1964 dependency files,
  recovers 631,775,232 bytes of D: free space and preserves all 55 sibling hashes.
  Evidence: `artifacts/native-20261009-rust-final-policy-evidence.json`. Preserve
  initial test-fixture failures; production inputs are unchanged by their repair.
- [ ] Complete retained interactive multi-agent chat and the actual browser's
  durable `/api/agent-runs` flow. The new authenticated Go gRPC scheduler has
  real-socket scheduling/auth tests. The 103-case pure reference fixture passes;
  eight actual Go-daemon/Rust-HTTP controlled-provider scenarios pass: normal,
  revision, retry, role denial, shadow refusal, bearer refusal, positive budget
  exhaustion and zero/unlimited budget. Pure scheduling does not grant durable
  mutation authority. Require remaining reference
  compatibility, real Rust/Go/provider execution, role denial, retry/cancellation,
  durable unique-writer state, restart/recovery and browser acceptance before
  qualifying this domain. Current Go full/race/95% coverage, current Rust 80%
  coverage, durable browser flow and whole-product acceptance remain open.

- [x] Wire native cascade draft/gate/judge/refine execution and final-result
  stream replay, including vision bypass, environment overrides, actual Ollama
  responses, failure masking, disconnect and zero-tool judge finalization.
  Current Rust input `44c20f629c64` passes 1376 default tests, workspace fmt and
  strict all-target/all-feature Clippy; all 30 Windows router tests pass.
- [x] Collect the original complete all-feature coverage, LCOV and inventory
  for current input `8548646e6de8`: **73837/90043 lines (82.001932%)**, 1393
  passed and one original ignored test, complete 1394-test inventory, no core
  exclusions, genuine dirty-source Git metadata and strict current Clippy.
  Production bytes match default-qualified `44c20f629c64`; the only delta fixes
  a clock-sensitive test assertion. Preserve its failed coverage run and the
  passing corrected Windows case. Source and original index are unchanged.
  Consolidated evidence: `artifacts/native-20261009-chat-cascade-evidence.json`.
- [x] Return ordinary `/api/chat` JSON for non-streaming requests and retain
  shared tool-round/search behavior. Full gateway 371-test pass; 16 router
  tests cover JSON shape, prefetch/tool rounds and request isolation.
- [x] Reduce native build disk growth: default symbol stripping, explicit
  diagnostic/coverage profiles, fixed caches, Android output/cache separation
  and capacity protection in all four native build entry points. Five exact
  task caches recover 5.85 GB without deleting or changing file contents.
  Two actual Windows builds reuse all 420 artifacts without cache growth.
  Forty-four build/coverage/contract tests pass with no exclusions; original
  coverage floor and complete test inventory remain enforced.
- [x] Qualify the final coverage-map input `3a38ddc2daaf` with full
  Rust fmt/strict Clippy/default tests and the original all-feature 80% gate.
  Earlier inputs `20364b1c6e9b` and `8b4f02e1fc9b` pass 1360 tests; their
  full-type/line-table coverage attempts hit the RAM reserve. Preserve both
  failures and the frozen-Git diagnosis. Final default gates pass; measurement
  passes 1377 all-feature tests with one ignored. The remaining formal export
  passes at 73452/89655 lines (81.927388%) with the complete 1378-test inventory,
  LCOV and true dirty-source metadata. Uses 10 GiB RAM, original capacity and
  coverage thresholds, and an independently verified RAM Git view. Original
  index and all 3640 frozen-source bytes remain unchanged. Consolidated local
  evidence: `artifacts/native-20261009-rust-disk-policy.json`.

## Previous qualified work — 2026-10-08

- [x] Complete original current-source Go/Rust full gates. Stable 343-file GUI
  Go v5 passes all ten gates, including both race suites; default coverage is
  95.083945% and Android 95.152198% at the original 95% floor. Frozen 415-file
  Rust search-prefetch input `f308ae998b47` passes workspace fmt, strict
  all-target/all-feature Clippy, 1355 default tests and original all-feature
  coverage 81.898729% at the unchanged 80% floor. Preserve earlier failed runs.
- [x] Implement and qualify forced-search prefetch at the router/provider/tool
  boundary: live prefetch progress, hardened result context, cache, seeded
  memo/budget/citations, failure context and disconnect before model dispatch.
  Thirteen focused tests pass, including 22 offline browser-projection oracle
  cases; the full current-source Rust gates above bind these same bytes.
- [x] Complete original Windows real-S3/recovery gates on frozen 415-file inputs.
  The race loader preflight is repaired with test-only synchronization-library
  linkage, without changing production CGO-disabled Go. Fresh five-MinIO gates
  pass: seven storage tests, ten worker tests and the actual Go race integration,
  including versioned replay and six lost-response/takeover cases. Receipt:
  `native-20261008-provider-current-windows.json`. Fixture data/logs are retained;
  actual remote-provider and whole-product recovery acceptance remain open.

- [x] Implement and locally qualify native CLI/mobile options, masked prompt,
  real control readiness, LAN URL guidance and actual headless/mobile HTTP
  workloads. Both full Go variants pass all ten original gates with the 95%
  floor; Windows checks and three source/symbol security scans pass. Rust fmt,
  strict Clippy/default workspace tests and original 80% coverage pass.
- [ ] Qualify actual default browser/Termux/device opening, retained mobile
  recovery and all platform packaging; existing receipts qualify the named slices.
- [ ] Complete the configuration GUI and ownership-controlled credential workflow.
  `--gui` now starts the private configuration page before the backend. The first
  frozen Windows candidate passes actual save/start/stop/reload/restart and owned
  HTTP message transport; the user screenshot proves the conversation entry.
  Frontend full check passes 613 tests. DPAPI/AES-GCM and exclusive settings
  locks are implemented. Corrupt ciphertext is retained until explicit recovery;
  actual Windows/Linux settings-owner hard-kill/reopen probes pass. Both HTTP
  and native-window stale-close regressions pass after atomic controller fencing.
  Focused launchgui coverage is 97.7%; GTK confirmation/callback boundary tests
  pass, with actual modal cancellation still open. The stable 343-file Go v4
  source builds three fresh Windows binaries on C: and passes three source-security
  variants. Matching pinned Rust Windows binaries build offline on C:, and the
  frozen v4 HTTP/DPAPI/native-process integration passes 34 real checks including
  nonempty model/file transport, restarts and explicit corrupt-settings recovery.
  The native window/launcher entry and browser message are not exercised by this
  harness. Complete v2 default coverage remains a genuine 94.979374% failure;
  v3/v4 full runs fail under D: disk I/O, and v4 additionally exposes `/tmp`
  `noexec`. Keep the original 95% floor and all failed receipts. Controlled
  legacy publication/migration, all-platform close/recovery, actual window/launcher
  workloads and fresh Android/package bindings remain open. Original full Go
  gates now pass at the original 95% floor on the unchanged 343-file inputs.
- [x] Fix and qualify async-runtime Rust browser-sidecar startup with an actual
  gRPC service regression and pinned fmt/strict Clippy/default workspace tests
  (1349 passes, 123 suites, one ignored) on the complete topology.
- [x] Complete current Rust 80% coverage after storage recovery. Fresh executable
  RAM target passes all-feature coverage at 73301/89502 (81.898729%) on current
  415-file inputs. Earlier disk/container I/O failures retain their null metrics.
- [ ] Recover the local validation environment without deleting runtime data.
  The explicitly authorised Docker restart recovered health before D: filled
  again. The authorised exact 1964-file task cache deletion was rejected by
  automatic approval even after authorisation; no deletion occurred. A reviewed
  human-only script and pending manual result are recorded in `continuation.md`.
  Docker now responds and full Go/Rust verification runs with caches/logs on C:
  and executable `/tmp`. D: headroom remains limited; no manual-removal receipt
  has been observed. Preserve all failed attempts and runtime volumes/data.

## Local integration — 2026-10-07

- [x] Apply the explicitly authorised local merge: HEAD `37efd8e9`, reviewed tree
  `678ce430`; preserve all 74 original task files. No push or production operation.
- [x] Repair stale Android asset staging; inspect corrected APK `32b35b1cc71b`;
  retain UID/token/all 21 store files across upgrade, and read the old business
  run/artifact. Three actual instrumentation tests and the full 26-check app
  workload pass for its dirty source `c593437df2d7`.
- [x] Qualify typed Android image/raster-PDF OCR APK `bde81447a61a` (dirty source
  `ab9d9f0b5616`): seven device tests, 26 recovery/UI checks, 17 file checks,
  ten EXIF/public-disconnect lifecycle checks, and actual browser upload/preview/
  byte-identical download. Blank pages and temporary-file recovery are proved.
- [x] Repair instrumented worker test profile leakage; complete Linux coverage
  at 81.454954% with unchanged inputs, 1361 passes and the original 80% floor.
- [x] Qualify clean local commit `71b05a62` APK `cf00d5ca167a`: preserve all
  44 upgrade files; rerun seven device tests, 26 recovery/UI checks, 17 file
  checks, ten lifecycle checks and actual browser text/render/source downloads.
- [x] Implement and locally qualify the actual Windows WebView2 host: retained
  token, nonempty native React upload/read/source workload and hard-kill cleanup
  of the default launcher's desktop, WebView2 and backend process tree.
- [x] Rebuild clean local commit `ff3fab80`; qualify Windows SDK workload, rapid
  natural-lease restart, owned-tree cleanup and zero-finding pinned security scan.
- [x] Qualify separate 299-file Linux guardian/GTK draft with all ten full Go
  gates and unchanged default/Android 95% coverage floors. Windows shared
  preparation build/vet/test, seven real SDK and twelve restart checks also
  pass, with stable source bytes.
- [x] Qualify the bundled Python/Node-free Linux consumer: default Chinese React
  rendering, actual native mouse/keyboard/system chooser upload, text preview,
  original view and byte-identical 129-byte download; retain token and REST/UI
  originals across same-container restart. Bind image/bundle/source/screenshots.
  Preserve earlier blank tools-image controls and correct HTTP counter labels.
- [x] Integrate the reviewed Linux slice with raw-byte backups, canonical Go
  equality and local guards, Windows build/vet/test, Linux boundary race and
  zero-finding pinned Linux security scan; record the local commit/content binding.
- [ ] Qualify permissions, all retained flows and platform packages. Investigate the earlier
  tools-image display difference without changing production acceleration.
- [ ] Complete the native desktop window and configuration/mobile launch flows
  on all retained platforms, with actual workload and close/failure recovery.
- [ ] Qualify exact-head CI/Evidence; retain shadow/control ownership, ARM64,
  formula/media and whole-product gates.

The resolved source retains the clean primary `707a0528` and the isolated task's
Android, browser authentication, Skills parity and store fencing work. Both Go
race/vet/95% coverage variants pass; the actual Rust HTTP comparisons pass
133 read, 49 mutation/recovery and 20 run cases. React checks and actual browser
create/edit/disable/reload pass. The new Android OCR APK passes its measured
local device/browser workloads. Clean commit `71b05a62` now has a freshly
qualified source-bound APK; exact-head CI remains open. Online Skill, Android
formula/media and ARM64 qualification remain open. Full proof scope, source hashes,
unchanged thresholds and retained pre-integration APK are in
[`continuation.md`](continuation.md#integration-preview--2026-10-07).

The preceding comparisons qualify the retained pre-merge review source. Local
integration is now applied in the isolated Git worktree; the clean `D:/deepseek`
checkout remains unchanged at `707a0528`. Whole-product source/package
qualification and exact-head CI remain required. Status remains **未完成 / NOT_READY**;
retain every whole-product gate. Current evidence is recorded at the top of
[`continuation.md`](continuation.md).

### Retained primary migration record


Status: all Phase 0-3 items are checked; Phase 4's two release gates (exact-head CI /
Evidence Assembly, qualification) remain open and no 4.8.1 release has been cut —
`VERSION` is `4.8.0`. Live slice work continues in
[`continuation.md`](continuation.md) under the 5.0 native milestone; the unified tracker
is [`migration-matrix.md`](migration-matrix.md). This file is the 4.8.1 contract-freeze
record; the active section below records current slices without rewriting that history.

## 5.0 active execution — 2026-10-06

- [x] Close the preceding CI repairs on exact HEAD `3406f7ad`: all 37 jobs pass,
  including Rust/Go coverage, race and Evidence Assembly. No native readiness flip.
- [x] Generate and retain an independent control signer inside Rust, encrypted
  with the frozen custody envelope; add a native initializer and typed scoped RPC.
- [x] Integrate default authoritative Go storage execution and renewable claims
  with Rust epoch/grant issuance after durable claim, with TLS, public binding,
  exact intent and post-signing ownership/lease checks.
- [x] Persist immutable Rust signature request/nonce receipts; deny changed replay,
  missing custody, wrong caller/scope, stale epochs and foreign/symlink journals.
- [x] Qualify native custody against three real MinIO providers, including a
  versioned provider, Unicode object bytes, force-kill/restart and stable receipts.
  The administrative promotion fixture and leased `VERIFYING` limit stay explicit.
- [x] Fix Go/Python/Rust canonical signing bytes for HTML/Unicode separators;
  retain all frozen fixtures and the unchanged full verification contracts.
- [x] Qualify custody checkpoint `005e5bb6` on exact-head CI: all 37 jobs,
  including Evidence Assembly, release-package and RC checks, pass.
- [x] Qualify six actual provider response-loss/lease-takeover scenarios under
  Go race. Retain original dispatch epoch 1 under successor epoch 2; a real 404
  stays unknown until a delayed original PUT is byte-verified. No repeated PUT;
  another worker death preserves receipt/object version. Proof settlement stays open.
- [ ] Complete production Fleet signer custody/rotation and leased proof settlement.
- [ ] Finish remaining product/platform/domain transfers and release qualification;
  the full migration remains **未完成 / NOT_READY**.

## Previous active execution — 2026-10-04

- [x] Fix public mirror body guards and Python text coercion: native 7 mirror +
  2 auth tests and Python HTTP oracle 12 tests pass; retain the red empty-body case.
- [x] Fix relative launcher binary/data/static paths; native child listener tests
  verify sibling cancellation after normal/failing exit. Launch tests pass at 96.5%.
- [x] Validate real control RPC health, missing policy state, duplicate credentials
  and the 1 MiB body bound. Restore 5515 pinned SDK files with exact SHA-256 matches.
- [x] Restore current whole Go coverage to 95.020796% without lowering 95%.
  Go format/vet and launch/API/lifecycle race pass (155 tests, no skips/races).
- [x] Repair whole Rust check/fmt/all-target/all-feature clippy and coverage crate
  inventory (actual fifteen from locked Cargo metadata, 19 evidence tests).
- [x] Reproduce and fix Rust/Python mirror writes during the restore lock window:
  hold the OS lock, recheck the fence and preserve successful recovery uploads.
- [x] Complete whole-Go Linux race: 1768 passes, no failures/races, one intentional
  fixture-generator skip; snapshot-qualified evidence, no Go changes since it.
- [x] Implement persistent mirror source fencing, byte inventory export and an
  independent Rust candidate importer. Verify real Age reads and force-kill
  recovery after a nonzero copy; imported stores remain denied pending admission.
- [x] Run the real Redis gate, reproduce error-reply poisoning and repair both
  clients; six parser/network tests and the real Lua/fence release test pass.
- [x] Refresh whole Rust tests/clippy and fifteen-crate coverage: 1334 passing
  executed tests, one explicit provider ignore, 80.732473% on snapshot `33acecda`.
  Exact-head release evidence is not established; investigate Git metadata.
- [x] Revoke a settled mirror candidate through the real native CLI; independently
  reattest and journal pre-admission handback, then prove original writer progress
  and persistent native/candidate denial with real Age data.
- [x] Serialize import/revocation/handback with the target workspace restore lock;
  refuse any invalid restore fence and deny Python writes to native candidates.
- [x] Move twelve production Redis Lua scripts into Rust, preserve the unchanged
  TypeScript oracle bytes and rerun the real Redis acceptance test.
- [x] Diagnose the ten full-Python failures without changing frozen fixtures;
  repair build inputs and current source-contract checks; 61 targeted tests pass.
- [x] Reproduce loss of Go control leaving the real candidate image healthy;
  use the Go supervisor, forward native trust configuration and handle SIGTERM.
- [x] Rebuild the pinned supervisor image; prove native persistent writes,
  sibling cancellation, live-writer restart denial, lease-expiry recovery and SIGTERM.
- [x] Fix actual private Compose channels and namespace-owner restart recovery;
  v1 RPC, credential/ownership refusal and native state preservation pass.
- [x] Refresh Go coverage to 95.030835%, vet, Ruff and 949-file mypy.
- [x] Complete current store race with its measured thirty-minute package bound;
  other whole-run packages passed, the default ten-minute store run timed out.
- [x] Rerun the complete Python 95% gate on the final frozen source; the resumed
  5629-pass/two-failure run at 95.574917% remains failed evidence; both failures
  are repaired and 32 targeted cases pass. The final `70b29cf9` input passes
  **5631 tests / 95.589801%**; session 27119 is collected. Git status failures reject evidence.
  Do not reuse older source PASS values.
- [x] Wire default worker S3 transport through dedicated production configuration;
  preserve raw supervisor values and isolate the worker state volume. Default
  Linux binary passes ten actual-provider tests including TLS, forced restart,
  exact completed receipt replay and a versioned bucket on three providers.
- [x] Complete Windows default-binary three-provider verification: ten real tests
  pass after isolating executable-location-dependent access denial; no ACL/security changes.
- [x] Rerun the combined Rust-byte/Go-promoted-control provider runner: seven
  storage cases, ten worker cases and three Go provider subcases pass on actual
  MinIO with the emitted default-binary hash and pinned Cargo/Go versions.
  Signing is still an isolated test fixture; production Rust custody is open.
- [x] Push checkpoint `7fe490d6`; run exact-head CI and preserve the six upstream
  failures. Repair oracle setup, versioned-read expectations, isolated MCP/native
  fixture and hybrid reference images, and default-worker TLS authority refusal.
  Local MCP/source/runtime guards and the Windows TLS process pass.
- [x] Collect repair checkpoint `181dbdf6` CI: 31 jobs pass, including native Go,
  MCP failover and hybrid. Repair missing Vite preparation in both Rust jobs and
  Go dependency downloads before S3 proxy isolation. The actual Linux production
  contract and ten workflow guards pass; coverage floors remain unchanged.
- [x] Obtain complete exact-head CI/Evidence for the prerequisite repairs on
  `3406f7ad`: all 37 jobs pass; earlier failed runs remain historical evidence.
- [x] Refresh complete Rust coverage after the `python_disabled` packaging and
  inventory assertion repairs: the unchanged 80% gate passes in that exact-head CI.
- [ ] Close mirror malformed-JSON/integer parity, browser upload, native handback,
  full restore consumer and Go action/epoch ownership and write admission.
- [ ] Finish all outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gates. The whole migration is **未完成**, readiness `NOT_READY`.

- [x] Serve `GET /api/workspace/home` on the Rust production router as
  `workspace_home.workspace_home`. The body matches a live Python call on the
  same root, including the truncated `counts.automationRuns` window. `"0"` is
  8 because `int(0 or 8)` is 8. A non-integer is 500. Missing auth is the
  nested 401. `HEAD` is empty 200. Other methods are 405 with
  `Allow: GET, HEAD`. An empty root stays empty. Not a domain flip.
  Inventory python-only routes 106 → 105 (`workspace.py` 28).
  Evidence: `cargo test -p deepseek-gateway --offline --test data_routes -- --test-threads=1`
  (31 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Replace the `501` on `POST /api/workspace/projects/{project_id}/artifacts`
  with `register_artifact`, and serve `PATCH` and `DELETE` of
  `.../artifacts/{artifact_id}` together. A truthy `path` appends a version.
  A legal create, title update, version append, and delete match the Python
  store only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and
  the files stay unchanged. The 500-row window matches the oracle. Not a
  domain flip. Inventory python-only routes 108 → 106 (`workspace.py` 29).
  Evidence: `cargo test -p deepseek-gateway --test data_routes -- project_`
  (7 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Replace the `501` on `POST /api/workspace/projects/{project_id}/saved-items`
  with `create_saved_item`, and serve `PATCH` and `DELETE` of
  `.../saved-items/{saved_id}` together. A legal create, rename, and delete
  match the Python store only when `DEEPSEEK_RUNTIME_MODE=python_disabled`.
  Otherwise 409 and the files stay unchanged. The 1000-row window matches the
  oracle. Not a domain flip. Inventory python-only routes 110 → 108
  (`workspace.py` 31).
  Evidence: `cargo test -p deepseek-gateway --test data_routes -- project_`
  (6 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Serve `POST /api/automation/{automation_id}/run` on the Rust production
  router as `runner.run_once`. A legal `save_item` run persists history, the
  saved item, the project touch, and one memory summary only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and those files stay
  unchanged. Missing automations are 404 with no history file. Invalid `now`
  is 400 before lookup. Empty or missing `Content-Length` is `{}`. This is
  not a domain flip and not full action parity: `traceId` is empty, and
  actions other than `save_item` that pass policy record a failed run.
  Inventory python-only routes 111 → 110 (`automation.py` 2).
  Evidence: `cargo test -p deepseek-gateway --test automation_run_routes`
  (3 passed) with the definition and template tests, plus
  `pytest tests/test_production_runtime_inventory.py`.
- [x] Serve `PATCH` and `DELETE /api/automation/{automation_id}` on the Rust
  production router. A legal rename and delete match `update_automation` /
  `delete_automation` only when `DEEPSEEK_RUNTIME_MODE=python_disabled`.
  Otherwise 409 and the file is unchanged. Project touch follows a successful
  write. The 500-record window matches the oracle. Not a domain flip.
  Evidence: `cargo test -p deepseek-gateway --test automation_definition_routes`
  (2 passed).
- [x] Serve `POST /api/automation/templates/{template_id}` on the Rust
  production router. A legal create matches `create_from_template` only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and the file is
  unchanged. Unknown templates are 404 before that gate. Project touch follows
  a successful write. Duplicate and 500-visible cap refusals do not rewrite.
  Not a domain flip. Inventory python-only routes 112 → 111 (`automation.py` 3).
  Evidence: `cargo test -p deepseek-gateway --test automation_template_routes`
  (2 passed) and `pytest tests/test_production_runtime_inventory.py`.

## 5.0 active execution — 2026-10-03

- [x] Serve backup retirement create/get/list on the Rust production router.
  Success path writes `.backup-retirements/retirements.sqlite3` only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and no directory.
  GC/marker execution is not ported. Not a domain flip.
  Evidence: `cargo test -p deepseek-gateway --test backup_retirement_routes`.
- [x] Serve `GET /api/workspace/backup-runs` as a read-only projection of
  `.backup-scheduler/scheduler.db`. Missing file stays missing. Inventory
  python-only routes 129 → 125 (`backup_governance.py` 65).
  Evidence: `cargo test -p deepseek-gateway --test backup_run_routes`.
- [x] Serve `GET /api/workspace/disaster-recovery/replication` as a read-only
  listing of `.backup-replication/*.json` (limit 100). A missing directory
  stays missing.
  Evidence: `cargo test -p deepseek-gateway --test backup_replication_routes`.
- [x] Serve `GET /api/workspace/disaster-recovery/drills/{restore_id}` as a
  read of `drill-result.json` or `drill-running.json`. A missing session stays
  missing. `POST .../drills/run` is still proxied and still Python-only.
  Inventory python-only routes 125 → 123 (`backup_governance.py` 63).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test backup_drill_routes`.
- [x] Leave `GET /api/workspace/resilience/journal` on Python.
  `list_actions` opens the journal through `_connect`, which creates
  `.resilience-journal`, sets WAL, and migrates the schema.
- [x] Serve `GET /api/automation/templates` from the in-memory builtin catalog.
  The response matches `registry.list_templates()`. `.automation` is not
  created. Inventory python-only routes 123 → 122 (`automation.py` 8).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_template_routes`.
- [x] Serve `GET /api/automation/{automation_id}/runs` as a read of
  `.automation/history.json`. Missing file stays missing. `limit=0` is 100;
  a negative limit returns every loaded run. Inventory python-only routes
  122 → 121 (`automation.py` 7). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_run_routes`.
- [x] Serve `GET /api/automation/{automation_id}` as a read of
  `.automation/automations.json`. Missing file is 404 and stays missing.
  PATCH and DELETE on that path are forwarded to the Go proxy, not
  implemented. The path-level scanner dropped them from the Python-only
  list anyway. Inventory output 121 → 118 (`automation.py` 4). Authority
  stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_definition_routes`.
- [x] Serve `GET /api/media/{media_id}/segments` as a read of `.media`.
  Missing library is 404 and stays missing. Missing segments file is an
  empty list. Other methods on that path are 405. The collection and item
  media routes stay Python. Inventory output 118 → 117 (`media.py` 6).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test media_segment_routes`.
- [x] Serve `GET /api/workspace/resilience/federation` from
  `build_federation_snapshot`. No store is created. A blank `fleetId` is
  `local`. Whitespace-only is 500. Inventory output 117 → 116
  (`backup_governance.py` 62). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test resilience_federation_routes`.
- [x] Serve `GET /api/workspace/artifacts/{artifact_id}/preview` as a read of
  `.projects/<id>/artifacts.json` plus the artifact file. A missing store
  stays missing. An empty `projectId` scans existing `project.json` rows only.
  `GET .../download` stays on the Go proxy. Inventory output 116 → 115
  (`workspace.py` 33). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test artifact_preview_routes`.
- [x] Serve `GET /api/scheduler` as a read of the fresh in-process snapshot
  and, when the file already exists, `.scheduler/scheduler.sqlite3`. A missing
  file stays missing. Inventory output 115 → 114 (`status.py` 3). Authority
  stays `python`. Taking a lease is not this slice.
  Evidence: `cargo test -p deepseek-gateway --test scheduler_routes`.
- [x] Serve `GET /api/edge/status` from `edge_inference_status()` with an empty
  payload. No store is created. Query strings are ignored. Inventory output
  114 → 113 (`status.py` 2). Authority stays `python`. Loading a model is not
  this slice.
  Evidence: `cargo test -p deepseek-gateway --test edge_status_routes`.
- [x] Serve `GET /api/rust/status` from `rust_status()`. A disabled gateway
  does not open a socket. An enabled gateway checks `GET /healthz` and is
  healthy only on HTTP 200. Inventory output 113 → 112 (`status.py` 1).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test rust_status_routes`.
- [ ] Port the remaining 62 `backup_governance.py` routes and the other
  Python-only public routes, without 501 placeholders or a second writer.
  105 Python-only routes remain. `status.py` still has
  `GET /api/semantic-cache/status`, which creates `.semantic-cache`.
  `automation.py` still has only `GET` and `POST /api/automation` (one shared
  path; POST is the full action dispatcher: create, run, rerun, simulate,
  run_due). The six `/api/media` methods are still open. Artifact download is
  still open. Scheduler admission and DLQ writes are still open.
  `GET /api/workspace/home` is **集成通过** and is not a domain flip.
  `GET /api/workspace/projects/{project_id}/provenance` is the next public route.
- [ ] Complete every outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gate. The product remains **未完成**.

## 5.0 active execution — 2026-10-02

- [x] Replace mirror recipients with the exact typed `control/v1` read RPC on the
  existing Go listener; preserve frozen descriptor JSON; pass pinned codegen
  drift/contract checks and 30 contract/mechanical-denial cases.
- [x] Pass four real Rust/Go process tests: policy reads/full CRUD, target health
  and actual encrypted mirror upload/replay/recovery. Wait for the recorded lease,
  advance the successor fence, refuse lost-RPC GET/PUT without filesystem effects.
- [x] Linux Rust check/build/clippy all targets/features, mypy 929 files, frontend
  typecheck/609 tests in 73 files/build/bundle.
- [x] Reproduce corrupt recovery-fence publication, preserve the red regression,
  and fix the swallowed error to retain the 423 refusal.
- [ ] Re-run that regression and whole Rust 80%/Go 95% coverage/race gates after
  the local Docker I/O failure. Interrupted checks are not PASS.
- [ ] Observe current policy CRUD in a real browser; extend mirror body coercion
  parity and implement fenced transfer, handback, restore and write admission.
- [ ] Complete every outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gate in the full matrix. The product remains **未完成**.

## Historical execution — 2026-10-01 (policy CRUD over the process boundary)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Add the **terminal tombstone state** so a policy delete is possible without
  removing a row whose immutable events would orphan it. `TombstoneState` (`DELETED`,
  `go/internal/store/schema.go`) is honoured as absent by `Control.Get` and
  `ListAuthoritativeRecords`, is terminal (no transition leaves it) and keeps the
  document for audit while advancing the store-owned revision. `DELETE
  /api/workspace/backup-policies/{id}` therefore answers the oracle's own
  `{"deleted": true, "policyId": …}`, and a second delete or a patch after the delete is
  the oracle's `404 Backup policy not found`. The `501 GO_POLICY_DELETE_UNSUPPORTED`
  placeholder and its constant are gone; the frontend's delete flow is servable.
  Verified: `go/internal/store/operator_mutation_test.go`
  (`TestTombstoneIsTerminalAndHiddenFromTheInventory`), `go/internal/store/authoritative_list.go`,
  `go/internal/api/backup_policy_writes_test.go` (`TestPolicyDeleteTombstonesTheRecord`) —
  `go test ./internal/store -run 'Tombstone|OperatorMutation|AuthoritativeList'` and
  `go test ./internal/api -run 'Policy|Backup'` pass.
- [x] Drive the whole policy CRUD over the **real process boundary**:
  `TestBackupPolicyRustGatewayToGoPolicyCrud`
  (`go/internal/api/backup_policy_process_integration_test.go`, `native_integration`) starts a
  real `deepseekd` and a real Rust gateway over one isolated Go-authoritative store, and
  asserts create → projected read → merge-and-advance update → tombstoning delete → terminal
  404 for both write verbs → a create refused by name for a foreign revision → Host/token
  refusals on the write verbs. The shared harness (`startNativeProcesses`) now serves the
  read, CRUD and target-health tests, reports both child logs on a transport failure, and
  takes the deployment write opt-in explicitly (`DEEPSEEKD_CONTROL_AUTHORITY` +
  `DEEPSEEKD_INTERNAL_BEARER`) because an operator write needs that opt-in *and* a durably
  Go-authoritative domain.
- [x] Two defects the slice exposed were fixed rather than worked around. (1) The harness
  cancelled its own child processes: `defer cancel()` inside the old single-function helper
  fired when the new split `startNativeProcesses` returned. (2) The Rust edge checked the
  original `Host` only on the **collection** paths, so `PATCH`/`DELETE
  /api/workspace/backup-policies/{id}` skipped it — and the Go-side check cannot catch that,
  because the proxy rewrites `Host` to the Go listener. `backup_inventory_route` now covers
  the item paths too, which is what the oracle's `require_api_auth` does.
- [ ] The Rust half above is **written and formatted, not locally linked**: `cargo test -p
  deepseek-gateway` fails at the link stage in this environment
  (`collect2.exe: error: ld returned 5`, with `multiple definition of __imp_atan` between the
  local mingw's `libntdll.a` and `libmsvcrt.a`), and `cargo build` cannot re-run the
  `deepseek-protocol` build script (`OS Error 5` opening `proto/action/v1/action.proto`).
  Executables whose image lives under `D:\deepseek` are restricted on this machine — one Go
  binary that binds `127.0.0.1` from `D:\deepseek\artifacts` fails with `winapi error
  #10106` while the same bytes run from `%TEMP%` or from `D:\wb-probe-tmp` — so the build
  scripts and the fresh test binary are the blocked part, not the source. Rebuild the gateway
  and re-run the fast path in an unrestricted shell before claiming the Rust half.
- [ ] Observe create/update/delete in a real browser against built Rust/Go processes. The
  harness supports it (`DEEPSEEK_NATIVE_BROWSER_HOLD_SECONDS` holds both processes, disables
  token auth and prints `BROWSER_URL`), the frontend already binds all four verbs
  (`frontend/src/api/workspaceBackupApi.ts`) and `AutomaticBackupsTab.tsx` now reads the three
  inventories with `Promise.allSettled`; the observation itself is outstanding.
- [ ] The mirror slice's remaining gaps: source fence, export/handback document, browser
  observation.

## 5.0 active execution — 2026-10-01 (policy write channel)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Add Go schema v15 and the append-only `control_operator_mutations` journal, as a
  **separate** table rather than a widened `control_operations.result_status` (which is
  `CHECK(... IN ('PROPOSED','APPLIED'))` and means "signed"). Update every historical
  fixture so the fail-closed object catalog still refuses a forged older version.
- [x] Implement `Control.ApplyOperatorMutation`: capability + durably Go-authoritative
  domain + live cutover read inside the write transaction; record, control event and
  journal row in one transaction, stamped with actor, server-generated `actionId`, the
  live `executionEpoch`, the cutover revision/fencing token and the writer fence. Replay
  is `ALREADY_APPLIED`; a reused key with a different body is a replay conflict.
- [x] Serve `POST` and `PATCH /api/workspace/backup-policies` through that channel, with
  the oracle's merge list, the read route's admission, lazy authoritative target-binding
  resolution and the oracle's own refusal messages.
- [x] Hold the unchanged Go 95.0% floor at **95.037888% (7776/8182)**.
- [x] Add the **terminal tombstone state** for policy so `DELETE` can remove a policy:
  removing the row leaves its immutable events orphaned, which the read path reports as
  `CORRUPT_RECORD`, so delete was 501 `GO_POLICY_DELETE_UNSUPPORTED` until a state existed
  that `Get` and `ListAuthoritativeRecords` both honour as absent. **Landed** — see the
  current section at the top of this file; the 501 constant is gone.
- [ ] Observe create/update/delete in a real browser against built Rust/Go processes.
- [ ] The mirror slice's remaining gaps: source fence, export/handback document, browser
  observation.

## 5.0 active execution — 2026-10-01 (policy write semantics)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Port the backup-policy write semantics (`normalize_policy`) to
  `go/internal/policy`, pinned by an 81-case oracle fixture
  (`scripts/generate_policy_normalization_fixture.py` →
  `go/internal/policy/testdata/policy_normalization_v1.json`, `--check` stable).
  Acceptance: every accepted payload normalises to the same document, every refusal
  carries the same message/code/status, the four uncaught `ValueError`s stay 500s, and
  the validation order is pinned boundary by boundary. Two real divergences were found
  and fixed (Python's `a or b` last-operand rule; `parse_cron` wrapping a `ValueError`).
- [x] Hold the unchanged Go 95.0% statement floor after the new package:
  **95.399049% (7423/7781)**, `artifacts/go-coverage-policy.out`;
  `internal/policy` itself is at 98.7%.
- [ ] Settle and implement the **operator mutation channel** — the blocker for every
  policy write. `control_operations.result_status` is constrained to
  `('PROPOSED','APPLIED')`, so a distinct journal needs a schema migration; preferred
  shape is a separate append-only `control_operator_mutations` table so the signed
  journal cannot become ambiguous, with an authenticated public route gated on
  `authorizeCutover` + a durably Go-authoritative domain, the record revision CAS,
  `prepareControlRecordWrite` admission, and a server-generated `actionId` plus the live
  cutover `executionEpoch` in the same transaction.
- [ ] Only after that: `POST`/`PATCH`/`DELETE /api/workspace/backup-policies` on top of
  `internal/policy`, with the browser flow observed end to end.

## 5.0 active execution — 2026-10-01

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Declare `frontend_mirror_store` (`data`, python -> rust, 4.9.4, `rust_data`) and
  port the sealed frontend mirror to `deepseek_policy::backup_mirror`: immutable
  generations, `HEAD.json` CAS, epoch indexes, idempotent replay, stale-epoch/sequence
  and head-conflict refusals, variant selection, corruption detection and the legacy
  4.4.4 read path. Sealing links `backup_crypto` (now lib+bin) rather than copying it.
  Acceptance: two-way byte-level oracle parity (8/8), a legal upload serving a real
  generation through the public routes, and the oracle's refusals on illegal ones.
- [x] Gate the Python mirror writer mechanically at its single write choke point and
  keep reads available, then prove both directions with real callers: the denied case
  leaves no directory behind, the permitted case publishes a verified generation.
- [x] Serve `GET /api/workspace/backup-mirrors`, `GET .../{profile}` and
  `PUT .../{profile}/frontend` from the Rust edge **only** after the store changes hands,
  so the Go proxy is never shadowed and Python keeps owning the path until then.
- [x] Keep the recipient sets in Go: the authenticated loopback route
  `GET /internal/control/backup-policy-recipients` derives the union over all policies
  (with Python's `protection or encryption` fallback) and one group per enabled policy
  (no `encryption` fallback, empty group preserved). An unavailable source refuses
  instead of returning an empty set that would skip the recipient check.
- [x] Hold the unchanged Go 95.0% coverage floor after the new route at
  **95.015755% (6634/6982)** — note the thin margin: two statements in the route are
  unreachable through any legal write path.
- [ ] Add the mirror **source fence** and a canonical export/handback document. Rust
  currently reads the directory Python wrote, so a cutover means "stop the Python
  service first"; there is no fence row, attestation or handback receipt yet.
- [ ] Observe the native mirror routes in a real browser against built Rust/Go
  processes, and prove `backup_scheduled`'s `mirror_files` consumer reads a
  Rust-written generation.
- [ ] Settle and implement the policy **write** channel: the signed v2 apply channel
  needs an external signer, so an authenticated browser CRUD request has no path today.
  The candidate design is an operator mutation channel gated by the durable cutover and
  journalled with `actionId + executionEpoch`, leaving the signed channel unchanged.
- [ ] Complete the repaired full Python gate, pinned Linux Go race and Rust
  coverage checks. Original full Python run: 5533 passed, nine failures,
  95.56% coverage. The current full rerun has no completed result yet.
- [x] Local read/transfer slice: preserve the target-health history in Python's separate
  scheduler SQLite, fence that source table, bind it to an exported target
  inventory and its signed promotion, and import/read it from Go without a
  Python runtime. Preserve the v1 inventory format; use a new v2 manifest for
  the additional scheduler source. Native target GET must refuse missing
  health-transfer evidence rather than fabricate an empty history.
  Acceptance: nonempty and attested-empty history, real old Python writer
  denial, exact retry, tamper/missing fence refusal, Go import atomicity,
  first-promotion source reattestation, restart and reversible handback on
  isolated copies, authenticated Rust→Go HTTP success and browser observation.
  Scheduler mutations, mirror generations and provider effects remain later
  slices; they are not qualified by a read-only health transfer.
- [x] Restore the full Go 95.0% coverage gate after the existing signed-binding
  changes and this slice: **95.009403% (6568/6913)**, profile
  `artifacts/go-target-health-coverage-v14.out`; no threshold or business-code
  exclusions changed. The fresh CGO-free Go daemon passed both policy and
  target Rust→Go process tests. A complete local LLVM MinGW toolchain fixes
  the prior Windows race startup failure; 17 packages passed. The store's 398
  top-level cases are all accounted for: 396 passed and two explicit skips
  (Windows symlink privilege, opt-in frozen-vector generator). Initial package
  timeouts remain recorded; this is partitioned local evidence. A pinned Linux
  run is in progress; exact-head CI race evidence remains open.
- [x] Repair host-capacity-dependent offline placement fixtures after the full
  Python gate exposed nine failures at 91.2% disk usage. Only eight explicit
  filesystem unit scenarios use the isolated capacity fixture; provider probes
  are untouched. The original failover unit now also proves refusal at 95%
  usage. All 109 affected placement/capacity/recovery checks pass; the unchanged
  90% production watermark and 95% coverage floor remain enforced.
- [ ] Complete the repaired full Python gate, pinned Linux Go race and Rust
  coverage checks. Original full Python run: 5533 passed, nine failures,
  95.56% coverage. The current full rerun has no completed result yet.

- [x] Exercise the public backup-policy read in an actual browser against built
  Rust gateway and Go daemon processes on isolated loopback fixtures. The
  signed nonempty policy and next run appear. A frontend `Promise.all` had
  hidden the policy when target and mirror endpoints returned 501; independent
  reads now show the policy and explicitly report those unavailable states.
  This qualifies only the read slice, not full automatic backup behavior.
- [x] Reject an authoritative Go cutover when its persisted signed promotion
  artifact or authorization row is missing or its artifact digest differs.
  Ordinary cutover, authority and public inventory reads fail closed in the
  regression case. Full local Go coverage now passes; exact-head CI remains open.
- [ ] Transfer backup-mirror generations/HEAD and ciphertext with validation,
  unique-writer fencing, crash recovery and rollback; implement native mirror
  reads and mutations. Target-health transfer and target GET are locally
  verified above, but native provider probing/refresh, target CRUD and scheduler
  writes remain open. No fabricated empty health or mirror view qualifies them.

- [x] Reconcile the current `c99d3de6` checkout and preserve the existing
  uncommitted control-plane work; update the matrix's Go control status.
- [ ] Expand the remaining aggregate matrix rows into per-capability entries
  with original entry, observable behavior, owner, dependency, implementation,
  compat case, platform, command, evidence and gap before any final acceptance.
- [x] Make `control-authority-v1` claim and live-head read reachable only through
  the authenticated loopback internal API. Locally verify claim → cutover →
  signed v2 apply → persisted result, exact replay and refusal paths.
- [x] Refuse cross-domain reuse of a control mutation operation ID; regression
  tests first reproduced the false `ALREADY_APPLIED` response in both v1 and v2.
- [x] Re-run the exact Go coverage gate after the new routes and tests without
  changing its 95.0% statement floor: local result 95.127394% (5115/5377),
  with fmt/vet/test passing. Windows `go test -race ./...` exits `0xc0000139`
  before tests execute; Linux CI race evidence remains open.
- [ ] Add an externally signed per-domain promotion artifact and verify
  export/import, unique writer fencing, rollback and restart on isolated data.
  The signed artifact is now integrated locally with a deployment-pinned
  Ed25519 public key, an append-only v10 journal and started-process HTTP
  success/refusal and exact-replay tests.
  This item remains open until independent export/import, fencing, rollback and
  restart evidence qualifies the transfer.
- [x] On isolated Python control stores, export nonempty policy/target records
  against the live authority checkpoint and atomically fence those source
  tables. Prove real Python write denial, concurrent serialization, exact retry,
  unknown-effect refusal and failed output recovery.
- [x] Require the same fenced-source attestation for **empty** policy/target
  inventory before signed Go promotion. Use Python-generated empty-source
  fixtures, bind manifest/source digests and transfer ID, and prove a legal Go
  write followed by later signed authoritative-state transitions.
- [x] Add the Go-owned backup-policy **read** slice behind the native `/api/*`
  proxy: gate the list on an authoritative policy cutover in the same read
  transaction, validate records and histories, authenticate the direct Go
  route, match Python's next-run/DST/jitter oracle, and prove nonempty imported
  data plus an attested empty import followed by a signed Go policy write.
  Local Go HTTP integration and the unchanged 95% statement gate passed; this
  is not the full policy migration or a default production cutover.
- [x] Run built Rust gateway and `deepseekd` executables together. The
  `native_integration` case applies a signed policy mutation to an isolated Go
  SQLite store, closes the seeding process, reopens it in `deepseekd`, reads the
  nonempty result through Rust over TCP, and proves foreign Host and missing
  token refusals. Installer, browser/device, CRUD/run/continuity, scheduler and
  production cutover evidence remain open.
- [x] Bind and recheck legacy `.backup-policies/*.json` projections before
  policy export. The offline exporter refuses unadopted or malformed
  policy/target JSON, then binds the projection directory's exact state
  (`legacyProjection`: file count plus a canonical digest over each file's
  name, size and SHA-256) into the export manifest. The Go attester re-derives
  that digest from the directory — inferred from the standard `.backup-control`
  layout, or named explicitly through `--projection-dir` — and fails closed when
  the directory appeared, vanished or changed. The directory is **bound and
  rechecked, not frozen**, so the Python service must stay stopped. Go's scan
  includes hidden JSON, rejects JSON-named directories, and independently
  validates every projection's JSON and ID against the fenced rows even if
  a caller reseals the manifest digest. Local Go tests and the unchanged full
  95% statement gate pass at 95.008473% (6167/6491); checked-in fixtures prove
  both runtimes agree on the digest byte for byte. Then port public policy
  create/update/delete/run/continuity,
  scheduler claims and recovery with unique Go ownership, and verify the
  public success path through real Rust→Go processes and a browser.
- [x] Reattest the Python source and legacy policy/target JSON projection at the
  first signed promotion. Schema v13 durably binds the exact manifest bytes and
  source/projection locations to each attested import; policy/target drift is
  refused after restart. Upgraded unpromoted v11/v12 imports lack that binding,
  cannot promote, and can be handed back for a fresh export; an older already
  promoted import refuses upgrade. Keep the source service
  stopped: the external projection directory is not mechanically frozen, and
  cross-store atomicity and production transfer remain open.
- [x] Require an explicit absolute projection directory for nonstandard source
  layouts in both the Python exporter and Go attester, including their library
  entry points. The former `digest: null` bypass is refused before a source
  fence or Go import; tests cover refusal, a real explicit empty-directory
  export, and a resealed null-binding manifest. The first promotion still
  needs fresh source/projection proof as tracked above.
- [ ] Import that fenced inventory into the Go-owned store without losing
  revisions or user behavior; verify source and target digests, linked effects,
  unique writer ownership, restart recovery and a reversible handback. The
  explicit offline Go importer now accepts a nonempty Python-exported policy or
  target inventory into a **fresh** dual-evaluate domain, verifies the installed
  checkpoint and hashes, reopens the explicit Python source SQLite read-only
  to verify the exact fence, live rows and pending effects, writes its Go side
  atomically, and survives Go restart on an isolated store. Schema v11 now
  journals import provenance with the rows/events, preserves the Python CAS
  revision in the Go record and first event, and binds the attested source
  digests and source boot epoch to signed promotion, including empty domains.
  This remains open: existing shadow history,
  linked state, cross-store atomicity, complete business/API parity and
  handback are not implemented. Nonzero policy
  `topology_generation` is refused because the checkpoint does not carry it;
  promotion/drain/placement generations remain in the checkpoint but are not
  consumed by native business logic yet.
- [x] Prove the **reverse** transfer on isolated stores. `go
  cmd/control-inventory-handback` refuses a promoted, de-promoted, Go-mutated,
  wrong-transfer or unjournaled domain, removes the imported records, their
  events and the import provenance row in one transaction, appends the
  append-only v12 `control_inventory_handbacks` row, and emits the canonical
  `control-inventory-handback-v1` document.
  `scripts/native_control_handoff.py --rollback` verifies those exact Go bytes
  against the fenced export, refuses a changed source, journals an append-only
  revocation, removes the fence row and its guard triggers atomically, and
  publishes `python-control-inventory-handback-receipt-v1`. A **separate Python
  process** is denied before the handback and writes for real after it; the
  handed-back source can be fenced and exported again for a new transfer.
  The checked-in Go-produced fixture caught a real defect: Go hashed the
  document with an empty `handbackDigest` field present while Python excludes
  it, so the two sides disagreed about the bytes.
  Cross-store atomicity is still not claimed: the two SQLite databases are not
  one transaction, existing Go shadow history and the Python control state
  outside the fenced set are still not transferable, and no production handback
  has been performed.
- [x] Freeze the **linked** control state the transfer binds, not only the
  exported rows. While a fence is held the Python writer is mechanically denied
  on `control_authority_head`, `control_authority_outbox`,
  `control_authority_mutations` and `control_boot_state` (global: any fence
  freezes them) plus the `lifecycle_intents` and `target_receipt_mutations` rows
  that name a fenced domain — 18 `native_control_fence_*` triggers, which the Go
  source attester now requires byte for byte before it reads a row. A Python
  service restarted after the export fails closed with
  `PythonWriterMechanicallyDeniedError` instead of resuming ownership. The
  linked objects survive a partial revocation and are released only when the
  last fence is lifted; the append-only revocation journal remains as the audit
  record. Proven by Python tests through the production control connection, by
  the Go linked-fence denial test, by refreshed cross-language fixtures and by a
  two-step revocation that lifts the whole fence at the end.
- [ ] Prove worker effect reconciliation through real MinIO provider state,
  process kill, lease loss and takeover; retain `EFFECT_UNKNOWN` until proof.
- [ ] Complete remaining native control/data APIs, Rust public edge, desktop,
  Android and server-side TypeScript replacement; measure a successful zero-Python
  workload on each production platform.
- [ ] Obtain exact-head CI, provider and platform artifacts, Evidence Assembly,
  and the final readiness qualification before changing ownership status.

## Phase 0 — Decision and ownership

- [x] Review/accept 5.0 native runtime specification and ADR-0049.
- [x] Resolve fresh-context adversarial findings.
- [x] Add machine-readable ownership contract and negative tests.
- [x] Confirm existing `tasks/plan.md` and `tasks/todo.md` remain unchanged.

## Phase 1 — Protocol foundation

- [x] Pin Go 1.27.1, protoc 36.1, generators/runtimes, and Windows/Linux checksums.
- [x] Add deterministic tool bootstrap/check path.
- [x] Define `common/v1` and `action/v1`.
- [x] Define `storage/v1` and `federation/v1`.
- [x] Define `control/v1`, `evidence/v1`, and `agent/v1`.
- [x] Generate hashed Go bindings, Rust Prost/Tonic types, and a binary descriptor from the proto contract.
- [x] Add descriptor compatibility and generated-code drift gates.
- [x] Bind Go admission/dispatch helpers to generated types and require Rust's descriptor to equal protoc bytes.
- [x] Freeze an immutable v1 semantic baseline and compare it against the complete binary descriptor, including oneof/presence/options/RPC streaming metadata.
- [x] Keep generated Go/Rust code out of business-logic coverage without lowering thresholds.

## Phase 2 — Non-authoritative native processes

- [x] Initialize Go module and `deepseekd` lifecycle/health.
- [x] Prove 4.8.1 Go mutation paths are mechanically absent/denied.
- [x] Add isolated deterministic Go shadow envelopes.
- [x] Add Rust protocol crate.
- [x] Add Rust worker admission/result foundation.
- [x] Prove empty/zero/stale/missing/future action fences are rejected before effects, with epoch advance isolated to the authority path.
- [x] Prove unknown effects cannot be treated as not-applied.
- [x] Add a loopback-only Rust Tonic worker process and a typed Go client that never forwards caller-controlled `live_epoch`.
- [x] Prove with a real Go-to-Rust process test that an authority-uninitialized worker returns exact `FENCE_MISMATCH`.
- [x] Make Go-to-Rust effect queries require an exact returned fence and keep missing or unproven effects fail-closed.

## Phase 3 — Canonical corpus

- [x] Add corpus manifest, schema, sensitivity policy, and provenance.
- [x] Freeze REST inventory (SSE remains in existing gateway fixtures).
- [x] Freeze MCP corpus via existing protocol-preparation fixture.
- [x] Freeze storage wire field inventories.
- [x] Freeze federation and evidence inventories.
- [x] Freeze legal durable state transition labels and fail-closed rules.
- [x] Replay eventual Rust-owned MCP cases through the Python oracle; Rust admits fences locally.
- [x] Replay eventual Go-owned shadow digests and mutation denial in Go tests.
- [x] Produce immutable corpus SHA-256 digests.

## Phase 4 — CI, operations, release

- [x] Add Go fmt/vet/test/race gates.
- [x] Run the real Go-to-Rust admission and effect-query boundary in the `native-go` CI gate with exact worker cleanup.
- [x] Extend Rust workspace to protocol/worker crates.
- [x] Add protocol generation and native contract gates.
- [x] Add native migration/rollback/unknown-effect runbook.
- [x] Run existing frontend/Python/Rust/eval/security/release gates.
- [ ] Run exact-head CI and Evidence Assembly.
- [x] Verify no production owner or frozen contract changed.
- [ ] Qualify 4.8.1 without skips, mocks, or synthetic Evidence.

## 4.8.1 release blockers

- [ ] Any unexplained canonical parity divergence.
- [ ] Any Go production mutation capability.
- [ ] Any unfenced/stale Rust worker effect.
- [ ] Any shared Python/Go/Rust writable durable state.
- [ ] Any change to a frozen contract.
- [ ] Any non-reproducible generated binding.
- [ ] Missing exact-head provider/native Evidence.
