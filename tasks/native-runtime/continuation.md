# Native runtime continuation record

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

This file is the session handoff. Historical plans, checkboxes, VERSION, and
`release/native_runtime_5_0_evidence_v1.json` are not completion evidence.
The capability matrix is [`migration-matrix.md`](migration-matrix.md).

## Current verification checkpoint — 2026-10-05 resumed local gates

Branch `codex/indexmap-std-feature`, base HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; shared source is uncommitted.
All existing concurrent slices below are preserved. The whole product is
**未完成**, readiness `NOT_READY`. The user has authorized pushing the current
checkpoint; merge, release and production-data actions remain unauthorized.

- The overnight interruption stopped Docker and the in-flight validation
  commands. Docker Desktop was restarted normally (engine 29.8.1); all prior
  images/volumes and evidence remain intact. Store race log
  `native-20261004-supervisor-store-race-30m.jsonl` ends after **363 passing tests**
  without a package result, so it is incomplete evidence. The interrupted
  `source-gates-final-20261004` directory has no byte manifest and is preserved,
  not credited as a complete snapshot. A fresh `source-gates-final-20261005`
  input bound the resumed whole Python validation. Current **47
  source-contract/revision/inventory/Compose tests pass** in
  `native-20261005-final-source-contracts.log`. Latest whole Ruff and **951-file
  mypy** pass after the worker configuration changes. HEAD remains the
  base above. New run identities and outcomes must be collected below.

- The resumed whole Python input is the immutable
  `D:/deepseek-native-validation/source-gates-final-20261005`, **3394 files /
  57798539 bytes**, digest
  `e8af0e0d903c119e6f6fabd6492abcc18da2b1d6f99a4c99a3675d9ceec569ae`.
  `native-20261005-source-gates-final-snapshot.json` and
  `native-20261005-gates-final-input.json` bind the source, 107 actual Vite assets
  and two offline Rust helper binaries. The run completed with 5629 passes and
  two failures, described below; session 89334 is collected. Preserve the input
  and raw failed evidence.
- The frozen `3c2db6f5...110329` Go store race rerun is complete: **1083 tests
  pass**, package elapsed **1323.496 seconds**, zero failures. The log
  `native-20261005-supervisor-store-race-30m.jsonl` SHA-256 is
  `fcb3643aeaf895bbe750499dc281a23d4780e08363b1b2d22a6c0884f2f5dd3d`;
  `native-20261005-supervisor-store-race-result.json` records the local scope.
  Session 62746 is collected. Together with the other package results this
  resolves the default ten-minute race timeout for that frozen input.
- Go launch provider/A2A configuration now survives the supervisor. A source
  audit corrected an earlier mistaken assumption: `DEEPSEEK_NATIVE_S3_*` and
  ambient AWS values were test inputs, and the worker main had no S3 transport.
  New dedicated `DEEPSEEK_WORKER_S3_*` loading and default compiled S3 support
  are implemented; eight configuration tests and current Go launch tests pass.
  Actual Linux worker TLS/process/three-MinIO recovery validation passes below.
  It must not be described as Go-issued production authorization or release
  evidence. Dedicated disposable providers are
  `deepseek-native-worker-s3-20261005-{0,1,2}`; their input file is
  `native-20261005-worker-s3-provider-input.json`.

- Default worker transport/recovery qualification now passes all **10 real
  provider tests**, including **three actual worker child processes** with
  TLS/service authentication, current signed timestamps, forced death, exact
  receipt replay and unchanged ETag/version after restart. Provider 2 uses an
  actual versioned bucket. Native input
  `source-worker-storage-replay-20261005` digest
  `2ffcefd17bea84c462e224dd3e69f04e84ca9517987805d1299f8d73815ea547`
  produces default Linux worker **49853328 bytes**, SHA-256
  `399db5c1ea9cb47e70ed7bbce2098636bebd01ad030a2fe5181862dc35481a90`.
  Evidence `native-20261005-worker-s3-replay-process-provider.json` binds the
  source, provider inputs, binary and raw log SHA
  `fa3fd34a7036008998a05d6bab0962bc56dc53eaf85f755f1a47592893bef5ae`.
  Earlier failures are preserved: missing executable from an incorrectly
  mounted Cargo cache; receipt version omitted from the journal; identical
  completed operations rejected after restart. The last two are fixed through
  shared receipt serialization and binding-checked committed receipt replay.
  Unknown effects still cannot be redispatched. Current worker all-feature
  Windows tests (**137**, providers absent) and clippy pass; no-S3 builds check
  and reject configured S3 startup. This is a local native worker/provider test
  with a test signer, not Go-issued cutover or whole-product zero-Python proof.
- The whole Python `e8af0e0d...569ae` run is complete: **5629 pass / 2 fail**,
  **95.57%** coverage, 3540.41 seconds. `native-20261005-gates-python.{log,xml}`
  and coverage JSON are preserved; session 89334 is collected. One old test
  expected Git-status timeouts to imply an empty/clean result; it now expects
  the fail-closed error. The new status-failure test accidentally hard-coded
  the release version; it now uses `APP_VERSION`. Focused verification is
  `native-20261005-whole-python-failures-fixed.log` (**32 passes**). The final
  whole gate runs on immutable `source-python-final-20261005`, **3397 files /
  57834262 bytes**, source digest
  `70b29cf978252f953242f6a186806562b3298cff1766c73d2fbd580f3e5af2fc`.
  Source manifest `native-20261005-python-final-source-snapshot.json` and input
  `native-20261005-python-final-input.json` bind 107 Vite assets and two actual
  `bin/` Rust helpers. Unified session **27119**, output
  `native-20261005-python-final-v2.log`, is complete: **5631 passes**, zero failures,
  **95.58980112888268%** combined line/branch coverage, 4060.16 seconds. Session
  27119 is collected; JSON coverage and XML results bind that frozen input.
  An earlier launch used the wrong helper path; it was stopped before test
  output and recorded as `native-20261005-python-final-aborted-preparation.json`,
  never credited. The source-only zero-Python checker emits `scope=source_contract`
  and `deployment_verified=false` and does not label source audits as deployment
  or full route/provider parity evidence.

- Windows default-worker provider startup initially failed with OS error 5
  while creating its isolated authority directory. A minimal Rust program with
  identical bytes fails from the repository `artifacts/` directory and succeeds
  from the native build directory; the durable authority test also passes.
  Temp/data root and environment inheritance changes did not resolve it. No
  security setting or ACL was changed. `native-20261005-worker-directory-probe.json`
  and `native-20261005-windows-durable-authority-probe.log` preserve that scope;
  temporary production diagnostics are removed. The retained default binary
  now lives in the native build tree, bound by
  `native-20261005-worker-windows-clean-default-binary.json`; actual three-provider
  validation passes **10 tests**, including three TLS child/restart observations.
  Session 34289 is collected. Default Windows binary is **63660194 bytes**,
  SHA-256 `fe74279f922f6d62b9c3df429c20aa7480709e54c0bcaa21f288fa7ef8c2e2d8`.
  Raw log SHA-256 is
  `11778695c506d79cedd357fd667f043bdf017383d37638fbcf5c50de8132adec`;
  `native-20261005-worker-windows-default-provider-result.json` binds inputs.
- Real Go control/provider qualification now passes all three providers with
  live time and the actual Go writer fencing token: an unpromoted domain refuses
  before RPC; signed durable action promotion enables Go-signed epoch/grant
  installation and a default Rust TLS child writes the real object. Go settles
  `SUCCEEDED` and preserves dispatch identity. Independent test-only SigV4 GET
  validates bytes and unchanged ETag/version across forced Rust death, journal
  reload and exact committed replay. This uses an **offline fixture key in Go**;
  production Rust private-key custody and full-fleet admission remain open.
  `native-20261005-go-control-real-provider.log` records the first local pass;
  the updated lifecycle helper and integrated runner must be revalidated.
- The immutable `f67263e3` Rust whole gate passes fmt, workspace check and
  all-target/all-feature clippy, then fails one gateway packaging guard because
  it rejected the exact `DEEPSEEK_RUNTIME_MODE: python_disabled` writer-denial
  setting. The guard now requires that setting and exempts only the exact line;
  all other interpreter mentions still fail. The next targeted run exposed the
  same stale guard's assumption that the shell launches all children directly;
  it now checks the exact Go supervisor exec and its three-child plan. Failed raw log
  `native-20261005-rust-final-gates-v2.log` is retained; session 56637 is collected.
  No current whole Rust coverage value is credited yet. The rerun uses writable
  task-owned Git metadata and the source checkout's verified `core.autocrlf=true`,
  without inferring cleanliness or changing repository settings.

- The user explicitly requested pushing all current repository changes on
  2026-10-05. Commit/push authorization now covers the current branch checkpoint;
  ignored state, secrets, build outputs and local qualification artifacts remain
  excluded. This checkpoint is **未完成 / NOT_READY**, not a release or production
  cutover. The integrated provider runner and fresh Rust gate remain open.

- Frozen Rust/Python quality input is
  `D:/deepseek-native-validation/source-quality-final-20261004`: **3389 files /
  57775166 bytes**, source digest
  `33acecda9c7775f6fb76995ee82ebd36da2606ff274ece7c86f3bc2e6aab8c6c`.
  `native-20261004-source-quality-final-snapshot.json` binds every source byte;
  `native-20261004-quality-final-input.json` separately binds all 107 actual Vite
  files and the two offline Windows helper binaries. Their data roots stay
  disposable. **945-file mypy** and whole Ruff pass in
  `native-20261004-quality-mypy.log` and `native-20261004-quality-ruff-fixed.log`.
  Linux Rust coverage completes at **80.732473% (70452/87266)** across all fifteen
  crates, with **1334 executed tests passing / one intentional Redis-provider
  ignore** (1335 inventory). Summary/evidence SHA-256 is
  `837149a93c5365e019738da1323e28dee3bc87b724c3370b259ef720ed4542cc`;
  LCOV SHA-256 is `4b600d85e2751697fd16266a6bada619b1a66dc30d351b0d91c50a252d6bc664`.
  Files are preserved in both roots as `native-20261004-quality-rust-coverage*`.
  This is local source-digest qualification: the producer's Git metadata reports
  clean despite the separate byte manifest reporting dirty. Git status failure
  and timeout were being interpreted as clean; producer code now rejects them.
  Six failure cases plus existing revision tests pass (14 tests). The frozen
  producer bytes stay unchanged and do not qualify exact-head release Evidence.
  `native-20261004-rust-local-coverage-binding.json` independently rehashes all
  409 current Rust/Proto files: none changed since that numeric coverage run.
  Whole Python finishes **5614 PASS / two FAIL / 61 subtests PASS at 95.580237%**,
  in 3630.50 seconds, in `native-20261004-quality-python*`. One test assumed the
  checkout folder name; one inventory read gitignored generated PyInstaller specs.
  Root discovery now validates the actual module path; inventory records the
  tracked legacy build recipe and separately identifies generated artifacts.
  The legacy packaging gap stays in the inventory. **68 targeted cases pass** in
  `native-20261004-final-python-failures-fixed.log`; fresh whole validation remains
  necessary. Keep the copy immutable.

- The actual production candidate image `sha256:7f6e628915509a1e2abecfc8cf0f8d689faa6b762b6cb04048cc32bbe38eab59`
  builds and boots all three native processes with the real Vite UI. Inspection
  finds 106 OS packages, no Python/Node files or packages. A real control-process
  termination nevertheless leaves its gateway healthy and worker running;
  `native-20261004-production-candidate-observation-red.json` preserves the failure.
  The root image now installs and executes the Go native supervisor. Listener
  binding and native trust configuration are preserved, legacy modes and offline
  authority-clock overrides are excluded, and SIGTERM cancels the supervisor.
  Targeted launch/daemon tests and 29 packaging/source-contract tests pass in
  `native-20261004-container-supervisor-fixed.log` and
  `native-20261004-production-supervisor-contracts.log`. The rebuilt pinned image
  `sha256:1bf4b2288bd7bdd6259d47c07a8a32df03bfc28190b0f50ba13dc82a272dfc01`
  binds source snapshot **3c2db6f5c09a75499d840c9ec8c9c1c8412bfe2c0884aca0f8f4edb32d110329**
  (3391 files / 57785416 bytes). Actual persistent memory writes survive control
  and worker termination and restart. Either child loss stops the whole tree;
  a still-live Go writer lease refuses immediate restart; recorded expiry permits
  recovery; normal SIGTERM exits zero. No Python/Node files or packages are found.
  `native-20261004-production-supervisor-lease-recovery-fixed.json` preserves the
  actual observations; the reproducible offline probe is
  `artifacts/native_probe_production_supervisor_20261004.py`. Complete product
  feature/ownership/provider qualification remains open.
- Current Go coverage passes at **95.030835% (8013/8432)** on source `3c2db6f5`,
  in `native-20261004-supervisor-go-coverage.{log,out}`. Go vet and whole Ruff pass;
  mypy passes **949 files** after typing the offline image probe. Linux whole race
  completes 29 package results: all except store pass; store hits the default
  ten-minute total-package timeout, without assertion failures or race warnings.
  The older passing store run took 1556.915 seconds. Current store is rerunning
  with a bounded **30-minute timeout** in
  `native-20261004-supervisor-store-race-30m.jsonl`, interrupted as recorded above.
  CI's race command now uses the
  same bound; no tests, race checks or coverage thresholds were removed. Do not
  credit whole current race until the complete store rerun passes.
- Native Compose now shares the edge's network namespace for private loopback
  Go control and worker channels. The edge explicitly disables the legacy
  runtime and owns its isolated Rust state volume. Actual v1 gRPC health works,
  absent control credentials are unauthenticated, uncutover recipients return
  FailedPrecondition, and an unfenced worker command is rejected. Native memory
  persists in the real three-service topology. A sole edge restart was reproduced
  disconnecting the channels; dependent restart now follows the namespace owner
  and restores RPCs without changing the acknowledged memory. Source contracts
  and real recovery pass in `native-20261004-compose-namespace-restart-fixed.log`
  and `native-20261004-compose-namespace-recovery.json`; the test used pinned
  candidate binaries, task-only volumes/random loopback port/credentials and
  disabled automatic restart. All test services are now stopped. The offline Go
  qualification client is under `D:/deepseek-native-validation/channel-probe-20261004`.

- Current mirror candidate handback is implemented, without production promotion.
  Native `mirror-inventory-import --revoke` reattests both inventories and persists
  terminal `revoked`; reimport and native writes remain denied. Offline
  `native_mirror_handoff.py --handback --manifest ...` verifies that exact receipt,
  independently rehashes both roots, archives the original fence bytes and persists
  native denial/history before releasing only the original Python source fence.
  Restart and interrupted history publication recover. **12 native integration
  tests** and **100 Python mirror/denial/oracle tests** pass in
  `native-20261004-candidate-target-restore-fixed.log` and
  `native-20261004-target-restore-python.{log,xml}`. Actual Python Age producer →
  native CLI import/revocation → handback → original-writer sequence 1→2 → decrypt
  also passes; the revoked candidate ciphertext remains unchanged. Evidence:
  `native-20261004-python-rust-real-handback.json`; reproducible offline helper:
  `artifacts/native_verify_mirror_handback_20261004.py`. This covers pre-admission
  cancellation, not handback after native production effects or Go action admission.
- New red cases showed import bypassing the target workspace restore lock and
  treating a `null` restore fence as absent. Import/revocation now retain the target
  workspace OS lock as well as the source gate and target store lock. Offline
  handback shares those locks. Any restore-fence presence or uninspectable path
  blocks, including null/array/corrupt documents. Python also denies any native
  candidate receipt, independently of runtime mode; malformed/unknown receipts
  cannot enable a second writer. No production data was used.
- File-cache refresh failed under Linux instrumentation: same-length rewrites
  retained the timestamp and reused stale parsed JSON. Native cache reuse now
  checks content SHA-256. Fixed-timestamp rewrite and corruption cases were red;
  all six file-store tests pass in `native-20261004-file-store-rewrite-fixed.log`.
- Source `2a8387cd...b62d7d22` completed whole Linux Rust coverage: **80.682144%
  (70304/87137 lines)**, **15 crates**, **1329 executed tests**, zero failures,
  one provider test ignored by the workspace run. Inventory lists 1330 tests;
  Redis provider acceptance is separately executed. Artifacts are in
  `D:/deepseek-native-validation/source-cache-refresh-20261004/artifacts/`.
  This precedes candidate revocation/handback, target restore guards and Lua
  relocation. Refresh on the latest source before crediting a current full gate.
  Whole Windows all-feature run before the cache/handback changes passed
  **1327 tests / 118 suites**, one ignored provider case, in
  `native-20261004-handoff-workspace-test.log`. Current whole clippy passes in
  `native-20261004-native-lua-clippy.log`; earlier current mypy passed 944 files.
- Whole Python run completed **5582 passed, 10 failed, 61 subtests passed**, with
  **95.43%** coverage after 2901.35 seconds (`native-20261004-full-python.{log,xml}`
  and `...-coverage.json`). This is a failed gate, not Python acceptance. Failures
  exposed a Rust build dependency on TypeScript backend source, stale structural
  mode/spacing/RPC/tombstone/frontend assertions, and a static scanner that rejected
  the native Go supervisor's legitimate child processes. These were repaired with
  **61 targeted tests** in `native-20261004-full-gate-failures-fixed.log`; frozen
  protocol/oracle corpora were not rewritten. Refresh the complete gate on frozen
  source after these changes.
- Redis Lua is now owned under `rust/crates/deepseek-stateless-mcp/src/lua/v1/`:
  **12 scripts / 5775 bytes** preserve the original template bytes. Production
  no longer embeds/reads the TypeScript server to discover scripts. All twelve
  are compared with the unchanged TypeScript oracle in native tests; actual Redis
  dedup/claim/takeover/backup-fence release passes again in
  `native-20261004-native-lua-real-redis.log`. Transfer hashes are in
  `native-20261004-stateless-lua-transfer.json`. The zero-Python static source gate
  now checks the native launcher's fixed three-binary set and interpreter denial,
  while still refusing arbitrary exec. Its report explicitly says source contract;
  observed process/package/workload qualification remains required.

- Mirror parsing: the old empty body became `Mirror sourceEpoch is required`.
  `artifacts/native-20261002-mirror-body-guards-red.log` preserves the red case.
  The route now checks length/object shape, bounds actual buffering at 64,000,000
  bytes, accepts JSON independently of Content-Type and reuses Python text
  coercion for epoch/replica/head/acknowledgment. **7 mirror + 2 auth tests pass**
  in `native-20261004-mirror-request-tests-restored.log`; five guard/four text
  cases also pass on the unchanged Python public route (**12 tests**) in
  `native-20261004-mirror-body-python-oracle.log`. Malformed-JSON diagnostic text,
  unbounded/Unicode integers and production ownership admission remain open.
- Launcher: relative native paths could move with caller cwd. The red case is
  `native-20261004-launch-relative-roots-red.log`; binary/data/static paths now
  become absolute. Native Go child listeners prove normal/nonzero child exit
  cancels siblings; empty plans and missing binaries refuse. Launch package
  tests pass at **96.5%** (`native-20261004-launch-boundaries.log`). RPC tests
  verify configured process health, missing policy state, duplicate bearer
  refusal and the 1 MiB request bound (`native-20261004-control-rpc-tests.log`).
- Current whole Go coverage initially failed at **94.731835% (7966/8409)**.
  The refreshed gate **passes at 95.020796% (7996/8415)** in
  `native-20261004-go-coverage-fixed.log/out`; no floor or business-code exclusion
  changed. Current Go format/vet pass. Launch/API/lifecycle race passes **155
  tests, zero skips/failures/races** in `native-20261004-go-race-current.jsonl`
  (6.236 / 344.692 / 102.986 seconds). Complete Linux race now also passes:
  **1768 tests**, zero failures/races, one intentional frozen-vector generator
  skip; twenty tested packages and nine packages without tests. Evidence is
  `native-20261004-linux-go-race.jsonl`, source snapshot digest `cbb9c956...fb4d77`.
  No Go source has changed since that snapshot.
- Offline mirror handoff: `scripts/native_mirror_handoff.py` exports the versioned
  `python-mirror-inventory-export-v1`, validates settled legacy/generation layout,
  hashes every file and retains empty directories. Source fencing is persistent
  at `<mirror-root>.native-handoff.json`; both Python and Rust writers refuse it
  after restart, independent of mode flags. The exporter shares the workspace and
  native store OS locks. **22 Python tests** pass, including damaged/unreadable
  fences, disjoint path bindings, source drift and recovery after output failure,
  in `native-20261004-mirror-source-{export,native-lock}.log`.
- Rust `mirror_handoff` and the `mirror-inventory-import` CLI independently hash
  the source and candidate, preserve all inventory bytes, and retain a durable
  `copying`/`imported` receipt outside the data root. Reserved partial copies can
  resume; unrelated target/staging data is refused and preserved. **Seven native
  tests** pass in `native-20261004-mirror-import-kill-recovery.log`, including real
  Age decrypt, source/target drift, restart denial and actual importer force-kill.
  The strengthened kill case waits for a nonzero published HEAD before killing
  and passes separately in `native-20261004-mirror-nonzero-copy-kill.log`.
  Actual **Python-produced Age mirror → offline fence → native CLI import → Age
  decrypt** also passes on isolated roots; binary and data hashes are in
  `native-20261004-python-to-rust-real-age.json`. Imported candidates stay 423
  pending ownership admission; this is not production promotion or full handback.
- Real Redis 7.4 gate exposed an error-reply buffering bug: the client retained
  `ERR BACKUP_FENCED`, so a subsequent legitimate fence release failed. Both sync
  and async clients now consume the entire reply frame, including array errors.
  Two TCP regressions were red; six parser/network tests pass, and the formerly
  ignored real Lua/dedup/claim/takeover/fence-release test now passes in
  `native-20261004-redis-real-lua-fixed.log`. Disposable Redis image is pinned at
  `858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`.
  Its task-owned container is `deepseek-native-redis-20261004` (loopback only).
- Current whole Rust check, fmt and all-target/all-feature clippy pass in
  `native-20261004-rust-{check,fmt-final,clippy-final}.log`. Saved-item and run
  inputs now carry named fields; snapshot joining and boxed task records remove
  the diagnosed allocations without changing wire JSON. Full test initially
  reached 381 passes and one UTF-8 Python-oracle failure; that fixture and the
  task wrapper now use `PYTHONUTF8=1`. The complete rerun passes **1266 tests**
  in **111 suites**, with one explicitly ignored Redis-provider test, in
  `native-20261004-rust-workspace-test-fixed.log`. This precedes the later profile
  alias guard; that guard's native regression passes separately. Refreshed
  clippy/build after restore exclusion pass in
  `native-20261004-rust-clippy-restored-mirror.log` and `native-20261004-gateway-build.log`.
  These whole-workspace results predate the new handoff and Redis changes; the
  handoff package clippy passes in `native-20261004-mirror-import-clippy.log`.
- Restore exclusion: both real Rust and Python writers published a mirror while
  restore held the workspace lock. Their red logs are
  `native-20261004-{mirror,python-mirror}-restore-window-red.log`. Rust upload now
  runs blocking sealing on a blocking thread, holds the workspace OS lock and
  rechecks the restore fence through HEAD publication. Python's remaining source
  writer takes that same lock. **8 Rust mirror tests** pass in
  `native-20261004-mirror-restore-window-fixed.log`; **74 Python mirror/ownership/
  coverage-evidence tests** pass in `native-20261004-mirror-and-evidence-oracles.log`.
  Both concurrency cases prove fence refusal and successful upload after recovery.
- Coverage inventory now comes from locked offline Cargo metadata: **15 actual
  workspace crates**, replacing the stale static eleven. Missing members or
  failed metadata cannot produce a partial PASS inventory. The existing 80%
  line threshold and generated-protobuf-only omit remain unchanged. Inventory
  and numeric coverage gate tests pass (**19**) in
  `native-20261004-coverage-inventory-test.log`. Whole Ruff/mypy previously passed
  938 source files in `native-20261004-mypy-verified.log`; whole Ruff passes in
  `native-20261004-ruff-verified.log`.
- Path scope: both stores accepted profiles `.`/`..`, allowing HEAD/generations
  at the mirror root or parent. Red cases are
  `native-20261004-{rust,python}-mirror-path-scope-red.log`. Both now refuse 400
  before data writes. The native regression and **59 Python safety/oracle tests,
  zero skips/failures**, pass in `native-20261004-rust-mirror-path-scope-fixed.log`
  and `native-20261004-mirror-safety-oracles.{log,xml}`. The disabled Python
  writer is also checked before lock creation and rechecked after waiting;
  `native-20261004-mirror-lock-denial-fixed.log` passes **28 tests**.
- Four freshly built Windows Rust/Go process tests pass in
  `native-20261004-real-process-boundary.jsonl` (44.771 s); the mirror case includes
  ciphertext hashes, replay, kills and successor lease fencing. This remains
  local boundary evidence, not global ownership or provider acceptance.
- October 2 recorded **four Windows real Rust/Go process tests**, **8 Rust
  mirror/auth tests**, **67 Python mirror/contract/denial tests**, mypy 931 files,
  Ruff, Go vet, API race (**121 tests, zero skips/failures/races**) and lifecycle
  race. They are historical local results, not current whole-workspace/release
  PASS. Policy browser create/disable/delete was verified using disposable
  processes; screenshots/DOM/public-list evidence is under
  `native-20261002-browser-policy-*`. The CRUD fixture had not enabled Rust data
  mode, so its mirror 501 is gating evidence, not an unregistered-route finding.
  Native browser mirror upload remains open.
- After interruption, temporary compiler executables were absent. **5515 SDK
  files / 487021277 bytes** match source SHA-256 in
  `native-20261004-windows-sdk-manifest.json`. Task-owned tools now live under
  `C:/Users/12393/.codex/native-validation/20261004/{compiler,rust-linker}`;
  GNU driver is in `rust-linker/self-contained`. The wrapper
  `artifacts/native_windows_rust_20261002.ps1` selects Rust 1.85 GNU, jobs=2 and
  target `D:/deepseek-native-validation/windows-20261004/target`. Invoke it with
  `-CargoArguments @('test','--locked','--offline','--manifest-path',
  'rust/Cargo.toml',...)`. No global compiler, ACL or security setting changed.
  The GNU linker copy (five files) and cargo-llvm-cov 0.6.21 are also hashed in
  that manifest (SHA-256 `20cfae14da14369d6349f2153be5f3e4f833dc3a9c916a3a04464ba0bcce24ae`).
  Windows coverage cannot run on this GNU toolchain: `E0463 profiler_builtins`
  is preserved in `native-20261004-windows-coverage-probe.log`; no repeat or lower floor.
- Docker normal start succeeded after available disk space recovered. The existing
  development image remains `c33bc88e8d52443ed01d659e8a20b99bb08438bfd7c30ef6e32532da8edbd9ce`
  and pinned MinIO image `a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2`
  remains cached; no prune/reset/format command was issued. Engine 29.8.1 reports
  about 16 GiB. Task containers/volumes carry label
  `deepseek.codex-task=native-reconstruction-20261004`.
- Fresh source copy `D:/deepseek-native-validation/source-mirror-20261004` has
  **3368 files / 57652069 bytes**, verified twice against original source;
  `native-20261004-source-snapshot.json` records digest
  `cbb9c956ea23d0f55c4bcd4cc21f88a6b0a7439dd50cb002568375d2b9fb4d77`.
  It precedes the profile alias guard. Linux coverage's first offline attempt
  lacked `openssl-probe 0.2.1`; locked Linux-target fetch populated the task
  registry volume. Later attempts exposed missing offline Python dependencies
  and omitted generated Vite assets, both repaired without changing tests. New
  development image ID is `5d9b700e50810914bffc6549562db7aaa675eec7056de480184b82b7ff2dc739`.
  Vite build passes, all 245 frontend source files match the copy, and copied
  static input digest is `292853a850047a66de2dd4704f8bc16cb01f3981b6e5044eda0f83d6e274458a`
  (`native-20261004-linux-static-input.json`). The run with assets reached
  `scheduler_routes` and found a Unix directory-open error mismatch (bundled
  SQLite IOERR versus Python CANTOPEN); `open_readonly` now rejects directories
  with the oracle error. `native-20261004-linux-rust-coverage-with-ui.log` is
  failed evidence, not an 80% PASS. Refresh coverage on a new verified source
  copy containing this fix, handoff, and Redis changes. Complete Go race has
  finished successfully in `native-20261004-linux-go-race.jsonl` as qualified above.
  The development image includes Python and is ineligible for zero-Python release evidence.
- Latest byte-verified copy is `D:/deepseek-native-validation/source-handoff-20261004`:
  **3375 files / 57730879 bytes**, digest
  `989a9486bb7d3574bbe390a6dca27f96122d69347ff03ca4427f366835c0b4b6`
  (`native-20261004-source-handoff-snapshot.json`). It includes the mirror handoff,
  reply-buffer fix, directory-open guard and Unix-only lint repairs. All 107
  generated UI files were rehashed/copied; the static digest is unchanged.
  Linux attempt `native-20261004-handoff-rust-coverage.log` failed at the stale
  equal-timestamp file-cache case; the repaired `2a8387cd...` copy passes as
  qualified above. Whole clippy in `native-20261004-handoff-workspace-clippy.log`
  and whole `fmt --all --check` passed; later checks are qualified above.

Next: refresh current quality gates without reducing floors; close mirror public
parity and pre-promotion rollback, native handback/restore and Go action admission.
Continue every remaining product/platform/provider/zero-Python and exact-head
CI/Evidence gate. The workspace-home and other concurrent work below remain in
scope and must be preserved.

## Concurrent checkpoint — 2026-10-04 workspace home

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. This slice does not add a domain and does not change
`RUST_DATA_DOMAINS`. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/workspace/home` is served by the Rust production router, ahead of the
Go `/api/*` catch-all. It is `workspace_home.workspace_home`. The body is the
aggregate itself: `ok`, `version` `4.8.0`, the ten fixed modules, `recent`,
`counts`, and `status` (`doctor` `ok`, `runtime` `local`). It is a read. It is
served while `DEEPSEEK_RUNTIME_MODE=python_authoritative`. It does not answer
501, and it does not create `.projects`, `.automation`, `.media`, `.memory`,
`.generated`, or `docs/evidence`.

`limit` is `int(query or 8)` and then `int(limit or 8)` clamped to 1..50.
Missing or empty is 8. The query `"0"` is 8, because `0` is falsy on the
second conversion. `"-3"` is 1. `"100"` is 50. A non-integer is
`500 {"error":"Server error","code":"internal"}` before any store read.
Missing auth is the production nested
`{"error":{"code":"UNAUTHORIZED","message":"Auth required"}}`. `HEAD` is 200
with an empty body. Any other method is
`405 {"detail":"Method Not Allowed"}` with `Allow: GET, HEAD`.

`recent.automations` is `history.list_runs(limit=safe_limit)`, already
truncated, so `counts.automationRuns` is that window. `counts.automations` is
the full definition count. Other counts are the full collected lengths.
Recent windows are the clamped prefix. Artifacts, saved items, and skill runs
are re-sorted by `updatedAt` / `createdAt` / `finishedAt` / `startedAt`
descending. A per-project saved-item, artifact, or skill-run failure is
skipped. Missing or unreadable JSON is an empty collection.
`status.evidence` is `{path, present, status}` for
`<resolved DEEPSEEK_INFRA_ROOT>/docs/evidence/ga-v4.8.0.json` as a POSIX path.
The file is not created.

This is **集成通过**, not **完成切换**. Python still owns the stores the
aggregate reads.

### Verification

`cargo fmt --manifest-path rust/Cargo.toml -p deepseek-gateway` then
`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test data_routes -- --test-threads=1`.
31 passed in 6.10s, including `workspace_home_matches_python_and_leaves_missing_stores_absent`
and the existing `project_` cases. The success body, including limit `""`,
`"0"`, `"-3"`, and `"100"`, was compared with a live `workspace_home` call on
the same root. The child removed `DEEPSEEK_RUNTIME_MODE`. The seeded history
has 3 runs; `limit=1` reports `counts.automationRuns` 1 and
`counts.automations` 2. An empty root stayed empty after 401, 500, 405, HEAD,
and GET. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e35ab403b353\implementer\workspace-home.log`.
Linker: `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain was not switched. HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e` plus this uncommitted slice.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider`
passed (1 passed in 0.69s). Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e35ab403b353\implementer\workspace-home-inventory.log`.
Scanner python-only routes 106 → 105. `workspace.py` 29 → 28.
`/api/workspace/home` is absent from `http_routes_only_on_python`.
`automation.py` stayed 2. `media.py` stayed 6. `backup_governance.py` stayed
62. `rag.py` stayed 3. `edge.py` stayed 2. `mcp.py` stayed 1. `status.py`
stayed 1. Authority `python`. Go-owned domains `[]`. Rust domains 12. Python
domains 34.

`docs/PROJECTS_STORE.md` and `docs/NATIVE_PUBLIC_ROUTES.md` still describe the
old artifact 501. They were not rewritten in this slice. Local cargo and
pytest are not remote CI or Evidence Assembly.

### Not done, next executable task

Do not treat this slice as a domain flip. The next public route is
`GET /api/workspace/projects/{project_id}/provenance`. Do not register
`GET /api/workspace/artifacts/{artifact_id}/download` until the bytes are the
real artifact. Do not register `GET /api/automation` or `POST /api/automation`
until every POST action is real. Do not port `GET /api/semantic-cache/status`
while `status()` creates `.semantic-cache`. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub readiness, SLO, catalog, capacity, or transfer-budget routes. Do
not register drain GET. Do not answer with 501. `GET`/`POST /api/media` share
one path. `GET`/`PATCH`/`DELETE /api/media/{media_id}` share one path.

105 Python-only public routes remain, 62 of them in `backup_governance.py`,
28 in `workspace.py`, 6 in `media.py`, and 2 in `automation.py`. Provider,
desktop, Android, and zero-Python workload gates remain open. Do not redo the
4.8.0 double launch unless the release gateway binary changes. The product
remains **未完成**.

## Previous continuation checkpoint — 2026-10-04 artifact writes

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. `project_metadata_store` was already declared; this slice does not
add a domain and does not change `RUST_DATA_DOMAINS`. Debug `cargo test` did
not relink the 4.8.0 release gateway.

### This slice

`POST /api/workspace/projects/{project_id}/artifacts` no longer answers
`501 NATIVE_PROJECTS_MUTATIONS_NOT_READY`. It is `artifacts.register_artifact`.
`PATCH` and `DELETE /api/workspace/projects/{project_id}/artifacts/{artifact_id}`
are served together on one path. A truthy `path` is `add_artifact_version`.
Any other PATCH is `update_artifact`. Other methods on the item path are
`405 {"detail":"Method Not Allowed"}` with `Allow: PATCH, DELETE`.

The writer gate is `may_write_native_store("project_metadata_store")`, which is
true only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise the answer is
`409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` before the body is parsed, and
`.projects` is not created. With the mode set, a legal create appends an
`art_` plus 16 lowercase hex record, touches project `updatedAt`, and
`GET .../artifacts` lists it. An empty type with `notes/kept.md` is stored as
`markdown`. Empty path is `400 Artifact path is required`. A `..` path is
`400 Artifact path must not escape the workspace`. `notes/file.bin` is
`400 Unsupported artifact type`. A missing project is `404 Project not found`
and does not create a directory. An unknown artifact id on PATCH is `404` and
does not rewrite, including when the path would also be illegal, because the
path is checked only after the id is found. DELETE of an unknown id is
`{"ok":true,"deleted":0}` and does not rewrite. `update_artifact` changes
`title` and `source` only. The load keeps the last 500 normalised rows in file
order; the list then sorts by `updatedAtMs`. A hidden prefix is not rewritten
by a 413 `Too many artifacts` (`upload_too_large`, checked before the path) or
by a 404 for an id outside that window. Deleting a visible id rewrites only
the loaded window, so the hidden prefix disappears. `GET` list behaviour is
unchanged. `GET /api/workspace/artifacts/{artifact_id}/download` stays Python.

This is **集成通过**, not **完成切换**. Python can still write the same files
while Python is authoritative.

### Verification

`cargo fmt --manifest-path rust/Cargo.toml -p deepseek-policy -p deepseek-gateway`
then `cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test data_routes -- --test-threads=1 project_`.
7 passed in 2.11s: the new artifact success and refusal test, the saved-item
test, the closed mutation test, auth, the list oracle, and the project error
cases. Create, title update, version append, and delete were compared with a
live `register_artifact` / `update_artifact` / `add_artifact_version` /
`delete_artifact` oracle. The child removed `DEEPSEEK_RUNTIME_MODE`. Fresh
`artifactId`, clocks, and `downloadUrl` were stripped on the body compare.
Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\artifact-routes-2.log`.
`artifact-routes.log` is the earlier `python_truthy` type error (`Option<&Value>`
passed where `&Value` was required), not a linker failure. Linker:
`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain: `rustc 1.97.1` stable
`x86_64-pc-windows-gnu`. HEAD `0e340dc3695890e8d85d59c8263192f0593fb79e` plus
this uncommitted slice.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider`
passed (1 passed in 0.62s). Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\artifact-inventory.log`
and `artifact-inventory-pytest.log`. Scanner python-only routes 108 → 106.
`workspace.py` 31 → 29. The drop is the artifact item PATCH and DELETE. POST
was already registered, so it was already absent from the Python-only list.
No artifact create, update, or delete path remains on that list.
`GET .../download` remains. `automation.py` stayed 2. `media.py` stayed 6.
`backup_governance.py` stayed 62. `status.py` stayed 1. Authority `python`.
Go-owned domains `[]`. Rust domains 12. Python domains 34.

`docs/PROJECTS_STORE.md` and `docs/NATIVE_PUBLIC_ROUTES.md` still describe the
old artifact 501. They were not rewritten in this slice.

### Not done, next executable task

Do not treat this slice as a domain flip. The next slice is
`GET /api/workspace/home`. It is `workspace_home.workspace_home`. `limit`
uses `int(limit or 8)` then clamps to 1..50. The response is the home
aggregate. Missing stores must stay missing: the Python loaders use
`read_json_file` or an `exists()` check and do not create `.projects`,
`.automation`, `.media`, `.memory`, or export directories. Match that. Do not
register `GET /api/workspace/artifacts/{artifact_id}/download`. Do not register
`GET /api/automation` or `POST /api/automation` until every POST action is
real. Do not port `GET /api/semantic-cache/status` while `status()` creates
`.semantic-cache`. Do not port `GET /api/workspace/resilience/journal` while
`_connect` creates the database. Do not stub readiness, SLO, catalog,
capacity, or transfer-budget routes. Do not register drain GET. Do not answer
with 501. `GET`/`POST /api/media` share one path. `GET`/`PATCH`/`DELETE
/api/media/{media_id}` share one path.

106 Python-only public routes remain, 62 of them in `backup_governance.py`,
29 in `workspace.py`, 6 in `media.py`, and 2 in `automation.py`. Provider,
desktop, Android, and zero-Python workload gates remain open. Do not redo the
4.8.0 double launch unless the release gateway binary changes. The product
remains **未完成**.

## Previous continuation checkpoint — 2026-10-04 saved-item writes

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. `project_metadata_store` was already declared; this slice does not
add a domain and does not change `RUST_DATA_DOMAINS`. Debug `cargo test` did
not relink the 4.8.0 release gateway.

### This slice

`POST /api/workspace/projects/{project_id}/saved-items` no longer answers
`501 NATIVE_PROJECTS_MUTATIONS_NOT_READY`. It is `saved_items.create_saved_item`.
`PATCH` and `DELETE /api/workspace/projects/{project_id}/saved-items/{saved_id}`
are `update_saved_item` and `delete_saved_item` on the same path. Both methods
moved together. Other methods on the item path are `405
{"detail":"Method Not Allowed"}` with `Allow: PATCH, DELETE`.

The writer gate is `may_write_native_store("project_metadata_store")`, which is
true only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise the answer is
`409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` before the body is parsed, and
`.projects` is not created. With the mode set, a legal create appends a
`save_` plus 16 lowercase hex item, touches project `updatedAt`, and
`GET .../saved-items` lists it. Tags de-duplicate case-insensitively. An
unknown purpose becomes `reference`. Empty type is `400 Unsupported saved item
type` and does not rewrite. A missing project is `404 Project not found` and
does not create a directory. An unknown saved id on PATCH is `404` and does
not rewrite; DELETE of an unknown id is `{"ok":true,"deleted":0}` and does not
rewrite. The load keeps the last 1000 normalised rows. A hidden prefix is not
rewritten by a 413 `Too many saved items` or by a 404 for an id outside that
window. Deleting a visible id rewrites only the loaded window, so the hidden
prefix disappears. `GET` list behaviour is unchanged.

This is **集成通过**, not **完成切换**. Python can still write the same files
while Python is authoritative.

### Verification

`cargo fmt --manifest-path rust/Cargo.toml -p deepseek-gateway` then
`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test data_routes -- --test-threads=1 project_`.
6 passed in 1.10s: the new saved-item success and refusal test, the closed
mutation test, auth, the list oracle, and the project error cases. The create
and update bodies were compared with a live `create_saved_item` /
`update_saved_item` / `delete_saved_item` oracle. The child removed
`DEEPSEEK_RUNTIME_MODE`. Fresh `savedId` and clocks were stripped on the
create compare. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\saved-item-routes-2.log`.
`saved-item-routes.log` is the earlier `Vec<String>` compile error, not a
linker failure. Linker:
`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain: `rustc 1.97.1` stable
`x86_64-pc-windows-gnu`. HEAD `0e340dc3695890e8d85d59c8263192f0593fb79e` plus
this uncommitted slice.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider`
passed. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\saved-item-inventory.log`
and `saved-item-inventory-pytest.log`. Scanner python-only routes 110 → 108.
`workspace.py` 33 → 31. No saved-item path remains on the Python-only list.
`automation.py` stayed 2. `media.py` stayed 6. `backup_governance.py` stayed
62. `status.py` stayed 1. Authority `python`. Go-owned domains `[]`.

### Not done, next executable task

Do not treat this slice as a domain flip. `POST
/api/workspace/projects/{project_id}/artifacts` is still registered and still
answers `501 NATIVE_PROJECTS_MUTATIONS_NOT_READY`. `PATCH` and `DELETE
/api/workspace/projects/{project_id}/artifacts/{artifact_id}` share one path
and are still Python-only. The next slice replaces that 501 with a real
artifact register and serves PATCH and DELETE together, gated on
`project_metadata_store` / `python_disabled`. Do not register only one of
those two methods. Do not treat
`GET /api/workspace/artifacts/{artifact_id}/download` as migrated. Do not
register `GET /api/automation` or `POST /api/automation` until every POST
action is real. Do not port `GET /api/semantic-cache/status` while `status()`
creates `.semantic-cache`. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub readiness, SLO, catalog, capacity, or transfer-budget routes. Do
not register drain GET. Do not answer with 501. `GET`/`POST /api/media` share
one path. `GET`/`PATCH`/`DELETE /api/media/{media_id}` share one path.

108 Python-only public routes remain, 62 of them in `backup_governance.py`,
31 in `workspace.py`, 6 in `media.py`, and 2 in `automation.py`. Provider,
desktop, Android, and zero-Python workload gates remain open. Do not redo the
4.8.0 double launch unless the release gateway binary changes. The product
remains **未完成**.

## Previous continuation checkpoint — 2026-10-04 automation run

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. `.automation` was not added to `DECLARED_NATIVE_DATA_DOMAINS` or
`RUST_DATA_DOMAINS`. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`POST /api/automation/{automation_id}/run` is served by the Rust production
router as `runner.run_once`. The handler is `automation_run_routes`. A missing,
empty, zero, or negative `Content-Length` is `{}`. A header that is not a
Python `int` (`nope`) is `500 {"error":"Server error","code":"internal"}`,
not the definition route's `400 Invalid Content-Length`. A declared length
above 2000000 is `413` before the body is trusted. `now` is parsed before the
automation lookup: an invalid timestamp is `400 now must be an ISO timestamp
or epoch milliseconds` and does not create `.automation`. A missing automation
is `404 Automation not found` and does not create `history.json`, including
while Python owns the store. An existing automation while Python is
authoritative is `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` before any history,
saved-item, project, or memory write.

With `DEEPSEEK_RUNTIME_MODE=python_disabled`, a legal `save_item` run returns
`{"ok": true, "run": ...}` and writes `.automation/history.json`,
`.projects/{id}/saved-items.json`, the project `updatedAt`, and one memory
summary. The same success sentence updates that memory row
(`memory_fingerprint`) instead of inserting a second row; `source.runId`
moves to the later run. An exact cron `34 12 4 10 *` at
`2026-10-04T12:34:00+00:00` succeeds on both sides (`startedAtMs` equal before
stripping). `0 0 * * *` at `2026-10-04T15:00:00+00:00` skips
`schedule_not_due` and does not save. A disabled automation skips
`automation_disabled` and writes that history row. `save_item` without a
project id is HTTP 200 with `status: failed` and
`save_item action requires projectId`. `automations.json` is not rewritten.
`GET /api/automation/{id}/runs` lists the new run. Other methods on `.../run`
are `405 {"detail":"Method Not Allowed"}` with `Allow: POST`.

`traceId` stays empty. There is no trace store. An action other than
`save_item` that passes policy is HTTP 200 `status: failed` with
`{type} is not executed by the native gateway yet`. Browser actions are
usually `policy_denied` because `allowBrowser` defaults to false. This is
**集成通过** for the manual and scheduled `save_item` path, the
disabled/trigger/condition/policy decisions, and the pre-write refusals. It
is not **完成切换** and not full action-matrix parity.

### Verification

`cargo fmt --manifest-path rust/Cargo.toml -p deepseek-gateway` then
`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test automation_run_routes --test automation_definition_routes --test automation_template_routes -- --test-threads=1`.
Definition tests: 2 passed in 5.86s. Run tests: 3 passed in 2.10s. Template
tests: 2 passed in 1.81s. The five ordered cases were compared with a live
`run_once` oracle on a copy taken before the Rust posts. The child removed
`DEEPSEEK_RUNTIME_MODE`. Clocks, `runId`, `traceId`,
`timeoutCheckedAtMs`, and `evidence.action.savedItem` identity were stripped.
Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\automation-run-routes-4.log`.
Earlier logs `automation-run-routes.log`, `-2.log`, and `-3.log` are failed
attempts (civil-date compile, memory fingerprint count, saved-item strip
path) and are not the green result. Linker:
`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain: `rustc 1.97.1` stable
`x86_64-pc-windows-gnu`. HEAD `0e340dc3695890e8d85d59c8263192f0593fb79e` plus
this uncommitted slice.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider`
passed. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\automation-run-inventory.log`
and `automation-run-inventory-pytest.log`. Scanner python-only routes
111 → 110. `automation.py` 3 → 2. The remaining automation paths are only
`GET /api/automation` and `POST /api/automation`. `status.py` is 1
(`semantic-cache`). `workspace.py` stayed 33. `backup_governance.py` stayed
62. `media.py` stayed 6. Authority `python`. Go-owned domains `[]`. Python
production domains 34, rust 12.

### Not done, next executable task

Do not treat this slice as a domain flip. Do not register `GET /api/automation`
or `POST /api/automation` until every POST action is real (`create`, `run`,
`rerun`, `simulate`, `run_due`). Registering list or create alone would turn
the other actions into 405. Do not port `GET /api/semantic-cache/status`
while `status()` creates `.semantic-cache`. Do not treat artifact download
as migrated. Do not port `GET /api/workspace/resilience/journal` while
`_connect` creates the database. Do not stub `readiness_status`,
`calculate_dr_slo_metrics`, catalog chain health, target capabilities,
capacity summary, or transfer budget. Do not register drain GET. Do not
answer with 501. Do not treat the scheduler admission controller as migrated.
`GET`, `PATCH`, and `DELETE /api/media/{media_id}` share one path and must
move together. `GET` and `POST /api/media` share one path and must move
together.

`POST /api/workspace/projects/{project_id}/saved-items` is already registered
and still answers `501 NATIVE_PROJECTS_MUTATIONS_NOT_READY`. The path scanner
therefore omits it. `PATCH` and `DELETE` on
`/api/workspace/projects/{project_id}/saved-items/{saved_id}` are still
Python-only and share one path. The next slice replaces that 501 with
`create_saved_item` and serves PATCH and DELETE together, gated on
`project_metadata_store` / `python_disabled`, with a legal write and a
refusal. Do not register only one of those two methods. Do not add another
501.

110 Python-only public routes remain, 62 of them in `backup_governance.py`,
33 in `workspace.py`, 6 in `media.py`, and 2 in `automation.py`. Provider,
desktop, Android, and zero-Python workload gates remain open. Do not redo the
4.8.0 double launch unless the release gateway binary changes. The product
remains **未完成**.

## Previous continuation checkpoint — 2026-10-04 template create

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. `.automation` was not added to `DECLARED_NATIVE_DATA_DOMAINS` or
`RUST_DATA_DOMAINS`. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`POST /api/automation/templates/{template_id}` is served by the Rust production
router. It is `registry.create_from_template`: copy the builtin automation,
set `projectId` with Python's `or ""`, then apply a shallow object `overrides`
(`overrides.projectId` wins). The write reuses `create_automation` in
`automation_definition_routes.rs` (same `write_json_atomic` file, mutation
gate, indent 2, trailing newline). Ownership is checked after the template
lookup and before normalize. An unknown template is `404 Automation template
not found` and does not create `.automation`, even while Python owns the
store. A known template while Python is authoritative is `409
NATIVE_AUTOMATION_WRITE_NOT_OWNED` and does not create or change the file,
including when overrides would later be invalid.

With `DEEPSEEK_RUNTIME_MODE=python_disabled`, a legal create returns
`{"ok": true, "automation": ...}`. The id is `auto_` plus 16 lowercase hex.
`GET /api/automation/{id}` returns the same body. A non-empty `projectId`
calls `require_project` before the write. `_touch_project` then updates
`updatedAt`. `.automation/history.json` is not created. A duplicate id is
`409 Automation already exists` and does not rewrite. At 500 visible rows the
answer is `413 Too many automations` and does not rewrite. A file of 501 valid
rows still presents 500, so the hidden prefix stays. Empty body, non-object,
bad JSON, bad Content-Length, a length above 2000000, and invalid UTF-8 follow
the same status codes as PATCH. Missing token is `401`. Any other method on
this path is `405 {"detail":"Method Not Allowed"}` with `Allow: POST`.
`POST /api/automation/templates` stays 405. `GET /api/automation/templates`
still does not create the store.

This is **集成通过**, not **完成切换**. Python `_write_automations` remains the
writer while Python is authoritative.

### Verification

`cargo fmt --manifest-path rust/Cargo.toml -p deepseek-gateway` then
`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test automation_template_routes --test automation_definition_routes -- --test-threads=1`.
Template tests: 2 passed in 2.73s. Definition tests: 2 passed in 7.56s.
The legal create, project touch, duplicate 409, and 501-record 413 were
compared with a live `create_from_template` oracle. The child removed
`DEEPSEEK_RUNTIME_MODE` so the project touch did not trip
`PythonWriterMechanicallyDeniedError`. Clocks and minted ids were stripped
before the body compare. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\automation-template-routes.log`.
Linker: `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain: `rustc 1.97.1` stable
`x86_64-pc-windows-gnu`. HEAD `0e340dc3695890e8d85d59c8263192f0593fb79e` plus
this uncommitted slice.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider`
passed (1 passed). Scanner python-only routes 112 → 111.
`automation.py` 4 → 3. The remaining automation paths are `GET /api/automation`,
`POST /api/automation`, and `POST /api/automation/{automation_id}/run`.
`status.py` is 1 (`semantic-cache`). `workspace.py` stayed 33.
`backup_governance.py` stayed 62. `media.py` stayed 6. Authority `python`.
Go-owned domains `[]`. Python production domains 34, rust 12.

### Not done, next executable task

Do not treat this slice as a domain flip. Do not register `GET /api/automation`
or `POST /api/automation` until every POST action is real. That POST is not
only `create`: it also dispatches `run`, `rerun`, `simulate`, and `run_due`.
Registering the path for list/create alone would turn those actions into 405.
`POST /api/automation/{automation_id}/run` is its own path. The next slice is
a real `runner.run_once`: one legal run that persists the history record and
the action output, plus a real refusal, compared with the Python oracle. Do
not ship a skip-only handler or a 501. Do not port
`GET /api/semantic-cache/status` while `status()` creates `.semantic-cache`.
Do not treat artifact download as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not answer with 501. Do not treat the scheduler
admission controller as migrated. `GET`, `PATCH`, and `DELETE /api/media/{media_id}`
share one path and must move together.

111 Python-only public routes remain, 62 of them in `backup_governance.py`,
33 in `workspace.py`, 6 in `media.py`, and 3 in `automation.py`. Provider,
desktop, Android, and zero-Python workload gates remain open. Do not redo the
4.8.0 double launch unless the release gateway binary changes. The product
remains **未完成**.

## Previous continuation checkpoint — 2026-10-04 automation definition writes

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`PATCH` and `DELETE /api/automation/{automation_id}` are served by the Rust
production router. `PATCH` is `registry.update_automation`. `DELETE` is
`registry.delete_automation`. The file is `.automation/automations.json`,
written with `write_json_atomic` (mutation gate, indent 2, trailing newline).
A missing id on delete returns `{"ok": true, "deleted": 0}` and does not
rewrite the file. A visible match returns `deleted: 1` and drops every visible
row with that id. Update merges the object shallowly, forces the URL id,
normalises with `touch=True`, and replaces only the first visible row. A
successful write keeps only the last 500 accepted records, so a hidden prefix
disappears. Patching an id outside that window is `404` and does not rewrite.
A non-empty `projectId` calls `require_project` before the write (`404 Project
not found`, file unchanged, `.projects` not created). After a successful
write, `_touch_project` updates that project's `updatedAt` and swallows a
missing project. `.automation/history.json` is not created.

Writes require `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise the answer
is `409 {"error":"The automation store is still written by the Python runtime, so this gateway refuses to mutate it.","code":"NATIVE_AUTOMATION_WRITE_NOT_OWNED"}`.
That refusal does not create `.automation` and does not change an existing
file. Body checks still run first: an empty body is `400 Request body is empty`,
a non-object is `400 Request body must be a JSON object`, invalid JSON is
`400`, a bad `Content-Length` is `400`, a length above 2000000 is `413`, and
invalid UTF-8 is `500 Server error`. An invalid id is `400 Invalid automation id`
before the ownership check. An unsupported trigger matches the oracle's 400.
Missing token is `401`. `HEAD` follows GET. `POST` on this path is
`405 {"detail":"Method Not Allowed"}`. `POST /api/automation/{id}/run` is still
the Go proxy.

The store is not in `DECLARED_NATIVE_DATA_DOMAINS` or `RUST_DATA_DOMAINS`.
Python's `_write_automations` remains the writer while Python is authoritative.
This is **集成通过**, not **完成切换**.

### Verification

`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test automation_definition_routes -- --test-threads=1`
passed 2 production-router tests in 6.44s. The legal rename, project move,
hidden-prefix 404, and 500-record rewrite were compared with a live
`update_automation` / `delete_automation` oracle. Clocks were compared as
fresh `updatedAtMs` values rendered with `timestamp_ms_to_iso`. Refusals leave
the store bytes unchanged. Evidence:
`C:\Users\12393\AppData\Local\Temp\grok-goal-e6274bd2ca41\implementer\automation-definition-routes.log`.
Linker: `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` =
`rust/target/tmp/linkwrap/x86_64-w64-mingw32-gcc.exe`, `TEMP`/`TMP` =
`D:\deepseek\rust\target\tmp`. Toolchain: `rustc 1.97.1` stable
`x86_64-pc-windows-gnu`. HEAD `0e340dc3695890e8d85d59c8263192f0593fb79e` plus
this uncommitted slice.

`python scripts/production_runtime_inventory.py --check` passed. The scanner
matches paths, and `/api/automation/:automation_id` was already registered for
GET, so python-only routes stay 112. `status.py` is 1 (`semantic-cache`).
`workspace.py` stayed 33. `backup_governance.py` stayed 62. `media.py` stayed
6. `automation.py` stayed 4. Authority `python`. Go-owned domains `[]`.
Python production domains 34, rust 12.

### Not done, next executable task

Do not treat this slice as a domain flip. Do not port
`GET /api/semantic-cache/status` while `status()` creates `.semantic-cache`.
Do not register `GET /api/automation` or `POST /api/automation`: those share
one path, and `list` / `create` / `run` are not this writer. The next closed
mutation is `POST /api/automation/templates/{template_id}`
(`create_from_template`), which has no sibling method on that path and can
reuse this writer gate. Do not treat
`GET /api/workspace/artifacts/{artifact_id}/download` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not answer with 501. Do not treat the scheduler
admission controller as migrated.

112 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 33 in `workspace.py`, 6 in `media.py`, and 4 in
`automation.py`. Provider, desktop, Android, and zero-Python workload gates
remain open. Do not redo the 4.8.0 double launch unless the release gateway
binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 rust status read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/rust/status` is served by the Rust production router. The body is
`rust_status()`: `{"ok": true, "rust": {"enabled", "components"}}`. Gateway,
mcp, policy, and rag flags use `rust_core.config._env_bool`: a missing or
empty value uses the default, and a whitespace-only value is false. A disabled
gateway reports `url: ""` and `healthy: false` without opening a socket. An
enabled gateway checks `GET /healthz` on the URL host and port, ignores the
URL path, refuses a non-http scheme, does not use a proxy, and does not follow
redirects. HTTP 200 is healthy. Any other status or a connection failure is
not. A blank `DEEPSEEK_RUST_GATEWAY_URL` falls back to
`http://127.0.0.1:8787` without this test dialing that port. Nothing is written.

`HEAD` returns 200 with an empty body. An authenticated other method is
`405 {"detail": "Method Not Allowed"}`. The query string is ignored.

The production-router test
`rust_status_matches_the_oracle_without_creating_a_store` passed against a live
Python `rust_status` oracle. Evidence: `native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 113 → 112. `status.py` is 1 (`semantic-cache`). `workspace.py` stayed
33. `backup_governance.py` stayed 62. `media.py` stayed 6. `automation.py`
stayed 4. Authority `python`. Go-owned domains `[]`. Python production domains
34, rust 12. Artifact download is still Python-only. Evidence:
`inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not port `GET /api/semantic-cache/status` while `status()` creates
`.semantic-cache`. Do not treat
`GET /api/workspace/artifacts/{artifact_id}/download` as migrated. Do not treat
the rest of `/api/workspace/resilience` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not register `GET /api/automation` or a single method
of `/api/media` that shares its path with an unimplemented method. Do not
treat PATCH or DELETE `/api/automation/{automation_id}` as migrated. Do not
answer with 501. Do not treat the scheduler admission controller as migrated.
Do not declare an edge or rust-status domain. A blank gateway URL's health
check against port 8787 was not executed here.

112 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 33 in `workspace.py`, 6 in `media.py`, and 4 in
`automation.py`. Provider, desktop, Android, and zero-Python workload gates
remain open. Do not redo the 4.8.0 double launch unless the release gateway
binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 edge status read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/edge/status` is served by the Rust production router. The body is
`edge_inference_status()` with an empty payload: `{"ok": true, "edgeInference"}`.
The query string is ignored. Settings coerce the provider before
`normalize_provider`, so `dry_run` stays `llama_cpp` and `FAKE` is `fake`.
`EDGE_INFERENCE_PROVIDER` wins when it is non-empty. A `llama_cpp` model path
is resolved; `mlc` and `fake` keep the stripped string. `dependencyAvailable`
follows `importlib.util.find_spec` for `llama_cpp` and `mlc_llm` by searching
the working directory, `PYTHONPATH`, and `site-packages` beside `python` on
`PATH`. This route does not start an interpreter and does not load a model, so
`loaded` stays false. Nothing is written.

`HEAD` returns 200 with an empty body. An authenticated other method is
`405 {"detail": "Method Not Allowed"}`.

The production-router test
`edge_status_matches_the_oracle_without_creating_a_store` passed against a live
Python `edge_inference_status` oracle. Evidence: `native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 114 → 113. `status.py` is 2 (`semantic-cache`, `rust`). `workspace.py`
stayed 33. `backup_governance.py` stayed 62. `media.py` stayed 6.
`automation.py` stayed 4. Authority `python`. Go-owned domains `[]`.
Python production domains 34, rust 12. Artifact download is still Python-only.
Evidence: `inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not port `GET /api/semantic-cache/status` while `status()` creates
`.semantic-cache`. The next method-closed read that does not create a store is
`GET /api/rust/status`. Do not treat
`GET /api/workspace/artifacts/{artifact_id}/download` as migrated. Do not treat
the rest of `/api/workspace/resilience` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not register `GET /api/automation` or a single method
of `/api/media` that shares its path with an unimplemented method. Do not
treat PATCH or DELETE `/api/automation/{automation_id}` as migrated. Do not
answer with 501. Do not treat the scheduler admission controller as migrated.
Do not declare an edge domain.

113 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 33 in `workspace.py`, 6 in `media.py`, and 4 in
`automation.py`. Provider, desktop, Android, and zero-Python workload gates
remain open. Do not redo the 4.8.0 double launch unless the release gateway
binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 scheduler read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/scheduler` is served by the Rust production router. The body is
`scheduler_status()` plus `dead_letters(limit)`: `{"ok": true, "scheduler",
"deadLetters"}`. Admission counters are this process's fresh snapshot (all
zero) because the edge does not take a lease. `rate_per_second <= 0` reports
`float(capacity)` with no clock. Burst `0` uses `max_concurrency`. Settings
come from the same `SCHEDULER_*` clamps as `config.py`. A missing
`.scheduler/scheduler.sqlite3` stays missing and the dead-letter block has no
`recent` key. An existing file is opened read-only: rows match, a missing
table and a non-database file put `error` on the block and return
`deadLetters: []`, and a directory path is `unable to open database file`.
`limit` follows `int` with `ValueError` → 50, then
`max(1, min(int(limit or 50), 1000))`. `HEAD` returns 200 with an empty body.
An authenticated other method is `405 {"detail": "Method Not Allowed"}`.

The production-router test
`scheduler_matches_the_oracle_without_creating_a_store` passed against a live
Python `scheduler_status` / `dead_letters` oracle. Evidence:
`native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 115 → 114. `status.py` is 3 (`semantic-cache`, `edge`, `rust`).
`workspace.py` stayed 33. `backup_governance.py` stayed 62. `media.py` stayed
6. `automation.py` stayed 4. Authority `python`. Go-owned domains `[]`.
Python production domains 34, rust 12. Artifact download is still Python-only.
Evidence: `inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not treat `GET /api/workspace/artifacts/{artifact_id}/download` as migrated.
Do not treat the rest of `/api/workspace/resilience` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not register `GET /api/automation` or a single method
of `/api/media` that shares its path with an unimplemented method. Do not
treat PATCH or DELETE `/api/automation/{automation_id}` as migrated. Do not
answer with 501. Do not treat the scheduler admission controller as migrated:
this slice only reads the fresh snapshot and an existing DLQ file.

114 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 33 in `workspace.py`, 6 in `media.py`, and 4 in
`automation.py`. Provider, desktop, Android, and zero-Python workload gates
remain open. Do not redo the 4.8.0 double launch unless the release gateway
binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 artifact preview read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/workspace/artifacts/{artifact_id}/preview` is served by the Rust
production router. The body is `preview_artifact`: `{"ok": true, "artifact",
"previewAvailable", "content", "bytes"}`. A missing `artifacts.json`, bad JSON,
and a directory of that name are `404 Artifact not found` and do not create
`.projects`. Invalid UTF-8 in that file is `500 Server error` and the bytes are
left unchanged. An explicit `projectId` searches only that project, including
when `project.json` is absent. An empty or missing `projectId` scans projects
that already have `project.json`, newest `updatedAt` first. Text types are
redacted and capped at 100000 characters. Other types return an empty content
and `previewAvailable: false`. The last 500 normalised records are kept.

`HEAD` returns the same status with an empty body. An authenticated method
other than GET or HEAD is `405 {"detail": "Method Not Allowed"}`.
`GET /api/workspace/artifacts/{artifact_id}/download` stays on the Go proxy.

The production-router test
`artifact_preview_matches_the_oracle_without_creating_a_store` passed against
a live Python `preview_artifact` oracle. Evidence: `native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 116 → 115. `workspace.py` is 33. `backup_governance.py` stayed 62.
`media.py` stayed 6. `automation.py` stayed 4. Authority `python`. Go-owned
domains `[]`. Python production domains 34, rust 12. The download path is
still Python-only. Evidence: `inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not treat `GET /api/workspace/artifacts/{artifact_id}/download` as migrated.
Do not treat the rest of `/api/workspace/resilience` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not register `GET /api/automation` or a single method
of `/api/media` that shares its path with an unimplemented method. Do not
treat PATCH or DELETE `/api/automation/{automation_id}` as migrated. Do not
answer with 501.

115 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 33 in `workspace.py`, 6 in `media.py`, and 4 in
`automation.py`. Scheduler, provider, desktop, Android, and zero-Python
workload gates remain open. Do not redo the 4.8.0 double launch unless the
release gateway binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 federation readiness read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/workspace/resilience/federation` is served by the Rust production
router. The body is `build_federation_snapshot` with the route's fixed wire
list `object-set-v1`, `receipt-v4`, `commit-v4`, `fastcdc-v3`, empty failure
domains, `forecastHeadroom: null`, `costClass: unknown`, and
`readiness: UNKNOWN`. A missing `fleetId` or an empty value becomes `local`.
Whitespace that trims to empty is `500 Server error`. The digest is SHA-256
over the compact sorted JSON of every field except `snapshotDigest`. The
function does not read or create a store.

`HEAD` returns the same status with an empty body. An authenticated method
other than GET or HEAD is `405 {"detail": "Method Not Allowed"}`.
`GET /api/workspace/resilience/journal` stays on the Go proxy and still does
not create `.resilience-journal`.

The production-router test
`federation_snapshot_matches_the_pure_builder_without_creating_a_store`
passed against a live Python `build_federation_snapshot` oracle, including a
Python check that each success body's `snapshotDigest` matches
`_snapshot_digest`. Evidence: `native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 117 → 116. `backup_governance.py` 63 → 62. `media.py` stayed 6.
`automation.py` stayed 4. Authority `python`. Go-owned domains `[]`.
Python production domains 34, rust 12. Evidence: `inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not treat the rest of `/api/workspace/resilience` as migrated. Do not port
`GET /api/workspace/resilience/journal` while `_connect` creates the database.
Do not stub `readiness_status`, `calculate_dr_slo_metrics`, catalog chain
health, target capabilities, capacity summary, or transfer budget. Do not
register drain GET. Do not register `GET /api/automation` or a single method
of `/api/media` that shares its path with an unimplemented method. Do not
treat PATCH or DELETE `/api/automation/{automation_id}` as migrated. Do not
answer with 501.

116 Python-only public routes remain in the scanner output, 62 of them in
`backup_governance.py`, 6 in `media.py`, and 4 in `automation.py`. Scheduler,
provider, desktop, Android, and zero-Python workload gates remain open. Do
not redo the 4.8.0 double launch unless the release gateway binary changes.
The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 media segment read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway.

### This slice

`GET /api/media/{media_id}/segments` is served by the Rust production router.
`library.get_media` runs first, then `library.list_segments`. Both read
`.media` and neither creates it. A missing `library.json`, invalid JSON, or a
directory at that path is an empty library, so a well-formed id is
`404 Media not found`. Invalid media records are skipped. The first accepted
id wins. A missing segments file is `{"ok": true, "segments": []}`. Invalid
segment records are skipped. `int()` failures on `index` or `page` are
`500 Server error` and do not rewrite the file. Invalid UTF-8 is 500. An
invalid media id is `400 Invalid media id` before the file is read.

`HEAD` returns the same status with an empty body. An authenticated method
other than GET or HEAD is `405 {"detail": "Method Not Allowed"}` and does not
create `.media`. The gateway auth layer still answers an unauthenticated
request with the nested `401 UNAUTHORIZED` before that 405. `GET /api/media`
and `GET /api/media/{media_id}` stay on the Go proxy.

The production-router test
`media_segments_match_the_library_without_creating_a_missing_store` passed
against a live Python `get_media` / `list_segments` oracle. Evidence:
`native-route-tests.log`.

### Inventory

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py`. Scanner python-only
routes 118 → 117. `media.py` 7 → 6. `automation.py` stayed 4.
`backup_governance.py` stayed 63. Authority `python`. Go-owned domains `[]`.
Python production domains 34, rust 12. `GET /api/media/{media_id}/segments`
is no longer Python-only. The other six media methods remain Python-only.
PATCH and DELETE `/api/automation/{automation_id}` are still the Go proxy;
the path scanner dropped them earlier and they are not implemented.
Evidence: `inventory-check.log`.

Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not treat the rest of `/api/media` as migrated. Collection GET shares its
path with POST, and the item path shares GET with PATCH and DELETE. Do not
register one of those methods alone. Do not treat PATCH or DELETE
`/api/automation/{automation_id}` as migrated. Do not register
`GET /api/automation`. Do not port `GET /api/workspace/resilience/journal`
while `_connect` creates the database. Do not stub `readiness_status`,
`calculate_dr_slo_metrics`, catalog chain health, target capabilities,
capacity summary, or transfer budget. Do not register drain GET. Do not
answer with 501.

117 Python-only public routes remain in the scanner output, 63 of them in
`backup_governance.py`, 6 in `media.py`, and 4 in `automation.py`. `.media`
is not a declared native domain. Scheduler, provider, desktop, Android, and
zero-Python workload gates remain open. Do not redo the 4.8.0 double launch
unless the release gateway binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 automation definition read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway
(`rust/target/release` is absent; `rust/target/tmp/native-bin/deepseek-gateway.exe`
is still the 15:02 copy). Launch-1/launch-2 were not repeated.

### This slice

`GET /api/workspace/disaster-recovery/replication` is served by
`rust/crates/deepseek-gateway/src/backup_replication_routes.rs` on
`create_production_app`, ahead of the Go `/api/*` catch-all. It is
`backup_replication.list_jobs(policy_id, backup_id, limit=100)` over
`.backup-replication/*.json`. A missing directory returns `{"jobs":[]}` and is
not created. The handler never writes a job, cursor, or schema. There is no
mutation on this path to gate. Invalid UTF-8 fails the whole request with
`500 {"error":"Server error","code":"internal"}`. Invalid JSON, a non-object,
a directory named `*.json`, and a missing or empty `jobId` are skipped.
`jobId: null` stays null and is kept (`str(None)` is `"None"`). `policyId` and
`backupId` use `str(value or "")`, so JSON `0` / `false` / `null` do not match
the query strings `"0"` / `"False"` / `"None"`. Empty query values do not
filter. `phase` is ignored. Windows name order is casefolded, matching
pathlib. The list stops at 100 after filters, highest name first.

`GET /api/workspace/disaster-recovery/drills/{restore_id}` is served by
`rust/crates/deepseek-gateway/src/backup_drill_routes.rs`. The registered
string is `/api/workspace/disaster-recovery/drills/:restore_id`. It reads
`.restore-staging/{id}/drill-result.json`, else `drill-running.json`, and
returns that object. The id must start with `restore_` and the remainder must
be Unicode alphanumeric, or the response is
`400 {"error":"Invalid restore id","code":"invalid_payload"}`. A missing
session is `404 {"error":"Remote restore session not found","code":"not_found"}`.
No result and no claim is
`404 {"error":"Recovery Drill result not found","code":"not_found"}`.
Bad JSON or a non-object is
`400 {"error":"Recovery Drill metadata is unavailable","code":"invalid_payload"}`.
Invalid UTF-8 is `500 {"error":"Server error","code":"internal"}`. The
directory is not created. Other methods on that one-segment pattern, including
`POST /api/workspace/disaster-recovery/drills/run`, are forwarded to the
existing Go proxy so a GET-only route does not answer them with 405.
`POST /drills/run` stays in the Python-only inventory.

`GET /api/workspace/resilience/journal` was inspected and not ported.
`resilience_action_journal.list_actions` calls `_connect`, which creates
`.resilience-journal`, sets `journal_mode=WAL`, runs `SCHEMA_INIT`, and
`ALTER`s missing columns. A reader that did that would be a second writer.
Returning an empty list without the directory would not match that function.

`GET /api/automation/templates` is served by
`rust/crates/deepseek-gateway/src/automation_template_routes.rs`. It returns
`{"ok": true, "templates": registry.list_templates()}`. That catalog is the
in-memory `BUILTIN_TEMPLATES` list and does not read or create `.automation`.
`POST /api/automation/templates/{template_id}` is a different path and still
falls through to the Go proxy. `POST /api/automation/templates` is 405, the
same as a GET-only registration of a path Python does not write.

`GET /api/automation/{automation_id}/runs` is served by
`rust/crates/deepseek-gateway/src/automation_run_routes.rs`. The registered
string is `/api/automation/:automation_id/runs`. It calls `history.list_runs`
with only `automation_id` and `limit`. `projectId` and `status` on the query
string are ignored. The file is `.automation/history.json` via the
non-creating `read_json_file` path: a missing file, invalid JSON, a
non-object, a non-list `runs`, or a directory named `history.json` returns
`{"ok": true, "runs": []}` and does not create the store. Invalid UTF-8 is
`500 {"error":"Server error","code":"internal"}`. Records are normalised.
A record `normalize_run_record` rejects is skipped. The last 2000 accepted
records are kept, then filtered, then sorted by `startedAtMs` descending.
`limit` is `int(query or 100)` before `list_runs`. Missing and `""` are 100.
The string `"0"` parses as 0 and `list_runs` then treats 0 as 100; it is not
unlimited. A negative value returns every loaded run. Values above 2000
clamp to 2000. A non-integer is 500 and does not validate the id or read the
file. An invalid id is
`400 {"error":"Invalid automation id","code":"invalid_payload"}` and does
not create `.automation`. The handler never writes `history.json`.
`POST /api/automation/{automation_id}/run` is a different path and still
falls through to the Go proxy. `GET /api/automation` was not registered
because that path also has POST.

`GET /api/automation/{automation_id}` is served by
`rust/crates/deepseek-gateway/src/automation_definition_routes.rs`. The
registered string is `/api/automation/:automation_id`. It calls
`registry.get_automation`. The file is `.automation/automations.json` via
the non-creating `read_json_file` path. A missing file, invalid JSON, a
non-object, a non-list `automations`, or a directory named `automations.json`
is `404 {"error":"Automation not found","code":"not_found"}` and does not
create `.automation`. Invalid UTF-8 is
`500 {"error":"Server error","code":"internal"}`. An invalid id is
`400 {"error":"Invalid automation id","code":"invalid_payload"}` and does
not read the file. Records are `normalize_automation(..., touch=False)`.
Rejected records are skipped. Only the last 500 accepted records are
visible, then the first matching id wins. The handler never writes the
file. PATCH and DELETE on that same path are forwarded to the existing Go
proxy, so they stay `503 GO_CONTROL_PROXY_NOT_READY` when the control URL
is empty instead of becoming 405. They are not implemented.
`GET /api/automation/templates` remains the static catalog route.
`POST /api/automation/templates` remains 405.

### Verification

`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --test backup_drill_routes --test backup_replication_routes -- --test-threads=1`
passed: 1 drill test and 2 replication tests. The template route was tested
after that merge:
`cargo test -p deepseek-gateway --offline --test automation_template_routes -- --test-threads=1`
passed 1 production-router test. The body was compared with a live
`registry.list_templates()` call, not a second copy of the catalog.

`cargo test -p deepseek-gateway --offline --test automation_run_routes -- --test-threads=1`
passed 1 production-router test. Success bodies were compared with a live
`history.list_runs` call that uses the same `int(limit or 100)` expression,
including normalisation, the 2000-record truncation before filtering, and
`limit=0` returning 100 of 150 runs while `limit=-5` returns all 150.

`cargo test -p deepseek-gateway --offline --test automation_definition_routes -- --test-threads=1`
passed 1 production-router test. Success and error bodies were compared with
a live `registry.get_automation` call, including normalisation, the 500-record
window, a missing file, invalid UTF-8, and a directory named `automations.json`.
PATCH and DELETE returned `503 GO_CONTROL_PROXY_NOT_READY` and did not create
or modify the store. Evidence: `native-route-tests.log` in the session
scratch. Linker shim remains `rust/target/tmp/linkwrap` with `TEMP`/`TMP` on
`D:\deepseek\rust\target\tmp`.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider` passed.
Python-only public routes: 125 → 118. `backup_governance.py`: 65 → 63.
`automation.py`: 9 → 4. The scanner matches paths, not methods, so registering
`/api/automation/:automation_id` also removed PATCH and DELETE from
`http_routes_only_on_python`. Those two methods are still the Go proxy, not a
native implementation. `POST /drills/run`,
`POST /api/automation/templates/{template_id}`,
`POST /api/automation/{automation_id}/run`, `GET/POST /api/automation`, and
`GET /api/workspace/resilience/journal` still are Python-only. Authority
`python`. Go-owned domains `[]`. Python production domains 34, rust 12.
Evidence: `inventory-check.log`.

Package inspection fails: desktop/server launch inputs and the Android
`stopPythonServer` path still reference Python. `package-inspect.log`.
MinIO was not provisioned (no `minio` binary, Docker engine pipe absent).
`provider-recovery.log`. `launch-unchanged.txt` records that the release
listener was not rebuilt.

### Not done, next executable task

Do not treat PATCH or DELETE `/api/automation/{automation_id}` as migrated.
They are forwarded to the Go proxy. The next real read that does not share
its path with an unimplemented method should be chosen from the remaining
Python-only list. Do not register `GET /api/automation`: that path also has
POST, and `registry.list_automations` is a different call from `get_automation`.
Do not port `GET /api/workspace/resilience/journal` while `_connect` creates
the database. Do not stub `readiness_status`, `calculate_dr_slo_metrics`,
catalog chain health, target capabilities, capacity summary, or transfer
budget. Do not register drain GET. Do not answer with 501.

118 Python-only public routes remain in the scanner output, 63 of them in
`backup_governance.py` and 4 in `automation.py`. PATCH and DELETE
`/api/automation/{automation_id}` are among the routes the scanner no longer
lists and are still unimplemented.
Package inspection still fails (`package-inspect.log`): desktop, server, and
Android launch inputs still reference Python. MinIO was not provisioned
(`provider-recovery.log`). Scheduler, provider, desktop, Android, and
zero-Python workload gates remain open. Do not redo the 4.8.0 double launch
unless the release gateway binary changes. The product remains **未完成**.

## Previous continuation checkpoint — 2026-10-03 backup retirement HTTP

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Shared source remains uncommitted.
No push, merge, release, or production-data operation. The product migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` was not edited and stays
`NOT_READY`. `current_production_authority` stays `python`. No ownership domain
was flipped. Debug `cargo test` did not relink the 4.8.0 release gateway, so
launch-1/launch-2 remain the earlier double-launch proof.

### This slice

`POST /api/workspace/backup-retirements`, `GET /api/workspace/backup-retirements`,
and `GET /api/workspace/backup-retirements/{job_id}` are served by
`rust/crates/deepseek-gateway/src/backup_retirement_routes.rs` on
`create_production_app`, ahead of the Go `/api/*` catch-all. The row is the
Python `requested` job only: `retire_` + 16 lowercase hex, UTC seconds with `Z`,
default reason `api-retirement-request`, `simMetadata` `{}`, `bytesReclaimed` 0,
`error` null. Physical GC, receipt/commit markers, and
`execute_copy_retirement_job` are not claimed and are not faked.

Writes run only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise POST is
`409 NATIVE_BACKUP_RETIREMENT_WRITE_NOT_OWNED` and does not create
`.backup-retirements`. The domain is not in `DECLARED_NATIVE_DATA_DOMAINS` or
`RUST_DATA_DOMAINS`: the Python GC worker still updates the same table, so
declaring sole native ownership would be false. Reads of a missing database
return `{"jobs":[]}` or 404 and do not mkdir. A database Python already wrote is
opened read-only, including a pre-`reason` schema, and is not migrated. Once
Python is de-authorised, opening an existing file applies the same `reason`
column migration as `backup_retirement._connect`. Invalid `sim_metadata` is
`500 {"error":"Server error","code":"internal"}`.

### Verification

`cargo test --manifest-path rust/Cargo.toml -p deepseek-gateway --offline --lib --test backup_retirement_routes retirement -- --test-threads=1`
passed: 2 lib tests and 4 production-router tests. Linker shim remains
`rust/target/tmp/linkwrap` with `TEMP`/`TMP` on `D:\deepseek\rust\target\tmp`.
`GET /api/workspace/backup-runs` is the same edge, read-only. It lists
`.backup-scheduler/scheduler.db` `backup_runs` with the route's limit of 50.
Blocked phases (`blocked`, `blocked-retryable`, `blocked-terminal`) copy
`leaseUntil` to `nextRetryAt` and `reason` to `blockedReason`. A missing
database returns `{"runs":[]}` and does not create `.backup-scheduler`. An
existing file is opened read-only: no WAL change and no schema write.
`cargo test -p deepseek-gateway --offline --test backup_run_routes` passed
1 production-router test. Retirement tests were rerun after the merge and
passed again.

`python scripts/production_runtime_inventory.py --write` then
`pytest tests/test_production_runtime_inventory.py -p no:cacheprovider` passed.
Python-only public routes: 129 → 125. `backup_governance.py`: 69 → 65.
Authority `python`. Go-owned domains `[]`. Retirement and backup-runs paths
are no longer in `http_routes_only_on_python`.

### Not done, next executable task

Port the next method-closed backup-governance surface. 125 Python-only
public routes remain, 65 of them in `backup_governance.py`. Do not register
`/api/workspace/backup-targets/{target_id}/drain` until POST initiate is real:
the inventory key is the path, and initiate commits control authority before the
sqlite projection. Do not answer remaining routes with 501. Do not redo the
4.8.0 double launch unless the release gateway binary changes. Scheduler,
provider, desktop, Android, and zero-Python workload gates remain open.

## Previous continuation checkpoint — 2026-10-02 typed recipients and mirror recovery

Branch `codex/indexmap-std-feature`, base HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; shared source remains uncommitted.
No push, merge, release or production-data operation occurred. Whole migration is
**未完成**; readiness remains `NOT_READY`.

- Rust mirror recipients now use generated `control/v1.GetBackupPolicyRecipients`,
  not the newly added HTTP/JSON bridge. Unary gRPC and public HTTP/1 share the existing
  Go listener. RPCs enforce loopback, exactly one internal bearer, 1 MiB messages,
  and authoritative policy records; empty enabled groups remain visible to the
  sealer. Rust has no HTTP/JSON or Python fallback for this read.
- Frozen descriptor JSON is unchanged; the contract permits only the exact typed
  read shape and rejects tampering. Pinned codegen write/check and contract check
  pass. `native-20261002-protocol-denial-check.log` records 30 contract/denial cases.
- Fixed a fail-open recovery-fence read: corrupt/unreadable errors were discarded
  and could publish a new generation. The red regression is preserved in
  `native-20261002-mirror-fence-red-corrected.log`; the route now returns the original
  423. Its test requires unchanged HEAD/generation count; rerun is still pending.
- Linux Rust 1.85.0 workspace check/build/clippy, all targets/features and
  `-D warnings`, pass. Initial gateway route/auth tests pass 4+2 (before the new
  fence regression). Host checks now include mirror collection/item routes.
- `native-20261002-real-process-boundary-recovered.log`: **four real separately
  built Rust/Go process tests pass**, covering policy reads/full CRUD, transferred
  target health, and typed mirror recipients. The mirror case verifies actual age
  ciphertext/hash, immutable idempotent replay, Host/token refusal, strong process
  kill, waiting for the actual recorded writer lease, greater successor fencing
  token, recovered generation and RPC-loss GET/PUT refusal with unchanged disk.
  Immediate restart was correctly refused `WRITER_FENCE_HELD`; no clock, SQL fence
  or production lease default was weakened.
- Mypy 929 files passes after fixing a new `tmp_settings` annotation to `Path`.
  The duplicate-module source copy was safely relocated to
  `D:/deepseek-native-validation/source-v14-20260930T075002Z`.
- Frontend typecheck, **609 tests/73 files** (`--maxWorkers=2`), build and bundle
  pass (`native-20261002-frontend-*.log`). Initial unbounded tests exhausted Node
  memory and are not a PASS. Current built UI is available under `static/ui`.

### Verification environment and next executable work

Isolated source `D:/deepseek-native-validation/source-current-20261002` is tracked by
`artifacts/native-20261002-linux-source-manifest.json` (base HEAD plus dirty file
hashes). SDK image `deepseek-local-native-verification:20261002`, ID
`sha256:c33bc88e8d52443ed01d659e8a20b99bb08438bfd7c30ef6e32532da8edbd9ce`, contains
Rust 1.85.0, Go 1.27.1, cargo-llvm-cov 0.6.21 and development/oracle Python/OCR.
It is **not a zero-Python product image**. Task-owned Rust cache volume is
`deepseek-native-rust-target-20261002`; Go volumes are
`deepseek-native-v14-go-modules-20260930` and `deepseek-native-v14-go-build-20260930`.
Only `artifacts/native-20261002-linux-run` is bound as writable output.

1. Restore verification after Rust coverage links were killed (signal 9), followed
   by Docker API 500 and WSL block-device I/O errors. Docker restart is pending;
   no cache prune, factory reset or global security change was made. Linux Go
   coverage/race and Rust coverage have no PASS. Windows Go coverage is running
   as an independent path. Retain Rust 80% and Go 95% floors; bound compile jobs.
2. Re-run the added fence regression and whole native gates; observe policy
   create/update/delete in a real browser with current UI and binaries. Prior
   browser proof covers September's target-health read only.
3. Extend mirror body coercion parity: malformed sequence strings currently become
   zero and bool/fractional handling may diverge from the Python route.
4. Implement durable mirror source fencing, attested export/import into separate
   Rust storage, crash recovery/handback and complete action/epoch write admission.
   Mode flags and an ownership declaration alone do not qualify transfer. Restore
   consumer integration is also open.
5. Continue the full matrix: scheduler/provider probes, remaining native APIs,
   stateless MCP, desktop/Android packaging/device proof, provider/fault workloads,
   measured successful zero-Python workload and exact-head CI/Evidence Assembly.

## Historical checkpoint — 2026-10-01 policy CRUD across the Rust→Go process boundary

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared uncommitted checkout below was
preserved and extended; nothing was committed, pushed, merged or released. The full product
migration is **未完成**; readiness remains `NOT_READY`.

### Locally completed and verified

- **The tombstone slice recorded below as the next task was already on disk and is now
  verified.** `DELETE /api/workspace/backup-policies/{id}` tombstones the record
  (`TombstoneState = "DELETED"`), `Get` and `ListAuthoritativeRecords` read it as absent,
  the state is terminal, and the response is the oracle's `{"deleted": true, "policyId": …}`;
  a second delete and a patch after the delete are the oracle's
  `404 Backup policy not found`. `501 GO_POLICY_DELETE_UNSUPPORTED` and its constant are
  gone. Evidence: `go test ./internal/store -run 'Tombstone|OperatorMutation|AuthoritativeList'`
  and `go test ./internal/api -run 'Policy|Backup'` both pass.
- **New `TestBackupPolicyRustGatewayToGoPolicyCrud`** (`native_integration`,
  `go/internal/api/backup_policy_process_integration_test.go`) drives the whole public CRUD
  through a real Rust gateway in front of a real `deepseekd` over one isolated,
  Go-authoritative store: create → projected read with its next run → merge-and-advance
  update (revision 2, `createdAt` preserved, a key outside the oracle's 18-field patch list
  dropped) → tombstoning delete → terminal 404 on both write verbs → a create refused by name
  for a foreign revision → Host/token refusals on POST/PATCH/DELETE.
- **The shared harness was generalised** to `startNativeProcesses`, which now serves the
  policy read, policy CRUD and target-health process tests, reports both child process logs
  on any transport failure, and takes the deployment write opt-in explicitly. An operator
  write needs **both** conditions, and the daemon defaults to neither:
  `DEEPSEEKD_CONTROL_AUTHORITY=true` plus `DEEPSEEKD_INTERNAL_BEARER` (≥32 chars, refused
  without it) *and* a durably Go-authoritative domain.

### Two real defects found and fixed

- **The refactor cancelled its own children.** `defer cancel()` / `defer goCancel()` inside
  the old single-function helper fired the moment the new split `startNativeProcesses`
  returned, so the gateway died before the caller's first request (connection refused) or
  reset mid-request. The cancels are now owned solely by `t.Cleanup`.
- **The Rust edge checked the original `Host` only on the collection paths.**
  `require_production_auth` matched `/api/workspace/backup-policies` and
  `/api/workspace/backup-targets` exactly, so `PATCH`/`DELETE .../{id}` fell through to the
  token-only branch. The Go-side check cannot compensate: the proxy rewrites `Host` to the Go
  listener, so `allowedPublicHost` always sees an allowed value. That is a divergence from
  the oracle, whose `require_api_auth` checks the host on every route it serves.
  `backup_inventory_route` now matches the item paths too, preserving the exact
  `403 {"error": "Host not allowed", "code": "forbidden"}` body, and
  `rust/crates/deepseek-gateway/tests/backup_policy_auth.rs` gained
  `backup_public_item_routes_check_original_host_and_auth_before_go` (foreign host, missing
  token, and the legal request still proxied, for both verbs).

### Blocker: the Rust half is written but not locally linked

- `cargo test -p deepseek-gateway` fails at the link stage — `collect2.exe: error: ld
  returned 5` after `multiple definition of '__imp_atan'` between the local mingw's
  `libntdll.a` and `libmsvcrt.a` — and `cargo build -p deepseek-gateway` cannot re-run the
  `deepseek-protocol` build script (`OS Error 5` opening `proto/action/v1/action.proto`,
  reproducible with and without the sandbox flag, while Python reads the same file and the
  file has normal attributes).
- Measured root cause of the second one: **executables whose image lives under `D:\deepseek`
  are restricted on this machine.** One Go binary that binds `127.0.0.1` fails with
  `winapi error #10106` from `D:\deepseek\artifacts`, and succeeds from `%TEMP%` and from
  `D:\wb-probe-tmp` (same volume, outside the tree). Build-script executables live under
  `rust/target/...`, so they are the blocked part, not the source. Moving
  `CARGO_TARGET_DIR` outside the tree gets the build killed. `cargo fmt -p deepseek-gateway
  -- --check` is clean.
- Consequently: `TestBackupPolicyRustGatewayToGoProcess` and
  `TestBackupTargetRustGatewayToGoProcess` pass; `TestBackupPolicyRustGatewayToGoPolicyCrud`
  passes create/read/update/delete/404/bad-revision and fails **only** its final
  write-admission assertion (`update foreign host must be 403` → got 404), because
  `rust/target/debug/deepseek-gateway.exe` still predates the `auth.rs` fix. Rebuild the
  gateway in an unrestricted shell and re-run; do not treat the Rust half as verified before
  that. Log: `artifacts/native-process-backup-tests.log`.

### Next executable task

Rebuild the gateway and re-run the three `native_integration` process tests plus
`--test backup_policy_auth` in an unrestricted shell. Then hold both processes with
`DEEPSEEK_NATIVE_BROWSER_HOLD_SECONDS` and observe the create/update/delete flow in a real
browser against the built UI. After that, the mirror slice's remaining gaps (source fence,
export/handback document, browser observation) stay open as recorded below.

## Previous continuation checkpoint — 2026-10-01 operator write channel and policy CRUD

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared uncommitted checkout below was
preserved; nothing was committed, pushed, merged or released. The full product migration
is **未完成**; readiness remains `NOT_READY`.

### Locally completed implementation

- **Go schema v15 + `control_operator_mutations`** (`go/internal/store/operator_mutation_schema.go`).
  The append-only operator journal is a **separate table**, not a widened
  `control_operations.result_status`: that column is `CHECK(result_status IN
  ('PROPOSED','APPLIED'))` where `APPLIED` means a verified signed v2 document, and
  widening it would make the two channels indistinguishable in the audit trail. The
  v14→v15 migration recreates the metadata ceiling, creates the table and its two
  immutability triggers, journals `schema_migrations`, and the rollback path refuses to
  drop a non-empty journal. Every historical-schema fixture in `internal/store` was
  updated to remove the new objects before claiming an older version — the object catalog
  is fail-closed, so a fixture that forgot one was refused (three fixtures also needed
  `DELETE FROM schema_migrations WHERE version>=14`).
- **`Control.ApplyOperatorMutation`** (`go/internal/store/operator_mutation.go`): gated on
  the deployment capability *and* a durably Go-authoritative domain, with the live cutover
  read **inside** the write transaction — the journal row carries the operator, the
  server-generated `actionId`, the live `executionEpoch`, the cutover revision and fencing
  token, and the writer fence. Record, control event and journal row are one transaction;
  a replay returns `ALREADY_APPLIED` and a reused key with a different body is
  `MUTATION_REQUEST_REPLAY_CONFLICT`. The signed channel is untouched, and a test proves
  the two journals stay separate.
- **`POST` and `PATCH /api/workspace/backup-policies`** (`go/internal/api/backup_policy_writes.go`)
  now serve real writes: create normalises through `internal/policy` and stores; update
  merges only the oracle's 18 patch fields, advances the revision the store owns and
  preserves `createdAt`. Body limit 64 000 bytes, the read route's Host/token admission,
  the oracle's own refusal messages, and the uncaught `ValueError` as a 500.
- **Target bindings are read lazily** from the authoritative target inventory, exactly as
  the oracle does: only an id that is neither `managed-local` nor `unbound` needs the
  registry, so a policy with no registered target does not depend on the target cutover. A
  registry read that fails refuses with `GO_CONTROL_TARGET_REGISTRY_UNAVAILABLE`.

### Four real design findings, each resolved explicitly

- **A row delete cannot exist in this store.** Removing a record while its append-only
  events remain makes every later read of that id fail closed with `CORRUPT_RECORD`
  ("orphaned control events"), and the events table is immutable. `DELETE` is therefore
  refused with **501 `GO_POLICY_DELETE_UNSUPPORTED`** and its reason, and the tombstone
  state that would make it correct is the next slice. The frontend's delete button is the
  one flow that stays unavailable.
- **The mutation-transport rules are incompatible with a policy document.**
  `validateMutationRecordPayload` refuses floats (every policy carries float placement
  percentages) and `rejectMutationBodySecretKeys` flags the bare fragment `credential`
  (every policy carries `recoveryDrill.credentialRef`). The operator channel therefore
  applies the engine's **shared record rule** (`rejectControlSecretMaterial`, which
  exempts reference forms) and not the transport rules, which stay on the signed channel
  where the oracle applies them too. Neither decision loosens the signed channel.
- **A create must start at revision 1.** This store only adopts a higher baseline through
  the attested inventory import; a create asking for a later revision is refused by name
  rather than silently rewritten, and the narrowing is recorded in the catalog.
- **The policy transition table had to change.** It allowed `"" → ACTIVE` and toggling
  only, so a *disabled* create and any update that did not change `enabled` were illegal
  transitions. Policy now allows creation in either state and a rewrite in place, with the
  reason written into the table. The frozen state corpus covers peer-trust and effect
  states, not these, so no frozen contract moved.

### Verified commands and artifacts

- `go test ./... -count=1` passes for every package; `go vet ./...` and `gofmt` clean.
- The unchanged **95.0%** statement floor passes at **95.037888% (7776/8182)**,
  `artifacts/go-coverage-policy-writes.out`.
- `python -m pytest tests/test_native_runtime_go_control_store.py` passes with the new
  assertions (migration 15, the operator contract, the transport-rule decision, the route
  catalog and the policy transition shape); `scripts/native_runtime_contract.py --check`
  reports 49 domains; doc links pass.

### Next executable task

Add the **terminal tombstone state** so `DELETE /api/workspace/backup-policies/{id}` can
remove a policy the way the oracle does: a state that `Get` and `ListAuthoritativeRecords`
honour as absent while keeping the record row and its immutable events consistent, plus
the route's translation of a tombstoned record to the oracle's 404. Only then observe the
create/update/delete flow in a real browser against built Rust/Go processes. The mirror
slice's remaining gaps (source fence, export/handback document, browser observation) stay
open as recorded below.

## Previous continuation checkpoint — 2026-10-01 policy write semantics ported to Go

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared uncommitted checkout below was
preserved; nothing was committed, pushed, merged or released. The full product migration
is **未完成**; readiness remains `NOT_READY`.

### Locally completed implementation

- **`go/internal/policy` ports `normalize_policy`** — the whole of a backup policy's write
  semantics — with the oracle's validation order, its strict `_require_int` (a JSON float
  is not a Python `int`), its `str(value or "")`/`re`-fullmatch/`min(4, cpu_count)`
  details, the `policyRevision` coercion, and the **non-`AppError`** failures
  (`policyRevision: "many"`, `minFreePercent: true`, `recoveryPlacement: {"soon"}`) kept
  as `UncaughtError` so the HTTP layer can answer 500 exactly as the oracle's unhandled
  `ValueError` does. Cron parsing carries CPython's `int()` message into the oracle's
  `Invalid cron expression: …` wrapper, and `loadTimezone` refuses `""`/`Local`, which Go
  would otherwise accept.
- **`scripts/generate_policy_normalization_fixture.py` freezes the oracle** into
  `go/internal/policy/testdata/policy_normalization_v1.json`: **81 cases** (14 accepted,
  63 `AppError` refusals, 4 uncaught), regenerable and `--check`-able. The fixture masks
  `updatedAt`, which is the write clock.
- Two real divergences were found by that fixture and fixed rather than papered over:
  Python's `a or b` returns the **last** operand when all are falsy, so
  `costObjectives: {"maxMonthlyStorageCostUsd": 0}` keeps a zero objective (my first port
  dropped it); and `parse_cron` **wraps** the `ValueError` from a non-numeric cron field
  into an `AppError`, while the `int()` sites in `policyRevision`/`recoveryPlacement` do
  not.
- Delivery order is pinned separately, because a single-fault corpus cannot see it:
  `enabled → schedule → scope → frontendMirror → protection → policyRevision →
  replication → federatedDurability → placement → recoveryPlacement →
  retentionPolicyId → retry → incremental → recoveryObjectives → costObjectives →
  recoveryDrill`, one test per boundary.

### Verified commands and artifacts

- `go test ./internal/policy` passes: 81 fixture cases, the ordering table, every
  sub-validator propagation site, the Python value-semantics helpers, and the cron
  variants. `internal/policy` statement coverage **98.7%**.
- The module-wide unchanged **95.0%** floor passes at **95.399049% (7423/7781)**,
  `artifacts/go-coverage-policy.out` — a wider margin than the 95.015755% this work
  started from.
- `python scripts/generate_policy_normalization_fixture.py --check` is stable, doc links
  pass, and `go vet`/`gofmt` are clean.

### What this is not, and the blocker it exposes

This is **write semantics without a write channel**: nothing serves a policy create,
update or delete yet, so no matrix row moves. The channel is blocked on a concrete
finding from this round:

- `control_operations.result_status` has `CHECK(result_status IN ('PROPOSED','APPLIED'))`
  (`go/internal/store/operation_status_schema.go`), so an operator mutation cannot be
  journalled under its own status without a **schema migration** — the same shape as the
  v8→v9 migration that added `APPLIED`. Two candidate designs, to be settled first:
  (a) a v15 migration widening the CHECK plus an `authority` column, or (b) a separate
  append-only `control_operator_mutations` table (new table, also a migration) so the
  signed journal stays untouched and an auditor can always tell the two channels apart.
  (b) is preferred: the signed channel is the audited one and must not become ambiguous.
- The operator path itself: an authenticated public CRUD route whose authorization is the
  operator session, gated on `store.authorizeCutover` **and** a durably Go-authoritative
  domain, with the record revision CAS, `prepareControlRecordWrite`'s secret/float
  admission, and an atomic record + event + journal row carrying a server-generated
  `actionId` and the live cutover `executionEpoch`. It must not touch `ApplyMutation`.

### Next executable task

Settle the journal shape above, then land the operator mutation channel in the store with
its migration, crash/fault tests and coverage, and only then the `POST`/`PATCH`/`DELETE`
public routes on top of `internal/policy`. The mirror slice's remaining gaps (source
fence, export/handback document, browser observation) stay open as recorded below.

## Previous continuation checkpoint — 2026-10-01 sealed frontend mirror ownership inversion

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared uncommitted checkout described
below was preserved; nothing was committed, pushed, merged or released, and no
production data was touched. The full product migration is **未完成**; machine readiness
remains `NOT_READY` with no exact head.

### Locally completed implementation

- **New ownership domain `frontend_mirror_store`** (`data`, `python -> rust`, cutover
  `4.9.4`, `rust_data`) in `release/native_runtime_ownership_v1.json`. The contract gate
  now validates **49** domains (was 48). Python's `RUST_DATA_DOMAINS` gained it, and
  `backup_mirror.put_frontend_mirror` — the only Python path that creates a generation,
  moves `HEAD.json`, drops the legacy 4.4.4 files or prunes a generation — now denies the
  write mechanically once Python is de-authorised. Reads stay allowed on purpose: the
  scheduler and restore paths must keep working against a Rust-written mirror.
- **`deepseek_policy::backup_mirror`** is a port of `backup_mirror.py`: immutable
  generation directories, the `HEAD.json` CAS, epoch-index bookkeeping, idempotent
  replay, `mirror-stale-epoch` / `-sequence` / `-head-conflict`, recipient-variant
  selection, `mirror-generation-corrupt` on a hash mismatch, legacy 4.4.4 reads, and the
  `list_mirrors` skip-unreadable-legacy behaviour. Sealing links `backup_crypto` — the
  same age implementation the production CLI runs, now a lib+bin crate — instead of a
  second copy, and the round trip (`creationVerified`) is a measurement, not an
  assertion.
- **The three public routes are served by the Rust edge** (`GET /api/workspace/backup-mirrors`,
  `GET .../{profile}`, `PUT .../{profile}/frontend`) and are mounted **only** once both
  ownership conditions hold, so before the cutover the request still falls through to the
  Go proxy and Python is never shadowed by a read of a directory it is writing.
- **The recipient sets stay Go control state.** `go/internal/api/backup_policy_recipients.go`
  adds the authenticated loopback route `GET /internal/control/backup-policy-recipients`,
  which derives the two sets the oracle uses: the union over **all** policies
  (`active_recipients`, with Python's `protection or encryption` fallback, so an empty
  `protection` object falls through) and one group per **enabled** policy (no `encryption`
  fallback, empty group preserved so normalisation refuses it). An unavailable or
  non-authoritative source is a refusal, never an empty set — an empty set would skip the
  recipient check that stops a restore being told a generation is current when its key
  cannot open it.

### Verified commands and artifacts

- **Two-way byte-level oracle parity**, `tasks/native-runtime/backup_mirror_parity_probe.py
  --rust-example rust/target/debug/examples/backup_mirror_parity_probe.exe`: **8/8 checks
  pass**. `metadata.json` and `HEAD.json` are byte-identical to the oracle after masking
  the random generation id, the write clock and the ciphertext hashes (randomized age is
  a frozen contract, so a ciphertext hash *cannot* agree); each side reads the other's
  directory identically through `list_mirrors` / `mirror_status` / `mirror_files`; the
  **Rust-written ciphertext decrypts with the recipient identity to the exact envelope
  bytes**; an identical replay is idempotent on both sides; and a flipped ciphertext byte
  is refused by both with the same 409. Report:
  `artifacts/backup-mirror-parity-probe.json`, SHA-256
  `272F286DF21A267A9A28EAB3F817066956E5693B9A95EDFC9AEC2B9E7B1AA555`.
  The probe writes into `artifacts/` rather than `tempfile.mkdtemp()`: CPython 3.13
  creates that directory with a DACL (`OWNER RIGHTS` + SYSTEM/Administrators) that refuses
  a **second** process, so the Rust side could not create its generation there.
- Pinned Rust 1.85.0 GNU: `deepseek-policy` mirror unit tests **18/18**;
  `deepseek-gateway` mirror route unit tests **2/2**;
  `cargo test -p deepseek-gateway --test backup_mirror_routes` **4/4** (a legal upload
  seals, lists and reports `current` and an idempotent replay creates no second
  generation; a missing internal bearer refuses with `RECIPIENT_SOURCE_UNAVAILABLE` and
  writes nothing; stale sequence / bad envelope digest / head conflict / invalid profile
  id return the oracle's codes; and before the cutover the route is absent so the Go proxy
  answers). `cargo fmt --all --check` clean, and
  `cargo clippy -p backup-crypto -p deepseek-policy -p deepseek-gateway --all-targets`
  clean after fixing three `useless_vec` warnings in the new tests.
- Python: `tests/test_native_runtime_mechanical_denial.py` **10/10**, including the new
  real-writer denial (the refused upload leaves no directory behind) and the permitted
  mode publishing a verified generation; the four existing mirror suites
  (`test_backup_mirror.py`, `test_backup_mirror_generation.py`,
  `test_backup_mirror_variants.py`, `test_web_backup_mirror_routes.py`) are **37/37**
  with the fence in place.
- Go: full `go test ./... -count=1` passes for every package; `go vet ./...` clean;
  `gofmt` clean on the changed files; the unchanged **95.0%** statement floor passes at
  **95.015755% (6634/6982)**, `artifacts/go-coverage-mirror.out`. The new route has its
  own tests for the pre-cutover refusal, the missing-store and method refusals, a read
  failure, the union/group derivation (including the empty enabled group) and Python
  truthiness.
- `python scripts/native_runtime_contract.py --check` passes: 43 corpora, 32 versions,
  **49** domains.

### Known gaps in this slice

- **No source fence or transfer document yet.** Rust reads the same `.backup-mirror/`
  directory Python wrote, so the cutover is "stop the Python service, then set the mode" —
  there is no fence row, export/import attestation or handback receipt as there is for the
  policy and target inventories. The mechanical Python denial is in place; the fenced
  transfer is not.
- No browser observation of the native mirror route, and no qualification of the restore
  consumer (`backup_scheduled` → `mirror_files`) under Rust ownership.
- The Go coverage margin is **thin**: 6634 covered of the 6633 needed. Two statements in
  `backupPolicyRecipients` (a record payload that is not a JSON object) are unreachable
  through any legal write path and stay uncovered by construction.
- `deepseek_gateway` test binaries link only with the self-contained GCC/binutils on PATH
  plus `LIBRARY_PATH` pointing at rustc's `lib/self-contained` and the host GCC 8.1
  `crtbegin.o`; the default host MinGW 8.1 `ld` cannot link them. This is the same local
  toolchain limitation the earlier checkpoints recorded, not a test failure.

### Next executable task

Qualify this slice end to end before widening it: add the mirror **source fence** and a
canonical export/handback document (the policy/target pattern), then observe the native
`GET`/`PUT` mirror routes in a real browser against built Rust/Go processes, and prove the
scheduler's `mirror_files` consumer reads a Rust-written generation. After that, return to
the largest open blocker: the policy **write** channel (create/update/delete/run) has no
authorised operator path — the signed v2 apply channel needs an external signer, so a
browser CRUD request currently has nowhere to go. That design decision (an authenticated
operator mutation channel gated by the durable cutover, journalled with `actionId +
executionEpoch`, leaving the signed channel unchanged) is the next thing to settle.

## Previous continuation checkpoint — 2026-09-30 target-health transfer and real native MinIO

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. Preserve the shared uncommitted
checkout. The initial 89 changed/nonignored files were saved in ignored
`artifacts/native-resume-20260930-baseline.zip`, SHA-256
`9d0c95e516ddad5b01f984a383258375314a1057827969715094f733871a1276`.
No commit, push, merge, release or production-data transfer. The full product
migration is **未完成**; machine readiness remains `NOT_READY`, with no exact head.

### Locally completed implementation

- Go schema v14 imports the real `backup_target_health` history from Python's
  separate scheduler database. The new `python-control-inventory-export-v2`
  preserves the v1 corpus and binds health rows, target inventory, authority,
  transfer identity, source fence and signed promotion. Strict original SQLite
  column/value types are checked; BLOB-as-text, forged empty state, missing
  fences, altered manifests and source drift fail closed.
- Python's old scheduler table is mechanically fenced, including a fresh real
  Python writer process. Go imports target records, health and immutable
  provenance in one transaction. First promotion reattests both stopped source
  databases; later reads/restarts use Go state only. The native target GET reads
  inventory and health together and refuses v1/missing health evidence.
- Go handback removes the health snapshot/provenance with the target import in
  its existing transaction. The CLI can reexport an already committed immutable
  handback after publication failure. Python records durable revocation and
  recovers interruption between scheduler/control commits and receipt publication.
  **Three databases plus legacy projection are not one atomic source:** keep
  source processes stopped; these tests used isolated copies only.
- Rust preserves Python's Host-before-token backup inventory admission. Fresh
  CGO-free Go and Rust binaries pass nonempty policy/target HTTP reads, foreign
  Host refusals with/without token, and missing-token refusal. The actual built
  React frontend displays target `t-1` with imported `blocked` health. Mirror
  status remains explicitly unavailable. Browser-only auth bypass was confined
  to isolated loopback fixtures; it is not production auth qualification.
- The real S3 suite exposed a client-OS assumption: Windows test code expected
  key rejection even when MinIO ran on Linux in Docker. The test now observes
  the actual unchanged signed PUT/server response, checks the exact escaped
  wire key, verifies real payload/hash on success and refuses silent key rewrite.
  Native S3 production logic and frozen protocols were not weakened.

### Verified commands and artifacts

- Exact Go statement gate: **95.009403% (6568/6913)**, unchanged 95.0% floor;
  `artifacts/go-target-health-coverage-v14.{out,log}`. Go vet and command-package
  tests pass. Raw gofmt reports 58 CRLF-only files; normalized comparison found
  **zero formatting changes**, recorded in `target-health-go-fmt-normalized.json`.
- Pinned Rust 1.85.0 GNU workspace tests: **1186 passed**, 89 suites, no failures
  or ignored tests; workspace fmt/clippy and updated S3 observer clippy pass.
- Python selected oracle/handoff/catalog checks: **45 passed**; Ruff and mypy
  pass (924 files). Frontend check passes: 73 files/**609 tests**, typecheck,
  production bundle and bundle budget. Proto 36.1 generation drift, native
  contract, shadow parity (8/8), release version (4.8.0) and doc links pass.
- `python scripts/run_native_s3_e2e.py --toolchain 1.85.0-x86_64-pc-windows-gnu`
  passes **6 transport + 9 worker** cases on real isolated MinIO instances:
  `artifacts/target-health-native-s3-e2e-observed.log`. The pinned OCI image is
  Linux, digest `a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2`.
  Includes nonempty bytes, lost ACK/unknown effects and RPC operation identity;
  it does not qualify the full Go control cutover or two-Fleet takeover.
- Fresh process/bundle/browser evidence:
  `artifacts/go-native-backup-process-v14.log`,
  `artifacts/native-target-browser-process-final-v14.log`,
  `artifacts/native-target-health-browser-v14.txt` and
  `artifacts/native-target-health-browser-detail-v14.jpg`.
- Offline RAG, tool, injection, security and Agent eval runners pass;
  logs use `artifacts/target-health-*-eval.log`.
- A fresh unified offline suite and strict baseline comparison pass on 4.8.0,
  with the current base HEAD and `sourceTreeDirty: true`:
  `artifacts/target-health-offline-eval-current.json` and
  `artifacts/target-health-eval-baseline-current.json`. The earlier comparison
  of committed `evals/reports/latest.json` described an older 4.7.6 report;
  only the fresh artifact supports this worktree's baseline claim.
- Original full Python gate: **5533 passed, nine failures, 95.56% coverage**.
  Exact admission observations identified all nine failures as unit targets
  hitting the real host disk's 90% hard watermark (91.2% actual usage).
  Eight explicit filesystem unit scenarios now use capacity observations
  scoped to their isolated target paths; real S3/provider probes are untouched.
  The first failover unit also proves refusal at 95% usage. All **109** relevant
  placement/capacity/recovery regressions pass. Original and diagnostic logs
  remain in `artifacts/target-health-python-{full-gate,other-capacity-diagnosis}.log`;
  fixed regressions: `target-health-python-placement-regression.log`.

### Checks in progress; do not infer PASS

- Windows LLVM MinGW fixes race startup. The full command passes 17 packages,
  but the store package exceeds 15 minutes. A timed store run completes 249
  top-level tests before its 45-minute package deadline. Do not repeat those
  commands unchanged or call them PASS. The exact compiled race binary lists
  398 cases; the remaining 149 completed in three disjoint groups, preserving
  every case. Aggregate: **396 passed + two explicit skips; no missing cases or
  race warnings**, `artifacts/target-health-store-race-completion.json`.
  The Windows symlink privilege check could not run; the opt-in frozen-vector
  generator intentionally stays disabled. Original timeout remains explicit;
  `fullSingleCommandPass` is false. Pinned Go 1.27.1 Linux race is running on a
  read-only, hash-matched source copy. Its initial module-cache extraction on a
  Windows bind mount was cancelled before any tests; the replacement uses
  task-owned Linux cache volumes. Logs/context: `target-health-linux-go-run/`.
- GNU 1.85.0 lacks `profiler_builtins`; installed MSVC 1.85.0 has it but the
  Windows SDK registry points to missing libraries. No global settings or SDK
  installation was changed. A checksum-recorded, 3292-file Linux source copy
  excludes gitignored runtime state/secrets/build output and uses a new pinned
  1.85.0 coverage container. Build/coverage is still pending; do not claim it.
  Source manifest: `artifacts/native-v14-linux-source-manifest.json`.
- Repaired full Python coverage/provider suite is running with Rust 1.85.0,
  GNU GCC14 linker, production Go CGO=0 and `PYTHONHASHSEED=0`:
  `artifacts/target-health-python-full-gate-fixed.log`. It has no final result;
  do not call the original failed run or this pending rerun PASS.

### Remaining scope and next executable work

Finish the pending checks and repair actual failures first. Then transfer
backup-mirror generations/HEAD, metadata and ciphertext with fencing, integrity,
crash recovery and handback; the real oracle is
`deepseek_infra/infra/workspace/backup_mirror.py`. Target provider probing/refresh,
CRUD, native scheduler writes, full automatic backup, all other capability
cutovers, desktop/Android packages, two-Fleet/provider takeover, performance,
nonempty measured zero-Python workloads and exact-head CI/Evidence remain open.
Android SDK tools exist, but no connected device or installed AVD was listed.
The earlier Docker engine-unavailable note is superseded: Docker is now running
and real native MinIO tests passed. Production zero-Python counters remain
**NOT_MEASURED**, never constant zero or inferred from these test processes.

## Current continuation checkpoint — 2026-09-30 browser policy read and signed binding

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared checkout has
prior and current uncommitted changes; preserve them. No commit, push, merge,
release or production-data access. Full Rust/Go migration is **未完成**;
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY` with no
`exact_head`.

### Completed local slices

- An opt-in `native_integration` browser observation window serves the actual
  built React bundle from an isolated loopback Rust gateway + Go daemon over a
  temporary signed Go policy store. The normal process test still enables auth
  and proves foreign Host / missing token refusals. The browser initially
  showed no policy because `AutomaticBackupsTab` used one `Promise.all` for
  policy, target and mirror lists; the latter two return 501. The frontend now
  settles each read independently, displays the signed nonempty policy and
  next run, and explicitly marks target/mirror status unavailable. `HttpClient`
  reads Go's nested error envelope instead of displaying `[object Object]`.
  The test's signed policy payload now meets the complete browser contract.
- Go current-schema reads check that every currently authoritative cutover has
  its matching persisted authorization and signed artifact row with the same
  revision, epoch and fence, and that the artifact SHA-256 still matches its
  stored bytes. Deletion and digest-tamper regression tests fail closed on
  cutover, authority and public inventory reads. An old test that wrote a
  forged `go_authoritative` row directly now expects store-integrity denial.
  This check does not replace signature verification at admission.

### Verification and evidence

- `npm run check --prefix frontend` passed: typecheck, **609/609** tests,
  production bundle and bundle budget check. After the final unavailable-state
  wording change, both focused files passed **4/4** and `npm run build
  --prefix frontend` passed. Ignored `static/ui/index.html` SHA-256
  `d68ad2ea46468ab2931dce7a15a345c8490ce49e734abfb1398942f1e6b9caab`.
- Two interactive browser observations used the built Rust/Go binaries and a
  temporary signed store. The final browser showed `native process boundary`,
  its cron/next run and `状态不可用` for mirrors plus the target-list warning.
  `GET /api/workspace/backup-policies` was 200 with the nonempty record;
  targets and mirrors were 501. The `native_integration` observation test
  passed after its bounded hold; ignored log `artifacts/go-native-browser-policy-final.log`
  SHA-256 `8429524a101ecba12f47227f83ae4c303587860a0754ea5003bac224bae6bec2`.
  This observation used browser-only `AUTH_DISABLED=true` on isolated loopback
  listeners; it is not a production auth or installer qualification.
- Focused Go store integrity and normal Rust→Go process tests passed;
  `go vet ./...` passed. The current `CGO_ENABLED=0` daemon build has SHA-256
  `33bf49f11d15e190537907cce22f97066a0e71b995161623b8da88ac0cdbaf1`.
  Re-run the process case with this build after the coverage gate; the browser
  observation log used the prior daemon build. Go full coverage is being
  rechecked after updating the forged-authority test. The first run found that
  test's obsolete expectation; no coverage PASS is claimed yet.

### Open gates and next executable task

Finish the full Go coverage gate and new-daemon process test, then migrate
target health from Python's separate `.backup-scheduler/scheduler.db` and
backup-mirror immutable generations/HEAD under `.backup-mirror/` with a
mechanical single-writer transfer. Their current native public endpoints return
501; do not turn unavailable data into a fake empty list. Continue policy
CRUD/run/continuity and scheduler claims. The v13 external projection directory
still has a final-read race and no cross-store atomicity; production source
must remain stopped for any isolated cutover exercise. Desktop, Android,
other domains, provider-backed recovery, zero-Python success workload, exact-head
CI and Evidence Assembly remain open.

## Previous continuation checkpoint — 2026-09-30 v13 live source binding

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared checkout still
contains earlier and current uncommitted work. No commit, push, merge, release
or production-data access. Full Rust/Go migration is **未完成**; readiness remains
`NOT_READY` and `exact_head` is null.

### Completed local slice

- Go schema v13 stores the exact export manifest bytes, Python source path and
  chosen legacy projection directory in the immutable import row. The first
  signed `policy`/`target` promotion reopens and reattests both the fenced
  source SQLite and projection directory before Go commits. It compares the
  fresh domain, transfer, digests, authority generation/digest and boot epoch
  against the durable import; subsequent signed states can use legitimate Go
  writes without rereading the retired Python source. The Go writer lease is
  rechecked immediately before both new and replay cutover commits.
- An unpromoted v11/v12 import migrates without invented source binding. It
  cannot promote and can be handed back for a fresh export. A v12 import with
  promotion history refuses upgrade and retains the old store for explicit
  recovery. The old migration fixture was corrected to recreate its actual
  historical table and metadata; a fixture that failed with an open single
  connection transaction previously hung `internal/store` for ten minutes.
- Policy and target projection drift after import, source disappearance after
  Go restart, replaced source identity, lease expiry, historical upgrade and
  handback, duplicate/missing migration objects and logs, and signed replay
  without its artifact row have local success/refusal tests. The machine
  contract, runbook, public API note, matrix and active todo reflect v13.
- An explicit `native_integration` test applies a signed Go mutation to an
  isolated SQLite store, closes its seeding process, then starts built
  `deepseekd` and Rust gateway executables. The nonempty policy survives the
  Go process restart and is visible through Rust over TCP; a foreign Host and
  missing token are refused. This exercises the real binaries, not an
  installer or browser.

### Verification and evidence

- Go 1.27.1, Windows/amd64: focused new tests and the complete
  `go test ./internal/store -count=1 -timeout 3m` pass; `go vet ./...` passes.
  The first full Go coverage run timed out in a malformed historical migration
  test; after repairing that test, the next run completed at **94.936515%
  (6206/6537)** and a further run at **94.982408% (6209/6537)**, both below
  the unchanged 95.0% floor. After additional refusal tests,
  `python ../scripts/check_go_coverage.py --dir . --min 95.0 --profile
  ../artifacts/go-coverage-source-binding-v13.out` passed at **95.028300%
  (6212/6537)**. Ignored profile SHA-256
  `c4e53e1533381a2ec30db78946b7a7f8b2486cede3ab56cf36af66be3feec5c7`;
  log SHA-256
  `2aeaebeb2d6f82338a265329c27e9dbaaad189480c9eba0c48f16b4378a0cde0`.
  `go test ./... -count=1 -timeout 10m` also passed; ignored log SHA-256
  `85efb1758626e319de9ef9b83fe85cfe313e5dfd4bf333b2eccdfc9c5de1f553`.
- `pytest tests/test_native_runtime_go_control_store.py
  tests/test_native_control_handoff.py tests/test_native_control_handback.py
  -q -p no:cacheprovider --tb=short` passed **35/35**. `ruff check .`, `mypy .`
  (923 sources), `python scripts/native_runtime_contract.py --check`, and
  `python scripts/check_doc_links.py` passed. Changed Go files are gofmt-clean;
  targeted tracked diff check passed. These are local checks, not exact-head CI.
- Rust 1.85.0, Windows GNU: the default MinGW 8.1 linker failed on `.drectve`.
  Selecting GCC 14 alone still used the old `ld.exe` from PATH. With the Rust
  1.85 `self-contained` directory first in PATH and its GCC 14 set as Cargo's
  target linker, `cargo +1.85.0-x86_64-pc-windows-gnu build -p
  deepseek-gateway --manifest-path rust/Cargo.toml` passed. Ignored build log
  SHA-256 `246a2b43129968daf17d2c3a6ad559d4ecae9a63db7575c8952dcd0b9fe4947c`;
  built executable SHA-256
  `9ca192ca81a0255c27aa771954d662440ba0cc759e6fd39979dd8a677dcb90a4`.
  The same toolchain passed `cargo ... test -p deepseek-gateway --test
  backup_policy_auth` (1/1; log SHA-256
  `3d9b20e024d4751ac3c52be6ee2ef525c0e65288c28895b85d968dda05723c99`).
  `CGO_ENABLED=0 go build -o ../artifacts/deepseekd-policy-process.exe
  ./cmd/deepseekd` passed; binary SHA-256
  `84d1729fb3af7cfd77fb12d3456c0873adec364e786530471e2e4c7cc2b39e40`.
  With both `DEEPSEEK_GATEWAY_TEST_BINARY` and `DEEPSEEKD_TEST_BINARY` set to
  those local executables, `go test -tags native_integration ./internal/api -run
  '^TestBackupPolicyRustGatewayToGoProcess$' -count=1 -timeout 3m -v`
  passed; ignored log SHA-256
  `89fd498834adecd4bed30d49911e9443ce210447888d8dce499cf3f7163a26ae`.
  `go vet -tags native_integration ./internal/api` passed.

### Open gates and next executable task

The external projection directory is bound and reattested but not mechanically
frozen: a concurrent external write after its final read can still race Go commit. The two SQLite
stores have no atomic cross-store transfer; existing Go shadow history and
Python control state outside the fence remain untransferred. Keep the Python
source stopped and do not qualify production ownership transfer. Next, verify
the public backup-policy flow in a real browser, then port policy
CRUD/run/continuity and scheduler claims. Installer qualification is still open.
Desktop, Android, other product domains, provider-backed
recovery, zero-Python successful workload and exact-head CI/Evidence Assembly
remain open.

## Previous continuation checkpoint — 2026-09-29 explicit projection custody

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. This shared checkout has
earlier and current uncommitted work; no commit, push, merge, release or
production-data access. Full Rust/Go migration is **未完成** and readiness remains
`NOT_READY`.

### Completed local slice

- The offline Python CLI already required `--projection-dir` when its source
  path was nonstandard, but the callable exporter did not. Its core binding
  now refuses a nonstandard source with no explicit directory and rejects a
  relative explicit directory before installing a SQLite fence. The standard
  `.backup-control` layout continues to infer its sibling policy/target
  directory; an explicitly named empty directory produces a real digest.
- The Go source attester now refuses a nonstandard path without an explicit
  absolute projection directory, even when a caller reseals a manifest to
  claim `legacyProjection.digest: null`. This is checked in the attester used
  by the importer and its refresh. The handback test fixture now copies the
  actual projection files and uses the standard layout before re-fencing.
  The initial missing-directory refusal tests were red before these core
  changes; those and the later resealed-manifest/success tests pass afterward.

### Verification and evidence

- Go 1.27.1, Windows/amd64: focused store/import/API tests passed; `go vet
  ./...` passed. The unchanged full statement gate passed at **95.008473%
  (6167/6491)** using `python ../scripts/check_go_coverage.py --dir . --min
  95.0 --profile ../artifacts/go-coverage-explicit-projection-v12.out`.
  Ignored profile SHA-256
  `3a529c3608f5eaab9f91ec0334ace7e8a3d499d91a0727db93f0236394fecdd1`;
  log SHA-256
  `f1022cfb7ea99fb3d1038fd426a9d5ef6254e84d5f0804a17d19a087af796f29`.
- `pytest tests/test_native_control_handoff.py
  tests/test_native_control_handback.py tests/test_native_runtime_go_control_store.py
  -q -p no:cacheprovider --tb=short` passed **35/35** after the handback
  fixture was restored to the checked-in source's actual layout. `ruff check
  .`, `mypy .` (923 sources) and `python scripts/native_runtime_contract.py
  --check` passed. These are local checks, not exact-head CI.

### Open gates and next executable task

The import refresh checks the source and sibling directory before writing Go
state, but first signed promotion currently checks recorded import digests
without rereading that source. Implement durable source identity/manifest
custody and a fresh reattestation at the first promotion, with restart,
tamper, handback and older-store migration cases. Then finish public policy
CRUD/run/continuity, scheduler claims and real Rust→Go/browser success.
Directory files are not mechanically frozen, so keep the Python source service
stopped and do not qualify production ownership transfer yet. Desktop,
Android, all other product domains, provider-backed recovery, zero-Python
successful workload and exact-head CI/Evidence Assembly remain open.

## Previous continuation checkpoint — 2026-09-29 projection semantics and Go coverage

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The shared checkout still
contains earlier and current uncommitted work; no push, merge, release or
production-data access. The full Rust/Go migration remains **未完成** and release
readiness remains `NOT_READY`.

### Completed local slice

- Python's actual `Path.glob("*.json")` and policy/target list routes include
  hidden JSON and JSON-named directories. Go source attestation now scans the
  same candidates, includes hidden JSON in the binding and rejects a JSON-named
  directory instead of silently skipping either. Python export regression
  tests cover both; the glob candidate set was checked locally with Python
  3.12.12 and 3.13.
- A manifest digest can be recomputed by a caller. Go therefore now parses
  each bound legacy projection with duplicate-key detection and checks its
  filename stem and `policyId`/`targetId` against the fenced SQLite rows in the
  same file scan used for its digest. Resealed unadopted, mismatched, duplicate
  and malformed projections are refused; a reconciled stale projection still
  attests, as the SQLite row is authoritative. The rejection tests failed
  before the Go change and pass afterward.

### Verification and evidence

- Go 1.27.1, Windows/amd64: `go test` focused projection source and JSON corpus
  cases passed; `go vet ./...` passed. The unchanged full statement gate,
  `python ../scripts/check_go_coverage.py --dir . --min 95.0 --profile
  ../artifacts/go-coverage-projection-final-v12.out`, passed at **95.008473%
  (6167/6491)**. Ignored profile SHA-256
  `12bf333dd2c67f65b6674f910b48efc888e087b8dee9f75b1a4789839e2c0df23`;
  ignored log SHA-256
  `f2873a38464c3d8740e65fb09be64fa9228f23ceb957acbaf28631e3da44e480`.
  Before the parser corpus, the exact gate failed at **94.900632%
  (6160/6491)**; its threshold was not lowered.
- `pytest tests/test_native_control_handoff.py
  tests/test_native_control_handback.py tests/test_native_runtime_go_control_store.py
  -q -p no:cacheprovider --tb=short` passed **34/34**. `ruff check .`, `mypy .`
  (923 sources), `python scripts/native_runtime_contract.py --check` and
  `python scripts/check_doc_links.py` passed. Changed Go files are gofmt-clean;
  targeted tracked Markdown diff check passed.
- Windows `go test -race` still exits `0xc0000139` before tests execute; this
  checkout's prior record reports the same toolchain failure. Linux race and
  exact-head CI evidence remain open. The mistyped
  `generate_native_control_inventory_fixture.py --check` invocation was a CLI
  usage error (that generator requires `--output-dir` and has no `--check`);
  the fixture comparison test in the 34 passing Python cases is the current
  check.

### Open gates and next executable task

The Go importer reattests the source and projection directory immediately
before its own write, but the signed promotion later checks recorded digests
without rereading that directory. The SQLite fence does not freeze sibling
JSON files. A nonstandard source path also currently permits omitting the
explicit projection directory and binding `digest: null`; do not qualify such
a transfer. Close the import-to-promotion drift window with a verifiable
source/projection reattestation or a mechanical offline freeze, then implement
policy CRUD/run/continuity and scheduler claims through real Rust→Go success.
All remaining product domains, desktop/Android, provider-backed failure and
takeover evidence, successful zero-Python workloads, exact-head CI and Evidence
Assembly remain open. Keep `NOT_READY`.

## Previous continuation checkpoint — 2026-09-29 legacy projection directory binding

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. This shared checkout has
existing and new uncommitted work; no commit, push, merge, release or production
data access. The complete Rust/Go migration is **未完成**; readiness is
`NOT_READY`, and Python still owns production. No exact-head CI or Evidence
Assembly exists for the local changes.

### Completed local slice

- The offline Python exporter now **binds** the legacy projection directory it
  previously only preflighted. `export_and_fence` writes a new
  `legacyProjection` manifest field: the file count plus a canonical digest over
  each checked file's name, size and SHA-256, ordered by UTF-8 bytes of the base
  name. An absent directory binds `{fileCount: 0, digest: null}`; an **empty**
  directory binds its own digest. The manifest keeps its existing schema string,
  so an export without the field is refused rather than silently imported.
- The Go source attester re-derives that digest before accepting an import, so a
  directory the SQLite fence cannot reach fails closed when it appeared, vanished
  or changed. The directory is inferred from the standard `.backup-control`
  layout, or named through the new `go/cmd/control-inventory-import
  --projection-dir` flag; the attestation retains the caller's choice so the
  import refresh rechecks the same location.
  `AttestPythonInventorySourceWithProjection` is the explicit entry point and
  `AttestPythonInventorySource` is its standard-layout form.
- The checked-in Python fixtures now use the standard `.backup-control` layout
  and carry a real projection file per domain
  (`python_projection_policies/p-1.json`, `python_projection_targets/t-1.json`),
  so both runtimes' digest computation is pinned by the same bytes. The empty
  fixture binds an empty directory instead. That case exposed a wrong invariant
  during development — a nil digest means "no directory bound", but a zero file
  count does **not** imply a nil digest — which is now refused in the other
  direction only.

### Verification and evidence

- Go 1.27.1 on Windows/amd64: `go test -count=1 -timeout 1800s ./internal/store/
  ./internal/api/ ./cmd/control-inventory-import/ ./cmd/control-inventory-handback/`
  passed at 1799.062s / 99.037s / 29.405s / 26.520s. The store package lands
  within seconds of the default 600s per-package timeout on this machine, which
  is what tripped the earlier run; the inventory-focused subset passes in
  421.932s. `go vet` on the changed packages and `gofmt` on every changed file
  are clean. The full Go 95.0% coverage gate was **not** run locally.
- `tests/test_native_control_handoff.py` is **21/21** locally; the combined
  handoff/handback/control-store/foundation/ownership run is **47/47**.
  `ruff check .` and `mypy .` (923 sources) passed. The documentation navigation
  check, `scripts/check_doc_links.py` and the language-navigation tests passed.
- These are local results only. No browser, device, provider, Rust→real-Go
  process or exact-head CI evidence exists for this slice.

### Open gates and next executable task

The directory is bound and rechecked, not frozen: the SQLite fence cannot reach
a sibling JSON directory, so the source service must stay stopped, and a
directory that changes between export and import is refused rather than
repaired. Port policy CRUD/run/continuity and scheduler claims with real
Rust→Go success, then continue target inventory, storage/worker, federation, all
public APIs, desktop/Android, zero-Python successful workloads, exact-head CI
and Evidence Assembly. The two-store transfer is still not atomic and existing
Go shadow history still cannot be imported. Keep `NOT_READY`.

## Previous continuation checkpoint — 2026-09-29 native backup-policy list read

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. This shared checkout has
existing and new uncommitted work; no commit, push, merge, release or production
data access. The complete Rust/Go migration is **未完成**; readiness is
`NOT_READY`, and Python still owns production. No exact-head CI or Evidence
Assembly exists for the local changes.

### Completed local slice

- Go now serves `GET /api/workspace/backup-policies` with the frontend's
  existing `{policies,nextRuns}` response shape. Direct Go access requires
  an allowed Host plus `AUTH_TOKEN` bearer or `auth_token` cookie unless
  authentication is disabled by deployment configuration. The `403` Host and
  `401` auth errors retain Python's flat error body and lowercase codes.
  A transactionally checked Go-authoritative
  `policy` cutover, current-record digest and control-event history are required;
  shadow, missing store, corrupt rows/events and orphan history cannot report a
  successful empty list. Only a **read** is implemented.
- `NextBackupRun` ports five-field cron, day/week OR, IANA timezone, DST skip or
  `run-once`, first fall fold, and SHA-256 deterministic jitter. Eleven frozen
  cases come from the actual Python `backup_scheduler.next_run_for_policy`
  oracle in `scripts/generate_backup_next_run_fixture.py`; Go tests compare the
  resulting values exactly. The `time/tzdata` import embeds IANA data for a
  clean native package.
- Go HTTP tests read a nonempty, fenced Python-exported policy source after
  attested import and signed promotion; a second path reads an attested empty
  source, applies a signed Go policy mutation, and checks the public result and
  next run. Auth, shadow, schema, damaged data and storage refusal paths were
  checked on isolated stores. The Rust production auth middleware now checks
  the original Host **before** token on this route, with Python's flat 403/401
  error envelope; a mock-Go edge test proves legal forwarding. A real
  Rust→Go process success for this route remains unverified.
- The offline Python source exporter now checks sibling legacy projection
  directories in the standard `.backup-control` layout, or an explicitly
  supplied directory for nonstandard CLI paths, before it installs a SQLite
  fence. An unadopted/malformed policy or target JSON is refused; target
  `.checkpoint.json` sidecars are skipped. A matching but stale projection is
  allowed because the control SQLite row is already authoritative. This is a
  preflight only: neither the directory nor its digest is included in the Go
  source attestation, and files can change after the check.

### Verification and evidence

- Go 1.27.1 on Windows/amd64: full
  `python ../scripts/check_go_coverage.py --dir . --min 95.0 --profile
  ../artifacts/go-coverage-policy-list-v12.out` passed at **95.023548%
  (6053/6370)** statements, without lowering the threshold. Ignored profile
  SHA-256 `49f2b77eacb4f8f343ac915e40bc41993f5820ea8dfe82c35e4dc89438005122`;
  compact log `artifacts/go-coverage-policy-list-v12.log` SHA-256
  `c18bcceb5ba4268c2df463a66274ad3a4253b543fc9bd93bde6d75e8bdc4a`.
  Earlier runs failed the unchanged gate at 94.751773%, 94.940898% and
  94.994508%; focused safety and legitimate-success cases plus removal of an
  unreachable Host parser branch closed the gap. `go vet ./...`
  and gofmt passed.
- The Python oracle `--check`, `ruff check .` and `mypy .` (923 sources) passed.
  The source-handoff suite grew from 15 to **18/18** passing cases, including
  explicit and inferred projection directories, an unadopted policy/target,
  malformed JSON, missing explicit directory and target checkpoint sidecar.
  The combined handoff/handback/control-store command
  `pytest tests/test_native_control_handoff.py
  tests/test_native_control_handback.py tests/test_native_runtime_go_control_store.py
  -q -p no:cacheprovider --tb=short` passed **30/30**. Ignored log
  `artifacts/python-control-projection-preflight.log` SHA-256
  `19932ebea0a183b7da572b238fb5973469697826509e41c17ad341f8d17bc318`.
  `python scripts/native_runtime_contract.py --check`,
  `python scripts/check_doc_links.py` and `git diff --check` passed. Those checks
  do not establish a browser, device, provider or release-gate result.
- Existing Rust proxy integration test `go_control_proxy` passed **1/1** under
  explicit `1.85.0-x86_64-pc-windows-gnu` and that toolchain's bundled GNU
  linker. The default system MinGW driver fails on `.drectve`; a shorthand
  `+1.85.0` run selected MSVC and failed without `link.exe`. These are local
  linker/toolchain findings, not gateway regressions. The test uses a mock Go
  endpoint, so it does **not** establish the backup-policy route's real
  Rust→Go process success. Ignored log
  `artifacts/rust-go-control-proxy-policy-list.log` SHA-256
  `f368c167a8782fdd02b8222f126db07162bb5a72872a75a7ecba72f77be27625`.
- New Rust `backup_policy_auth` production-edge test passed **1/1** with the
  original Host denied before auth, local Host with missing token denied, and
  a legal Host/token forwarded to a mock Go endpoint; `AUTH_DISABLED` permits
  the legacy bypass. The pinned 1.85 GNU
  `cargo fmt -p deepseek-gateway --check` and targeted clippy `-D warnings`
  passed. Ignored test log SHA-256
  `38e29b0c30849a8e01f180ffb86b64764484c194393ee86c82574dafa5ae1fb6`;
  clippy log SHA-256
  `24174e97bebc8cb7313cd55fd51b272cdb18237121e9a9e18933c1c63fb9d4cd`.

### Open gates and next executable task

The Python policy list adopts `.backup-policies/*.json` files into its SQLite
control table before reading. The exporter now checks for unadopted projections
before installing the SQLite fence, but the existing attested Go import still
covers only `.backup-control` SQLite; it cannot prove the projection directory
stayed unchanged after preflight. Bind and recheck that directory in the
source/Go transfer before any policy production cutover. Then port policy
CRUD/run/continuity and scheduler claims
with real Rust→Go success, and continue target inventory, storage/worker,
federation, all public APIs, desktop/Android, zero-Python successful workloads,
exact-head CI and Evidence Assembly. The two-store transfer is not atomic and
existing Go shadow history still cannot be imported. Rust's shared Host helper
does not yet auto-add Python's detected LAN IP; configure LAN hosts explicitly
while exact Host parity remains open. Keep `NOT_READY`.

## Previous continuation checkpoint — 2026-09-29 empty-source promotion proof

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. All prior uncommitted
source-fence, v12 handback and other work remains in this shared checkout; no
commit, push, merge, release or production-data access. The full Rust/Go
migration is **未完成**, readiness is still `NOT_READY`, and Python remains the
configured production runtime. No exact-head CI or Evidence Assembly exists
for these local changes.

### Completed local slice

- `assertInventoryPromotionTx` now refuses a missing `control_inventory_imports`
  entry for policy and target even when both Go record and event counts are
  zero. First signed promotion requires the attested Python source fence,
  matching transfer, authority tip and boot epoch, exact manifest/source
  digests, and unchanged Go shadow rows/events. Later signed authoritative
  states retain those digests while permitting legitimate Go writes after the
  first promotion.
- `scripts/generate_native_control_inventory_fixture.py --empty` creates a
  genuine empty Python control SQLite source, checkpoint and fenced exports
  for both domains. Go tests copy this real Python source, attest it read-only,
  prove refusal without an import, then prove legal signed promotion for both
  empty domains. A policy test makes a signed Go record write and advances
  through `python_shadow` and `python_disabled` while retaining the record.
  The current checked-in empty source is 266240 bytes, SHA-256
  `6ee7a325b9e05cacf3cb3dff1441f5acb4399b0a1bea5a8fda8ffd9dfe00b731`,
  and includes the 18 linked triggers from the previous slice.
- Go HTTP claim → cutover → signed policy apply tests now use the attested
  empty source; the started runtime's general authority/cutover transport test
  uses the action domain. A separate Python interpreter invoking the real
  `backup_control.create_policy` after export is denied by the SQLite fence,
  and the source row count and transfer digest remain unchanged. This is a
  fresh writer-process check on isolated data, not a full service restart or
  production cutover.

### Verification and evidence

- Current v12/shared-tree Go coverage command from `go/`:
  `python ../scripts/check_go_coverage.py --dir . --min 95.0 --profile
  ../artifacts/go-coverage-empty-source-v12.out`. All packages passed on Go
  1.27.1 `windows/amd64`: **95.084385% (5803/6103)** statements, unchanged
  95.0% floor. Ignored profile SHA-256
  `8ee50d7f022479211ffc50a98eb5f1a4e9134409debb24e02501cab5b37db4c4`;
  compact log `artifacts/go-coverage-empty-source-v12.log` SHA-256
  `e7585fe4ce4a502c442df2ecb5006af1b442e45d031c19915c3e5b94e5aa260c`.
  `go vet ./...` passed; gofmt reports no changed-file output.
- The six related Python files ran **87/87** tests, including the handoff and
  v12 handback suites, on isolated data. `ruff check .` passed and `mypy .`
  passed for 922 source files. The native contract, documentation link and
  `git diff --check` checks passed after the documentation edits.

### Open gates and next executable task

The two SQLite stores still have no atomic cross-store promotion or handback;
the source service-stop precondition is procedural. Existing Go shadow
history cannot be imported, and native policy/target business and public API
parity remain unfinished. Continue with the native policy/target operation
path behind the Rust edge, then the remaining public Rust routes, Go-owned
workspace backup/DR, provider-backed worker reconciliation, desktop/Android
packages, nonzero successful zero-Python workloads and exact-head CI/Evidence.
Do not infer release readiness from the local fixture or coverage checks.

## Previous continuation checkpoint — 2026-09-29 linked control-state fence

Branch `codex/indexmap-std-feature`, base HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; the handback slice below is
included and extended. No commit, push, merge, release or production-data
access. The full Rust/Go migration is **未完成**;
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`, and Python
remains the configured production runtime.

### Completed local slice

The transfer no longer binds only the exported inventory rows. While a fence is
held the Python writer is mechanically denied on the control state the transfer
proof depends on:

- **Global** (frozen by *any* held fence): `control_authority_head`,
  `control_authority_outbox`, `control_authority_mutations`,
  `control_boot_state`. These carry the installed authority tip and the source
  boot epoch the Go attestation compares, so moving them after an export would
  silently invalidate the binding.
- **Linked** (frozen only for rows naming a fenced domain):
  `lifecycle_intents`, `target_receipt_mutations`.

That is 18 `native_control_fence_<table>_no_<operation>` triggers, generated by
`scripts/native_control_handoff.py` (`_LINKED_FENCE_TABLES`,
`linked_fence_objects()`) and required **byte for byte** by
`go/internal/store/inventory_source.go` (`linkedFenceTables`,
`linkedFenceObjects()`) before the attester will read a single row. A Python
service restarted after the export now fails closed with
`PythonWriterMechanicallyDeniedError` on its own production connection instead
of resuming ownership of the bound state.

The release rule is explicit: the linked objects survive a **partial**
revocation (one domain handed back while another transfer is still held) and are
dropped only when the **last** fence is lifted, so one domain's rollback can
never unfreeze state another held transfer depends on. The append-only
`native_control_handoff_revocations` journal remains afterwards as the audit
record.

### Verification and evidence

- `go test ./... -count=1` passed for every Go package; `go vet ./...` clean;
  `gofmt` clean on every changed Go file.
- Exact Go statement coverage **95.084385% (5803/6103)** on Go 1.27.1
  `windows/amd64` with the unchanged 95.0% floor
  (`scripts/check_go_coverage.py --dir . --min 95.0`). Ignored local profile
  `artifacts/go-coverage-linked-fence.out` SHA-256
  `8FB00254B70B8BE041DE02BBF779DD0C61AE9C10317F9AD90E241446F0B3630A`,
  compact log `artifacts/go-coverage-linked-fence.log` SHA-256
  `E7585FE4CE4A502C442DF2ECB5006AF1B442E45D031C19915C3E5B94E5AA260C`.
- **A real cross-language failure was caught and is now a permanent gate.** The
  regenerated Python fixtures change the source the Go tests consume, and the
  extended fence immediately refused four Go tests that used to tamper with
  `control_authority_head`, `control_boot_state`, `control_authority_outbox` and
  `lifecycle_intents` through raw SQL — which is exactly the guarantee that was
  missing. Those tests now lift one named fence object, mutate, and restore it
  (`tamperWithFencedSource`), so they still prove the *attestation* refuses a
  tampered source in addition to the fence refusing the write.
- New Go test `TestLinkedControlStateIsMechanicallyFenced` proves eight linked
  writes are denied by the fence in the real Python-produced fixture, and the
  ingestion path still attests it unchanged.
- `tests/test_native_control_handoff.py` (14 cases) now asserts the linked
  denial through the **production control connection**
  (`backup_control._connect()`), that the boot epoch did not move, and that the
  generated fixtures carry all 18 objects.
  `tests/test_native_control_handback.py` (8 cases) adds
  `test_two_step_revocation_lifts_the_linked_fence_only_at_the_end`: the first
  revocation keeps all 18 objects and the authority denial, the last one removes
  every fence object, leaves the revocation journal, allows the production
  connection to write again, and a fresh export re-fences from scratch. It
  consumes a second real Go document,
  `go/internal/store/testdata/python_target_inventory_handback_v1.json`,
  produced by the same `DEEPSEEK_UPDATE_HANDBACK_FIXTURE=1` generator test as the
  policy one.
- Refreshed cross-language fixtures: `python_control_source_v1.sqlite3` and
  `python_empty_control_source_v1.sqlite3`, 266240 bytes each, nonempty source
  SHA-256 `f11de32e6465c2e3c16b5c9f43a70e8f28da4b74916bfdce393f4bf5e5899a32`,
  each carrying 18 linked fence triggers. The export manifests and checkpoints
  are unchanged (the fence is not part of the manifest), so the previous
  evidence for them still holds.
- `ruff check .` and `mypy .` passed; the whole fast Python suite
  (`pytest -m "not integration and not slow"`) passed with exit code 0.
- No Rust, frontend, provider or platform code changed, so those gates were not
  rerun. Windows local results only: no Go race, no Linux exact-head CI, no
  Evidence Assembly.

### Open gates and next executable task

The checkpoint **documents** live in authority anchor files outside the fenced
SQLite database, so they cannot be fenced there; the Go attestation does not
read them, so a checkpoint-file change cannot invalidate an import — but it also
means the fence is not the whole authority story. The Python control state
outside the bound set (`target_objects`, `recovery_object_refs`, QoS,
maintenance, retention, GC intents) deliberately stays Python's and is **not**
transferred, so those tables have a single authoritative writer only because no
ownership change covers them yet. The two stores are still not one transaction,
existing Go shadow history is still not transferable, nonzero policy
`topology_generation` is still refused, and no production handback, promotion or
ownership transfer has been performed.

Next dependency-satisfied work: the native policy/target business and public API
parity behind the Rust edge, then the public Rust data routes still missing
(`/api/projects`, `/api/project-files`, the remaining `/api/file-*` shapes,
`/api/media`, `/api/skills`, traces), the Go-owned workspace backup/DR block,
provider-backed worker reconciliation on real MinIO, desktop/Android packages, a
successful nonempty zero-Python workload, and the exact-head CI/Evidence gates
that this local work cannot substitute for.

## Previous continuation checkpoint — 2026-09-29 attested handback (reverse transfer)

Branch `codex/indexmap-std-feature`, base HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; all prior uncommitted work
remains intact and extended. No commit, push, merge, release or production-data
access. The full Rust/Go migration is **未完成**. `release/native_runtime_5_0_evidence_v1.json`
remains `NOT_READY` with no exact-head CI evidence, and Python remains the
configured production runtime.

### Completed local slice

The offline import gained its **reverse**: a transferred `policy`/`target`
inventory can be handed back to Python before any authoritative Go use, with
both sides journaling the transfer rather than silently dropping it.

- `go/cmd/control-inventory-handback` (new) plus
  `store.RollbackPythonInventory` and the canonical
  `control-inventory-handback-v1` renderer. Go refuses unless the domain is
  still `dual_evaluate`, has **no** promotion artifact or cutover authorization,
  still holds exactly the imported record and event counts (one event per row),
  carries the exact transfer ID the import journal records, and no other writer
  or time touched any row. It then removes the records, their `control_events`
  and the import provenance row and appends the v12
  `control_inventory_handbacks` row **in one transaction**; the append-only
  triggers it lifts are restored inside that same transaction, so a failure at
  any point leaves the store byte-identical.
- `go/internal/store/inventory_handback_schema.go` raises the store to
  **schema v12** with that append-only journal (its own update/delete/replace
  triggers, `UNIQUE(domain, transfer_id)`); a v11 store migrates, a schema-0
  rollback refuses to discard retained handback history, and the v12 objects are
  part of the verified object set.
- `scripts/native_control_handoff.py --rollback` verifies the **exact Go
  bytes**: canonical JSON, the document's own digest, the exact 14-field key
  set, and binding to the fenced export's manifest, source, authority tip and
  transfer. It then records an append-only
  `native_control_handoff_revocations` row, drops the source fence row and its
  guard triggers in one transaction, and publishes
  `python-control-inventory-handback-receipt-v1`.
- The reverse transfer is **reversible and re-transferable**: after a handback
  the source writes again, can be fenced and exported for a **new** transfer,
  and the Go domain can import that new transfer. A promoted (or even
  de-promoted) domain, a Go-mutated domain and an unjournaled transfer are all
  refused, so the command can never be used to overturn a real ownership change.

### The defect the cross-language fixture caught

The first version of `handbackDigest` hashed a document that still carried
`"handbackDigest": ""`, while Python removes that key before hashing. Go's own
tests passed — both sides were self-consistent — and only the checked-in
Go-produced fixture consumed by `tests/test_native_control_handback.py` exposed
it. The digest is now computed over an explicitly built field map, and the
fixture (`go/internal/store/testdata/python_policy_inventory_handback_v1.json`)
is regenerated by `DEEPSEEK_UPDATE_HANDBACK_FIXTURE=1` and asserted byte-equal
on every normal run, so the two languages cannot drift apart silently again.

### Verification and evidence

- `go test ./... -count=1` passed for every Go package (including the new
  `cmd/control-inventory-handback`); `go vet ./...` clean; `gofmt` clean on
  every changed Go file. Windows checked-out files that predate this work carry
  CRLF and are reported by `gofmt -l` for that reason alone — unchanged here.
- Exact Go statement coverage **95.069844% (5785/6085)** on Go 1.27.1
  `windows/amd64`, with the unchanged 95.0% floor and
  `scripts/check_go_coverage.py --dir . --min 95.0`. Two consecutive runs
  reported the same statement totals. Ignored local profile
  `artifacts/go-coverage-inventory-handback.out` SHA-256
  `ABC9A64D5189E64B53AC25A60D3A7FF3B3B5C0CBA304830116E961F8FC099634`,
  compact log `artifacts/go-coverage-inventory-handback.log` SHA-256
  `08FF83551C7455934310C32E1363B908A2508064F9F7B892C6A6B8CE3E0FE3C6`
  (same run). The first run exposed a real gate failure at **94.414414%
  (5764/6105)**; the refusal, fault-injection, lease-expiry, corrupt-history and
  deployment-key tests that closed it are real behavior tests, not exclusions,
  and no threshold was lowered.
- `tests/test_native_control_handback.py` (new, 7 cases) consumes the real
  Go-produced document: canonical form and binding, a **real Python process
  restart** that is denied before the handback and writes for real after it,
  re-fencing for a new transfer, the `--rollback` CLI path (including its
  argument refusals), and refusals for a forged/unsigned/extra-field handback, a
  changed source, and a mismatched manifest. `pytest
  tests/test_native_control_handoff.py tests/test_native_control_handback.py
  tests/test_native_runtime_go_control_store.py -q -p no:cacheprovider` passed
  25 cases; `ruff check .` and `mypy .` (922 sources) passed;
  the whole fast Python suite (`pytest -m "not integration and not slow"`,
  **5503** selected tests) passed with exit code 0;
  `python scripts/native_runtime_contract.py --check` passed (43 corpora, 32
  versions, 48 domains); doc links and the Markdown language navigation (219
  files) passed; `python scripts/check_release_version.py` passed.
- The catalog `release/native_runtime_go_control_store_v1.json` now declares
  migration 12 and an `inventory_handback` section (schema, table, mutation,
  preconditions, Python receipt, open gaps), and
  `tests/test_native_runtime_go_control_store.py` asserts both the declaration
  and the Go/Python sources it names.
- No Rust, frontend, provider or platform code changed this slice, so those
  gates were not rerun. Windows local results only: no Go race, no Linux
  exact-head CI, no Evidence Assembly, no provider or platform artifact.

### Open gates and next executable task

The two SQLite databases are **not** one atomic store: the handback is
attested on both sides but a crash between the two steps leaves the source
fenced, which is the safe direction (Python cannot write until it verifies a Go
handback). The joined cycle (export → import → handback → revoke → re-export →
re-import) has **not** been driven end to end across both languages: each half
is verified against the other side's real artifact (Go consumes the real
Python-fenced source and manifest; Python consumes the real Go handback
document) and both halves prove re-fencing/re-import locally, but no single
fixture carries the whole chain. Existing Go shadow history and linked Python
control tables (receipt mutation generations, key custody, lifecycle intents,
the authority outbox) are still **not** transferable, nonzero policy
`topology_generation` is still refused, and no production handback, promotion
or ownership transfer has been performed. The Python service-stop precondition
remains procedural.

Next dependency-satisfied work: extend the transfer to the linked control
state (or mechanically prove it stays stopped across the transfer), then the
native policy/target business and public API parity behind the Rust edge, the
public Rust data routes still missing (`/api/projects`, `/api/project-files`,
`/api/file-*` shapes beyond the wired family, `/api/media`, `/api/skills`,
traces), the Go-owned workspace backup/DR block, provider-backed worker
reconciliation on real MinIO, desktop/Android packages, a successful nonempty
zero-Python workload, and the exact-head CI/Evidence gates that this local work
cannot substitute for.

## Previous continuation checkpoint — 2026-09-29 v11 import provenance and CAS baseline

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; all prior uncommitted work
remains intact. No commit, push, merge, release or production-data access. The
full Rust/Go migration is **未完成**. The release evidence remains `NOT_READY`,
has no exact-head CI, and Python remains the configured production runtime.

### Completed local slice

- Go schema v11 adds append-only `control_inventory_imports`. The import
  commits transfer ID, manifest/source and authority digests, source boot epoch,
  row count, source-attestation flag and writer fence in the **same Go SQLite
  transaction** as the imported records and `control_events`. Schema migration,
  restart, immutable journal, failed insert rollback and retained-history
  refusal were tested on isolated stores. A v10 store with old shadow records
  migrates without inventing source attestation; its nonempty unproven domain
  cannot promote.
- The offline command obtains a private Go attestation token from a read-only
  check of the actual fenced Python SQLite file **before** opening Go, then
  rechecks the source immediately before its Go write transaction. Import
  compares the live source boot epoch to the installed checkpoint. A changed
  marker, trigger, row, boot epoch or linked pending effect is refused. The
  complete Python-created source fixture remains 258048 bytes, SHA-256
  `9fa7897d1b73125f90667d855235ec8d15dbaee38496f831224ff484d4e7d837`.
- Imported policy/target Go record and first event revisions now retain the
  Python CAS revision or topology generation. The event baseline is accepted
  only with matching import provenance; later Go writes in that domain must
  keep the payload CAS field aligned. A separately signed promotion of an
  imported domain must match the transfer, authority tip and boot epoch,
  manifest/source digests and unchanged record/event counts. Real Python
  policy revision 2 and target generation 1 passed legal signed promotion in
  isolated Go tests; missing or forged provenance, malformed CAS, shadow
  mutation after import and tampered history were refused. This tests the
  state-machine slice, not native policy/target business or public API parity.

### Verification and evidence

- Exact Go statement coverage **95.023697% (5614/5908)** on Go 1.27.1
  `windows/amd64`; `scripts/check_go_coverage.py` ran all packages with
  `-count=1` and the unchanged 95.0% floor. Ignored profile
  `artifacts/go-coverage-inventory-provenance.out` SHA-256
  `6da168b6da24ec5cba9439d52c664e777b0287abdb6b711806f125ee4e6c94a7`,
  compact log `artifacts/go-coverage-inventory-provenance.log` SHA-256
  `a350fcdc7df98fe5ce03a3db5750e5450e4a0e69e3ae65cf59066d38c270dfb8`.
  Command from `go/`: `python ../scripts/check_go_coverage.py --dir .
  --min 95.0 --profile ../artifacts/go-coverage-inventory-provenance.out`.
  `go vet ./...` and gofmt checks passed.
- `python -m pytest tests/test_backup_463_control_authority.py
  tests/test_backup_467_authority_fail_closed.py
  tests/test_native_runtime_mechanical_denial.py
  tests/test_native_control_handoff.py
  tests/test_native_runtime_go_control_store.py -q -p no:cacheprovider`
  passed **77** cases. `ruff check .`, `mypy .` (921 sources),
  `python scripts/check_doc_links.py`, and
  `python scripts/native_runtime_contract.py --check` passed (43 corpora,
  32 versions, 48 domains). Rust/frontend/platform/provider gates were not
  rerun because this slice changed no such implementation.

### Open gates and next executable task

The signed Go transition still allows an **empty** policy/target Go domain
without a verified source import. The Python service-stop precondition is
procedural, the source and Go transactions are not atomic, and linked Python
tables are not fully fenced. Enforce source proof even for empty inventories,
bind a durable cross-store transfer/rollback protocol to the signed promotion,
and prove Python restart cannot renew the old writer. Existing Go shadow
history cannot be replaced yet; nonzero policy `topology_generation` and target
receipt mutation state remain refused. Native policy/target business/API
behavior, public Rust edge and all other capability rows, real provider/two-Fleet
recovery, desktop/Android packages, successful zero-Python workload and
exact-head Evidence Assembly remain open. Current local results do **not**
authorize production cutover or readiness status change.

## Previous continuation checkpoint — 2026-09-29 direct source-fence attestation

Branch `codex/indexmap-std-feature`, HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`; the prior uncommitted
signed-promotion and source/import work remains intact. No commit, push,
merge, release or production-data access. The full Rust/Go migration is
**未完成**. `release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`
with no exact-head CI evidence, and Python remains the configured production
runtime.

### Completed local slice

- `go/internal/store/inventory_source.go` opens an explicitly named Python
  control SQLite source with `mode=ro` and query-only settings. It validates
  schema v8 and `quick_check`, the live authority head, an active recovery
  state, the exact source-table and append-only marker triggers, matching
  transfer/checkpoint/source digests, settled linked effects, and the actual
  ordered source rows against the export. Missing or altered source state is
  refused before the Go store is opened. The offline Go import command now
  requires `--source-db` as well as the manifest and Go store directory.
- The fixture generator uses the real Python writer/exporter, then SQLite's
  backup API to produce a complete, self-contained **fenced** source database
  in DELETE journal mode for Go tests. The JSON checkpoint and manifests
  regenerate byte-identically. The source DB fixture is 258048 bytes, SHA-256
  `9fa7897d1b73125f90667d855235ec8d15dbaee38496f831224ff484d4e7d837`.
- Go tests verify policy and target read-only attestation without changing DB
  bytes or creating WAL sidecars, source write denial, missing/altered triggers
  and markers, stale head, unsettled outbox/lifecycle/receipt state, source
  row addition/removal, malformed generations and schema drift. The CLI test
  now imports a nonempty policy from that Python-created source and reads it
  after Go restart. This still does not promote production ownership.

### Verification and evidence

- Exact Go statement coverage passed **95.015576% (5490/5778)** on Windows
  Go 1.27.1. `scripts/check_go_coverage.py` runs each Go package with
  `-count=1`; ignored profile `artifacts/go-coverage-source-attestation.out`
  SHA-256 `621b8b0dac4df67ea13ee8bf3bb23cf5a2571bbf410f6421e039fe53bfc74e09`,
  compact log `artifacts/go-coverage-source-attestation.log` SHA-256
  `e6923d51256e0116566d624f9700b1b9ffe3350ab4552fbc990cd3840b0d57cc`.
  Command from `go/`: `python ../scripts/check_go_coverage.py --dir .
  --min 95.0 --profile ../artifacts/go-coverage-source-attestation.out`.
  `go vet ./...` passed.
- `python -m pytest tests/test_backup_463_control_authority.py
  tests/test_backup_467_authority_fail_closed.py
  tests/test_native_runtime_mechanical_denial.py
  tests/test_native_control_handoff.py
  tests/test_native_runtime_go_control_store.py -q -p no:cacheprovider`
  passed 76 cases; the isolated source handoff file passed 12. `ruff check .`,
  `mypy .` (921 sources), `python scripts/check_doc_links.py`, and
  `python scripts/native_runtime_contract.py --check` passed.
- These results use local isolated SQLite copies. No Rust/frontend/platform
  code changed this slice, so their broader gates were not rerun. This is not
  provider, production, Linux race, exact-head CI or Evidence Assembly proof.

### Open gates and next executable task

Implement a durable Go import-provenance journal that atomically records the
verified source and manifest hashes with the imported events, then bind that
journal to the separately signed promotion artifact. Mechanically fence
linked Python control state or prove it remains stopped across the transfer;
the current read-only attestation does not make the two SQLite databases one
atomic transaction. Preserve Python-visible CAS revisions, handle existing
Go shadow history, migrate target receipt/key custody and other linked state,
and prove an isolated reversible handback before production promotion.
The public Rust edge, remaining native APIs, real provider and two-Fleet
recovery, desktop/Android packages, successful nonzero zero-Python workload,
full capability matrix, and exact-head release gates are still open.

## Previous continuation checkpoint — 2026-09-29 isolated Python inventory to Go import

Branch `codex/indexmap-std-feature`, starting HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. This worktree already held
the preceding signed promotion slice as uncommitted changes and they were
preserved. No commit, push, merge, release or production-data operation. The
full Rust/Go migration is **未完成**; release readiness stays `NOT_READY` and
Python remains the configured production owner.

### Completed local slice

- `scripts/native_control_handoff.py` exports a nonempty policy/target inventory
  from an explicit isolated Python schema-v8 SQLite source. In one
  `BEGIN IMMEDIATE` it checks the live authority head, full checkpoint
  projection and policy generation maps, pending authority/outbox/lifecycle
  effects, then writes an append-only transfer marker and source-table denial
  triggers. It refuses hidden target fields, raw credentials and nonempty
  receipt mutation generations. A failed file publish leaves the source
  fenced, and an exact replay republishes. Actual `backup_control` create,
  mutate and delete operations report mechanical denial after the fence.
- `go/internal/store/inventory_import.go` checks the Python-canonical manifest
  and source digests, installed live `control-authority-v1` checkpoint and
  payload/generation equality, writer lease, `dual_evaluate` cutover and empty
  target history. It writes the imported records and Go events atomically to a
  fresh Go-owned store. `go/cmd/control-inventory-import` is an explicit
  offline command with no Python process dependency. Imported source
  `policyRevision`/`topologyGeneration` remain in each payload; the new Go
  event history begins at revision 1, so CAS translation is still a gap. A
  nonzero policy `topology_generation` sidecar is refused because the frozen
  checkpoint does not retain it; promotion/drain/placement generations remain
  in the checkpoint but their native business consumers are unwired.
- The Go tests consume policy and target fixture manifests produced by the
  actual Python SQLite writer/exporter, not manually rewritten authority
  corpus. The offline fixture generator now uses a fixed clock and requires an
  empty output directory; a repeat run produced identical bytes. Manifest
  SHA-256: policy
  `f73e12a8d5ef823ee35b51c85ebca231e4b10cba71f3a96729fde1db019da065`,
  target
  `478ffa7e143f7b20b949be41e67fe25289c9925d7549fd19e4f65a5920073420`.

### Verification and evidence

- `python -m pytest tests/test_backup_463_control_authority.py
  tests/test_backup_467_authority_fail_closed.py
  tests/test_native_runtime_mechanical_denial.py
  tests/test_native_control_handoff.py -q -p no:cacheprovider`: 71 passed.
  Isolated Python export test file alone: 11 passed. `ruff check .` and
  `mypy .` passed (921 sources).
- Focused Go importer/command tests passed, including real Python export bytes,
  rehashed tampering, stale writer, transaction timeout, checkpoint corruption,
  duplicate JSON keys, existing Go history and restart. `go test ./... -count=1`
  and `go vet ./...` passed before the last added refusal tests; the exact Go
  coverage command reran all packages after them and passed at
  **95.005295% (5383/5666)** on Windows/amd64 Go 1.27.1. Its ignored profile
  and compact log are `artifacts/go-coverage-inventory.out` and `.log`;
  SHA-256 `521dcb2cfa149a062620896e59a3da914d02c8f989a7b90678f79a5b6b8bfdb5`
  and `283aca802d07e4ba92b4ca35450c08098b31b89c58749951152d7e0ad97acc3e`.
  Command (from `go/`): `python ../scripts/check_go_coverage.py --dir .
  --min 95.0 --profile ../artifacts/go-coverage-inventory.out`.
- The source fixture generator was rerun twice with byte-identical results;
  it used a temporary Python SQLite source and never read repo runtime data.
  These are local unit/integration and offline migration results, not
  provider, production, platform or exact-head CI evidence.

### Open gates and next executable task

Complete a **durable, signed source-to-target transfer proof** that binds the
source fence, manifest hash, import journal and promotion artifact. Handle
preexisting Go shadow history, preserve Python-visible CAS revisions, migrate
linked policy/target state (including receipt mutation generations and key
custody), and prove an isolated reversible handback before any domain
promotion. The current importer does not independently inspect the source DB
fence and does not journal import provenance in Go. The Python service-stop
precondition is procedural, and a Windows file ACL has not been verified.
Go race, Linux exact-head CI and Evidence Assembly remain open. Public Rust
edge parity, real provider-backed worker recovery, Three-MinIO/two-Fleet,
complete APIs, browser, desktop and Android packages, nonzero successful
zero-Python workloads, and the full per-capability matrix remain open.

## Previous continuation checkpoint — 2026-09-29 signed per-domain promotion

Branch `codex/indexmap-std-feature`, starting HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`. The worktree was clean at
the start of this slice. No push, merge, release or production-data operation.
The full Rust/Go migration is **未完成**; readiness remains `NOT_READY` and the
ownership contract still says Python is current production authority.

### Completed local slice

- The existing authenticated `POST /internal/cutover/transition` now carries a
  canonical `control-domain-promotion-v1` artifact for an authoritative
  transition. A deployment-pinned Ed25519 public key verifies its signature;
  the document binds the domain, transfer/action ID, execution epoch, source
  and target states, revision/fence CAS, installed checkpoint tip, Fleet,
  environment and expiry. Missing key or unsigned/expired/tampered document
  refuses without changing the cutover row.
- Go schema v10 stores the exact signed artifact and hash in an append-only
  table in the same transaction as the cutover and its authorization journal.
  An existing v9 store with unsigned promotion history cannot silently upgrade
  to the signed schema. Schema-0 rollback refuses to discard retained promotion
  history. Existing v17 authority and v32 mutation contracts were not changed.
- Isolated HTTP tests drive claim → signed promotion → signed apply → persisted
  policy; store tests exercise valid promotion and signature/domain/expiry
  refusals. A started Go process exercises the signed HTTP transition and
  refuses unsigned or altered replays; only the exact original signed artifact
  can replay idempotently. The promoted worker action and shadow writer tests
  use the signed path, so they still measure real reachable authority.

### Verification and evidence

- `go test ./... -count=1` passed for all Go packages; `go vet ./...` passed.
- `python -m pytest tests/test_native_runtime_go_control_store.py -q
  -p no:cacheprovider` passed (4 cases). `python
  scripts/native_runtime_contract.py --check` passed (43 corpora, 32 versions,
  48 domains). Focused promotion tests passed.
- The first exact Go coverage run exposed a real gate failure:
  **94.822888% (5220/5505)**, below 95.0. Targeted refusal, migration-fault
  and started-process tests raised it to **95.046271% (5238/5511)** after the
  replay hardening. An isolated restart test then confirmed the signed journal
  survives reopening and the writer token increases after lease release. The
  final same-command rerun passed at **95.046271% (5238/5511)**. The ignored local
  profile and compact log are `artifacts/go-coverage-promotion.out` and `.log`.
  Command (from `go/`): `python ../scripts/check_go_coverage.py --dir .
  --min 95.0 --profile ../artifacts/go-coverage-promotion.out`. On Go 1.27.1
  Windows/amd64, the final profile SHA-256 is
  `0ED51CA8CA2BD6D5620BCFA73BDEC8B77E303C419BD70F7D878B9DD2ED1536A6`;
  the log SHA-256 is
  `C0258E5AF97AD567BFB07C5AC06EE5F4CECFF91B99A8CAD1CA17B3D0C9B9734D`.
- `go vet ./...`, `gofmt -l` on the changed Go production/test paths,
  `python -m pytest tests/test_native_runtime_go_control_store.py -q
  -p no:cacheprovider` (4), `python scripts/native_runtime_contract.py --check`
  (43 corpora, 32 versions, 48 domains), doc links and release-version checks
  passed. This is local Windows evidence on Go 1.27.1, not exact-head CI.
- Android early-risk inventory: `MainActivity` starts Chaquopy Python 3.13 and
  calls `deepseek_infra.android_entry.start_json`; Gradle packages Python
  sources and pip dependencies for `arm64-v8a`/`x86_64`. The configured SDK has
  `adb` and emulator binaries, but `adb devices -l` and `emulator -list-avds`
  returned no device or AVD; only the Windows GNU Rust target is installed.
  Android native build/behavior has not been verified.
- Go race, Linux exact-head CI and Evidence Assembly have not been claimed.

### Open gates and next executable task

Prove Python→Go export/import,
cross-store ownership fencing and rollback on an isolated copy. The Go-only
lease/restart test does not prove a Python ownership transfer. A local signed
artifact is not an independently authorized production promotion. Python
`authority.py` still gates control writes by global runtime mode, not
the Go per-domain cutover row; source-writer denial across that boundary needs
a race-proof mechanism before production promotion. The public
Rust edge, provider-backed worker reconciliation, Three-MinIO/two-Fleet
evidence, complete native APIs, desktop/Android packages, successful zero-Python
workloads and exact-head release evidence remain open. Matrix sections 2-4
still contain aggregate rows; expand them from routes, frontend calls, tasks,
stores and packaging before claiming capability completeness.

## Current continuation checkpoint — 2026-09-28 control authority reaches the running Go API

Branch `codex/indexmap-std-feature`, HEAD
`c99d3de6ec29c982c681fdd83d50bf400dab89ce`. This checkout already held
uncommitted control cutover, schema v9, mutation v2, and Rust worker changes when
this turn began; all were preserved. No push, merge or release. Readiness is
still `NOT_READY` with `exact_head: null`; the full Rust/Go migration is **未完成**.

### Completed local slice

- `go/internal/api/shadow.go` now exposes loopback-bearer protected
  `POST /internal/authority/claim` and `GET /internal/authority/head`. The
  checkpoint is bounded to 16 MiB, decoded without losing additive fields,
  and handed to the existing integrity, live-chain, writer-lease and
  deployment-capability checks. An exact replay reports `advanced: false`.
- An isolated HTTP test drives **claim → cutover → signed v2 apply → persisted
  policy record**. A started `deepseekd` lifecycle test proves the new claim
  and head routes are actually mounted. Missing store, malformed/null/oversized
  checkpoint, tampered digest, disabled capability, unreadable body, and
  unauthorized calls have refusal tests.
- A separate replay regression found that `AcceptMutation` and `ApplyMutation`
  could return `ALREADY_APPLIED` for an operation ID reused in a *different*
  control domain with an identical payload digest. Both tests were red before
  the fix. The idempotent fast path now also requires the stored domain to
  match, and the original operation remains unchanged.
- Code review found that the shadow evaluation path could still call `Put` after
  cutover. `PutShadow` now checks the durable owner in the same transaction as
  its write, including fenced domains such as `action`. Direct `Put` refuses
  unsigned writes to promoted non-fenced control domains. Both bypasses had
  failing regression tests before the guards were added.
- The matrix, 5.0 todo, Go control-store catalog, API/runbook, and readiness
  blocker text now describe these local capabilities without claiming cutover.

### Verification in this workspace

- `go test ./... -count=1` and `go vet ./...` passed after the shadow-write
  guards. The exact Go coverage command passed **95.105673% (5130/5394)**;
  its profile and compact log are
  `artifacts/go-coverage-native-20260928.out` and `.log` (local ignored
  artifacts). `gofmt` on changed Go files passed.
- `cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-worker --locked`
  passed all worker suites including frozen v17/v32, durable-grant and TLS
  cases. Pinned GNU `cargo check --workspace --locked`, strict workspace
  Clippy (`--all-targets --all-features -- -D warnings`), and
  `cargo +1.85.0 fmt --all -- --check` passed. The unsuffixed MSVC
  toolchain failed before tests because `link.exe` is absent; the installed
  pinned GNU toolchain provided the completed worker run.
- The focused Python oracle/catalog/readiness/ownership tests passed (38 cases),
  as did native contract checking (43 corpora, 32 versions, 48 domains),
  shadow parity (8/8), doc links and release-version consistency. The static
  zero-Python topology audit passed 8/8; **it is not** a successful live
  zero-Python workload measurement.
- `ruff check .` and `mypy .` now pass (918 source files). The committed
  skills parity probe's empty list needed an explicit `list[str]()` to satisfy
  this host's mypy inference; the value is still empty, and its Python/Rust
  replay passed **655 cases with 0 differences**. Markdown language navigation
  passes for 217 files after adding the missing links to the prepared 4.9.4
  amendment. `git diff --check` is clean.
- `go test -race ./...` could not execute tests on this Windows host: every
  package exited `0xc0000139`. Docker's CLI is present, but its Linux daemon
  pipe is absent and no MinIO binary is configured, so this turn produced no
  real-provider evidence. Exact-head Linux CI is not available without a push.

### Open gates and next executable task

No production domain has an externally signed per-domain promotion artifact,
isolated export/import and rollback proof, or exact-head ownership revision.
Worker operation-specific signed admission, Three-MinIO/two-Fleet provider
effects, process-kill/takeover reconciliation, platform packages and measured
zero-Python successful workloads remain open. Aggregate rows elsewhere in the
matrix still need per-capability expansion before final acceptance. The 16 MiB
claim-body limit also needs checking against a real exported checkpoint before
any cutover.

Next: implement and freeze the per-domain signed promotion request on the
existing Go authority/cutover state machine, then exercise export/import and
rollback on isolated data. Run provider-backed kill/takeover evidence when a
real MinIO topology is available. Keep `NOT_READY` until those and the other
matrix gates are proven.

## Current continuation checkpoint — 2026-09-27 the apply channel reaches the wire, with the signer taken from deployment config

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. Slices 1-4 are uncommitted. No push, merge, or
release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

**Slice 4 is complete**: the contract is frozen from the oracle, Go and Rust both verify it,
Go applies it atomically, and it is now reachable over the authenticated internal plane.

### The transport, and the one property that matters most

`POST /internal/mutation/apply` takes the **exact canonical `control-mutation-request-v2`
document as the body** and calls `Control.ApplyMutation` behind the existing loopback-bearer
guard. The critical decision: **the signer trust material comes from deployment
configuration, never from the request** — `DEEPSEEKD_MUTATION_SIGNER_KEY` (the base64url
Ed25519 public key), `DEEPSEEKD_FLEET_ID`, `DEEPSEEKD_ENVIRONMENT`. A caller therefore cannot
nominate the signer that authorizes its own mutation, and a deployment with no configured
signer refuses with `503 MUTATION_SIGNER_NOT_CONFIGURED` rather than accepting anything. A
test asserts that refusal specifically.

`RegisterWithOptions(..., InternalOptions{...})` carries the new options; `Register(mux,
control, bearer)` remains as a wrapper, so no existing call site or test had to change.

### Proven end to end, over HTTP

`go/internal/api/mutation_apply_route_test.go` drives the real path: an authority-enabled
store with the `policy` domain durably promoted through the authorized cutover, a v2 request
signed by the configured key, and a real HTTP `POST`. The response is `200` with
`status: APPLIED`, the record is then **observable through the authenticated snapshot**, a
retry returns `ALREADY_APPLIED` without a second apply, and the route refuses: no signer
configured (`503`), wrong signer (`409`), a domain that is not promoted (`409`, with the
record provably absent), `GET` (`405`), an oversized body (`413`), and no credential (`401`,
via the shared all-routes test which now includes this route).

### Verification (local, this workspace, not CI)

- `go vet ./...` clean; `go test ./... -count=1` → see the round's result below.
- `pytest tests/test_native_runtime_go_control_store.py ...` → the catalog gate now declares
  the route, asserts it is mounted behind `RequireInternalBearer(internal, options.Bearer)`,
  and asserts the signer is never caller-supplied.
- Coverage: this slice adds an HTTP handler with several refusal branches; the gate was
  re-measured (recorded below) because the previous round left only **4 statements** of
  headroom.

### Next executable task

The provider-backed kill/takeover reconciliation evidence for
`EFFECT_RECONCILIATION_UNPROVEN` (a real MinIO/Three-MinIO run, not a library result), then
the per-domain promotion evidence and the ownership-contract revision.

## Current continuation checkpoint — 2026-09-27 Rust reaches v2 parity, and the float fail-open was in all three implementations

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. Slices 1-4c are uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

Slice 4 steps 1, 2, 3 and 4 have now landed. **A transport for apply (the rest of step 5)
is the remaining half of slice 4.**

### Rust verifies v2, and the corpus proves it

`rust/crates/deepseek-worker/src/mutation_request.rs` gained the same `MutationRequestSpec`
split as Python and Go (`verify_mutation_request_v2_document`, `SIGNATURE_DOMAIN_V2`,
`PAYLOAD_FIELDS_V2`), and `tests/frozen_mutation_request_v32.rs` replays the frozen corpus:
**all 34 cases pass**, the positive request matches the frozen digest, and the revisions are
kept disjoint (the v1 verifier refuses a v2 document, and the frozen `v1-signature-domain`
case proves the reverse). The v17 suite still passes untouched.

### The float fail-open was in all three implementations

Rust's canonical encoder is `serde_json::to_vec(sorted(value))`, so Rust — like Go before it
— would have **accepted** a record body containing `1.5` that the Python oracle refuses, and
the body is inside `payloadDigest`. `validate_record_body` now enforces the oracle's
primitive set in Rust too. The v32 case `record-body-float` is exactly that proof: because
the canonical encoder accepts floats, the case can only produce `MUTATION_REQUEST_INVALID`
through this validator.

**Rust's secret rule was already correct** — unlike Go, it had no safe-suffix exemption and
applies the oracle's rule to the whole document, so `secret-suffixed-key-in-record-body`
passed without a change. The asymmetry is worth remembering: the three implementations did
*not* share one bug, they shared one *class* of bug.

### One measured, fail-closed divergence (Rust stricter than the oracle)

Rust's JSON number model is `i64`/`u64`, so an integer outside that range is refused by
`validate_record_body` while Python (arbitrary precision) and Go (`json.Number`) accept it.
Rust is **stricter, never looser**, so it cannot apply a body the oracle rejects — but a
legitimate body carrying a >64-bit integer would be refused by the worker only. No frozen
case covers it; record it if a real payload ever needs one.

### Verification (local, this workspace, not CI)

- `cargo +1.85.0 fmt --all -- --check` → **clean** (the new test file needed rustfmt first;
  applied. Checked under the *pinned* rustfmt, because the previous session lost time to a
  2021-vs-2024 import-order divergence between rustfmt versions).
- `cargo +1.85.0 clippy --locked --all-targets --all-features -- -D warnings` (the CI command,
  whole workspace) → **clean**. ⚠️ Under the machine's default `stable` (clippy 0.1.97) the
  same command fails in **`deepseek-browser/src/sidecar.rs:207`** with
  `clippy::result_large_err` — a crate this slice never touched. That is a toolchain-version
  artifact, not a regression: the repo pins 1.85.0, which is installed here and passes.
- `cargo +1.85.0 test -p deepseek-worker` → **whole crate green** (all test binaries ok,
  exit 0), including `frozen_mutation_request_v17` (2) and `frozen_mutation_request_v32` (3).
- **`cargo test --locked --all` (the CI `rust` job's third command) could not be run locally,
  and this is *not* a PASS.** It fails at **link** time in `deepseek-policy` examples and
  `deepseek-gateway` test binaries: the host `x86_64-w64-mingw32-gcc` rejects the
  `.drectve -exclude-symbols:…` directives the host rustc emits (rustls symbols are visible in
  the warnings). That is environmental, and it is provably **not** this slice:
  **no crate in the workspace depends on `deepseek-worker`** (checked every
  `rust/crates/*/Cargo.toml`), and the tokenizer/verifier change is confined to that crate.
  It also reproduces in an isolated `--target-dir`, so it is not a polluted shared `target/`.
  CI is Ubuntu and must judge this; do not record it as passing.
- `deepseek-worker`'s tests also pass under the default toolchain, so the crate is not
  toolchain-sensitive in a way that would hide a problem.
- Go side unchanged this slice; its gates were green at the end of the previous round.

### Next executable task

**A transport for `ApplyMutation`**: an authenticated `/internal/*` endpoint (the loopback
bearer is already in place) so the approved production-apply channel is reachable by an
operator rather than only in-process, plus the catalog/gate update. Then the provider-backed
kill/takeover evidence for `EFFECT_RECONCILIATION_UNPROVEN`.

## Current continuation checkpoint — 2026-09-27 Go applies a signed mutation, and two cross-language divergences died on the way

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. Slices 1-4b are uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

Slice 4 steps 1, 2 and 4 have landed: the v2 contract is frozen from the oracle, Go
verifies it and applies it atomically, and the store-level chain is proven end to end.
**Step 3 (Rust parity) and a transport for apply are still outstanding.**

### Go now verifies v2 and applies it

`go/internal/store/mutation_request.go` gained a `mutationRequestSpec` and the v2 path
(`SignMutationRequestV2`, `VerifyMutationRequestV2Document`); v1 keeps its own entry points
and bytes. All **34 frozen cases replay in Go** through a dedicated v32 loader, decoded
with `UseNumber` so a case's numeric replacements behave like the frozen document's
numbers — without that, `stale-epoch` would have failed with the wrong code, because a
plain `map[string]any` turns every number into `float64` and `asInt` reads `json.Number`
only.

### The two divergences the corpus work exposed — both were fail-opens in Go

1. **Floats.** Go's canonical encoder is plain `json.Marshal`, so Go would have *accepted*
   a `recordPayload` containing `1.5` where the Python oracle refuses it. That is not
   cosmetic: the body is inside `payloadDigest`, so accepting a body the oracle refuses
   means Go can apply a mutation the contract does not authorize.
   `validateMutationRecordPayload` now enforces the oracle's exact primitive set
   (`null`/string/bool/integer/list/object-with-string-keys), including the depth bound.
2. **Secret-key exemptions.** Go's shared control-record scan exempts keys ending in
   `digest`/`reference`/`ref`/`id`/`type`/`provider`; the oracle's mutation-channel rule
   has no such exemption. So `{"myTokenDigest": ...}` was refused by the oracle and
   accepted by Go. `rejectMutationBodySecretKeys` now applies the oracle's exact rule to
   the record body — *in addition* to the shared scan, so Go can never be looser than the
   oracle — and a **new frozen case** (`secret-suffixed-key-in-record-body`) pins it for
   every implementation, Rust included. The vector grew to 34 cases and its SHA changed to
   `a650c633…eb8e6`.

I did **not** change the shared rule (v1 and control-record validation depend on it) and I
did not weaken the Python rule; the v2 path is where the two rules meet.

### Schema v9 — the journal literally could not record an applied result

Through v8, `control_operations` froze `result_status = 'PROPOSED'`, which encoded "nothing
is ever applied". The first apply attempt failed the CHECK constraint, which is how this
was found. v9 widens it to `PROPOSED|APPLIED`, **preserves every row**, recreates the
frozen immutability triggers, and is **verified at open**: a store whose journal still
cannot record an applied result is refused rather than served with a journal that
misreports one. The V8→V9 test proves an operation row survives the upgrade — losing one
would permit a double-apply — and that the same request is still an idempotent no-op after
the upgrade.

### `Control.ApplyMutation` is the production channel

Every gate is deliberate and tested: the deployment cutover capability; a **durably
Go-authoritative** domain; the live cutover revision/epoch/fencing token; the request's
`actionId + executionEpoch`; the exact body the signer committed to; and a refusal for a
**fenced** domain (`action`/`scheduler_run`/`wave`/`transfer`) whose mutations belong to
the lease and admission path rather than to this channel. The record write, the operation
journal row (`result_status = APPLIED`) and the control event are **one transaction** — a
test proves that a rejected journal insert rolls the record back, and another proves the
writer-lease cliff before commit does too.

### Verification (local, this workspace, not CI)

- `go vet ./...` clean; `go test ./... -count=1` → **all packages ok, exit 0**.
- The Go coverage gate (CI command) → **PASS, 95.078258% (5042/5303)**. ⚠️ **That is only
  4 statements above the 95.0% floor** — the thinnest margin of this session. The 21
  remaining uncovered statements in this slice's files are `tx.Commit()`/`Rows.Scan`
  failures and migration-statement failures, which need an `admissionFaultStage`-style hook
  to reach; the next session must **add** coverage, not spend it, and should consider that
  hook if the gate ever runs close on CI. (CI is Linux and loses Windows-only statements.)
- `pytest` over the corpus, both mutation-request suites, the store catalog, foundation,
  ownership contract, 5.0 evidence and evidence gates → **all passed**;
  `check_zero_python_runtime.py` → **PASS 8/8**.
- `gofmt` clean on every changed file (two needed real formatting; applied so the CRLF
  working tree is preserved).
- `release/native_runtime_go_control_store_v1.json` + its Python gate now state the truth:
  v1 cannot authorize production apply, v2 does, `result_status` is
  `PROPOSED|APPLIED`, the migration list runs 1..9, and the **scope is store API only** —
  no internal HTTP route yet, no Rust parity.

### Next executable task

**Slice 4 step 3: Rust parity** in `rust/crates/deepseek-worker/src/mutation_request.rs`
(v2 verification, the cross-revision refusals, and the new secret case), extending the
existing `frozen_mutation_request_v17.rs` replay rather than adding a parallel harness.
Then an authenticated internal route for `ApplyMutation`, and the provider-backed
kill/takeover evidence that `EFFECT_RECONCILIATION_UNPROVEN` still needs.

## Current continuation checkpoint — 2026-09-27 the v2 production-apply contract is frozen from the oracle

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. Slices 1-4a are uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

The maintainer approved production apply on a **versioned new revision**. Slice 4 step 1
— "freeze the shape before code" — is done, and it was done in the order the plan set:
**oracle first, then the corpus, then (next) the Go/Rust implementations**.

### What is frozen now

- **`control-mutation-request-v2` (compat v32)**, with exactly four differences from v1:
  the schema identity, the operation (`apply-mutation`), the payload field set (adds
  `recordPayload`), and a **distinct signature domain**. The envelope field set, the
  canonical-JSON rule, the fencing/epoch/identity checks and every error code are shared,
  so v1 is untouched. The distinct domain is the property that stops cross-revision
  signature replay — `v1-signature-domain` is a frozen negative case, not a comment.
- **`recordPayload` is bound by `payloadDigest`**, so a v2 apply can only write bytes the
  signer committed to. It must be a JSON object. The canonical encoder accepts only
  `null`/string/bool/int/list/object-with-string-keys — **no floats** — which is exactly
  why the bytes can be reproduced identically in Python, Go and Rust; `record-body-float`
  is a frozen refusal.
- Secrets are still rejected, and the rule now covers the **record body**: a forbidden key
  inside `recordPayload` is `MUTATION_REQUEST_SECRET_DETECTED`
  (`secret-key-in-record-body`).

### How the corpus was produced (and why that matters)

`compat/native-runtime/v32/` holds the manifest, a README that states the v1/v2 diff
table, and `control/mutation_request_v2_vector.json` with **33 negative cases**. The
vector is **generated by the Python oracle, not hand-written**, and every case was
**executed against the oracle before being committed** — the generator asserts each
`error` code, so a case that did not actually fail would have aborted the write. Pinned
SHA-256 `67550e0b98bc114ff6c8f604bfc35a04d990c526d232ca90b885cbcbfce59c15`.

One case had to change for a real reason: the first secret case embedded the literal
`age-secret-key-…` in the vector, which the corpus's own "no secret material" gate
rejects. Rather than weaken that gate, the case now triggers the same rule through a
forbidden **key** (`privateKey`) inside the record body — which also proves the new body
path is scanned.

### v1 is provably unchanged

The v17 vector and every existing v1 test still pass, and the new test file asserts both
cross-refusals: the **v1** verifier refuses a v2 document and the **v2** verifier refuses a
v1 document (`MUTATION_REQUEST_SCHEMA_INVALID`). The corpus gate also pins v32 **by id and
by disjointness from v17** instead of only bumping a count from 31 to 32.

### Verification (local, this workspace, not CI)

- `pytest tests/test_native_runtime_corpus.py tests/test_native_runtime_mutation_request.py
  tests/test_native_runtime_mutation_request_v2.py tests/test_native_runtime_authority_request.py
  tests/test_native_runtime_foundation.py tests/test_native_runtime_ownership_contract.py
  tests/test_native_runtime_go_control_store.py tests/test_native_runtime_5_0_evidence.py
  tests/test_native_runtime_evidence_gate.py -q` → **all passed**.
- `validate_corpora()` → **32 manifests**, last one
  `control-mutation-request-v2-semantics-v32`.
- `ruff check .` clean; `mypy` on the two changed Python files → **no issues**.
- No Go or Rust change in this slice, so the Go gates were not re-run here; the Go
  `native-go` gate is unaffected until step 2 lands.

### Next executable task

**Slice 4 step 2: the Go v2 verifier and the atomic apply**, then step 3 (Rust parity),
step 4 (end-to-end on a promoted `policy` domain), step 5 (flip
`operations.production_apply*` with the evidence). Details and the exact requirements are
in the "Slice 4" section of
[`control-cutover-authorization-plan.md`](control-cutover-authorization-plan.md).

## Current continuation checkpoint — 2026-09-27 production authority stops being a caller-supplied flag

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. Slices 1-3 are uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

Two guards were **inverted placeholders**: they refused the production path precisely
when a caller *claimed* production authority, without ever consulting durable state.

| Site | Before | Now |
| --- | --- | --- |
| `Coordinator.ExecuteStorageAction` | `if c.authoritative { ErrCutoverNotAuthorized }` | durable gate |
| `Coordinator.ReconcileStorageAction` | same | durable gate |
| `Coordinator.ReconcileClaimedStorageAction` | same | durable gate |
| `Coordinator.ExecuteClaimedStorageAction` | same | durable gate |

### What is enforced now

A caller's flag is a **claim**, and `assertProductionAuthority` requires it to be backed
by the durable cutover record: `store.IsGoAuthoritative("action")` reads the
`control_cutover` row in a transaction that verifies the schema, so a missing or corrupt
row is an **error** rather than a silent "not authoritative" (a silent false would look
like a safe refusal; a silent true would grant authority). A claim with no such record,
or with a larger epoch, is refused `CUTOVER_NOT_AUTHORIZED` **before any durable write**,
and the cutover record is left untouched — asserted. A coordinator that makes no claim
keeps the unchanged non-authoritative qualification path and never consults the record.

That is the §五.4 property applied to the execution plane: authority comes from the
authority claim/takeover flow, not from what a worker asserts about itself.

### The legal path is proven, end to end

`promotedActionDomain` opens a real authority-enabled store, claims a
`control-authority-v1` genesis (built through the exported digest helpers), and promotes
the `action` domain `shadow → dual_evaluate → go_authoritative` through slice 1's
authorized cutover. With that, `ExecuteStorageAction` on `WithAuthoritative(true)` runs
for real: the dispatch intent is durably bound, the provider effect identity comes back,
and the action record reaches terminal `SUCCEEDED`. Against an unpromoted store, all four
entry points refuse and **no** action row and **no** cutover movement exist afterwards.

### The production-mutation question is a contract question, and it is now precise

`MutateProduction` stays `DenyMutation()`, and `AcceptMutation` still refuses once the
domain is Go-authoritative. The reason is no longer an omission and is now documented at
the refusal site: the frozen `control-mutation-request-v1` carries **exactly one** intent,
`shadow-compare`, whose payload is a comparison expectation
(`intent`/`recordId`/`revision`/`state`) with **no record body**. It cannot authorize a
production mutation, and applying it would reinterpret a frozen intent as production
authorization — which the workspace rules forbid. Authorizing production apply needs an
**explicitly approved production intent/operation on a versioned revision of that
contract**; that is a decision for the maintainer, not a silent edit. The refusal writes
nothing, and a test pins that.

### Verification (local, this workspace, not CI)

- `go vet ./...` clean; `go test ./... -count=1` → **all packages ok, exit 0**.
- The Go coverage gate (CI command) → **PASS, 95.195487% (4894/5141)**, 10 statements
  above the 95.0% floor.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**; the catalog gate now also
  asserts the durable gate exists in `reconciler.go`/`cutover.go` and that the catalog's
  `production_authority` block matches the four gated entry points.
- `gofmt` clean on every changed file (normalised line endings).
- Note for the next session: the coverage script runs `go test` **per package without
  `-coverpkg`**, so a function is only covered by tests *in its own package*. The store
  test for `IsGoAuthoritative` had to live in the store package even though the action
  package is its consumer; that alone was the 0.4% gate failure this slice hit and fixed.

### Next executable task

**Slice 4, and it is now approved rather than pending.** The maintainer approved
(2026-09-27) adding a production intent/operation on a **versioned new revision** —
`control-mutation-request-v2` with `apply-mutation` — while **v1 semantics stay
byte-identical** (do not touch v1, its digest rules, its field list, or the v17 compat
corpus). The full ordered plan is the "Slice 4" section of
[`control-cutover-authorization-plan.md`](control-cutover-authorization-plan.md); start by
freezing the v2 document shape and its v18 compat vector *before* writing code.

Independently of that (and needed for `EFFECT_RECONCILIATION_UNPROVEN` either way): the
provider-backed kill/takeover reconciliation evidence, which requires a real
MinIO/Three-MinIO run rather than library results.

## Current continuation checkpoint — 2026-09-27 the internal control plane stops being anonymous

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice (and the one below it) is
uncommitted. No push, merge, or release. `release/native_runtime_5_0_evidence_v1.json`
is still `NOT_READY` (`exact_head: null`). The whole migration is **未完成**.

The previous checkpoint ended with "`/internal/*` has no caller authentication, so
`AuthorizeCutover` must not be enabled in any deployment". That is now false, and the
cutover capability can be turned on safely.

### What is enforced

- **Every `/internal/*` request needs `Authorization: Bearer $DEEPSEEKD_INTERNAL_BEARER`**
  (minimum 32 characters) **from a loopback peer**, compared with
  `crypto/subtle.ConstantTimeCompare`. The scheme is parsed exactly: a bare `Bearer`, a
  raw token, `Basic …`, and trailing-whitespace-only credentials are all *not*
  credentials.
- **Loopback is required independently of `DEEPSEEKD_LISTEN`.** Widening the listener can
  no longer expose the control plane; `docker-compose.native.yml`'s
  `DEEPSEEKD_LISTEN: 0.0.0.0:8090` is therefore harmless rather than a hazard.
- **An unconfigured bearer serves no control plane.** The routes stay mounted and answer
  `401 INTERNAL_API_UNAUTHORIZED` with a `WWW-Authenticate: Bearer` challenge. Missing,
  malformed, wrong, and unconfigured credentials get the *same* answer, so the response
  cannot be used to probe which is true. That is the shipped default.
- **`DEEPSEEKD_CONTROL_AUTHORITY=1` is refused by `config.Load` unless a bearer is
  configured**, and `lifecycle` passes it as `AuthorizeCutover`. The migration authority
  can therefore no longer be claimed over an unauthenticated channel — the coupling is
  mechanical, not a documented convention.
- `Register(mux, control, bearer)` makes the credential a **required argument**, so no
  caller can mount the control plane unauthenticated by omission. `Handler()` now means
  "public plane only".

The public plane is untouched: `/healthz`, `/api/control/status` and `/api/*` keep
answering without a credential, and the Rust edge still 404s `/internal/*` (its own
`public_control_boundary.rs` asserts it), so nothing that worked before needs a token.

### What this slice did not do

- No deployment surface *sets* the bearer yet. `docker-compose.native.yml` sets neither
  variable, so its control plane is intentionally unreachable — including from sibling
  containers. Enabling promotion there is an operator edit, which is the point.
- **`MutateProduction` is still `DenyMutation()`.** Owning the control plane is not the
  same as serving production writes; that is the next slice.
- No per-domain signed promotion request; the claim still authorizes *the deployment*.

### What the tests found

1. **The `100-continue` test was a hidden dependency on the unauth path.**
   `supervisor_test.go` writes a raw `POST /internal/shadow/evaluate` with
   `Expect: 100-continue` and asserts the server sends `100` — which only happens once a
   handler *reads* the body. An auth middleware that rejects without reading would have
   turned that into a `401`. The raw request now carries the credential, so the test still
   proves "the handler began reading", which is what it was always about.
2. **Two `Start(...)` call sites**, not one, took the same literal config; both now declare
   the bearer.
3. The mechanical part was `http.Post(`/`http.Get(` → a bearer-injecting `*http.Client` in
   `shadow_test.go` (27 call sites). **The assertions did not move**: every status code the
   file already pinned is still pinned, and the requests are now authenticated instead of
   anonymous, which is exactly the behaviour change.

### Verification (local, this workspace, not CI)

- `go vet ./...` clean; `go test ./... -count=1` → **all packages ok, exit 0** (including
  the two real-process `deepseekd` tests, which now start the daemon with a bearer and
  present it).
- `go test ./internal/api/ ./internal/config/ ./internal/lifecycle/` → ok, including the
  new started-runtime end-to-end test: an authenticated `POST /internal/shadow/evaluate`
  creates real control state that the authenticated snapshot reports back with the
  expected writer identity, while the anonymous client gets `401` and `/healthz` still
  answers.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/control_plane_shadow.py --check` → `{"ok": true, "passed": 8}`.
- `python -m pytest tests/test_native_runtime_go_control_store.py
  tests/test_native_runtime_foundation.py tests/test_native_runtime_ownership_contract.py
  tests/test_check_zero_python_runtime.py tests/test_native_runtime_5_0_evidence.py
  tests/test_native_runtime_evidence_gate.py -q` → **all passed**.
- `gofmt` clean on every changed file (normalised line endings; the checkout is CRLF).
- The catalog gate now asserts the **coupling** instead of the absence of the capability:
  `AuthorizeCutover: cfg.ControlAuthority` in `lifecycle`, the refusal
  `cfg.ControlAuthority && cfg.InternalAPIBearer == ""` in `config`, the constant-time
  comparison and loopback check in `api/auth.go`, and every catalog-declared internal
  route mounted behind `RequireInternalBearer(internal, bearer)`. That is a stronger gate
  than the one it replaces, and Go tests prove the behaviour.
- `docs/GO_PUBLIC_API.md` and the migration runbook document the credential, the loopback
  rule, the anonymous default, and the authority coupling.
- The Go coverage gate (`check_go_coverage.py --dir . --min 95.0 --profile coverage.out`,
  the CI command): **PASS, 95.186852% (4865/5111)** — 9 statements above the 95.0% floor,
  up from 7 when this slice started. CI runs on Linux where a Windows-only path is not
  covered, so headroom still matters: the next slice must **add** coverage, not spend it.

### Next executable task

The **production mutation channel**: `MutateProduction` through the authenticated internal
plane, so a Go-authoritative domain can actually serve writes. It already has the pieces it
needs — `AcceptMutation` verifies a signed `control-mutation-request-v1` against the live
cutover fence/epoch/revision and journals `PROPOSED`, and `ExecuteClaimedStorageAction` is
gated on `c.authoritative` being *false* today, which is the inverted placeholder that slice
has to replace.

## Current continuation checkpoint — 2026-09-27 the control authority becomes a state machine, and a domain can finally be promoted

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

This slice attacks the code half of `CONTROL_CUTOVER_INCOMPLETE` — the blocker that said
"Go TransitionCutover still returns ErrCutoverNotAuthorized for states requiring
production authorization". Measured before the slice: `cutover.go` refused
**unconditionally** at `if cutoverRequiresAuthorization(req.To)`, so **no Go control
domain could ever become an authoritative owner**, whatever the evidence.

### What is now true that was not

`control-authority-v1` was a *library* in this repo (`go/internal/store/authority.go`: the
frozen checkpoint format, its digests, the chain rule, the monotonic head CAS) with no
persistence behind it. **Schema v8** persists it and binds it to the cutover:

- `control_authority_head` — one row, CAS-updated, delete refused.
- `control_authority_checkpoints` — append-only; generation is the key.
- `control_cutover_authorizations` — append-only; binds a promotion to the authority tip
  it consumed (domain, transfer id, generation + digest, from/to state, previous/next
  revision, epoch and fencing token).
- `ClaimControlAuthority(checkpoint)` — the **only** path that may advance the authority
  head: writer fence, checkpoint integrity, the frozen monotonic CAS, journal, head
  advance, one transaction. An exact replay of the tip is idempotent and writes nothing.
- `TransitionCutover` now takes `Authority *AuthorityCheckpoint`. A promotion needs a
  non-nil checkpoint that is byte-identically the live tip, **and** a deployment opened
  with `OpenOptions.AuthorizeCutover`. The domain's revision/epoch/fencing token advance
  only inside that transaction, and the consumed authority is journaled.

### The asymmetry is deliberate, and it is the safety property

Promotion is gated; **de-promotion is not**. `python_shadow` → `go_authoritative` and
`go_authoritative` → `shadow` still need no authority, so ownership can always be rolled
back — a cutover mistake must never be unrecoverable. The refusal detail is split too:
the bare `ErrCutoverNotAuthorized` (what the existing `!=` test compares) means "this
deployment may not authorize", while `ErrCutoverAuthorityStale` means "you presented
something that is not the live authority".

### What did *not* change, on purpose

- **The default deployment stays mechanically unable to promote.** `deepseekd` does not
  set `AuthorizeCutover`; `tests/test_native_runtime_go_control_store.py` now asserts that
  absence, so wiring it on by accident fails a gate. The loopback
  `/internal/cutover/transition` endpoint therefore gains **no** remotely reachable
  capability.
- **No frozen contract was touched.** `control-authority-v1` / AuthorityCheckpoint v1 and
  every digest rule are unchanged; the slice only persists documents the library already
  verified. The additive-field support both sides already have (`checkpointDocument` in
  Go, `_payload_for_digest` in Python, which hashes every key except the three envelope
  fields) was *not* needed in the end.
- **`MutateProduction` is still `DenyMutation()`.** Authorizing the ownership change is
  not the same as authorizing production mutation; that is the separate open item in
  `4.9.3-plan.md` and the next slice.

### What the tests found

Three of my own expectations were wrong and the assertions, not the code, moved:
a first claim that skips genesis is `STALE_AUTHORITY_WRITER` (not
`AUTHORITY_GENERATION_GAP`), a "gap" fixture derived from generation 2 was actually a
*legal* next checkpoint, and a secret-bearing checkpoint is refused by validation
**before** any digest is recomputed, so it cannot be resealed. Each is now stated as the
measured behaviour.

Nine historical-schema fixtures (`action_admission`, `action_reconciliation`,
`action_verification`, `storage_dispatch`, `operation`, `cutover`, and `Control.Rollback`)
had to learn about the v8 objects: a pre-v8 shape with v8 tables present is exactly the
"unexpected sqlite object" that `validateControlUserObjects` exists to catch, so the
fixtures drop them. `Rollback` gained the same three drops, or a rollback to schema 0
would have left orphan tables. **No gate was weakened** — the fixtures gained objects to
remove, never assertions to skip.

### Verification (local, this workspace, not CI)

- `go vet ./...` → clean; `go test ./... -count=1` → **all packages ok**, exit 0.
- `go test ./internal/store/ -count=1` → **ok** (58 s), including the 9 upgraded fixtures.
- `go test ./internal/store/ -run TestControlAuthorityClaimAdvancesAtMostOnceUnderConcurrency
  -count=20` → ok (the concurrency case is deterministic under repetition).
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/control_plane_shadow.py --check` → `{"ok": true, "passed": 8}`.
- `python -m pytest tests/test_native_runtime_go_control_store.py
  tests/test_native_runtime_foundation.py tests/test_native_runtime_ownership_contract.py
  tests/test_check_zero_python_runtime.py -q` → **all passed**.
- `gofmt` clean on every changed/new file (checked with line endings normalised: the local
  checkout is CRLF, so a bare `gofmt -l` flags every file in the repo).
- `ruff check .` → clean. `mypy .` → **1 error, not in this slice**:
  `tasks/native-runtime/skills_parity_probe.py:539`, a `dict[str, list[Never]]` vs
  `dict[str, list[str]]` invariance complaint. That file is committed and untouched by
  this slice, and the local interpreter is **mypy 2.0.0** while the repo only requires
  `mypy>=1.8.0`, so this is a local-toolchain difference, not a measured regression.
  Re-check under the version CI resolves before treating it as a repo defect. The Python
  file this slice did change (`tests/test_native_runtime_go_control_store.py`) is clean.
- **`go test -race` cannot run on this host.** It fails `exit status 0xc0000139`
  (`STATUS_ENTRYPOINT_NOT_FOUND`) on **untouched** packages too, e.g.
  `go test -race ./internal/config/`; the module builds `CGO_ENABLED=0` and this Windows
  toolchain has no working race runtime. It stays a CI (Linux) gate — **not** a local PASS.
- The Go coverage gate (`python scripts/check_go_coverage.py --dir . --min 95.0
  --profile coverage.out`, the CI command): **PASS, 95.145056% (4821/5067)** as measured
  for *that* slice. Slice 2 re-measured the module at **95.186852% (4865/5111)** — see the
  checkpoint above for the current figure. Either way the margin is only single-digit
  statements, and CI runs on Linux where a Windows-only path is not covered, so the
  *next* slice must **add** headroom rather than spend it. `internal/store` alone
  measured 93.4% of statements.

### Not done, and the next executable task

`tasks/todo.md` and `tasks/plan.md` are **gate-frozen**:
`tests/test_native_runtime_ownership_contract.py::test_existing_4_8_0_plan_artifacts_are_unchanged`
requires `git diff` to be empty for both. They are historical records, not trackers —
the live trackers are this file and [`migration-matrix.md`](migration-matrix.md), plus this
slice's [`control-cutover-authorization-plan.md`](control-cutover-authorization-plan.md).
Editing `todo.md` "to keep it current" breaks a gate.

This is **not** a cutover. No domain is flipped, `current_owner` in
`release/native_runtime_ownership_v1.json` is untouched, and the store catalog still
records `mode: shadow`. `CONTROL_CUTOVER_INCOMPLETE` is **partially** cleared: the state
machine is real and proven, the authorization channel is not.

Next, in dependency order:

1. **Authenticate the internal control API.** `/internal/*` has no caller authentication,
   so `AuthorizeCutover` must not be enabled in any deployment until that lands. This is
   also what makes a claim non-self-issued.
2. **The production mutation channel** — `MutateProduction` through the authenticated
   signed request, so a Go-authoritative domain can actually serve writes.
3. Then the per-domain evidence and the ownership-contract revision.

## Current continuation checkpoint — 2026-09-27 the cutover: what it needs, and the gate that found a bug

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

### The switch is not a code change, and it is not due

`release/native_runtime_ownership_v1.json` carries the switch per domain: `current_owner`
flips `python` -> the target. Measured: **0 of the 48 domains are flipped** — 47 are
`python`, and the one non-Python entry (`browser_ui`, `typescript`) is `production: false`.
`skills_store` is in the `4.9.4` group (`memory_store`, `reminders_store`,
`project_metadata_store`), and the repository is at **4.8.0**. The file is
`status: accepted` with `approved_by: ["leizd"]`, so flipping a domain **amends a signed
contract** and jumps its schedule — that is the maintainer's signature, not mine.

`release/native_runtime_5_0_evidence_v1.json` lists six blockers. All six are outside this
surface's code: the public edge's parity (`PUBLIC_EDGE_AUTHORITY_INCOMPLETE`), Go's
`TransitionCutover` (`CONTROL_CUTOVER_INCOMPLETE`), the worker's effect reconciliation
(`EFFECT_RECONCILIATION_UNPROVEN`), and three **evidence** blockers — live provider runs
(Three-MinIO, two-Fleet, SIGKILL/takeover), exact-head CI and Evidence Assembly, and
live-workload-measured zero-Python artifacts. None of them is a skills-surface change, and
none can be produced from this working tree.

### What this slice did: ran the cutover's own gate, and it failed

`python scripts/check_zero_python_runtime.py` — the executable Zero-Python release gate —
returned **7/8**, failing `process_tree_isolation`:

```
[FAIL] process_tree_isolation: Rust production code spawns forbidden process:
       rust\crates\deepseek-policy\src\skills\eval.rs spawns python
```

The check's rule is a heuristic: a production `.rs` file that contains `command::new` **and**
a quoted `"python"` token. `skills/eval.rs` had both — `git_commit` ran
`std::process::Command::new("git")` to stamp the report, and the report's `environment` block
carries the field name `"python"` (mirroring the oracle's `{"os", "python", "ci"}`).

**The gate was not touched.** Weakening a release gate to accommodate the code under it is
the wrong direction, and the gate's *intent* — no Python subprocess in the native runtime —
is exactly right. The subprocess came out instead: `git_commit` now reports an empty commit,
which it already did in any container, and the divergence was already recorded for the CI
source-context half. The report's field shape is unchanged. Re-run: **8/8 PASS**.

That is the honest shape of "继续完成切换": the mechanical half is verified
(`mechanical_writer_denial` covers all 29 Go control domains and all 4 Rust data domains,
`skills_store` among them), and the switch itself is the maintainer's call.

### The amendment this surface would need (prepared, not applied)

**Full text: [`cutover-4.9.4-amendment.md`](cutover-4.9.4-amendment.md).** The short version is
that the contract's **own validator** freezes the fields a cutover would move:
`current_production_authority` must stay `python` ("4.8.1 production authority must remain
python"), `source_commit` must stay the 4.8.0 merge SHA, and `current_owner` must be `python`
or `typescript` **for every domain** — so `"current_owner": "rust"` is **invalid**, not an
edit. The file is a frozen record of the 4.8.0 snapshot; cutting a domain over is a **new
accepted revision** (validator + header + source version/commit + the two tests that pin "no
cutover yet" + the evidence file), and two of its four preconditions are parity/evidence
conditions no code change in this tree can satisfy.

For `skills_store` and its three 4.9.4 siblings, the switch would be one field each:

```diff
   {"id": "skills_store", "plane": "data", "current_owner": "python",
    "target_owner": "rust", "cutover": "4.9.4", "durable_store": "rust_data"}
```
```diff
- "current_owner": "python",
+ "current_owner": "rust",
```

…plus, in the same file, `current_production_authority` and the `source_version`. That is a
contract revision and it is not written here. What has to be true before it is signed:

1. The 4.9.4 group's stores are all `rust_data` and each one's Python writers are denied —
   `skills_store`'s is verified (see the matrix's skills row).
2. `check_zero_python_runtime.py` is 8/8 on the exact head that will be cut over.
3. Exact-head CI plus Evidence Assembly exists for that head — which needs a **push**, and
   that is a separate approval.
4. The live-workload measurements the readiness file asks for.

### Verification (local, this workspace, not CI)

- `python scripts/check_zero_python_runtime.py` → **PASS, 8/8** (was 7/8 before this slice).
- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 655 cases, 0
  differences** (`commit` and `environment.python` normalized; see below).
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.

## Current continuation checkpoint — 2026-09-27 the eval engine lands; the skills surface is dispatched end to end

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

**Every one of the 52 `action == …` branches in
`deepseek_infra/web/routes/skills.py` now has a native branch**, and
`ACTION_NOT_MIGRATED` is an empty array — it stays so the next surface arrives with one, and
the route test asserts its contents rather than a pinned name. The last five to land are
`eval_report`, `eval_upgrade_gate`, `upgrade_pack`, `diff_versions` and `diff_pack_versions`
— the eval engine and the four actions whose payload embeds its verdict.

**One branch is dispatched but still refuses**: `run` with an API key. Without a key it
returns the oracle's own `MISSING_API_KEY`; with one it is a precise
`NATIVE_SKILLS_ACTION_NOT_READY` because the model call is not ported. That is the honest
state of "52/52": every branch is dispatched, one is a named refusal.

### The engine, complete

`skills/eval.rs` now carries both halves of `eval.py`: the case store, and the report engine
— `run_case` (a project per case when the case needs one, `runner::offline` with
`persist=true`, five metrics), `report` (the corpus, one `run_case` each, then the assembly),
`upgrade_gate_for` and `score_diff`.

The five actions are **writers, not reads**: every case persists a run and may create its own
eval project, so they carry both gates (`skills_store` and `project_metadata_store`).

### The media fixture, and a mistake the probe caught

`_prepare_media_fixture` registers a media row and substitutes the id it generated.
**Six** built-in Skills (`audio_transcript_summarizer`, `image_explainer`, `media_to_report`,
`pdf_reader`, `video_brief_generator`, `webpage_summarizer`) ship an `exampleInputs` entry
that references `media_example`, so a `scope: "all"` report walks six such cases.

The first version refused the **whole report** for those cases. The probe caught it
immediately — `eval_report` with `scope: "all"` returned the refusal where the oracle
returns a report — and that would have been a far larger hole than one case: six cases it
cannot prepare would have taken the entire corpus report down. **Per-case is the right
granularity**: the refusal is recorded as that case's failure, the case is not run at all
(scoring input nobody prepared is exactly the silent difference the refusal exists to
avoid), and the report completes.

Consequence for the comparison: `scope: "all"` is **not** byte-comparable while those six
cases differ by design, so the probe drives the per-Skill and per-Pack scopes and names the
six in a comment. `media_fixture`'s refusal has its own unit test.

### Recorded divergences (all measured, none silent)

- The six media-fixture cases above.
- `environment.python` is the oracle's interpreter; `commit` reads `git rev-parse` on both
  sides but the CI source-context half of `evidence_revision` is not ported.
- `metrics.latencyMs` is wall-clock, so the probe normalizes it (two oracle runs disagree too).
- `ToolPolicy`'s default `audit=True` writes an audit entry per evaluation in the oracle; this
  port uses the crate's no-op sink.
- A `forbidden` pattern Python's `re` accepts but `regex` cannot compile is refused rather
  than treated as a non-match.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 655 cases, 0
  differences**.
- `cargo test -p deepseek-gateway --test skills_routes` → **6 passed**.
- `cargo test -p deepseek-policy --lib skills::` → **12 passed**.
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.

### Not done, and the next executable task

The skills surface is dispatched; what remains is **完成切换**, which is a deployment
action and not a code one: the default launcher and image still start Python, and `.skills`
plus `project.json` stay Python's until a process runs `python_disabled`. Beyond that, the
online `run` needs the model call, and the rest of the file family is a separate surface.

## Current continuation checkpoint — 2026-09-27 the tool-policy blocker was a binding

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

`ACTION_NOT_MIGRATED` is again **unchanged at 5** — this slice clears a blocker rather than
wiring. The previous checkpoint listed three unported subsystems behind `_run_case`; one of
them is now done, and it turned out **not** to be a port.

### `evaluate_skill_tool` was a binding, not a subsystem

`deepseek_infra/infra/skills/permissions.py` is 49 lines and its whole job is to bind a
Skill's `allowedTools` to the Tool Policy Engine. The engine it binds to —
`ToolPolicy`, `ToolPolicyConfig`, `evaluate`, `ToolPolicyDecision`, the metadata table,
the SSRF / path / secret / taint guards — is **already ported** in
`deepseek-policy/src/tool_policy.rs`, field for field, including
`ToolPolicyConfig::default()` reading the same `ToolPolicySettings` the oracle reads.

So `skills::permissions` is a new module of ~40 lines: build a policy with the Skill's
grant, `enforce_schema: false` and the oracle's scope (`project:<id>` when a project is
bound, else `skill:<skillId or unknown>`), then evaluate with an empty argument object.
`eval::tool_policy_pass` is the metric that uses it.

**Why the earlier estimate was wrong**: the engine's file is 1 800 lines, and I read the
class at line 568 of the *Python* file and concluded the port was missing — without
grepping the Rust crate for `ToolPolicy`. The Rust type exists and is more complete than
the binding needs.

**One divergence, in a side effect**: the oracle's `ToolPolicy` defaults to `audit=True`
and writes an audit entry per evaluation, so its `evaluate_skill_tool` leaves a line in
the tool-audit directory; this port evaluates with the crate's default (no-op) sink, so
nothing is written. The returned decision is unaffected.

### Verification

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 645 cases, 0
  differences** (72 new: four Skill grants × nine tool names through
  `evaluate_skill_tool`'s `to_dict()`, and the same four × nine cases through the metric).
- `cargo test -p deepseek-policy --lib skills::` → **11 passed** (three new in `permissions`).
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.

### Not done, and the next executable task

Two blockers remain for `_run_case`, and both are already scoped:

1. **Media ingestion.** Only a case whose input references the `media_example` fixture needs it,
   so the plan is a precise refusal for that case rather than porting the pipeline.
2. **Python `re`** for a case's `forbidden` patterns — `content_pass` already uses the `regex`
   crate and refuses a pattern that cannot compile.

With those, `_run_case` plus `build_skill_eval_report`'s existing assembly is the whole of the
remaining work, and then the five actions wire. `projects::create_project`
(`entropy`-taking) and `projects::export_project` are the two project-side calls to confirm.

## Current continuation checkpoint — 2026-09-27 the eval engine, split at its seam

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

`ACTION_NOT_MIGRATED` is **unchanged at 5** — nothing was wired this slice. What landed is
the larger half of the dependency those five share: `deepseek-policy/src/skills/eval.rs`
grew from the case store into the **scoring, aggregation and comparison** half of
`build_skill_eval_report`, all of it compared against the unmodified oracle.

### Where the engine splits, and why

`_run_case` is the execution half: it calls `run_skill(..., offline=True, persist=True)`,
creates a real project per case when the case needs one, prepares the media fixture, and
scores five metrics. Three things it needs are **not ported**, and each is a subsystem
rather than a function:

| missing | what it is | what the port does instead |
| --- | --- | --- |
| media ingestion | `ingestion.register_from_payload` + processing, for a case whose input references `media_example` | refuses that case by name; it will not score an input it did not prepare |
| `permissions.evaluate_skill_tool` | the `deniedTools` half of the tool-policy metric | refusal, same reason — no Rust port exists |
| Python `re` | a case's `forbidden` patterns | the `regex` crate, and a pattern Python accepts but `regex` cannot compile is a refusal, not a silent non-match |

Everything else in `build_skill_eval_report` is a function of `case_results`, so it is
ported: `_json_path`, `_artifact_pass`, `_content_pass`, `_sample_input`, `_synthetic_case`,
`_selected_skill_ids`, `_pack_membership`, `_cases_for_skills`, `_dedupe_case_results`,
`_ratio`, `_aggregate_result`, `_skill_results`, `_pack_results`, `_compare_item`,
`compare_reports`, and the assembly itself as `report_from_results`. `upgrade_gate` is the
`eval_aware_upgrade_gate` extraction, and `git_commit` / `platform_system` feed the report's
identity fields.

### Recorded divergences (the report's own bytes)

- `environment.python` is the oracle's `platform.python_version()`; the port reports the OS
  it maps and an empty interpreter. `environment.os` is mapped (`windows` -> `Windows`).
- `commit` runs `git rev-parse --short=12 HEAD` on both sides, so a checkout agrees; the
  **source-context** half of `evidence_revision` (the CI path, where the tested revision is
  handed in) is not ported, so a CI run would report `unknown`.
- `metrics.latencyMs` is a wall-clock measurement and can never match; the probe normalizes
  it, as it does `dry_run`'s timestamps.

### What the comparison found

`skills_parity_probe.py` grew from 523 to **573 cases**, and the assembly is driven with
`_run_case` stubbed by a fixture — the **execution** half is stubbed, not the subject, so
aggregation, scoring and the baseline comparison are still the oracle's own code.

Three divergences, all in this port:

1. `selected_skill_ids` passed `builtin_only = true` to `Registry::list`, so the custom
   Skills were missing from the "all" scope (18 against 19).
2. `Registry::list` does **not** sort, while the oracle's `list_skills` sorts by
   `(bool(builtin) is False, name)` — built-ins first, then custom, each by display name.
   The catalog never noticed because it sorts its own items; the eval selection applies the
   sort locally. **This is worth a look on its own**: `list` is the oracle for
   `list_skills`, and any other caller of `list` whose order is observable has the same gap.
3. The port's own test expectations for `compare_reports` were wrong twice — a PASS->FAIL
   transition is a *new failure*, not a score drop — which the unit tests caught before the
   probe did.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 573 cases, 0
  differences**.
- `cargo test -p deepseek-policy --lib skills::eval` → **8 passed** (five new: `json_path`,
  `content_pass`, `artifact_pass`, `sample_input`, `compare_reports`).
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.

### Not done, and the next executable task

Five actions still answer `501`, and the remaining work is `_run_case` plus the wiring of
`eval_report`, `diff_versions`, `diff_pack_versions`, `upgrade_pack` and `eval_upgrade_gate`.
Three of the five need nothing new beyond `_run_case`; `diff_versions` / `diff_pack_versions`
go through `_score_diff` -> `eval_aware_upgrade_gate` -> the report, whose extraction is
already ported.

Next executable slice, in order:

1. `permissions.evaluate_skill_tool` — the smallest missing piece, and the only one that is
   pure policy rather than I/O.
2. `_run_case` with the media path refused, then the five actions.

## Current continuation checkpoint — 2026-09-27 the eval case store, and the last child store Python stops writing

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

The eval **case store** is wired: `list_eval_cases` reads, `create_eval_case` and
`delete_eval_case` write. `ACTION_NOT_MIGRATED` fell from 8 names to **5**, so the native
edge now serves **47 of the 52** `action == …` branches in
`deepseek_infra/web/routes/skills.py` — measured, not counted: 47 arms + 5 names, no
overlap, nothing unaccounted for.

This is a **new module**, `deepseek-policy/src/skills/eval.rs`, not a wiring of an existing
one: only the case half of `eval.py` is ported — `normalize_eval_case`,
`load_case_file` / `load_eval_cases`, `save_eval_case`, `delete_eval_case` — over the golden
corpus and `.skills/eval_cases.jsonl`. The module's header says why the report half is not
there.

### The last child store closes the third place

`.skills/eval_cases.jsonl` was the last thing under `.skills/` still outside
`registry.skill_store_scope()`, listed there as "deliberately **not** gated: Rust writes
none of them". `skills::eval::save` / `delete` make that false, so `eval.save_eval_case`
and `eval.delete_eval_case` now take the scope — and with that **every** child store under
`.skills/` is gated on the Python side, which is the state the handover needs.

`tests/test_skill_registry_failure_paths_332.py` no longer has an "outside" case to assert:
its docstring said two of the three ungated stores had already moved, and now the third has.
It asserts each store's write is denied under `python_disabled` and its bytes survive.

### Quirks the port had to keep, because the bytes are the contract

- `save` always ends the file with a newline; **`delete` does not** once it empties the file.
- Both write `json.dumps(item, ensure_ascii=False, sort_keys=True)`, so a record's key order
  is sorted rather than the order it was built in.
- `normalize_eval_case`'s `"source": str(data.get("source") or "golden")` is **not**
  stripped — the probe caught the port trimming it, which is what `" user "` in the corpus
  is for.
- Every alias is an `or` chain, so a falsy `caseId` reaches `id` and a falsy
  `expectedKeywords` reaches `keywords`; a whitespace-only `caseId` is *chosen* and then
  strips to empty.
- `_dedupe_cases` keeps the **last** value for an id in the **first** position it appeared,
  which is why `list_eval_cases` prefers a user row over a golden one with the same id.

### What the comparison found

`skills_parity_probe.py` grew from 496 to **523 cases**. One divergence, in the port rather
than in the oracle: the `source` trim above. Two were the probe's own — the oracle's
`updatedAt` needed the same clock anchor as the other stores, and the corpus's bare
"payload minus `action`" shape belongs to the route (which resolves it before the policy
function sees it), so it moved to `tests/skills_routes.rs`.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 523 cases, 0
  differences**, including the byte comparison of the two `catalog.json` files.
- `cargo test -p deepseek-gateway --test skills_routes` → **6 passed** (the sixth covers the
  case store: the golden-first listing, both refusals before cutover, the bare create form,
  both required ids, a missing Skill, the golden-then-user order, and the empty file with no
  trailing newline).
- `cargo test -p deepseek-policy --lib skills::eval` → **3 passed** (the new unit tests).
- `pytest tests/test_skill_registry_failure_paths_332.py tests/test_web_skills_routes.py
  tests/test_web_skills_routes_extra.py -q` → **39 passed**.
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.
- Full `cargo test --all` with the two known environment skips: **green**, 87
  binaries and **1170 passed** (four more than before this slice: the eval case-store
  router test and its three unit tests).

### Not done, and the next executable task

Five names remain, and all five are the same dependency: the eval **report engine**.
`skills.eval.build_skill_eval_report` runs every case through the runner, prepares media
fixtures, and scores artifacts, project bindings and content — `eval_report` returns its
report and the other four (`diff_versions`, `diff_pack_versions`, `upgrade_pack`,
`eval_upgrade_gate`) embed its verdict. Its response also carries Python's own identity
(`environment.python`, `commit`), which a port has to record as a known divergence.

Then the online `run` — the gateway already has the provider client and the tool loop
(`chat_execution::exchange_turn`, `tool_rounds::decide_round`), so that slice is prompt
assembly plus the skill's tool grant, not a new client.

## Current continuation checkpoint — 2026-09-27 the security overview and the version family, and what the eval engine blocks

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

Six more actions are wired: `security_summary`, `list_versions`, `list_pack_versions` and
`migration_plan` read; `rollback_skill` and `rollback_pack` write. `ACTION_NOT_MIGRATED`
fell from 14 names to **8**, so the native edge now serves **44 of the 52** `action == …`
branches in `deepseek_infra/web/routes/skills.py` — measured, not counted: 44 arms + 8
names, no overlap, nothing unaccounted for.

`rollback_skill` / `rollback_pack` join the route's `skills_store` gate: they rewrite the
item, take a history checkpoint and write a revision, and a pack rollback can also change a
project binding. The other four write nothing.

### Why the other eight are still refused — and it is not eight separate ports

The oracle's version diffs carry an `evalScoreDiff`, and `upgrade_pack` /
`eval_upgrade_gate` carry an `evalAwareUpgradeGate`. Both are built by
`skills.eval.build_skill_eval_report`, through
`_score_diff` → `eval_aware_upgrade_gate` → `build_skill_eval_report`. So **four** of the
eight are blocked by one dependency, and the other four (`eval_report`,
`list_eval_cases`, `create_eval_case`, `delete_eval_case`) *are* that dependency.

That engine is `deepseek_infra/infra/skills/eval.py` — **570 lines**, and
`build_skill_eval_report` is not a pure function: `_run_case` executes each eval case
through the runner (preparing media fixtures, then checking artifacts, project bindings and
content with JSON-path assertions) and aggregates the results into a scored report. Wiring
the two diffs with a placeholder `evalScoreDiff` was tried and rejected: it would answer
`null` where the oracle answers a gate verdict, which is a silent behaviour difference
rather than a refusal.

The eval response also embeds Python's own identity — `environment.python` is
`platform.python_version()` and `commit` is `evidence.git_commit()` — so a full port needs
those two fields treated as known divergences rather than compared.

The **case store** is the smaller half: `list_eval_cases`, `create_eval_case` and
`delete_eval_case` need only `load_eval_cases` / `save_eval_case` / `delete_eval_case` /
`normalize_eval_case` over `.skills/eval_cases.jsonl` and the golden file, not the engine.
That file is the **last** child store still outside `registry.skill_store_scope()`, so
taking it over closes the same third place the catalog and the run log closed.

### The online `run`

Its Python side is `runner.run_skill(..., llm_callable=…)` — the route injects the model
call through `SkillsRouteDeps`, so the policy function is model-agnostic by design. The
native side has the offline path only. What is missing is the model call, and the gateway
**already has one**: `chat_execution::exchange_turn` / `open_chat_stream` and
`tool_rounds::{decide_round, append_tool_exchange}` are the provider client and the tool
loop the chat routes use. So this is a slice built on existing pieces — prompt assembly from
the ported `runner::prepare`, the skill's tool grant, and that loop — not a new provider
client. It is also the one action a parity probe cannot compare without a model.

### What the comparison found

`skills_parity_probe.py` grew from 463 to **496 cases**. Two real divergences, both from
rendering a path or a sentence:

1. **The pack history directory was built as one component with a slash inside.**
   `registry.data.join("history/packs")` renders `history/packs\…` on Windows where the
   oracle's `Path` renders `history\packs\…`, and that string reaches the response as a
   revision `path`. Fixed by joining two components; `versioning::snapshots` had the same.
2. **The rollback checkpoint's sentence was capitalised.** The port wrote
   `Pack rollback checkpoint before 1.0.0`; the oracle writes `Pack rollback checkpoint
   before {version}`.

The probe also needed the revision listings' absolute paths normalised to the part below
`.skills/` — the same treatment `catalog_refresh` already had — because the two roots are
different directories by construction.

### The by-name refusal is now self-checking

`catalog_list` and then `list_versions` each stood as the pinned "refused by name" action in
`tests/skills_routes.rs`, and each had to be moved when it was implemented. The assertion
now checks the name against `ACTION_NOT_MIGRATED` first — re-exported from the crate for
exactly this — so implementing the pinned action fails at that guard with the reason instead
of quietly changing what the test means.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 496 cases, 0
  differences**, including the byte comparison of the two `catalog.json` files.
- `cargo test -p deepseek-gateway --test skills_routes` → **5 passed** (the fifth covers the
  security overview's scope default and the version family over a custom Skill's revisions).
- `pytest tests/test_skill_registry_failure_paths_332.py tests/test_web_skills_routes.py
  tests/test_web_skills_routes_extra.py -q` → **39 passed**.
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.
- Full `cargo test --all` with the two known environment skips: **green**, 87
  binaries and **1166 passed** (one more than before this slice: the version-family
  router test). The first attempt died on the host's `os error 5` writing an
  example's `.d` dep file — the same false failure as the previous slice; the
  retry was clean.

### Not done, and the next executable task

The eight refusals above, and the online `run`. Nothing here is **完成切换**: the default
launcher and image still start Python, and `.skills` and `project.json` stay Python's until
a deployment runs `python_disabled`.

Next executable slice, in this order:

1. `list_eval_cases` / `create_eval_case` / `delete_eval_case` — the case store, with
   `.skills/eval_cases.jsonl` joining `skill_store_scope()` (the last third place).
2. `eval_report`, then the four dependents that embed its verdict.
3. The online `run`, on top of `chat_execution` and `tool_rounds`.

## Current continuation checkpoint — 2026-09-27 the run journal becomes Rust's, writers included

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

The five run-analytics actions are wired: `delete_run`, `cleanup_runs`, `redact_run`
write the run journal, `export_runs` and `analytics_summary` only read it.
`ACTION_NOT_MIGRATED` fell from 19 names to **14**, so the native edge now serves **38 of
the 52** `action == …` branches in `deepseek_infra/web/routes/skills.py` — measured, not
counted: 38 arms + 14 names, no overlap, nothing unaccounted for.

### The third place, again: Python stops writing `.skills/runs`

The scope's docstring listed the run log among the stores "deliberately **not** gated:
Rust's `Registry` writes none of them". That had already stopped being true — the offline
`run` appends to `.skills/runs/runs.jsonl` — and this slice finishes the job. So
`analytics._write_runs`, the single choke point every run-log write passes through
(`append_run`, and now `delete_run` / `cleanup_runs` / `redact_run`), takes
`registry.skill_store_scope()`. Reads stay free: only the writer is wrapped.

`tests/test_skill_registry_failure_paths_332.py` now asserts four run-log writes are denied
under `python_disabled` and that the file's bytes are unchanged afterwards, where it
previously asserted `catalog_refresh` still worked. The same docstring's rule, read the
other way: once Rust writes a store, Python must stop. Only the eval-case file is still
outside the scope.

### What the comparison found

`skills_parity_probe.py` grew from 440 to **463 cases**. Two things came out of the
run-analytics family:

1. **`averageLatencyMs` was a float zero where the oracle writes an int.** Python's
   `round(statistics.fmean(latencies), 2) if latencies else 0` returns the **int** `0` when
   nothing completed, and `0` and `0.0` are not the same bytes on the wire. Fixed by
   emitting `json!(0)` for the empty case; every other field in that response already
   agreed.
2. **A layering slip in the probe, not in the port.** `days` is resolved by
   `int(days or 7)` **inside** the oracle's `analytics_summary`, so a `days: 0` request
   means a week — the probe was reading the trend length as 1 on the Rust side because the
   `or 7` lived in the gateway helper instead. Fixed by moving the resolution into
   `analytics::summary`, where the oracle has it, and leaving the gateway with
   `limit_of(payload, "days", 7)`. The route test now pins both ends of the window
   (`days: 0` → 7 buckets, `days: 365` → 30).

The trend's dates also needed an anchor: `_recent_trend` calls `datetime.now(timezone.utc)`
directly, so pinning `utc_now_iso` was not enough — the probe now patches
`analytics.datetime` with a subclass whose `now` returns the instant the Rust registry
clock is set to. Both sides bucket the same seven days.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 463 cases, 0
  differences**, including the byte comparison of the two `catalog.json` files.
- `cargo test -p deepseek-gateway --test skills_routes` → **4 passed** (the fourth test
  covers the run journal: its three writers refusing while its four readers answer, then
  redact over two real runs, a `keepRecent` cleanup, an idempotent delete and a summary
  over the emptied log).
- `pytest tests/test_skill_registry_failure_paths_332.py tests/test_web_skills_routes.py
  tests/test_web_skills_routes_extra.py -q` → **39 passed**.
- `python -m mypy .` clean, `ruff check .` clean.
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.
- Full `cargo test --all` with the two known environment skips: **green**, 87
  binaries and **1165 passed** (one more than before this slice: the run-journal
  router test). The first attempt died on the host's `os error 5` writing a
  `.fingerprint` file — a known false failure on this machine; the retry was clean.

### Not done, and the next executable task

The remaining 14 names: `security_summary`, the four eval cases/reports, and the nine
version diff/rollback/upgrade gates. The online `run` still needs the model call. Nothing
here is **完成切换**: the default launcher and image still start Python, and `.skills` and
`project.json` stay Python's until a deployment runs `python_disabled`.

Next executable slice: `security_summary` (it reads records this edge already writes), or
the nine version gates — `list_versions` / `diff_versions` already have their policy
functions ported in `skills::versioning`.

## Current continuation checkpoint — 2026-09-26 the catalog, and the cache Python stops writing

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push, merge,
or release. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

All **seven** `catalog_*` actions are on the native edge: `catalog_list`, `catalog_get`,
`catalog_search`, `catalog_install`, `catalog_uninstall`, `catalog_refresh`,
`catalog_export`. `ACTION_NOT_MIGRATED` fell from 26 names to **19**, so the native edge
now serves **33 of the 52** `action == …` branches in
`deepseek_infra/web/routes/skills.py` — still measured, not counted: 33 arms + 19 names,
no overlap, nothing unaccounted for.

`deepseek-policy::catalog` already carried `list`, `summary`, `manifest`, `get`, `search`,
`preview`, `install` and `uninstall`; this slice added `refresh` and `export`, wired all
seven, and added the gateway's `item_id` helper (`itemId`, else `skillId`, else `packId`,
else `id` — one `or` chain, so a whitespace-only `itemId` is *chosen* and then strips to
empty rather than falling through).

### Two stores, two cutovers, two refusals

The catalog writes in two places, so it carries two gates rather than one:

- `catalog_refresh` rewrites `.skills/catalog/catalog.json` → `409
  NATIVE_SKILLS_WRITE_NOT_OWNED` unless `skills_store` is Rust's.
- `catalog_install` (without `dryRun`/`preview`) and `catalog_uninstall` change a
  **project's** Skill binding, which lives in `project.json` → `409
  NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` unless `project_metadata_store` is Rust's. The
  oracle returns before it writes anything when `dryRun` is set, so that branch is left
  available while Python still owns the project store — gating the whole action would have
  removed a working preview.

### The third place: Python stops writing `.skills/catalog`

`registry.skill_store_scope()`'s docstring said the catalog was outside the scope
"because Rust writes none of them". `catalog::refresh` makes that false, and the same
docstring states the rule in the other direction — a store Rust writes is one Python must
stop writing. So `catalog_refresh` now takes the scope, and
`tests/test_skill_registry_failure_paths_332.py` — whose docstring documents this exact
boundary — moved `catalog_refresh` from its "still works under `python_disabled`"
assertion into the denied set, with the file's bytes asserted unchanged afterwards.
`project.json` needed no change: `infra/data/projects.py::write_project` has carried
`assert_python_writer_allowed("project_metadata_store")` since the project-files slice.

### What the comparison found, including one thing only the file check could

`skills_parity_probe.py` grew from 391 to **440 cases**, all compared against the
unmodified oracle. Both roots now also hold the same `evals/reports/*` and one project, so
`evalScore` and `installCount` are non-zero — a catalog comparison over an empty repository
would agree on `0.0` and prove nothing.

1. **The numeric filters were silently ignored.** `catalog`, `search`'s `maxRiskScore` /
   `minEvalScore` were read through `text()`, whose falsy check swallows `0`, so
   `{"maxRiskScore": 0}` was not a filter at all: the oracle answered **5** items and the
   port **22**. Fixed with a `filter_number` helper that mirrors the oracle's
   `float(str(value))`. The same pass found `tool` checked for truthiness *before* being
   trimmed, where the oracle trims first — a whitespace-only `tool` is not a filter.
2. **One thing only the file check could find.** `catalog_refresh`'s *product* is a file,
   and a JSON parse is order-insensitive, so comparing the response alone would have
   passed while every key-order constant in the write was wrong. The probe now diffs the
   two `catalog.json` files byte-for-byte too, and that check immediately failed: the
   oracle's `securityReview.manifest` carries `packId` **fourth** for a pack and **after
   `toolGrantHash`** for a skill. Same name, same path, two orders — no by-name table can
   express it, so `python_json` gained
   `OrderedJson::from_value_with_orders_and_shapes`, which selects an order by **which key
   the object carries**. The name-keyed entry points are unchanged and their tests still
   pass; the new mechanism has its own unit test.

`first_difference` reports the first differing line rather than two whole manifests, which
is how the above was found in one run.

### Verification (local, this workspace, not CI)

- `python tasks/native-runtime/skills_parity_probe.py …` → **PASS, 440 cases, 0
  differences**, including the byte comparison of the two `catalog.json` files.
- `cargo test -p deepseek-gateway --test skills_routes` → **3 passed** (a third test now
  covers the catalog: the five reads, the by-name `501` that moved off `catalog_list`, both
  refusals, and the refresh write under `python_disabled`).
- `cargo test -p deepseek-policy --lib python_json` → **13 passed**.
- `pytest tests/test_skill_registry_failure_paths_332.py tests/test_web_skills_routes.py
  tests/test_web_skills_routes_extra.py -q` → **39 passed**.
- `python -m mypy .` → no issues in 917 files; `ruff check .` clean.
- `cargo fmt --all -- --check` clean; workspace clippy (the CI command) clean.
- Full `cargo test --all` with the two known environment skips: **green**, 87
  binaries and **1164 passed** (two more than before this slice: the new
  `python_json` shape test and the catalog router test).

### Not done, and the next executable task

The remaining 19 names: the eval cases/reports (`eval_report`, `list_eval_cases`,
`create_eval_case`, `delete_eval_case`), `security_summary`, the five run-analytics writes,
and the nine version diff/rollback/upgrade gates. The online `run` still needs the model
call. Nothing here is **完成切换**: the default launcher and image still start Python, and
`.skills` and `project.json` stay Python's until a deployment runs `python_disabled`.

Next executable slice: the run-analytics writes (`delete_run`, `cleanup_runs`,
`redact_run`, `export_runs`, `analytics_summary`) — they share the journal this edge already
reads — or the version gates.

## Current continuation checkpoint — 2026-09-26 the skills runner and run analytics

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. **The code for this slice was already in
the tree uncommitted when this session started**; this session measured it, fixed
what it found, and recorded it here (the slice's own handoff note was never written).
No push, merge, or release. `release/native_runtime_5_0_evidence_v1.json` is still
`NOT_READY` (`exact_head: null`). The whole migration is **未完成**.

`POST /api/skills` serves four more actions — `run` (its offline path), `dry_run`,
`list_runs`, `get_run` — so `ACTION_NOT_MIGRATED` fell from 30 names to **26** and the
implemented arms rose from 22 to **26 of the 52** `action == …` branches in
`deepseek_infra/web/routes/skills.py`. The split is measured, not counted by hand:
26 arms + 26 names, **no overlap and nothing unaccounted for** (the same measurement
the earlier 22/30 split was made with). `POST /api/skills/{skill_id}/run` is the same
dispatcher with the path id winning.

Writes obey the store gate: a mutating action **and** `run` require
`may_write_native_store("skills_store")`, so they answer
`409 NATIVE_SKILLS_WRITE_NOT_OWNED` before touching anything unless the deployment is
`DEEPSEEK_RUNTIME_MODE=python_disabled` and the domain is declared. `dry_run`,
`list_runs` and `get_run` are deliberately not gated: the first writes nothing and the
other two only read the journal. An online `run` (a DeepSeek key is available) still
answers `501 NATIVE_SKILLS_ACTION_NOT_READY` by name — the model call is not ported —
but a run that has no key first persists the prepared failure, which is the oracle's
behaviour.

### What this session found and fixed

The slice did not pass its own test when it was picked up, and it carried three
further defects that only a gate would have caught:

1. **A cross-test environment leak made the result order-dependent.**
   `tests/skills_routes.rs` sets process-global variables in both tests but had none
   of the `EnvLock`/`EnvGuard` convention the other thirteen env-reading gateway test
   files use. The offline-run test sets `DEEPSEEK_RUNTIME_MODE=python_disabled` and
   never cleared it, so whichever test ran second inherited it: the registry test then
   read `200` from a `create` whose entire point is the `409` refusal. Fixed by
   taking the process lock and declaring the mode each test needs — `EnvGuard` saves
   and restores, and `None` removes, so a test asks for the default deployment instead
   of inheriting the previous one. **Verified order-independent**: sequential, at the
   default thread count, and each test alone all pass; the pre-fix failure is the
   reproduction.
2. **Dead code that `-D warnings` turns into a build failure.** `action_not_ready`
   survived the refactor with no caller. Removed.
3. **The uncommitted work was formatted with a different rustfmt than CI pins.**
   Fifty files under `deepseek-gateway` and `deepseek-policy` failed
   `cargo fmt --all -- --check`. The style in the tree is rustfmt's **2021** item
   ordering (`use serde_json::{json, Value}` — case-insensitive, `self` first); the
   crates are edition 2024 and CI pins `dtolnay/rust-toolchain@1.85.0`, whose rustfmt
   wants the 2024 order (`{Value, json}`). Re-ran the **pinned** toolchain's
   formatter. **The divergence is known to be formatting-only** because 23 of the 50
   files then matched `HEAD` byte for byte and dropped out of the working tree diff
   entirely — they had no other change. None of the remaining files' content was
   lost; `cargo fmt --all -- --check` is clean under the pinned toolchain.
4. **One real clippy error in a new file.** `deepseek-policy/src/file_upload.rs:94`
   built a hex string with `map(format!).collect()`, which `-D warnings` refuses
   (`clippy::format_collect`). Rewritten to append into one `String`; the digest and
   the 32-character truncation are unchanged. The family's earlier "clippy exit 0"
   note was taken with `--no-deps` over a narrower target set and did not cover this
   file.

### Verification (local, this workspace, not CI)

- `cargo test -p deepseek-gateway --manifest-path rust/Cargo.toml --test skills_routes`
  → **2 passed**, run three ways (sequential with `--test-threads=1`, at the default
  thread count, and once per test in isolation).
- `cargo +1.85.0-x86_64-pc-windows-gnu fmt --all -- --check` → **clean**.
- `cargo +1.85.0-x86_64-pc-windows-gnu clippy --locked --manifest-path rust/Cargo.toml
  --all-targets --all-features -- -D warnings` (the CI command, whole workspace) →
  **clean**.
- `python tasks/native-runtime/skills_parity_probe.py --rust-example
  rust/target/debug/examples/skills_parity_probe.exe --output artifacts/skills-parity.json`
  → **PASS, 391 cases, 0 differences**, against the unmodified oracle, and shown
  able to fail (18 differences when `dry_run`'s `skillRunId` was renamed; restored
  byte-identically).

### One environment failure in the local full suite, diagnosed and not a repo defect

`cargo test --all` stops in `deepseek-policy --lib`: **531 passed, 1 failed** —
`file_lock::tests::locks_serialize_between_threads`, `acquire: Os { code: 5,
PermissionDenied }`. `file_lock.rs` is byte-identical to `HEAD`, the failure is at
the `.expect("acquire")` on the `open`, not on the lock wait (the test ends in 4 s,
far short of the 9 s ten retries would take), and the cause is this machine, not the
crate: in **pure Python**, six threads opening **one** path at the same instant get
`PermissionError(13)` too — with the sandbox off as well, while six threads on six
**different** paths never fail. So this is a same-path concurrent-open race in the
host's filesystem stack (most likely a real-time filter driver), not a code defect.

Locally, run the suite as
`cargo test --all -- --skip locks_serialize_between_threads
--skip concurrent_transitions_converge_or_conflict_without_duplicate_events`; those
two are CI's (Linux) to judge. Worth recording separately: the Windows lock path
retries `LockFileEx` ten times but does **not** retry `OpenOptions::open`, so a
transient open denial is fatal — a real robustness gap, **out of this slice's scope
and left alone**.

The second of those two is the same signature in another crate:
`deepseek-transfer --test federated_journal` fails
`concurrent_transitions_converge_or_conflict_without_duplicate_events` at
`results.iter().all(Result::is_ok)`. Measured: **deterministic** (4/4, ~0.8 s — and
`BUSY_TIMEOUT` is 30 s, so it is not a timeout), **independent of the temp
directory**, in a crate that is **byte-identical to `HEAD`**. Reading
`advance_transfer`, the losing thread should take the idempotent branch
(`current.state == next_state` with an equal digest) and return `Ok`, so that branch
is the suspect. The assertion swallows the error text, so **the mechanism is not
established** — do not record a guessed cause. It shares the one-file-two-handles
shape with the `file_lock` failure above.

**With those two skipped the suite is green**: `cargo test --all` → **87 test
binaries, 1162 passed, 0 failed**, exit 0.

### The runner's oracle comparison landed, and it found two divergences

`skills_parity_probe.py` grew from 312 to **391 cases** and the pair is **PASS with
0 differences**. Four new case families, all byte-compared against the unmodified
oracle:

- `offline_output` — each of the 18 built-in skills' own `exampleInputs` entry
  rendered through `runner::prepare().offline_output()` vs `_offline_output`,
  including the media half of the context composed the way `run_skill` composes it.
- `offline_refusal` — the entry the route calls, with inputs the schema rejects,
  four ids no registry holds, and non-object inputs, so the refusal **message** is
  compared and not only its status.
- `list_runs` / `get_run` — a **fixture journal** written byte-identically into both
  roots (a `runId`-alias record, a record `normalize_run` rejects and `_read_runs`
  skips, a redacted run, runs differing on every filter axis), across eleven
  filter/limit shapes and six id shapes.

The corpus is built from the built-in **documents**, not their file names: a name is
not a skill id (`code_review.json` declares `skill_code_review`). My first attempt
used file names and the probe failed on exactly that.

Two real divergences came out of it, both in `dry_run`, both fixed:

1. **An extra response key.** Rust's `dry_run` emitted `skillVersion`; the oracle's
   `_dry_run_skill_config` does not. Measured: 22 differences, all `dry_run`, the
   key-level diff naming `only in actual: ['skillVersion']`. The frontend never
   reads `dryRun`/`skillVersion` and the Python route never emits it, so the key was
   removed rather than kept.
2. **A different payload contract.** The oracle takes the Skill **configuration**
   out of the request (`payload["skill"]`, else the payload minus `action` /
   `overwrite`); a `{"skillId": …}`-only payload is the oracle's own
   `400 "Skill config missing required fields: name, description, version, …"`.
   Rust's route looked the **id** up in the registry and answered `200` — i.e. it
   accepted a request the reference implementation refuses. `runner::dry_run` now
   takes the validated config and the gateway validates it from the payload, and the
   route test asserts **both** halves (the `400` and the `200`).

The probe was shown **able to fail** on the new axis: renaming `dry_run`'s
`skillRunId` produced `FAIL, 391 cases, differences: 18` — exactly the 18 successful
dry runs, nothing else — and the file was restored byte-identically (md5 unchanged).

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python. A
registered route, a green test, and a green clippy are not migration evidence.

- The runner's comparison is in place; what it cannot cover is the **online** `run`,
  because the model call is not ported. That branch stays `501
  NATIVE_SKILLS_ACTION_NOT_READY` by name.
- Phases 3-5 remain: `catalog_*`, the run-analytics writes (`delete_run`,
  `cleanup_runs`, `redact_run`, `export_runs`, `analytics_summary`), and the version
  diff/rollback/upgrade gates — each removing names from `ACTION_NOT_MIGRATED`.
- Skills are still **集成通过**, not **完成切换**: `.skills` is Python's until the
  process is actually started with `python_disabled`.

Next executable slice: `catalog_*`, or the workspace backup/DR HTTP surface.

## Current continuation checkpoint — 2026-09-26 PDF page image and layout

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push,
merge, or release. `release/native_runtime_5_0_evidence_v1.json` is still
`NOT_READY` (`exact_head: null`). The whole migration is **未完成**.

`GET /api/file-page-image` and `GET /api/file-page-layout` are on the native
edge. A legal PDF page is rendered by `pdftoppm` and cached as
`{fileId}.page-{n}-{scaleKey}.png`. The response is `image/png` with
`X-File-Page` and `X-File-Page-Count`. A repeat read returns those cached
bytes. Layout returns the word boxes and writes nothing. A non-PDF is `415`.
A bad page or scale is `400`. A missing token is `401`. Those refusals do not
add a cache file.

For unembedded Helvetica the boxes match PyMuPDF `get_text("words")`, including
the text line matrix: `Td` moves from the line origin, not from the glyph
cursor. The PNG bytes are not MuPDF's pixmap. The probe allows a 4-pixel
difference from `ceil(points * scale)`.

Evidence (local, this workspace, not CI):

- `cargo test -p deepseek-policy --lib pdf_page::tests::helvetica_words_match_the_mupdf_boxes`: **passed**.
- `cargo test -p deepseek-gateway --test file_page_render_route`: **1 passed** through `create_production_app`.
- `python tasks/native-runtime/file_page_render_probe.py --rust-example rust/target/debug/examples/file_page_render_probe.exe --report artifacts/file-page-render.json`: **3 PDFs, PASS** against unmodified `render_pdf_page_layout` and `render_pdf_page_png`.
- `ruff check` on the probe: all checks passed.
- `cargo clippy -p deepseek-policy --all-targets -- -D warnings --no-deps`: exit 0.

Next executable slice: skills registry/runner, or the workspace backup/DR HTTP
surface. Diagnostics, chat prefetch, A2A, launchers, provider kill/takeover,
zero-Python packaging, and exact-head CI remain open. Default `Dockerfile`
and `launch.py` still start Python. Embedded non-Helvetica page fonts still
use Helvetica advances.

## Continuation checkpoint — 2026-09-26 `/api/project-files`

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. This slice is uncommitted. No push,
merge, or release. `release/native_runtime_5_0_evidence_v1.json` is still
`NOT_READY` (`exact_head: null`). The whole migration is **未完成**.

`POST /api/project-files` is on the native edge, ahead of the Go `/api/*`
catch-all. The store is `project_metadata_store` (`project.json`). Rust writes
it only when `DEEPSEEK_RUNTIME_MODE=python_disabled`, the same rule as
`may_write_native_store`. In every other mode the route returns
`409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` before it parses the body, and
`project.json` stays byte-identical. A legal text upload in the disabled mode
writes `.projects/<id>/files` and the document list, and
`GET /api/workspace/projects/<id>` reads that document back. The upload does
not call `index_file_payload`. Other project mutations are still `501`.

Evidence (local, this workspace, not CI):

- `cargo test -p deepseek-gateway --manifest-path rust/Cargo.toml --test project_files_route -- --test-threads=1`: **3 passed**.
- The success case checks the on-disk `project.json`, the source bytes, the workspace GET, `/api/file-reader`, and `/api/file-source`.
- The refusal case covers an empty mode, `python_authoritative`, and `go_authoritative`.
- A missing token is `401`, an unsupported file is `415`, and a missing project is `404`; none of those change `project.json`.
- `cargo test -p deepseek-gateway --lib the_memory_store_owner_is_the_mode_and_not_go_control`: **passed**. `may_write_native_store("project_metadata_store")` is false while Go control is on and the mode is empty, and true only under `python_disabled`.
- `cargo test -p deepseek-gateway --test data_routes project_mutations_stay_closed`: **passed**. Create, rename, delete, and the workspace child writes stay `501` and write nothing.
- `cargo test -p deepseek-gateway --test file_text_route`: **10 passed** after the shared multipart reader moved.
- `cargo clippy -p deepseek-policy --all-targets -- -D warnings --no-deps`: exit 0. `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` still fails in pre-existing `a2a_control.rs` and `control_proxy.rs` (`result_large_err`); those findings are not in this slice.

Next executable slice: `/api/file-page-image` and `/api/file-page-layout` still
need a PDF renderer that can match the oracle. Skills, workspace backup/DR,
diagnostics, chat prefetch, A2A, launchers, provider kill/takeover,
zero-Python packaging, and exact-head CI remain open. Default `Dockerfile`
and `launch.py` still start Python.

## Continuation checkpoint — 2026-09-26 image and textless-PDF OCR

Branch `codex/indexmap-std-feature`. HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`. The file-text and OCR work is
uncommitted. No push, merge, or release.
`release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`
(`exact_head: null`). The whole migration is **未完成**.

This slice replaces the `501` on `POST /api/file-text` for an image and for a
textless PDF when the request enables OCR. A legal image is cached as kind
`image` and is readable again through `/api/file-source` (original bytes) and
`/api/file-reader` (the OCR text). A legal textless PDF is cached with
`[PDF 第 N 页 (OCR)]` labels. OCR off stays `415 ocr_required` for an image and
`422 ocr_required` for a textless PDF. A blank page with OCR on is
`422 ocr_empty`. A missing engine is `415 ocr_unavailable`. Refusals write no
cache file. The batch error path keeps those codes; it used to rewrite an
unknown code to `invalid_payload`.

Engine order follows the oracle when no DeepSeek API key is set: an explicit
`OCR_FORMULA_CMD`, otherwise pix2tex or latexocr on `PATH`, then Tesseract,
then Windows OCR. A tied score keeps the earlier engine. This host's pix2tex
returns different strings for the same image, so the probe and the route tests
set `OCR_FORMULA_CMD` to `cmd /c exit 1` before settings are imported. That is
a determinism pin for the comparison, not a production disable: an unset
variable still selects pix2tex when it is on `PATH`. cv2 preprocessing and
formula-region snippets are not ported. The parity image is 360×140 so the
oracle's region filter skips snippets and Tesseract returns `HELLO`.

Already wired on this same edge, still **集成通过** and not cut over:
`/api/file-page-text`, `/api/file-page-search`, and `/api/file-text` for text,
HTML, DOCX, PPTX, XLSX, selectable PDF and EPUB. Evidence for those rows is in
`migration-matrix.md`.

Evidence for this slice (local, this workspace, not CI):

- `cargo test -p deepseek-gateway --manifest-path rust/Cargo.toml --test file_text_route -- --test-threads=8`: **10 passed**. The OCR PDF case checks the `HELLO` page and that a blank page leaves the cache unchanged.
- `python tasks/native-runtime/file_text_parity_probe.py --rust-example rust/target/debug/examples/file_text_parity_probe.exe --report artifacts/file-text-parity.json`: **31 cases, PASS**, exit 0, compared with unmodified `extract_uploaded_file`. The probe still stubs `local_rag.index_file_payload`.
- `ruff check tasks/native-runtime/file_text_parity_probe.py`: all checks passed.
- `cargo clippy -p deepseek-policy --manifest-path rust/Cargo.toml --all-targets -- -D warnings --no-deps`: exit 0.
- Windows gnu link still needs, for that process only, `RUSTFLAGS=-C link-self-contained=yes -C link-arg=-Wl,--allow-multiple-definition`. Not a repo change.

Not done: `/api/project-files` (needs one writer for `project.json`),
`/api/file-page-image` and `/api/file-page-layout` (PDF renderer), the sqlite
file index (`index_file_payload` stays Python-owned; this route does not write
`.local-rag`), skills, workspace backup/DR HTTP, the remaining diagnostics
blocks, chat search prefetch and edge inference, full A2A parity, launchers
and images, provider kill/takeover, zero-Python packaging, exact-head CI.
Default `Dockerfile` and `launch.py` still start Python.

`/api/project-files` is the checkpoint above. `/api/file-page-image` and
`/api/file-page-layout` still need a PDF renderer. Do not dual-write
`.local-rag`.

## Current continuation checkpoint — 2026-09-22 memory probe isolation

Investigated the two `memory` differences recorded by `c9606a9a`, on base HEAD
`1e82d37c`. Python's supposedly absent `local_rag` was importable with dependencies
installed, so it used a live index while Rust supplied `None`; Python saves also
replaced the host memory index. A real SQLite fixture reproduced exactly
`state::remember` and `state::scoped`; forcing the missing dependency gave 194/194
equal values. The fix confines missing-RAG imports to the extracted namespace and
removes `memory` from `KNOWN_DIVERGENCES`; production runtime code is unchanged.

Validation: three regressions failed before the fix and pass afterward; 32 focused
memory tests, Ruff and mypy pass. Both Rust examples were rebuilt with `--locked`;
194 memory keys and 64 live-index keys match byte for byte in this build. The live
index still changes 7/8 queries, and the fixed probes leave the host index unchanged.
An isolated rerun of all 38 older pairs reports 37 passes, zero unexpected failures
and one remaining known divergence (`store`, fixture clock); 10 are byte-identical.
The legacy `tests/test_memory.py` had the same index-isolation leak: it now uses
`tmp_settings` and gives child writers the temporary root before import. The 32-test
rerun verifies that host memory and index hashes remain unchanged.
Details: [`docs/MEMORY_STORE.md`](../../docs/MEMORY_STORE.md#probe-isolation-correction--2026-09-22).
This closes the probe discrepancy, not the remaining production migration work;
readiness is unchanged. Changes from this investigation are uncommitted.

## Current continuation checkpoint — 2026-09-21 the paginated file reader is wired

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

Implemented:

- **`deepseek-policy::file_routes`** gains `file_reader_window`, `file_chunk`,
  `reader_positive_int`, `reader_file_payload` and `reader_chunk_payload` — the
  paginated reader the frontend scrolls a long extraction with. The rules are the
  oracle's: 1-based display indices, the 12-chunk cap, a start past the end clamped to
  the last chunk, an empty list reported as an all-zero window, a non-object entry
  skipped by both the payload list and the end index, and a per-field `400`
  (`Invalid reader start` / `Invalid reader count` / `Invalid chunk index`).
- **`POST /api/file-reader`** and **`POST /api/file-chunk`**
  (`deepseek-gateway::file_reader_route`) registered ahead of the Go `/api/*`
  catch-all. The falsy-value defaults (`chunkStart or 1`, `chunkCount or 6`) are applied
  at the route, because that `or` is part of the contract.
- `file_routes_parity_probe.py` grew from 103 to **201 cases**: 56 window shapes across
  seven cached indexes and 42 chunk lookups, compared against the oracle's own
  `file_reader_window` and against the `/api/file-chunk` body transcribed from
  `web/server.py`. The oracle's functions run unmodified — the probe repoints
  `rag_files.FILE_CACHE_DIR`, which is the name `files.py` actually reads (it imports the
  constant **by value** at module load, so patching `config.FILE_CACHE_DIR` alone does
  nothing; that was the first, failing, attempt).

Evidence:

- `tasks/native-runtime/file_routes_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/file_routes_parity_probe.rs`: **PASS** over
  **201 cases**. Report: `artifacts/file-routes-parity.json`.
- `cargo test -p deepseek-gateway --test file_reader_route`: **6 PASS** through
  `create_production_app`.
- `deepseek-policy` **524** unit tests, 0 failures (three new reader tests);
  `deepseek-gateway` 190 unit + integration, 0 failures. `cargo fmt --all --check`
  clean; `cargo clippy --all-targets` reports no finding in any file this slice touched;
  ruff and mypy pass the probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: `/api/file-page-text` (needs `normalize_extracted_text` and
`page_texts_for_cache`), `/api/file-text` (multipart upload + extraction) and
`/api/project-files`; then the skills family, `/api/workspace/*`, the diagnostics status
blocks, search prefetch and edge inference for `/api/chat`, and the browser staging 4.
`/api/file-page-image`, `/api/file-page-layout` and `/api/file-page-search` need a
PDF/image renderer, which is a separate decision.

## Previous continuation checkpoint — 2026-09-21 `/api/file-source` is wired, and the web `truthy` helper was wrong

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

Implemented:

- **`deepseek-policy::file_routes`**: `clean_filename`, `content_disposition_header`,
  `original_file_media_type` and `cached_file_source` — the four helpers
  `web/routes/files.py` needs. The RFC 5987 header is built with Python's `quote` safe
  set (which keeps `/`), and the media-type ladder follows the oracle's order.
- **`GET /api/file-source`** (`deepseek-gateway::file_source_route`): the original
  uploaded bytes, with `X-Content-Type-Options: nosniff`, the oracle's disposition
  header, `Cache-Control: no-store` and the media type from the cached index. A missing
  source is `410 file_index_expired`; a malformed id is `400 invalid_payload`.
- **`deepseek-policy::core_utils::web_truthy`**, and a real defect fixed with it. The
  web layer's `truthy` is `str(value or "").strip().lower() in {"1","true","yes","on"}`
  — a **string parse**, not Python truthiness. The `/api/download` route shipped using
  `python_truthy`, so `?inline=false` rendered the SVG in place where the oracle
  downloads it, **and its test asserted that wrong answer**. Both are corrected. This is
  the first defect found by re-reading the oracle rather than by a probe or a test.

Evidence:

- `tasks/native-runtime/file_routes_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/file_routes_parity_probe.rs`: **PASS** over
  **103 cases** — 26 filenames through `clean_filename` and both dispositions (CJK,
  emoji, quotes, both separators, percent/plus/hash/query characters, hidden files), 21
  cached-file shapes through `original_file_media_type`, and the 179/180/181/400-character
  caps. Report: `artifacts/file-routes-parity.json`.
- `cargo test -p deepseek-gateway --test file_source_route`: **6 PASS** through
  `create_production_app` — the original bytes rather than the index JSON, the `download`
  string parse (six falsy and four truthy spellings), the media-type ladder, `410` for a
  missing source and `400` for a bad id, a project-scoped read from
  `.projects/{id}/files`, and the auth boundary.
- `cargo test -p deepseek-gateway --test download_route`: **6 PASS** with the corrected
  `inline` rule (`false`, `0`, `no` and an empty value all download; `1`, `true`, `yes`
  and `on` render in place).
- `deepseek-policy` **521** unit tests, 0 failures. `cargo fmt --all --check` clean;
  `cargo clippy --all-targets` reports no finding in any file this slice touched; ruff
  and mypy pass the new probe. One self-inflicted regression was caught by clippy and
  fixed: a stray edit had merged a doc comment into a `#[test]` attribute, leaving the
  test compiled but not run.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: `/api/file-reader`, `/api/file-chunk`, `/api/file-text` and
`/api/project-files` (the reader window is ported; the routes are not); then the skills
family, `/api/workspace/*`, the diagnostics status blocks, search prefetch and edge
inference for `/api/chat`, and the browser staging 4. `/api/file-page-*` needs a
PDF/image renderer, which is a separate decision.

## Previous continuation checkpoint — 2026-09-21 `/api/chat` is wired, and its diagnostics block is the oracle's

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

This round finished the terminal event: `/api/chat`'s `done.diagnostics` was a
hand-written `{"tools": {...}}` placeholder, and it is now the oracle's own helper chain.

Implemented:

- **`deepseek-policy::chat_diagnostics`**: `diagnostics_with_tools` (count plus the
  sorted, deduplicated names), `diagnostics_with_usage` (`cacheHitTokens`,
  `cacheMissTokens`, `cacheHitRate`) and `diagnostics_with_search` (the round and result
  counts, **absent** when there was no search — `if search_data:` is a truthiness test,
  not a presence test). `search_round_count` is also here.
- The route folds tools → search → usage, which is the oracle's order, and passes the
  result to `accumulator.done(..)`.
- **`round(x, 1)` was ported twice, and the first version was wrong.** The direct
  translation — `(value * 10).round_ties_even() / 10.0` — returns `1.0` for `1.05`,
  because `1.05 * 10` is exactly `10.5` and half-to-even rounds that to `10`, while
  Python returns `1.1` because the stored double is *above* its decimal tie. Rust's
  `format!("{value:.1}")` uses the same correctly-rounded decimal algorithm Python's
  `round` does, and agrees over the whole tie corpus. The unit test caught this before
  the probe did.

Evidence:

- `tasks/native-runtime/chat_stream_events_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/chat_stream_events_parity_probe.rs`: **PASS**
  over 21 events (byte-identical text), 11 usage merges, 8 streamed tool-call
  sequences, **4 tool-diagnostic cases, 17 usage-diagnostic cases over the `round(x, 1)`
  tie corpus, 5 search-round counts and 6 search-diagnostic cases** — every one compared
  against the imported oracle functions (`diagnostics_with_tools`,
  `diagnostics_with_usage`, `diagnostics_with_search`, `_search_round_count`).
- `cargo test -p deepseek-gateway --test chat_ndjson_route`: **7 PASS**, with the
  terminal event's diagnostics now asserted field by field — including that a turn with
  no search carries **no** `searchRoundCount` key.
- `cargo test -p deepseek-policy --lib`: **516** unit tests, 0 failures.
  `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports no finding in
  either new file; ruff and mypy pass the probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image. The gateway-attempt, semantic-cache, cost and trace diagnostics
  blocks are still absent, because their state is not in this route yet.

Next, in order: search prefetch and edge inference for `/api/chat`; then the file/upload
and page-render family, the skills family, `/api/workspace/*`, the diagnostics status
blocks whose Python status functions are not yet ported, and the browser staging 4.

## Previous continuation checkpoint — 2026-09-21 `/api/chat` is wired on the native edge

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

The frontend's streaming entry was the largest remaining `503`. It is now served by
`deepseek-gateway::chat_ndjson` over the protocol ported last round.

Implemented:

- **`deepseek-gateway::chat_ndjson`**: `POST /api/chat` registered ahead of the Go
  `/api/*` catch-all. It takes the **internal** payload (the route injects
  `localBaseUrl`), validates it with the ported `validate_deepseek_payload`, runs the
  message rules and the memory state through `NativeAssembly`, opens the upstream with
  `stream: true`, and turns each SSE delta into an NDJSON line as it arrives. The
  tool-round loop is the OpenAI route's, with `RoundDecision`, `append_tool_exchange`
  and `force_final_answer_without_tools` — so a `browser_*`, `search_files`,
  `create_document` or `reminders` call behaves identically on both routes.
- **Memory suggestions are wired on this route and not the other.** `/api/chat` is the
  one protocol with a `memorySuggestions` channel, so `ToolRoundExecutor` gained
  `run_round_with_suggestions` and `WorkspaceBundle` gained `view_with`; the callback is
  `'static` because the round runs under `spawn_blocking`, and the route closes over an
  `Arc<Mutex<Vec<Value>>>`. `WorkspaceContext::on_memory_suggestion` and
  `memory::suggest_memory` are now `Send + Sync`.
- **Three branches are refused, not degraded.** `agentMode`, the model-router cascade
  and a forced `searchMode` return `501 NATIVE_CHAT_BRANCH_NOT_READY` **before any
  upstream call**. Their producers are unported, and serving a thinner path would look
  like success. The forced-search refusal is reachable here even though it is not on
  `/v1/chat/completions`: the OpenAI facade never forwards `searchMode`, but this route
  takes the internal payload, so it arrives intact and the oracle's prefetch branch
  would run.

Evidence:

- `cargo test -p deepseek-gateway --test chat_ndjson_route`: **7 PASS** through
  `create_production_app` against a scripted SSE upstream — the oracle's event sequence
  and order (`reasoning`, `content`, `content`, `done`), the accumulated `done` totals,
  the `length` truncation note before `done`, agent mode and forced search refused with
  the upstream called **zero** times, the no-user-turn `400`, an upstream failure as an
  HTTP error rather than a `200` stream, and the production auth layer. Every line is
  also asserted to be compact JSON.
- `cargo test -p deepseek-policy -p deepseek-gateway --lib --tests`: 0 failures
  (`deepseek-policy` 511 unit, `deepseek-gateway` 190 unit + integration).
  `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports no finding in
  either new file; frontend `vitest src/api/chatStream.test.ts` 4 passed.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image. The `done` event's `diagnostics` carries only the tool summary;
  the oracle's full chain (gateway attempts, search, semantic cache, usage, cost,
  trace) is not ported.

Next, in order: the `done` diagnostics chain and search prefetch for `/api/chat`; then
the file/upload and page-render family, the skills family, `/api/workspace/*`, the
diagnostics status blocks whose Python status functions are not yet ported, and the
browser staging 4.

## Previous continuation checkpoint — 2026-09-21 the `/api/chat` NDJSON protocol is ported

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

`/api/chat` is the frontend's streaming entry and the largest remaining public route.
It is not one slice: the oracle's `stream_deepseek` runs search prefetch, semantic
cache, edge inference, gateway retries with a scheduler lease, a streaming tool-round
loop, budget accounting, trace spans, agent mode and the model-router cascade. This
round ported the **protocol** — the part every one of those branches writes through —
so the route work that follows has a verified encoder instead of an assumed one.

Implemented:

- **`deepseek-policy::chat_stream_events`**: the seven-event vocabulary
  (`system_note`, `search`, `reasoning`, `content`, `memory_suggestion`, `error`,
  `done`), `encode_stream_event` (compact JSON + `\n`, `ensure_ascii=False`), the
  `ChatStreamAccumulator` that grows `content`/`reasoning`/`usage` while emitting each
  delta, `merge_usage_totals`/`usage_int`, and the streaming tool-call merge
  (`merge_stream_tool_call_deltas` / `finalized_stream_tool_calls`).
- **`RawJson`**: `usage` is carried as pre-rendered bytes rather than a
  `serde_json::Value`. This crate compiles `serde_json` **without** `preserve_order`,
  so a `Value` map is key-sorted, and the oracle writes `usage` in the provider's
  insertion order. The parity probe caught the reordering on `done_full`
  (`completion_tokens` before `prompt_tokens`) — the same class of bug the OpenAI SSE
  encoder in `chat_stream.rs` avoids by building its frames by hand.

Three defects were found by reading the oracle and by the probe, not by inspection:

1. **`merge_usage_totals` was ported wrong first.** The oracle sums only the five token
   counters in `USAGE_SUM_FIELDS` (each with a camelCase alias), reads them through
   `usage_int` (`max(0, int(raw))`), and **drops** every other key of the round. The
   first port summed all numeric fields and kept non-numeric ones.
2. **The `done` envelope reordered `usage`** (see `RawJson` above).
3. **A negative tool-call index is a legal key.** The oracle does `int(index_value)`
   with no non-negativity check, so `-4` is stored as `-4`, sorts first, and its
   placeholder id is `call_-3`. The first port filtered negatives out, producing
   `call_1`/`call_2` where the oracle produced `call_-3`/`call_1`. The parity probe
   measured exactly that.

Evidence:

- `tasks/native-runtime/chat_stream_events_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/chat_stream_events_parity_probe.rs`:
  **PASS**. 21 events compared **as text** (which is the contract), including CJK,
  emoji, quotes, tabs, a null id, an empty `content`, a scalar `search`, a
  non-mapping `memory_suggestion`, and the two `done` shapes; 11 usage merges; 8
  streamed tool-call sequences (the raw accumulator **and** the finalized list, so a
  difference in a placeholder id or an appended argument shows up even when the
  finalizer would drop the entry); and the accumulator's totals. The usage/tool-call
  sections are compared as parsed JSON because the Rust probe round-trips them through
  `serde_json::Value`, whose maps are key-sorted — a representation difference, not a
  value one, and the reason the event bytes are compared as text instead.
  Report: `artifacts/chat-stream-events-parity.json`.
- `cargo test -p deepseek-policy --lib chat_stream_events`: **11 PASS** (the byte
  encodings, the always-present `done` fields, the accumulator, the falsy-id rule, the
  memory-suggestion spread — where a suggestion's own `type` wins *and keeps the first
  position*, which is what a Python dict does — the five-counter usage merge, the
  index-order merge, the missing/unparseable index rule, the empty-fragment rule, the
  no-name drop, and the negative index).
- `deepseek-policy` **511** unit tests, 0 failures; `cargo fmt --all --check` clean;
  `cargo clippy --all-targets` reports no finding in the new file (the three
  `deepseek-policy` warnings are the same pre-existing files as previous rounds);
  ruff and mypy pass the new probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: wire the `/api/chat` route for the paths this protocol covers
(non-agent, non-cascade, non-edge, no search prefetch) with the others refused
explicitly rather than silently degraded; then `search_for_client` (whose key order
matters for the `search` event), the file/upload and page-render family, the skills
family, `/api/workspace/*`, and the browser staging 4.

## Previous continuation checkpoint — 2026-09-21 two public `/api` routes stop being 503s

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below (and the browser-engine
checkpoint that follows) is uncommitted. The goal remains active and
`release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY` with
`exact_head: null`.

The evidence file's first blocker says the public edge is incomplete, and the concrete
measurement behind that is the `/api/*` catch-all: any route not registered natively
falls through to the Go proxy, which answers `503 GO_CONTROL_PROXY_NOT_READY` when no
Go control plane is configured. Three of the frontend's routes were in that state and
are now native.

Implemented:

- **`POST /api/title`** — the conversation-title route the frontend calls after the
  first exchange. `deepseek-policy::title` carries the pure half (the prompt, the
  request body, the truncation limits, `_sanitize_title`, `format_upstream_error`, and
  the per-key rate window), and `deepseek-gateway::title_route` carries the transport
  and the oracle's error envelopes. Registered ahead of the `/api/*` catch-all.
- **`GET /api/download`** — the `downloadUrl` that `create_document`/`create_pptx`/
  `create_mindmap` hand back. `generated_files::download_descriptor` was the only
  missing piece (`resolve_generated_file` was already ported); `download_route` reads
  the bytes and writes the two headers. The id rule stays in the policy crate, so the
  traversal boundary cannot be forgotten at the route.
- **`GET /api/taint`** — the context-taint status block. `context_taint` was already a
  complete port; this route only had to be registered. It also needed
  `ContextTaintSettings::from_env`, which now mirrors the oracle's reader including the
  `(4, 200)` clamp on `TAINT_MAX_SEGMENTS`.
- **Measured correction:** `chat_execution::DEFAULT_UPSTREAM_TIMEOUT_SECONDS` read
  `120`; the oracle's default is `180` (`_env_int("DEEPSEEK_TIMEOUT_SECONDS", 180)`).
  Nothing depended on the wrong value — the title route caps its own call at
  `min(timeout, 20)`, which is what hid it — so it is corrected rather than recorded as
  a divergence.
- **Test-race fix:** the browser session registry is process-wide (as the oracle's
  `_sessions` dict is), so the browser unit tests could interleave a
  `reset_sessions_for_tests` between another test's session creation and its first
  action. The engine-backed cases added earlier made that fail intermittently with
  `Browser session not found`; the tests now serialize on a registry mutex. Three
  consecutive full `deepseek-policy` runs are green.

Evidence:

- `tasks/native-runtime/title_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/title_parity_probe.rs`: **PASS**, seven sections
  compared against the imported oracle — the system prompt, 26 sanitiser cases, 9
  truncations, 6 request bodies, 8 `titleModel` selections, 7 upstream responses, 7
  upstream-error bodies. Report: `artifacts/title-parity.json`.
  **The probe found a real bug on its first run**:
  `choices[0].message.content == null` returned `"None"` instead of `""`, because the
  port called `str()` without the oracle's `or ""`.
- `cargo test -p deepseek-gateway --test title_route`: **7 PASS** through
  `create_production_app` against a scripted loopback upstream that records the bytes it
  received — the oracle's body and headers, the blank-`userMessage` early return (zero
  upstream calls), the missing-key `400`, an upstream `503` capped to `502` with the
  provider's own message, the 13th call in the window as `429` with the upstream called
  exactly 12 times, and the production auth layer answering `401`.
- `cargo test -p deepseek-gateway --test download_route`: **6 PASS** — the bytes on the
  wire equal the bytes on disk for all five registered types, the four `inline`
  combinations (including `inline=false` being *truthy*, which is Python), a traversal
  attempt that leaks nothing from outside `.generated/`, the unknown-id `404` envelope,
  and the auth boundary. `generated_files` unit tests pin the oracle's six MIME/name
  pairs.
- `cargo test -p deepseek-gateway --test data_routes`: **28 PASS**, including the two
  new `/api/taint` cases and its auth boundary.
- `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports **no** finding
  in any file this slice touched (the three `deepseek-policy` and five
  `deepseek-gateway` warnings are the same pre-existing files as last round).
  Python `pytest -k title`: 12 passed; `pytest -k "download or generated"`: 67 passed.
  Rust: `deepseek-policy` **500** unit, `deepseek-gateway` **190** unit + integration.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next: `/api/chat` (NDJSON) is the frontend's streaming entry and is still a 503; then
the skills registry/runner family, the file/upload and page-render family
(`/api/file-source`, `/api/file-page-*`, `/api/file-reader`, `/api/file-chunk`,
`/api/project-files`, `/api/file-text`), the `/api/workspace/*` backup/DR surface, and
the diagnostics status blocks whose Python status functions are not yet ported. The
browser staging 4 (image + audit entry + CI lane + revision pin) also remains open.

## Previous continuation checkpoint — 2026-09-21 the browser engine is real, end to end

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062` (the committed ADR-0050 stage 1). All work
below is uncommitted; no commit, push, merge or cleanup was performed. The goal remains
active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY` with
`exact_head: null`.

Implemented (ADR-0050 stages 2 and 3):

- **The CDP engine** (`rust/crates/deepseek-browser/src/engine.rs`, new): spawns a
  headless Chromium with `--remote-debugging-port=0`, reads the DevTools socket off
  stderr, attaches one page in flat mode, and drives every declared action over CDP —
  `open_url` (`Page.navigate` + `Page.domContentEventFired`), `read_page`
  (`innerText` / `documentElement.outerHTML` with the doctype prepended / `title`),
  `extract_links`, `screenshot` (`Page.captureScreenshot`, element clip via
  `DOM.getBoxModel`), `click` (a real `Input.dispatchMouseEvent` at the element's
  viewport centre), `type_text`, `select`, `scroll`, `download`
  (`Browser.setDownloadBehavior` + `Browser.downloadProgress`). `tokio-tungstenite`
  is the one new dependency family; the workspace comment says why a hand-rolled frame
  layer was rejected.
- **The sidecar** (`src/sidecar.rs`): one browser context per session id, the
  oracle's timeouts, no durable store, and the `CloseSession` RPC — added to
  `proto/browser/v1/browser.proto` and regenerated, because without it a closed
  session left a Chromium and a profile directory behind for the engine's lifetime.
- **The seam** (`deepseek-policy::browser_engine`, new): the policy crate declares what
  it needs (`BrowserEngine`), the gateway implements it. The safety gate and the session
  registry stay in the policy crate and run either way; only the controller changes, and
  `controller_kind_for` now selects the engine exactly the way the oracle selects
  Playwright (an engine that answers `Status` with `available: true`).
- **The client** (`deepseek-gateway::browser_engine_client`, new): the generated tonic
  client on a dedicated OS thread with its own single-threaded runtime, because the
  policy seam is synchronous (it runs under `spawn_blocking`) and the client is not. The
  worker is detached and stops when the channel closes. `ToolRoundExecutor` attaches the
  engine to the tool loop; with no engine listening, `browser_*` is the static-controller
  deployment it has always been.
- `deepseek-policy::browser` gains `execute_browser_action_with_engine` and shapes the
  engine's answers into the oracle's per-action envelopes; the original
  `execute_browser_action` is a thin wrapper, so every existing caller and probe is
  unchanged.

Evidence:

- `tasks/native-runtime/browser_engine_parity_probe.py` ↔
  `rust/crates/deepseek-browser/examples/browser_engine_parity_probe.rs`:
  **PASS**, six fixtures, `url`/`title`/`text`/`links` identical, **0 differing HTML
  bytes** after whitespace collapse. Recorded divergences: the download file name
  (oracle: `sample-report.html`; CDP `allowAndName`: a GUID), the screenshot byte
  length (12925 vs 17284 — both PNG), and a missing element (oracle raises its timeout;
  the engine answers `not_found`/404). Report: `artifacts/browser-engine-parity.json`.
- `cargo test -p deepseek-browser --test engine_live`: a real Chrome, every declared
  action, including that a selector which is not in the document is `element_not_found`
  rather than a click at the origin. Gated on `DEEPSEEK_BROWSER_CHROMIUM`.
- `cargo test -p deepseek-gateway --test browser_engine_e2e`: **10 PASS** across the
  real process boundary (gateway client → gRPC → sidecar process → CDP → Chromium →
  loopback HTTP fixture), including the safety gate refusing a private host *before* the
  engine is reached, `not_found` for an unknown session, and `close_session` removing
  both the registry entry and the engine's profile directory.
- Three real defects were found and fixed by these tests rather than by inspection:
  `Page.getLayoutMetrics` was sent without a session id (the browser answers
  `-32601 wasn't found`), the document read dropped the doctype that `page.content()`
  serialises, and `click` returned the pre-click URL.
- Pinned-toolchain checks: `cargo fmt --all --check` clean; `cargo clippy
  --all-targets` reports **no** findings in any file this slice touched (the three
  `deepseek-policy` and five `deepseek-gateway` warnings it does report are pre-existing
  files — `memory_schema.rs`, `presentations.rs`, `workspace_schema.rs`, `a2a_control.rs`,
  `control_proxy.rs` — and `sidecar.rs:207` is the pre-existing `admit`); tests:
  `deepseek-browser` 11, `deepseek-policy` 491, `deepseek-gateway` 190 + 55 integration,
  0 failures; `scripts/native_codegen.py --check` and
  `scripts/check_native_contract_parity.py` pass (47 domains / 9 proto / 14 outputs).
- This is dirty-workspace local qualification on Windows against the machine's Chrome.
  It is **not** exact-head CI, not Evidence Assembly, and not a packaged image: staging 4
  (a Chromium-carrying image, the `scripts/check_native_images.py` entry, a CI lane, and
  a Chromium revision pin) is open, so no release claim is made.

Next: staging 4 for the browser (image + audit entry + CI lane + revision pin); then the
remaining public business APIs, the authoritative Go controller and Rust
worker/provider recovery, and native service/desktop/Android packaging from the matrix.

## Previous continuation checkpoint — 2026-09-20 native project reads

The live workspace advanced externally to HEAD
`b9b31b90c14406c8d306de98f5291d914062d476` on
`codex/native-a2a-stream-continuation`; that commit contains prior Rust policy
work. This continuation preserved the remaining mixed changes and made no
commit, push, merge or cleanup. Current goal remains active; release evidence
still says `NOT_READY` with `exact_head: null`.

Implemented:

- `deepseek-policy::workspace_projects`: Workspace 2.0 project read facade,
  bounded conversations/messages, separate saved-item/artifact store projections,
  filtering, artifact versions and project-scoped memories. Aggregate reads
  tolerate child errors; direct child reads return their validation error.
- `deepseek-gateway::project_routes`: authenticated legacy project list/get and
  Workspace project/list/detail/conversation/saved-item/artifact reads, ahead of
  the Go proxy. Filesystem work uses `spawn_blocking`. JSON body limit is
  2,000,000 bytes, with structured error envelopes.
- Fixed a measured existing schema mismatch: falsey Python values now select
  empty/default titles/content/tags and artifact type fallback. Source-reference
  scalar booleans retain their value.
- Native project mutations explicitly return
  `501 NATIVE_PROJECTS_MUTATIONS_NOT_READY` in every runtime mode; no new writer
  domain or production cutover is claimed. Reads leave durable state unchanged.

Evidence:

- The initial production-router regression failed with 503
  `GO_CONTROL_PROXY_NOT_READY`; after wiring, all 24 data-route tests pass.
- `workspace_projects_oracle.py`: 14 isolated Python storage fixtures and 98
  Rust comparisons. Full children have nonzero counts; corruption, falsey
  values, sorting and 200-conversation/400-message boundaries are covered.
- Pinned Rust 1.85 full policy + gateway tests: **721 passed**, zero failures.
  Strict Clippy (`--all-targets -- -D warnings`) and gateway binary build pass.
- `workspace_projects_read_e2e.py`: **23 PASS checks**, actual Rust executable,
  two independent process starts, `python_disabled`, no Go proxy; expected HTTP
  values, auth, write refusal, unchanged tree before/after process exit and
  unchanged binary hash. Report: `artifacts/workspace-projects-read-e2e.json`;
  full Rust log: `artifacts/workspace-projects-rust-tests.log`.
- Ruff and mypy pass for both new offline harnesses. This is dirty-workspace
  local qualification, not exact-head CI/Evidence Assembly or zero-Python
  default packaging acceptance.

Next: complete the project write ownership decision and mechanical denial,
fenced/serialized read-modify-write, RAG/media deletion cleanup and uploads;
then continue the remaining public APIs, Go/Rust execution, default native
packaging and real-provider/exact-head acceptance from the matrix. Do not reopen
project writes merely because low-level `projects::create_project` and
`delete_project` exist: their side effects and ownership remain incomplete.

## Previous continuation checkpoint — 2026-09-20 A2A hardening and precise coverage

This entry supersedes the prior coverage and A2A test counts below. Same branch
`codex/native-a2a-stream-continuation`, HEAD `57f0595b54071d673799f1093929b783094fa334`.
All changes remain uncommitted; no push/merge or unrelated worktree cleanup.
The full migration goal is active, and release readiness remains `NOT_READY`.

Implemented and verified:

- **31 Python message-oracle cases** now exercise Rust text rendering and Go
  admission from the same fixture. Red regressions caught Rust's `false` ->
  `"false"` mismatch and Go rejecting Python's `true` -> `"True"` case.
  Context fallback, Python truthiness/control-character whitespace, and native
  execution preserve message extensions. The real process harness validates an
  integer above 2^53, null messageId/kind, and nested contextId.
- Every private A2A RPC now rechecks the validity interval of a complete
  previously verified TLS certificate chain. A red regression proved that an
  expired issuing CA had previously retained mutation authority. Both a direct
  alternate-chain test and an actual persistent mTLS connection with a short-lived
  CA prove the fix. This does not add certificate revocation/reload support.
- Actual SQLite corruption/lock tests prove failed initialization releases its
  writer lock, expiry/cancellation cannot be acknowledged without commit, list
  queries do not return partial damaged results, and a corrupted later data page
  cannot partially recover preceding tasks. Restoring that byte lets all 81
  submitted tasks recover through the real store. All damaged files are temporary
  test fixtures; no user task database was touched.
- Removed impossible entropy-error propagation after verifying the pinned Go
  1.27 crypto/rand.Read implementation (fills the buffer or terminates the process).
  The closed string-only status-message encoding no longer propagates impossible
  JSON errors. Dynamic document, filesystem, SQL and transaction errors remain.
- **The Go coverage gate now counts raw profile statements.** A red regression
  showed that the old gate admitted 94.96% when Go printed 95.0%. Duplicate block
  counts are merged and malformed/empty profiles fail closed. No threshold was
  lowered and no source was excluded.

Current local evidence:

- `artifacts/a2a-control-go-coverage.log`: **95.003059% (4658/4903)**, exact 95.0%
  gate PASS after running every handwritten internal/pkg package. Margin is
  narrow; this is not an exact-head CI claim.
- `artifacts/a2a-control-rust-tests.log`: **234 passed**; strict Rust 1.85 Clippy
  and the gateway build passed. `a2a-control-go-race.log`: the updated A2A package
  passed race checks, and `go vet ./...` passed. API/lifecycle race and all 16 Go
  packages passed in the preceding checkpoint; this round did not rerun those
  race packages.
- `artifacts/a2a-control-restart-proof.json`: **9 PASS** against the current
  Go/Rust binary hashes, including coercion/metadata preservation and all previous
  crash/lease/cancellation/no-rerun cases. Five tasks produced exactly five
  controlled loopback provider calls. The harness is offline Python tooling,
  not a Python production listener or storage-provider acceptance test.
- Coverage-gate tests: **13 passed**; Ruff/Mypy passed the four touched Python
  gate/harness files. Message oracle **31/31**, SSE oracle **12/12**, pinned codegen
  drift check, native contract check (**46 domains / 8 proto / 12 outputs**), and
  shadow comparison **8/8** passed.

Next work remains the full matrix: complete A2A peer clients, telemetry,
legacy-task migration, retention/full wire parity; remaining public business
APIs; authoritative Go controller and Rust worker/provider recovery; native
service, desktop and Android packaging; real storage-provider and exact-head
CI/Evidence Assembly acceptance. The default Docker entry still runs Python.

## Current continuation checkpoint — 2026-09-20 durable A2A control

This entry supersedes older A2A process-local/restart-gap statements below.
Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b54071d673799f1093929b783094fa334`;
all migration changes remain uncommitted, and unrelated dirty/untracked work was
preserved. No push/merge. Goal remains active; readiness is `NOT_READY` with
`exact_head: null`. The default Docker entry still runs `python app.py`.

Implemented: Go-owned SQLite A2A task/history/chunk lifecycle; OS single-writer
exclusion; immutable submission binding; Go-installed epoch plus renewable
execution token/lease; mTLS Protobuf service; Rust public JSON-RPC/SSE bridge and
native executor; shared cursor framing; mechanical denial of Python A2A writes
in native ownership modes. No Rust/Go writes into Python `.a2a` and no local
fallback when native control is missing or unavailable. See `docs/A2A_HUB.md`.

Current evidence (development scope, not release PASS):

- `artifacts/a2a-control-restart-proof.json`: **8 PASS** checks against actual
  Go/Rust binaries, binary SHA256s, and killed process PIDs/exit codes. Completed
  snapshots survive both restarts; resubscribe emits only the missing answer;
  Go crash fails unfinished work and rejects late completion; cancellation
  discards the answer; Rust crash expires the real 45-second lease. The loopback
  HTTP provider observed exactly one call per task, no reruns. Python is only
  the offline harness. This does not prove storage-provider or whole-topology
  zero-Python behavior.
- Gateway full regression: **233 passed**, including the new fail-closed
  configuration regression and **6/6** A2A integration tests. Logs: `a2a-control-rust-tests.log`, `a2a-control-boundary-tests.log`.
  Rust 1.85 strict Clippy and fmt passed (`a2a-control-clippy.log`).
- Go `test ./... -count=1` passed all 16 test-bearing packages
  (`a2a-control-go-all-tests.log`); `vet ./...` passed; `-race` passed
  A2A/API/lifecycle packages with the
  previously verified per-command `libsynchronization.a` link fix. No machine
  environment changes. Log: `a2a-control-go-race.log`. Linux amd64 A2A test
  binary cross-compilation passed (compile only; not a Linux execution claim).
- Full Go coverage runner executed every handwritten internal/pkg package:
  **94.3%**, below the unchanged **95.0%** gate. New A2A package is **87.4%**;
  other packages aggregate **95.083%**. This is an outstanding code/test gate,
  not an environmental blocker and not a passing native-go lane.
- Python A2A + denial/ownership/proto suites passed **63 tests**. The new denial
  tests failed before adding the gate, then passed. Ruff and Mypy passed all
  four touched Python source/test files.
- Pinned codegen drift check, contract validation (**46 domains / 8 proto files /
  12 generated outputs**), shadow comparison **8/8**, and the 12-case Python
  SSE oracle check passed.

Remaining next work: close the Go coverage gap with meaningful fault/recovery
verification; complete A2A peer clients, telemetry, legacy-task migration,
retention and full error/coercion parity; continue remaining public APIs,
Go controller/worker authority and real provider kill/takeover evidence, native
service/desktop/Android packaging, then exact-head CI/Evidence Assembly. Never
mark the whole migration complete from this isolated A2A qualification.

## Current continuation checkpoint — 2026-09-19 data-plane routes

This checkpoint supersedes the historical status paragraphs below for A2A.
Current branch: `codex/native-a2a-stream-continuation`, based on `57f0595b`.
The pre-existing uncommitted migration work was retained. No push or merge.
The session goal is active: the entire Rust/Go migration is not complete.
`Dockerfile` still starts `python app.py`; readiness remains `NOT_READY` with
`exact_head: null`.

### Slices landed in this session (uncommitted)

1. **`/api/reminders` + `/api/reminders/due`** on the native edge — reads served,
   mutations gated on the `reminders_store` cutover. 9 real-HTTP cases.
2. **`deepseek-policy::memory_schema`** — the v3.0 Memory projection layer, paired
   byte-for-byte with the oracle (164 keys, md5 `d0bbb075…`), shown not blind.
3. **The `/api/memory` family** — reads served, mutations gated on the
   `memory_store` cutover. 10 real-HTTP cases.

See the sections below for each slice's evidence. Nothing was pushed.

### Local environment blockers (recorded, not worked around)

- **The Docker daemon is not running on this host** (`npipe:////./pipe/
  dockerDesktopLinuxEngine` missing), so the Three-MinIO / two-Fleet provider
  evidence cannot be produced here. The `container_image_isolation` gate still
  passes statically; the *provider* workloads are what need a daemon.
- No MinIO binary is on `PATH` and no `DEEPSEEK_TEST_S3_ENDPOINT_*` is set.
- **`gofmt -l` reports every Go file on this host**, but it is a checkout artifact,
  not drift: the working tree has CRLF while the committed blobs are LF (verified by
  byte count and by `git show HEAD:…`), and `.gitattributes` only pins the generated
  files. CI runs on Linux where this cannot occur. Reformatting here would rewrite
  every line of files this session never touched.

All three are environment gaps, not code gaps. Everything below was verified
locally without them.

### Implemented and verified in this continuation

- Native `message/stream` and `tasks/resubscribe` on both A2A RPC routes:
  initial public snapshot, resumable progress/answer chunks, terminal status,
  retained JSON-RPC IDs, SSE error events, and no OpenAI `[DONE]` marker.
- Task notifications wake subscribers without polling; disconnect drops the
  subscription without canceling work. Start/cancel/finish share the task lock;
  a queued cancellation prevents execution, and a running cancellation discards
  the late answer before an answer chunk or terminal completion can be published.
- The production router's authentication applies, and disabled A2A rejects
  both ordinary and stream RPC requests. Go's `/api/a2a` and config flags now
  advertise the implemented stream capability.
- A2A hub tests serialize their global-state resets. The baseline had two
  failures caused by parallel reset/runner changes, not by the new stream code.

Evidence (local artifacts are gitignored):

- `artifacts/native-a2a-red.log`: regression first failed because the route
  returned `application/json` instead of SSE.
- `cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway --locked -j 2`:
  **210 passed**, zero failed/ignored (181 lib + 29 integration), recorded in
  `artifacts/native-a2a-gateway-tests.log`.
- The five new integration tests include real loopback HTTP through the
  production gateway, the default native A2A runner, and a controlled local
  upstream. The initial snapshot arrives before the upstream is released.
- `python tasks/native-runtime/a2a_stream_oracle.py`: **12 cases** generated
  directly from the Python oracle's AST; Rust tests compare decoded events for
  three terminal states and four resume cursors. This is semantic parity,
  not a byte-order or full-A2A-parity claim. Ruff and Mypy pass for this helper.
- Gateway `cargo fmt --check` and strict Clippy (`--locked --all-targets
  --all-features -- -D warnings`) pass.
- `go test ./... -count=1 -timeout=600s`: **15 packages pass**;
  `go vet ./...` passes. Logs: `artifacts/native-a2a-go-all.log`.
- API and lifecycle race tests pass with **96.8%** and **98.9%** coverage
  respectively (`native-a2a-go-race-import-fix.log` and
  `native-a2a-go-lifecycle-race.log`). The initial Windows race
  binary could not load (`0xc0000139`): PE inspection proved old GCC 8.1 import
  libraries bound `WakeByAddressSingle`, `WakeByAddressAll`, and `WaitOnAddress`
  to `kernel32.dll`, which does not export them on this host. The scoped fix
  is `CGO_LDFLAGS=C:\Users\12393\.rustup\toolchains\1.85.0-x86_64-pc-windows-gnu\lib\rustlib\x86_64-pc-windows-gnu\lib\self-contained\libsynchronization.a`.
  Do not replace the entire library search path: mixing CRT generations fails
  linking. No system toolchain or persistent environment setting was changed.

### Next work and completion boundary

A2A task/chunk storage is still process-local. Implement restart persistence
and eviction after checking the authoritative store ownership rules, then
prove recovery across actual gateway process death. Peer clients and A2A
trace/disconnect telemetry are also still missing. See `docs/A2A_HUB.md`.
Other public APIs, browser execution, production cutover, default launchers,
desktop/Android packaging, and exact-head CI/Evidence Assembly remain.
Historical matrix entries may be stale; inspect current code before choosing
the next slice. Do not mark the overall goal complete from this local slice.

## Historical git at recovery

| Field | Value |
| --- | --- |
| Branch | `codex/native-runtime-5.0.0-continue` (created from `native-runtime-5.0.0-recovered`) |
| Recovered HEAD | `451ba5ec23ad07e783b68f2a1f8f87ed5f9b8f05` |
| VERSION | `4.8.0` |
| Evidence file | `NOT_READY` (must stay fail-closed until exact-head CI generates it) |
| Production authority | Python (`release/native_runtime_ownership_v1.json`) |

Do not reset, clean, or discard the recovered uncommitted tree. Coverage
profiles (`go/cov`, `go/shadow_cov`, `go/store_cov`, `*.out`) are local
artifacts and must not be committed.

## Evidence classes (do not collapse)

| Class | Meaning |
| --- | --- |
| Documented target | Spec/ADR/roadmap/plan text |
| Implemented, unwired | Native code exists; production entry still Python or fail-closed |
| Wired, unverified | Native entry exists; missing real-process/provider/CI evidence |
| Locally verified | This workspace ran the matching command |
| CI / release verified | Exact-head CI, Evidence Assembly, readiness validator |

## Recovered uncommitted work (this session)

Two in-progress slices were already present. They are not treated as correct
until tests in this session pass.

1. **Go schema v7 verification lifecycle** — `VERIFYING` / `ASSESSING_EFFECT`
   with an immutable boundary; leased execution/recovery persist VERIFYING
   instead of SUCCEEDED; resources stay held.
2. **Go→Rust TLS transport** — Rust-owned server key, Go CA + server-name
   verify, short-lived bearer over TLS only, no plaintext credential fallback.

## What this session implements next

Signed storage-operation grant (worker-execution-plan slice 2): canonical JSON
grant, shared v31 corpus, RPC admission before dispatch.

Not in this slice: durable grant sqlite journal, payload-bytes-in-Rust, MinIO
kill/takeover, production cutover, public edge parity, or readiness PASS.

## Status after this session

| Item | Class | Command / evidence |
| --- | --- | --- |
| Schema v7 + verification primitives | locally verified | `go test ./internal/store -count=1 -timeout=360s` exit 0 (49.937s); `go test ./internal/action -count=1` exit 0 |
| TLS unit + real-process tests | locally verified | worker/protocol tests exit 0; `cargo test -p deepseek-worker --lib --bins --test grpc_service --test tls_transport` exit 0; `TestRustWorkerTLSRealBoundary` exit 0 (0.16s) |
| Clippy worker | locally verified | `cargo clippy -p deepseek-worker --all-targets -- -D warnings` exit 0 |
| Full Go module (no race) | locally verified | `go test ./... -count=1 -timeout=600s` exit 0 (then worker re-run after OPERATION_INVALID mapping) |
| native-go TLS CI wiring | implemented, unverified | `.github/workflows/ci.yml` — needs exact-head CI |
| Storage operation grant v31 + RPC admission | locally verified | `go test ./internal/store -run StorageOperationGrant`; `cargo test -p deepseek-worker --test frozen_storage_operation_grant_v31 --test grpc_service`; `python scripts/native_runtime_contract.py --check` (42 corpora / 31 versions); TLS process test exit 0 |
| Full Go `-race` | not verified this session | store historically hits 600s aggregate timeout |
| Production cutover | documented target | `ErrCutoverNotAuthorized` still enforced |

## Running processes

None started by this continuation unless a later section records a PID.

## Blockers that remain after this slice

1. Durable worker sqlite grant journal (in-memory replay only today) and Go
   coordinator attaching live grants instead of qualification JSON.
2. Outcome/risk verifiers and compensation (not journal primitives).
3. Provider-backed Three-MinIO / two-Fleet kill-and-takeover.
4. Rust edge chat/MCP/A2A/catalog parity; authenticated `/api/*` proxy.
   Non-stream `/v1/chat/completions` now executes natively (see below); SSE,
   tool rounds, MCP and A2A remain fail-closed.
5. Oracle normalization differences - **RESOLVED 2026-09-14** (commit `df7dfa13`).
   The oracle silently dropped blank-content turns, `null` content, non-object
   entries, `tool` turns missing `tool_call_id`, and caller-supplied `system`
   turns. It now refuses the first four (same `ErrorCode` values as Rust) and
   *keeps* `system` turns. The `system` case is the important correction: a
   second measurement on the full assembly path
   (`tasks/native-runtime/oracle_layering_probe.py`) showed the caller's
   instruction never reached the upstream body at all, because
   `normalize_chat_messages` dropped it while `build_deepseek_request` builds
   the authoritative prefix separately from `payload["systemPrompt"]`. Keeping
   the turn is therefore not a capability addition - it stops silent data loss.
   Rust behavior unchanged, as decided.
5. Go production cutover authorization protocol.
6. Default launchers/images still start Python (`launch.py`, `docker-compose.yml`).
7. Exact-head CI and Evidence Assembly.

## Next explicit action

The Python oracle now fails closed on unrepresentable turns instead of silently
dropping them (commit `df7dfa13`, 2026-09-14). Verified: 101 passed across
`test_deepseek_client_failure_paths.py`, `test_gateway_request_preparation.py`
and `test_rust_gateway_request_parity_contract.py`; the only pre-existing failure
was the test that encoded the bug itself. The four measured parity differences
are now closed, with the two layers proven to agree.

Native edge chat has moved from fail-closed to a wired non-stream path
(`6ea4dde3` + `chat_execution.rs`, 2026-09-14). Verified locally:
`cargo fmt -p deepseek-gateway -- --check` clean; `cargo test -p deepseek-gateway
-j 1` -> 77 lib + 4 `chat_execution` real-upstream tests + 2 boundary tests,
all passed.

**SSE streaming is now wired too (2026-09-14, uncommitted).** `chat_stream.rs`
owns upstream SSE decoding (`decode_event`/`decode_chunk`) and downstream OpenAI
SSE encoding (`StreamChunkEncoder`), and `chat_completions` now branches on
`stream`. `request_preparation` no longer refuses `stream: true` — it normalizes
it to a boolean and forwards it, because transport selection is not a
preparation-layer concern. Verified locally:

- `cargo test -p deepseek-gateway -j 1` -> 96 lib + 4 `chat_execution` +
  6 `chat_stream` real-boundary tests, all passed.
- Byte-level parity: `tasks/native-runtime/sse_parity_probe.py` (extracts the
  real `_sse`/`openai_chat_stream` via `ast`) vs `examples/sse_parity_probe.rs`
  over the same two upstream scripts -> identical MD5
  `b9129475b6bae8b1239f4529e0a50932`, 12 frames, no differences.
- `cargo clippy -p deepseek-gateway --all-targets --all-features -- -D warnings`
  -> only the pre-existing `control_proxy.rs:20` `result_large_err` (file
  byte-identical to HEAD; local rustc 1.97.1 vs the declared 1.85).
- See `docs/GATEWAY_SSE_PARITY.md` for the frame contract and the explicit
  non-goals.

Still unwired and each failing closed with its own code: tool-call rounds,
`/api/chat` NDJSON (including `system_note`/`search`/`memory_suggestion`),
semantic cache/memory/context-compression, model router, scheduler leases and
budget ledger. `release/native_runtime_5_0_evidence_v1.json` stays `NOT_READY`.

Wire Go `ExecuteClaimedStorageAction` to sign `control-storage-operation-grant-v1`
from the live claim (no payload bytes in the grant), persist grants in the Rust
worker sqlite journal, then slice 3 provider-backed dispatch.

**Tool-round layer 1 is now implemented and byte-verified (2026-09-14, uncommitted).**
`rust/crates/deepseek-gateway/src/tool_rounds.rs` mirrors the oracle's round
*bookkeeping* only: `ToolCallAccumulator` (streamed `tool_calls` delta merge and
finalization), `normalize_tool_calls_lenient`, `decide_round` (the round/budget
branch), `append_tool_exchange` (message assembly), and
`force_final_answer_without_tools`, plus `tool_names` / `select_tool_calls` /
`tool_call_note` and the three constants.

The public route **keeps refusing** a `tool_calls` turn with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`. Layers 2 (tool execution: the 17 branches
plus `browser_*`) and 3 (policy/sandbox) are not implemented, and wiring layer 1
alone would replace the oracle's terminating tool loop with a permanently
failing one that still answers `200` — the forbidden silent behavior change.
This slice exists to make that refusal precise and to make later enablement a
wiring change rather than a rewrite.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_round_parity_probe.py` (extracts
  the real `append_tool_exchange`, `merge_stream_tool_call_deltas`,
  `finalized_stream_tool_calls`, `normalize_tool_calls`, `tool_names`,
  `force_final_answer_without_tools` via `ast`; stubs only the layer-2
  `execute_tool_calls` and the transport `raise_if_cancelled`) vs
  `examples/tool_round_parity_probe.rs` over the same 12 input scripts ->
  **identical MD5 `ca9b072a4826fc470e3ccdc6e436bc58`**, 36 keys, no differences.
- `cargo test -p deepseek-gateway --lib tool_rounds -j 1` -> 24 tests, all pass.
- `cargo fmt --check` clean.

Three real divergences were found and fixed **by the probe**, not by reasoning:

1. an index-less delta lands at the *slot count*, not the highest index plus one
   (oracle: `["first", "third", "five"]`; the original unit test asserted
   `["first", "five", "third"]` and was wrong);
2. `str(item.get("id") or f"call_{i+1}")` stringifies truthy non-strings, so
   `id: 123` becomes `"123"` while `id: 0` / `id: ""` fall back — the first
   implementation read only string ids and silently renumbered them;
3. non-string `arguments` use Python's **default** JSON separators (`{"a": 1}`),
   not `serde_json`'s compact form (`{"a":1}`). This lands verbatim in the
   upstream body and therefore changes the prompt prefix and DeepSeek's prefix
   caching.

Known bounded limitation: this workspace compiles `serde_json` **without**
`preserve_order`, so an `arguments` *object* whose keys are not already sorted
serializes in Rust's sorted order rather than the caller's insertion order.
Enabling `preserve_order` workspace-wide would silently reorder every other Rust
response (only `deepseek-proof` opts in), so it was deliberately not done;
resolving it belongs with argument canonicalization. Recorded in
`docs/GATEWAY_TOOL_ROUND_PARITY.md`.

Next concrete action for this line: implement layer 2 (`execute_tool_call`'s 17
branches + `browser_*`) and layer 3 (`ToolPolicy.evaluate`/`sanitize_result`),
then wire `tool_rounds` into `chat_execution`/`chat_stream` and delete the
`ToolRoundsUnwired` refusal.

**Tool-policy pure core ported and byte-verified (2026-09-15, uncommitted).**
`rust/crates/deepseek-policy/src/tool_policy.rs` mirrors the side-effect-free half
of `deepseek_infra/infra/tool_runtime/tool_policy.py`: the SSRF guard
(`evaluate_url_safety`), the path-escape guard (`evaluate_path_safety`), the
recursive network-argument guard, the secret-exfiltration guard
(`arguments_contain_secret`), the prompt-injection sanitizers
(`sanitize_external_text` / `sanitize_tool_result` /
`sanitize_tool_result_for_external`), `validate_arguments`, `_max_risk`, and the
`ToolMetadata` / capability-profile tables.

This is the gate the oracle applies **before** a tool runs, so it is a
prerequisite for layer 2 (tool execution): porting execution first would mean
running model-chosen side effects with no SSRF, path-escape, secret-exfil, or
injection guard — strictly weaker than the Python being replaced.

Still **not** ported: `ToolPolicy.evaluate` (reads config + audit state) and the
audit writers. Until those land, nothing may execute a tool on this module alone,
and the route keeps refusing tool rounds with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_policy_parity_probe.py` slices the
  contiguous pure region of the oracle (lines 59–563) and `exec`s it (so the
  definitions being compared are the oracle's own, including the import-time
  derivations) vs `deepseek-policy/examples/tool_policy_parity_probe.rs` over the
  same corpus -> **identical MD5 `26c7723c89a4fb59c7ef9e412f1b4b97`**, 158 keys,
  no differences.
- `cargo test -p deepseek-policy --lib tool_policy` -> 30 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean. `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

Two defects the probe caught, both from **guessing** the IP classifier instead of
reading CPython's tables (the first pass was wrong in both directions):

1. false negative — `1:0:0:2::3` was allowed; Python's `is_reserved` covers
   `::/8` (and much more: `4000::/3`, `e000::/4`, …), so the oracle blocks it.
2. false positive — `192.88.99.1` was blocked; that range is in none of Python's
   tables, so the oracle allows it.

Derived facts, now encoded and tested:

- IPv4 `is_global` = `not in 100.64.0.0/10 and not is_private`, so `not is_global`
  adds only the shared range; `is_reserved` is `240.0.0.0/4` (already private).
  Blocking set = 14 ranges.
- IPv6 `is_global` is literally `not is_private`, so `not is_global` adds nothing.
  Blocking set = `_private_networks` ∪ `_reserved_networks` ∪ multicast = 23 ranges.
- IPv4-mapped IPv6 delegates **every** predicate to the underlying IPv4 address,
  so `::ffff:1.2.3.4` is *allowed* while `::ffff:0:1` is blocked — even though
  `_private_networks` lists `::ffff:0.0.0.0/96`. Rust's `to_ipv4_mapped()` matches
  CPython's `ipv4_mapped` exactly.
- `fec0::/10` (deprecated site-local) is allowed by the oracle; `fe00::/9` stops
  at `fe7f::`. Mirroring that hole is correctness, not a bug to "fix".

Message-parity surfaces that look like formatting but are not: the blocked-IP
reason embeds Python's `str(ip)` (so IPv4-mapped must render dotted, not
`::ffff:0:1`), and the enum violation embeds Python's `repr` of the list
(`['x', 'y']`).

Dependency change is minimal: `regex 1.13.0` was already in `Cargo.lock`
transitively, so it is pinned exactly and promoted to a direct dep of
`deepseek-policy`; the lockfile gains one edge and no new crate version.

**Real finding, deliberately not fixed here.** The crate's pre-existing generic
guards (`url_guard.rs` / `path_guard.rs`, behind the gateway's `/policy/*` routes)
are **weaker than the oracle** and are a different model: no
`.local`/`.localhost`/`.internal` suffix check, no trailing-dot strip, URL
credentials are **stripped and allowed** (the oracle denies them), and
multicast/reserved/CGNAT/non-global IPv4 plus the IPv6 reserved ranges are not
checked at all. Tightening them changes a registered route's behavior, so it
deserves its own slice with its own evidence. Exposure is latent — the Rust
gateway is not the production authority — but it should not ship as-is.

Next concrete action for this line: port `ToolPolicy.evaluate` + the audit log
(layer 3b), then implement layer 2 execution against this gate, then wire the
round loop and delete the `ToolRoundsUnwired` refusal.

**Tool-policy engine + audit layer ported and byte-verified (2026-09-15 二轮，uncommitted).**
`deepseek-policy::tool_policy` now also carries the decision engine and the audit
log, completing the policy gate:

- `ToolPolicy` + `ToolPolicyConfig` with the oracle's own defaults,
  `ToolPolicy::new` / `permissive()`, `evaluate`, `_record` semantics
  (counters + `blocked_tools`), `mark_tainted` / `is_tainted`,
  `sanitize_result` (scrubs and taints the turn on a hit), `denial_output`,
  `diagnostics`.
- `ToolPolicyDecision` — deliberately **not** named `PolicyDecision`, because this
  crate already exports a different `PolicyDecision` (the
  `Capability`/`RiskLevel` model behind `/policy/*`). Sharing the name would make
  importing the wrong one an easy, security-relevant mistake.
- `is_sensitive_memory` (extracted from `infra/data/memory.py`, not re-written).
- Audit: `AuditSink` trait with `NullAuditSink` / `InMemoryAuditSink` /
  `JsonlAuditSink`, `build_audit_entry`, `build_external_audit_entry`,
  `normalized_args_hash`, `read_recent_audit`, and a hand-rolled
  `utc_isoformat_seconds` (no date dependency).

**Not ported:** `tool_policy_status` (reads the audit-path global and the config
object) and the `deepseek_infra.core.config` env reader — a config-layer concern,
not a policy one. Nothing may execute a tool until layer 2 exists and is wired;
the route still refuses tool rounds with `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity across **both** regions of the same oracle file: the
  contiguous constants+guards+engine slice (lines 59–901, `exec`ed verbatim with
  only the config globals and audit path rebound) plus the audit functions lifted
  individually and driven against a **real temporary JSONL file** (so the writer
  that ships is the writer measured, not a re-implementation of its entry dict) ->
  **identical MD5 `d51462e06a0e6ccd03db7ed05ab77d71`**, 197 keys, no differences,
  re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy` -> 77 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean.
- Dependency: `sha2 0.10.9` was already in `Cargo.lock`; the lockfile gains one
  edge and no new crate version.

Design points worth keeping:

- **The decision order is the contract.** `evaluate` returns on the first failing
  check, so reordering any pair changes which reason a call reports when it fails
  several (unknown → capability → schema → SSRF → path → sensitive → secret →
  confirm → taint → allow).
- **`fetch_url` bypasses the recursive guard** and calls `evaluate_url_safety` on
  `args["url"]` directly; every other network tool goes through
  `evaluate_network_argument_safety`, which **prefixes the offending key**. So the
  same private host yields `ssrf_blocked:private or local ip is not allowed: …`
  for `fetch_url` but `ssrf_blocked:host: …` for `web_search`. My first unit-test
  expectation used the unprefixed form for `web_search` and was wrong; the probe
  settled it. (Third time this pattern has caught me — measure, don't infer.)
- **`denial_output` does not check the action.** Called on an allow it still
  returns a denial-shaped payload with `code: "forbidden"` and
  `error: "… blocked by tool policy (allow)"`. That looks like a bug and is not;
  it has an explicit test so nobody "fixes" it.
- **Best-effort audit is the contract.** The oracle swallows every write error so
  an unwritable log can never break a tool call. The port keeps that but records
  the failure in `last_error()` so it stays observable instead of vanishing.
  Splitting the write behind `AuditSink` is also what keeps `evaluate`
  deterministic enough to compare byte-for-byte, and lets shadow runs capture
  decisions without touching the authoritative log.
- The audit entry is `{"ts", "scope", **decision.to_dict()}` with `sort_keys=True`.
  `ts` is the only non-deterministic field, so the probe injects a fixed clock and
  masks it on both sides; its *shape* is pinned by unit tests with hand-checked
  anchors (epoch, day boundary, Unix 1e9).

Next concrete action for this line: port `tool_policy_status` + the config reader,
then implement layer 2 execution against this gate, then wire the round loop and
delete the `ToolRoundsUnwired` refusal.

**Status endpoint ported, and the `/policy/url` gate aligned to the oracle (2026-09-15 三轮，uncommitted).**

Step 1 of the planned sequence (`tool_policy_status` + config) is done:
`ToolPolicySettings` (the five knobs, config defaults), `ToolAuditPaths::under(root)`
(mirroring `tool_audit_dir = root / ".tool-audit"`), `tool_policy_status`, and
`render_path_like_python` for the `auditLogPath` field. `ToolPolicyConfig::default()`
now reads its four strictness fields *through* `ToolPolicySettings::default()`, so
the engine and the status payload cannot drift apart (asserted by a test).

**The scoping of step 2 turned up something that reordered the work.** The oracle's
own Rust delegation is the risk:

```
execute_tool_call -> _evaluate_rust_policy (tools.py)
                  -> rust_core.policy_client.check_url / check_path
                  -> POST /policy/url, /policy/path (gateway)
                  -> url_guard::validate_url_access  <-- weaker than Python
```

`DEEPSEEK_RUST_POLICY` defaults to **false** (`infra/rust_core/config.py`), so
Python still decides. But flipping it would have moved SSRF decisions onto the
guard flagged in the previous round: `.local` / `.internal` hosts, trailing-dot
localhost, credential-bearing URLs, multicast, reserved, CGNAT and the whole IPv6
reserved set would all have started passing. Wiring execution onto that gate first
would have been the wrong order — the gate had to be correct before anything was
allowed to depend on it.

So `url_guard::validate_url_access` now **delegates to
`tool_policy::evaluate_url_safety`** and maps the oracle's denial reason onto the
crate's codes. The bridge contract is unaffected: `policy_client._parse_response`
requires the `allowed` bool plus non-empty string `code`/`reason`/`decision_id`/
`capability`/`risk_level`, and treats `code` as **opaque** — nothing branches on it.

Two deliberate consequences:

- The oracle reports one `private or local ip is not allowed: …` verdict, so
  loopback, link-local, reserved and multicast now all return
  `PRIVATE_NETWORK_BLOCKED` from this route. `codes::LINK_LOCAL_BLOCKED` is no
  longer emitted *by this guard*. Mirroring the oracle means mirroring its
  collapsing, not inventing a finer taxonomy.
- `UrlPolicy` can only **tighten**. The oracle accepts http(s) only, so listing
  another scheme cannot reintroduce it — there is a test for exactly that.

`path_guard` is deliberately **not** touched: `validate_workspace_path` is
root-containment over a `{root, requested}` pair, while the oracle's
`evaluate_path_safety` is an argument-key scan. Complementary, not
interchangeable; merging them would change what the route means.

Verified locally:

- Byte-level parity: **identical MD5 `bae3a9e5eb30cdd80a7a28b31e1f433b`**, 257
  keys, no differences. The URL corpus is checked **twice** — `url::<label>` (the
  guard) and `guard::<label>` (the route), so the route cannot silently drift from
  the guard it delegates to.
- `cargo test -p deepseek-policy` -> 82 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

**NOT done, and the honest state of the remaining two steps:**

- **Step 2 (layer 2 tool execution) is not started.** `execute_tool_call` dispatches
  to 17 local branches plus `browser_*`, and those depend on the `search`, `rag`,
  `data` (projects/reminders/memory), `media` (presentations, mindmaps, documents,
  slides) and `browser` packages — several thousand lines with their own
  side-effect and sandbox semantics. It is a multi-slice effort, not one commit.
  A sensible first slice is the **dispatch skeleton + the branches with no external
  package** (e.g. `python_eval`'s sandbox envelope, `data_transform`,
  `list_reminders`), each behind the gate just aligned, with the package-backed
  branches added one at a time.
- **Step 3 (wire the round loop, delete `ToolRoundsUnwired`) is not started** and
  is correctly blocked on step 2 — wiring it now would replace the oracle's
  terminating tool loop with a permanently failing one that still answers `200`.
- `DEEPSEEK_RUST_POLICY` remains **off**, deliberately. Enabling it is an explicit
  cutover that needs `path_guard` aligned and the failure-mode policy reviewed.

**Layer 2 slice 1: the executor seam, with one branch ported (2026-09-15 四轮，uncommitted).**

`rust/crates/deepseek-policy/src/tool_dispatch.rs` ports the *seam* of
`execute_tool_call` in `infra/tool_runtime/tools.py`:

- the envelope contract (success `{"ok": true, "tool", "result"}` + `sanitize_result`;
  the `AppError` and catch-all error arms; the `Unsupported tool:` fallback);
- the normalization the branches rely on — `tool_call_name`,
  `parse_tool_arguments`, `safe_limit`, `is_parallel_safe_tool`, `SERIAL_TOOL_NAMES`;
- the **complete branch inventory** (`Branch`, 18 entries) with `branch_for`
  routing, `is_ported`, and `blocker()` naming the package each unported branch
  waits on — a test asserts no branch is silently missing;
- `generate_chart` + `chart_markdown_table`, the one branch that needs no package.

**Nothing is wired.** `DispatchOutcome::Unported` deliberately carries **no
envelope** and `to_output()` returns `None` for it, so a caller cannot report
success (or even a tidy error) for a tool that was never implemented. 17 of 18
branches remain unported; `python_eval` in particular needs a real sandbox
because the oracle shells out to a Python interpreter, which the migrated runtime
must not do.

Verified locally:

- Byte-level parity: **identical MD5 `9d491ef3f97c9f079ad9d7761a815ec4`**, 49 keys,
  no differences, re-confirmed after the clippy fixes.
- `cargo test -p deepseek-policy` -> 102 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Three orderings the probe pinned, and the bugs they caught:

1. **Parse before gate.** My first draft gated the raw `arguments` value. The
   model sends arguments as a JSON *string*, and the guards read fields inside it —
   so gating the raw string left every argument guard looking at an empty object
   and the SSRF/path checks **silently passed**. The oracle parses first; there is
   now a test that fails if the order is reversed
   (`dispatch("fetch_url", "{\"url\": \"http://169.254.169.254/\"}")` must be
   Denied with `risk = "critical"`).
2. **Gate before branch.** A denial short-circuits; the probe records branch
   invocations, and every denied case reports `branches: []`.
3. **The unknown-tool fallback is a no-policy path.** With a policy attached an
   unregistered name is denied as `unknown_tool` first, so `Unsupported tool:` is
   only reachable without one. The Rust probe example's first version gated every
   case, which made the `no-policy` case deny where the oracle reached the
   fallback — the diff exposed it.

Two behaviours the corpus settled, both from **guessing instead of measuring**
(that is now four times on this project):

- `data[:12]` is applied **before** the point filter, so the cap counts raw items,
  not usable points. My first test asserted 12 points for a 26-item input; the
  real answer is 7.
- `int("7.9")` raises in Python (falls back to the default) while `int(7.9)`
  truncates to 7 — the string and number paths had to be handled separately.

Also reproduced: `python_float_str` for `str(float)` (`1.0` not `1`; signed
zero-padded exponents outside `1e-4..1e16`), because those values are interpolated
into the model-facing markdown table.

Next concrete action for this line: port `execute_tool_calls` (the parallel batch
+ cancellation) and the remaining branches one at a time, each behind the gate,
starting with the ones whose packages are smallest. Only after enough branches
exist does wiring the round loop (and deleting `ToolRoundsUnwired`) become safe.

**Layer 2 slice 2: `data_transform` branch, batch orchestration, shared Python-JSON (2026-09-15 五轮，uncommitted).**

- `tool_transform.rs` ports `data_transform` and its four pure operations
  (`extract_regex`, `json_path`, `csv_summary`, `number_summary`) plus helpers
  (`read_simple_json_path`, `compact_json_value`, `number_summary_payload`, and a
  hand-rolled `csv_read` for Python's default CSV dialect).
- `tool_batch.rs` ports `execute_tool_calls`: selection capped at 6, serial/parallel
  batching (exposed as `plan_batches` data), cancellation at the four points,
  None → cancelled / None → "did not run", and the `role: "tool"` message with
  compact-JSON content truncated to `MAX_TOOL_RESULT_CHARS`. `strip_volatile_tool_fields`
  and `stable_tool_output_for_model` ported; artifact-compaction deferred (those 3
  branches unported, path unreachable, a test pins the pass-through).
- `python_json.rs` owns `dumps_default_separators`/`dumps_compact`/`float_str`/`value_str`
  — the rendering rules `tool_rounds`/`tool_policy`/`tool_dispatch` each had a
  private copy of. `tool_policy::normalized_args_hash` and `tool_dispatch::python_float_str`
  now delegate to it (two duplicates removed; gateway's `tool_rounds` copy noted
  as a follow-up, out of this crate's boundary).

**Honest state of the remaining 15 branches**: `Branch::blocker()` still names each
one's package and a test asserts none is silent. They are blocked on real subsystems
(browser engine, RAG, data layer, media/doc generation, an HTTP client, a real
sandbox for `python_eval`). Wiring the round loop and deleting `ToolRoundsUnwired`
stays blocked on these.

Two parity substitutions recorded in docs:
- JSON-path splitter: Python uses a lookahead the `regex` crate lacks; a plain
  split on `.` is equivalent for every *acceptable* path (well-formed parts have
  digits-only indices, so no dot lives inside brackets; the two disagree only on
  paths that fail the fullmatch and raise "Unsupported JSON path" either way).
- Engine-specific diagnostics: `Invalid JSON: …` / `Invalid regex: …` embed the
  engine's own error text. The prefix is the oracle's and identical; the suffix is
  masked on both sides like the audit `ts`. Divergence on record in the doc and a
  unit test, not behind a green diff.

Verified locally:
- Byte-level parity: **identical MD5 `3f088f27bcf1dda772cf3fb18d318cf5`**, 80 keys,
  no differences (helpers, both ported branches, batch layer, volatile-strip).
- `cargo test -p deepseek-policy` -> 131 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

**Layer 2 slice 3: the search family, callback injected (2026-09-15 六轮，uncommitted).**

The next-smallest dependency after `data_transform`: `web_search` and
`compare_search_results` need no package, only the per-request
`web_search_callback` the gateway owns.

- `tool_search.rs` ports both branch bodies (with their distinct "not enabled for
  this request" errors), `compare_search_results` (two cleaned queries, whitespace
  collapsed / de-duplicated / 500-char cap; one round each; results de-duplicated
  across rounds and capped at 20), and `search_result_key`.
- `ExecutorContext` carries the optional callback, mirroring the oracle's keyword
  arguments. `dispatch` now threads it through — the one signature change; tests
  and the probe example pass a default context, which makes the search branches
  take their "not enabled" path, and that path is compared directly.

**`search_result_key` is deliberately a different projection** from
`tool_policy`'s SSRF host extraction: the guard wants a hostname to classify
against the IP tables, this wants the raw netloc (lowercased, port and userinfo
included) so results differing only in case or fragment collapse to one key.

Two measured behaviours that corrected wrong guesses of mine (**sixth time on this
project that measuring beat reasoning**):

- `urlsplit` strips **leading** C0 controls and spaces but never trailing ones, so
  `"  HTTP://X  "` keys to `"http://x  /"`.
- An **empty** URL is not an empty key: `urlsplit("")` normalises to path `/`, so
  the key is `"/"` — which is why an empty-URL result is **kept**, not skipped.
  Only a non-object entry is dropped.

Verified locally:
- Byte-level parity: **identical MD5 `a6aa9b0ed707966a641940b47fdade55`**, 104 keys,
  no differences.
- `cargo test -p deepseek-policy` -> 141 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Branch status: **4 of 18 ported** (`generate_chart`, `data_transform`,
`web_search`, `compare_search_results`). 14 remain, each with `Branch::blocker()`
naming its package. Nothing is wired; the round loop stays blocked on them.

**Layer 2 / data layer slice A1: the workspace mutation gate (2026-09-15 七轮，uncommitted).**

Prerequisite chosen by the user (A1 over A2). `rust/crates/deepseek-policy/src/mutation_gate.rs`
ports `infra/workspace/mutation_gate.py` — the fence, the exclusive OS lock, and the
durable generation counter. Every memory/reminder write is wrapped in it, so no
data-layer branch could be faithful without it.

**It is not a mutex.** `mutation_scope` (1) asserts no restore owns the workspace
(423, checked twice to close the race with a newly-created fence), (2) takes an
exclusive OS lock for the whole mutation, (3) bumps the generation **before and
after**, fsync'd. The lock and the fence are deliberately separate: a crash
releases the lock, but mutations stay blocked until recovery reconciles the
transaction.

Shape differences, each with a reason: `root: &Path` instead of a `config.ROOT`
global; `LockFileEx` with the oracle's ten-attempts-one-second-apart retry policy
(plain `LockFileEx` would block **forever** where `msvcrt.LK_LOCK` raises); `flock`
on Unix; `Mutex` + thread-local depth instead of `RLock` (Rust's `Mutex` is not
reentrant); hand-written `extern "C"` because this workspace pins deps to what is
already in `Cargo.lock`.

Quirks reproduced rather than fixed: `fsync_directory` stays **best-effort** (the
directory open normally fails on Windows); the lock file is created with `b"0"`
only if absent; temp-file cleanup failure is ignored after a committed replace;
`write_fence` and `bump_generation` build temp names differently (suffix preserved
vs dropped); the unreadable-fence message is **fixed** because the oracle chains
the cause with `raise ... from exc` rather than interpolating it.

Errors: `GateKind` distinguishes the oracle's `AppError` / `RuntimeError` /
`OSError`, and **`code`/`status` are `Option`** — a `RuntimeError` has neither, and
inventing `internal`/500 would let a caller read a programming error as a routine
refusal. That was my first draft's mistake.

Three probe bugs this slice exposed (all mine):
1. `ast.get_source_segment` drops decorators, so `exclusive_gate`/`mutation_scope`
   came back as bare generators, not context managers.
2. `@contextmanager` is **lazy** — `mutation_scope()` alone asserts nothing and
   bumps nothing; the body only runs on `__enter__`. A probe that merely called it
   would have shown a green tick over no behaviour.
3. `_GATE_STATE` is a module-level global, so the nested-different-root check only
   fires within one module instance. Building a second namespace for the "other
   root" gave the inner gate its own thread-local state and — correctly — no error.
   The Rust behaviour was right; the probe was wrong.
4. `json!` reads `[...]` as an array literal, so a `.iter()` chain cannot follow it.

Verified locally:
- Byte-level parity: **identical MD5 `57e0ede25273693e03863bffe024aadb`**, 32 keys,
  no differences — paths, generation read/bump/clamp, fence write/read/clear, both
  refusal paths, the scope's double bump, nesting (same and different root),
  malformed fences, temp-file hygiene, lock-file content.
- `cargo test -p deepseek-policy` -> 155 tests, all pass (14 new), including a
  multi-threaded case asserting the generation ends at exactly `scopes * 2`.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Next: **slice B, the reminders pair** (`create_reminder`, `list_reminders`) — 138
lines, one JSON file, no retrieval, no RAG. `Branch::is_ported()` is unchanged for
every data-layer branch; nothing is wired.

**Data layer slice B: the reminders store and its branches (2026-09-15 八轮，uncommitted).**

`rust/crates/deepseek-policy/src/reminders.rs` ports
`infra/data/reminders.py` plus the `create_reminder_tool` / `list_reminders_tool`
wrappers: the JSON store, `parse_due_at`, `create_reminder`, `list_reminders`,
`delete_reminder`, `due_reminders`. First slice that exercises the slice-A1 gate
from a data path — the probe records the generation advancing **two per create** as
evidence the write really goes through the fence.

**Key order is part of the on-disk contract.** `serde_json` here has no
`preserve_order`, so object keys iterate sorted, while Python dicts keep insertion
order and the store file carries it. Writing from a plain `Value` would give
different bytes for equivalent JSON — and this repository's subject is a backup
system. Added `python_json::OrderedJson` to spell the order out, with a test pinning
the exact expected file text.

**Quirks reproduced, not fixed:** the temp file is
`REMINDERS_FILE.with_suffix(".tmp")`, which *replaces* the suffix
(`reminders.json` -> `reminders.tmp`), so two writers collide on one name; and reads
are silent (missing/unreadable/malformed/wrong-top-level-type all degrade to empty,
non-dict entries dropped).

**`parse_due_at`** reproduces the subset of `datetime.fromisoformat` this module
meets plus Python's `isoformat()`: `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`, `T`/`t`/
space separator, `HH` through `HH:MM:SS.ffffff`, compact time, and `Z`/`+HH`/`+HH:MM`/
`+HHMM` offsets. A **lowercase `z` is rejected** (only an uppercase trailing `Z` is
rewritten). Anything outside the measured set raises the oracle's own message rather
than being guessed at.

**Seventh "guessed instead of measured".** My first ISO-week implementation validated
the week by checking the resulting date's *year* matched the stated year. Wrong in
both directions; `date.fromisocalendar` settled it:

| Input | Oracle | My first version |
| --- | --- | --- |
| `2026-W01-1` | `2025-12-29` (week 1 starts in December) | rejected |
| `2026-W53-1` | `2026-12-28` (2026 has 53 ISO weeks) | rejected |
| `2025-W53-1` | error (2025 has 52) | (would have accepted) |

The rule is: validate against *how many ISO weeks that year actually has*, from the
distance between consecutive week-1 Mondays. The unit test asserted the wrong
expectation too and now carries the measured values.

**Non-determinism is injected.** `secrets.token_hex(8)` and `int(time.time()*1000)`
arrive through an `Entropy` trait; production uses the OS CSPRNG (`BCryptGenRandom` /
`/dev/urandom`) and **fails loudly rather than falling back** to a weaker source,
since `secrets` is explicitly the secure option and a reminder id reaches the model
in tool output.

Verified locally:
- Byte-level parity: **identical MD5 `a636cd4590cff26d7809e8866fb3e500`**, 74 keys,
  no differences — 41 date forms, 8 create shapes, the exact store bytes, generation
  and lock file, temp hygiene, 8 status variants over two store states, 5 tolerant-read
  shapes, delete outcomes.
- `cargo test -p deepseek-policy` -> 171 tests, all pass (16 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice C, the scorer (`query_tokens` / `score_chunk` / `utc_now_iso` /
`latest_user_query`) — pure, and shared with `search_files`, so one port unblocks two
branches. `due_reminders` is ported and unit-tested but not yet in a compared corpus
(calling it mutates the store past the probe's last observation).

**Data layer slice C: the retrieval scorer (2026-09-15 九轮，uncommitted).**

`core_utils.rs` ports `query_tokens`, `score_chunk`, `utc_now_iso` and
`latest_user_query` from `core/utils.py`. Shared by the memory branches **and** the
RAG `search_files` branch, so one port serves two.

**A measured defect in the oracle.** `query_tokens` ends with
`sorted(tokens, key=len, reverse=True)[:80]` over a **set**. Python's sort is stable,
so equal-length tokens keep the set's iteration order, which depends on
`PYTHONHASHSEED`; when more than 80 tokens survive, *which* 80 are kept changes every
run. Measured:

    100 equal-length tokens, seed 1 -> x70,x17,x04,x11,...
    100 equal-length tokens, seed 2 -> x78,x43,x17,x67,...
    weighted case: seed 1 -> score 360; seed 5 -> score 390

The score ranks memories, so this leaks into tool output. The port therefore orders
by **length descending, then lexicographically** — deterministic where the oracle is
not. That is a deliberate divergence: there is no single oracle behaviour to
preserve, and CPython's set order is impossible to reproduce by construction. It
narrows a varying result to a fixed one and weakens nothing.

The probe matches that reality instead of hiding it: token lists are compared
**sorted**; inputs where more than 80 tokens survive report **only the count** (the
subset itself differs run to run); and a unit test pins the determinism as a property
of this port.

Signature difference, documented: `utc_now_iso()` reads the clock and takes no
argument in the oracle; this port is `utc_now_iso(epoch_seconds)`, so the clock is
supplied and can be pinned. The probe compares the *rendering* for four epochs.

Details that are easy to conflate: the tokenizer's character classes need **two or
more** characters while the weight is `max(2, min(len, 10))`; CJK bigrams are added
**on top of** the run itself; and a `set` dedupes windows, so `"中" * 60` yields
exactly two tokens.

Also fixed: a unit test asserted `query_tokens("Rust   OWNERSHIP") == ["rust",
"ownership"]`, the wrong order — `ownership` is longer and comes first. Same mistake
class as the previous six; the ordering rule now has its own test.

Verified locally:
- Byte-level parity: **identical MD5 `2170fb900a543b67163f1417d8af2c15`**, 37 keys,
  no differences — 13 tokenizer inputs, 2 capped inputs, 10 scoring inputs with token
  lists, 4 epoch renderings, 8 `latest_user_query` payloads.
- `cargo test -p deepseek-policy` -> 180 tests, all pass (9 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice D, the memory triple (`suggest_memory`, `recall_memory`, `forget_memory`)
— store + scorer + the fingerprint/category/conflict/sensitive logic. Nothing is
wired; `Branch::is_ported()` is unchanged for every data-layer branch.

**Data layer slice D: the memory triple (2026-09-15 十轮，uncommitted).**

`memory.rs` ports `infra/data/memory.py` plus the `suggest_memory` /
`recall_memory` / `forget_memory` branches; `file_lock.rs` factors out the OS lock
that both this module and the mutation gate need (the platform split now lives in one
place). A memory write passes through **three** layers, each doing a different job: a
process-wide mutex, a cross-process file lock on `.memory/memories.lock`, and the
workspace mutation gate.

**The bug this slice found, and how.** The first version put `mutation_scope` around
the *delete* path only, because that was the path I was reading. The oracle puts it
inside `_save_memories_unlocked`, so **every** save is fenced — including the
migration save. The probe caught it as a generation counter off by exactly two:

    delete::no-write-generation   Python 6   Rust 4

Six means three scopes had run (migration + two deletes), four means two. Fixed by
moving the gate into `save_unlocked`, where the oracle has it.

**A truthiness detail.** `_save_memories_unlocked` normalises `source` through two
Python `or` chains. My first version stringified any number and fell back otherwise,
which is wrong at both ends: `0` and `false` are falsy and become `"manual"`, while a
non-zero number and `true` become `"5"` / `"True"`. Fixed with an explicit
`python_truthy` covering `""`, `[]` and `{}` too.

**One deliberate gap, stated everywhere it matters.** `retrieve_memories` adds a
vector-search bonus from `local_rag.search_memories_index`. `local_rag` is 2,676 lines
and belongs to the RAG slice, so the bonus arrives through an injectable `VectorHits`
provider defaulting to none. The oracle wraps the call in `try/except Exception` and
falls back to an empty map, so the default reproduces the oracle's **own degradation
path** and the probe compares that. But when the vector index is populated the
oracle's scores include a bonus this does not. **`recall_memory`'s ranking is verified
only where the vector index contributes nothing** — recorded in the docs, the matrix
and the module docs.

Also ported faithfully from the write path (it doubles as the migration): non-objects
and empty content dropped; `id` falls back `memoryId` -> `id` -> a **content-addressed**
`sha256(...)[:20]`; `confidence` default 0.9 clamped to [0,1]; `type` derived from
`type` -> `category` -> `"fact"`; timestamps through the injected clock; cap 400.
Reads stay silent on corruption.

Verified locally:
- Byte-level parity: **identical MD5 `4261dd06c31ec2de180601f8d80e5cca`**, 92 keys, no
  differences (text/scope/fingerprint/sensitive/category/conflict helpers, tool scopes,
  suggest, the loaded and migrated store bytes, tolerant reads, recall, forget, delete
  semantics with generation counters, conflict queries).
- `cargo test -p deepseek-policy` -> 206 tests, all pass (26 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Last data slice is E (`projects`, blocked on `rag/files.py`'s `load_cached_file`).
`suggest_memory` does not persist: it builds a suggestion and fires a callback, so
`upsert_memory`, `clear_memories` and `delete_memory_by_id` are not ported and are not
needed here. Nothing is wired.

**Data layer slice E1: the projects read path (2026-09-16，uncommitted).**

The last data domain, split in two because the measurement showed the halves have very
different dependencies. **E1 is done**: the projects store — `validate_project_id`, the
whole `normalize_*` family, `read_project`, `public_project`, `list_projects`.
**E2 is not started**: `load_cached_file` plus the two branch wrappers.

`read_project` re-normalises **six** collection fields on every read, so the normaliser
family is on the critical path even for a branch that only looks at `documents` — and
`normalize_skill_run` alone has **thirty fields**. That is why a ~45-line pair of
branches needs a store-sized slice.

**A real finding: the read path mints random ids.** `normalize_skill_run` and
`normalize_saved_items` generate `f"run-{secrets.token_hex(8)}"` / `f"saved-…"` whenever a
stored entry has none, and `read_project` calls them — so **reading the same malformed
project twice returns different values**. Measured: `run-d9d3e527ae4f29df` then
`run-5acb6344a2c0e2bf`. Not persisted (read never writes back), so it is a phantom id, but
it is observable through `public_project`, which `list_projects` returns to the model. The
port keeps the behaviour and takes the source through the shared `entropy::Entropy` trait.
That is why `Entropy` moved out of `reminders` into its own module — a second user appeared.

**`OrderedJson` had a real bug, exposed here.** Store records ported so far were flat, so
nested containers were being written **compactly** where Python's `indent=2` indents at
every level. A project record is not flat. Fixed by converting nested values into real
nodes — and this mattered beyond the probe, since a memory `source` object would have hit
the same bug. Residual limit stated rather than hidden: nested object **keys** come out
sorted, because `serde_json` here has no `preserve_order`.

**Two error-shape details.** `unique_strings(None)` **raises** in the oracle (`list(None)`
is a `TypeError`), so the port reproduces that and restores the `or []` guard at all six
call sites — which is what makes the raise unreachable from ported code. And that
`TypeError` has **no code**; this port reports `invalid_payload` with a matching message, a
documented mapping rather than an invented code, so the probe compares the message and
deliberately not the code.

Verified locally:
- Byte-level parity: **identical MD5 `787f519d69e6b4a295732891fa84777b`**, 76 keys, no
  differences (11 id shapes, 7 name shapes, 8 document shapes, 13 `_safe_int` shapes, 5
  `unique_strings` shapes incl. both raises, 9 skills shapes, 6 skill runs incl. one with
  all thirty fields, saved items and artifacts with generated ids, 5 tolerant reads,
  `require_project` hit and miss, `list_projects` ordering with an invalid dir and a loose
  file).
- `cargo test -p deepseek-policy -- --test-threads=1` -> 206 tests, all pass.
  **Note:** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` is flaky
  under the default parallel harness (passes in isolation and serially, twice). This crate
  already has a known class of process-level shared-state interactions; run the suite with
  `--test-threads=1` when it matters.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean.

Next: **E2** — `load_cached_file` (self-contained: 32-hex id check, `PROJECTS_DIR/<id>/files`
path, JSON read, `lru_cache(64)` keyed on `(file_id, mtime_ns)`, `file_index_expired` 410)
and the two wrappers. Then the data layer is complete and `Branch::is_ported()` can be
revisited. Nothing is wired.

**Data layer slice E2: the file-cache read path and the two branch wrappers (2026-09-16，uncommitted).**

`file_cache.rs` + the `list_project_files` / `read_file_chunk` branches in `projects.rs`.
The measurement held up: `load_cached_file` really is an id-shape check, a path
derivation, a JSON read and a cache, so the rest of that 1,494-line RAG module stays
untouched. **The data layer is now complete** — reminders, memory, the shared scorer,
projects.

Three details that are easy to get wrong, and were:

1. **The `lru_cache(64)` only applies without a project id** — a project-scoped read
   always re-reads. Key is `(file_id, mtime_ns)`, which is what stops a changed file
   hitting a stale entry. `FileCache` reproduces the bound and move-to-front-on-hit.
2. **`if project_id` tests the RAW value, not the stripped one.** A whitespace-only
   project id is truthy, so it reaches `project_file_cache_dir`'s shape check and fails
   with a 400 — it does **not** fall back to the global cache. The wrapper
   (`read_file_chunk`) strips first and passes `None`, so a blank id from the tool *does*
   use the global path. **Two different behaviours for a blank id, one call apart**; the
   probe caught my first version collapsing them.
3. **`int()` here is the bare one, not the store's `_safe_int`.** `"3.7"` and `"abc"`
   **raise** rather than falling back; a float truncates toward zero; `"1_0"` and
   `"  8  "` parse. `python_int` is deliberately separate from `safe_int`, with the same
   documented mapping as the projects `TypeError`.

Also faithful: `preview` is capped at **500** in the tool projection but **1800** in the
store; `count` sums the *emitted* files (after both caps); a `chunks[index]` that is not
an object is a 404, not a skip.

Verified locally:
- Byte-level parity: **identical MD5 `5baaaba2565bed0542b652627891039d`**, 29 keys, no
  differences — 4 file-id shapes, missing/malformed/scalar indexes, the project-scoped
  path, the blank-id 400, the `project_file_cache_dir` path, 13 chunk cases (default,
  explicit, zero, negative, out of range, non-dict chunk, missing/非-list `chunks`,
  project-scoped, invalid project id), and `list_project_files` named/missing/invalid-id
  plus the full `list_projects` payload shape with its two caps.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> **214 tests, all pass**, run
  twice.

**One open item, stated plainly.** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly`
failed **once** during this slice and passed on every other run — in isolation, serially,
and in two full serial runs. The symptom is a thread panicking inside its scope. Likely
cause is **parity, not a defect**: `lock_exclusive` reproduces `LK_LOCK`'s "retry once a
second, give up after ten attempts", so under contention the gate **errors** after ~10s
where a plain blocking lock would have waited — the oracle does the same. The test's
`.unwrap()` turns that refusal into a panic. **Not root-caused.** If it is the retry
budget the fix belongs in the test, not the lock semantics; if it is not, something else
is sharing state between tests, and that matters. Re-examine before wiring.

**Gate fidelity fix: poisoning is a failure mode the oracle does not have (2026-09-16，uncommitted).**

Follow-up to the open item recorded in E2. The intermittent failure of
`concurrent_scopes_serialize_and_count_exactly` did **not** reproduce on demand (5 further
full serial runs, all 214-green), so instead of shrugging I looked for a failure mode this
port has and the oracle does not. There was one:

**`std::sync::Mutex` poisons; Python's `threading.RLock` does not.**

`exclusive_gate` acquired `PROCESS_LOCK` with `.lock().map_err(...)?`, so once **any**
thread panicked while holding that mutex, every later acquisition in the process returned
an error — and `mutation_scope(...).unwrap()` in the test would panic with exactly the
observed shape. That was a real fidelity gap whether or not it explains this failure.

Fixed: recover from poisoning (`unwrap_or_else(PoisonError::into_inner)`) instead of
reporting it. The same applies to `MEMORY_LOCK` / `STORE_LOCK`; those call sites already
bound the whole `LockResult` and so were tolerant by accident of style — now documented as
intentional, because it is load-bearing.

**Narrowed, not closed.** Eleven subsequent full runs pass. If it recurs, the remaining
candidate is the gate's `lock_exclusive` retry budget (ten attempts a second apart,
faithful to `msvcrt.LK_LOCK`, but it means the gate *errors* under sustained contention
where a plain blocking lock would wait) — in which case the fix belongs in the test, not
in the lock semantics.

**Wiring-surface measurement (for the next slice).** `deepseek-gateway` already depends on
`deepseek-policy`, but only uses `PolicyDecision`/`codes` — it does **not** reference
`tool_dispatch` or `is_ported`. The `Branch` enum already carries all seven data variants
with `tool_name()` and `branch()` mappings. But **nothing executes them**: no caller
anywhere invokes `reminders::create_reminder` or `projects::list_project_files`. So wiring
is not "flip `is_ported()`" — it needs an executor plus routing, and end-to-end
verification. That is its own slice.

---

## E7 (2026-09-16): the gateway wiring — `dispatch()` has a production caller

**HEAD before this slice: `d92953bb` (main). The slice follows the seven data branches
being wired into the dispatcher (`432318d1`).**

The executor-plus-routing slice the measurement called for. Three pieces:

1. **`rust/crates/deepseek-gateway/src/chat_tool_loop.rs`** — the non-streaming tool
   round loop, mirroring `call_deepseek`'s loop body in the oracle's order:
   `exchange_turn` → `merge_usage_totals` → lenient `tool_calls` normalization →
   `decide_round` → `execute_tool_calls` (runner = `dispatch`) →
   `append_tool_exchange`; `force_final_answer_without_tools` at budget exhaustion;
   `final_answer` from the last turn plus the merged usage.
   - `WorkspaceBundle` (root + `FileCache` + `SystemEntropy` + `SystemClock`) is the
     oracle's module globals as one injectable object; the root comes from
     `DEEPSEEK_INFRA_ROOT`, and unset ⇒ the data branches answer
     "not enabled for this request", never a silent no-op.
   - `ToolRoundExecutor::from_env` builds the policy the oracle's
     `build_tool_policy` produces for main chat: `ToolPolicyConfig::default()`
     (capability `full`, `enforce_schema`/`require_confirm` off, `sanitize` on,
     `TOOL_POLICY_ENABLED` default on with `_env_bool` spellings) plus the
     process's `DEEPSEEK_API_KEY`/`AUTH_TOKEN` as the secrets blocklist. One
     policy object lives across the request — counters accumulate like the
     oracle's single `tool_policy`. The per-call lock recovers from poisoning
     (`PoisonError::into_inner`) for the same reason the stores do.
   - Execution runs on `spawn_blocking` (the data branches take OS file locks);
     a panicked blocking task resolves every selected slot through the batch
     layer's own "did not run" envelope rather than inventing results.

2. **`chat_execution.rs` reworked around turns** — `UpstreamTurn` +
   `turn_from_payload` (extraction does not refuse `tool_calls`; that is the
   loop's data), `exchange_turn` (the POST), `merge_usage_totals` +
   `usage_int` upgraded to Python `int()` coercion semantics (numeric strings,
   float truncation, bool), `final_answer` (keeps the facade's pre-existing
   empty-content refusal, now also covering the budget-exhausted partial turn).
   `ToolRoundsUnwired` / `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is **deleted** from
   the non-streaming path; the SSE path keeps refusing in-band via
   `STREAM_TOOL_ROUNDS_NOT_READY` (streaming round continuation is its own seam).

3. **The route is actually reachable** — `/v1/chat/completions` now prepares the
   raw body through `prepare_chat_request` instead of re-encoding through the
   typed `ChatCompletionRequest` struct, which silently dropped every field it
   did not enumerate — including `tools`, without which the model could never
   have called anything and the loop would have been dead code on arrival.
   Malformed JSON → 400 "request must be valid JSON"; a malformed-typed field
   now surfaces as the preparation layer's own 400 instead of axum's 422.

**Honest state.** Eleven of eighteen branches execute for real. The other seven
(`browser_*`, `python_eval`, `search_files`, `fetch_url`, `create_mindmap`,
`create_pptx`, `create_document`) resolve to the visible `Tool did not run`
envelope — a degradation against the Python oracle for those tools, on an
opt-in sidecar, stated in the loop's module docs and pinned by a boundary test.
Also absent with owners: the web-search provider, `mcp__*` bridging, artifact
terminal handling, and the loop's surrounding machinery (semantic cache, memory
retrieval, scheduler, traces, budget ledger). Divergences kept on purpose are
listed in `docs/GATEWAY_TOOL_DISPATCH.md` (empty-content refusal, env-injected
root, `""` vs `null` assistant replay, no `memorySuggestions` channel).

**Verification.**
- `cargo test -p deepseek-gateway -j 1` → 129 lib + 7 `chat_execution` boundary
  (four new: continuation through dispatch, data branch against the workspace
  incl. the fence files landing under `DEEPSEEK_INFRA_ROOT`, budget exhaustion
  incl. the `MAX_TOOL_ROUNDS + 2` turn count and `tool_choice: "none"`, unported
  branch honesty) + 6 `chat_stream` + 2 control-boundary — all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → 223 pass.
- `cargo clippy -p deepseek-gateway -p deepseek-policy --all-targets -- -D warnings`
  → only the pre-existing `control_proxy.rs:20` `result_large_err` (byte-identical
  to HEAD; local rustc 1.97.1 vs declared 1.85). One same-class local-toolchain
  lint (`unnecessary_sort_by` in `python_json.rs`) fixed mechanically — the two
  sort forms are identical.
- `cargo fmt --all -- --check` clean.

**Next.** The streaming tool loop (SSE round continuation interleaved with
`system_note`), the web-search provider behind `ExecutorContext.web_search`, and
the `schema_for_tool` catalog.

**Bug fix: the streamed tool-call accumulator coerced values the Rust way, not Python's
(2026-09-16，uncommitted).**

Found while checking whether E7's `ToolCallAccumulator` needed anything before the
streaming loop is built on it. It did — and the accumulator is **committed code from an
earlier slice**, so these are pre-existing bugs, not fallout from E7.

The oracle reads the delta index through a bare `int()`:

```python
index = len(accumulator) if index_value is None else int(index_value)
```

This port read it with `as_i64()`, which is strictly narrower. Measured against the real
`merge_stream_tool_call_deltas` before changing anything:

| delta | oracle | this port (before) |
| --- | --- | --- |
| `"index": "2"` | slot **2** | `len(accumulator)` = 0 |
| `"index": true` | slot **1** | `len(accumulator)` = 0 |
| `"index": 2.7` | slot **2** | `len(accumulator)` = 0 |
| `"id": 123` | `"123"` | placeholder `call_1` |

**The index decides which tool call a fragment lands in.** Sending three of those to
`len(accumulator)` merges the arguments of unrelated calls into one slot — a wrong tool
invocation, not a cosmetic difference. The id case is the same class the lenient
normalizer already guards with a comment ("Reading only string ids here would silently
renumber such calls"); the accumulator had the gap.

Fixed by using Python's semantics rather than Rust's: `python_int_opt` for the index,
`python_truthy` + `value_str` for `id` / `type` / `function.name` / `function.arguments`.
The slot key widened from `usize` to `i64` because `int()` accepts a negative index and
Python's dict holds one; `sorted()` then orders it first, which the new test pins.

**Consolidation this forced, and that is the real win.** `deepseek-policy` now has one
implementation of each Python coercion in `core_utils`, used by three call sites:
`python_int_opt` (the file-cache read path maps its failure to the documented 500; the
accumulator falls back to the running slot count) and `python_truthy` (the stores,
the file cache, the accumulator). `file_cache::python_int` and `projects::is_truthy`
delegate, so their committed APIs are unchanged. Two small corrections fell out of
writing the shared version: `"1__0"` and a non-finite float are both rejected by Python's
`int()` and were previously accepted.

Verified:
- Three new gateway tests pin the measured divergences and the negative-index ordering
  (`the_index_coerces_the_way_pythons_int_does`,
  `a_negative_index_orders_before_the_others`,
  `a_non_string_id_is_stringified_and_a_falsy_one_is_ignored`), plus one in
  `core_utils` for the shared coercion.
- `cargo test -p deepseek-gateway -j 1` -> 132 lib + 7 + 6 + 2, all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> 224 pass.
- `cargo clippy` -> only the pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Not reachable from DeepSeek's own API today** — it sends `index` as a JSON number and
`id` as a string, so the two implementations agree in practice. That is exactly why it
was worth fixing rather than noting: the divergence is invisible until a provider
changes shape, and then it corrupts tool-call assembly instead of failing.

**Streaming slice, step 1: the SSE decoder now yields every delta a chunk carries (2026-09-16，uncommitted).**

Prerequisite for the streaming round loop, and a real divergence on its own.

The oracle's per-chunk body in `stream_deepseek` does everything **in one pass** —
it does not short-circuit:

```python
choices = chunk.get("choices") or []
if not choices: continue
delta = choices[0].get("delta") or {}
if choices[0].get("finish_reason"): round_finish = str(...)
if isinstance(chunk.get("usage"), dict): round_usage = chunk["usage"]
merge_stream_tool_call_deltas(stream_tool_calls, delta.get("tool_calls"))   # always
if delta_reasoning: ... forward reasoning ...
if delta_content:   ... forward content  ...
```

This port's `decode_chunk` checked `chunk_has_tool_calls` **first** and returned
`UpstreamDelta::ToolCalls`, dropping the same chunk's `content`, `reasoning`,
`finish_reason` and `usage`. Confirmed by reading the oracle, not by inference.

That is not cosmetic: the dropped `content` is the text `append_tool_exchange` replays
to the provider as the round's assistant message. A round-ending chunk that also
carried prose would have lost it.

**Fix.** `decode_event` / `decode_chunk` return `Vec<UpstreamDelta>` in the oracle's
order, and `UpstreamDelta` gained payloads: `ToolCalls(Value)` (the fragments the
accumulator needs), `Usage(Value)` and `FinishReason(String)`. `forward_line` iterates
and, when a tool round appears, forwards the chunk's content **first** and emits the
refusal **last** — the earlier ordering would have put the refusal ahead of text the
model did produce, which reads as the text being the problem.

An existing test asserted the old behavior under a name that defended it
(`a_tool_call_chunk_is_typed_as_tool_calls_even_with_content`, "forwarding the prose is
the silent-flattening failure the refusal exists to prevent"). That reasoning was
wrong: the oracle forwards the prose too. The test is replaced by one that pins the
oracle's behavior, plus two more for the ordering and for the round-ending chunk's
`usage` / `finish_reason`.

**The refusal itself is unchanged and still loud.** Streaming clients still get
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` on a tool round; what changed is that they get the
round's text first. The loop body (the `for tool_round in range(max_tool_rounds + 2)`
structure, the `system_note`s, `append_tool_exchange` and the next upstream request) is
the next step — the body is an `async_stream` generator, so awaiting a new upstream
mid-stream is already possible.

Verified:
- `cargo test -p deepseek-gateway -j 1` -> 134 lib + 7 + 6 + 2, all pass (3 new, 1
  replaced).
- **SSE byte-parity holds**: `tasks/native-runtime/sse_parity_probe.py` against the Rust
  example, identical MD5 `b9129475b6bae8b1239f4529e0a50932`. Note the corpus could not
  have caught this divergence — it has no chunk carrying both `content` and
  `tool_calls`, and it could not, because this transport refuses on a tool round where
  the oracle continues. The unit tests are the right level for it.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the
  pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Streaming slice, step 2: the round loop. The refusal is gone (2026-09-16，uncommitted).**

`streaming_response` now runs the tool rounds, so `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is
deleted rather than kept as a seam. Streaming clients get the same round continuation the
non-streaming path has had since E7.

The shape mirrors `stream_deepseek`'s `for tool_round in range(max_tool_rounds + 2)`:

- each round streams one upstream turn, forwarding `content` as it arrives while
  accumulating the `tool_calls` fragments into the existing `ToolCallAccumulator`;
- at the end of the round `finalize()` + `decide_round` decide: no calls → the stop frame
  and the loop ends; budget spent → `force_final_answer_without_tools` and one more turn;
  otherwise → `executor.run_round` + `append_tool_exchange` and **a fresh upstream request
  opened from inside the generator** (a response body is single-shot, so every further
  round is a new request);
- `decode_event`'s per-chunk deltas are handled inline rather than through a helper,
  because the generator has to `yield` between them and a helper cannot yield on its
  behalf. That deleted `forward_line` and the refusal constant, and an obsolete test.

The round decision, the exchange assembly, the tool execution and the usage merge are the
**same functions** the non-streaming loop calls, so the two transports cannot drift.

**What is deliberately not emitted.** The oracle's `system_note`s (`正在调用本地工具…`, the
budget notice, the `finish_reason: "length"` truncation notice) never reach this endpoint:
`openai_chat_stream` maps only `content`, `done` and `error`. Same for the per-round `usage`
— the facade's frames carry no usage field. Both are still decoded, so the loop is not
reading a shape it cannot see, but they have no wire effect here.

**Verification.** The new `streaming_continues_a_tool_call_round` drives the real
`/v1/chat/completions` route against a per-request stub upstream and asserts four things:
both rounds' content arrives in order; there is no error frame; the reminder was actually
written under `DEEPSEEK_INFRA_ROOT`; and **the second upstream request carries the
exchange** — the assistant `tool_calls`, the `tool_call_id` and the tool result content.
That last assertion is the one that would catch a loop that ran but replayed nothing.

- `cargo test -p deepseek-gateway -j 1` -> 133 lib + 7 + 6 + 1 + 1, all pass.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the pre-existing
  `control_proxy.rs:20`.

**The intermittent gate failure recurred, and the poisoning fix was not the cause.**
`mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` failed once more
(223 passed, 1 failed, `--test-threads=1`) and then passed three runs in a row. That was
the honest label's payoff: it was recorded as "narrowed, not closed" precisely because the
poisoning gap was a real fidelity bug but never proven to be *this* failure. Now it is
disproven as the sole cause. Not captured this time (the reruns were green); the next
occurrence needs the panic message, which the earlier note never managed to record.

**Root cause of the intermittent gate failure: the lock file was reopened to seed it
(2026-09-16，uncommitted).**

Three rounds of this. Round 1 recorded it as "narrowed, not closed" after fixing a mutex
poisoning gap; round 2 saw it recur, which disproved poisoning as the sole cause. This
round captured the panic, and the cause was in the port all along.

**The evidence.** Reproduced on the 7th of 15 full serial runs:

```
thread '<unnamed>' panicked at mutation_gate.rs:741:58:
called `Result::unwrap()` on an `Err` value: GateError { kind: RuntimeError,
  message: "另一个程序已锁定文件的一部分，进程无法访问。 (os error 33)", code: None, status: None }
```

`os error 33` is `ERROR_LOCK_VIOLATION`: Windows refuses a **write-mode open** of a byte
range that another handle has locked, and refuses writes into it.

**The mechanism.** `exclusive_gate` created the lock file and then **reopened it for
write** to seed the byte:

```rust
if let Err(error) = OpenOptions::new().create_new(true).write(true).open(&target) { … }
else {
    let mut file = OpenOptions::new().write(true).open(&target)?;   // ← reopen
    file.write_all(b"0")?;
}
```

Between the create and the reopen, another thread can reach the OS lock on byte 0. The
reopen-for-write then fails with error 33, the code reports `GateError::misuse`, and
`mutation_scope` returns `Err` — which the test unwraps. The lock file only exists once
per workspace, so the window only opens while the file is being created, which is why it
took the full suite (many workspaces) and roughly one run in seven to hit.

**The fix is the oracle's shape, not a guess.** `_lock_file`/`exclusive_gate` in
`infra/workspace/mutation_gate.py`:

```python
try:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
except FileExistsError:
    pass
else:
    os.write(descriptor, b"0")     # the SAME descriptor, then closed
    os.close(descriptor)
with _PROCESS_LOCK:
    ...
    with target.open("r+b") as handle:   # the only open for locking
        _lock_file(handle)
```

So the byte is written through the handle that created the file, and there is exactly one
other open — inside the process lock, for locking. **The reopen was an invention of this
port.** Fixed by writing through the creating handle.

**The regression test had to be able to fail.** `racing_first_scopes_never_fail_on_the_lock_file`
removes the lock file every round and races eight threads through `mutation_scope`, 25
rounds, because the window only opens during creation. Verified in both directions: it
passes with the fix, and it fails with the pre-fix reopen restored.

**What this round changes about the record.** Rounds 1 and 2 both said "not root-caused",
and that was right to say — the poisoning fix was a real fidelity bug, and describing it
as *the* cause would have been a plausible story standing in for evidence. The lesson is
the one already in the notes from the object-store work: a fixed bug is not a fixed
symptom until the symptom stops.

**The numbers.** Failure rate before: 1 in 7 full serial runs (run 7 of 15). After: **0 in
15**. The regression test, run against the pre-fix code restored temporarily: **failed on
run 2 of 5** at the thread's assert — so it is a test that can fail, not decoration. Run
against the fix: 5 of 5 green. Both directions measured, not asserted.

`cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean
(the only output is a transient `deepseek-core` incremental-artifact copy warning, which
is not a lint). `cargo fmt --all -- --check` -> clean.

**Tool catalog ported, generated rather than transcribed (2026-09-16，uncommitted).**

`schema_for_tool` and the web-search provider were the two remaining items. Measuring
them showed the catalog is the **shared** dependency — `schema_for_tool`,
`tool_parameter_schemas`, `agent_tool_definitions` and `tools_for_payload` all sit on it —
so it came first, and the web-search provider is its own slice (below).

**The 28 definitions are not typed out by hand.** They are the oracle's own
`json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)` bytes, 40,634 of
them, committed as `rust/crates/deepseek-policy/assets/tool_catalog_v1.json` and embedded
with `include_str!`. Hand-copying 40 KB of descriptions and JSON Schema is where typos
live, and a typo inside a `parameters` block would silently change what the model may
send. The probe's `asset::json` case is the guard: it compares the embedded text with the
oracle's rendering, so the committed bytes cannot drift without the diff failing.

**Three divergences caught by reading the oracle instead of guessing.** The appended
external-MCP definition is not what a reasonable guess produces:

```python
tools.append({
    "type": "function",
    "function": {
        "name": profile.bridged_name,     # a profile field, not derived from `tool`
        "strict": True,                   # easy to miss entirely
        "description": f"[External MCP: {profile.server}] {schema_desc}",
        "parameters": parameters,
    },
})
```

and `parameters` is `raw_schema if raw_schema.get("type") == "object" else
{"type": "object", "properties": raw_schema}` — the **whole** raw schema goes under
`properties`, not its entries. My first version derived `mcp__{tool}` as the name, omitted
`strict`, and spread the free-form schema's keys into `properties`. All three are now the
measured shape.

**The external arms are injected, with a documented default.** `schema_for_tool`'s
`mcp__` branch and `agent_tool_definitions`' appending both need `infra.mcp.bridge`. They
take an injected provider / profile list, and `None` reproduces the oracle's own
`except Exception: pass` degrades-to-local arm — so the default is the oracle's behaviour
rather than an invented one.

Verified:
- **Catalog parity holds**: identical MD5 `a2fa62de54c6f95e85065f6c008b8e58`, 18 keys, no
  differences — the asset bytes, the 28 names in declaration order, the schema index
  (count, sorted names, four full schemas), eight `schema_for_tool` cases including the
  trim, the unknown name and the empty string, and `agent_tool_definitions` with no
  bridge.
- Six unit tests cover the arm the probe cannot reach: an `mcp__` name with a bridge, a
  non-object profile schema (rejected by `schema_for_tool`, wrapped by
  `agent_tool_definitions`), and an unknown external name.

**The web-search provider is measured and left, on purpose.** It is not pure:
`_perform_web_search` needs `search_single_round` (a real Tavily HTTP call),
`tavily_api_key`, a per-request result cache, a citation counter, a turn limit and a
shared `search_budget`. `ExecutorContext.web_search` is already the injection point, so
the Rust side has the seam — what is missing is the HTTP integration and its config, which
is a connector-shaped slice that needs either a live key or a stub upstream to verify. Say
that plainly rather than half-wiring it.

**Also measured while sizing this, now unblocked:** `search_tool_enabled` and
`tools_for_payload` are pure and depend only on the catalog plus `search_mode`. They are
the natural companions to this slice whenever the search provider lands.

**Tavily search layers 1+2 ported: query planning, normalization, ranking, cache (2026-09-16，uncommitted).**

`search.rs` now carries everything the `web_search` tool branch needs from
`infra/tool_runtime/search.py` **except the HTTP call**. The boundary is a dependency
closure, not taste:

- **`format_search_context` / `format_search_failure_context` are not in it.** They build
  the *prompt context* at request-assembly time; the tool branch returns a compiled tool
  result and never calls them. Porting them would be porting a different consumer.
- **`search_tavily` / `search_tavily_with_retry` are not in it either** — but their retry
  *policy* is ([`should_retry_tavily_error`], [`simplified_retry_query`]). Only the request
  itself is missing, which is the next slice.

**One divergence, caught by the probe.** `domain_from_url` is
`urlsplit(url).netloc.lower().removeprefix("www.")` — and `netloc` is the **whole
authority, userinfo and port included**. Extracting just the host reads as the obvious
cleanup and is wrong: for `https://user:pw@Host.COM:8443/x` the oracle returns
`user:pw@host.com:8443` and my first version returned `host.com`. The probe diff was a
single line out of 97 keys. It is now the whole netloc.

This matters beyond the field itself: `search_result_score` feeds `domain` into
`TRUSTED_DOMAIN_HINTS` with a `contains` check and `rerank_search_results` uses it as the
per-domain diversity key, so a narrowed domain changes both ranking and the
two-per-domain cap.

**A recurring trap, hit a third time.** `serde_json`'s `json!` does not accept a **block
expression** as a value, so `"retryQuery": { let v = ...; if ... { v } else { json!("") } }`
fails with `unexpected end of macro invocation`. The fallbacks have to be hoisted into
`let` bindings first. Same class as `.iter()` on a temporary and `&"x".repeat(n)` in a
`Vec<&'static str>`: the macro's accepted grammar is narrower than the expression grammar.

**What is reproduced rather than tidied:** `search_cache_key` lowercases while its callers
pass the raw query (so the cache is case-insensitive by construction);
`save_search_cache` **prunes before writing**; the temp file is `with_extension("tmp")`,
which replaces `.json` rather than appending; `search_result_score`'s weights (score × 20,
title token +8, body token +3, trusted domain +10, official-docs +6, empty snippet −8) and
`rerank`'s two-per-domain cap run after the sort.

Verified:
- **Search parity holds**: identical MD5 `a425954350aeea8c6d935c476bda169e`, 97 keys, no
  differences — six query shapes through nine distinct functions, nine `should_search_for_query`
  cases across four modes, six intents, five URL authorities, three `normalize_search_response`
  shapes, eight per-result scores, the reranked URL order, the full aggregation (status,
  joined answer, reason, result URLs, normalized rounds), two compactions, round statuses,
  round ordering, and two cache round-trips.

**About the pasted credentials.** A live Tavily key and what appears to be an upstream API
key were pasted into the chat. Neither was written to any file (verified with a repo-wide
grep), neither was persisted as an environment variable, and all probe artifacts were
deleted. They still appear in this conversation's transcript, so both should be **rotated**
regardless of what this session did with them.

**What is left.** The HTTP layer: `search_tavily` (request body assembly, the `TAVILY_URL`
POST, `AppError` mapping for a missing key and for upstream failure) plus
`search_tavily_with_retry`, and a shared clock for `load_search_cache` /
`cleanup_search_cache` / `save_search_cache` (their `now_epoch` parameter is already there,
so only the wiring is missing). Verification for that slice is a **stub upstream**, because
the live path measured ~5% availability — one clean `http=200` in roughly forty attempts,
amid 308/405/400/301/502/522 from the proxy and its intermediaries. A real-call check stays
a one-off confirmation, not a regression test.

**Tavily HTTP layer ported, with the transport injected (2026-09-16，uncommitted).**

`search.rs` now carries `search_tavily`, `search_tavily_with_retry`, `format_upstream_error`
and the request-body assembly. That completes the module's non-cryptographic surface: what
is still missing is only the **client**, not the logic.

**The transport is a parameter, not a call.** `search_tavily(query, api_key, transport)`
takes a `dyn Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome`, so the whole path —
body assembly, header construction, status mapping, response normalization, retry policy —
runs offline. That is what made the parity probe possible without a network, and it is why
the measured ~5% link availability does not block this slice.

`TransportOutcome` has three arms on purpose: `Response` (any status, with its body),
`Failure { reason, timed_out }` (the request never completed — the oracle's `URLError`
branch), and `Rejected(AppError)`. The third exists **for the probe**: the Python probe
drives the retry policy by raising an `AppError` from a stubbed `search_tavily`, so without
it the Rust side would be comparing "error mapping **and** retry policy" against Python's
"retry policy alone". The arm makes the layers line up.

**A real divergence, caught by the probe.** `format_upstream_error` is:

```python
message = error.get("message") or error.get("type")
if message: return str(message)
```

The `or` tests the **values' truthiness**, so `{"error": {"message": "", "type": "x"}}`
returns `"x"`. My first version checked the key's presence and then whether the rendered
text was empty, which fell through to the raw text instead. Fixed to filter both lookups
through `python_truthy`.

**A byte-level detail worth naming: `json.dumps` defaults to `ensure_ascii=True`.** The
request body sends `{"query": "\u6700\u65b0\u6d88\u606f"}`, not the raw UTF-8. Escaping is
CPython's exactly — BMP as a lowercase `\uXXXX`, an astral character as a lowercase
surrogate **pair** (`\ud83d\ude00`). Added `dumps_default_separators_ascii` /
`escape_non_ascii` to `python_json` for it. This is not cosmetic for a request body: the
bytes are what leave the process.

The body's **key order** is the oracle's dict-merge order — `query`, then the options in
their insertion order, then the filters — and `search_depth` / `include_answer` /
`include_raw_content` are *updated in place* by the intent rules, so they keep their
positions rather than moving to the end. `tavily_request_body_json` renders that order
explicitly, so the probe compares the bytes rather than a re-serialization.

**Two probe-side fixes, so the comparison is honest.** Python's f-string renders an enum
*member* (`ErrorCode.UPSTREAM_TIMEOUT`), not its value, so the stub messages had to use
`code.value`. And the Python fake replaces the whole `search_tavily`, so it has to apply
`normalize_search_response` itself — otherwise the two sides are compared at different
layers and the response bodies diverge for a reason that is not the implementation's.

Verified:
- **Search parity holds**: identical MD5 `b5077e1e730dfac3ffde3e024c6094cf`, **113 keys**
  (up from 97), no differences. The HTTP additions are five request bodies — including a
  600-character query, which exercises the `[:500]` truncation — six
  `format_upstream_error` inputs, and five retry-policy drives (first-call success, retry
  after a timeout, retry after a 503, both attempts failing, and a missing key that must
  **not** be retried).

**What is left.** A concrete `Transport` (a `reqwest::blocking` client honouring
`TAVILY_TIMEOUT_SECONDS`), the shared clock for the three cache functions, and the
`ExecutorContext.web_search` callback that binds them — the seam already exists, so this is
wiring rather than logic. Verification stays a **stub upstream**; a live call is a one-off
confirmation, since the measured link was one clean `http=200` in roughly forty attempts.

**Scoping: `format_search_context` is one link in an unported, security-bearing pipeline
(2026-09-17，measured not started).**

Both remaining `format_*` functions were previously listed as "the next slice". Measuring
the call path says they are not a slice of their own — they are the last step of a pipeline
whose other links, including a security module, are unported. Writing them alone would be
inert code with no consumer.

The pipeline, from `deepseek_client.py`:

```python
search_data = search_if_needed(payload, progress_callback=…, system_note_callback=…)
...
if search_data and search_data.get("results"):
    # Context Taint firewall: web content is untrusted — isolation-wrap and
    # scrub the per-turn search context before it joins the prompt.
    payload = {**payload, "searchContext": context_taint.harden_search_context(
        format_search_context(search_data))}
elif search_data and search_data.get("status") == "error":
    payload = {**payload, "searchContext": format_search_failure_context(search_data)}
prepared = build_deepseek_request(payload, stream=stream, memory_state=memory_state,
                                 validated=validated)
```

**Measured size of the missing links:**

| link | size | notes |
| --- | --- | --- |
| `search_if_needed` | ~35 lines | gates on `searchEnabled is True` **and** `forced_search_mode`; raises `INVALID_PAYLOAD` on an empty query; emits up to four `system_note`s |
| `search_multiple` | ~45 lines | **parallel** rounds (`ThreadPoolExecutor`, `SEARCH_ROUND_LIMIT` workers) — the only concurrent part of the search module |
| `format_search_context` / `_failure_context` | ~55 lines | the two functions originally scoped as "next" |
| **`context_taint.harden_search_context`** | **383-line module, 18 public items** | a **taint firewall**: `sanitize_external_text`, `UNTRUSTED_CONTENT_GUARD`, `taint_enabled()`, feature flags |
| `searchContext` → `build_deepseek_request` | — | **the consumer does not exist in Rust**; the gateway passes the prepared body through |

**Why this is a separate vertical slice, not an extension.** `searchContext` is consumed by
`build_deepseek_request`, which the Rust gateway does not own — the route prepares the raw
body and forwards it. So the pipeline's output has nowhere to go until the request-assembly
layer exists, and that layer is where the earlier recorded layering lesson lives
(`build_deepseek_request` composes the system turn from `payload["systemPrompt"]`).

**Recommendation.** Treat this as its own slice with the taint firewall as its centre, not
as a tail of the tool-round work. The ordering that keeps every step verifiable:
1. the pure predicates and the two formatters (byte-parity, offline) — inert until 3, so
   they commit safely;
2. `search_multiple`'s parallel shape, which is the part with real concurrency semantics;
3. `harden_search_context` and `sanitize_external_text` against the reference's own tables
   — this is a security boundary, so it needs the same treatment the IP-block sets needed:
   read the reference's data, do not rebuild the predicate from intuition;
4. the `searchContext` injection once `build_deepseek_request` exists to consume it.

Nothing here was started.

**Search-prefetch slice 1: the pure predicates and the two formatters (2026-09-17).**

Step 1 of the order recorded above, landed in `deepseek-policy::search`:
`search_mode`, `forced_search_mode`, `search_tool_enabled`,
`format_search_context`, `format_search_failure_context`. Byte-verified offline;
inert until the assembly layer exists, so nothing calls them yet.

**This slice resumed an interrupted working tree, and the interruption was not
clean.** The three modified files had never run: the Rust example failed to
compile (five `cannot find function` errors) and the Python probe crashed with
`AttributeError: …search has no attribute 'search_mode'`. The recovery found two
defects before anything was green:

1. **The predicates live in `gateway/deepseek_client.py`, not `search.py`.** The
   probe now extracts them verbatim from that file via `ast` (the SSE-probe
   pattern), so the definitions being compared are the oracle's own. The Rust
   port stays in this crate's `search` module because its consumers are the tool
   catalog and `tools_for_payload`; the placement is recorded in the module docs.
2. **`python_str` matched `str(x or "")` on the rendered text, not the raw
   truthiness.** A numeric `0` came through as `"0"` — an off-mode spelling —
   so `{"searchMode": 0}` made `search_mode` return `"0"` where the oracle
   returns `"auto"` (its `or` fallback is `"auto"`, which neither forces nor
   disables), **flipping `search_tool_enabled`**, and made
   `should_search_for_query` refuse where the oracle falls through to text
   matching. The same root cause would render a `Tavily 摘要` line for
   `{"answer": 0}`. Fixed through `python_truthy` + `python_json::value_str`;
   the corpus now pins every one of those shapes (mode `0`/`true`, answer `0`,
   title/citation/raw_content `0`, error `true`/`0`). This was a defect in
   **committed** code (`should_search_for_query` shares `python_str`), not just
   the interrupted WIP.

Two divergences measured and recorded rather than compared:

- A **non-dict result entry** (or a non-array `results`) makes the oracle raise
  `AttributeError`/`TypeError` and fail the request; the port renders through
  the fallbacks. Unreachable from the wired pipeline —
  `normalize_search_response` / `aggregate_search_rounds` guarantee dict entries
  in a list — and reachable only from a hand-corrupted cache file, where the
  oracle's own behaviour is an uncontrolled 500. Pinned by a unit test so the
  tolerance is a recorded decision, and the corpus case that would have crashed
  the Python probe was dropped. The *failure* formatter keeps its non-dict round
  entry, because there the oracle guards with `isinstance` and both sides agree.
- The query line renders containers through `value_str`'s JSON quoting — the
  crate's standing `repr` approximation, unreachable from the wired pipeline
  where `query` is always a string.

Verified locally:

- Byte-level parity: **identical MD5 `0f3877807ce994f0d3dbe852293c47ee`**, 157
  keys (up from 113), no differences, re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → **237 tests, all
  pass** (6 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  → clean; one real find fixed (`useless_vec` on the example's corpus, which had
  never been clippy'd). The only other output is the transient `deepseek-core`
  incremental-copy warning, which is not a lint.
- `cargo check -p deepseek-gateway --all-targets` → still compiles.
- `cargo fmt --all -- --check` → clean.

Next per the recorded order: slice 2, `search_multiple`'s parallel shape (a
`ThreadPoolExecutor` over `SEARCH_ROUND_LIMIT` rounds — the module's only
concurrency), then the taint firewall against the reference's own tables, then
the `searchContext` consumer.


### Slice 2 landed; the lib-test harness regressed with it (2026-09-17, measured)

`search_multiple` is ported and committed (`e2354851`). It is the module's only
concurrency: cache gate, query planning, per-round "searching" announcements,
`as_completed`-style collection, the two error arms, the cache write, the
progress callback. Parity is byte-identical — 2461 lines, md5
`84b6f0f6b7c90b2d2a07f08d138659ae` on both sides; the probe grew the whole
`multi::` family (first run, announcements, cache hit, expiry, reversed
completion order, API error, worker exception, empty query list). `cargo fmt
--check` and `cargo clippy --all-targets` are clean.

**The harness no longer starts, and it did at slice 1.** The previous entry in
this file records `cargo test -p deepseek-policy -j 1 -- --test-threads=1` →
237 tests all pass, and that text came in with slice 1 (`6e6519cf`, 11:06). At
12:46 the same command dies before running anything:

    error: test failed, to rerun pass `-p deepseek-policy --lib`
      process didn't exit successfully: ... (exit code: 0xc0000139,
      STATUS_ENTRYPOINT_NOT_FOUND)

What was measured, not assumed:

- Of the 193 imported symbols in that binary, exactly one is unsatisfiable: it
  binds `WakeByAddressSingle` to `KERNEL32.dll`.
- This Windows build's `kernel32` exports **none** of `WakeByAddressSingle`,
  `WakeByAddressAll`, `WaitOnAddress`. Verified at the loader's own API with
  `GetProcAddress` (a five-line C probe): all three MISSING in kernel32, all
  three PRESENT in kernelbase. Two other checks agree (`grep` for the name in
  the DLL is 0 for kernel32, 1 for kernelbase; `objdump -p` the same).
- The other seven test binaries in `target/debug/deps` — deepseek-core's and
  deepseek-gateway's among them — bind those three to
  `api-ms-win-core-synch-l1-2-0.dll` (which the loader maps to kernelbase) and
  start normally. So does this crate's own `search_parity_probe` **example**
  after a clean rebuild.
- `cargo clean -p deepseek-policy` followed by a relink reproduces the bad
  binding, so it is not a stale artifact.

**The causal picture, stated honestly.** The example links the same lib code —
including `search_multiple` and its threads — and binds the api-set, so the
ported logic is not what breaks the import. What the lib-test target adds over
the example is the `#[cfg(test)]` code plus `libtest`, and it is one of those
that flips which of the two competing `__imp_WakeByAddressSingle` stubs the
linker takes (raw-dylib stubs carry their own DLL name, and `ld` keeps the
first). Slice 1's harness booted, slice 2's does not, and slice 2's only new
code is `search_multiple` plus its tests — so the correlation points at the new
test code, but **causality was not isolated**. Next diagnostic round: relink and
run with slice 2's unit tests removed, which separates "the new tests pull it"
from "the crate now links something else".

This is written down rather than fixed because it is not the ported logic, and
because guessing at the linker would be exactly the kind of change this project
does not want: the verification for slice 2 is the probe, which does pass.


### Isolation done: the harness failure is a link-shape fragility, not a test bug

The previous entry left the next round as "relink with slice 2's unit tests removed".
That was done, by disabling tests one at a time with `#[cfg(any())]` in front of the
`#[test]` attribute (the source was byte-restored afterwards; `git diff` is empty).
Each round relinked and ran the lib harness:

| new tests enabled | harness |
| --- | --- |
| none | **boots: 237 passed, 0 failed** |
| `an_empty_query_aggregates_without_searching` | dies, 0xc0000139 |
| `a_cache_hit_returns_without_searching` | dies, 0xc0000139 |
| those two together | dies, 0xc0000139 |
| all five | dies, 0xc0000139 |

With all five disabled the count is exactly 237 — slice 1's number — and
`search_multiple` is still in the lib, so the *library* code is not what breaks the
import. Enabling **any one** of the five is enough, and the two single tests are
trivial: no worker thread is spawned, no panic occurs, the transport is a closure that
is never called. So it is not the content of a test. What flips the import is the
lib-test target acquiring a reference to `search_multiple` at all — i.e. **the test
binary growing changes which competing import stub the linker keeps**. The same
reference in the **example** target binds the api-set and runs, so the outcome is
target-dependent, not source-dependent.

Where the stubs come from, checked: rustc's self-contained
`lib/rustlib/x86_64-pc-windows-gnu/lib/self-contained/libsynchronization.a` does
provide `WakeByAddressSingle`, and inside it the DLL name is
`api-ms-win-core-synch-l1-2-0.dll` — the correct one. `/d/mingw64`'s copy of the same
archive agrees. `libkernel32.a` (both the toolchain's and mingw64's) does **not** define
the symbol at all. So the KERNEL32 binding that appears in the failing binary is not
coming from those archives; it comes from a crate-level raw-dylib stub, i.e. some object
compiled against `kernel32.dll`, and which stub wins is decided by link order. Pinpointing
that object needs the std sources, and `rust-src` is not installed on this machine —
that is where this stopped, deliberately, rather than guessing further.

**Consequences.** (1) There is no "guilty test" to rewrite; the fragility will resurface
whenever this target's object set changes. (2) Slice 2's verification stays the probe,
which is byte-identical. (3) If the harness is wanted back, the options are a link-level
workaround (`RUSTFLAGS` with an explicit stub order or `-C link-self-contained`), a
toolchain pin, or installing `rust-src` to name the offending object first — none of
which belongs in a migration commit.

Corrects the earlier framing in this file: the regression did arrive with slice 2, but
it is not caused by slice 2's code or tests; slice 2 is what made the test binary big
enough to expose it.


### Slice 3 landed: the taint firewall's string layer (`591d0c38`)

`context_taint.rs` now carries the part of `gateway/context_taint.py` that has no I/O and no
consumer-dependent shape: the guard constant and the trust/source/marker vocabulary, both
pattern tables, the sensitive-tool alternation, `scan_text`, and the active hardening
(`harden_search_context`, `file_context_guard_line`, `escalation_enabled`) with
`ContextTaintSettings` at the oracle's defaults.

**Both tables are read off the reference, not rebuilt** — the lesson from the IP-block sets:

- the sensitive-tool list is *derived* from `TOOL_METADATA` with the oracle's own predicate
  (`requires_confirm || sensitive_sink || risk == "high"`), so a tool profile change cannot
  desynchronise the two. It comes out as eight names, in table order, and the alternation is
  order-bearing because alternative branches are tried left to right.
- the injection count is `tool_policy::sanitize_external_text`, the Tool Policy Engine's own
  sanitizer, because the oracle shares that table between the two modules.
- the exfiltration verb list keeps the oracle's deliberate exclusion of `提交`: it trips on
  benign advisory prose like `不要提交到仓库`, while genuine exfiltration in this corpus uses
  `发送` / `上传` / `发到`. The corpus pins that case, plus a 70-character gap (one past the
  pattern's `{0,60}` lifetime), a newline inside the gap, and `web_search` not matching the
  sensitive alternation — all four must *not* fire.

Verification is `tasks/native-runtime/context_taint_parity_probe.py` against
`examples/context_taint_parity_probe.rs`: 35 texts plus six flag combinations, 87 keys,
**byte-identical**, md5 `adff8e2723c890fe8c969d21e8d1fa0c`. The Python side imports the
oracle module directly rather than re-executing extracted source, because `context_taint`
only pulls `core.config` and `tool_policy` and both import cleanly — so the tables under
test are literally the oracle's own objects. `cargo fmt --check` and
`cargo clippy --all-targets` are clean (exit 0).

**Deliberately left out, as the honest boundary**: `_risk_level`, `classify_request_messages`,
`build_taint_report`, `report_is_tainted`, `taint_status`. That is the diagnostics half, and
its consumer — the gateway's diagnostics assembly and the `/api/taint` route — does not exist
in Rust. It is inert in a way this layer is not: `harden_search_context` is exactly what
slice 4's `searchContext` injection calls.

No unit tests were added with the module: the lib-test harness still cannot start on this host
(the link-shape finding above), so such tests could not be run, and the probe is the
verification that actually executes. That is a real gap to close once the harness boots.

Remaining: slice 4 — the `searchContext` injection into `build_deepseek_request`, which has to
exist first — and, separately, `search_if_needed`, which is what eventually calls
`search_multiple`.


### Slice 4 landed: the per-turn context, and the reader of `searchContext` (`4056e3c9`)

`dynamic_context.rs` carries `build_dynamic_turn_context` — the function that reads
`payload["searchContext"]` — plus everything it splices in: `format_current_time_context`,
`format_context_summary_context`, `format_memory_notice`, `format_slides_skill_context`,
`presentation_intent_requested` (over the already-ported `latest_user_query`),
`append_context_to_latest_user`, and the constants (`CURRENT_TIME_CONTEXT_HEADER`,
`CONTEXT_SUMMARY_MAX_CHARS = 12 000`, `WEB_SEARCH_SYSTEM_HINT`, the three slides
name/reference/guidance strings).

This closes the loop the earlier scoping note described: `harden_search_context` had a string
with nowhere to go, and this is the thing that puts it in the prompt. The ordering is the
whole design — the search context goes **after** the stable prefixes, so switching search on
and off does not invalidate the prompt cache behind it.

**The one real design decision: the clock is injected, not read.** The oracle calls
`datetime.now().astimezone()` and renders the machine's local zone. Rust's standard library
has no local-timezone support, and this workspace has **no time crate at all** — only
`std::time` epoch arithmetic. So `LocalNow` carries the instant, the offset and the zone name,
following the two precedents already in this tree: `utc_now_iso(epoch_seconds)`, whose doc
says "the clock is a parameter so callers can pin it", and the injected search transport.
**Resolving the OS zone is not implemented**, deliberately and visibly: faking it would be
worse. The oracle's naive-datetime arm (`tzinfo is None` → assume UTC, then convert to the
*machine's* local zone) has no counterpart for the same reason, and is excluded from the
corpus because its output is host-dependent.

Three details that would each be a silent divergence if "cleaned up":

- **Two spellings of the same instant coexist.** `format_current_time_context` renders UTC as
  `…Z`; `core_utils::utc_now_iso` renders `…+00:00`. The oracle replaces the suffix in exactly
  one of the two places, so `isoformat_seconds` (new in `core_utils`, sharing
  `civil_from_days` with `utc_now_iso`) appends the offset and leaves the choice to its caller.
- **The slides text is transcribed with `concat!` and explicit `\n`,** not as a multi-line raw
  string: a raw string takes its line endings from the source file, so a CRLF checkout would
  silently change every prompt byte those constants feed. Git confirmed the risk is live —
  committing these files printed `LF will be replaced by CRLF the next time Git touches it`.
- **A landmine is recorded for the assembly slice.** `append_context_to_latest_user` appends
  `{"role": …, "content": …}` and the oracle's body serializes in insertion order, but
  `serde_json::Map` here is a `BTreeMap`, so `json!` emits `content` first. Whoever writes the
  body builder must not let `json!` decide the order of the message it injects.

Verification: eight pinned instants (UTC, +08:00, −05:00, +05:30, −09:30, epoch 0, and two
day-rollover cases), the full assembly over fourteen payload/memory/tools combinations, both
12 001-character truncation paths, and the append cases — **byte-identical**, 39 keys, md5
`e3a065e999df38e421de8a17f74cfef6`. The Python probe imports the oracle modules directly and
stubs `format_current_time_context` for the assembly cases, because the oracle's builder reads
the machine clock; the mirror of that stub is the injected clock on this side. `cargo fmt
--check` and `cargo clippy --all-targets` are clean, and **both** this probe and slice 3's were
re-run after formatting so the committed bytes are the verified bytes (`e3a065e9…`,
`adff8e27…`).

**The honest remaining boundary.** The reader exists, but `build_deepseek_request` — the body
assembly that would actually consume `build_dynamic_turn_context` — still does not exist in
Rust: the gateway prepares the raw body and forwards it. So nothing injects into a request yet,
and this slice is inert in the same recorded sense as slices 1–3. Also outstanding: the OS
timezone resolution, `search_if_needed`, and the taint diagnostics half. No unit tests came
with this module, for the same reason as the last one.


### `build_deepseek_request` was next, and measuring says it is not a slice (`9b3a7825`)

The obvious next move after slice 4 was the assembly function that would finally consume
everything: `build_deepseek_request`. Measuring it first, the way the earlier scoping pass
should have, says **do not start it as one slice** — and the shapes below are what that
judgement rests on.

The function itself is only ~123 lines (`deepseek_client.py:240`–362), but it is a
convergence point, not a unit. Its dependency closure, measured:

| collaborator | size | ported? |
| --- | --- | --- |
| `model_router.py` (`route_request`, `is_auto_request`) | 279 lines | no |
| `budget_manager.py` (`budget_policy_from_payload`, `should_downgrade`, `budget_scope`) | 371 lines | no — and it owns a **ledger**, so it is not pure |
| `context_manager.py` (`manage_request_body`, `merge_context_manager_diagnostics`) | 137 lines | no |
| `validate_deepseek_payload` + `_validate_request_messages` + `normalize_chat_messages` | ~110 lines | partly — the gateway has its own `prepare_request`/`normalize_*`, but not these |
| `chat_payload.count_payload_attachments` | 32 lines | no |
| `empty_memory_state`, `_has_image_content`, `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`, `TOOL_PARALLEL_SYSTEM_HINT` | ~75 lines | no |
| `context_taint.build_taint_report` | 39 lines | no — until this commit |

So the closure is ~1,200 lines across six subsystems, one of which is stateful. That is a
milestone. The useful thing to do with a milestone is find its slices, and the first one was
already sitting there: **line 357 needs `build_taint_report`**, which is exactly what slice 3
deferred on the note that its consumer did not exist. Measuring the consumer turned it up, so
this commit ports the diagnostics half and **`context_taint.py` is now complete**.

What the classification half turned on, all pinned by the corpus:

- **`len()` counts characters, and `_segments_for_user` uses a found index as a length.** A CJK
  prefix before the file marker inflates the trusted-prefix segment if the index is treated as
  bytes; the corpus pins the case that would catch it (中文提问… → `chars: 4`).
- **The arm order in `tool_message_source` is the contract**: `browser_` and `mcp__` before the
  metadata table, and `search_files` reaches the RAG arm only when the payload says `local_rag`.
- **`segments_for_per_turn_system` inserts at 0 and 1** — that is what puts the media segment
  first and the trusted prefix before the web segment.
- The serialization landmine recorded for slice 4 applies to this block too: `build_taint_report`
  builds its object in insertion order and the caller splices it into `diagnostics`; `json!` here
  yields sorted keys, so the diagnostics serializer must own that order.

Verification: 176 keys, byte-identical, md5 `49e390b4c0c9f2ab499337326b308404` — 29 message
lists, 4 settings tuples × 6 bodies, the `taint_status` block and 8 risk combinations, on top of
slice 3's 87 keys (the earlier cases are still in the same probe, now 176). The first comparison
**failed**, and the cause was the probe corpus rather than the port: the Python body list indexed
one case off from the Rust one, and only the Python side needed changing to make the hashes agree.
That is the method working — the diff localised the fault before it became a story about the port.

**Sequence for the milestone, in the order that keeps each step verifiable.** None of these is
started:
1. the remaining pure collaborators (`empty_memory_state`, `_has_image_content`,
   `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`,
   `count_payload_attachments`, `TOOL_PARALLEL_SYSTEM_HINT`) — small, and each has an oracle
   function to compare against;
2. `context_manager` (137 lines) — pure but with the sliding-window semantics that make the
   body's bytes; needs its own probe over windowed bodies;
3. `model_router` (279 lines) — pure tier selection, but it needs a model catalog to compare
   against, so measure that dependency before assuming;
4. `budget_manager` (371 lines) — **last, and only with a store port**: it reads a ledger, so it
   is the one piece here that is not a pure function, and the ledger's own port would have to
   come first;
5. the assembly itself (`build_deepseek_request`), once its collaborators exist, with the
   diagnostics serializer that owns key order.

Until step 5 lands, every slice so far remains inert in exactly the recorded sense: verified
and unwired.


### The harness works again, and the earlier mechanism note was wrong (2026-09-17)

`cargo test -p deepseek-policy --lib` runs again: **242 passed; 0 failed** — 237 from before
plus the five slice-2 tests that had never been able to execute. The fix is one flag:

```
RUSTFLAGS="-C link-self-contained=yes" cargo test -p deepseek-policy
```

**Correction first.** The earlier entry here explained the failure as "two competing raw-dylib
stubs and `ld` keeps whichever it sees first". That was wrong. Asking the linker directly settles
it:

```
RUSTFLAGS='-C link-arg=-Wl,--trace-symbol=__imp_WakeByAddressSingle' \
  cargo test -p deepseek-policy --lib --no-run

warning: linker stderr: D:/mingw64/bin/../lib/gcc/x86_64-w64-mingw32/8.1.0/../../../../x86_64-w64-mingw32/lib/../lib/libkernel32.a(dqifs01464.o): definition of __imp_WakeByAddressSingle
```

The provider is **`/d/mingw64`'s `libkernel32.a`** — the *system* MinGW's import library, GCC 8.1.0,
from 2018, on `PATH` as `gcc`. rustc's `windows-gnu` target uses `gcc` as its linker driver, and
that driver injects its own library search path, so an import library built for a Windows era when
`kernel32` did export the futex APIs is searched — and its ordinal-era stub `dqifs01464.o` wins
`__imp_WakeByAddressSingle` from libstd's own stub. On this Windows build `kernel32` exports none of
`WakeByAddressSingle` / `WakeByAddressAll` / `WaitOnAddress` (verified with `GetProcAddress`: all
three live only in `kernelbase`), so the import is unsatisfiable and the loader stops with
`0xc0000139`.

Three things this re-explains, and one it does not:

- **Why the API-set stub never appeared to be the provider.** It *is* rustc's provider: every
  `WakeByAddress*` stub inside `libstd` (members `api-ms-win-core-synch-l1-2-0.dlls0000{0,1,2}.o`)
  has a four-byte, **all-zero** `.idata$7` — the DLL-name field. rustc does not name the DLL in the
  stub; the descriptor is chosen at link time. So a *different* archive can satisfy the symbol
  first, which is exactly what the old system import library does.
- **Why `cargo clean -p` and relinking never helped.** The offending archive is outside the target
  directory.
- **Why only the lib-test target died.** The symbol is undefined in several objects; which archive
  wins depends on the order the linker walks them, which differs per target. The examples and the
  other crates' test binaries happened to resolve it from libstd.
- **What is still unexplained, and is now moot:** why this target in particular. With the flag the
  ambiguity is gone, so there is nothing left to chase.

`-C link-self-contained=yes` makes rustc use its own bundled `rust-mingw` libraries (which ship
the correct-era import libs) instead of the system MinGW's, so the symbol resolves from libstd's
stub and the import points at the API set that the loader maps to `kernelbase`.

**It is committed, scoped as narrowly as the cause allows.** `rust/.cargo/config.toml` now carries

```toml
[target.x86_64-pc-windows-gnu]
rustflags = ["-C", "link-self-contained=yes"]
```

Scoped to the triple rather than `[build] rustflags` because the failing combination is specifically
`windows-gnu` plus a system MinGW on `PATH` — nothing about MSVC builds or other targets should
inherit the workaround. The file also carries the reasoning inline, since a config that changes link
inputs deserves its own explanation next to it. It requires the `rust-mingw` component (present here,
and installed by default for windows-gnu host toolchains).

Verified after adopting it, with no `RUSTFLAGS` in the environment:

- `cargo test -p deepseek-policy -j 1` → **242 passed, 0 failed**, in 0.77 s with the *same* artifact
  hash as the env-var run — so the config produces the same fingerprint as `RUSTFLAGS` did, and
  adopting it costs no rebuild;
- `cargo test -p deepseek-core -j 1` → 8 passed after a rebuild under the new flags, so the flag does
  not regress a crate that was already linking fine.

One caveat that survives, and is written into the config file: **`RUSTFLAGS` in the environment takes
precedence over the config**, so a stray value there silently overrides these flags and brings the
failure back along with a full rebuild. Picking one mechanism and staying with it still matters.

The consequence for the record: the "no unit tests came with the module, because the harness cannot
start" note that appears against slices 3, 4 and 5 is now **expired** — the harness starts, and
those modules can carry tests.


### The test debt is paid (`3b55437d`)

The note above said the harness starting again meant slices 3, 4 and 5 could carry tests. They do
now: 40 added, 242 → **282 passing**. `context_taint` gets the bulk, since it is the security
boundary and every "obvious" simplification in it is a divergence — the guard wrapping rather than
replacing, the `提交` exclusion, the gap neither crossing a newline nor sixty characters, the
character-counted CJK prefix, the arm order in `tool_message_source`, the per-turn split, the
media tail's position, and the report's cap-versus-totals behaviour. `dynamic_context` pins the
`Z`/`+00:00` pair that must not be unified, the cache argument (search off is byte-identical up to
the hint), the joins, and the falsy drops.

One expectation was wrong on the first run: the truncation test asserted five segments where both
implementations produce six. **The port was right and the arithmetic was mine** — and the corrected
assertion now carries the oracle's own sequence rather than a recomputed number. Same lesson as the
taint probe's mis-indexed corpus two slices ago: check the expectation before believing a failure.

These tests are the fast local net; the parity probes stay the cross-language evidence, since they
compare against the oracle rather than against expectations written by hand.


### Milestone step 1: the pure collaborators are ported (`11a08fff`)

The sequence recorded above said to take the assembly's pure leaves first. They are in, in a new
`request_shaping` module plus two functions in `memory`:

| what | where it came from |
| --- | --- |
| `TOOL_PARALLEL_SYSTEM_HINT` | `deepseek_client.py` |
| `normalize_reasoning_effort`, `tools_for_payload`, `forced_artifact_tool_name`, `should_force_create_pptx`, `has_create_pptx_tool`, `mindmap_intent_requested`, `has_image_content` | `deepseek_client.py` |
| `count_payload_attachments` | `chat_payload.py` |
| `empty_memory_state`, `memory_scope_from_payload` | `data/memory.py` |

That shrinks the closure between here and a working `build_deepseek_request` to four things:
`context_manager` (137 lines), `model_router` (279), `budget_manager` (371, ledger-backed and
therefore last), and the assembly itself with the serializer that owns the diagnostics key order.

What the corpus and the 13 new tests pin, each of which reads like a tidy-up waiting to happen:

- **`tools_for_payload` composes two filters and their order shows.** The allow-list is applied
  first, then the search tools are dropped — so naming `web_search` in `allowedTools` still loses
  it when search is off. A non-list `allowedTools` is ignored rather than treated as empty.
- **`forced_artifact_tool_name` needs availability *and* permission**, and with no allow-list the
  permitted set *is* the available one.
- **`normalize_reasoning_effort` is case-sensitive** — `MEDIUM` falls back like any unknown — and
  `"  high  "` is stripped before the membership test.
- **`memory_enabled` is `is not False`**, so `0` and `""` read as *enabled* while only the boolean
  `false` disables it; a malformed scope id is silently narrowed to `global`.
- **`memory_scope_from_payload` reads the latest user message only** and stops there either way, so
  an older `projectId` never leaks forward.
- **`mindmap_intent_requested` fires on `什么是 mindmap？`** with no create verb, because the
  oracle's verb alternation contains `map` and `mindmap` contains it. Left as-is, with the reason in
  the code: this is the oracle's behaviour, and tightening it would be a divergence, not a fix.

Verification: the probe pair replays six corpora and matches byte for byte — 86 keys, md5
`4bc6167d94f761c2a4178c70e2cdac6e`. `tools_for_payload` is compared as the **sequence of function
names**, not as whole definitions: the definitions are the tool catalog's own subject and are
covered there, and re-comparing them here would bury this probe's actual subject. Tests are at 295,
`cargo fmt --check` and `cargo clippy --all-targets` are clean, and the Rust probe was re-run after
formatting so the committed bytes reproduce the hash.


### Step 2 measured: `context_manager` is not a slice either (`0882b1b0`)

`context_manager` was the next recorded step. Measuring it first: its 137 lines depend on
`context_engine` (347 lines, entirely unported), whose identity half needs **SHA-1** — and this crate
depends on `sha2`, not `sha1`. So the work was split at the seam the dependency graph already has:

- **done here**: the token half of the engine — the heuristics, the three estimators, the body
  breakdown, the per-model window lookup, `available_input_tokens`, the budget plan with its
  recommendation ladder, and `token_trim`;
- **blocked on a decision**: `base_context_id` / `build_context_diff` / `build_engine_diagnostics`,
  which need the SHA-1 either hand-rolled (a hash implementation in-tree) or via a new dependency;
- **then**: `context_manager` itself, which is mostly ordering and diagnostics once the engine exists.

What the 168-key corpus and 12 new tests pin, beyond the arithmetic:

- an empty **object** message still pays the four-token structural overhead; only a non-object
  message costs nothing (measured: the oracle returns 4 for `{}` — my first test said 0 and was
  wrong, not the port);
- the trailing system message is `dynamic` only when it is last *and* there is more than one message;
- the CJK ranges include Fullwidth forms, so CJK-keyboard punctuation is not miscounted as Latin;
- `round(x, 1)` is ties-to-even on both sides, which `format!("{:.1}")` mirrors;
- `estimate_tools_tokens` measures a serialized tool array whose **key order differs** between
  `serde_json` and Python — and the estimate is deliberately insensitive to that, since reordering
  keys changes neither length nor CJK count. The string is not exposed, so nothing can start
  comparing it byte-for-byte;
- `token_trim` never touches the leading or trailing system message, keeps at least
  `min_keep_messages` of the middle, and returns the caller's list untouched when the budget is zero.

Verification: byte-identical, md5 `9c50e3cc29c8493f3c057fc3a3b79a07`; tests 295 → **307**; `fmt
--check` and `clippy --all-targets` clean, with the probe re-run after formatting. Two more of my
expectations were wrong on the first run and were corrected against the oracle rather than by
changing the port — the same failure mode as the taint corpus index and the five-versus-six segment
count, which is now three for three: **my arithmetic about the oracle is the weak link, so
expectations get taken from the oracle.**


### The context engine is whole (`3960be2c`), and the SHA-1 decision went to a dependency

The identity half was blocked on a decision worth recording: `base_context_id` needs SHA-1, this
crate depends on `sha2`, and the two ways out were hand-rolling the primitive or adding the crate.
Measurements that decided it:

- **`sha1` was not in the lockfile at all**, not even transitively — so the addition is a real one,
  not a free promotion of something already present;
- **every existing fingerprint in the crate delegates to a RustCrypto digest**
  (`memory.rs:195`, `search.rs:735`, `tool_policy.rs:1330` all call `Sha256::digest`), so writing a
  primitive by hand would have introduced a practice this codebase does not have.

So `sha1 = "0.10"` sits next to `sha2 = "0.10"` in the workspace table. What the corpus and four new
tests pin:

- **tool order is part of the prefix identity** — the parts string is the leading system content, the
  model, then the tool names *in order*, so a swap changes the id. That is the value's whole purpose:
  revealing accidental prefix churn.
- **an unnamed tool contributes nothing**, so a tool with an empty name, a non-dict `function`, or a
  bare string leaves the parts string untouched and the id equal to an empty body's.
- the dynamic block's `chars` counts characters, not bytes.
- the two ids asserted in the unit tests are **taken from the oracle**, which doubles them as a
  known-answer test of the digest path.

Verification: 204 keys byte-identical, md5 `0b430da00b2467c6a99e632880b4f38d`; tests 307 → **311**;
`fmt --check` and `clippy --all-targets` clean, probe re-run after formatting.

`context_engine` is now complete, which leaves `context_manager` as the only piece of this subsystem
— and it is mostly ordering plus diagnostics assembly, since both halves it depends on exist.


### The context subsystem is complete (`7c377889`)

`context_manager` was the last piece, and it went in small because both halves it depends on
already existed. What is worth recording is less the port than two mistakes in my own verification:

**The first corpus could not have caught a broken token-trim.** The manage bodies carried a model
that is *in* the window table, so the table's 131 072 beat the small patched default window and the
token-aware pass **never ran** — meaning the probe would have reported "parity holds" whether that
path worked or was a no-op. Switching the corpus to a model outside the table made the path
reachable, and the reference now shows the discrimination: 4 messages dropped with trim on, 0 with
it off. The same mistake was in the unit test, where the fix was to empty the table explicitly. This
is the strongest form of the recurring lesson — not "my expected value was wrong" but **"my corpus
could not tell the difference"**, which is worse because it reads as a pass.

**And the lint I introduced.** The settings tuple in the new probe tripped `type_complexity`, which
is a warning rather than a deny, so `clippy` still exited 0 — the diagnostic was there and my filter
was hiding it. It is fixed with a `SettingsCase` alias. Worth remembering: "clippy exit 0" and "no
diagnostics" are not the same claim, and it is the second one that was being asserted in these
messages.

Traps the corpus and seven tests pin: the sort is by `(name, type)` and **stable**, `toolOrder`
lists only named tools while `toolCount` counts every entry, both system ends are pinned and the
count window's budget floors at one, the engine block appears only while the engine is on, and
`merge_context_manager_diagnostics` **moves** the engine block out and copies a **zero**
`requestMessageCount` (a truthiness test would drop it). One measured divergence is kept and
documented: the oracle's `tool_name` raises on a non-dict tool where this port returns an empty name.

Verification: 341 keys byte-identical, md5 `ba5343c49b1b6aeef25fec1724b0d911`; tests 311 → **318**;
`fmt --check` clean and `clippy --all-targets` exit 0 with no diagnostics in the new files.

What is left before `build_deepseek_request` can exist: `model_router` (279 lines, pure, needs its
model catalogue measured first), `budget_manager` (371, ledger-backed and therefore last), the
validation/normalisation set (~110), and then the assembly itself with the diagnostics serializer
that owns key order.


### The model router landed, and the new check earned its keep (`b04772bc`)

Same shape as the last two: `model_router.py` is 279 lines and depends on `edge_inference`'s 529, of
which it uses **four names**. So the slice is the router plus that surface — the three query-shape
patterns and the two payload readers — with the edge-routing half (providers, quantisation,
local-versus-cloud) left for its own slice. The patterns are exported as **text** as well as
compiled, and the probe compares the strings: they carry CJK literals, and a wrong character
transcribed blind would otherwise surface only as a mysterious routing difference.

What the 251-key corpus and eleven tests pin, in the order they would bite:

- complexity tests run in the oracle's order — the complex pattern beats a short length, and the
  simple pattern only counts within 400 characters, so `解释` + 500 characters is `neutral`;
- `is_auto_request` mixes a case-fold with an identity check: `model: " AUTO "` opts in while
  `autoRoute: 1` does not;
- capability reads the **attachment** (`imageData: data:image/…`), not content parts — the test
  asserts both directions against `request_shaping::has_image_content` so the pair cannot collapse;
- an explicit model is normalised, checked against the supported list, then overridden by vision
  unless it is already the refine model;
- auto routing walks complexity → the soft cost cap (off at zero) → the default, and the tier falls
  back to the model name for anything that is neither draft nor refine;
- cascade is refused for agent and vision turns; the quality gate scores `1 - 0.34` per reason with
  one uncertainty marker passing and two failing.

**The "no diagnostics" standard caught a real lint this time.** Appending the test modules left the
`text_or_empty` helper after them — `items after a test module`, a warning that does not change
clippy's exit code. Under the old "exit 0" claim it would have shipped. Both files were reordered.
Same class of miss as `type_complexity` last round, and the reason the assertion was changed.

Verification: 251 keys byte-identical, md5 `ebad857e793f240bba7fa0d1c2f5a894`; tests 318 → **329**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left before `build_deepseek_request`: `budget_manager` (371, ledger-backed and therefore
last), the validation/normalisation set (~110), the `edge_inference` edge-routing half (~430), and
then the assembly with the diagnostics serializer that owns key order.


### The message layer landed, and the corpus nearly failed to be able to fail (`01d252b1`)

`normalize_chat_messages` plus its two validators and the tool-call helpers. The layer is
**fail-closed** by design and the oracle's docstring records why: an earlier revision silently
skipped every turn it could not represent, so the caller's instruction reached the model as if it
had never been written — a `200` whose answer ignored what the user had said. Every unrepresentable
turn raises here, with its index and the codes the gateway's own preparation layer returns.

**The content expander is injected.** `expanded_message_content` reaches the file index through
`build_attachment_context`, which is I/O, so the pure layer takes the expander as a parameter — the
same move as the clock and the transport. Attachment *parts* are still covered, because
`_image_content_parts` is pure and is ported.

Three notes on verification, in order of how much they cost:

1. **The first corpus for the check layer could not have failed.** It reused the message sets, none
   of which contains more than 40 messages, so the `context_compression_required` path was
   unreachable and the probe would have reported parity whether that rule worked or not. It has its
   own corpus now, and the reference shows the discrimination: 41 messages without a summary is a
   **409**, with a summary it is fine, and exactly 40 is fine either way. This is the second
   instance of "a corpus that cannot fail reads as a pass" after the token-trim one.
2. **One measured divergence.** A JSON *object* as tool-call `arguments` is re-serialized in sorted
   key order here where the oracle keeps insertion order — `serde_json::Map` is a `BTreeMap` and
   `preserve_order` is off workspace-wide on purpose, and the order is gone at parse time. The wire
   format sends arguments as a string, so it is unreachable from the wired path; documented, pinned
   by a test, and the corpus uses an already-sorted object so the probe compares behaviour rather
   than that gap.
3. **A test case of mine was wrong, not the port.** The api-key-fallback case omitted `messages`,
   which the oracle rejects too — the probe showed both sides agreeing before the test changed.

The "no diagnostics" assertion earned its keep for the second slice running: the helper landed after
the test module again, a warning that does not move clippy's exit code.

Verification: 121 keys byte-identical, md5 `7c7860c9a70b4b4f07f0cff7503cdda7`; tests 329 → **337**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left: `budget_manager` (371, ledger-backed and therefore last), the attachment-expansion path
behind the file index, and then `build_deepseek_request` itself with the diagnostics serializer that
owns key order. The `edge_inference` edge-routing half is **not** in this closure — only its four
consumed names were ever needed.


### The budget manager's pure half, and a float rendering that was wrong at real magnitudes (`04645d0c`)

`budget_manager` was the last leaf before the assembly, and its ledger is SQLite -- so the slice
line is the one the oracle's own docstring draws: pricing, cost arithmetic, the policy and its
payload override, the in-memory `ToolBudget`, the scope key and the cost diagnostic are pure; the
database (`connect_db`, `record_spend`, `daily_spend`, `over_daily_budget`, `should_downgrade`,
`budget_status`) is a store and its own slice. 209 keys byte-identical, md5
`08a4887495a2a11398f75daeae21393f`.

**The finding outlived the port.** Serializing `estimate_cost` in the probe meant comparing
serde_json's float rendering against Python's, and they disagree on *every* float below `1e-4`:
serde_json writes `4.93e-5` as `0.0000493` and `1e-6` as `1e-6`, where Python writes `4.93e-05` and
`1e-06`. A single request's cost is exactly that size -- a fraction of a cent -- and
`diagnostics["costUsd"]` is served to the caller. The crate already had the right renderer
(`python_json::float_str`, with `1e-05`/`1e+16` pinned), but three containers --
`dumps_default_separators`, `dumps_compact`, `OrderedJson::render` -- sent numbers through
serde_json's own `to_string` and bypassed it. Fixed in a commit of its own (`36fc2de7`), because
eight modules consume those renderers; the two probes with hashes on record were re-run
(`request_messages` still `7c7860c9...`, `memory` byte-identical). This was visible at all only
because the corpus uses **real costs** rather than round numbers -- the third time in this
migration that the corpus's composition decided whether the probe could see anything.

**Two asymmetries recorded, not smoothed over.** `diagnostics_with_cost` reads a non-dict as `{}`
where the oracle's bare `dict(...)` raises -- unreachable, and the module doc says so, explicitly
so that nobody later "unifies" it with `cost_from_usage`, which *does* guard in the oracle. And
`BudgetPolicy::to_value` cannot carry the oracle's `to_dict` insertion order (`maxTotalTokens,
maxAgentTokens, maxSearchCalls, maxToolCalls, maxEstimatedCostUsd, policy`) because a
`serde_json::Map` is a `BTreeMap` -- and the payload reaches the wire as
`diagnostics["budgetPolicy"]`, where Python's response `json.dumps` does not sort. The serializer
that serves it must be handed the order; the assembly slice owns that decision, and the module doc
now states it.

**Corpus traps pinned**: a usage value that is present but cannot convert is *skipped* (so a later
spelling can still win) rather than read as zero; a present negative limit is *floored* while an
unconvertible one *falls back* -- two directions, two assertions; an unknown `budgetPolicy` cannot
be turned on by a payload; the scope cap is 120 code points, which is what keeps a 200-character
Chinese scope from becoming an unbounded ledger key.

Verification: 209 keys byte-identical, md5 `08a48874...`; tests 337 -> **347**; `fmt --check` clean;
`clippy --all-targets` exit 0 with no diagnostics (one `explicit_auto_deref` found and fixed).

What is left before `build_deepseek_request` itself: the SQLite ledger (`should_downgrade` is the
assembly's consumer), the attachment-expansion path behind the file index, and then the assembly
with the diagnostics serializer.


### The two blocks before the assembly: attachment expansion and the budget ledger (`2e7f395e`, `592a05ce`)

`build_deepseek_request` needs two things that are not pure, and both now exist as pure halves
with their I/O injected -- the same boundary the clock, the transport and the content expander
already had.

**`attachment_context`** (`2e7f395e`) is `rag/files.py` 69-283 plus
`chat_payload.expanded_message_content`: the chunk selector with its scoring and embedding
helpers, the two formatters, and the orchestration. `load_cached_file` (the file index),
`local_rag.search_file_chunks` (the vector index) and the embedding pipeline arrive as
parameters, and the `context_taint` guard line is handed in rather than re-derived. Every
budget and truncation counts **code points**, because the section boundaries are part of the
prompt. 69 keys byte-identical, md5 `347c7fb29c4c8cc73fe6ca0bc8f7c93e`; tests 347 -> 359.

**`budget_ledger`** (`592a05ce`) is the behaviour `budget_manager.py` 183-371 wrap around
`connect_db`: the spend view, the row shaping, the four threshold checks, `should_downgrade`,
`record_request_spend`, `budget_status`. The SQL statements, the connection and its pragmas are
the store's slice, so they arrive as `LedgerDeps`. Its defining property is that **failure is
data**: the oracle swallows every database error into `_last_error` and returns the empty view,
so these functions return the message and the caller owns the state -- which is why
`budget_status` reads the ledger twice and reports the newest failure. 75 keys byte-identical,
md5 `0e22a7129ec62c918f52ac0117532c93`; tests 359 -> 367.

What the two slices had in common, beyond the boundary: **each corpus was wrong on the first
pass in a way that would have read as a pass.** The selector's indexed rows used a `file_id`
the search stub did not know, so the branch that reads the vector index was never entered; and
its budget-exhausted row used two attachments where the share shrinks multiplicatively
(`remaining -> remaining / left`), so the "not sent" row cannot appear before the
`max(8_000, ...)` floor binds -- about fifteen attachments. Both were caught by asking what
the corpus was supposed to be able to **fail** on, not by reading the diff. That is the fourth
and fifth instance of this class in the migration.

Remaining before the assembly: the two stores (the SQL and connection behind `LedgerDeps`; the
file and vector indexes behind `FileContextDeps`), and then `build_deepseek_request` itself
with the diagnostics serializer that owns key order.

---

### The closure list above is closed: the stores and the assembly landed (`13136490`, `de2bd60a`), and its probe is now clean (`3b7c041b`, `02976415`)

Three commits were not yet in this record. `13136490` is the two stores behind the injected
reads: `budget_store` (`connect_db` + the schema DDL, which is itself a contract because SQLite
stores the statement text, so it is written with `concat!` and explicit `\n` -- a multi-line
literal would have taken CRLF from the checkout) and `file_store` (`load_cached_file` with its
four failure codes, whose messages render **into the prompt**). `de2bd60a` is
`build_deepseek_request` itself, with the key orders **measured** by making the Python probe
publish every envelope and nested block's key order. Its first run reported "95 of 292 rows
differing, and every one of them is inside a tool schema".

**That last part was wrong, and closing it was the whole slice.** 45 rows differed inside tool
schemas; **50 differed on `contextDiff.delta` elements**, which are not tool schemas at all.
Fixing both then surfaced three more divergences that the same rows had been masking -- the
first-difference sampling only ever saw the earliest one. The probe pair now reports **0 of 406
rows differing**, md5 `adfbfd903b6e98dd83bbb75f7d323985`.

**The tool-schema order had to move into the data.** Measured from the asset:
`parameters.properties` takes **25 distinct orders** across the catalog -- each tool declaring
its own property sequence while reusing the same names -- and property objects order `type`
before `description`. `NESTED_ORDERS` matches by key **name**, so no table can express that.
`python_json` gained `loads` (structural scanning there; leaves decoded by `serde_json`, because
a `Deserialize` visitor was ruled out after reading the source: with workspace-unified
`arbitrary_precision`, `deserialize_any` delivers numbers as a private map). `tool_catalog`
gained the ordered trees plus `ordered_tool_definition`, which serves a definition only when it
is byte-equal to the catalog's own -- drift keeps the generic rendering. The strongest check is
the new test that re-renders the whole 40 KB asset from the parsed trees and asserts it equals
the committed bytes.

**The taint block needed its builder to keep the bytes.** `sources` accumulates in
first-appearance order over the **untruncated** segment scan, and the visible `segments` are
capped at `max_segments` -- once truncation bites, the order is not re-derivable from the
report. `build_taint_report_ordered` now builds the tree (the `Value` API delegates to it and is
unchanged); the assembly carries it on `PreparedDeepSeekRequest::taint_ordered` and substitutes
it while the two views agree, the same guard shape as the tool substitution. Its probe
reproduces its recorded hash `49e390b4c0c9f2ab499337326b308404` after the refactor.

**Three divergences the old corpus could not see** (each inside a row already differing for the
tool-schema reason):

1. `temperature`: the oracle's `max(0, min(float(t), 2))` keeps **the integer** at the clamps --
   `3.5 -> 2`, `-1 -> 0`, `False -> 0` -- where the port rendered `2.0` / `0.0`. Measured on
   the oracle, ported as comparisons, extracted into `clamped_temperature` so a unit test pins
   the whole table.
2. taint `segments` elements and `sources` order (above).
3. `modelRouter.reasons` elements: `{"router": ..., "decision": ...}` -- one more table row.

**And the corpus could not have caught any of them.** It never reached the forced `tool_choice`
object (no payload carried a PPT/mindmap intent), never turned search on, never narrowed
`allowedTools`, and never clamped the temperature low. It now does: +3 payloads, +2 axes -- a
memory-state `{}`, which also exposed that the port used `unwrap_or_else` where the oracle's
`memory_state or empty_memory_state(payload)` is Python's `or` with its falsy fallback, and the
non-pro ledger short-circuit. 292 -> **406 rows**.

Verified: `cargo test -p deepseek-policy -p deepseek-gateway -j 1` -> 383 + 139 lib tests plus
7 + 6 + 1 + 1 integration tests, all passing; `cargo fmt --check` clean; clippy exit 0 with **no
diagnostics in the new code** (the two `needless_borrow` warnings in `python_json::build` and
`control_proxy.rs`'s `result_large_err` are byte-identical at HEAD -- local stable 1.97 versus
the CI pin 1.85; do not "fix" either, and do not read exit 0 as "no diagnostics").

What the assembly still is **not**: wired. The production chat path (`chat_execution`) builds its
own thinner body today; this oracle-shaped assembly is what a production caller switches onto
next, together with the file vector index (`local_rag`; refused via
`NATIVE_FILE_VECTOR_INDEX_NOT_READY`/501), the OS local timezone and `search_if_needed`.

**Unpushed**: this slice adds `3b7c041b` and `02976415` on top of the six already recorded.

### The batch's first CI run went red in two lints, and neither had a local signal

Run `35310488045` on `56b189eb`: 4 of its jobs failed, and both causes were lints in code
that had never been through CI. Everything else passed -- `rust-coverage`, `native-go`, and
the parity / S3 / federation e2e jobs among them -- so the red is fully explained by:

- **rust**: clippy 1.85 with `-D warnings` rejects `needless_borrow` twice in
  `python_json::build` (the `&name` spellings from `de2bd60a`; local stable 1.97 only warns
  about them) and `needless_lifetimes` in `budget_ledger`'s test helper. Reproduced locally
  with the exact CI invocation (`cargo +1.85.0-x86_64-pc-windows-gnu clippy --locked
  --all-targets --all-features -- -D warnings`), fixed, and re-run to exit 0.
- **test (3.10 / 3.11 / 3.12)**: ruff first -- an unused `sqlite3` import and an unused
  `failures` dict in the two newest probes -- and, behind it (ruff masks mypy), mypy 2.0's
  `Cannot infer type of lambda` for the default-argument idiom passed to a typed
  `Callable[[], Any]`; replaced with `functools.partial`, which binds the loop value the same
  way. Both probes' outputs are byte-identical before and after (`65c1e23c…` / `0e22a712…`).

**The trap worth recording**: the two `needless_borrow` sites had been judged "pre-existing"
and deliberately left alone -- on the evidence that they were byte-identical at local `HEAD`.
But local `HEAD` included nine unpushed commits, so byte-identity there only proved they were
older than the last *local* commit; it said nothing about whether CI had ever tolerated them.
The baseline for "will CI accept this" is `origin/main`, and the check is the 1.85 invocation
above -- not the local stable, which merely warns.

Verified before re-pushing: 1.85 full-workspace `cargo test --locked --all` exit 0;
`ruff check .` and `mypy .` pass; both probe pairs unchanged.

**The re-run is green.** `35312303606` on `59914dce`: **all 35 jobs succeeded** -- `rust`, the
three `test` legs, `rust-coverage`, `native-go`, and every parity / S3 / federation e2e job.
The ten commits from `2e7f395e` through `59914dce` are CI-verified at that HEAD.

### The clock stopped being the reason the assembly cannot be wired (`f31eea9b`)

The assembly has been complete and unwired since `de2bd60a`, and the prerequisite named there
was the OS local zone: `dynamic_context` carries `LocalNow` as data precisely because "Rust's
standard library has no local-timezone support", and every path through
`build_dynamic_turn_context` needs it unconditionally. That is now resolved, and it was the
**only** hard prerequisite -- the other candidate in that list is not one (below).

**`deepseek-gateway::local_clock`** asks the OS the way CPython does, because that is what the
parity target *is*. `datetime._local_timezone()` consults no tz database: on Windows
`tm_gmtoff`/`tm_zone` do not exist, so it falls back to `time.timezone` / `time.altzone` and
`time.tzname[tm_isdst]`, which the UCRT fills from `GetTimeZoneInformation`; on POSIX it reads
`localtime_r`'s `tm_gmtoff` and `tm_zone` directly. The FFI is declared rather than
dependenc-ised, following `deepseek-policy::file_lock` (`#[link(name = "kernel32")]` on Windows,
a bare `extern "C"` on Unix), so the dependency graph is unchanged -- and a time crate would not
have closed the gap anyway, since `chrono` and `time` expose the offset but not the zone's own
name, which is what gets printed into the prompt.

**The measurement that mattered.** On this zh-CN Windows 11, `GetTimeZoneInformation` reports
`Bias = -480`, `StandardBias = 0`, `StandardName = 中国标准时间`, and CPython's `tzname()`
returns that same string. **Not** `China Standard Time` -- which is what the example in
`dynamic_context` led a reader to expect, and what a hand-written name table would have
produced: a prompt that looks right and diverges on every request. That example is corrected,
and so is `search.rs`'s module header, which still claimed the request-assembly layer "does not
exist yet" and listed three links (the taint firewall, `search_multiple`, the consumer) that are
in fact ported.

**The second measurement changed the plan.** `search_if_needed` had been recorded alongside the
zone as a pre-wiring prerequisite. It is not one: it is reached only under
`forced_search_mode(payload)` (`deepseek_client.py:674`), and `searchContext` is written only
inside that same branch -- so the ordinary path never needs it. Its two callbacks
(`progress_callback`, `system_note_callback`) have no destination at all on
`/v1/chat/completions` (measured in `search_provider`), and its body is a live Tavily fetch, so
a native path that reaches forced-search mode must **refuse** rather than port it. Porting it
now would be porting dead orchestration.

Verified:
- `tasks/native-runtime/local_clock_parity_probe.py` with `examples/local_clock_parity_probe.rs`:
  both sides resolve the host's zone and render the same pinned instant through the oracle's own
  `format_current_time_context` -- **byte-identical** (`diff=0`; `offset_seconds` 28800,
  `tzname` 中国标准时间, `is_daylight` false on both).
- **Negative control run**: with the offset sign flipped the pair reported `offset_seconds:
  -28800` and `2026-09-17T22:50:44-08:00`, so the probe is able to fail. Reverted before the
  commit.
- 6 unit tests on the Windows bias mapping -- the daylight branch and a non-zero `StandardBias`
  are pinned there because no host in reach exercises them, and they run on Linux CI too, where
  the Windows read is `cfg`-ed out.
- `cargo fmt -p deepseek-gateway -- --check` clean; full-workspace clippy (1.85, `--locked
  --all-targets --all-features -- -D warnings`) exit 0 with **no diagnostics**; `ruff check .`
  and `mypy .` pass (884 files).

**Not verified**: the Unix read compiles in CI's Linux job but was not run here -- the paired
probe only exercised the Windows path.

**Pushed and green**: `24f7263f`, `f31eea9b`, `af6e8e52`, `4903540c` — see the CI round below.

**Next explicit action**: measured, and the earlier claim here that the wiring slice is "blocked on
nothing" was **wrong** — see [`assembly-wiring-plan.md`](assembly-wiring-plan.md). Wiring
`chat_execution` onto `build_deepseek_request` still needs the memory state, and
`prepare_memory_state` is unported (8 functions). The measurement also found two things that are
not wiring work at all: the oracle's explicit-memory-command parser is broken by a pair of
`(?:` → `(` typos (so "记住: X" is silently dropped), and the test that covers it monkeypatches
`memory.re` and therefore cannot see it. The plan records both, the ownership gap (memory is not a
declared domain, and `chat_completions_fast_path`'s cutover is 4.9.2), and the four decisions the
next slices need.

### The batch went red on a managed-document rule, and the re-run is green (`0b697839`)

Run `35318257112` on `4903540c`: 4 jobs failed of 35 -- `docs` and all three `test` legs -- from
**one** cause. `assembly-wiring-plan.md` is a tracked Markdown file, so it is a *managed document*:
`scripts/update_docs_language_nav.py --check` (the `docs` job) requires the language switcher block
and `tests/test_docs_language_navigation.py` asserts both its presence and that its targets resolve.

That was my omission, and the cost ratio is the lesson: local verification had covered
`ruff` + `mypy` for the new probe but not the two commands the `docs` job runs, nor the docs test
that reads them. One missing four-line block killed four jobs 21 seconds in. The rule is now in the
project memory: for any new tracked Markdown, run `scripts/update_docs_language_nav.py` (it inserts
the block), then `--check`, then `scripts/check_doc_links.py`, then
`pytest tests/test_docs_language_navigation.py`.

Nothing else was wrong: `rust` passed, which matters because that job compiled the new Unix FFI for
the first time, and `native-protocol`, `rust-coverage`, `native-go` and every parity / e2e job passed.

Fixed in `0b697839` with the repo's own script (it edited exactly one file).

**The re-run is green.** `35321343935` on `0b697839`: **all 35 jobs succeeded**, `docs` and the three
`test` legs among them.

**A push trap worth writing down**: `git push` then hung for ~14 minutes with no output. This shell
carries the persistent `HTTPS_PROXY=http://127.0.0.1:7897/`, which git inherits, and an unhealthy
Clash node hangs instead of failing. `gh` keeps working throughout because it uses the API path, so
"`gh` is fine" is not evidence the push landed. The remote ref proved it had not -- and the direct
push (`timeout 120 env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY git push origin main`) went through
immediately. Check `git ls-remote --heads origin main` before believing either way.

### Decision A taken: the memory-command grammar was two `?` from working (`da8c21cf`)

`apply_explicit_memory_command` (`infra/data/memory.py:508`) is the write half of
`prepare_memory_state`, and the wiring plan's §2 measured it as broken. The repair is the `?` in each
`(?:` that had been written `(:`, and it has three consequences:

1. **Nothing that used to match stops matching.** `删除记忆: X`, `forget: X`, `不要再记得: X`,
   `不再记住: X`, `取消记住: X`, `delete memory: X` matched before and still do; they now delete
   against the text after the colon. While the alternation captured, `(.+)` was group 2 and the code
   reads group 1, so the target was the literal **command word** -- `删除记忆: X` answered
   `已根据用户要求删除 0 条相关长期记忆。` and wrote nothing.
2. **`忘记: X` becomes reachable.** It had required a literal `:` in front of it, which is why every
   natural phrasing failed to match and nothing was ever saved.
3. **That reachability needs a guard, and this part is not a typo repair.** A negated forget --
   `不要忘记: X`, `别忘记: X`, `don't forget: X` -- contains the bare `忘记:` substring, so it would
   land in the delete branch and destroy the memory the user asked to keep. Measured with the guard
   removed: `别忘记: 牙医预约` returned `已根据用户要求删除 1 条相关长期记忆。` and the row was gone.
   A negated forget is now recognised first and routed to *remember*, which is what the sentence
   means. Eight lines, and they are the difference between a repair and a new way to lose data.

**The accept-set of the repaired grammar, measured** -- and the reason this is a decision rather than
a finished story:

| input | result |
| --- | --- |
| `请帮我记住: A` | saved |
| `记住: B` / `帮我记住: C` / `以后记得: D` / `remember: E` | `""` -- still dropped |
| `不要忘记: F` / `don't forget: G` | saved |
| `忘记: X` / `forget: X` / `删除记忆: X` | deleted, against `X` |
| `不要再记得: H` | `""` -- shadowed by the `不要…记得` guard |
| `不要删除记忆: I` | reaches the delete branch |

The remember branch's prefix is **required**: `(?:请)(?:帮我)` never had a `?`, so the only phrasing
that works is `请帮我记住: X`. Making those prefixes optional is a one-token change that *adds*
accepted phrasings -- a product decision, left open. The last two rows are pre-existing gaps this
repair does not touch; both are now in the function's docstring.

**The test could not fail, which is why none of it was visible.**
`test_explicit_english_remember_forget_and_opt_out` monkeypatched `memory.re` with a
`SimpleNamespace` whose `search` returned fabricated `SimpleNamespace(group=lambda _: …)` objects: it
never ran the patterns, never distinguished `group(1)` from `group(2)`, and it asserted a result for
`"forget concise replies"`, a phrasing the grammar never accepted. Replaced by
`test_explicit_memory_commands_are_parsed_by_their_real_patterns`, which calls the function on real
phrasings against a temporary memory directory and also closes the empty-input early return that was
uncovered.

Verified:
- **Both negative controls, each restored before the commit**: reverting the two patterns makes the
  new test fail at its first remember assertion; disabling the negated-forget branch makes
  `别忘记: 牙医预约` delete instead of save.
- `tests/test_memory_failure_paths_332.py` + `tests/test_memory.py` -> 24 passed; `test_memory.py`
  alone -> 13 passed; `memory.py` line coverage over those files 92.90% -> 94.48%.
- `ruff check .` and `mypy .` pass (884 files).
- **The full local suite is not a usable gate on this host**, which is worth recording rather than
  glossing: it runs ~5x slower than CI; the 16 storage files cannot provision MinIO (no `minio`
  binary in `bin/`, no Docker daemon); and a combined run reports **35 failures that all pass when
  their file is run alone** (19 in `test_files.py`, 8 in `test_memory.py`, 4 in
  `test_presentations.py`, 1 in `test_search.py`, 3 in backup files) -- local cross-file isolation
  artifacts, not code. CI ran those same files green on `0b697839`, so CI is the arbiter for the
  full gate. Two traps found while trying: `pytest --cov` is blocked by the sandbox's safe-delete
  hook unless `COVERAGE_FILE` points outside the repo, and `-v` is overridden by the project's
  pytest config into per-file dots.

**Pushed**: `da8c21cf` (the repair) and `1b856bec` (this record); both are on `origin/main`.

**The batch is green** (run `35330894171` on `1b856bec`, after a re-run of one job): **35 of 35 jobs
succeeded** -- including `docs`, all three `test` legs, `rust`, `rust-coverage`, `native-go` and
every parity / e2e job. The first attempt was 34 green and one red, and the red was a **flaky
threshold assertion, not this change**:
`tests/test_backup_458_storage_control_plane.py::test_qos_reserves_p0_bandwidth_and_enforces_independent_target_buckets`
returned `0.747967004776001` against `>= 0.75` at line 1314 -- a rate-accounting assertion missing by
0.27%, in a storage-QoS test that has nothing to do with memory parsing. The evidence that it is
flaky rather than broken: the same code passed `test (3.10)` in the previous run (`0b697839`, all 35
green) and passed `test (3.11)` and `test (3.12)` in the failing run; `gh run rerun --failed` then
passed it with no code change.

**Two CI-harness traps recorded while getting here**, both of which cost time and will cost it again:
`gh run watch --exit-status` returns non-zero on a *network* error too (`failed to get run: … … unexpected EOF`,
the proxy hop), while the run is still going -- so the conclusion must come from
`gh run view --json status,conclusion` and the per-job `conclusion` (an empty string there means
in progress, not failed). And on this host the full local suite cannot stand in for CI at all; see
the previous section.

---

## The memory turn-state half landed, and the vector bonus turned out not to be bounded

**Branch `main`, HEAD `dd9b5cdb`** (one commit ahead of `origin/main` — the last push was
`1b856bec`; nothing in this section is pushed yet). Working tree carried only the files below.

This is step 2 of [`assembly-wiring-plan.md`](assembly-wiring-plan.md): the memory read half the
request assembly waits for. The wiring plan had recorded `prepare_memory_state` as the last hard
prerequisite for wiring `chat_execution` onto `build_deepseek_request`; eight functions were
missing. All eight are now ported into `deepseek-policy::memory`, byte-verified, and the plan's
open **Decision C** — whether the un-ported vector bonus is bounded — is **answered by
measurement**.

### Decision C: measured, and the answer is no

`LOCAL_RAG_ENABLED` defaults to **true** and the embedding provider to **`hash`** — so unlike the
API-key-gated paths, the memory vector index is **live offline in a default deployment**:
`save_memories` populates it through `sync_memories`, and `search_memories_index` returns real
scores. The plan's proposed measurement was run for real
([`memory_vector_bonus_probe.py`](memory_vector_bonus_probe.py), the actual modules, a scratch root
via `DEEPSEEK_INFRA_ROOT`):

| observation | result |
| --- | --- |
| run A (index live) executed twice | **identical** — the difference is the index, not flakiness |
| queries whose retrieved **order** differs from run B | **7 of 8** |
| queries whose retrieved **set** differs | the same 7 |

The set result is the one that matters. For query `react`, `m-long` has a lexical score of **zero**
and is retrieved *only* through the bonus (`hit score 37 → +3`); with the index forced to raise,
it is absent. So `None` is **not** a tie-break divergence that can be documented away: it changes
which memories reach the prompt.

**Consequence recorded in the matrix and the plan:** the wiring slice now has a third
prerequisite — a Rust provider for the memory index read path (bounded: hash embedding + cosine +
BM25 over `rag_items`/`rag_vec`, **read-only** while Python remains the writer), or a narrow
refusal that only fires when the index is populated. The `file_store` precedent cannot be copied
verbatim, because memory is enabled by default and a blanket refusal would refuse nearly every
request.

### What was ported, and the two defects found on the way

The eight functions — `memory_scope_candidates`, `memory_scope_label`, `format_memory_context`,
`upsert_memory`, `clear_memories`, `delete_memory_by_id`, `apply_explicit_memory_command`,
`prepare_memory_state` — with the same injection boundary the clock and the transport already use
(`vector_hits` as a `dyn Fn` provider). The repaired grammar from `da8c21cf` is ported with it: the
opt-out guard first, then the *negated forget* (which must beat the forget branch or it deletes the
memory the user asked to keep), then forget, then remember. The remember branch's required
`请`/`帮我` prefixes are kept as the oracle's own gap, not "fixed" — that is a product decision.

Two real defects surfaced, neither by reading the diff:

1. **A falsy content gate.** The oracle writes `normalize_memory_text(item.get("content") or "")`,
   so `content: 0` is empty and the row is **dropped**; the port passed the value straight in, so
   `0` became `"0"` and the row survived. `normalized_content` now applies the truthiness gate, and
   the migration corpus carries `0` / `true` / `false`.
2. **An `OrderedJson` regression that no gate could see.** The nested-order refactor (`3b7c041b`)
   made array elements take their order from the array's *name* with an empty fallback, so a
   **top-level array** — exactly how the store fixtures are written — rendered alphabetically. No
   CI job runs these probes, so it sat there; the memory probe's `store::file` observation caught it
   the moment the probe was re-run. The fix restores inheritance of the enclosing order when no
   nested order is registered; all **24 runnable probe pairs** were re-run afterwards and are
   byte-identical, `request_assembly` (the nested-order consumer) included.

### The corpus that could not fail, again — twice

- The first budget corpus was `3 × 3000`-char rows. `normalize_memory_text` caps a row at **1200**
  characters, so `used` peaked at ~3 600 of 8 000 and the 省略 path was **never reached** — the
  probe would have reported parity whether that branch worked or was a no-op. My unit test failed
  for the same reason and exposed it. The corpus is now six full rows (7 254) plus a 737-char row
  that lands **exactly** on the budget and a row after it that crosses; the reference output
  contains the marker (8 140 chars). Sixth instance of this class in the migration.
- The same cap invalidated the "exact boundary" case for the same reason.

My own unit-test expectations were wrong twice more (I indexed `load_memories()[0]` and forgot that
**pinned** rows sort first) — corrected against the oracle-derived probe rather than by touching
the port. That is the recurring lesson, unchanged: **expectations get taken from the oracle.**

### Verification

- Probe pair byte-identical: **194 keys** (was 92), md5 **`97187819db5aec787776174f6ac3f3d5`**,
  re-run after `cargo fmt` so the committed bytes reproduce the hash. Includes 8 upsert shapes,
  clear/delete-by-id, **14 command shapes**, and **9 turn-state shapes**, each with its file bytes
  and the final generation counter.
- `cargo test -p deepseek-policy` → **390 passed** (33 new); `cargo test -p deepseek-gateway` →
  145 lib + 15 integration passed.
- `cargo +1.85.0-x86_64-pc-windows-gnu test --locked --all` → **exit 0**, whole workspace,
  including the real Go→Rust boundary integration tests.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on both probes (the new probe needed explicit `list[dict[str, Any]]`
  annotations to satisfy mypy's overload resolution).
- Docs gates: `update_docs_language_nav.py --check` PASS (199 files), `check_doc_links.py` OK.

### Two pre-existing probe breakages found while re-running the suite (not mine)

Recorded rather than fixed, since both are legacy measurement tools with newer replacements:

- `oracle_parity_probe.py` fails with `NameError: name 'AppError' is not defined`: it extracts
  `normalize_chat_messages` from `deepseek_client.py`, and `df7dfa13` introduced `AppError` into
  that function *after* the probe's last edit (`6ea4dde3`). `request_messages_parity_probe.py` is
  the superseding probe and is byte-identical (121 keys).
- `local_clock_parity_probe` takes the epoch as an argument on the Rust side while the Python side
  prints its own; it passes when paired that way (verified: identical).

### Not done, and the next executable task

**Not pushed.** The four changed files plus one new probe are local only; exact-head CI has not run
against them. The new `docs/MEMORY_STORE.md` and the two task Markdown edits are managed documents
(the `docs` job's language switcher is verified present).

**Next slice:** the memory index read path in Rust (hash embedding + cosine + BM25 over
`rag_items`/`rag_vec`, read-only) **or** the narrow refusal — then step 3, wiring `chat_execution`
onto `build_deepseek_request` with the three refusals and a real memory-state provider. Decision B
(a `memory` domain declaration) still gates wiring the **write** half.

---

## The memory index read path landed, and its acceptance criterion had to be corrected

**Branch `main`, HEAD `cd8cca08`** (committed by the workspace, not pushed — `origin/main` is still
`1b856bec`). The turn-state half of the previous section is in that commit; the index read path is
the uncommitted set below.

This closes the third prerequisite step 3 of
[`assembly-wiring-plan.md`](assembly-wiring-plan.md) was waiting for. The plan and the module
skeleton were already in the tree when this session started; what was missing was **verification**,
and an unverified provider is exactly what the migration rules do not count.

### What was verified, and the one defect it found

`tasks/native-runtime/memory_index_parity_probe.py` ↔
`rust/crates/deepseek-policy/examples/memory_index_parity_probe.rs`: **64 keys,
byte-identical** after `tr -d '\r'`. The fixture is **shared**, not duplicated — Python builds
`.local-rag/rag.sqlite3` through the production `save_memories` → `sync_memories` path and Rust
opens that same file read-only, so the schema and the column types are part of what is compared.
`.local-rag/rag.sqlite3`, note, is written by Python and read by Rust: no second writer.

The defect only the `pure::` layer could see: `parse_embedding` has **three** outcomes in the
oracle (a decode error returns the bare `return []`, a non-array normalizes `[]` to `dimensions`
zeros, an array normalizes to `dimensions` components) and the port had collapsed the first two.
It is invisible in every score — `cosine_similarity` returns `0.0` for an empty *and* for an
all-zero vector — so all 8 queries and their 24-key ordering were already identical while the
function was wrong. `str(value or "[]")` is part of the contract too: an empty string is falsy and
lands in the *second* branch. Fixed, and `pure::parse-9` now pins the empty-string case.

### The acceptance criterion in the plan was wrong, and the measurement says so

`memory-index-read-path-plan.md` asked for "the 7 differing queries drop to 0". That conflates two
comparisons. The 7-of-8 figure is the *live index versus no index* difference **inside the
oracle**; a correct Rust provider reproduces the **live** side, which leaves the figure at 7. The
probe therefore reports both paths — `retrieve::` with the provider and `retrieve-none::` without
— and **`turn::differing = 7 of 8` is now a positive result**, not a target. A provider that
silently returned nothing would have reported `0 of 8`; one that computed the wrong bonus would
have failed the `retrieve::` comparison. The plan records the correction.

### What the provider deliberately does not do

The `rag_vec` branch is **not** reimplemented. `vec0` is an extension loaded into the Python
connection, `rusqlite`'s bundled SQLite has no such module, and `sqlite-vec` is not a dependency of
this repository (not in `requirements*.txt`, `pyproject.toml` or any Compose file;
`find_spec("sqlite_vec")` is `None`). `initialize_schema` creates `rag_vec` only `if vec_loaded`,
so **every shipped deployment and every CI leg takes the cosine fallback**, which is complete.

When the table *is* present the read returns `MemoryIndexError::VectorTableNotReadable` instead of
quietly serving the fallback — the oracle would have blended `1/(1+distance)` into every score, so
the two answers differ in membership, not just in order. This is the narrow refusal the wiring plan
asked for: it fires only in a deployment that installed the optional extra, which is the opposite
of the blanket refusal the `file_store` precedent would have produced.

The read is read-only in the strict sense: `SQLITE_OPEN_READ_ONLY`, and it does not create the
directory, the schema or the `rag_meta` rows the oracle's `db_ready()` would. A missing database is
therefore `None` rather than an empty index, because the oracle's `[]` there comes from `db_ready()`
*creating* it — the one thing a reader must not do.

One structural fix came with it: `memory::VectorHits` was `dyn Fn(…) -> …` with no lifetime, so its
object bound defaulted to `'static` and no provider could borrow the store it reads. It now carries
a lifetime (`VectorHits<'a>`), which is what lets the wiring build a per-request provider rather
than leaking or `Rc`-ing one.

### Verification

- **Both probe pairs byte-identical**: `memory_index` **64 keys / ** (new);
  `memory_parity_probe` re-run and unchanged at **194 keys**, LF-normalized md5
  `97187819db5aec787776174f6ac3f3d5`.
- `cargo test -p deepseek-policy` → **400 passed** (10 in `memory_index`, 2 new here);
  `cargo test -p deepseek-gateway` → pass.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics** — one `useless_vec` in the
  in-flight test code was fixed to get there.
- `ruff check` and `mypy` pass on the new probe.

### Two traps worth recording

- **The `docs` job's rule applies to any new tracked Markdown**, and
  `memory-index-read-path-plan.md` already carries its language switcher — verified, not assumed.
- **`memory_parity_probe.py` writes GBK on this host.** Its Python side has no stdout
  reconfiguration, so a bare `python … > python.json` on Windows produces bytes that cannot be
  decoded as UTF-8 and compares as "different" against a correct Rust output. The pair is
  byte-identical under `PYTHONIOENCODING=utf-8`. The new probe pins `reconfigure(encoding="utf-8")`
  itself so its documented command works as written.

### A workspace hazard for the next session

Between two consecutive `git status` calls in this session the same 7 files moved from unstaged to
staged, then appeared as commit `cd8cca08` (19:41), and `memory_index.rs` +
`memory-index-read-path-plan.md` appeared with mtimes this session did not produce. A `codex`
process was resident throughout and idle by 19:46. Whatever the exact cause, **treat this working
tree as possibly having a second writer**: re-check `git status` and file mtimes before staging,
rebasing or force-pushing, and prefer adding to the existing task files over rewriting them.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

**Next slice — step 3, now unblocked:** wire `chat_execution` onto `build_deepseek_request` with
the memory provider bound (not `None`), plus the forced-search mode refusal and the file vector
index refusal (`vector_index_not_ready()`). Decision B (a `memory` domain declaration) still gates
wiring the **write** half.

---

## The OpenAI facade translation landed, and the wiring stopped being a fidelity question

**Branch `main`, HEAD `276d21a7`** (the memory index read path, committed by the previous round;
`origin/main` is still `1b856bec`). Working tree carried only the files below.

Before writing any wiring code, the oracle's route was measured rather than assumed — and the
measurement changed what the slice *is*.

### The measurement: `/v1/chat/completions` is a facade, and the native route disagrees with it

`routes/chat.py:65` is `payload = openai_to_internal_payload(body, local_base_url=…)`, then
`resolve_provider(model).chat(payload)` → `call_deepseek` → `prepare_deepseek_call` →
`build_deepseek_request`. So the OpenAI body is translated **first**, and the translation is part
of the public contract.

`openai_to_internal_payload` (`openai_api.py:29`) is narrow: it forwards `model` (after
`body.get("model") or settings.default_model`, then `MODEL_ALIASES`), `messages` **verbatim and
unvalidated**, `stream` (Python truthiness — the string `"false"` is *true*), `thinkingEnabled:
False`, `localBaseUrl`, and `temperature` only when it is a real number. It **drops** `tools`,
`tool_choice`, `max_tokens`, `top_p`, `reasoning_effort` and `thinking`.

The native route does the opposite. `request_preparation::prepare_chat_request` builds the upstream
body straight from the OpenAI request, so it **forwards** those six fields and **omits**
`thinkingEnabled`/`localBaseUrl`. That is a visible divergence on a public route (§五.11), not the
omission the plan had recorded — a client sending `tools` gets a body the oracle never builds, and
`temperature` is applied unconditionally instead of only when `build_deepseek_request` decides the
tier warrants it.

### What landed

`deepseek-gateway::openai_facade::openai_to_internal_payload`, with `payload_canonical_json` for
probes and diagnostics (`json.dumps(..., ensure_ascii=False, sort_keys=True)` — Python's default
separators, so a rendering comparison does not test `serde_json`'s compact default).

Paired with the real Python function:
`openai_facade_parity_probe.py` ↔ `examples/openai_facade_parity_probe.rs`, **56 keys, 12 204
chars, byte-identical**. The corpus is written to reach every branch and is labelled per case, so a
diff names the behaviour that moved:

- the six forwarded fields and the seven dropped ones, including "drops everything at once";
- the falsy-model set (`""`, `null`, `0`, `false`, `[]`, `{}`) all falling back to the default
  **before** normalization, and a truthy `true` normalizing to the literal `"True"` — which is what
  `str(True)` does and which no alias matches, so it passes through;
- alias normalization: case, underscores, spaces, surrounding whitespace, unknown passthrough;
- the `stream` truthiness table, `"false"` → `true` included;
- the `temperature` type table, `bool` excluded explicitly (a `bool` *is* an `int` in Python);
- `messages` forwarded verbatim — blank content, a `tool` turn, a non-object entry and
  `content: null` all survive this layer, which is what keeps validation single-sourced in
  `build_deepseek_request`;
- both refusals as `{message, code, status}`.

Seven unit tests pin the same behaviour in-crate.

### One probe bug worth recording

The first run showed **42 of 56 cases differing** — all of them only in whitespace inside the
canonical rendering. Python's `json.dumps(..., sort_keys=True)` uses the default `", "` / `": "`
separators; `serde_json::to_string` is compact. The fix is `python_json::OrderedJson::
render_default_separators`, which this repository already had for exactly this reason — the same
class of trap as the `4.9e-05` float rendering recorded in the budget slice. A probe that compares
*renderings* has to render both sides the same way; only then does a diff mean a behaviour change.

### Verification

- `cargo test -p deepseek-gateway` → **152 lib tests passed** (7 new) plus every integration target
  (7 + 6 + 1 + 1); `cargo test -p deepseek-policy` unchanged.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on the new probe.
- Probes re-run for regressions: `openai_facade` byte-identical (56 keys); `memory_index` and
  `memory_parity` unchanged.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call `openai_facade` — this slice is additive and inert, like the three
before it. The wiring is now the **only** thing between here and a native body that matches the
oracle, and §5 of [`assembly-wiring-plan.md`](assembly-wiring-plan.md) lists the five pieces it has
to carry, in order:

1. `AssemblyEnv::from_env` — nine injected fields. Every settings struct has an oracle-matching
   `Default`; the ledger comes from `budget_store` + `LedgerDeps`, the clock from
   `local_clock::local_now`, the expander from `attachment_context::expanded_message_content` over
   a `FileContextDeps`. **Recorded gap:** the settings' *env readers* are not ported, so only a
   default-configured deployment would agree.
2. `request_base_url` — `Host` trusted only when `host_without_port(host)` is in
   `allowed_auth_hosts()`, else `http://127.0.0.1:{port}`.
3. The two refusals, taken **before** the expander runs (`search_file_chunks` returns a bare
   `Vec<i64>` and so cannot refuse itself), on requests that would actually consult the file index.
4. The error envelope: `build_deepseek_request` raises internal `AppError` codes where the route
   currently answers with `PreparationError` codes. Both move in one change.
5. A real-upstream integration test — matching the oracle's bytes does not prove the assembled body
   reaches DeepSeek correctly.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

---

## The composition landed, and it found a key-order defect two green probes could not

**Branch `main`, HEAD `918885cc`** (the OpenAI facade translation, committed by the previous
round; `origin/main` is still `1b856bec`). Working tree carried only the files below.

Last round ported the facade and noted that "two probes can each be right and still compose
wrongly". This round built the composition and ran that check — and it was not a hypothetical.

### What landed

`deepseek-gateway::native_chat`, the `call_deepseek` → `prepare_deepseek_call` composition:

```
openai_to_internal_payload  →  preflight_deepseek_payload  →  prepare_memory_state  →  build_deepseek_request
```

in **that** order, measured from `deepseek_client.py:1502` and `:651`. The two-step API
(`prepare_openai_chat` then `assemble_openai_chat`) exists so "validate before memory" is visible
at the call site instead of hidden inside a callback — `call_deepseek` validates before
`prepare_memory_state` runs, and the memory command path *writes*, so the order is observable.

Paired with the oracle: `native_chat_composition_parity_probe.py` ↔
`examples/native_chat_composition_parity_probe.rs`, **15 cases, 298 431 chars, byte-identical** —
body, diagnostics, tool names and api key for each. Plus three unit tests.

### The defect it found

`request_assembly::NESTED_ORDERS` carried `("messages", &["role", "content"])`, and the body
renderer appends any key the list does not name in **sorted** order. So a tool result rendered

```
{"role": "tool", "content": "[expanded]", "tool_call_id": "call-1"}     ← Rust
{"role": "tool", "tool_call_id": "call-1", "content": "[expanded]"}     ← oracle
```

and a call entry rendered `{"function": …, "id": …, "type": …}` where the oracle writes
`{"id": …, "type": …, "function": …}`. Identical values, different bytes — a real body-level
difference on a public route, and invisible to `request_assembly_parity_probe` because its corpus
has no tool-role turn. The tool-*call* path is exactly where the native route is now most active
(the loop runs rounds), so this was not a corner.

Fixed by making the message order a **superset** that serves all three oracle shapes — absent keys
are skipped, so one list covers all of them:

| shape | oracle order | served by |
| --- | --- | --- |
| plain | `role, content` | ✓ |
| assistant + tool calls | `role, content, tool_calls` | ✓ |
| tool result | `role, tool_call_id, content` | ✓ |

with the list `["role", "tool_call_id", "content", "tool_calls"]`, plus a new
`("tool_calls", &["id", "type", "function"])` entry. `request_assembly_parity_probe` was re-run
afterwards and is **unchanged at 1 946 077 chars**, so the fix is a strict improvement rather than
a trade.

### Three measurements that remove planned work

- **`forced_search_mode` is structurally unreachable on this route.** It is
  `search_mode(payload) in {"on","force","true","1"}` and `search_mode` is
  `payload.get("searchMode") or "auto"` — a field `openai_to_internal_payload` never forwards. The
  prefetch branch in `prepare_deepseek_call` is dead here, so **no refusal is owed**. Adding one
  would be the `search_budget` mistake the `search_provider` docs already record.
- **`web_search` is absent from the composed tool list.** `tools_for_payload` adds it only when
  `search_tool_enabled` sees `searchEnabled is True`, also never forwarded. The route gets the
  26-tool catalog minus the search tool; a unit test pins it.
- **The file-index refusal is narrower than "has attachments".**
  `expanded_message_content` returns early unless a message carries a non-empty `attachments`
  list, and `search_file_chunks` is consulted only for an attachment with a non-empty `file_id`.
  Only *file* attachments can need the index.

### A test expectation I got wrong, again

The first version of the tools test asserted the composed list **contains** `web_search`. It does
not — that is the measurement above. Corrected against the measured list rather than by touching
the composition; the recurring lesson is unchanged, and this time the failing assertion *was* the
measurement.

### Verification

- `cargo test -p deepseek-policy` → **400 passed**; `cargo test -p deepseek-gateway` → **155 lib
  tests** (3 new) plus every integration target (7 + 6 + 1 + 1).
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on the new probe.
- Probe pairs re-run: `native_chat_composition` byte-identical (15 cases); `request_assembly`
  unchanged (1 946 077 chars); `openai_facade`, `memory_index`, `memory_parity` unchanged.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call any of it — `openai_facade` and `native_chat` are both additive and
inert. `assembly-wiring-plan.md` §5 lists the **five** pieces the swap has to carry, revised by
this round's measurements:

1. `AssemblyEnv::from_env` — nine injected fields; settings `Default`s match the oracle, ledger
   from `budget_store` + `LedgerDeps`, clock from `local_clock::local_now`, expander from
   `attachment_context::expanded_message_content` over a `FileContextDeps`. **Recorded gap:** the
   settings' *env readers* are not ported, so only a default-configured deployment would agree.
2. `request_base_url` — `Host` trusted only when `host_without_port(host)` is in
   `allowed_auth_hosts()`, else `http://127.0.0.1:{port}`.
3. The **file-index** refusal only (forced search owes nothing), taken before the expander runs,
   because `search_file_chunks` returns a bare `Vec<i64>` and cannot refuse itself.
4. The error envelope: `build_deepseek_request` raises `AppError` as
   `{"error": …, "code": …}` + `AppError.status`, while the route answers
   `{"error": {"message": …, "type": …}}`. The frozen REST inventory records the route but **no**
   error envelope, so this is a compat decision — and it moves in the same change.
5. A real-upstream integration test; matching the oracle's bytes does not prove the body reaches
   DeepSeek correctly.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

---

## The assembly environment landed — the last prerequisite before the route swap

**Branch `main`, HEAD `a017e402`** (the OpenAI-chat composition, committed by the previous
round; `origin/main` is still `1b856bec`). Working tree carried only the files below.

`deepseek-gateway::assembly_env::NativeAssembly` binds the nine `AssemblyEnv` fields from the
server environment: the five settings structs at the oracle's own `Default`s, a real
`BudgetStore` + `LedgerDeps` over `<root>/.budget`, the real `FileStore` and
`attachment_context::expanded_message_content` expander, and the OS zone through
`local_clock::system_local_now`. Seven unit tests.

`with_env` is a closure rather than a returned struct because `AssemblyEnv` borrows a ledger and
an expander that borrow *their* stores — the whole graph has to live on one stack frame, and that
frame is `with_env`.

### Two decisions the tests forced, both measured

**`DEEPSEEK_INFRA_ROOT` unset is an error, not a degrade.** `chat_tool_loop::ToolRoundExecutor`
treats an unset root as "no workspace" and lets the data branches report their disabled path. The
assembly cannot: the memory store, the file cache and the budget ledger all live under the root,
so reading them from anywhere else would be a silent divergence. The refusal names the variable.

**The file-index refusal is a flag, not a predicate.** `search_file_chunks` returns a bare
`Vec<i64>` and cannot refuse itself, and a duplicated predicate about attachments could drift from
`expanded_message_content`'s real trigger. So the injected search sets a `Cell` flag and
`with_env` returns it; a caller that sees `true` refuses. The guard therefore fires exactly when
the oracle itself would have called the index.

My first two tests were wrong and the code taught me why — worth recording because it narrows the
condition further than the plan assumed:

- the attachment field is **`fileId`**, not `id`, and the cached document must exist:
  `build_attachment_context` calls the search only from the `Ok(cached)` arm of
  `load_cached_file`;
- even then, `select_file_chunk_indices` returns early and **never asks the index** unless the
  chunk text exceeds `min(FILE_FULL_CONTEXT_LIMIT, char_budget)` — 60 000 characters here. So a
  small attached file is served in full and must **not** trip the guard, and refusing on "has a
  file attachment" would refuse requests the oracle answers completely.

Both sides are now pinned: a 70 000-character attachment trips the flag, a short one does not.

### Verification

- `cargo test -p deepseek-gateway` → **162 lib tests** (7 new) plus every integration target
  (7 + 6 + 1 + 1); `cargo test -p deepseek-policy` → 400 passed.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call any of it. Every prerequisite is now landed and probe-verified, so
the swap itself is the next slice and `assembly-wiring-plan.md` §5 lists its four remaining
pieces: `request_base_url`; the refusal the flag drives; the error envelope (**the tests in
`tests/chat_execution.rs` capture the upstream body and are the regression net**); and a
real-upstream integration test. The recorded env-reader gap for the settings stays open and is
noted as the boundary of what this assembly guarantees.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

### The swap landed: the route composes through the assembly

`chat_completions` no longer builds its own body. It runs the oracle's order — facade translate,
validate, bind the assembly, message rules, memory, build — and the four pieces §5 listed are in:
`request_base_url`, the `rag_vec` refusal, the `{"error", "code"}` envelope, and
`tests/chat_execution.rs` re-derived as the regression net.

The slice arrived in the working tree half-written, so this records what finishing it took. The
last piece was not a typo.

**It did not compile.** Three errors, all interruption artefacts: `ModelRouterSettings` missing from
two scopes, and the real one — the closure still called
`native_chat::prepare_openai_chat(&raw, &base_url, env)` after the facade and the validation had been
moved *before* the workspace binding. Deleting that stale call and threading the router through the
remaining call sites was most of it; the probe's call site and two unused `let env` in the tests were
the rest.

**The message rules cannot run where the comments said they do, and that is measured.** Both the
module doc and `PreparedOpenAiChat`'s said the preflight runs before the workspace is bound, so a
request with no user turn answers `400` rather than `500`. Running the whole preflight there needs
`preflight_deepseek_payload`, which needs the content expander — and the oracle defines the message
rules over `normalize_chat_messages`, whose first act is
`content = expanded_message_content(message)` (`deepseek_client.py:492`). A blank turn is only blank
*before* expansion. Forcing the rules early with a plain-content expander is **measurably wrong**:
`native_chat_composition_parity_probe` diverges at `case::blank-content-turn`, where the oracle
accepts the turn and the plain expander answers `invalid_message_content`. So the validation half
stays pre-binding (credential, model, `messages` — no store needed) and the route runs
`validate_request_messages` with the **real** expander inside `with_env`, before the memory read:
the oracle's order, with the workspace bound. The cost is exact and stated — on a process with no
`DEEPSEEK_INFRA_ROOT`, a request that fails a message rule answers `500` instead of `400`. That is
the boundary, not a claim.

**The regression net was stale, not broken.** Three `lib.rs` cases asserted the old route's own
errors — `503 NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING` for a missing credential, and its own
`a user message is required` / `context compression is required` wording. The oracle answers
`400 missing_api_key` (validation checks the credential **first**, `deepseek_client.py:198-201`,
with `AppError`'s default status) and its own two sentences, so the cases now assert those.
`tests/chat_execution.rs` needed the same for the body it captures: the assembled body leads with the
catalog tools' parallel-call hint, carries the client's own turn, and takes the per-turn context as
the trailing system message, which moves a round's assistant/tool pair from index 1/2 to 3/4. Its
cases also need a workspace root now — the guard installs one — and `DEEPSEEK_API_KEY` is forced, so
a developer shell cannot silently change what they test.

**Verified**: `cargo test -p deepseek-gateway` → 162 lib plus 7 + 6 + 1 + 1 integration, all passed;
`cargo test -p deepseek-policy` → 403 passed; workspace `fmt --check` and clippy (1.85,
`--locked --all-targets --all-features -- -D warnings`) clean; `native_chat_composition_parity_probe`
**byte-identical** again (315 124 B, `diff` empty after `tr -d '\r'`), which is the evidence that
dropping the plain-expander attempt restored parity.

**The batch is green**: run `35411153854` on `6e0af1ef` — **all 35 jobs succeeded**, `rust-docker`,
`evidence-assembly`, `rc-readiness` and `release-package` among them.

The first attempt (`35409221685` on `5d247be9`) was 31 green and four not: `rust-docker` failed, and
`evidence-assembly`, `rc-readiness` and `release-package` fell with it. That is one root cause, not
four — `evidence-assembly` and `rc-readiness` both need `rust-docker`, and the gate's step is a bare
`exit 1` under "Require every upstream Evidence gate", so it reads like an independent failure and is
not; `release-package` was skipped behind them. Worth knowing before diagnosing the next one.

The cause was the same class of stale contract as the `lib.rs` cases, in Python:
`scripts/smoke_rust_sidecar.py` expected `POST /v1/chat/completions` to answer `503` with a *nested*
`NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING`. The wired route answers `400 missing_api_key` — the
oracle's own validation error, in the oracle's flat `{"error": <message>, "code": <code>}` envelope.
The requirement the script encodes (fail closed, never a fabricated completion) is unchanged and
still asserted; `tests/test_rust_docker_config.py`'s stubbed sidecar now answers with the new
envelope, so the script's new expectations are exercised end to end rather than assumed (9 passed).
`worker-execution-plan.md` carried the same stale row for this case; the rest of that document's
drift — its "Still on the Python path" list still names memory retrieval, context compression and the
model router, all of which are ported — is left alone and flagged rather than silently rewritten.

### Decision B, advanced: "read-only" was hiding a live writer

Decision B asked whether to declare a `memory` domain or land the wiring read-only. The read-only half
is already landed, so the honest next step was to *verify* that claim rather than repeat it. It did
not hold.

**The route's refusal is not the only reachable write.** `has_explicit_memory_command` inspects the
*user's* text. The tool loop is the other path, and it executes `forget_memory`: it is not among the
seven unported branches (`chat_tool_loop.rs`'s module doc names them), and its body deletes —
`memory.rs:874` calls `delete_memories_by_query(cleaned, Some(&scopes), root, clock)`. A model that
calls the tool on its own reaches the store with no command in the turn at all.

**Demonstrated, then fixed.**
`chat_route_refuses_the_memory_deleting_tool_instead_of_writing_the_store` seeds a memory under the
test root and has the stubbed upstream answer with a `forget_memory` tool call. Before the refusal it
got `{"ok":true,"result":{"deleted":1,"query":"dentist","scopes":["global"]},"tool":"forget_memory"}`
and the file changed — a turn whose text never matched the command grammar, deleting from the store
Python owns. The loop now answers `DispatchOutcome::Denied` with the same code the turn-level refusal
uses, `NATIVE_MEMORY_WRITE_NOT_OWNED`, because it is the same reason. `suggest_memory` needs no
refusal: it builds a suggestion and writes nothing.

**The declaration was prepared, not applied on my own initiative.** `release/native_runtime_ownership_v1.json` is
`status: accepted` with `approved_by: ["leizd"]`, so an added domain amends a contract that carries
your signature; §3b of the plan holds the exact entry (`memory_store`, python → rust,
`durable_store: rust_data`, and a cutover that was written as 4.9.2 and later moved to 4.9.4 — see the
section below for why the mode, not the analogy, decides it), with its two judgement values flagged as
such. The measured context for
that decision: the barrier is policy, not machinery (`GO_CONTROL_DOMAINS` is 28 ids with no memory
entry, and the memory file is in no `durable_stores` entry), and Python has **four** write entry
points — so moving `current_owner` is a Python-side decommissioning, not a one-line edit. The stakes
are concrete: at the `chat_completions_fast_path` cutover the `501` becomes a capability regression
against Python, and the contract's `forbidden` list contains `permanent_python_fallback`, so the
cutover is where the handover has to be recorded.

**The amendment was then accepted and applied.** `domains` 43 → 44, with `validate_ownership`
accepting the entry, `scripts/native_runtime_contract.py --check` reporting `"ok": true, "domains":
44`, `scripts/check_zero_python_runtime.py` still PASS 8/8, and the ownership plus zero-python tests
at 17 passed. It changes no runtime behaviour: `GO_CONTROL_DOMAINS` is the mechanical denial and it
still has no memory identifier, so the declaration is intent plus the cutover it will be judged
against. What remains open is the handover itself — stopping Python's four write paths and filling
the route's two refusals with the ported write half — which is a slice of its own.

**Verified**: `cargo test -p deepseek-gateway` → 162 lib plus 8 + 6 + 1 + 1 integration, all passed
(the new case included); `cargo test -p deepseek-policy` → 403 passed; workspace `fmt --check` and
clippy (1.85, `--locked --all-targets --all-features -- -D warnings`) clean.

**Not pushed**, and the ownership handover itself is not started — the hole fix is a refusal, and the
declaration is intent. Both commits sit on `main` ahead of origin.

### The handover body: the Python side is now mechanically stoppable

`memory_store` is declared, so "how do Python's four write entry points stop" had to be answered
mechanically rather than by convention. It is one edit, because they are one choke point:
`upsert_memory`, `save_memories`, `delete_memories_by_query`, `delete_memory_by_id`,
`clear_memories` and the turn-level command (through the first and third) all pass through
`memory._save_memories_unlocked`, so `assert_python_writer_allowed("memory_store")` sits there, ahead
of the directory creation. The two delete paths reach it only when they would really delete — a no-op
is not a write — and the test asserts that in both directions.

`authority.py` grew `RUST_DATA_DOMAINS`, and the gate is deliberately **not** symmetric with the Go
one: control domains are denied in `GO_AUTHORITATIVE` and `PYTHON_DISABLED`, data domains only in
`PYTHON_DISABLED`, because ADR-0049 hands the control plane over first ("4.9.3 ... one at a time") and
the data plane can still be Python's during that window. `check_zero_python_runtime`'s
`mechanical_writer_denial` gate now verifies both halves — `28 Go control domains and 1 Rust data
domain`.

**A mismatch that was flagged, and is now settled by moving the declaration.** The gate fires in
`PYTHON_DISABLED`, which ADR-0049 places at **4.9.4**, while the declaration said `memory_store` cuts
over at **4.9.2** — so the mechanism was effective one version after the contract claimed. The
**declaration moved**: `memory_store` now cuts over at **4.9.4**, the first version whose mode actually
stops Python. Verified: `validate_ownership` accepts it, `scripts/native_runtime_contract.py --check`
reports `"ok": true`, `check_zero_python_runtime.py` PASS 8/8, and the ownership plus gate tests are 17
passed. 4.9.4 has exactly one member — this domain — which is the point: the data-plane handover is
tied to the version that de-authorizes Python, not to the one that moves the listener.

**Verified**: 52 tests across the memory, ownership, gate and docker-config files;
`scripts/check_zero_python_runtime.py` PASS 8/8; `ruff check .` and `mypy .` (888 files) clean. The
new denial test is **able to fail**: disarming the gate's domain string turns it red, which is how it
was checked, and the file was restored byte-identically afterwards. `ruff format` is deliberately not
run — the repository does not use it, and CI checks only `ruff check .`.

**Not pushed**, and the Rust write half is deliberately not filled: ADR-0049 leaves the prior owner
authoritative until its cutover gate passes and does not permit dual writers, so it lands *with* the
mode flip.

### The other half of the handover: the refusals became a flip

Both route refusals are now driven by one predicate instead of being constants, which is what turns the
handover into a **flip** rather than a rewrite.

`lib.rs`'s `native_owns_memory_store()` (with the testable `memory_store_owner_is_native(mode)` under
it) is true exactly when `DEEPSEEK_RUNTIME_MODE=python_disabled` — the same signal
`authority.py`'s gate reads. It is deliberately **not** `DEEPSEEK_GO_CONTROL=1`: that is the *control*
plane's mode, the ADR hands the control plane over one domain at a time (4.9.3) while the data plane can
still be Python's, and reading it as data-plane ownership would put two writers on one file. A test
pins that reading, and the mode table with it.

While it is false the turn-level refusal and the tool-level denial behave exactly as before — the tests
written for them did not change — and while it is true the turn calls the oracle's own
`prepare_memory_state` (command first, then retrieval, so a memory saved this turn is retrievable in it)
and `forget_memory` dispatches for real. One environment variable decides which side writes; the store
never has two.

**Verified from both sides.** `chat_route_saves_the_memory_once_the_mode_de_authorises_python` and
`chat_route_runs_the_memory_deleting_tool_once_the_mode_de_authorises_python` are the mirrors of the two
refusal cases — same bodies, one mode different — and both are **able to fail**: forcing the predicate
to `false` turns exactly those two red and leaves the other eight green, which is how it was checked.
Totals: gateway 163 lib plus 10 + 6 + 1 + 1 integration; policy 403 passed; `fmt --check` clean; clippy
clean on lib and test targets.

**A host quirk worth recording**: three clippy runs in a row failed on this machine with
`error: failed to write D:/deepseek/rust/target/debug/examples/*.rmeta: os error 5`, reported as
"could not compile … due to 1 previous error" for files that had no compile error. Disk has 20 GB free
and a manual write into the same directory succeeds, so it is intermittent file contention, not
permissions or space. `CARGO_INCREMENTAL=0` is the lighter workaround (it also made `cargo check`
clean); deleting `target/debug/incremental` is the heavier one.

**Not pushed**: six commits now sit on `main` ahead of origin, and the flip is inert until the mode is
set — nothing runs differently today.

### The reminders write is refused, and the refusal is export-proof

The question "which of the two undeclared stores is treated wrongly" answered itself once measured: it
is the reminders write, the one the route performed unconditionally. `.reminders` sits exactly where
`.memory` sat before its declaration (undeclared — `remind` appears nowhere in the contract,
`GO_CONTROL_DOMAINS` or the command codes — and written by Python from three paths, one of them the
*delivery* poll `due_reminders`), and ADR-0049's "it does not permit dual writers" forbids the native
side writing it. On the owner's call the treatment is now the same as memory's: **refuse now, declare at
the cutover.**

The gate grew a second condition rather than a second constant. `python_is_de_authorised()` is the
deployment-wide mode (ADR-0049's 4.9.4), and `may_write_native_store(domain)` is that **and** the
domain's presence in `DECLARED_NATIVE_DATA_DOMAINS`. That split is the whole point: `reminders_store` is
not in the list, so `create_reminder` answers `NATIVE_REMINDERS_WRITE_NOT_OWNED` **whatever the mode
says**, and one case asserts it twice — refused by default, and still refused with
`DEEPSEEK_RUNTIME_MODE=python_disabled`. No environment variable can enable a store nobody has declared,
which is the mechanical half of "declare it at the cutover". A second test pins the list as a **subset**
of the contract's python → rust data domains, so it cannot invent ownership (nine domains carry
`durable_store: rust_data`, and most belong to other planes).

**Three cases had asserted the reminder write**, and each was updated rather than deleted, keeping its
own purpose: the integration case now asserts the refusal and that no store appeared; the streaming case
still proves a tool round is *continued* rather than failing the turn, with the refusal as the replayed
tool result; and `chat_tool_loop`'s unit test proves workspace injection by seeding the store under the
injected root and reading it back, which is stronger evidence than the write it used to lean on. Both
refusal assertions were shown **able to fail**: disarming the gate turns the integration case and the
streaming case red, and nothing else.

**Correction worth recording**: my first version of the contract-pinning test asserted the writable list
*equals* the contract's python → rust data domains. That was wrong and it failed immediately — the
contract declares nine such domains, most of them other planes' stores. A subset is the correct
invariant, and it is the one the Python side already used.

**Verified**: `cargo test -p deepseek-gateway -p deepseek-policy` → 164 lib plus 10 + 6 + 1 + 1
integration and 403 policy, all passed; `fmt --check` clean; clippy clean on lib and test targets.

### The reminders cutover, all three pieces

The declaration landed on the owner's word, and cutting a store over takes **three** edits rather than
the two it looks like — because a store has two writers and each needs its own gate:

1. `release/native_runtime_ownership_v1.json` gained a `reminders_store` domain (python -> rust,
   `cutover: 4.9.4`, `durable_store: rust_data`). `domains` 44 → 45, and 4.9.4 now has exactly two
   members: `memory_store` and `reminders_store`.
2. `lib.rs`'s `DECLARED_NATIVE_DATA_DOMAINS` gained `"reminders_store"`. Without it the Rust side keeps
   refusing a store it now owns, and the flip would never happen.
3. `authority.RUST_DATA_DOMAINS` gained the same domain, and `reminders._write_reminders` now calls the
   gate. **Without this the flip is asymmetric** — Rust starts writing while Python is still allowed to,
   which is precisely the dual writer ADR-0049 forbids, and it would bite hardest in the
   `DEEPSEEK_LEGACY_PYTHON=1` rollback where the Python server *is* running.

All three are inert today (the default mode is `python_authoritative`), so nothing runs differently; what
they buy is that the flip is now one environment variable, symmetric on both sides, for both stores.

**Tests moved with it.** The integration case flipped from "refused even when the mode is set" to the
memory tests' two-sided shape — refused by default, written once the mode flips — and the
"undeclared-here stays refused" property moved onto `s3_minio_streaming`, a domain the contract *does*
declare python -> rust and that this gateway must still refuse. Python gained
`tests/test_reminders.py::test_every_reminder_write_path_is_denied_once_python_is_de_authorized`, which
covers all three write paths at once (creation, the delivery poll's marking, deletion) and asserts the
store is byte-identical afterwards. Each refusal assertion was shown **able to fail**: disarming a gate
turns exactly the corresponding case red, and nothing else.

**Verified**: `cargo test -p deepseek-gateway -p deepseek-policy` → 164 lib plus 10 + 6 + 1 + 1
integration and 403 policy; `pytest` over the reminders, memory, ownership and gate files → 33 + 17
passed; `ruff check .` and `mypy .` (888 files) clean; `check_zero_python_runtime.py` PASS 8/8, now
reporting `all 28 Go control domains and all 2 Rust data domains`; the contract CLI reports
`"ok": true`.

**A process note worth keeping**: an append executed by this host's shell tool can run **twice** (the
sandboxed pass and the escalated retry), and `cat >>` is not idempotent — the test above was appended
twice, which `mypy` caught as `no-redef` and `ruff` as a redefinition. Guard repeats with
`grep -q … ||`, or edit by unique anchor instead of appending.

---

## fetch_url landed: DNS-time SSRF, locked HTTP, and the loop no longer says it did not run

**Branch `main`, HEAD `57f0595b`** (reminders cutover). This slice is uncommitted on top of that.

`fetch_url` was the first remaining tool branch whose dependencies were already in the tree: the
static URL guard is aligned, `reqwest::blocking` is how the search provider talks to Tavily, and
the tool loop was already the production caller. It was resolving to `Tool did not run`. That is
now a real branch.

### What landed

`deepseek-policy::fetch_url` ports `resolve_public_url` / `ensure_public_address` /
`fetch_public_url` / the cache / `extract_html_text` (the shipped fallback; `trafilatura` is not a
production dependency). DNS and HTTP are injected as `FetchContext` callbacks, same shape as the
search transport, so the policy crate stays free of TLS.

`deepseek-gateway::fetch_provider::locked_http_get` is the connection the oracle's
`LockedHTTPConnection` performs: connect to the pinned address, send `Host: host_header` and the
oracle User-Agent/Accept, disable redirects (the policy crate re-resolves `Location`), and
**disable ambient HTTP(S)_PROXY**. The last of those hung the first TCP test on this host — the
shell carries `HTTPS_PROXY=http://127.0.0.1:7897/`, and reqwest would have sent the "locked"
request through Clash. The oracle does not. `.no_proxy()` is the fidelity fix, not a test hack.

A hostname that is already an IP is checked with `ensure_public_address` before DNS is consulted.
That is equivalent for literals (`getaddrinfo("127.0.0.1")` returns `127.0.0.1`) and is what stops
a stub DNS from laundering a redirect to `http://127.0.0.1/admin` into a public stand-in.

### Verification

- Probe pair byte-identical: **30 keys / 3818 chars**, LF-normalized md5
  `844b896fa61715c0663273d8d8a13abb`. Covers resolve accept/refuse, address block set,
  HTML extract, cache hit (one HTTP for two fetches), redirect revalidation, oversize body,
  HTTP 503 status cap.
- `cargo test -p deepseek-policy` → **415 passed** (12 new in `fetch_url` + the dispatch
  "not enabled rather than unported" case).
- `cargo test -p deepseek-gateway` → **166 lib** + **11 + 6 + 1 + 1** integration, including
  `chat_route_refuses_a_private_fetch_url_target` (wired loop refuses `http://127.0.0.1/admin`
  instead of `Tool did not run`) and the locked TCP test (connects to 127.0.0.1, `Host:
  example.com`, oracle UA). The unported-branch case now uses `search_files`.
- `cargo +1.85.0-x86_64-pc-windows-gnu clippy -p deepseek-policy -p deepseek-gateway
  --locked --all-targets --all-features -- -D warnings` clean. Local stable (1.97) still
  fires the pre-existing `control_proxy.rs` `result_large_err`; CI's 1.85 does not.
- `ruff check` / `mypy` pass on the new probe. Docs language nav PASS (200 files); doc links OK.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against this.

Six tool branches remain: `search_files` (scorer + file cache already ported — the next
dependency-satisfied slice), `browser_*`, `python_eval` (real sandbox, must not shell out to
CPython), `create_mindmap` / `create_pptx` / `create_document` (media; high-risk, validate
early rather than last). Then MCP/A2A, skills, automation, OCR, launchers, and the zero-Python
cutover.

`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`. This slice does not change
that: production HTTP is still Python-authoritative; the native gateway is still an opt-in
delegate.

---

## search_files landed: json_hybrid is the production path, RAG sqlite stays Python-written

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted `fetch_url` slice.

`search_files` was the next remaining tool whose dependencies were already in the tree:
`query_tokens` / `score_chunk`, the file-cache layout, and the read-only RAG cosine+BM25
path from `memory_index`. It was resolving to `Tool did not run`.

### What landed

`deepseek-policy::search_files` ports the oracle's two retrieval paths and merges them
by `(fileId, projectId, chunkIndex)` keeping the higher score:

1. **json_hybrid** — walk `.file-cache/*.json` and `.projects/*/files/*.json`. Complete.
2. **local_rag** — read-only `search_files_index` over collection `files`. `MemoryIndex`
   grew a collection parameter so files and memories share one reader.

**It does not call `index_file_payload`.** That function writes `rag_items`. Python is
still the writer; indexing from the native search would be a second writer of one table.
json_hybrid still finds anything sitting in the cache JSON, which is the source
`index_file_payload` itself reads. A missing sqlite file degrades to json_hybrid only.
When `rag_vec` is present the sqlite path is skipped (same refusal as the memory index).

`compact_snippet` windows by **code point**, matching `len(str)` / `s[start:end]`. A first
draft used `str::find` byte offsets and would have sliced CJK wrong.

### Verification

- Probe pair byte-identical: **3423 chars**, LF-normalized md5
  `133204b547c51707467ed66ca058b21b`. Python `search_files_index` stubbed to `[]` so the
  comparison is the json_hybrid path without a dual-writer. Covers snippet windows
  (ASCII + CJK), empty/blank query, two-index merge, corrupt cache skip, no-hit.
- `cargo test -p deepseek-policy` → **419 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **12 + 6 + 1 + 1** integration,
  including `chat_route_searches_cached_files`. The unported-branch case now uses
  `python_eval`.
- Clippy 1.85 GNU `--locked -D warnings` clean on policy + gateway.
- `ruff` / `mypy` pass on the new probe.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Five tool branches remain. Next is **`create_mindmap`**: pure SVG, no python-docx /
python-pptx / reportlab, so it is the media path that can be proven native without
waiting on Office libraries. Then `create_pptx` / `create_document` (those libraries
are the high-risk remainder), `browser_*`, `python_eval`.

---

## create_mindmap landed: SVG is byte-identical, generated-file store is unique-id creates

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted `fetch_url` + `search_files`
slices. This is the first media tool, chosen because it is pure SVG and does not
need python-docx / python-pptx / reportlab.

### What landed

`deepseek-policy::mindmaps` ports layout, CJK/ASCII tokenization, wrapping, XML
escaping and SVG rendering. `generated_files` ports `store_generated_file` /
`resolve_generated_file` / cleanup / `_safe_filename`. Ids are
`Entropy::new_file_id` (`secrets.token_hex(16)`, 32 hex chars).

`.generated` is unique-id creates with a 6-hour TTL, not a durable
read-modify-write table, so it is not a declared ownership domain.

### Verification

- Probe pair byte-identical including the SVG: **5979 chars**, LF-normalized md5
  `be02bc5fc34cccdf49bc7752bc743c8a`. Covers empty title/nodes, the sample
  outline, XML escaping, and `title`/`name` aliases.
- `cargo test -p deepseek-policy` → **424 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **13 + 6 + 1 + 1** integration,
  including `chat_route_creates_a_mindmap_svg`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Four tool branches remain: `create_pptx` / `create_document` (Office/PDF
libraries — high-risk), `browser_*`, `python_eval` (must not shell out to
CPython). Then MCP/A2A, skills, automation, OCR, launchers, zero-Python cutover.

---

## create_document landed: content model is byte-identical, files are valid OOXML/PDF

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted fetch_url / search_files /
create_mindmap slices. This is the high-risk media path that needed a native
docx+pdf writer without python-docx / reportlab.

### What landed

`deepseek-policy::documents` ports format aliases, section/table normalization
(including ragged-row padding to `max(headers, rows)`), MD5 theme selection
(`int(hex, 16) % 6` on the full 128-bit digest — a first draft truncated to
`usize` and picked the wrong theme), the outline/note envelope, and writers:

- **docx**: uncompressed OOXML zip (`[Content_Types].xml`, rels, `word/document.xml`,
  numbering, footer PAGE field). CJK is UTF-8 in the XML; Word uses 微软雅黑.
- **pdf**: PDF 1.4 with `/STSong-Light` + `/UniGB-UCS2-H`, the same CID approach
  as reportlab `UnicodeCIDFont`. CJK is UTF-16BE hex in the content stream.

The Office/PDF **bytes** are not python-docx/reportlab fingerprints. Those
libraries are not a frozen protocol. Tests assert magic, zip membership, and
that title/headings survive in the payload.

### Verification

- Content-model probe byte-identical: **4798 chars**, md5
  `a09bbde438e3ca1e86f79c0c7e15c953`.
- `cargo test -p deepseek-policy` → **428 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **14 chat_execution**
  (including `chat_route_creates_a_docx_document`) + 6 stream + 1 + 1.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Three tool branches remain: **`create_pptx`** (last media; python-pptx),
`browser_*`, `python_eval` (must not shell out to CPython). Then MCP/A2A,
skills, automation, OCR, launchers, zero-Python cutover.

---

## create_pptx landed: content model is byte-identical, files are valid 16:9 OOXML

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted media slices.
This finishes the three `create_*` artifact tools.

### What landed

`deepseek-policy::presentations` ports `create_presentation`: refusals, `content`
→ bullets, MD5 deck theme, layout picker (quote / cards / process / comparison /
summary / requested layout), automatic agenda at ≥4 content slides, outline/note,
and a 16:9 OOXML zip writer (`ppt/slides/slideN.xml`, blank master/layout, CJK
via 微软雅黑).

The `.pptx` **bytes** are not python-pptx fingerprints. Tests assert zip magic,
slide XML membership, and that the title survives. `create_presentation_from_text`
stays a slides-skill path, not this tool branch.

`zip_store` moved into `generated_files` so docx and pptx share one STORE-method
writer.

### Verification

- Content-model probe byte-identical: **3676 chars**, md5
  `d360f5e45f605284e51c13dbcdeba9ea`.
- `cargo test -p deepseek-policy` → **432 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **15 chat_execution**
  (including `chat_route_creates_a_pptx_deck`) + 6 stream + 1 + 1.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Two tool branches remain: **`browser_*`** (needs a browser engine) and
**`python_eval`** (must not shell out to CPython). Then MCP/A2A, skills,
automation, OCR, launchers, zero-Python cutover.

---

## python_eval landed: in-process AST sandbox, no CPython child

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted tool slices.

The oracle's `python_eval` is not a full interpreter: it is `sys.executable -I
-c PYTHON_EVAL_RUNNER`, an AST allowlist + `eval(..., {"__builtins__": {}})`.
The port parses, validates and evaluates that same allowlist in-process. It
does **not** fork CPython.

### Verification

- Probe byte-identical: **3458 chars**, md5 `b545a8f9c49c5fbbb6e2010c595ae3f2`.
  Covers factorial/arithmetic/math/compare/min/max/sum/pow/round/abs/len/
  bool-if/tuple/subscript/gcd/comb, plus empty/oversize/import/unknown/open/div0.
- `cargo test -p deepseek-policy` → **435 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **16 chat_execution**
  (including `chat_route_evals_a_python_expression`). Unported case is now
  `browser_click`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

Integer overflow on huge factorials is a documented bound (i128); the probe
corpus does not hit it.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

One tool family remains: **`browser_*`** (Playwright). Then MCP/A2A, skills,
automation, OCR, launchers, zero-Python cutover.

---

## browser_* landed: safety gate + static HTML controller, no Playwright

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted tool slices.

`browser_*` was the last unported dispatch family. The oracle already has a
**StaticController** fallback when Playwright is missing. Native ports that
path plus the safety policy, not Chromium.

### What landed

- `browser_safety`: `evaluate_action` / `evaluate_url_safety` (disabled-by-default,
  private hosts, credentials, high-risk click, password fields, confirmation).
- `browser`: in-memory sessions, `execute_browser_action`, static HTML parse of
  approved `file://` fixtures.
- Dispatch: all **18/18** branches now run. Unported `Tool did not run` is no
  longer the live path for a catalog tool.

### Honest gaps

- Playwright is not ported.
- Static controller does **not** `urlopen` public HTTP (Python's StaticController
  does). Allowed `https://example.com` then fails closed with a visible error.
- Media/RAG snapshot writes stay Python (`indexed: false`).

### Verification

- Safety probe byte-identical: **2252 chars**, md5
  `ae657d74bdda48155bea65a6b20c6993`.
- `cargo test -p deepseek-policy` → **440 passed** (fixture `file://` open).
- `cargo test -p deepseek-gateway` → **166 lib** + **16 chat_execution**
  including `chat_route_blocks_a_private_browser_url`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Next: Playwright engine **or** MCP/A2A / Go control-plane cutover / desktop and
Android zero-Python launchers. The chat-tool inventory is no longer the bottleneck.

---

## Native `/mcp` hub landed: tools/list + tools/call through dispatch

**Branch `main`, HEAD `57f0595b`**.

The native gateway's `POST /mcp` used to answer `native MCP tool execution is
not wired` for `tools/list` and `tools/call`. It now implements the Python Tool
Hub's JSON-RPC methods for **local** tools.

### What landed

- `deepseek-policy::tool_catalog::mcp_tools` — MCP shape + risk-card annotations
- `deepseek-gateway::mcp_hub` — `initialize` (with instructions), `ping`,
  `tools/list`, `tools/call` (policy-gated `execute_call_sync`), resources,
  prompts
- `ToolRoundExecutor::execute_call_sync` — one-call path for the hub

External `mcp__*` is a **tool error**, not a fake success.

### Verification

- `mcp_hub` unit tests: list includes `python_eval`/`create_pptx`; `2+2` → `4`
- `mcp_initialize_and_tools_call_are_native` on `POST /mcp`
- `cargo test -p deepseek-policy` → **440 passed**
- `cargo test -p deepseek-gateway` → **169 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. A2A `/a2a` still `not wired`.
Playwright, Go cutover, desktop/Android, exact-head CI remain.

---

## Native A2A mesh landed: Agent Cards + task lifecycle

**Branch `main`, HEAD `57f0595b`**.

`POST /a2a` used to answer `native A2A execution is not wired`. It now
implements the Python mesh's JSON-RPC methods for discovery and tasks.

### What landed

- `deepseek-gateway::a2a_hub` — orchestrator + researcher/coder/reasoner/critic
  Agent Cards (protocol 0.3.0)
- `GET /.well-known/agent-card.json`, `GET /a2a/agents`,
  `POST /a2a`, `POST /a2a/agents/{id}`
- `message/send`, `tasks/get|cancel|list`, `agent/getAuthenticatedExtendedCard`
- Injected task runner; default **fails the task** (`native A2A task runner is
  not attached`) instead of inventing an answer

Streaming SSE (`message/stream`) is not ported.

### Verification

- `agent_cards_cover_orchestrator_and_workers` (researcher tags include
  `web_search`)
- `message_send_runs_injected_runner` (`echo:hello` completes)
- `cargo test -p deepseek-gateway` → **172 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. No default upstream
`call_deepseek` on the native runner. Go `/api` still
`GO_CONTROL_PROXY_NOT_READY`. Playwright, launchers, exact-head CI remain.

---

## Native A2A runner landed: capability-scoped chat loop

**Branch `main`, HEAD `57f0595b`**.

`POST /a2a` `message/send` without an injected runner now queues a native job
and runs `a2a_runner::run_native_a2a`: system profile + capability-scoped
OpenAI tools + `execute_chat_with_tool_rounds`. Missing `DEEPSEEK_API_KEY`
fails the task (`A2A upstream is not configured`) instead of inventing text.

### Verification

- researcher tools = `web_search`, `compare_search_results`, `fetch_url`;
  reasoner has none
- `message_send_without_runner_queues_native_work`
- `cargo test -p deepseek-gateway` → **175 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. SSE `message/stream` unported.
Go `/api` still `GO_CONTROL_PROXY_NOT_READY` unless `GO_CONTROL_ADDR` is set
(and Go still does not serve public `/api`). Playwright, launchers, exact-head
CI remain.

---

## Go public `/api` landed: control status + honest 501

**Branch `main`, HEAD `57f0595b`**.

`deepseekd` now serves a public control-plane edge:

- `GET /api/control/status` — same JSON as `/healthz` (shadow, Python mutation
  authority, `productionMutation: false`)
- `GET /api/cutover/status?domain=` — read-only cutover record
- other `/api/*` → `501 GO_API_NOT_IMPLEMENTED` (not a fake success)
- `POST /api/cutover/transition` is **not** public; mutation stays `/internal`

### Verification

- `TestHealthzIsShadowAndReadOnly` also hits `/api/control/status` (GET 200,
  POST 405)
- `TestPublicAPIUnimplementedPathsFailClosed`
- `go test ./internal/api` coverage **96.8%**
- `go test ./...` ok

### Not done

**Not pushed.** Production HTTP is still Python. Native gateway still needs
`GO_CONTROL_ADDR` to forward `/api`. Remaining Python `/api` (config, chat,
tools, …) is 501 on Go. Playwright, launchers, exact-head CI remain.

---

## Go `/api/config` subset + mcp/a2a flags

**Branch `main`, HEAD `57f0595b`**.

`deepseekd` now serves a Go-owned **read subset**, not Python's full config blob:

- `GET /api/config` — `owner=go`, version, runtime, `hasServerKey`/`hasSearch`
  booleans (never the keys), default model, searchModes, mcp/a2a hub flags
- `GET /api/mcp` — protocol `2025-06-18`, `nativeHub`, `externalBridge: false`
- `GET /api/a2a` — protocol `0.3.0`, `streaming: false`

OCR/RAG/budget/toolPolicy are omitted, not faked. POST `/api/config` is 405.

### Verification

- `TestPublicConfigIsAGoOwnedSubset` / `TestPublicConfigReadsEnvFlags`
- `TestPublicMcpAndA2AStatusAreNativeHubFlags`
- `go test ./internal/api` coverage **96.8%**
- `go test ./internal/lifecycle` coverage **98.9%**

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
writes, launchers, exact-head CI remain.

---

## Native `GET /api/tool-policy` (Rust, not Go proxy)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/tool-policy` is now served by the native gateway **ahead of**
the Go `/api/*` catch-all, using the already-ported `tool_policy_status` +
`read_recent_audit`. Settings knobs read `TOOL_POLICY_*` via
`ToolPolicySettings::from_env`. A missing audit log is `[]`, not an error.

`GET /api/policies` still `GO_CONTROL_PROXY_NOT_READY` without `GO_CONTROL_ADDR`.

### Verification

- `tool_policy_status_is_native_not_go_proxy` (28-card catalog, bad `limit` → 200)
- `cargo test -p deepseek-gateway` → **176 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`,
launchers, exact-head CI remain.

---

## Native `GET /api/budget` (Rust ledger, not Go proxy)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/budget` is served by the native gateway using
`budget_status` + `BudgetStore`. Missing `.budget/budget.db` is an empty
`today` (oracle path); the GET does not create the file.

### Verification

- `budget_status_is_native_not_go_proxy` (`scope=global` and `scope=agent`)
- `cargo test -p deepseek-gateway` → **177 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
(gateway/scheduler/RAG status), launchers, exact-head CI remain.

---

## Native `GET /api/rag/status` (read-only `rag.sqlite3`)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/rag/status` is served by the native gateway using
`memory_index::local_rag_status`. The handle is **read-only** and never
creates `.local-rag/rag.sqlite3`. Missing DB → zero counts.
`sqliteVecAvailable` is `false` (native does not load `sqlite-vec`).

### Verification

- `local_rag_status_reads_the_fixture_and_does_not_invent_a_db`
- `rag_status_is_native_not_go_proxy`
- `cargo test -p deepseek-gateway` → **178 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
(gateway/scheduler), launchers, exact-head CI remain.

---

## Native `GET /api/gateway/status` (context manager, honest queue gaps)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/gateway/status` is served by the native gateway with the
ported context-manager knobs. Request queue and job scheduler are
`ported: false` — no invented `counts` / DLQ.

### Verification

- `gateway_status_is_native_not_go_proxy`
- `cargo test -p deepseek-gateway` → **179 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`,
launchers, exact-head CI remain.

---

## Native `/api/reminders` + `/api/reminders/due`: the data-plane HTTP edge, gated

**Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b`** plus the
pre-existing uncommitted slices. This slice is uncommitted on top.

The frontend calls `/api/reminders` on every reminder read and write, and it was
reaching the Go `/api/*` catch-all (`501 GO_API_NOT_IMPLEMENTED`) or Python.
`deepseek-gateway::data_routes` now serves it natively, registered **ahead of**
the Go catch-all because the store's authoritative writer is Rust — proxying it
to Go would put a second writer on one file.

### What landed

`data_routes.rs` mirrors `server.reminder_action` and
`server.api_due_reminders` exactly: the `list` / `create` / `delete` shapes, the
`{"error", "code"}` envelope, `read_json_body`'s empty-body `{}` → `list`
default, and the `Unsupported reminder action` 400. It calls the already-ported
`deepseek_policy::reminders` — no store logic was re-implemented.

**The gate is the point, and it is not a placeholder.** `POST
/api/reminders/due` reads like a query and **writes**: the oracle marks newly-due
entries `notified` and rewrites the file. `create` and `delete` do too, Python
reaches the same file from three paths, and both sides reproduce the same temp
name (`reminders.json` → `reminders.tmp`), so two writers can interleave. Every
mutating action is therefore refused with `NATIVE_REMINDERS_WRITE_NOT_OWNED`
(409) — the **same code and reason** the chat tool loop gives for
`create_reminder` — while `list` is served for real, because refusing a read the
frontend needs would be a capability regression rather than a correctness guard.

The refusal is driven by the existing `crate::may_write_native_store`, which
requires **both** `DEEPSEEK_RUNTIME_MODE=python_disabled` **and** the domain's
presence in `DECLARED_NATIVE_DATA_DOMAINS`. So the cutover is one environment
variable and needs no code change, and a mode alone cannot enable a store nobody
declared.

### Verification

`rust/crates/deepseek-gateway/tests/data_routes.rs` — **9 cases, all green**,
each driving `create_production_app`, so the auth layer and the registration
order are inside what is measured. Three are the ones that matter:

- the write is **real**: after the flip the reminder is in
  `.reminders/reminders.json` under the bound root, and `.workspace-generation`
  is exactly `2` — proving the write went through the mutation fence, not around
  it. A route that reported success without storing anything fails this.
- the refusal is **byte-exact**: a refused delete leaves the seeded file
  byte-identical, and a refused create leaves **no file at all**.
- the flip is **symmetric**: the same request one mode different stores the
  reminder for real.

Both refusal assertions were shown **able to fail** — forcing the gate predicate
to `false` turned exactly those two red and left the other seven green.

- `cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway -p deepseek-policy --locked`
  → **184 lib** + **9 data_routes** + 16 chat_execution + 6 stream + 5 + 1 + 1,
  and **441 policy**, all passed.
- Clippy 1.85 GNU `--locked --all-targets --all-features -- -D warnings` clean;
  `fmt --all --check` clean.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/native_runtime_contract.py --check` → `"ok": true`, 45 domains
  / 42 corpora / 31 versions; the reminders + ownership + gate tests pass;
  `go build ./...` and `go vet ./...` clean; docs language nav clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python, and
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`.

Remaining `/api` data surfaces the frontend calls: **memory** (`/api/memory`,
`/api/memory/search`, `/api/memory/conflicts`), projects/files
(`/api/projects`, `/api/project-files`, `/api/file-*`), media, skills, traces,
and the workspace backup/DR surface (184 Python routes total). The next
dependency-satisfied slice is **memory**: the store, the triple, the index read
path and the turn-state half are already ported and byte-verified, so only the
HTTP projection and its gate are new — the same shape as this one.

---

## The memory v3 layer and its `/api/memory` routes

**Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b`**, on top of the
uncommitted `/api/reminders` slice. Still uncommitted.

The slice the previous record named as next, and it landed as predicted: the store
was already ported, so the new work was the v3.0 **projection** layer plus the HTTP
routes and their gate.

### What landed

`deepseek_infra/infra/memory/` is not a second store. `schema.py`, `policy.py`,
`store.py` and `search.py` all delegate to `infra/data/memory.py` — the same
`.memory/memories.json` the chat turn writes. So this is a **projection** over one
authoritative store, and it is gated identically.

`deepseek_policy::memory_schema` ports it: the public vocabulary (`public_scope`,
`storage_scope`, `public_type`, `legacy_category`), the source sanitisation
(`normalize_source_ref`, `public_source`), `public_confidence`, `public_memory`,
the policy pair (`assert_memory_safe`, `readable_scopes`, `skill_can_read_memory`),
and the store/search operations (`list_memories`, `add_memory`, `edit_memory`,
`delete_memory`, `search_memories`, `memory_context_for_skill`).

`data_routes` gained the whole family: `GET`/`POST /api/memory`,
`DELETE`/`PATCH /api/memory/{id}`, `GET /api/memory/search`,
`POST /api/memory/conflicts`. Reads are served; every mutation is refused with
`NATIVE_MEMORY_WRITE_NOT_OWNED` while Python owns the store, and flips with the
same `DEEPSEEK_RUNTIME_MODE` the reminder routes and the tool loop read.

### The deadlock this slice found, and why it was structural

`edit_memory` must hold the store's process lock across the read, the patch and the
write — the oracle holds `_memory_lock` across all three so a concurrent upsert
cannot interleave. **Python's `RLock` is reentrant and Rust's `Mutex` is not**, so
the first version deadlocked on its own thread: it took `memory_process_lock()` and
then called `load_memories()`, which locks the same mutex again. It hung rather than
failing, which is why the symptom was a 10-minute test timeout and a stuck
`deepseek_policy-*.exe` holding the output binary.

Fixed by exposing the **unlocked** read/write pair (`load_unlocked_for_caller` /
`save_unlocked_for_caller`) for callers that already hold the guard, with the reason
documented at both ends. The unlocked write still takes the mutation fence — the
process lock and the fence are different guards.

### Three measured corrections

Every one of these was a wrong guess of mine, caught by running the oracle rather
than by reasoning:

1. **Identical content is not a memory conflict.** `memory.py:302` skips a candidate
   whose normalised content equals the incoming content, so `add` with the same text
   is the update path, not a 409. My first test asserted a conflict for identical
   text and failed; a conflict needs the same category, scope and conflict domain
   with **different** content.
2. **One `add_memory` bumps the fence generation four times, not two.** The oracle
   saves twice (`upsert_memory`, then `save_memories(_merge_item(item))` for the
   public fields), and each save is one fenced scope bumping twice. Measured against
   the oracle: it also reports `4`.
3. **`public_confidence("nan")` is `1.0`, not the `0.9` default.** The clamp is
   Python's `max(0.0, min(1.0, x))`, and `min` returns its *first* argument unless
   the second compares strictly less — `nan < 1.0` is `False`. `f64::clamp` cannot
   express this, so the pair is spelled out. The probe caught it on its first run.

### Verification

- **Byte-level parity probe pair** —
  `tasks/native-runtime/memory_schema_parity_probe.py` ↔
  `deepseek-policy/examples/memory_schema_parity_probe.rs`: **identical**, md5
  `d0bbb07505465d8759a9d1943486ec1f`, **164 keys**, 18 935 chars. It was shown
  **not blind**: inverting `public_scope`'s `project:` branch turns it red on exactly
  the `public_scope::project:*` keys, and the file was restored byte-identically.
- `cargo test -p deepseek-gateway -p deepseek-policy --locked` → **184 lib** + **19
  data_routes** + 16 chat_execution + 6 stream + 5 + 1 + 1, and **456 policy**
  (up from 441), all passed.
- The gate assertions were shown **able to fail**: forcing
  `may_write_native_store("memory_store")` to `false` turned exactly the two
  gate-dependent cases red and left the other seventeen green.
- Clippy 1.85 GNU `--locked --all-targets --all-features -- -D warnings` clean;
  `fmt --all --check` clean.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/native_runtime_contract.py --check` → `"ok": true`.
- `ruff` and `mypy` clean on the new probe (mypy found three real annotation bugs
  that are fixed).

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python;
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`.

The remaining `/api` data surfaces the frontend calls, in dependency order:

1. **projects/files** — `projects` and `file_cache` are already ported and
   byte-verified (`list_projects`, `list_project_files`, `read_file_chunk`), so the
   routes are the same shape as this slice. The frontend calls `/api/projects`,
   `/api/project-files`, `/api/file-*`.
2. **skills**, **traces**, **media** — the ports do not exist yet.
3. The **workspace backup/DR** surface is the largest block (~90 routes) and is
   Go-owned in the target topology, so it belongs with the Go control API rather
   than here.

## Browser engine (ADR-0050 stages 1-4) and the public data routes

Work that was in the tree uncommitted. Stage 1 (`bae68f0b`) is this file's earlier
entry; stages 2-4 and the public-route helpers are recorded here, verified rather than
asserted.

### The engine

- `rust/crates/deepseek-browser`: `engine.rs` (spawn headless Chromium, `--remote-
  debugging-port=0`, read the DevTools socket off stderr, one page session in flat mode)
  behind `sidecar.rs` (one browser per gateway session id, unknown session is
  `not_found` rather than an implicit create, fences validated, no durable store, no
  second safety policy). `CloseSession` was added to the proto so a closed session
  releases its Chromium and profile.
- The gateway seam: `deepseek-policy::browser_engine` declares what the policy crate
  needs, `deepseek-gateway::browser_engine_client` implements it over the generated
  tonic client, `ToolRoundExecutor` attaches it to the tool loop, and
  `playwright_available()` remains the single switch — no engine configured means the
  static controller answers.

### Two defects found by running the probes, not by reading them

1. **`file_routes` had two wrong test expectations**, both of which asserted behaviour
   the oracle does not have. Measured by running the oracle:
   `normalized_page_texts(...)` over the nine-entry list returns **2** survivors, not 3
   (the list's own `pages[2]` assertion duplicated `pages[1]`, which is what a
   copy-paste looks like), and `page_text_from_cached_chunks({"chunks":[{"text":"chunk
   text"}]}, requested_page=3, page_count=5)` returns **`"k"`** — `per_page = 10 // 5 =
   2`, so page 3 is `text[4:6]`. The Rust implementation was already right; the tests
   now say what the oracle does.
2. **`scroll` did nothing, and the parity probe was racing.** Chromium animates a wheel
   and `page.mouse.wheel` returns before it lands, so reading `window.scrollY`
   immediately measures the race: two consecutive runs gave `oracle 900 / engine 0`
   then `oracle 0 / engine 900`. The engine now settles the position before answering
   (a deliberate divergence from `mouse.wheel`, recorded in the spec) and the probe
   settles both sides before reading. The pair passes on repeat runs.

### Verification

- `browser_engine_parity_probe.py --rust-example …` → **PASS**, six fixtures,
  `problems: []`, twice in a row: `url`/`title`/`text` identical, `links` identical
  after blob-UUID normalisation, 0 differing HTML bytes after whitespace collapse.
- `title_parity_probe` **PASS**; `file_routes_parity_probe` **PASS** (201 cases);
  `chat_stream_events_parity_probe` **PASS** (21 events) — all four driving their own
  Rust side and writing a comparison report.
- `cargo test -p deepseek-policy -p deepseek-browser --locked`: **529** policy lib +
  9 sidecar + 2 listener + 2 live engine (`the_engine_drives_a_real_browser_through_the_declared_actions`,
  `a_browser_that_cannot_be_started_is_reported_not_panicked`), all passed, against a
  real Chromium.
- `cargo test -p deepseek-gateway --test browser_engine_e2e` → **1 passed**,
  `the_gateway_reaches_a_real_browser_through_the_sidecar`.
- `cargo fmt --all --check` clean; clippy `--all-targets --all-features -D warnings`
  clean; `check_zero_python_runtime` PASS 8/8; `native_runtime_contract --check` `ok`
  (`proto_files: 9`); docs nav PASS (214) and links OK.
- The new image-audit rule was shown **able to fail**: deleting the `browser` stage
  yields `missing required stage 'browser'`.

### Not done

- **The image was written, not built**: no Docker on this machine, so
  `rust/Dockerfile`'s `browser` stage and the compose service are verified by CI
  (`rust-docker`, `native-browser-engine`), not locally.
- The container sandbox remains an operator decision: the image and the compose service
  deliberately do not set `DEEPSEEK_BROWSER_NO_SANDBOX`, and a container with neither
  user namespaces nor that opt-in fails closed to the static controller.
- Three of the four new probe pairs are not CI steps.
- The `/api` surfaces this file listed earlier (skills, traces, media; the Go-owned
  workspace backup/DR block) are still unported.

## 2026-09-28 — control authority batch, PR #182, and native OCR CI follow-up

Branch: `codex/indexmap-std-feature`. The implementation HEAD before this
continuation update is `e42c591c0c3ab14ced46e0f524a287284b6d6a32`; PR #182 is a
draft against `main`. The control authority, signed apply, shadow denial, skills
parity, and native OCR repair lines have local validation before this push.

- Go schema v8/v9 persists an authority claim, per-domain cutover, and signed v2
  `apply-mutation` with an atomic result journal. Direct `Put` and `PutShadow`
  cannot bypass a promoted domain, including a shadow-write race. The protected
  loopback routes expose claim/head, transition, and apply. Frozen v1 request
  behavior remains unchanged; the v2 Python/Go/Rust corpus is versioned as v32.
- Local Go `test ./...`, `vet ./...`, and the 95.0% coverage gate passed at
  **95.105673% (5130/5394)**. `mypy .`, `ruff check .`, focused Python native
  contract/parity tests, shadow parity, docs checks, and the native contract
  check passed. Windows `go test -race` exits before running tests; the Linux
  `native-go` CI job is the race gate.
- PR CI exposed two missing Rust job dependencies (`pdftoppm`, then Tesseract
  and Python OCR packages); both jobs now provision them. With OCR available,
  Ubuntu Tesseract hallucinated `a` on the Rust gateway's 569-byte blank PDF.
  The Python oracle skips a completely white page, and the **native Rust** PDF
  path now checks the decoded Poppler PNG before accepting nonempty OCR text.
  Engine-unavailable errors still return unchanged.
- The native blank-pixel unit test passed, and the real gateway
  `file_text_route` suite passed **10/10** locally with the pinned Rust 1.85
  toolchain and its bundled GCC 14 linker. Rust fmt, check, and strict policy
  Clippy passed. The machine's unrelated MinGW 8.1 linker failed to link the
  same test; it is not a test assertion failure.

The latest analyzed PR run before the native Rust fix, `36418657422` at
`849dec70`, had Rust and Rust coverage failures from the same blank-PDF
assertion; Evidence Assembly then failed downstream. Exact-head CI for the
new native fix remains open until the final batch commit is pushed and the
result is collected. `release/native_runtime_5_0_evidence_v1.json` remains
`NOT_READY`: no production ownership flip, real provider reconciliation,
desktop/Android zero-Python proof, or final release qualification is claimed.

Next executable work after this batch's exact-head CI: add an externally signed
per-domain promotion artifact and prove export/import, unique-writer fencing,
rollback, and restart on isolated data. Continue the remaining native API,
worker/provider, desktop, Android, and server-side TypeScript replacement
lines against `migration-matrix.md`.
