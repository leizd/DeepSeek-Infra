# Native runtime migration matrix

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

**2026-10-10 process signing preparation:** the repository process producer
compiles against the actual Go API/store and passes real-UTC planning/tasks.
The cloud checks job is prepared to run the production Rust custody provisioner
and Worker over pinned TLS, exercise real Go lease callbacks, refuse rebinding
and expired/cancelled scope, and prove immutable journal replay across a forced
Worker restart. The complete process cases have not run in the cloud yet;
producer preflight and environment/public-metadata tests are not their proof.
Current Go batch `agent-grant-current-v2` passes all 10 default/Android gates,
including both races and exact **95.000931% / 95.062547%** coverage against the
unchanged 95% floor; the final receipt confirms unchanged Go inputs. Rust caches
were explicitly cleared with source/index integrity checks; no new host Rust
build was started. Agent provider/node effects, handoff, full daemon topology,
platforms and release acceptance remain open. **NOT_READY**.

**2026-10-10 Agent signing checkpoint:** an additive typed signing purpose
now invokes Go lease renewal before Rust issuance, with exact durable epoch,
request/plan/claim binding, bounded expiry and immutable retry bytes. Rust Worker
format/strict Clippy/all 145 tests and focused Go signature regressions pass.
The lease producer in this new test is controlled; actual Go/Rust signing and
provider effects remain unqualified. Complete prior PASS receipts belong to the
preceding input. Current Rust qualification now passes in the user-authorized
cloud snapshot `291092e9210570416036d4a7292f53498a8572d8`, actual run
`38034954077`: formatting, strict all-target/all-feature Clippy, 1414 passing
tests/one existing Redis ignore, all 15 crates and **81.797900%** line coverage
at the original 80% floor. The clean cloud revision and local source/index match
are recorded in `artifacts/native-20261010-rust-cloud-preview/cloud-evidence.json`.
Only small reports were downloaded; compiler caches stayed in the cloud.
Current complete Go gates and actual Go/Rust signing remain pending. **NOT_READY**.
The isolated real-UTC Go store/RPC producer probe passes planning/tasks,
replay/rebinding/cancellation and independent writer expiry (`agent-real-utc-probe-green-v4`);
it does not qualify the Rust signer, TLS or provider/artifact effects.

**2026-10-10 execution admission checkpoint:** the actual Go store and registered
AgentRunControl RPC now support native execution claim/renewal. The metadata and
execution fences remain distinct. Both Go domain authorities, immutable request
and plan binding, lease/resource reservation and replay receipt commit together.
Invalid raw UTF-8 identities refuse before persistence; current focused tests
cover bounded default/planning leases, damaged/expired replay, actual SQLite
write rollback, scope loss and cancellation/plan-edit denial. Read paths now use
one shared transactional scope check without weakening the rules.
`agent-execution-go-replay-scope-v17` passes. Full default tests pass, but complete
coverage attempts remain failed at 94.665665% and 94.973222%; both original failed
receipts are retained. The next frozen batch is `agent-execution-go-full-v3`.
That batch now passes default vet/tests and exact **95.020201%** coverage on Go
digest `755d8d9b6e32`; all 374 Go/Proto input hashes match. All ten default/Android
checks now pass, including both race suites and Android **95.073185%** coverage;
the original floors are unchanged. Source and owned-container cleanup are verified
(`agent-execution-go-full-v3.json`). Current complete Rust qualification also
passes: all 15 crates, 1409 tests/one original ignore, 1410 inventory entries,
complete LCOV and **81.773383%** at the original 80% floor
(`agent-execution-rust-full-v3.json`). Both oracle-image failures are retained;
only the verification environment changed, all 3673 product input bytes match.
The actual Go/Rust lease/read product probe passes (`agent-execution-product-v1`):
admission, replay, renewal, terminal denial, body reopen and public HTTP/NDJSON.
Its fixed clock 1000 and signed fresh-empty data are declared; both containers are
removed, no RAM archive. Runtime bytes still match the qualified inputs; only
these three migration Markdown records changed afterwards.
Signed Rust Agent effects, durable provider execution/recovery, nonempty handoff,
platform and exact-head release completion remain open.
**NOT_READY**.

Previous Agent metadata slice: Go owns the actual event cursor, projection and
operation receipt in its existing control store. The private typed RPC requires
loopback authentication, authoritative process mode and a transactionally checked
domain fence. SQLite/RPC regressions pass; 62 original-reference histories match
all 1000 checkpoints. Source-bound proof:
`artifacts/native-20261009-agent-events-contract-green.json`. All ten Go
default/Android gates pass on unchanged digest `6830e490faeb`, including both race
suites and 95.087178% / 95.162398% coverage at the original 95% floors; source and
RAM-cleanup receipts pass (`agent-events-go-full-v2.json`). Complete current Rust
qualification for those preceding metadata receipts is superseded by the current
full Rust receipt above. Rust body custody and public reads are locally verified;
the complete public Agent API, actual fenced effects/recovery and nonempty legacy
handoff remain open; opaque artifact handles do not prove these. **NOT_READY**.

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


**2026-10-09 Agent durable control checkpoint:** Go metadata now retains pending
approval, active-run orphan checkpoints, terminal resume/rerun and snapshot
updates. Both new regressions and the full Windows Store suite pass; restart
reads and identical retries retain history without claiming execution authority.
The actual Python writer and enabled automatic resume now use existing ownership
denial before filesystem/worker mutation. All 54 affected tests pass without
skips, plus Ruff/local Mypy; direct start/approve/resume/rerun cannot call providers
after control authority changes. All ten Go default/Android/race/95% gates pass
in executable RAM (`99296f01d834`, 349 files): default **95.146884%**, Android
**95.215456%**. The verification container and 991,043,584-byte RAM cache are
removed; no build cache is archived. Android-tag tests do not qualify an APK.
The real legacy
exporter supports policy/target only: nonempty Agent import/fencing/rollback,
actual public run/event/DAG/execution/recovery and browser/provider/platform
acceptance remain open. Review: `native-20261009-agent-durable-control-review.md`.
Keep `NOT_READY`; fresh isolated signed lifecycle tests are not legacy-source
migration proof.

**2026-10-09 build capacity and interactive Agent checkpoint:** current production
inputs pass complete workspace fmt/check, strict all-target/all-feature Clippy
and two full all-feature test runs, each 1394 passed/one original ignored. The
repeat reuses 599 artifacts; RAM cache remains 7951 files and 4,176,035,014 logical
bytes. The child-only scratch and registry/metadata guard passes 64 regressions;
missing toolchains are not implicitly downloaded. The approved 1964-file cleanup
adds 631,775,232 bytes of D: free space and preserves 55 sibling hashes. See
`native-20261009-rust-final-policy-evidence.json`.

Interactive Agent pure compatibility has 103 reference cases; eight actual
Go-daemon/Rust-public-HTTP scenarios pass with a controlled provider. The fixture's
zero-budget expectation was corrected to the reference's unlimited semantics;
positive 20-token budget stops before the next tier and preserves synthesis.
Cryptographic input reconstruction confirms the later change touches only that
qualification example, and its current eight scenarios and strict Clippy pass.
This grants no durable authority: the browser's actual `/api/agent-runs`, plan,
rerun/resume/cursor/restart recovery, current Go 95%/race, current Rust 80%
coverage, remote provider and whole-platform/exact-head CI remain open. Keep
`NOT_READY`; prior failed receipts remain failures.

**2026-10-08 current verification and search qualification:** Docker recovered;
persistent outputs are on C: and Linux Rust intermediates use executable RAM.
Stable 343-file GUI Go v5 passes all ten original gates, including both race
suites: default 9458/9947 (95.083945%), Android 9284/9757 (95.152198%). Current
415-file Rust input `f308ae998b47`, frozen over 3638 complete files, passes full
workspace fmt/strict all-target/all-feature Clippy, 1355 default tests (one
ignored) and all-feature coverage 73301/89502 (81.898729%). Both original floors
remain unchanged. Forced-search prefetch passes thirteen focused tests, including
22 offline projection-oracle cases: live prefetch progress, hardened result
context, caching, seeded budgets/citations, failure context and disconnect before
model dispatch. Bindings are the `search-prefetch-freeze`, `search-prefetch-focused`,
`search-prefetch-ram-rust-gates` and `search-prefetch-ram-coverage` receipts.
The earlier 413-file C-bind test timeout on Windows 9p and prior disk failures
remain failures. Windows original real-S3 gates pass after an actual race
loader preflight fixed test-only synchronization-library linkage; production Go
remains CGO disabled. Seven storage tests, ten worker tests and the actual Go
race integration exercise three of five fresh MinIO providers, including
versioned C, hard-kill replay and six lost-response/takeover cases without
duplicate PUT/version. Receipt: `native-20261008-provider-current-windows.json`;
all five fixtures are stopped with data/logs retained. Remote-provider and
whole-product recovery acceptance remain open. Earlier
package receipts do not qualify this newer Rust source. No agent cache deletion
or runtime-volume removal occurred. Status remains `NOT_READY`.

**2026-10-08 native CLI/mobile, local qualification:** source
above `bbcbdb51` adds mode/address/provider-key/OCR arguments, mobile control
readiness, optional masked input and Windows/Linux/Termux browser glue. Focused
unit/source boundaries, actual Windows/Linux terminal restoration, native
headless/mobile HTTP and LAN/peer workloads pass. Both Go build variants pass
all ten original full gates at the 95% floor; Windows checks/security and Rust
fmt/strict Clippy/workspace tests/80% coverage pass. Current binaries complete a
nonempty owned model/search/file workload without an installed interpreter.
Actual default browser/Termux/device recovery, complete configuration GUI,
platform packaging and whole-product/provider/CI acceptance remain open. The
credential-storage draft at that checkpoint was separate. These named local slices
do not rebind earlier package receipts or change `NOT_READY`.

**2026-10-08 configuration GUI, implementation/local integration:** above local
commit `0caf8ec4`, Go hosts only the private configuration page and native process
lifecycle; the retained React page provides seven settings and launch controls.
Local encrypted settings have one kernel-held writer; signed control-domain
ownership/data migration still requires separate qualification. The first frozen
Windows candidate `0cd16dcc5b02` passes save/start/stop/reload/restart, with the
user-supplied conversation-page screenshot and a successful native HTTP message
stream against an owned fixture model. Frontend full gates pass 613 tests.
Current code repairs late restart after confirmed close, both HTTP/native-window
stale-state close races and Windows exclusive port classification. The Go
controller atomically checks confirmation and fences starts. Corrupt encrypted
settings are retained until explicit recovery, and real Windows/Linux
settings-owner hard-kill/reopen probes pass. Focused launchgui coverage is 97.7%;
GTK confirmation/callback boundary tests pass. These are named local boundaries,
with actual native modal cancellation still open. The v4 freeze contains 343 Go
and 412 Rust inputs; three fresh Go Windows binaries build on C: and the unchanged
Go sources pass three source-security variants. Matching pinned Rust gateway and
worker build offline on C:. Actual frozen-v4 Windows HTTP/DPAPI/native-process
integration passes 34 checks: model streaming, nonempty upload/read/original,
restarts with retained token/file, corrupt-ciphertext refusal and explicit
clear/recovery, and owned-tree cleanup. The native window/launcher entry and
browser message are not exercised by this harness. Full v2 default coverage failed
at 94.979374%; subsequent complete attempts failed with D: disk/container I/O,
and v4 Go execution also failed under the new `/tmp` mount's default `noexec`.
The original 95% floor is retained. The authorised exact task-cache deletion
remains unexecuted after automatic approval rejection; human recovery is pending.
Legacy import, actual native-window/platform close behavior, current
window/launcher workloads, fresh Android/package bindings, provider recovery and exact-head CI
remain open.
Scope: implementation and named local integrations, not complete acceptance.

**2026-10-08 async Rust browser startup:** the gateway now connects to the real
gRPC sidecar on an owned runtime thread without nesting a runtime inside async
startup. Actual sidecar red/green tests and complete pinned fmt/strict Clippy/
default workspace tests pass (1349 tests, 123 suites, one ignored). The current
412-file Rust component is bound per file to the GUI freeze. Both all-feature
coverage attempts fail under disk/container I/O with no metric; the original
80% coverage gate and whole-product acceptance remain open.

Generated from ownership JSON, route modules, launchers, Compose files, Go/Rust
packages, and CI jobs — not from README claims. Completeness is **wired native
behavior with evidence**, not file/crate counts.

**2026-10-07 Windows desktop window:** the default supervisor now launches the
actual WebView2 platform host. A real authenticated React text-upload/read/source
workload and separate default-launcher process-tree/hard-kill qualification each
pass seven checks at unchanged dirty source `6eb60ff37692`. The persistent token
is retained and all owned WebView2/backend descendants exit on supervisor death.
The production UI has no JS business bridge or provider/control credentials.
The earlier immediate-restart failure is retained. A bounded wait now uses only
the normal writer claim after release/expiry, and the UI checks the proxied Go
control route. The secure draft passes twelve actual rapid-restart checks with
retained token/file bytes and unchanged old fence until natural expiry. This
delta and SQLite failure cleanup tests are committed locally at `ff3fab80`, with
unchanged product inputs `a15030bc4314`. Fresh clean-commit Windows build/vet/test,
WebView2 workload, rapid-restart and zero-finding security checks pass. The full
candidate Go gates pass at 95.050863% default and 95.028941% Android; their original
source bindings are retained and canonically match the commit.
These are local Windows slices; the Linux consumer slice is recorded below.
macOS windows and fencing, launcher
configuration/mobile flows, chooser and all business workflows, upgrades,
packaging and exact-head CI remain open. Whole-project status is **未完成 / NOT_READY**.

The Linux GTK/guardian candidate passes all ten full Go gates and
retains the original 95% floors: default 95.036252%, Android 95.071938%. Its 299
files have ordinal digest `7fc6247cb68c` (Windows path-order digest `744ee239b959`).
Actual non-root SDK DOM/file/cookie/profile and default-supervisor hard-kill,
orphan custody, natural writer-lease restart and retained-source checks pass.
The 22-source-file delta is now integrated locally above `ff3fab80`, with original
bytes backed up and canonical equality to the qualified manifest. Shared Windows
build/vet/test, actual WebView2 SDK and restart regressions also pass.
The clean consumer image `16b2934ad5cd` bundles all five native executables and
static assets, runs non-root with no Python/Node and default Docker seccomp,
and visibly renders the Chinese React desktop. Actual native pointer/keyboard
and GTK chooser upload, text preview, original view and byte-identical download
pass for 129 English/Chinese bytes. Same-container restart retains auth and both
REST/UI source bytes and restores the visible desktop. Source, image, packages,
bundle and screenshot bindings are in
`native-20261007-linux-consumer-workload-and-ui.json`. No production acceleration
override was introduced. Earlier tools-image blank captures and independent
GTK/WebKit controls are preserved; their environment difference is unresolved.
Permissions, all retained business flows, whole-product zero-Python counters,
macOS, ARM64 execution, platform packages and exact-head CI remain open.

**2026-10-07 clean Android OCR package:** local commit `71b05a62`, source digest
`22a28aaf6548`, produces APK `cf00d5ca167a`. Actual upgrade retains UID/token and
all 44 business files. Seven device tests, 26 recovery/UI checks, 17 file checks,
ten EXIF/public-disconnect checks and new-generation React upload/text/PDF-render/
byte-identical downloads pass on API 35 x86_64. Both ABI binaries pass inspection;
ARM64 execution and Go shadow-to-authoritative cutover remain unqualified.
Formula/media, all-platform desktop/launcher, full ownership/provider settlement
and exact-head CI/Evidence remain required. The Windows window slice is recorded
above; the remaining desktop platforms and launcher flows are still required.
The preserved package qualifies its exact recorded source, not later desktop
edits. Whole-project status remains **未完成 / NOT_READY**.

**2026-10-06 native control custody:** clean base `3406f7ad` passes all 37 CI jobs,
closing the prerequisite failures below. The new default Rust worker generates
and loads its independent encrypted control key, exposes typed epoch/PUT signing,
and persists immutable issuance receipts. Authoritative Go storage execution,
including renewable claims, calls this path after durable claim and rechecks
ownership/lease before sending unchanged payloads. Three actual MinIO providers
pass native custody, Unicode byte reads, TLS, process-kill/restart and stable
object-version checks. Administrative promotion is still an offline fixture;
the leased action reaches `VERIFYING`. Full Fleet custody/rotation, proof settlement,
all-domain/platform migration and release Evidence remain required. Status remains
**未完成 / NOT_READY**. Exact logs and current coverage scope are in `continuation.md`.

The custody checkpoint `005e5bb6` now passes all **37/37** exact-head CI jobs,
including Evidence Assembly, release-package and RC checks. The next qualification
adds six actual response-loss scenarios across three providers, including a
versioned bucket, with Go race enabled. After worker death and action lease expiry,
epoch 2 reconciles the original epoch 1 dispatch; real absence stays unknown until
the delayed original PUT is verified. The recovered receipt survives another
worker death. Neither recovery path repeats a PUT or creates an epoch 2 dispatch.
These outcomes stop at `VERIFYING`; they do not certify Receipt/Commit outcome,
risk assessment, live production promotion or whole-product readiness.

**2026-10-05 CI prerequisite repair:** pushed `181dbdf6` passes 31 complete jobs,
including native Go, MCP failover and hybrid. Rust tests/coverage need the actual
Vite build, and the Go provider stage needs module downloads before proxy refusal.
The workflow now prepares those inputs and disables Go module lookup during the
provider run. Both local Linux production-contract cases, the frontend build,
ten workflow guards and module verification pass. Exact-head CI/Evidence remains
open; status remains **未完成 / NOT_READY**.

**2026-10-05 pushed checkpoint and CI repairs:** `7fe490d6` is pushed; its exact-head
CI passes 28 jobs and the Go format/vet/whole-test/race/coverage stages, but six
upstream jobs fail before complete Evidence qualification. Current repairs isolate
Python compatibility images to offline hybrid reference tests, supply a real Rust
MCP failover fixture, install complete Rust oracle dependencies and keep authority
refusal explicit in the Go TLS boundary. The combined actual-MinIO runner passes
seven storage tests, ten worker tests and three Go-promoted provider subcases;
its emitted production-worker hash is `399db5c1...481a90` on pinned Rust 1.85.0 /
Go 1.27.1. The local Windows TLS boundary and native fixture execution pass.
These are local integration/source qualifications; production Rust key custody,
all remaining route/domain ownership, desktop/Android and repaired exact-head
CI/Evidence remain open. Status remains **未完成 / NOT_READY**.

**2026-10-05 verification slice:** the frozen Go store race completes with 1083
passes at its measured thirty-minute bound. Dedicated production S3 configuration
now reaches the default Rust worker through the supervisor; ten actual Linux
provider tests pass, including TLS, forced process restart, binding-checked exact
receipt replay and a versioned third provider. Windows default-binary qualification
also passes ten tests from the native build directory after isolating an OS write
denial. Actual Go control promotion, signed epoch/grant issuance, TLS dispatch and
independent provider reads pass on all three providers. These use an offline signer
and do not prove production Rust key custody or full-fleet cutover.
Resumed whole Python reaches 5629 passes and 95.574917% but fails two now-repaired
tests; the final frozen `70b29cf9` rerun passes **5631 tests / 95.589801%**. The zero-Python checker
now explicitly reports source-contract scope and unverified deployment. Historical
coverage values below remain bound to their older inputs; no exact-head release
credit or full-product readiness is claimed. Status remains **未完成 / NOT_READY**.

**2026-10-04 verification slice:** mirror source fencing, independent native
inventory import, force-kill recovery, terminal revocation and pre-admission
handback pass 12 native and 100 Python cases. A real Python Age producer passes
native import/revocation, original-writer handback, sequence 1→2 and decryption;
the native candidate stays permanently denied. Source and target restore locks
are retained and any unknown restore fence blocks. Twelve Redis Lua scripts are
now Rust-owned with unchanged TypeScript oracle bytes; actual Redis acceptance
passes after fixing RESP error-buffer poisoning. Frozen input `33acecda...ab8c6c`
passes fifteen-crate Rust line coverage at 80.732473% and 1334 executed tests,
with one intentional provider ignore separately exercised against Redis. The
producer Git metadata discrepancy prevents exact-head release credit; status
failures now refuse generation. Whole Python reaches 95.580237% with 5614 passing
cases and two failures, now repaired; fresh whole validation remains pending.
Current Go coverage passes at 95.030835%. Whole race times out only the store
package at its default ten-minute bound; its thirty-minute rerun remains pending.
A rebuilt no-Python/Node candidate proves native memory writes, sibling shutdown,
writer-lease refusal/expiry recovery and normal SIGTERM. Actual three-service
Compose v1 channels and namespace-owner restart preserve state. Full
production admission, post-effect handback, restore consumption, all public
parity, platform/provider and release qualification remain open; readiness stays
`NOT_READY`. Exact source bindings and logs are in `continuation.md`. Preserve
concurrent workspace-home, projects, automation and stateless-MCP work.

**2026-10-02 historical slice:** typed `control/v1.GetBackupPolicyRecipients` replaces
the newly added HTTP/JSON recipient read. Four current Linux Rust/Go process tests
pass, including actual ciphertext verification, replay, force-kill/restart after
the recorded lease and unavailable-RPC refusal. A corrupt restore-fence fail-open
was reproduced and fixed; its added regression awaits rerun. This is local
**集成通过**, not **完成切换**. Mirror source/target attestation, durable data-domain
admission, handback, restore-consumer and browser proof remain open. Whole native
coverage/race runs were interrupted by a local Docker I/O failure; no older
coverage result is credited to the new RPC. See `continuation.md` for exact logs.

**2026-10-01 sealed-mirror slice:** `frontend_mirror_store` is declared (python -> rust,
4.9.4) and its Python writer is mechanically denied at the single choke point;
`deepseek_policy::backup_mirror` ports the store and the three public routes are mounted
only after the cutover. Two-way byte-level oracle parity passes 8/8
(`artifacts/backup-mirror-parity-probe.json`). No domain is flipped in the default
deployment; the mirror has **no source fence or transfer document** yet, so Rust reads
the same directory Python wrote and a cutover still means stopping the Python service.

**2026-09-29 current checkout:** branch `codex/indexmap-std-feature`, base HEAD
`0e340dc3695890e8d85d59c8263192f0593fb79e`, clean before this slice.
The Go control-claim, **signed per-domain promotion**, cutover and signed-apply
HTTP chain is **集成通过 locally** on an isolated store; it is not a production
ownership transfer. No domain is flipped in the default deployment,
`release/native_runtime_ownership_v1.json` remains the baseline, and 5.0
readiness is `NOT_READY`. The 2026-09-26 and 2026-09-27 entries below remain
historical snapshots, including their then-current HEAD values.
Sections 2-4 still aggregate some routes and platform flows; they do not yet
provide a per-capability source entry, observable behavior, implementation,
compatibility case, platform and evidence record for every existing feature.
That inventory work remains open and no aggregate row may be counted as
完成验收. The control-authority slice below records those details explicitly.

Re-verified 2026-09-26 against branch `codex/indexmap-std-feature`, HEAD
`79aba745c7f17101349e64edc8e2cca101c873df`, clean worktree at the start of the
slice. `release/native_runtime_5_0_evidence_v1.json` is still `NOT_READY`. Rows
below keep their historical evidence text until that row is re-measured. A row
this slice re-measured uses only these states: 未实现 / 框架 / 单测通过 /
集成通过 / 完成切换 / 完成验收. **集成通过** means the native entry serves a
legal success and refuses an illegal request in-process. It is not **完成切换**:
the default launcher and image still start Python, and readiness is unchanged.
The `/api/skills` row and the `skills` row were re-measured later the same day with
this slice's files still uncommitted in the worktree, so "clean worktree" above
describes the start of the earlier slice, not that measurement.

**2026-09-27 addendum (uncommitted worktree on the same HEAD):** the Go control
plane gained schema v8 — a `control-authority-v1` authority head, its append-only
checkpoint journal, and append-only cutover authorizations — so a control domain can
now actually be promoted, but only when the deployment opts in
(`DEEPSEEKD_CONTROL_AUTHORITY`) **and** presents the live authority tip. Opting in is
refused unless `DEEPSEEKD_INTERNAL_BEARER` is configured, and every `/internal/*`
request now requires that bearer from a loopback peer (401 otherwise; no control plane
at all when unconfigured), so the shipped default is unchanged. Section 5's
`internal/store`, `internal control API` and `cutover` rows and section 9 item 7 were
re-measured by that slice; **no domain is flipped** and
`release/native_runtime_ownership_v1.json` is untouched.

Companion files: [`continuation.md`](continuation.md),
[`go-action-admission-plan.md`](go-action-admission-plan.md),
[`worker-execution-plan.md`](worker-execution-plan.md). Historical 4.8.1–4.9.x
plans remain; this matrix is the unified tracker.

Legend: `doc` documented target; `code` native code exists; `wire` production
or public entry uses it; `local` this workspace verified; `ci` exact-head CI;
`prod` production owner. `py` = Python still owns the live path.

## Local integration — 2026-10-07

The authorised local merge is applied at `37efd8e9`, with reviewed tree
`678ce430`. The isolated API 35 x86_64 package `32b35b1cc71b` (dirty source
`c593437df2d7`) passes package inspection, retained-data upgrade, three device
tests and the complete 26-check recovery/UI workload. Its Go plane remains
shadow and does not establish production ownership. Actual plain-text upload,
reader and exact source bytes passed, while image/scanned-PDF uploads exposed
a missing platform connection. New APK `bde81447a61a` (source `ab9d9f0b5616`)
now qualifies the streamed authenticated platform connection: seven device
tests, 26 recovery checks, 17 file checks and ten EXIF/public-disconnect checks
pass, plus actual browser upload/Chinese text/PDF page-two rendering and exact
source download. Upgrade preserves UID/token/all 25 business files. Its Go
plane remains shadow and ARM64 execution is unqualified. Worker test profile
fixes pass complete Linux coverage at 81.454954% with unchanged source inputs;
the final source-bound commit/package and exact-head CI remain required.
Current receipts and remaining gates
are recorded at the top of [`continuation.md`](continuation.md).

The resolved source retains the clean primary `707a0528` and the isolated task's
Android, browser authentication, Skills parity and store fencing work. Both Go
race/vet/95% coverage variants pass; the actual Rust HTTP comparisons pass
133 read, 49 mutation/recovery and 20 run cases. React checks and actual browser
create/edit/disable/reload pass. The current Android Java code compiles, while its
new APK/device workload and exact-head CI remain unqualified. Online Skill and
Android product OCR/media paths remain open. Full proof scope, source hashes,
unchanged thresholds and retained pre-integration APK are in
[`continuation.md`](continuation.md#integration-preview--2026-10-07).

The preceding comparisons qualify the retained pre-merge review source. The
clean `D:/deepseek` checkout remains at `707a0528`; the isolated worktree holds
the authorised merge and ongoing source changes. Exact-head CI is pending.
Status remains **未完成 / NOT_READY**; retain every whole-product gate.

### Retained primary migration record


## Current audit — 2026-10-06

Source: `release/native_runtime_ownership_v1.json` (**51** domains; trace/generated stores are included and `frontend_mirror_store`
was added on 2026-10-01 as `data`, python -> rust at 4.9.4, `rust_data`). Current
production authority is Python. Target owners are 5.0 goals.

| Domain | Target | Current prod | Native code | Wired | Evidence | Blocker |
| --- | --- | --- | --- | --- | --- | --- |
| public_http_listener | rust | py | deepseek-gateway routes registered | chat + MCP + A2A + Go public `/api` status | local gateway + Go tests | remaining Python `/api/*`, `GO_CONTROL_ADDR` |
| llm_gateway_sse | rust | py | gateway request-prep + non-stream + SSE execution | **SSE wired, byte-parity verified locally** | `chat_stream.rs` (15 unit) + `tests/chat_stream.rs` (7 real-boundary) + byte-identical probe | exact-head CI; `/api/chat` NDJSON |
| chat_completions_fast_path | rust | py | route exists; non-stream loop runs tool rounds through `dispatch` | wired; boundary-verified | `tests/chat_execution.rs` (real scripted upstream: continuation, data branch, budget exhaustion) | streaming tool loop; exact-head CI |
| chat_streaming_openai_sse | rust | py | `chat_stream.rs` decoder + encoder + read loop | wired via `chat_completions` | byte-identical to oracle (`b9129475…`); 6 scripted upstream cases | tool-round refusal is in-band; no `/api/chat` |
| chat_streaming_ndjson (the `/api/chat` protocol) | rust | py | `deepseek-policy::chat_stream_events`: the seven-event vocabulary, `encode_stream_event`, the accumulator, `merge_usage_totals`/`usage_int`, and the streaming tool-call merge; `deepseek-policy::chat_diagnostics`: `diagnostics_with_tools`/`diagnostics_with_usage`/`diagnostics_with_search`/`search_round_count` | **ported; wired by `deepseek-gateway::chat_ndjson`** | `chat_stream_events_parity_probe.py` **PASS**: 21 events byte-identical (including the `done` envelope and its `usage` key order), 11 usage merges, 8 streamed tool-call sequences (raw accumulator + finalized), 4 tool-diagnostic cases, 17 usage-diagnostic cases over the `round(x, 1)` tie corpus, 5 search-round counts and 6 search-diagnostic cases, all against the imported oracle; 15 unit tests; `tests/chat_ndjson_route.rs` | search prefetch, edge inference, cascade, agent mode and the gateway-attempt/semantic-cache/cost/trace diagnostics blocks are refused or outstanding |
| openai_facade_translation + composition | rust | py | `openai_facade::openai_to_internal_payload` — the six forwarded fields (`model`/`messages`/`stream`/`thinkingEnabled`/`localBaseUrl`/`temperature`), the seven dropped (`tools`, `tool_choice`, `max_tokens`, `top_p`, `reasoning_effort`, `thinking`, everything else), and the two refusals (`invalid_payload`, 400); plus `native_chat` — the `call_deepseek`/`prepare_deepseek_call` composition (validate → memory → build) with `PreparedOpenAiChat` | **ported and probe-verified; not yet invoked by the route** | translation byte-identical (`openai_facade_parity_probe`, **56 keys, 12 204 chars**); **composition byte-identical** (`native_chat_composition_parity_probe`, **15 cases, 298 431 chars** — body + diagnostics + tool names + api key); 10 unit tests | **the native route still builds its body from the OpenAI request directly** (`request_preparation::prepare_chat_request`), which **forwards six fields the oracle drops** and **omits `thinkingEnabled`/`localBaseUrl` that the oracle sets** — a visible divergence on a public route (§五.11). Remaining: `AssemblyEnv::from_env` + its settings env readers, `request_base_url`, the error envelope, a real-upstream test. **Measured and closed on the way:** the composed tool list has **no `web_search`** (`searchEnabled` is never forwarded) and `forced_search_mode` is **structurally unreachable** here, so no refusal is owed for it |
| chat_message_key_order (nested) | rust | py | `request_assembly::NESTED_ORDERS` — the message/tool-call key orders the body renderer reconstructs | **fixed** — `messages` now `role, tool_call_id, content, tool_calls` (a superset serving all three oracle message shapes) plus a `tool_calls` entry `id, type, function` | composition probe byte-identical; `request_assembly_parity_probe` unchanged at 1 946 077 chars | the old `["role","content"]` rendered a tool result as `role, content, tool_call_id` and a call entry as `function, id, type`; the assembly probe's corpus has no tool-role turn, so only the composition probe could see it |
| assembly_env | rust | py | `assembly_env::NativeAssembly` — the nine `AssemblyEnv` fields bound from the server environment (five settings `Default`s, `budget_store` + `LedgerDeps`, `FileStore` + `attachment_context` expander, `local_clock::system_local_now`), with the file-index guard returned as a flag | **landed; not yet invoked by the route** | 7 unit tests incl. both sides of the index guard (a 70 000-char attachment trips it, a short one does not) and the unset-root refusal | **env readers for the settings are not ported**, so only a default-configured deployment agrees; `DEEPSEEK_INFRA_ROOT` unset is an error rather than a degrade (unlike `chat_tool_loop`, the assembly has nowhere else to read the stores from) |
| chat_message_normalization | rust | py | both layers fail closed on the same codes | aligned | 101 passed (`df7dfa13`); `oracle_layering_probe.py` | none for preparation; SSE/tool rounds separate |
| tool_round_control (layer 1) | rust | py | `tool_rounds.rs`: accumulator, lenient normalize, round decision, message assembly, force-final | **implemented; wired into the non-streaming loop** (`chat_tool_loop.rs`) | byte-identical to oracle (`ca9b072a…`), 36 keys / 12 scripts; 24 unit tests; `docs/GATEWAY_TOOL_ROUND_PARITY.md` | streaming round continuation; layer 2's unported branches degrade, not block |
| tool_dispatch_seam (layer 2a) | rust | py | `deepseek-policy::tool_dispatch`: envelope, `parse_tool_arguments`/`safe_limit`/`tool_call_name`/`is_parallel_safe_tool`, branch inventory + routing, parse→gate→route order | **ported; wired — `chat_tool_loop`'s runner calls `dispatch` per call** | byte-identical to oracle (`9d491ef3…`), 49 keys; `docs/GATEWAY_TOOL_DISPATCH.md`; boundary test pins the follow-up request | none |
| tool_branch: generate_chart | rust | py | `tool_dispatch::generate_chart` + `chart_markdown_table` | **ported; wired via the loop** | in the same 49-key diff; boundary test asserts the markdown table reaches the model | none |
| tool_branch: data_transform | rust | py | `tool_transform`: `data_transform` + 4 operations + helpers (json-path, csv, stats) | **ported; wired via the loop** | in the 80-key batch diff (`3f088f27…`) | none |
| tool_batch (layer 2b) | rust | py | `tool_batch`: `execute_tool_calls` batching + cancellation + `role:"tool"` message assembly | **ported; wired — the loop runs one batch per round** | in the 80-key diff; concurrency not reproduced (sequential group = same index-ordered list) | thread pool is a mechanism; the 7 unported branches degrade to "Tool did not run", visibly |
| tool_branches: search family | rust | py | `tool_search`: `web_search` + `compare_search_results` (callback injected via `ExecutorContext`) + `search_result_key` | **ported; wired via the loop (no provider yet → "not enabled for this request")** | in the 104-key diff (`a6aa9b0e…`) | the web-search provider the gateway owns |
| data_layer_storage (memory/reminders/projects) | rust | py | memory/reminder stores; legacy and Workspace project read projections | tool reads and native public data reads wired; memory/reminder writes gated | store probes, `tests/data_routes.rs`, project oracle and process proof | project mutation ownership, upload/extraction and cleanup; full production cutover |
| data_branch: reminders | rust | py | `reminders`: store + `create_reminder` + `list_reminders` + `delete_reminder` + `due_reminders` + `parse_due_at` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`a636cd45…`), 74 keys; 171 tests; file bytes incl. key order; boundary test writes through the fence | `due_reminders` ported but not yet in a compared corpus |
| data_branch: memory triple + memory index read path | rust | py | `memory`: store + `suggest_memory` + `recall_memory` + `forget_memory` + normalization/fingerprint/category/conflict helpers + **the turn-state half** (`prepare_memory_state`, `apply_explicit_memory_command`, `format_memory_context`, `memory_scope_candidates`/`_label`, `upsert_memory`, `clear_memories`, `delete_memory_by_id`) + **`memory_index`**: the `local_rag` memory-collection read path (`hash_text_embedding`/`normalize_vector`/`cosine_similarity` reused from `attachment_context`, `bm25_scores`, `_python_normalize_query`, `parse_embedding`, `load_candidate_rows`, `_search_db`, `search_memories_index`, read-only on `.local-rag/rag.sqlite3`) | **ported; wired via the loop's `WorkspaceContext`**; the turn-state half and the index provider are ported but unwired | byte-identical to oracle (`97187819…`), 194 keys; **index read path byte-identical (`memory_index_parity_probe`, 64 keys, `turn::differing = 7 of 8`)**; 400 tests; `docs/MEMORY_STORE.md` | **the bonus is not bounded — measured**, and the provider that reproduces it now exists, so the wiring must inject it rather than `None`. The `sqlite-vec` deployment still refuses (`VectorTableNotReadable`): `vec0` is a Python-loaded extension, absent from every shipped dependency set and from CI. **Wiring `chat_execution` is the remaining step** |
| workspace_file_lock (shared) | rust | py | `file_lock`: exclusive OS lock, `LockFileEx` retry / `flock` split | **ported** (used by `memory` and `mutation_gate`, both now reached from the loop) | in the same 206 tests, incl. cross-thread serialization | none |
| data_branch: projects read path (slice E1) | rust | py | `projects`: id validation, the `normalize_*` family, `read_project`, `public_project`, `list_projects` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`787f519d…`), 76 keys; `docs/PROJECTS_STORE.md` | none |
| tool_catalog (28 definitions + schema index + ordered trees) | rust | py | `tool_catalog`: `available_tool_definitions` + `tool_parameter_schemas` + `schema_for_tool` + `agent_tool_definitions` + `ordered_tool_definitions` / `ordered_tool_definition` (the `OrderedJson` trees the request body renders from; `python_json::loads` reads the asset order-preservingly) | **ported; consumed by the assembly render; not wired** | byte-identical asset + 18-key parity probe (`a2fa62de…`); asset round-trip test (parse + indent renderer reproduce the committed bytes); 231 tests; generated from the oracle rather than transcribed | the `mcp__` / external-MCP arms are injected; `None` reproduces the oracle escape hatch |
| search layers 1+2 (planning/normalize/rank/cache) | rust | py | `search`: `normalize_search_query_text` + `simplified_retry_query` + `should_search_for_query` + `search_intent` + `search_reason_for_query` + `search_queries_for` + `tavily_options_for_query` + `search_domain_filters` + `normalize_search_response` + `aggregate_search_rounds` + `rerank_search_results` + `search_result_score` + `domain_from_url` + `normalize_search_url` + `compact_search_tool_result` + `search_round_status` + `rounds_in_order` + `search_round_from_cache` + `should_retry_tavily_error` + the cache | **ported; not wired** | byte-identical to oracle (`a4259543…`), 97 keys; 231 tests | the HTTP layer is next: `search_tavily` + `search_tavily_with_retry` + a shared clock for the cache. Live path measured ~5% available, so a stub upstream is the verification route |
| search HTTP layer | rust | py | `search`: `search_tavily` + `search_tavily_with_retry` + `format_upstream_error` + `tavily_request_body_json` | **ported; transport unimplemented** | byte-identical to oracle (`b5077e1e…`), 113 keys; the transport is a `dyn Fn` parameter, so the whole path runs offline | WIRED: `search_provider.rs` (`TavilyConfig` + `reqwest::blocking` transport + the `web_search` callback) bound in `chat_tool_loop`; `reqwest` gained the `blocking` feature. `search_budget` and `progress_callback` remain out, with owners noted in the module docs |
| search prefetch (slices 1-4) + per-turn context + request-shaping leaves | rust | py | `search`: the three payload predicates + the two `format_*` + `search_multiple` (parallel); `context_taint`: **the whole module** — guard, both tables, alternation, `scan_text`, the hardening trio, `classify_request_messages`, `build_taint_report`, `report_is_tainted`, `taint_status`; `dynamic_context`: the reader of `searchContext` + `append_context_to_latest_user` + the per-turn helpers; `request_shaping`: the parallel hint, `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`, `has_image_content`, `count_payload_attachments`, plus `empty_memory_state` / `memory_scope_from_payload`; `context_engine`: **complete** — the token half (heuristics, estimators, breakdown, window lookup, budget plan, `token_trim`) and the identity half (`base_context_id` with a new `sha1` dependency, `build_context_diff`, `build_engine_diagnostics`); `context_manager`: **complete** — tool ordering, the count window, the token-aware pass, the counters and the merge; `model_router`: **complete** — auto-routing, the explicit path with the vision override, `cascade_plan`, `quality_gate`, `router_status`; plus the **consumed surface** of `edge_inference` (three query-shape patterns + the two payload readers) and `normalize_model_name`; `request_messages`: fail-closed `normalize_chat_messages`, both validators, the tool-call helpers and `MESSAGE_HARD_LIMIT` (the content expander is **injected** — the attachment path reads the file index); `budget_manager`: the **pure half** — the pricing table and cost arithmetic, the policy with its payload override, the in-memory `ToolBudget`, the scope key and the cost diagnostic, plus a byte-level float fix in `python_json` (`serde_json` renders a fraction-of-a-cent cost as `0.000049` where Python writes `4.9e-05`) | **slices 1-4 + the assembly ported; `context_taint` complete; not wired** | byte-identical to oracle (`84b6f0f6…` 2461 lines; `49e390b4…` 176 keys; `e3a065e9…` 39 keys; `7c7860c9…` 121 keys; `08a48874…` 209 keys; `347c7fb2…` 69 keys; `0e22a712…` 75 keys; `65c1e23c…` 21 keys; `adfbfd90…` 406 rows — the whole assembly, byte-identical); lib-test harness **fixed** by a committed `rust/.cargo/config.toml` rustflag (374 tests green) — see `continuation.md` | remaining: **the assembly is ported now** (`de2bd60a`, probe closed by `02976415`): the probe pair is byte-identical over **406 rows** (md5 `adfbfd90…`, every branch the corpus can reach, including the forced `tool_choice` object and the taint block's builder-owned order) — **not wired**, since the production chat path builds its own thinner body today. Remaining: the file **vector index** (`local_rag`, ~1,200 lines; refused via `NATIVE_FILE_VECTOR_INDEX_NOT_READY`/501 until then), the OS local-timezone resolution, `search_if_needed`, and a production caller for the assembly. The `edge_inference` edge-routing half (~430) is **not** in this closure |
| data_branch: projects branches (slice E2) | rust | py | `projects` branches + `file_cache`: `list_project_files` + `read_file_chunk` + `load_cached_file` + `project_file_cache_dir` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical to oracle (`5baaaba2…`), 29 keys; `docs/PROJECTS_STORE.md` | **data layer complete and wired**; the A1 gate test's rare failure led to a poisoning fidelity fix (see docs/PROJECTS_STORE.md) - narrowed, not closed |
| workspace_mutation_gate (slice A1) | rust | py | `mutation_gate`: fence read/assert, exclusive OS lock, durable generation counter, `mutation_scope` | **ported; wired — the loop's data writes pass through it** | byte-identical to oracle (`57e0ede2…`), 32 keys; 155 tests incl. a multi-thread serialisation case; `docs/WORKSPACE_MUTATION_GATE.md` | none |
| core_utils_scorer (slice C) | rust | py | `core_utils`: `query_tokens` + `score_chunk` + `utc_now_iso` + `latest_user_query` | **ported; not wired** | byte-identical to oracle (`2170fb90…`), 37 keys; 180 tests; `docs/RETRIEVAL_SCORER.md` | **oracle is non-deterministic here** (hash-seed dependent `[:80]` cap) — the port is deterministic by design; unblocks slice D and `search_files` |
| tool_branch: fetch_url | rust | py | `fetch_url`: `resolve_public_url` + DNS-time `ensure_public_address` + redirect revalidation + cache + HTML extract; gateway `locked_http_get` pins the resolved IP | **ported; wired via the loop's `FetchContext`** | byte-identical to oracle (`844b896f…`), 30 keys; `docs/FETCH_URL.md`; `chat_route_refuses_a_private_fetch_url_target`; locked TCP test sends the oracle Host/UA | trafilatura is not a production dependency, so the HTML-parser fallback is the shipped path; `gb18030` decode uses `encoding_rs` |
| tool_branch: search_files | rust | py | `search_files`: json_hybrid over `.file-cache` / `.projects/*/files` plus read-only `search_files_index` on `rag_items` collection `files` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical json_hybrid probe (`133204b5…`), 3423 chars; `docs/SEARCH_FILES.md`; `chat_route_searches_cached_files` | does **not** call `index_file_payload` (Python remains the RAG writer); `rag_vec` deployments skip the sqlite path |
| tool_branch: create_mindmap | rust | py | `mindmaps` + `generated_files`: SVG layout/render + `.generated/{id}.svg` | **ported; wired via the loop's `WorkspaceContext`** | byte-identical SVG probe (`be02bc5f…`), 5979 chars; `docs/MINDMAP.md`; `chat_route_creates_a_mindmap_svg` | unique-id creates, not a dual-written durable table |
| tool_branch: create_document | rust | py | `documents`: format aliases, section/table normalize, MD5 theme, OOXML zip + CID PDF | **ported; wired via the loop's `WorkspaceContext`** | content-model probe byte-identical (`a09bbde4…`), 4798 chars; `docs/CREATE_DOCUMENT.md`; `chat_route_creates_a_docx_document` | Office/PDF **bytes** are not python-docx/reportlab fingerprints; files are valid zip/`%PDF-` with CJK |
| tool_branch: create_pptx | rust | py | `presentations`: slide normalize, layout picker, MD5 theme, 16:9 OOXML zip | **ported; wired via the loop's `WorkspaceContext`** | content-model probe byte-identical (`d360f5e4…`), 3676 chars; `docs/CREATE_PPTX.md`; `chat_route_creates_a_pptx_deck` | pptx **bytes** are not python-pptx fingerprints; `create_presentation_from_text` stays a slides-skill path |
| tool_branch: python_eval | rust | py | in-process AST allowlist matching `PYTHON_EVAL_RUNNER` (no CPython child) | **ported; wired** | probe byte-identical (`b545a8f9…`), 3458 chars; `docs/PYTHON_EVAL.md`; `chat_route_evals_a_python_expression` | integers are i128 (overflow refuses huge factorials the CPython ints would accept) |
| tool_branch: browser_* | rust | py | safety gate + in-memory sessions + static HTML controller + **CDP engine sidecar** (`deepseek-browser`) reached over versioned gRPC | **ported; wired — the engine is selected when one answers `Status`** | safety probe byte-identical (`ae657d74…`), 2252 chars; **engine parity probe PASS with 0 differing HTML bytes** across six fixtures; `engine_live` (real Chromium, every declared action); `browser_engine_e2e` (**10 PASS** across the process boundary); `docs/BROWSER.md`; `docs/specs/browser-engine-sidecar.md` | no image carries Chromium and no CI lane runs the engine (staging 4); media/RAG snapshot writes stay Python; the static controller still does not fetch http; the download file name is a GUID under CDP `allowAndName` where the oracle reports the link's attribute |
| tool_policy_pure_core (layer 3a) | rust | py | `deepseek-policy::tool_policy`: guards, sanitizers, schema validation, metadata + capability tables | **ported; wired — the loop's executor attaches a policy per request** | byte-identical to oracle (`26c7723c…`), 158 keys; 30 unit tests; `docs/GATEWAY_TOOL_POLICY_PARITY.md` | none |
| tool_policy_engine (layer 3b) | rust | py | `deepseek-policy::tool_policy`: `ToolPolicy.evaluate`, denial output, diagnostics, taint, `AuditSink` + JSONL writer, args hash, `tool_policy_status` + `ToolPolicySettings` + `ToolAuditPaths` | **ported; wired — `ToolRoundExecutor::from_env` builds the main-chat profile** | byte-identical to oracle (`bae3a9e5…`), 257 keys; 82 unit tests; `docs/GATEWAY_TOOL_POLICY_PARITY.md` | payload-driven narrowing (capability/allowedTools/approvedTools) and the context-taint firewall are not ported; config env reader not ported |
| policy_url_route_parity | rust | py | `url_guard::validate_url_access` delegates to `tool_policy::evaluate_url_safety` | **aligned (route now matches the oracle); flag off** | `guard::*` keys compare the route against the oracle over all 59 URL cases in the same 257-key diff | `path_guard` still a different (containment) operation — needs its own slice; do not enable `DEEPSEEK_RUST_POLICY` before that |
| mcp_jsonrpc | rust | py | `mcp_hub`: initialize/ping/tools/list/call + resources/prompts | **wired on native `/mcp`** | `mcp_initialize_and_tools_call_are_native`; `python_eval` `2+2` → `4`; `docs/MCP_HUB.md` | external `mcp__*` bridging refused as tool error; production HTTP still Python |
| data_route: /api/reminders (+ /due) | rust | py | `deepseek-gateway::data_routes` over `deepseek-policy::reminders`; registered ahead of the Go `/api/*` catch-all | **wired on the native edge; mutating actions gated** | `tests/data_routes.rs` (9 real-HTTP cases through `create_production_app`): gate refusal + no file, the flip writing for real, fence generation `2`, byte-identical refused delete, due-marking persistence and second-poll silence, oracle 400s, and the auth boundary | **`list` is served for real; `create`/`delete`/`due` are refused with `NATIVE_REMINDERS_WRITE_NOT_OWNED` until the `reminders_store` cutover** (declared, cutover 4.9.4) — the same gate the tool loop applies |
| memory_v3_schema (projection layer) | rust | py | `deepseek-policy::memory_schema`: `public_scope`/`storage_scope`/`public_type`/`legacy_category`/`normalize_source_ref`/`public_source`/`public_confidence`/`public_memory`, the policy pair (`assert_memory_safe`/`readable_scopes`/`skill_can_read_memory`), and the store/search operations (`list`/`add`/`edit`/`delete`/`search_memories`/`memory_context_for_skill`) over the existing `deepseek-policy::memory` store | **ported; wired via `/api/memory`** | byte-identical to oracle (`d0bbb075…`), **164 keys / 18 935 chars**, shown not blind; 15 unit tests; `docs/DATA_ROUTES.md` | the store is the *same* `.memory/memories.json` the chat turn writes — one authoritative writer, gated identically |
| data_route: /api/memory family | rust | py | `data_routes`: `GET`/`POST /api/memory` (list/add/clear/delete/deletebyid), `DELETE`/`PATCH /api/memory/{id}`, `GET /api/memory/search`, `POST /api/memory/conflicts` | **wired on the native edge; mutations gated, reads served** | `tests/data_routes.rs` (10 cases): all four POST actions + DELETE + PATCH refused with no file, the flip writing for real (generation **4**, two saves), the 409 conflict path checked **before** the gate, `replaceIds`, and the bare-`int()` limit 400 | three measured corrections recorded in `docs/DATA_ROUTES.md`: identical content is **not** a conflict, one `add_memory` bumps the generation **four** times, and `public_confidence("nan")` is `1.0` |
| rag_hot_path | rust | py | deepseek-rag | no | document-prep sidecar | query/index ownership |
| tool_sandbox | rust | py | policy crate | no | none | sandbox execution |
| url_path_capability_policy | rust | py | deepseek-policy | **URL aligned; path partial** | crate tests + 59-case route parity | `path_guard` root-containment vs oracle argument scan — separate slice |
| fastcdc_chunk_pipeline | rust | py | deepseek-backup / storage | no | library tests | worker dispatch |
| age_crypto | rust | py | backup-crypto | helper binary | helper tests | production key path |
| s3_minio_streaming | rust | py | deepseek-storage s3 | optional feature | local MinIO e2e (not this HEAD) | signed ops + kill/takeover |
| object_set_validation | rust | py | deepseek-storage | library | frozen corpus | production writes |
| receipt_commit_verification | rust | py | storage + proof | library | frozen corpus | provider-backed Receipt |
| backup_restore_data_movement | rust | py | storage/transfer | no | library / some MinIO | Go claim → Rust effect |
| federation_ciphertext_transfer | rust | py | deepseek-federation / transfer | no | journal corpora | Gate A leases |
| ed25519_sign_verify | rust | py | proof / federation | library | corpora | Rust-only key custody in prod |
| evidence_crypto_verifier | rust | py | deepseek-proof | library | corpora | not a production signer |
| federation_private_key_custody | rust | py | federation custody | library | crate tests | no Go private keys |
| transfer_jobs | rust | py | deepseek-transfer | no | frozen phase corpora | worker RPC dispatch |
| data_plane_effect_journal | rust | py | worker sqlite | qualification | local worker tests | TLS+signed ops |
| worker_checkpoint | rust | py | worker / transfer checkpoints | qualification | local | process-kill |
| config_runtime_lifecycle | go | py | deepseekd lifecycle | shadow | go tests | production mode |
| control_api | go | py | `/internal` + gateway proxy isolation | no public `/api` | gateway tests | authenticated proxy |
| policy_crud | go | py | control store domain; `POST`/`PATCH`/`DELETE /api/workspace/backup-policies[/{id}]` through the append-only operator journal, delete as a terminal tombstone | 集成通过 locally | `go/internal/policy` oracle fixture (81 cases), `backup_policy_writes_test.go`, `operator_mutation_test.go`, `TestBackupPolicyRustGatewayToGoPolicyCrud` (real gateway → real deepseekd) | Rust half not locally linked; browser observation; no production cutover |
| target_registry | go | py | control store domain | shadow | store tests | cutover |
| backup_scheduler | go | py | scheduler shadow | digest parity | shadow tests | mutation denied |
| a2a_task_lifecycle | go | py | `go/internal/a2a`, `a2a_control.rs`, versioned mTLS gRPC | native edge with explicit control configuration | 9 real process checks, race tests, 31 message + 12 SSE oracle cases, Python writer denial | qualification only; exact local Go coverage 95.003059%; full parity and production cutover remain |
| agent_dag_scheduler | go | py | internal/agent | shadow | fuzz/parity | production DAG |
| job_controller | go | py | action coordinator | qualification | go tests | cutover + signed ops |
| lease_fencing | go | py | writer + action leases | shadow/qual | go tests | live Rust lease install |
| action_journal | go | py | schema v5–v7 | shadow/qual | this slice | v30 replay; verifiers |
| wave_scheduler | go | py | resilience/wave | shadow | digest parity | cutover |
| risk_lifecycle | go | py | resilience/risk | shadow | digest parity | cutover |
| capacity_forecast | go | py | store forecast domain | shadow | store tests | cutover |
| maintenance_controller | go | py | shadow | shadow | tests | cutover |
| resilience_coordinator | go | py | coordinator qualification | no | go tests | provider settlement |
| federation_peer_registry | go | py | store peer domain | shadow | tests | Gate A |
| federation_session_lifecycle | go | py | store session | shadow | tests | Gate A |
| ingress_grant_lifecycle | go | py | store grant | shadow | tests | Gate A |
| federated_transfer_journal | go | py | store transfer | shadow | tests | Rust data path |
| dr_orchestration | go | py | shadow | shadow | tests | cutover |
| health_readiness | go | py | deepseekd `/healthz` + `/api/control/status` + `/api/config` subset | **public read subset wired on deepseekd** | `TestPublicConfigIsAGoOwnedSubset`; `docs/GO_PUBLIC_API.md` | production edge still Python; OCR/RAG/budget stay 501 |
| offline_eval_oracle | python | python | allowed | n/a | pytest/evals | must stay non-prod |
| migration_release_tooling | python | python | allowed | n/a | scripts | must stay out of images |
| browser_ui | ts | ts | frontend/ | `/` via Python today | frontend CI | serve from rust edge |

## 2. Python capability packages still on the production path

`deepseek_infra/infra/` is the production package. Native crates exist beside
several of these, but HTTP/CLI still import Python.

| Package | Production entry | Target owner | Native stand-in | Status |
| --- | --- | --- | --- | --- |
| gateway | `web/routes/chat.py`, `openai_api.py` | rust | deepseek-gateway | chat + SSE + MCP hub + A2A JSON-RPC; production HTTP still Python |
| agent_runtime | A2A routes, agent runs | go+rust | proto/agent, go/agent, go/a2a | A2A durable lifecycle over mTLS verified locally; DAG remains shadow |
| rag | `routes/rag.py`, local_rag | rust | deepseek-rag | library |
| tool_runtime | tools, OCR, documents, slides | rust | none equivalent | **unmigrated** |
| observability | traces, metrics, `/api` status | rust+go | gateway observability | partial |
| mcp | `POST /mcp`, registry, executor | rust | deepseek-mcp | codec/library |
| evaluation | evals/ | python oracle | n/a | allowed if offline |
| data | projects, reminders | rust reads/stores; project writer ownership pending | native reminder API and project read facade | reads wired; project writes refused pending explicit ownership and cleanup |
| workspace | backup/DR/federation/resilience HTTP | go+rust | store + worker + proof | Python HTTP |
| automation | `routes/automation.py` | go | none | **unmigrated** |
| browser | browser controller | rust worker | **CDP sidecar wired through the gateway tool loop** | image/CI lane for Chromium (staging 4); media/RAG snapshot writes |
| media | `routes/media.py` | rust | `GET /api/media/{media_id}/segments` read-only | **未完成** — that GET is 集成通过; create, list, item, and process remain Python |
| memory | `routes/memory.py` | go/rust | none | **unmigrated** |
| skills | `routes/skills.py` | rust/go | `skills_routes.rs` — **all 52** actions dispatched; `run` with an API key is a named refusal | **集成通过** for the registry, packs, security reviews and the overview, the offline runner, the whole run journal, dry runs, the catalog, the version family (list, migration plan, both rollbacks), the eval case store and the eval report engine. `run` with an API key is the one branch that is dispatched but still `501 NATIVE_SKILLS_ACTION_NOT_READY` |
| diagnostics | evidence assembly | python tooling | n/a | allowed as release tool |
| native_runtime | mechanical denial | both | authority.py | enforcement helpers |
| rust_core | optional sidecar clients | rust | crates | not production owner |

## 3. Public and control HTTP (from route modules)

Python registers these today in `deepseek_infra/web/`. Rust gateway lists the
public inventory but does not implement behavior.

| Surface | Current entry | Target | Native | Retire when |
| --- | --- | --- | --- | --- |
| `/v1/chat/completions`, `/v1/models` | `routes/chat.py` | rust edge | non-stream + SSE wired (tool rounds refuse) | tool-round parity + browser parity |
| `/api/chat` (JSON/NDJSON), search | `routes/chat.py` | rust edge | Native JSON for false/missing `stream`, NDJSON for streaming, shared tool rounds, request-local memory suggestions, forced-search prefetch/cache/memo/budget/citations/diagnostics and cascade draft/gate/zero-tool judge/refine (including Ollama and final-result replay) | 30 Windows real-router cases; production input `44c20f629c64` passes full fmt/strict Clippy/1376 default tests. Current `8548646e6de8` changes only the clock assertion and passes strict Clippy, complete all-feature coverage 82.001932%, 1393 tests plus one original ignored, and 1394-test inventory/LCOV. Evidence: `artifacts/native-20261009-chat-cascade-evidence.json`. Agent mode, edge inference and gateway-attempt/semantic-cache/cost/trace parity remain open; production retirement requires these branches and real browser/provider/platform acceptance. |
| `/api/title` | `routes/chat.py` | rust edge | **wired** — the oracle's request body, sanitiser, rate window and error envelopes | `tests/title_route.rs` (7 real-HTTP cases against a scripted upstream: the body the socket received, the blank-message early return, the missing-key 400, the 503→502 cap, the 13th-call 429, and the auth boundary); `title_parity_probe.py` **PASS** over 7 sections (26 sanitiser cases, 9 truncations, 6 bodies, 8 model selections, 7 responses, 7 upstream errors); `docs/NATIVE_PUBLIC_ROUTES.md` | the probe measured one real bug: `choices[0].message.content == null` returned `"None"` instead of `""` |
| `/api/taint` | `web/server.py` | rust edge | **wired** — the ported `context_taint::taint_status` block, with `ContextTaintSettings::from_env` | `tests/data_routes.rs` (2 cases: the block's fields and the oracle's `(4, 200)` segment clamp) | the block is the same one `/api/config` embeds; `/api/config` itself is still Go/Python |
| `/api/download` | `routes/downloads.py` | rust edge | **wired** — `generated_files::resolve_generated_file` + `download_descriptor`; the id rule and the per-type MIME/name mapping stay in the policy crate | `tests/download_route.rs` (6 real-HTTP cases through `create_production_app`: the bytes on the wire are the bytes on disk for all five registered types, the `inline` rule for SVG — the web layer's `1/true/yes/on` parse — a traversal attempt leaking nothing, the unknown-id 404 envelope, and the auth boundary); `generated_files` unit test pins the oracle's six MIME/name pairs | `/api/file-page-text` and `/api/file-page-search` are wired (see those rows). `/api/file-page-image` and `/api/file-page-layout` are still the proxy's 503 |
| `/api/file-source` | `routes/files.py` | rust edge | **集成通过** — `file_routes::cached_file_source` + `content_disposition_header` + `original_file_media_type`; the id rule, the project-scoped directory and the media-type ladder stay in the policy crate | `tests/file_source_route.rs` (6 real-HTTP cases through `create_production_app`, re-measured 2026-09-21); `file_routes_parity_probe.py` **PASS** over **345 cases** on 2026-09-26 (the filename/media corpus is inside that run) | not **完成切换**. `/api/file-page-image` and `/api/file-page-layout` are wired; the PNG comes from `pdftoppm`, not MuPDF's pixmap bytes. `/api/file-text` and `/api/project-files` are wired on this edge; project metadata is written only after `python_disabled` |
| `/api/file-reader`, `/api/file-chunk` | `web/server.py` | rust edge | **集成通过** — `file_routes::file_reader_window` + `file_chunk` + `reader_positive_int` (Python `int()`, so `"1.5"` is refused). Re-ran `tests/file_reader_route.rs`: 6 passed on 2026-09-26 | `file_routes_parity_probe.py` **PASS** over **345 cases**, including 56 window shapes and 42 chunk lookups | not **完成切换** |
| `/api/file-page-text` | `web/server.py` `api_file_page_text` → `files.file_page_text` | rust edge | **集成通过** — `POST /api/file-page-text` on `create_production_app`, ahead of the Go catch-all. Policy: `deepseek-policy::file_routes::file_page_text`. Read-only: success and refusal leave the cache bytes and mtimes unchanged. User-visible: `{ok, file, page:{index, pageCount, text, hasText}}`; falsy `page` is 1; a page past the raised count clamps; a page with no text is the chunk split; `"1.5"` / `"True"` / `"x"` are `400 Invalid page` and write nothing; a bad id is `400`; a missing index is `410`; a bad project id is `400` and does not read the global file; no token is `401` | `tests/file_page_text_route.rs` 7 page-text cases passed; `file_routes` unit tests passed; `file_routes_parity_probe.py` **PASS** 345 cases (130 of them `page_texts`) against unmodified `files.file_page_text`. `docs/NATIVE_PUBLIC_ROUTES.md`. Local only, HEAD `79aba745` plus this uncommitted slice | not **完成切换**. `page_texts_for_cache` / PDF rendering for `/api/file-page-image` and `/api/file-page-layout` are still Python |
| `/api/file-page-search` | `web/routes/files.py` `api_file_page_search` → `files.file_page_search` | rust edge | **集成通过** — `GET /api/file-page-search` on `create_production_app`. Casefold is Python's `str.casefold` (`ß` → `ss`), and the match index is into the folded text. A blank query is `400 Search query is required` and writes nothing. Missing token is `401` | `tests/file_page_text_route.rs` search case passed; unit test `file_page_search_is_casefolded_and_refuses_a_blank_query` passed; parity probe `page_search` is inside the **345** PASS cases against unmodified `file_page_search` | not **完成切换**. Image and layout are separate rows |
| `/api/file-page-image` | `web/routes/files.py` `api_file_page_image` → `files.file_page_image` | rust edge | **集成通过**. `GET /api/file-page-image` on `create_production_app` renders one PDF page to PNG with `pdftoppm`, writes `{id}.page-{n}-{scaleKey}.png` beside the source, and returns `image/png` plus `X-File-Page` and `X-File-Page-Count`. A second request reads that cache. A non-PDF is `415 unsupported_file`. A bad page or scale is `400 invalid_payload`. A missing token is `401`. Those refusals do not add a cache file and do not change the index bytes. Layout word boxes are a separate route | `tests/file_page_render_route.rs` 1 passed on 2026-09-26; `file_page_render_probe.py` **PASS** 3 PDFs against unmodified `render_pdf_page_png` for page number, PNG signature and pixel size within 4 pixels (`artifacts/file-page-render.json`). Local only, HEAD `79aba745` plus this uncommitted slice | not **完成切换**. The PNG encoder is `pdftoppm`, so the bytes are not MuPDF's `pixmap.tobytes("png")`. Embedded non-Helvetica fonts are not metrically matched |
| `/api/file-page-layout` | `web/routes/files.py` `api_file_page_layout` → `files.file_page_layout` | rust edge | **集成通过**. `GET /api/file-page-layout` returns the word boxes for one PDF page and does not write a cache file. For unembedded Helvetica the boxes match PyMuPDF `get_text("words")`: Adobe widths, the 1.075 / -0.299 em box, and a new block when the gap below the previous line exceeds 0.15 em. A non-PDF is `415`. A non-numeric page is `400` and writes nothing | `pdf_page::tests::helvetica_words_match_the_mupdf_boxes` passed; `file_page_render_probe.py` **PASS** compares `simple.pdf`, a PyMuPDF-drawn page and a two-page file with unmodified `render_pdf_page_layout`, including the clamped second page of a one-page file | not **完成切换**. Courier is monospaced; other embedded fonts still use the Helvetica advances |
| `/api/file-text` | `web/server.py` `api_file_text` → `files.extract_uploaded_file` | rust edge | **集成通过** for text, HTML, DOCX, PPTX, XLSX, selectable PDF text, EPUB, and OCR of images and textless PDFs. `POST /api/file-text` parses multipart, writes `.file-cache/{id}.json` and `{id}.source`, and the same process reads them back through `/api/file-source` and `/api/file-reader`. PPTX numbers slides by sorted `slideN.xml` position, including empty slides that do not appear in the text. XLSX follows the installed openpyxl path (`read_only`, `data_only`): shared strings, inline strings, numbers, bools and styled date serials. A package openpyxl cannot load, including a ZIP without `[Content_Types].xml`, is `422 Invalid xlsx file`. Selectable PDF text follows pypdf plain mode, including `[PDF page N]` labels and a newline when the baseline moves; `pageCount` includes blank pages that contribute no text. A truncated PDF is `422 Could not extract text from this PDF`. A parsed PDF with no text is `422 ocr_required` when OCR is off. EPUB chapters are lexicographic, `nav.xhtml` and `toc.xhtml` are omitted, and a non-ZIP is `422 Invalid epub file`. Empty body, empty file, NUL binary, an image with OCR off, a nav-only EPUB, and corrupt DOCX/PPTX/XLSX/PDF/EPUB match the oracle and write nothing. With OCR on, an image is cached as kind `image` and a textless PDF is cached with `[PDF 第 N 页 (OCR)]` labels. A blank page is `422 ocr_empty` and writes nothing. A missing engine is `415 ocr_unavailable`. The probe and the route tests set `OCR_FORMULA_CMD=cmd /c exit 1` because pix2tex output is not stable across processes; production still selects pix2tex when that variable is unset and the binary is on PATH. cv2 preprocessing and formula-region snippets are not ported. | `tests/file_text_route.rs` 10 passed on 2026-09-26; `file_text_parity_probe.py` **PASS** 31 cases against unmodified `extract_uploaded_file` (`artifacts/file-text-parity.json`). The probe stubs only `local_rag.index_file_payload` so it does not touch the host RAG database. `cargo clippy -p deepseek-policy --all-targets -- -D warnings` exits 0 | not **完成切换**. The sqlite file index (`index_file_payload`) is not written here, so this route does not dual-write `.local-rag`. `/api/project-files` is a separate row |
| `/api/project-files` | `web/routes/workspace.py` `api_project_files` → `projects.add_project_files` | rust edge | **集成通过** for attaching an uploaded file to an existing project. `POST /api/project-files?projectId=` is registered ahead of the Go catch-all. While Python owns `project_metadata_store` (default, `python_authoritative`, `go_authoritative`, `shadow`) the route answers `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` and does not create `.projects/<id>/files` or change `project.json`. With `DEEPSEEK_RUNTIME_MODE=python_disabled`, a legal text upload writes `.projects/<id>/files/{fileId}.json` and `.source`, updates `project.json`, and `GET /api/workspace/projects/<id>` reads the document back. A missing token is `401`. An unsupported file is `415` and leaves `project.json` unchanged. A missing project is `404` and does not create a directory. The route does not call `index_file_payload`, so `.local-rag` stays untouched. Other project mutations (`create`, `rename`, `delete`, workspace child writes) remain `501` | `tests/project_files_route.rs` 3 passed on 2026-09-26 through `create_production_app`; `cargo test -p deepseek-gateway --lib the_memory_store_owner_is_the_mode_and_not_go_control` pins `may_write_native_store("project_metadata_store")` to `python_disabled` only. Local only, HEAD `79aba745` plus this uncommitted slice | not **完成切换**. Python `add_project_files` remains the writer until the process is actually started with `python_disabled`. Document ids and `createdAt` are fresh entropy, so the record is not byte-identical to a Python write of the same upload. The RAG file index is not written |
| `POST /api/workspace/projects/{project_id}/saved-items`, `PATCH/DELETE .../saved-items/{saved_id}` | `workspace.py` → `saved_items.create_saved_item` / `update_saved_item` / `delete_saved_item` | Rust edge, `.projects/{id}/saved-items.json` and project `updatedAt`. Writes only when `project_metadata_store` is writable (`DEEPSEEK_RUNTIME_MODE=python_disabled`) | **集成通过**. The collection POST no longer returns `501`. While Python owns the store, POST, PATCH, and DELETE are `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` and do not create `.projects`. With `python_disabled`, a legal create returns `{"ok":true,"savedItem":...}`, mints `save_` plus 16 lowercase hex, and `GET .../saved-items` lists it. Tags keep the first spelling. An unknown purpose becomes `reference`. Empty type is `400 Unsupported saved item type` and does not rewrite. A missing project is `404 Project not found` and does not create a directory. PATCH of a missing id is `404` and does not rewrite. DELETE of a missing id is `{"ok":true,"deleted":0}` and does not rewrite. Empty body and a non-object are `400`. The loader keeps the last 1000 rows. A 413 `Too many saved items` and a 404 for an id outside that window leave the file bytes unchanged, including a hidden prefix. Deleting a visible id rewrites only that window. Other methods on the item path are `405 {"detail":"Method Not Allowed"}` with `Allow: PATCH, DELETE`. `GET` list filtering is unchanged | `tests/data_routes.rs` 6 passed on 2026-10-04 through `create_production_app`, including `project_saved_item_writes_match_python_and_refuse_without_a_store_change` compared with a live create/update/delete oracle. `production_runtime_inventory.py --write` then `pytest tests/test_production_runtime_inventory.py` passed. Scanner python-only routes 110 → 108. `workspace.py` 33 → 31. `automation.py` 2, `media.py` 6, `backup_governance.py` 62, `status.py` 1. Authority `python`. Evidence log `saved-item-routes-2.log` | not **完成切换**. Python `saved_items._write_items` still writes while Python is authoritative. Artifact create, update, and delete are the next row. Artifact download stays Python. The domain was already declared and was not flipped |
| `POST /api/workspace/projects/{project_id}/artifacts`, `PATCH/DELETE .../artifacts/{artifact_id}` | `workspace.py` → `artifacts.register_artifact` / `update_artifact` / `add_artifact_version` / `delete_artifact` | Rust edge, `.projects/{id}/artifacts.json` and project `updatedAt`. Writes only when `project_metadata_store` is writable (`DEEPSEEK_RUNTIME_MODE=python_disabled`) | **集成通过**. The collection POST no longer returns `501`. While Python owns the store, POST, PATCH, and DELETE are `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` before the body is parsed and do not create `.projects`. With `python_disabled`, a legal create returns `{"ok":true,"artifact":...}`, mints `art_` plus 16 lowercase hex, and `GET .../artifacts` lists it. An empty type with `notes/kept.md` is `markdown`. Empty path is `400 Artifact path is required`. A `..` path is `400 Artifact path must not escape the workspace`. `notes/file.bin` is `400 Unsupported artifact type`. A missing project is `404 Project not found` and does not create a directory. An invalid artifact id is `400 Invalid artifact id`. PATCH of a missing id is `404` and does not rewrite, even when the path would also be illegal. A truthy `path` calls `add_artifact_version` and increments `version`. Any other PATCH calls `update_artifact` and changes `title` and `source` only. DELETE of a missing id is `{"ok":true,"deleted":0}` and does not rewrite. Empty body and a non-object are `400`. The loader keeps the last 500 rows in file order; the list sorts by `updatedAtMs`. A 413 `Too many artifacts` (`upload_too_large`, before the path check) and a 404 for an id outside that window leave the file bytes unchanged, including a hidden prefix. Deleting a visible id rewrites only that window. Other methods on the item path are `405 {"detail":"Method Not Allowed"}` with `Allow: PATCH, DELETE`. `GET` list filtering is unchanged. `GET .../download` stays Python | `tests/data_routes.rs` 7 passed on 2026-10-04 through `create_production_app`, including `project_artifact_writes_match_python_and_refuse_without_a_store_change` compared with a live register/update/add-version/delete oracle. `production_runtime_inventory.py --write` then `pytest tests/test_production_runtime_inventory.py` passed. Scanner python-only routes 108 → 106. `workspace.py` 31 → 29. `automation.py` 2, `media.py` 6, `backup_governance.py` 62, `status.py` 1. Authority `python`. Go domains `[]`. Evidence log `artifact-routes-2.log` | not **完成切换**. Python `artifacts._write_artifacts` still writes while Python is authoritative. Artifact download stays Python. The domain was already declared and was not flipped. Home is the next row |
| `GET /api/workspace/home` | `workspace.py` → `workspace_home.workspace_home` | Rust edge, read-only aggregate over the existing project, memory, skill-run, media, automation, export, and evidence paths. No directory is created | **集成通过**. Authenticated GET on `create_production_app` matches a live `workspace_home` call on the same root and `APP_VERSION` `4.8.0`. The ten modules stay `ready`. `recent.automations` is the truncated run window. `counts.automations` is the definition count. `counts.automationRuns` is `len(list_runs(limit=clamped))`. Other counts are the full collected lengths. Artifacts, saved items, and skill runs keep the string timestamp sort. A bad saved-item type is skipped and does not fail the response. Missing or invalid JSON is an empty collection. Query `""` and `"0"` are 8. `"-3"` is 1. `"100"` is 50. A non-integer is `500 {"error":"Server error","code":"internal"}` before a read. Missing auth is the nested `UNAUTHORIZED` 401. `HEAD` is empty 200. Other methods are `405` with `Allow: GET, HEAD`. The route is served while Python is authoritative and is not 501. An empty root leaves `.projects`, `.automation`, `.media`, `.memory`, `.generated`, and `docs` absent. Evidence is `{path, present, status}` for the resolved application root `docs/evidence/ga-v4.8.0.json` as a POSIX path, and the file is not created | `tests/data_routes.rs` 31 passed on 2026-10-04 through `create_production_app`, including `workspace_home_matches_python_and_leaves_missing_stores_absent` and the existing `project_` cases. `production_runtime_inventory.py --write` then `pytest tests/test_production_runtime_inventory.py` passed (1 passed in 0.69s). Scanner python-only routes 106 → 105. `workspace.py` 29 → 28. `automation.py` 2, `media.py` 6, `backup_governance.py` 62, `rag.py` 3, `edge.py` 2, `mcp.py` 1, `status.py` 1. Authority `python`. Go domains `[]`. Rust domains 12. Python domains 34. Evidence log `workspace-home.log` | not **完成切换**. The stores the aggregate reads stay Python-owned. No domain was flipped. Provenance and artifact download stay Python |
| `/api/reminders`, `/api/reminders/due` | `web/server.py` | rust edge | **wired** — read served; mutations gated on the `reminders_store` cutover | `docs/DATA_ROUTES.md`; 9 real-HTTP cases |
| `/api/memory` family | `routes/memory.py` | rust edge | **wired** — reads served; mutations gated on the `memory_store` cutover | `docs/DATA_ROUTES.md`; 10 real-HTTP cases; byte-identical parity probe |
| `/mcp`, `/api/mcp/*` | `routes/mcp.py` | rust | no | MCP corpus on edge |
| `/api/projects`, `/api/workspace/projects` reads and child lists | `routes/workspace.py` | rust edge | **wired**: legacy list/get; Workspace list/detail/conversations/saved-items/artifacts | 14 Python storage fixtures / 98 comparisons; 5 added production-router tests; 23 real Rust process checks. Project writes, upload and artifact delivery remain unfinished; `docs/PROJECTS_STORE.md` |
| `/.well-known/agent-card.json`, `/a2a` | agent_runtime | rust | **wired** — cards + tasks + native chat runner + SSE/resubscribe | `docs/A2A_HUB.md`; real HTTP native-runner test + 12 Python oracle event cases + 9 real Go/Rust process checks; Go-only task/chunk persistence; remaining peer clients, metrics, migration, full parity and production cutover |
| `/api/workspace/*` backups/DR/resilience | workspace + backup_governance | go `/api` via edge | isolation only | Go control API |
| `POST/GET /api/workspace/backup-retirements`, `GET /api/workspace/backup-retirements/{job_id}` | `backup_governance.py` → `backup_retirement.create_copy_retirement_job` / `get_copy_retirement_job` / `list_copy_retirement_jobs` | Rust edge, same sqlite file `.backup-retirements/retirements.sqlite3` | **集成通过** for the HTTP job row only. `create_production_app` returns the `requested` job (`retire_` + 16 hex, UTC `Z` seconds, default reason `api-retirement-request`, empty `simMetadata`, `bytesReclaimed` 0). Writes require `DEEPSEEK_RUNTIME_MODE=python_disabled`; otherwise `409 NATIVE_BACKUP_RETIREMENT_WRITE_NOT_OWNED` and the directory is not created. A missing database reads as `{jobs:[]}` / 404 without mkdir. An existing Python database is read-only and is not migrated. Invalid stored `sim_metadata` is `500 Server error`. Physical GC and `execute_copy_retirement_job` are not this slice | `tests/backup_retirement_routes.rs` 4 passed and 2 lib tests on 2026-10-03 through `create_production_app`. Inventory regenerated after this row and the backup-runs row: python-only routes 125, `backup_governance.py` 65, authority still `python`, go domains `[]` | not **完成切换**. The Python worker can still update the same table, so the domain stays out of `DECLARED_NATIVE_DATA_DOMAINS` and `RUST_DATA_DOMAINS`. Drain, catalog, retention, scrub, restore, and the rest of backup governance remain Python-only on the public route list |
| `GET /api/workspace/backup-runs` | `backup_governance.py` → `backup_scheduler.list_runs` | Rust edge, read-only `.backup-scheduler/scheduler.db` | **集成通过** for the list. Limit 50, `policyId` filter, blocked phases expose `nextRetryAt` and `blockedReason`. Missing database is `{runs:[]}` and the directory is not created. Existing file is not migrated and journal mode is not changed | `tests/backup_run_routes.rs` 1 passed on 2026-10-03 through `create_production_app` | not **完成切换**. The Python backup worker remains the writer of `backup_runs`. Creating or claiming a run is still Python |
| `GET /api/workspace/disaster-recovery/replication` | `backup_governance.py` → `backup_replication.list_jobs` | Rust edge, read-only `.backup-replication/*.json` | **集成通过** for the list. Limit 100, `policyId` and `backupId` filters, stored objects returned unchanged, missing directory is `{jobs:[]}` and is not created. Invalid UTF-8 is `500`. Invalid JSON is skipped. `phase` is not a filter | `tests/backup_replication_routes.rs` 2 passed on 2026-10-03 through `create_production_app` | not **完成切换**. Creating and advancing replication jobs stays on the Python worker. The directory is not a declared native domain |
| `GET /api/workspace/disaster-recovery/drills/{restore_id}` | `backup_governance.py` → `backup_recovery_drill.get_recovery_drill` | Rust edge, read-only `.restore-staging/{id}/drill-result.json` then `drill-running.json` | **集成通过** for the read. Id must be `restore_` plus an alphanumeric suffix. Missing session and missing result are the Python 404s. Bad JSON is 400. Invalid UTF-8 is 500. The directory is not created. Other methods on the one-segment pattern are forwarded to the Go proxy | `tests/backup_drill_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. `POST .../drills/run` remains Python-only | not **完成切换**. `POST /drills`, schedule, and `POST /drills/run` still execute the drill on Python. The session directory is not a declared native domain |
| `GET /api/automation/templates` | `automation.py` → `registry.list_templates` | Rust edge, in-memory `BUILTIN_TEMPLATES` | **集成通过**. Body is `{"ok": true, "templates": ...}` and matches a live Python `list_templates()` call, including the six builtin ids and nested automation fields. `.automation` is not created. `POST /api/automation/templates` is 405. Creating from a template is the next row | `tests/automation_template_routes.rs` catalog case passed again on 2026-10-04 through `create_production_app` (2 tests in that file). The 2026-10-03 catalog inventory was python-only routes 122, `automation.py` 8 | not **完成切换**. The catalog does not own `.automation` |
| `POST /api/automation/templates/{template_id}` | `automation.py` → `registry.create_from_template` | Rust edge, `.automation/automations.json`. Writes only when `DEEPSEEK_RUNTIME_MODE=python_disabled` | **集成通过**. The body is parsed before the template lookup. An unknown template is `404 Automation template not found` and does not create `.automation`, including while Python owns the store. A known template while Python is authoritative is `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` and does not create or change the file, including when `overrides` would later fail validation. `projectId` uses Python's `or ""`. A JSON object `overrides` then replaces keys, so `overrides.projectId` wins. `create_automation` normalises with `touch=True` and no existing row, requires a non-empty project, returns `413 Too many automations` at 500 visible rows without rewriting, and returns `409 Automation already exists` for a duplicate id without rewriting. A file with 501 valid rows still presents 500, so create is 413 and the hidden prefix stays. A legal create returns `{"ok": true, "automation": ...}`, mints `auto_` plus 16 lowercase hex, and `GET /api/automation/{id}` returns that same body. `_touch_project` updates `updatedAt`. `.automation/history.json` is not created. Empty body, non-object, bad JSON, bad Content-Length, a length above 2000000, invalid UTF-8 500, and a missing token 401 leave the store untouched. Any other method is `405 {"detail": "Method Not Allowed"}` with `Allow: POST` | `tests/automation_template_routes.rs` 2 passed on 2026-10-04 through `create_production_app`, compared with a live `create_from_template` oracle. `tests/automation_definition_routes.rs` 2 passed again. `production_runtime_inventory.py --write` then `pytest tests/test_production_runtime_inventory.py` passed. Scanner python-only routes 112 → 111. `automation.py` 4 → 3. `backup_governance.py` 62, `workspace.py` 33, `media.py` 6, `status.py` 1. Authority `python`, go domains `[]`, python domains 34, rust domains 12 | not **完成切换**. `.automation` is not a declared native domain, and Python `_write_automations` still writes while Python is authoritative. `GET` and `POST /api/automation` share one path and stay unregistered. `POST /api/automation/{automation_id}/run` is the run row below |
| `GET /api/automation/{automation_id}/runs` | `automation.py` → `history.list_runs` | Rust edge, read-only `.automation/history.json` | **集成通过**. Body is `{"ok": true, "runs": ...}` and matches a live `list_runs` call using the route's `int(limit or 100)`. Records are normalised. Invalid records are skipped. The last 2000 accepted records are kept before the automation filter and the `startedAtMs` descending sort. Missing, empty, and `"0"` limits return 100; a negative limit returns every loaded run; above 2000 clamps to 2000; a non-integer is `500 Server error` before the id is validated. Invalid id is `400 Invalid automation id` and does not create `.automation`. A missing file is `{runs: []}`. Invalid UTF-8 is 500. `projectId` and `status` query keys are ignored. The file is not written | `tests/automation_run_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: python-only routes 121, `automation.py` 7, `backup_governance.py` 63, authority `python`, go domains `[]` | not **完成切换**. `POST /api/automation/{automation_id}/run` is the next row. The collection `GET/POST /api/automation` still runs on Python. `.automation` is not a declared native domain |
| `POST /api/automation/{automation_id}/run` | `automation.py` → `runner.run_once` | Rust edge, `.automation/history.json`, `.projects/{id}/saved-items.json`, project `updatedAt`, and `.memory/memories.json`. Writes only when `DEEPSEEK_RUNTIME_MODE=python_disabled` | **集成通过** for a legal `save_item` run and for the disabled, trigger, condition, and policy decisions. A missing, empty, zero, or negative `Content-Length` is `{}`. A non-integer length such as `nope` is `500 Server error`, not `400 Invalid Content-Length`. A declared length above 2000000 is `413` before the body is trusted. `now` is parsed before lookup: invalid `now` is `400 now must be an ISO timestamp or epoch milliseconds` and does not create `.automation`. A missing automation is `404 Automation not found` and does not create `history.json`, including while Python owns the store. An existing automation while Python is authoritative is `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` before any history, saved-item, project, or memory write. With `python_disabled`, `save_item` returns `{"ok": true, "run": ...}`, appends the history row, writes the saved item, moves project `updatedAt`, and upserts one memory sentence (`source.runId` follows the latest success). Cron `34 12 4 10 *` at `2026-10-04T12:34:00+00:00` matches the oracle `startedAtMs`. `0 0 * * *` at `15:00` skips `schedule_not_due` and does not save. Disabled without `force` skips `automation_disabled` and writes that history row. `save_item` without `projectId` is HTTP 200 `status: failed` with `save_item action requires projectId`. `automations.json` is not rewritten. `GET .../runs` returns the same run. Other methods are `405` with `Allow: POST`. `traceId` is empty. Any other action that passes policy is HTTP 200 `status: failed` with `{type} is not executed by the native gateway yet` | `tests/automation_run_routes.rs` 3 passed, `tests/automation_definition_routes.rs` 2 passed, and `tests/automation_template_routes.rs` 2 passed on 2026-10-04 through `create_production_app`, compared with a live `run_once` oracle. `production_runtime_inventory.py --write` then `pytest tests/test_production_runtime_inventory.py` passed. Scanner python-only routes 111 → 110. `automation.py` 3 → 2 (`GET` and `POST /api/automation` only). `backup_governance.py` 62, `workspace.py` 33, `media.py` 6, `status.py` 1. Authority `python`, go domains `[]`, python domains 34, rust domains 12. Evidence log `automation-run-routes-4.log` | not **完成切换**. `.automation` is not a declared native domain, and Python can still write it while Python is authoritative. Traces are not stored. Non-`save_item` actions are not executed. `GET` and `POST /api/automation` stay unregistered until every POST action is real |
| `GET/PATCH/DELETE /api/automation/{automation_id}` | `automation.py` → `registry.get_automation` / `update_automation` / `delete_automation` | Rust edge, `.automation/automations.json`. Writes only when `DEEPSEEK_RUNTIME_MODE=python_disabled` | **集成通过** for GET, PATCH, and DELETE. GET matches a live `get_automation` call (`touch=False`, last 500, first id wins). PATCH matches a live `update_automation`: shallow merge, URL id forced, `touch=True`, first visible row only. DELETE matches `delete_automation`: missing id is `{"ok":true,"deleted":0}` and does not rewrite; a visible id is `deleted: 1`. A successful write persists only the visible window, so a hidden prefix disappears; patching that hidden id is 404 and does not rewrite. A non-empty `projectId` requires the project before the write. `_touch_project` then updates `updatedAt` and swallows a missing project. While Python is authoritative, a well-formed PATCH or DELETE is `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` and does not create or change the file. Empty body, non-object, bad JSON, bad Content-Length, 413 above 2000000 bytes, invalid UTF-8 500, invalid id 400, unsupported trigger 400, missing project 404, and missing token 401 leave the bytes unchanged. `HEAD` follows GET. `POST` on this path is 405. `POST .../run` is the run row | `tests/automation_definition_routes.rs` 2 passed on 2026-10-04 through `create_production_app`, compared with a live update/delete oracle. `production_runtime_inventory.py --check` passed. Scanner python-only routes stay 112 because the path was already registered. `automation.py` 4, `backup_governance.py` 62, authority `python`, go domains `[]` | not **完成切换**. `.automation` is not a declared native domain and Python `_write_automations` still writes while Python is authoritative. `GET/POST /api/automation` remain Python. `POST .../run` is the run row. Template creation is the row above |
| `GET /api/rust/status` | `status.py` → `rust_core.registry.rust_status` | Rust edge, flags and one `GET /healthz`, no store | **集成通过**. Body is `{"ok": true, "rust": {"enabled", "components"}}` and matches a live `rust_status()` call. Flags use `rust_core.config._env_bool`: empty uses the default, whitespace-only is false. A disabled gateway has `url: ""` and `healthy: false` and does not open a socket. An enabled gateway requests `GET /healthz` on the URL host and port, ignores the URL path, rejects a non-http scheme, uses no proxy, and does not follow redirects. Only HTTP 200 is healthy. A blank `DEEPSEEK_RUST_GATEWAY_URL` falls back to `http://127.0.0.1:8787`. `HEAD` has an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}`. The query string is ignored. The data root stays empty | `tests/rust_status_routes.rs` 1 passed on 2026-10-03 through `create_production_app`, plus 2 lib tests for the blank URL and the whitespace flag. Inventory after this row: scanner python-only routes 112, `status.py` 1, `workspace.py` 33, `backup_governance.py` 62, `media.py` 6, `automation.py` 4, authority `python`, go domains `[]` | not **完成切换**. `GET /api/semantic-cache/status` stays Python because `status()` creates `.semantic-cache`. No domain was declared. The contract test did not dial port 8787 |
| `GET /api/edge/status` | `status.py` → `edge_inference.edge_inference_status` | Rust edge, `EDGE_*` settings only, no store | **集成通过**. Body is `{"ok": true, "edgeInference"}` and matches a live `edge_inference_status()` call with an empty payload. The query string is ignored. Settings coerce the provider first, so `dry_run` stays `llama_cpp` and `FAKE` is `fake`. A non-empty `EDGE_INFERENCE_PROVIDER` wins over `EDGE_PROVIDER`. A `llama_cpp` path is resolved; `mlc` and `fake` keep the stripped string. `dependencyAvailable` follows `find_spec("llama_cpp")` or `find_spec("mlc_llm")` without starting an interpreter. `loaded` stays false. Clamps match `config.py`. `HEAD` has an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}`. The data root stays empty | `tests/edge_status_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: scanner python-only routes 113, `status.py` 2, `workspace.py` 33, `backup_governance.py` 62, `media.py` 6, `automation.py` 4, authority `python`, go domains `[]` | not **完成切换**. `GET /api/semantic-cache/status` and `GET /api/rust/status` stay Python. No edge domain was declared. The route does not load a model |
| `GET /api/scheduler` | `status.py` → `scheduler.scheduler_status` and `dead_letters` | Rust edge, read-only `.scheduler/scheduler.sqlite3` when the file already exists | **集成通过**. Body is `{"ok": true, "scheduler", "deadLetters"}` and matches a live `scheduler_status` / `dead_letters` call. Admission counters are the fresh process snapshot. `rate_per_second <= 0` reports `float(capacity)`. Burst `0` uses `max_concurrency`. A missing database stays missing and has no `recent` key. An existing file is opened read-only and is not rewritten. A missing table or a non-database file sets `error` and returns `deadLetters: []`. A directory path is `unable to open database file`. `limit` uses `int` (`ValueError` → 50) then `max(1, min(int(limit or 50), 1000))`. `HEAD` has an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}` | `tests/scheduler_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: scanner python-only routes 114, `status.py` 3, `workspace.py` 33, `backup_governance.py` 62, `media.py` 6, `automation.py` 4, authority `python`, go domains `[]` | not **完成切换**. Taking a lease and writing the DLQ stay on Python. `.scheduler` is not a declared native domain |
| `GET /api/workspace/artifacts/{artifact_id}/preview` | `workspace.py` → `artifacts.preview_artifact` | Rust edge, read-only `.projects/<id>/artifacts.json` and the artifact file | **集成通过**. Body is `{"ok": true, "artifact", "previewAvailable", "content", "bytes"}` and matches a live `preview_artifact` call. An explicit `projectId` searches only that project, even without `project.json`. An empty `projectId` scans projects that already have `project.json`, newest `updatedAt` first. Missing file, bad JSON, and a directory named `artifacts.json` are `404 Artifact not found` and do not create `.projects`. Invalid UTF-8 is `500 Server error` and the bytes stay unchanged. Text previews are redacted and capped at 100000 characters. The last 500 normalised records are kept. `HEAD` has an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}` | `tests/artifact_preview_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: scanner python-only routes 115, `workspace.py` 33, `backup_governance.py` 62, `media.py` 6, `automation.py` 4, authority `python`, go domains `[]` | not **完成切换**. `GET /api/workspace/artifacts/{artifact_id}/download` stays on the Go proxy. `.projects` is not a newly declared native domain |
| `GET /api/workspace/resilience/federation` | `backup_governance.py` → `resilience_federation_readiness.build_federation_snapshot` | Rust edge, pure snapshot, no store | **集成通过**. Body matches a live `build_federation_snapshot` call with the route's fixed wires `object-set-v1`, `receipt-v4`, `commit-v4`, `fastcdc-v3`, empty failure domains, null headroom, `costClass=unknown`, and `readiness=UNKNOWN`. Missing or empty `fleetId` is `local`. A value that trims to empty is `500 Server error`. `snapshotDigest` matches the oracle `_snapshot_digest`. `generatedAt` is UTC second precision with `Z`. No directory is created. `HEAD` has an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}` | `tests/resilience_federation_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: scanner python-only routes 116, `backup_governance.py` 62, `media.py` 6, `automation.py` 4, authority `python`, go domains `[]` | not **完成切换**. The other resilience routes, including the journal, stay Python. No resilience domain was flipped |
| `GET /api/media/{media_id}/segments` | `media.py` → `library.get_media` then `library.list_segments` | Rust edge, read-only `.media/library.json` and `.media/segments/{id}.json` | **集成通过**. Body is `{"ok": true, "segments": ...}` and matches a live `get_media` + `list_segments` call. Invalid media records are skipped and the first accepted id wins. A missing library, bad JSON, and a directory named `library.json` are `404 Media not found` and do not create `.media`. A missing segments file is `[]`. Invalid segment records are skipped. `int()` failures on `index` or `page` are `500 Server error`. Invalid id is `400 Invalid media id` before the read. Invalid UTF-8 is 500. `HEAD` is the same status with an empty body. An authenticated other method is `405 {"detail": "Method Not Allowed"}`. Files are not written | `tests/media_segment_routes.rs` 1 passed on 2026-10-03 through `create_production_app`. Inventory after this row: scanner python-only routes 117, `media.py` 6, `automation.py` 4, `backup_governance.py` 63, authority `python`, go domains `[]` | not **完成切换**. `GET/POST /api/media`, `GET/PATCH/DELETE /api/media/{media_id}`, and `POST .../process` remain Python. `.media` is not a declared native domain. Unauthenticated requests are still the gateway's nested 401 |
| `/api/media`, `/api/memory`, `/api/skills` | media/memory/skills | rust/go | `GET /api/media/{media_id}/segments` is the row above. The other media methods are still Python | native impl |
| `/api/skills`, `/api/skills/{skillId}/run` | `web/routes/skills.py` | rust edge | **集成通过** for all 52 `action == …` branches, all on `create_production_app` behind the same auth as the rest of `/api` and ahead of the Go catch-all. Registry reads and validation; `create`/`import`/`update`/`enable`/`disable`/`delete`; the pack family; the security reviews **and `security_summary`**; trust changes; the offline runner; `dry_run`; all seven `catalog_*`; the run journal; the version family — `list_versions`, `list_pack_versions`, `migration_plan` as reads, `rollback_skill` and `rollback_pack` as writes — and the eval case store — `list_eval_cases` as a read, `create_eval_case` / `delete_eval_case` as writes. Every mutation **and** `run` is gated on `may_write_native_store("skills_store")` → `409 NATIVE_SKILLS_WRITE_NOT_OWNED` before any work unless the deployment is `DEEPSEEK_RUNTIME_MODE=python_disabled`. The catalog adds a **second** gate because it writes a second store: `catalog_install` (without `dryRun`/`preview`) and `catalog_uninstall` change a project's Skill binding in `project.json` → `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED`. The reads — `dry_run`, the journal reads, the catalog reads, the version listings, `security_summary`, `list_eval_cases` and a `dryRun` install — write nothing, and answer without the gate. An action with no native branch is `501 NATIVE_SKILLS_ACTION_NOT_READY` **by name**; an action nobody serves keeps the oracle's own `400 Unsupported Skill action`. All five refusals are one **dependency**, not five missing ports: the oracle's `_score_diff` → `eval_aware_upgrade_gate` → `skills.eval.build_skill_eval_report`. That engine's **scoring, aggregation and comparison** half — everything after `case_results` — is ported and compared (`skills::eval`, 573 probe cases); what is missing is `_run_case`, whose execution half needs media **ingestion**, `permissions.evaluate_skill_tool` and Python's `re`, each of which the port refuses by name rather than approximating | `tests/skills_routes.rs` — **6 passed** through `create_production_app`, run sequentially, at the default thread count, and once per test in isolation: the auth boundary, registry reads and validation, the `409` refusal before cutover, the write path after it, the offline run's journal record and its `list_runs`/`get_run` reads, `dry_run` leaving the journal bytes unchanged **and carrying the oracle's payload contract** (a `{"skillId": …}`-only payload is the oracle's `400 "Skill config missing required fields: …"`; a config payload is `200` with `dryRun: true`), the catalog's five reads, both of its refusals and its refresh write under `python_disabled`, the run journal's three writers refusing while its four readers answer and then redact/cleanup/delete over two real runs, and the security overview with the version family — its scope default, a built-in's `403`, the route's `version is required`, a custom Skill's revision count before and after a rollback, and the eval case store — its golden-first listing, both refusals, the bare create form, both required ids and the empty-file byte. The by-name `501` assertion is checked against the exported `ACTION_NOT_MIGRATED` first, so implementing the action it pins fails there with the reason instead of silently weakening the test. `skills_parity_probe.py` **PASS** over **523 cases** against the unmodified oracle — the runner family, the whole catalog manifest, eleven search filter shapes, previews, installs, uninstalls, the run-analytics family, the security overview, the version family over two fixture revisions, and the eval case store — plus a **byte comparison of the two `catalog.json` files** the refresh writes. The 52-of-52 split is measured against the 52 branches in `skills.py` with no overlap and nothing unaccounted for. Local only, HEAD `79aba745` plus this uncommitted slice | not **完成切换**. `.skills` and `project.json` stay Python's until the process is started with `python_disabled`; `catalog_refresh` and `analytics._write_runs` joining `registry.skill_store_scope()` is what keeps that handover symmetrical, and `tests/test_skill_registry_failure_paths_332.py` pins both directions. The eval engine, the online `run` and the rest of the file family are unported |
| `/api/automation` collection | `automation.py` list and action dispatcher | still Python. `GET` and `POST` share one path | **未实现** on the native edge. POST dispatches `create`, `run`, `rerun`, `simulate`, and `run_due`. Do not register one method. Item GET/PATCH/DELETE, template create, template list, run history, and `POST .../run` are the rows above | scanner still lists both collection methods. `automation.py` 2 after the run row | not **完成切换**. Registering list or create alone would turn the other POST actions into 405 |
| `/api/rag/*`, `/api/file-*` | rag/files | rust | library | edge RAG |
| `/federation/v1/*` | `web/federation_app.py` | rust data + go control | crates | signed Federation Gate A |
| `/healthz`, `/metrics` | server + gateway | rust edge | registered | edge is public listener |
| traces | `observability/trace_api.py` | rust/go | partial | native export |

## 4. Launchers, images, platforms

| Entry | Current runtime | Target | Python in default path | Evidence needed |
| --- | --- | --- | --- | --- |
| `launch.py` default `--app` | Python desktop WebView | native host + rust edge | yes | no-Python desktop run |
| `launch.py --server` | `deepseek_infra.app` | rust edge | yes | no-Python server |
| `launch.py --gui` | Tk launcher | native GUI | yes | native launcher |
| `launch.py --mobile` | Python mobile launcher | platform UI | yes | Android/desktop evidence |
| `docker-compose.yml` | `deepseek-infra:4.0.3` Python | native compose | yes | default must change after gates |
| `docker-compose.native.yml` | edge/deepseekd/worker images | 5.0 topology | no Python service entry | runtime workload, not static YAML |
| `docker-compose.stateless-mcp.yml` | TS + Redis | rust MCP | TS server | migrate server, keep Redis if needed |
| Android `android/` | Chaquopy removed; Java WebView + packaged Rust/Go; file chooser and ML Kit retained | Rust business/data; Go control shadow in measured APK; platform glue Java | No Python package, dex bridge or child process in measured nonzero workload | 集成通过: three app-UID tests and 26 APK/UI/recovery checks. Whole OCR/upload flow, ARM64 execution, full workloads and existing-data upgrade pending |
| PyInstaller `scripts/build_exe.py` | Python exe | native installer | yes | no python3.dll |

## 5. Go control plane (qualification, not production)

| Module | Role | Schema / status | Tests | Remaining |
| --- | --- | --- | --- | --- |
| `internal/store` | isolated SQLite, unique writer | CurrentSchema **v13** in this worktree; append-only import/handback provenance and durable source binding for first signed promotion; old promoted imports refuse upgrade | store tests, `inventory_import_schema_test.go`, `inventory_provenance_test.go`, `inventory_handback_test.go`, `inventory_handback_fault_test.go` | cross-store atomicity, directory freeze, v30 oracle replay, exact-head CI |
| production apply (control mutation) | `control-mutation-request-v2` + `Control.ApplyMutation`, schema v9 journal admits `APPLIED`; **Python/Go/Rust verification parity** (34 frozen cases) | **集成通过 locally** at `POST /internal/mutation/apply` with deployment-pinned signer | `mutation_apply_route_test.go` (HTTP success/refusals), `mutation_request_v2_test.go` and `operation_test.go` (cross-domain replay refusal), `frozen_mutation_request_v32.rs` (Rust), `test_native_runtime_mutation_request_v2.py` (oracle) | exact-head CI, per-domain promotion evidence and production cutover |
| admission v5 | claim/lease/resources | implemented | local historically | policy identity |
| reconciliation v6 | RECONCILING takeover | implemented | local historically | coordinator wiring |
| verification v7 | VERIFYING / ASSESSING_EFFECT | this tree | this session | outcome/risk verifiers |
| authority v8 + signed promotion v10 | checkpoint head/journal + append-only cutover authorization and signed artifact journal | **集成通过 locally**, default-off; claim, head and transition are loopback-bearer protected | `authority_claim_route_test.go` (claim → signed cutover → signed apply), `internal_api_test.go` (started process), `promotion_test.go` (legal success, invalid signature/domain/expiry, exact replay, retained history, isolated restart/fencing), store cutover tests | independent externally authorized promotion, provider and exact-head evidence |
| `internal/action` | leased execute + recover | VERIFYING on APPLIED | this session | signed ops |
| production authority (worker execution plane) | durable `action`-domain cutover record; `WithAuthoritative(true)` is a claim, not authority | **durable gate implemented; refused before any write while unpromoted** | `production_authority_test.go` (promoted success + four refusals + durable read), `cutover_authority_read_test.go` | provider-backed kill/takeover evidence; signed ops |
| `internal/worker` | gRPC client + TLS dial | this tree | this session | CI TLS + signed ops |
| `internal/shadow` | decision digest and qualification persistence | implemented locally; `PutShadow` refuses promoted domains in the write transaction | Go shadow tests, including promoted action refusal | production cutover evidence |
| `cmd/deepseekd` | lifecycle, shadow HTTP | shadow | process tests | production auth |
| internal control API | nine `/internal/*` handlers | **集成通过 locally** for claim → signed cutover → apply; bearer + loopback required, anonymous deployment serves no control plane | `internal_auth_test.go`, `authority_claim_route_test.go`, `mutation_apply_route_test.go`, `internal_api_test.go` | export/import, provider and release evidence |
| cutover | checkpoint tip plus deployment-pinned Ed25519 signature on one domain/transition; no key or unsigned document refuses | **集成通过 locally**, v10 persists exact artifact bytes in the promotion transaction; exact signed replay survives restart | `promotion_test.go`, `cutover_authorization_test.go`, `authority_claim_route_test.go`, `internal_api_test.go`; full `go test ./... -count=1` | independent externally signed artifact production use, Python→Go data transfer/rollback and exact-head CI |
| Python→Go control inventory transfer | Python `backup_control_authority.py` can export a secretless checkpoint; policy/target state still lives in Python-owned stores | source policy/target export and table-level fence, direct Go read-only source attestation, offline Go import into a fresh dual-evaluate domain; v11 journals digests, boot epoch and attestation with imported records and preserves source CAS revision; v13 stores exact manifest bytes and source/projection locations, and first signed promotion reattests both before Go commit. Upgraded unpromoted v11/v12 imports without a live binding cannot promote and can be handed back; older already promoted imports refuse upgrade without invented proof. **Even an empty** policy/target domain needs a Python-created fenced source, attested import and signed manifest/source digests for first promotion. Later signed authoritative states retain that source binding after legitimate Go writes. The fence also freezes the linked control state the transfer binds (see the linked-fence row below). **集成通过 locally** on isolated copies; actual ownership transfer **未实现** | `tests/test_native_control_handoff.py` (23 cases, including a fresh Python interpreter denied by the source fence and legacy projection checks); `go/internal/store/inventory_import_test.go`, `inventory_source_test.go`, `inventory_provenance_test.go`, `inventory_import_schema_test.go` (real nonempty and empty Python-created SQLite, projection drift/recovery, restart, old-store migration and rollback, legal signed promotion, Go write and refusals), `go/internal/api/authority_claim_route_test.go` and `mutation_apply_route_test.go` (HTTP success on attested empty transfer), `go/cmd/control-inventory-import/main_test.go` (restart) | nonzero policy `topology_generation` refused; external directory can race final check, cross-store atomicity, existing shadow-history replacement, native policy/target business/API parity and release evidence |
| public backup-policy list + next-run projection | Python `backup_governance.api_backup_policies_list` reads `backup_policies.list_policies`, which adopts legacy `.backup-policies` files, then `backup_scheduler.next_run_for_policy`; the React client displays `nextRuns` | Go `GET /api/workspace/backup-policies` uses a transactionally checked `policy` cutover, validates current records and event histories, and returns the existing `{policies,nextRuns}` shape. Go cron handles five fields, IANA zones, DST gap/fold, misfire and deterministic jitter. The Rust production edge checks the original Host/token before proxying; Go separately checks direct callers. **集成通过 locally on Go HTTP, Rust→mock edge and built Rust gateway→deepseekd executables over TCP after an isolated Go-store restart; no default cutover** | `go/internal/api/backup_policies_test.go` (auth, shadow denial, real nonempty fenced import and signed Go write), `go/internal/store/authoritative_list_test.go` (ordered records, orphan/corrupt data refusal), `go/internal/scheduler/backup_cron_test.go` and checked-in Python oracle (11 cases), `rust/crates/deepseek-gateway/tests/backup_policy_auth.rs` (original Host/auth refusal + mock-Go legal response); `go/internal/api/backup_policy_process_integration_test.go` (`native_integration`: signed Go mutation, built deepseekd restart and Rust gateway over TCP, real Go public handler/SQLite, nonempty result and Host/auth refusals) | **Read slice + policy CRUD.** The projection directory is bound in the export manifest, checked by the Go attester and reattested at first promotion; it is not mechanically frozen. Create, update and delete are now served through the same operator-journal channel and driven across real processes (`TestBackupPolicyRustGatewayToGoPolicyCrud`): an update merges only the oracle's 18 patch fields and preserves `createdAt`, a delete tombstones the record and both write verbs then answer the oracle's `404 Backup policy not found`. The edge now checks the original `Host` on the **item** paths too, because the Go-side check cannot see it — the proxy rewrites `Host` to the Go listener. Remaining: `run`/`continuity`, scheduler claims, exact Python error/response parity, installer and browser/device qualification, full native backup path and exact-head CI |
| Python→Go control inventory handback (reverse transfer) | Python `backup_control` writes are fenced by the exporter's triggers; an abandoned transfer must return ownership without hand-editing SQLite | Go `cmd/control-inventory-handback` refuses a promoted, de-promoted, Go-mutated, wrong-transfer or unjournaled domain, removes its imported records/events and provenance row atomically with the v12 handback journal, and emits canonical `control-inventory-handback-v1`; Python `--rollback` verifies those exact bytes, journals an append-only revocation, lifts the fence and republishes the writer. Source rows must be unchanged since fencing. **集成通过 locally** | `go/internal/store/inventory_handback_test.go`, `inventory_handback_fault_test.go`, `inventory_handback_fixture_test.go` (policy **and** target documents), `cmd/control-inventory-handback/main_test.go`; `tests/test_native_control_handback.py` (8 cases against the real Go documents, cross-process restart denial, two-step revocation of the linked fence and re-fencing) | two SQLite stores are not one atomic transaction; existing Go shadow history and the Python state outside the fenced set are still not transferable; no production handback and no exact-head CI |
| Python→Go linked control fence | Python `backup_control_authority` writes the authority tip, its journals, the boot epoch and the linked lifecycle/receipt rows the transfer proof binds; after an export none of them may move in Python. | Python keeps sole authority for its own store; the fence is written by the exporter and verified by the Go source attester, so no runtime is authoritative for both sides. | `control_authority_head`, `control_authority_outbox`, `control_authority_mutations` and `control_boot_state` are frozen by any held fence; `lifecycle_intents` and `target_receipt_mutations` only for rows naming a fenced domain; the linked objects are released only when the last fence is lifted. | `scripts/native_control_handoff.py` `_LINKED_FENCE_TABLES`/`linked_fence_objects()`; `go/internal/store/inventory_source.go` `linkedFenceTables`/`linkedFenceObjects()` requires the same 18 objects byte for byte. | Refreshed Python fixtures (source DB SHA-256 `f11de32e6465c2e3c16b5c9f43a70e8f28da4b74916bfdce393f4bf5e5899a32`, 266240 bytes, 18 linked triggers) are the cross-language contract; `tests/test_native_control_handoff.py`, `tests/test_native_control_handback.py`, and Go `TestLinkedControlStateIsMechanicallyFenced` plus the adapted tamper cases. Windows local; Linux CI pending. | The checkpoint *documents* live in authority anchor files outside the fenced SQLite database, so they cannot be fenced there; the Python control state outside the bound set (target objects, QoS, maintenance, retention) remains Python's and is explicitly not transferred. **集成通过 locally**. |

### Browser-facing backup policy dependencies (2026-09-30 local qualification)

| Capability | Original entry and observable behavior | Target / authority | State and dependency | Implementation and compatibility case | Platform and verification | Evidence and remaining gap |
| --- | --- | --- | --- | --- | --- | --- |
| Automatic backup policy read | React `AutomaticBackupsTab` calls policy, target and mirror lists together; Python `backup_governance.api_backup_policies_list` supplies policies and next runs | Go policy control state, Rust public listener; browser TypeScript presents it | **集成通过 locally for read only**; depends on signed policy cutover, Go store and Rust gateway | Frontend settles the three reads independently, displays a nonempty Go policy and next run, and preserves errors for unavailable dependencies. `frontend/src/features/backup-restore/AutomaticBackupsTab.test.tsx`, `frontend/src/api/httpClient.test.ts`, `go/internal/api/backup_policy_process_integration_test.go` | Windows browser against separate built Rust/Go executables and isolated SQLite; `npm run check --prefix frontend`, `go test -tags native_integration ./internal/api -run '^TestBackupPolicyRustGatewayToGoProcess$' -count=1 -v` | Browser displayed `p-process-boundary` and its next run; mirror remains 501; the subsequent target-health slice now reads native targets and were explicitly marked unavailable. No CRUD, run, scheduler, installer, Android, provider, zero-Python or release qualification. Browser observation used loopback auth-disabled test mode; the normal process test proves Host/token refusals. |
| Backup target list and health | Python `backup_governance.api_backup_targets_list` returns `backup_targets.list_targets()` plus the actual `backup_scheduler.target_health()` history; React displays target status | Go registry and transferred health snapshot, Rust public listener; each source table stays fenced in Python and only Go writes its imported tables | **Integration passed locally for GET and isolated-copy transfer**, with signed target cutover, v14 provenance and real attested empty or nonempty health; production cutover remains incomplete | v2 export preserves v1 corpus, fences the scheduler health table, validates exact SQLite schema/value types and digests, reattests at import/first promotion and supports atomic Go handback plus interrupted Python revocation recovery. `target_health*.go`, `backup_targets.go`, Python handoff/oracle and CLI tests | Windows; pinned Rust/Go processes over TCP and actual browser. `go test -tags native_integration ./internal/api -run ^TestBackupTargetRustGatewayToGoProcess$ -count=1 -v`; `artifacts/native-target-health-browser-detail-v14.jpg` | Browser displayed imported target `t-1` with `blocked`; real process retained 2 health rows including historical null detail. Restart reads only Go state. Provider probe/refresh, CRUD, native scheduler, mirror generations, desktop/Android packages, zero-Python measurements and exact-head CI remain open. Three databases plus projection are not one atomic source; legacy processes must remain stopped during transfer. |
| Backup policy create and update (write channel) | Python `backup_governance.api_backup_policies_{create,patch}` → `backup_policies.create_policy`/`update_policy` → `backup_control` SQLite, plus the browser's toggle and delete buttons | Go control plane: `internal/policy` semantics, the operator mutation channel, and the public `POST`/`PATCH` routes on the Go `/api` surface | **集成通过 locally for create and update**; **delete is refused** (501 by name) until a terminal tombstone state exists, so the flow is not complete | Schema v15 adds the append-only `control_operator_mutations` journal beside the signed `control_operations` one (a separate table, because `result_status` means "signed"); `ApplyOperatorMutation` requires the deployment capability plus a durably Go-authoritative domain and stamps the journal with actor, `actionId`, the live `executionEpoch`, cutover revision/fence and writer fence in one transaction with the record and its event. The routes apply the oracle's 18-field merge list, lazy authoritative target-binding resolution, the 64 000-byte body limit and the oracle's refusal messages; the policy state table allows a disabled create and a rewrite in place. Contract: `release/native_runtime_go_control_store_v1.json` (`operator_mutation`, `policy_write_routes`), asserted by `tests/test_native_runtime_go_control_store.py`. Cases: `go/internal/store/operator_mutation_test.go`, `go/internal/api/backup_policy_writes_test.go` (+ the unit file) | Windows; `go test ./internal/store ./internal/api ./internal/policy`, full `go test ./...`, and the unchanged 95.0% floor at **95.037888% (7776/8182)** | A legal create and update are stored, journalled and readable through the same public plane; refusals cover a foreign Host, a missing token, a non-authoritative store, an unverifiable target binding, an oversized or non-object body, every normalisation refusal including the uncaught `ValueError` as a 500, and an id collision as 409. Gaps: delete needs a tombstone; no browser observation yet; no scheduler/run/continuity; the create revision must be 1 in this store (the oracle would store a later one — recorded as a narrowing). |
| Backup policy write semantics (`normalize_policy`) | Python `backup_policies.normalize_policy` turns a client payload into the stored document and raises every write refusal; `create_policy`/`update_policy`/`delete_policy` then commit it | Go (`internal/policy`), the control plane that owns `policy_crud`; the route that serves it is the write-channel row above | **单测通过 locally** — semantics ported and pinned; the routes that consume it are the row above | The port reproduces the validation order, strict `_require_int`, `str(value or "")`, `re.fullmatch`, `min(4, cpu_count)`, the `policyRevision` coercion, the oracle's uncaught `ValueError`s (kept as 500s), the cron `Invalid cron expression:` wrapper and `loadTimezone`'s refusal of `""`/`Local`. Contract: `scripts/generate_policy_normalization_fixture.py` → `go/internal/policy/testdata/policy_normalization_v1.json` (**81 cases**, SHA-256 `1C76AD3A7002E6C07305E681D5714CEB678093D566071E86D2F809FBBF6BA16C`, `--check` stable). Cases: `go/internal/policy/normalize_test.go` (fixture, delivery order, sub-validator propagation), `semantics_test.go` (Python value semantics) | Windows; `go test ./internal/policy`; fixture regenerated and re-checked | Two real divergences found and fixed by the fixture: Python's `a or b` keeps a falsy last operand (a zero cost objective), and `parse_cron` wraps a `ValueError` while the `int()` sites do not. The operator channel that stores these documents now exists (schema v15) and serves create/update; delete still needs a tombstone, and no browser observation has been made. |
| Backup mirror list, status and upload | Python `workspace.api_workspace_backup_mirrors` / `api_workspace_backup_mirror_get` / `api_workspace_backup_mirror_put` read and seal immutable generations plus `HEAD.json` under `.backup-mirror/`; React lists the latest acknowledged mirror and the browser uploads the sealed replica | Rust owns the store (`frontend_mirror_store`, data, python -> rust at 4.9.4, `rust_data`); the recipient sets stay Go policy control state and arrive over the authenticated loopback plane | **集成通过 locally** for all three routes, with a mechanical Python writer denial and a two-way byte-level oracle parity; no production cutover, and no browser observation of the native route yet | `deepseek_policy::backup_mirror` reproduces generation directories, the `HEAD.json` CAS, epoch-index bookkeeping, idempotent replay, `mirror-stale-epoch`/`-sequence`/`-head-conflict`, variant selection and `mirror-generation-corrupt`; sealing links `backup_crypto` rather than copying it. Routes are mounted only when `may_write_native_store` holds, so Python is never shadowed. `go/internal/api/backup_policy_recipients.go` derives the union over all policies and one group per enabled policy (no `encryption` fallback, empty group preserved). Cases: `crates/deepseek-gateway/tests/backup_mirror_routes.rs`, `crates/deepseek-policy/src/backup_mirror.rs`, `tests/test_native_runtime_mechanical_denial.py`, `tasks/native-runtime/backup_mirror_parity_probe.py` | Windows; pinned Rust 1.85 GNU tests, `go test ./internal/api/`, and `python tasks/native-runtime/backup_mirror_parity_probe.py --rust-example rust/target/debug/examples/backup_mirror_parity_probe.exe` | Parity probe: 8/8 checks, `metadata.json` and `HEAD.json` byte-identical to the oracle after masking the random generation id, clock and ciphertext hashes; the Rust-written ciphertext decrypts to the exact envelope with the recipient identity; each side reads the other's directory identically. Gaps: the generation transfer has no source fence or handback document yet (Rust reads the same directory Python wrote, so a cutover must stop the Python service), provider/restore qualification, a browser observation of the native route, and exact-head CI remain open. |
| Current signed cutover binding | Go `GetCutover` and public authoritative reads formerly accepted a promoted state after its artifact row was removed | Go control store | **单测通过 locally**; all current authoritative rows require matching authorization and promotion artifact digest | `go/internal/store/promotion_schema.go` checks current cutover revision/epoch/fence against journal rows and artifact SHA-256; deletion/digest-tamper regression tests | Go store tests and 95% statement gate; Linux race and exact-head CI pending | Fails closed on missing/altered artifact rows; does not replace signature verification at admission or prove external DB administrator tamper resistance. |

Backup-policy list slice detail (2026-09-29):
`tests/test_native_control_handoff.py` now has 21/21 local cases after adding
projection preflight and directory binding (the earlier 15-case count in the
inventory row describes the prior checkpoint). The reconcile check precedes
installation of a source SQLite fence, and the same pass binds the projection
directory's exact state into the export manifest so the Go attester can
re-derive it.
The original user entry is
`GET /api/workspace/backup-policies` in `backup_governance.py`; the React
`AutomaticBackupsTab` displays every policy and `nextRuns[policyId]`. Go is the
target and only writer of `go-control/policies`; this **GET has no side effect**.
The Rust edge forwards `/api/*` to Go when configured, but a real Rust→Go
process success for this route has not yet been run. It depends on the signed
and attested policy-domain import/promotion, the public token and IANA timezone
data. Implementation: `go/internal/store/authoritative_list.go`,
`go/internal/api/backup_policies.go`, `go/internal/scheduler/backup_cron.go`.
Compatibility: 11 checked-in Python-generated next-run cases, including the
spring DST gap and autumn first fold; Go HTTP tests use both the **nonempty**
Python-fenced policy source and an attested empty source followed by a signed
Go mutation. Auth, shadow, orphan/corrupt row and schema-loss refusals are
tested. Local Windows commands: `go test ./internal/api ./internal/scheduler
./internal/store -run 'TestBackup|TestAuthoritativeList' -count=1`,
`python -m scripts.generate_backup_next_run_fixture --output
go/internal/scheduler/testdata/backup_next_run_python_v1.json --check`, and the
full Go 95% coverage gate. Browser, desktop, Android, Linux race and exact-head
CI have no evidence for this slice. The Python exporter refuses unadopted or
malformed legacy `.backup-policies` and `.backup-targets` projections against
source rows (a nonstandard path is refused without an explicit absolute
projection directory), then binds that
directory's exact state as `legacyProjection` — the file count plus a canonical
digest over each file's name, size and SHA-256. The Go source attester re-derives
that digest from the directory (inferred from the standard `.backup-control`
layout, or named through `--projection-dir`) and fails closed when the directory
appeared, vanished or changed, so the check is now bound end to end. The
scanner includes hidden `*.json` names and rejects `.json` directories, matching
the Python list routes. It also parses each projection and checks its ID against
the fenced SQLite rows, so a resealed manifest with an unadopted, mismatched,
duplicate-key or malformed projection is refused; focused source tests cover
the rejection and a legal reconciled projection.
The directory is **rechecked, not frozen**: the SQLite fence cannot reach it, so the
Python service must stay stopped.
Public CRUD/run/continuity and the active scheduler
are still Python-owned. State: **集成通过 locally for Go HTTP read only**;
**完成切换/完成验收未达到**.

Control-authority slice audit (current checkout): original production entry is
Python's `deepseek_infra/infra/workspace/backup_control_authority.py` checkpoint
export and Python-owned policy/target state. The observable native behavior is
an authenticated local claim of the live head, legal shadow → dual-evaluate →
Go-authoritative transition, signed `policy` apply, persisted snapshot, exact
replay and cross-domain replay refusal. Go uniquely owns `go-control/` tables;
Rust worker parity verifies the frozen v32 request without writing those
tables. Dependencies are the v8/v9 migrations, deployment bearer and signer,
and a valid `control-authority-v1` checkpoint. Implementation is in
`go/internal/api/{shadow,auth}.go` and `go/internal/store/{authority_state,
cutover,operation}.go`. Compatibility checks are v17/v32 oracle and Rust
worker replays; local verification is Go tests, 95.0% coverage gate, the
started-runtime HTTP test, and the contract checker. Platform coverage is
Windows local only; Linux exact-head CI, desktop/Android production callers,
independent external promotion use, export/import and provider recovery remain
unverified. Status: **集成通过 locally**, with no ownership cutover.

Signed promotion slice audit (this worktree):

| Original entry / observable behavior | Target and sole state/effect owner | Dependencies | Native implementation | Compatibility and platform | Local command and evidence | Remaining gap / status |
| --- | --- | --- | --- | --- | --- | --- |
| Python `backup_control_authority.py` exports a checkpoint; the Go `POST /internal/cutover/transition` changes one domain, its epoch and writer. A legal signed `policy` request succeeds and is visible in the cutover record; unsigned, wrong-domain, expired and bad-signature requests leave it unchanged. | Go alone writes `go-control/control.sqlite3` cutover, authorization and promotion artifact tables. Python remains current production owner. | Installed live `control-authority-v1` tip, writer lease, revision/epoch/fence CAS, loopback bearer, configured `DEEPSEEKD_PROMOTION_SIGNER_KEY`, Fleet/environment. | `go/internal/store/promotion.go`, `promotion_schema.go` (v10 append-only artifact), `cutover.go`; `go/internal/config/config.go` and `lifecycle.go` pin trust material. | Existing v17 authority and v32 mutation corpora unchanged. `promotion_test.go` validates the v1 signature domain and v9 unsigned-history refusal; `authority_claim_route_test.go` exercises the HTTP chain with an attested Python source. Windows local; Linux CI pending. | Historical local `go test ./... -count=1` PASS, `go vet ./...` PASS, Python catalog/contract checks PASS; current slice gates are in continuation. | Independent signing/review ceremony, cross-store atomicity, provider-backed recovery, production cutover and exact-head CI remain. **集成通过**, not 完成切换. |
| Python `backup_control.create_policy`/`mutate_policy`/`delete_policy` and target writes persist full user records in `.backup-control/control.sqlite3`; a transfer must preserve nonempty revisions, prove empty domains and refuse later source writes. | Python retains sole authority for the source database until an independently approved cutover; Go writes only `go-control/control.sqlite3`. | Stopped Python service, exact policy/target columns in schema v8, live checkpoint head with exact projection and generation maps, active recovery state, settled authority/outbox/lifecycle effects, secretless payloads; Go live writer lease, installed matching checkpoint, `dual_evaluate`, fresh domain. | `scripts/native_control_handoff.py` reads/fences one source SQLite transaction, including bound linked control tables; `go/internal/store/inventory_source.go` checks the source and 18 trigger definitions read-only; `inventory_import.go` checks manifest/checkpoint, preserves source CAS and atomically writes records/events plus v11 provenance; `cutover.go` requires the attested digests and unchanged shadow event set even when the source is empty; `go/cmd/control-inventory-import` requires explicit source DB and manifest paths. | Windows isolated Python and Go SQLite copies; full Python-created nonempty and empty source fixtures plus manifests are consumed by Go tests. No Android, desktop, provider or production source touched. | `pytest tests/test_native_control_handoff.py -q -p no:cacheprovider --no-cov` 23 PASS, including a fresh Python interpreter denied by the source fence; focused Go source/import/command/provenance tests PASS; full gates tracked in continuation. | Cross-store atomicity, existing shadow history, complete business/API parity and exact-head CI remain. Source→fresh-Go import **集成通过 locally**; ownership cutover **未实现**. |
| Python `backup_control` writes are fenced by the exporter's SQLite triggers; a transfer that is abandoned before any authoritative use must return ownership without hand-editing SQLite or restoring a pre-fence backup. | Go writes only its own store: it removes the imported records, their events and the import provenance row it wrote, and appends the immutable v12 `control_inventory_handbacks` row. Python writes only its own store: it appends the `native_control_handoff_revocations` row, drops the fence row and its guard triggers, and republishes the writer. Neither runtime writes the other's tables. | `dual_evaluate` cutover, no promotion artifact or cutover authorization for the domain, unchanged imported row/event counts and writer/time, exact transfer ID, live Go writer lease; Python side: Go-emitted canonical bytes bound to the fenced manifest, source, authority tip and transfer, and unchanged source rows. | `go/internal/store/inventory_handback.go`, `inventory_handback_schema.go` (v12), `go/cmd/control-inventory-handback`; `scripts/native_control_handoff.py --rollback` (`revoke_handoff`). | `inventory_handback_test.go` (legal handback, promoted/mutated/de-promoted/wrong-transfer/unjournaled refusals, restart, retention, v11→v12 migration), `inventory_handback_fault_test.go` (lease-expiry, tampered digest/schema, four durable-write fault injections, lifted-trigger faults), `cmd/control-inventory-handback/main_test.go`, `tests/test_native_control_handback.py` (8 cases consuming real Go documents, including a Python process restart and linked-fence revocation). Windows local; Linux CI pending. | The two SQLite databases are not one atomic transaction, existing Go shadow history and Python control state outside the fenced set are still not transferable, and no production handback has been performed. Reverse transfer **集成通过 locally**; ownership cutover **未实现**. |

## 6. Rust data / security plane

| Crate | Role | Production | Remaining |
| --- | --- | --- | --- |
| deepseek-gateway | public HTTP | not authoritative | implement routes; proxy `/api` |
| deepseek-worker | gRPC worker | mutation denied | TLS (this slice), signed ops, provider kill |
| deepseek-storage / transfer / federation / proof | libraries | Python still writes | dispatch + evidence |
| deepseek-mcp / rag / policy | libraries | Python HTTP | edge wiring |
| backup-crypto, deepseek-backup | production *helpers* | not control plane | keep as helpers until worker owns bytes |

## 7. CI jobs that native changes must keep green

From `.github/workflows/ci.yml`: `test` (pytest 95%), `frontend`, `rust`
(fmt/clippy/test), `rust-coverage` (80%), `native-protocol`, `native-go`
(gofmt/vet/test/race/coverage 95% + real worker), `native-s3-transport`,
eval/security/docs/release-version/evidence. Do not lower thresholds.

## 8. Python that may remain (must be registered, not a dumping ground)

| Use | Entry | Production reachable? |
| --- | --- | --- |
| Offline eval | `evals/runners/*` | no if images exclude it |
| Frozen oracles | `scripts/native_action_lifecycle_oracle.py`, compat corpora | no |
| Codegen/contract check | `scripts/native_codegen.py`, `native_runtime_contract.py` | no |
| Evidence assembly | `deepseek_infra/infra/diagnostics/*` | no |
| Release version | `scripts/check_release_version.py` | no |

Unmigrated business modules above are **not** oracles.

## 9. Ordered remaining chain

1. TLS transport qualification — landed locally (worker server key, Go CA +
   server-name verify, `TestRustWorkerTLSRealBoundary`); exact-head CI pending.
2. Durable grant sqlite journal + Go coordinator attaching live grants (v31
   document and RPC admission are locally verified).
3. Provider-backed execute/reconcile with process kill.
4. Verification/compensation beyond journal primitives; v30 replay.
5. Edge behavior + Go `/api`. The assembly's last prerequisite is ported
   (`prepare_memory_state` and the command grammar); what remains before wiring
   `chat_execution` onto `build_deepseek_request` is a **Rust provider for the
   memory vector index read path or a narrow refusal** — the paired measurement
   proved `None` is not a bounded divergence — plus the two existing refusals
   (forced-search mode, the file vector index).
6. All 18 chat-tool branches run; native `/mcp` `/a2a` (including SSE/resubscribe) `/api/tool-policy` `/api/budget` `/api/rag/status` `/api/gateway/status` `/api/taint` `/api/title` `/api/download` `/api/file-source` `/api/file-reader` `/api/file-chunk` `/api/file-page-text` `/api/file-page-search` `/api/chat` (ordinary streaming turns + tool rounds; agent mode, cascade and forced search refused); Go `/api/config` subset. Remaining: the eval **report engine** (`eval_report`) and the four actions whose payload embeds its verdict (`upgrade_pack`, `eval_upgrade_gate`, `diff_versions`, `diff_pack_versions`), the online `run`, the rest of the file family (`/api/file-text`, `/api/project-files`, `/api/file-page-image` and `/api/file-page-layout` are wired; project metadata writes only when `python_disabled`; page PNG bytes follow `pdftoppm`), the whole `/api/workspace/*` backup/DR surface, the remaining diagnostics status blocks (`/api/mcp`, `/api/scheduler`, `/api/semantic-cache/status`, `/api/edge/status`, `/api/rust/status` — each needs its Python status function ported first), search prefetch and edge inference for `/api/chat`, A2A Python-task migration/retention/peer clients/telemetry/full parity, launchers.
7. Per-domain cutover shadow → dual-evaluate → Go-authoritative → Python-disabled.
   **The mechanism exists, is authenticated, its consumers check it durably, Go can apply a
   signed production mutation, and the verification has full three-language parity** (slices
   1-4: authority claim + authorized cutover + loopback-bearer `/internal/*` + durable
   production-authority gate + v2 apply with schema v9 + Rust v2 parity). The claim,
   live-head read, cutover and apply are now wired through the internal HTTP plane and
   have isolated local success/refusal tests. No production domain is flipped and the
    deployment gate is off. The per-domain signed artifact and append-only v10 journal
    now pass local Go/HTTP tests, and the reverse transfer (v12 handback: Go
    abandons its imported copy, Python verifies the exact Go document and re-owns
    its fenced tables) passes local Go and Python tests on isolated stores. What
    remains: independent external authorization, cross-store atomicity for
    existing shadow history and linked Python tables, provider-backed
    kill/takeover evidence, exact-head
   CI, and the ownership-contract revision.
8. Launchers/images/Android without Python.
9. Exact-head CI, Evidence Assembly, performance, zero-Python workload.
