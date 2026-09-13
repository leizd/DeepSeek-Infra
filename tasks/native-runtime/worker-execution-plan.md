# Native Go-to-Rust worker execution: dependency plan

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


Status: transport-authentication implementation and testing approved on 2026-09-13.
No production authority is enabled by this plan. Full 5.0 scope remains the accepted specification
in `docs/specs/5.0-native-rust-go-runtime.md`.

## Verified starting point (2026-09-07, 2302574)

- `proto/action/v1/action.proto` exposes admission, effect query and signed epoch
  installation, but no storage execution RPC. Storage request messages exist in
  `proto/storage/v1/storage.proto` without an execution service.
- `go/internal/worker/client.go` dials plaintext loopback. The Rust worker main
  restricts its plaintext listener to loopback, but does not authenticate the Go
  service identity. Loopback placement is not caller authentication.
- The signed install request binds action/epoch, not a particular storage operation.
  `control-mutation-request-v1` is a frozen propose-mutation envelope, not a storage
  execution grant. Neither format should be repurposed by changing its semantics.
- The optional Rust S3 library now binds durable placement/condition and verifies
  bytes during reconciliation. Its gRPC commands still do not dispatch these effects.
- Go `TransitionCutover` rejects production-authoritative transitions with
  `ErrCutoverNotAuthorized`. Removing that rejection is not an authorization protocol.
- ADR-0049 requires Federation and other security private keys to stay in Rust.
  Go signing helper functions currently have only test call sites; they must not
  become the production signing path.

## Authentication decision confirmed (2026-09-13)

Approved first boundary: server-authenticated TLS with the server private key in
Rust, plus an explicitly provisioned Go service credential sent only over that TLS
channel. Go must validate a configured trust root and server identity, without
ambient trust, skip-verification, plaintext fallback or credential logging.

This authenticates a service, not an action or lease. Credential lifetime, rotation,
revocation and role scope must be explicit in the implementation contract. A stolen
bearer credential can impersonate that service; short validity and a narrow role
reduce exposure but do not remove that property. A mutually authenticated certificate
design would need Rust-backed client signing to preserve the private-key boundary;
loading a client private key into Go is not silently accepted as an exception.

The user explicitly approved continuing implementation and tests with Rust-owned
server TLS keys, explicit Go certificate and service-name verification, and
short-lived service credentials sent only over TLS. This resolves the prior
security-design pause. It does not authorize provisioning production credentials,
enabling production authority, or switching away from Python. Existing transport
work must still pass the acceptance checks below; approval is not test evidence.

### Qualification transport contract

- Server key files are read only by the Rust runtime. Go uses a configured CA
  pool and explicit server name, TLS 1.2 or newer, with no ambient root fallback
  and no client private key. Test-only certificate generators are not key custody
  evidence for a deployed runtime.
- The explicitly provisioned bearer has 32–4096 printable non-space ASCII bytes
  and at most one hour of remaining validity. Provisioners must generate it with
  a cryptographic RNG; a length check cannot establish entropy. Both runtimes
  check expiry on requests. Credentials are neither silently renewed nor logged.
- Rust's environment loader maps the bearer only to `go-control-plane` with role
  `controller`. This identity is not operation authority. Signed operation scope,
  exact action/epoch fencing and production cutover checks remain separate.
- Rotation and early revocation currently require replacing the explicit
  configuration and restarting the worker/client. Existing connections do not
  extend expiry. No rolling refresh or online revocation service is claimed.
- A TLS client has one credential source: configured per-RPC credentials.
  Additional authorization metadata or a separate call-site bearer is rejected.
  Plaintext diagnostic clients reject credentials, including pre-existing metadata.

The approved transport is still under qualification. The retained admission,
generic effect-query and signed-epoch-install RPCs must receive explicit transport
authentication guards and matching fixture/CI updates before slice 1 is complete.
Their current legacy behavior is not an approved production fallback.

Implementation follows the pinned gRPC
[per-RPC credential contract](https://pkg.go.dev/google.golang.org/grpc@v1.83.0/credentials#PerRPCCredentials)
and explicit [Go TLS trust configuration](https://pkg.go.dev/crypto/tls#Config).

## Ordered implementation slices

### 1. Authenticate the existing real Go-to-Rust channel

Acceptance:
- Explicit credentials work against the actual Rust binary; missing/wrong/duplicate
  credentials and invalid server identity fail before worker state changes.
- Partial configuration, expired credentials and invalid trust material fail closed;
  no fallback exposes credentials over plaintext. Private keys remain Rust-local.
- Test logs/errors/debug surfaces exclude credential values. Existing shadow-only
  compatibility remains explicitly non-production, not a permanent execution fallback.

Verification: Go worker unit tests, real Go-to-Rust TLS process tests, Rust worker
tests/Clippy and default-feature build. These are transport tests, not MinIO evidence.

### 2. Add a typed operation-specific execution contract

Depends on slice 1 and its confirmed identity/lifetime model.

Acceptance:
- Additive Protobuf commands bind action/epoch, operation identity, placement/key,
  exact condition, payload digest/length and authority/lease identity. Existing frozen
  messages and descriptor baseline remain unchanged.
- Rust validates authenticated identity and signed operation authority against the
  exact command, with durable replay protection before dispatch. Go owns claim/epoch
  decisions; Rust owns signing keys and its effect journal.
- Canonical signing input is explicitly specified; deterministic Protobuf encoding
  is not assumed to be a portable cryptographic canonicalization rule.

Verification: descriptor compatibility/drift gate, Go/Rust shared positive/negative
vectors and real cross-process substitution/replay tests. Decompose signer and
admission implementation into separately verified increments before dispatch wiring.

### 3. Wire bounded storage execution and control reconciliation

Depends on slice 2 and a qualified Go authority/lease path.

Acceptance:
- Go durable claim -> typed command -> Rust durable intent -> actual provider effect
  -> conditional byte verification -> bound result -> Go journal reconciliation.
- Payload bytes stay in Rust; Go never opens the Rust database. Service auth alone
  never supplies missing operation authority, lease state or production cutover proof.
- Cancellation, lost ACK and fence loss retain uncertain effects; no blind replacement
  effect or late successful Go commit after authority loss.

Verification: real Three-MinIO execution, actual controller/worker termination at
commit windows, durable takeover and measured provider observations. No manually
seeded effect journal or synthetic success is accepted as execution evidence.

### 4. Qualify production ownership

Depends on complete per-domain parity and cutover implementation, not just slice 3.

Acceptance: complete backup/restore/repair/rebalance and native federation workflows,
two-Fleet/four-MinIO takeover, measured zero-Python/unique-writer invariants, performance
SLOs, every production target/distributable and exact-head CI Evidence Assembly.

The original `tasks/plan.md` and `tasks/todo.md` recovery plans remain untouched.

## Implementation sources checked

- Workspace Tonic 0.12.3 `ServerTlsConfig` and `tls` feature source in the pinned
  Cargo registry. Its TLS configuration is not caller authentication by itself.
- [gRPC Go 1.83.0 per-RPC credentials](https://pkg.go.dev/google.golang.org/grpc@v1.83.0/credentials#PerRPCCredentials)
  provide a transport-security requirement for service metadata.
- [Go TLS configuration](https://pkg.go.dev/crypto/tls#Config) provides explicit
  trust roots and server-name validation. No `InsecureSkipVerify` path is proposed.

## Implementation Status (2026-09-07)

1. **Protobuf & Wire Schema**:
   - Added `ExecuteStorageMutation` and `QueryStorageEffect` RPCs, `StorageMutationRequest`, `StorageMutationResponse`, `QueryStorageEffectRequest`, `StorageMutationStatus`, `StorageConditionType`, and `StoragePrecondition` in `proto/action/v1/action.proto`.
   - Regenerated descriptor and Go stubs; verified contract parity via `native_codegen.py --check` and `native_runtime_contract.py --check`.

2. **Rust Worker Service (`deepseek-worker`)**:
   - `TransportAuthenticator` trait with `ProductionFailClosedAuthenticator` (fails closed with `SERVICE_AUTHENTICATION_UNAVAILABLE` by default) and `StaticTokenAuthenticator` (Bearer token for test/qualification).
   - `execute_storage_mutation`: Authenticates caller, validates fence and epoch against local authority, checks payload digest and expected length, verifies target identity, maps preconditions (`CreateOnly`, `IfMatch`), executes durable intent via `Worker::execute_storage_put`, and journals effect.
   - `query_storage_effect`: Authenticates caller, validates fence, queries SQLite effect record, and triggers idempotent byte-level reconciliation (`reconcile_storage_mutation`) when uncertain.
   - All tests in `grpc_service.rs` and `authorized_storage.rs` pass; Clippy clean with zero warnings under `-D warnings`.

3. **Go Control Plane & Client (`go/internal/action`, `go/internal/worker`)**:
   - `worker.Client`: `ExecuteStorageMutation` and `QueryStorageEffect` with outgoing metadata auth, fence validation, operation ID checking, and sentinel error mapping.
   - `action.Coordinator`: Enforces fail-closed production cutover (`ErrCutoverNotAuthorized`), validates Go writer lease before claim and after dispatch, manages transition lifecycle (`PENDING -> CLAIMED -> EXECUTING -> SUCCEEDED / FAILED_BEFORE_EFFECT / EFFECT_UNKNOWN`), and reconciles uncertain effects. Query failures do not establish a no-effect outcome; see the safety corrections below.
   - Test coverage: `internal/action` at 99.5%, `internal/worker` at 97.9%, overall Go module at 95.5% (meeting the 95.0% CI gate).

4. **Multi-Process Integration Verification**:
   - `go/internal/worker/rust_integration_test.go`: Added `TestRustWorkerStorageMutationFailsClosedWithoutAuth` and `TestRustCoordinatorStorageActionAgainstRealWorker` orchestrating real `deepseek-worker` process, SQLite `ControlStore`, and Go `Coordinator`.
   - Wired integration test invocation into `.github/workflows/ci.yml` `native-go` job.
   - All 13 real MinIO provider tests in `python scripts/run_native_s3_e2e.py` pass without skips.

## 2026-09-07 dispatch and result-settlement corrections

- Regressions reproduced missing fence/operation acceptance, redispatch of an
  `EXECUTING` action, and erroneous terminal settlement after query failures.
  Additional RED cases reproduced replay rejection being classified as no effect
  and the reconciliation entry bypassing the explicit cutover-denial option.
- Both storage RPC client methods now require an exact nonempty operation ID and
  matching action/epoch fence in responses. Confirmed coordinator results must
  match that identity and carry `APPLIED`. The production cutover gate applies to
  reconciliation as well as execution.
- `EXECUTING` is a durable dispatch claim: a retry must reconcile, not issue another
  mutation RPC. A real Go SQLite test exercises two coordinators against the same
  claim while the first worker call is outstanding.
- A query error (including a new, unrecognized code) is not proof of no remote
  effect. Terminal no-effect settlement requires bound `NOT_APPLIED` and a known
  recorded terminal result. `REPLAY_REJECTED` during execution stays uncertain,
  since Rust also returns it for an already-confirmed effect.
- Local Go 1.27.1 formatting, `go vet ./...`, `go test ./... -count=1`, and the
  unchanged 95.0% coverage gate pass (95.6%). These are control/protocol regressions,
  not provider or production takeover evidence.
- Rebuilt the actual Rust worker with Rust 1.85 GNU and ran four real Go-to-Rust
  integration cases: missing authority, unconfigured signed install, unauthenticated
  storage mutation, and coordinator rejection/reconciliation against the real
  process. All passed; no provider was contacted and no production auth was enabled.

### Durable operation-binding gap identified on 2026-09-07

Before the v3 change below, Rust's journal recorded action/epoch, placement,
condition and bytes but not the RPC `operation_id`; query echoed the caller's ID.
Client response checks alone could not prove the original operation association.
The Rust v3 work binds that identity atomically, rejects substitution and unbound
legacy rows, and returns the persisted identity across journal reopen.
At that checkpoint Go still needed durable dispatch intent; the v4 coordinator
implementation below addresses that binding. This does not replace
operation-specific signed authorization, service authentication or renewable action
and resource leases; all remain required by the full ownership migration.

## Rust operation binding implementation contract (2026-09-08)

- Additive worker schema v3 retains v1/v2 unchanged and adds a Rust-owned immutable
  `storage_rpc_operations` association keyed by action/epoch. No historical operation
  identity is inferred or backfilled; old library rows remain diagnostically readable.
- The first RPC reservation inserts parent intent, placement/condition binding and
  operation ID in one `BEGIN IMMEDIATE` transaction before dispatch. A pre-existing
  intent with a different or absent operation association cannot be adopted by RPC.
- IDs are opaque and compared exactly (no normalization), nonblank, NUL-free and
  at most 1024 UTF-8 bytes. Query validates identity before reconciliation/provider
  access and returns the persisted ID; a caller cannot name an old effect arbitrarily.
- Existing Rust library methods retain their signature and non-RPC semantics.
  They cannot modify or dispatch an intent already associated with an RPC identity.
- Verify legacy rejection, exact-ID retry, substitution before/after restart,
  insert/update/delete/replace guards, atomic rollback and v1/v2 migration rollback.
  Provider tests must use real MinIO and actual RPC-created intent, not seeded effects.
- Stop workers before upgrade. An older binary must reject v3; there is no downgrade
  that deletes replay/operation history. Migration failure rolls back without partial
  schema changes. This preservation rule takes precedence over destructive down scripts.
- This association is not an operation authorization signature, a renewable lease,
  service authentication or proof of full Go/Rust ownership.

### Implemented and locally verified (2026-09-08)

- Schema v3 and atomic operation association are implemented. Query rejects legacy
  unbound effects and substituted IDs before reconciliation. Immutable association
  checks also run inside state-transition transactions, closing an unbound library
  dispatch/settlement bypass reproduced during self-review.
- Eight real-SQLite unit cases cover exact retry, UTF-8 limits, restart recovery,
  library/RPC separation, insert rollback, immutable guards, v2 migration rollback
  and corrupted/orphaned associations. The v1 migration test now checks upgrade to
  v3 while preserving historical rows and signed bytes; neither fixture is provider
  evidence or a supported downgrade.
- Rust 1.85 GNU: worker all-target tests pass with S3 (71) and without it (45), and
  strict Clippy passes with `s3-e2e` and without S3. Formatting, frozen contract
  checks (40 corpora, 29 versions, 43 domains, 7 proto files, 10 generated outputs)
  and checksum-pinned code generation checks pass. No frozen message changed.
- The native provider runner passes 15 tests without skips (6 storage, 9 worker),
  using three real MinIO instances. New RPC-handler tests create actual provider
  writes, reject substituted IDs before/after journal reopen, independently verify
  bytes, and recover a dropped successful ACK through the original operation ID
  and bound conditional GET. These do not manually seed effect history.
- This is local development evidence. The RPC-handler provider tests reopen worker
  handles; they do not prove operation recovery across an actual worker/controller
  process kill, qualified transport authentication, or exact-head release CI.
- The Go-owned durable dispatch intent/recovery dependency is now implemented as
  described in [the Go dispatch plan](go-storage-dispatch-plan.md). Its real SQLite
  and killed Go test-process evidence does not prove provider-backed takeover.
  Remaining gates include signed operations, renewable leases and real
  provider-backed controller/worker process-kill/takeover.
  Production transport-authentication confirmation and full ownership cutover remain
  pending. No Python production surface is declared migrated by this journal change.

## Local TLS transport qualification (2026-09-13)

Slice 1 is implemented in this tree. It is **local verification**, not production
authority, not signed operations, and not exact-head CI.

Contract now enforced:

- Rust reads server cert/key; Go reads only a CA file, server name, target, and
  bearer. TLS 1.2+, no `InsecureSkipVerify`, no ambient roots, no client key.
- Bearer is 32–4096 graphic ASCII bytes with at most one hour remaining, checked
  at Go dial/env load, on every per-RPC metadata call, at Rust process load, and
  on every authenticated request. Far-future expiry fails closed.
- Rust maps a complete env to `go-control-plane` / `controller`. Incomplete env,
  wrong role, weak token, unreadable PEM, or expiry failure refuse to listen.
- Unconfigured workers stay plaintext-loopback and fail-closed for storage
  mutation/query (`SERVICE_AUTHENTICATION_UNAVAILABLE`). Admit/QueryEffect/Install
  keep the existing shadow qualification path only while that authenticator is
  the production-fail-closed default. A configured `ServiceBearerAuthenticator`
  guards those RPCs too; missing/wrong/duplicate/expired credentials never reach
  fence or authority logic.
- Go plaintext clients refuse to attach a bearer (`WORKER_PLAINTEXT_CREDENTIAL_FORBIDDEN`).
  Any TLS-related env blocks `DialPlaintextLoopback`.
- Logs, `Debug`, and error strings redact the bearer and PEM bodies.

Local commands (this workspace, 2026-09-13):

- `go test ./internal/worker ./internal/protocol ./internal/action -count=1` exit 0
- `go test ./internal/store -count=1 -timeout=360s` exit 0 (49.937s)
- `go test ./... -count=1 -timeout=600s` exit 0 (pre-OPERATION_INVALID mapping;
  worker/protocol/action re-run after the mapping also exit 0)
- `cargo test -p deepseek-worker --offline --lib --bins --test grpc_service --test tls_transport` exit 0
- `cargo clippy -p deepseek-worker --all-targets --offline -- -D warnings` exit 0
- Real process: `DEEPSEEK_TEST_RUST_WORKER_BINARY=rust/target/debug/deepseek-worker.exe`
  `go test -tags=integration ./internal/worker -run '^TestRustWorkerTLSRealBoundary$'`
  exit 0 (0.16s). Authenticated Admit/QueryEffect/Install reached fence/authority
  rejection; wrong bearer/CA/server-name/duplicate credential did not.

CI: `native-go` now also runs `TestRustWorkerTLSRealBoundary` against the built
worker binary. That is wiring, not an exact-head PASS.

Slice 2 (signed operation grant) remains the next blocking execution chain.
Production cutover, MinIO kill/takeover, and zero-Python images are unchanged.

## Local signed operation grant (2026-09-13)

`control-storage-operation-grant-v1` is a new canonical JSON document. It is not
`control-mutation-request-v1` and not Protobuf. Signing input is sorted JSON plus
domain separator `deepseek-infra:control-storage-operation-grant-v1\x00`.
Existing frozen messages and the descriptor baseline are unchanged;
`canonical_authorization` already existed on `StorageMutationRequest`.

The grant binds action/epoch (must equal the worker's installed live epoch, never
advance it), operation identity, placement/key, CREATE_ONLY/IF_MATCH condition,
object digest/length, and claim revision. Service TLS auth is not a substitute
for a missing grant. Command-field substitution after signing is
`STORAGE_OPERATION_GRANT_COMMAND_MISMATCH`. Replay/nonce/operation-digest
conflicts fail closed. Replay state is in-memory on the worker handle in this
increment; a durable sqlite grant journal is still required.

Frozen corpus: `compat/native-runtime/v31/`. Contract check: 42 corpora, 31
versions. Go and Rust verifiers share the vector. RPC
`ExecuteStorageMutation` admits the grant before any mutation-type or provider
path. Missing grant is `STORAGE_OPERATION_GRANT_MISSING`.

Local commands: store grant tests exit 0; `cargo test -p deepseek-worker
--test frozen_storage_operation_grant_v31 --test grpc_service` exit 0; clippy
`-D warnings` exit 0; `python scripts/native_runtime_contract.py --check` ok;
`TestRustWorkerTLSRealBoundary` exit 0 (authenticated path now reaches grant
admission). No MinIO dispatch or production authority is claimed.

Next: persist grants in the Rust authority sqlite, then have the Go coordinator
request a claim-bound grant from a Rust-owned signer before slice 3 provider-backed
execution. The Go signing helper is qualification/test support only: Go must not
load production signing private keys. Transport approval did not grant a
private-key custody exception.

## Admission hardening after signed grants (2026-09-13)

Regression tests reproduced and now guard three gaps in the new boundary:

- An authenticator returning `SERVICE_AUTHENTICATION_UNAVAILABLE` could select
  the unauthenticated shadow path. Only the explicitly unconfigured qualification
  authenticator may now select that path. A configured authenticator outage
  rejects Admit, QueryEffect and Install; reopening the SQLite journal proves
  that the denied Install did not create an epoch.
- Exact cached grant bytes previously bypassed current time and epoch checks.
  A retry now revalidates signature, expiry, installed epoch, local authority
  configuration and command binding. Only that previously verified document's
  own request ID/nonce is exempted from the in-memory replay sets. This is not yet
  durable grant admission or proof of cross-process writer fencing.
- Go and Rust both coerced non-string optional scope fields to empty strings.
  Validly signed numeric `prefix` and null `expectedEtag` regressions now fail
  `STORAGE_OPERATION_GRANT_INVALID`; legitimate empty strings remain accepted.
  Grant byte length is also checked before worker-side JSON decoding.

The provider regression distinguishes signed-command substitution from durable
operation-identity substitution: an unchanged grant rejects an altered operation
ID at authorization; a freshly signed replacement is still rejected by the
original effect's operation binding, before and after journal reopen. The real
three-MinIO runner passes all 15 tests (6 storage, 9 worker), without skips or
manual insertion of operation effects. This is not a process-kill/TLS/provider
combined proof, nor exact-head release evidence.

The unchanged frozen contract check passes (42 corpora, 31 versions, 43 domains,
7 protobuf files, 10 generated outputs); the shadow comparison passes 8/8.
The Go coverage gate reports 95.1% (4167/4383 statements, unrounded 95.0719%);
the earlier rounded 95.0% result was not used as sufficient margin. Signed grant
payload/envelope/binding validation has 100% statement coverage. These figures
do not imply branch-complete or provider-backed verification.
Rust default-feature targeted regressions pass (57), all-target tests with
`s3-e2e` pass (111), and Clippy with `-D warnings` passes both feature selections.
The all-target run alone does not provision MinIO; the separate 15-test runner
above is the real provider evidence.
`go vet ./...` and `go test ./... -count=1 -timeout=600s` pass. The real Go-to-Rust
TLS test passes against the default, no-S3 worker binary used by `native-go` CI;
the S3-enabled binary is a different qualification target and must not be used
as a substitute for that test's no-S3 UNKNOWN expectation.
The isolated Windows LLVM-MinGW Go race run also passes: `go test -race -p 1
./... -count=1 -timeout=1200s -json`, 15 tested packages and 218 store top-level
test completions, zero race warnings or failed test events. The larger local
deadline was selected after the preceding 600-second store-package timeouts;
CI's command/deadline is unchanged. This is local evidence, not an exact-head
CI result.
Production authority, durable grant replay protection, Rust signer integration,
provider-backed process-kill/takeover and zero-Python deployment remain open.

### Durable grant slice: implementation contract

The next Rust-only authority schema extension is additive; v1/v2/v3 epoch,
effect and operation history is retained byte-for-byte. It records the canonical
signed grant, admission time, request ID/nonce, action/epoch, writer token and
operation/payload binding. Existing effect rows do not acquire inferred grants.

Admission must use one `BEGIN IMMEDIATE` transaction to read the actual database
writer and installed epoch, verify the current grant and command, check durable
request/nonce/operation conflicts, and append the record. A cached handle must
not replace the current database writer check. Exact previously accepted bytes
may retry only after current expiry/fencing checks; retries do not rewrite the
original admission timestamp. Epoch installs and grants share the authority's
request/nonce namespace. An operation ID cannot change its action, epoch or
payload scope under a new signed request.

On reopen, signatures and stored bindings are checked at their recorded admission
times, preserving valid history after a grant expires. That historical validation
does not renew execution permission. Schema, journal or writer uncertainty fails
closed, and new grant acceptance is not a storage dispatch/effect record.
Transaction semantics follow SQLite's [transaction documentation](https://www.sqlite.org/lang_transaction.html);
immutable/replay constraints must also guard replacement statements, not only
UPDATE/DELETE.

### Durable grant implementation and qualification (2026-09-14, in progress)

Rust authority schema v4 now persists grants in `storage_operation_grants`, with
immutable triggers, a shared epoch/grant replay namespace and an indexed operation
binding. The durable worker path no longer uses its in-memory cache for grant
acceptance: current writer/epoch reads, full verification and append occur in the
same immediate transaction. Prior v1/v2/v3 schema definitions remain unchanged.

Seven initial SQLite regressions reproduced five fail-open paths before the
implementation (2 passed / 5 failed), then all seven passed. A further clock-rollback
regression demonstrated that admission could precede its epoch installation;
the write path now enforces the same time boundary as restart validation, avoiding
an append that would make the next reopen reject the journal.

Thirteen grant regressions currently pass, covering replay/nonce/scope preservation,
writer takeover, expiry, immutable rows, corrupted signed history, v3 migration
rollback, reauthorization without rewriting history, and a TLS-connected worker
child that is forcibly killed and restarted. The existing v1 migration regression
also passes and explicitly checks that historical effects receive no inferred
grants. The process test reads the worker's committed grant and exercises RPCs;
it never manually inserts a grant/effect row or configures an S3 transport.
It proves durable authorization across process death, not provider-effect recovery.

The S3-disabled and unconfigured-S3 cases retain distinct exact status/error
expectations. The final worker all-target runs pass 124 tests with `s3-e2e` and
89 with default features; strict Clippy passes both selections. All 15 real
three-MinIO runner tests also pass on the schema-v4 implementation (6 storage,
9 worker, no skipped tests). These provider tests are separate from the TLS
process-death test; they do not yet form a controller/worker/provider kill proof.
The v1 and v2 fixtures now remove all later schema objects when constructing
their historical test databases, preserving strict validation and rollback
assertions; no validator was weakened to accept mixed schema versions.
The unchanged frozen contract check passes (42 corpora / 31 versions) and
shadow reconciliation passes 8/8. The real Go-to-Rust TLS boundary also passes
against the rebuilt default-feature worker (Go test exit 0).
Workspace Rust coverage is being measured with CI's pinned cargo-llvm-cov 0.6.21
and 80% threshold. The local Rust 1.85 GNU distribution lacks `profiler_builtins`
and failed before tests; it produced no coverage PASS. The same-version installed
MSVC distribution includes the profiler runtime and is the next local measurement
target. Earlier hardening checkpoint counts above are not evidence for this schema.
Production authority, a Rust-owned signer called by Go, controller/worker/provider
kill-takeover integration, and the complete zero-Python deployment remain required.

## Session re-verification of the workspace baseline (2026-09-14, 99e055c)

This session started by re-establishing the actual state instead of trusting the
plan text. Findings that change how the remaining chain must be read:

### Verified locally on this HEAD (99e055c)

| Gate | Command | Result |
| --- | --- | --- |
| Frozen contract inventory | `python scripts/native_runtime_contract.py --check` | ok: 42 corpora, 31 versions, 43 domains, 7 proto files, 10 generated outputs, Go 1.27.1 |
| Go module build | `go build ./...` | exit 0 |
| Go full tests | `go test ./... -count=1 -timeout=800s` | exit 0, 15 packages ok (store 52.9s, deepseekd 86.9s) |
| Rust workspace check | `cargo check --workspace --offline` | exit 0 (2m17s), 13 crates |
| Rust worker tests | `cargo test -p deepseek-worker --offline` | exit 0; `grpc_service.rs` 17 passed, `tls_transport.rs` 5 passed |

So the durable-grant / TLS / signed-grant slice described above is genuinely
present and reproducible on this HEAD. Schema v4, `storage_operation_grants`,
TLS transport and `frozen_storage_operation_grant_v31` all hold.

### Gaps in the recorded baseline that must not be silently inherited

1. **The tree is the 4.8.0 lineage, not the 4.9.x/5.0.x lineage the spec plans
   assume.** `VERSION` is `4.8.0`; the newest branch refs are
   `codex/native-runtime-5.0.0-continue` (2026-09-13) and
   `native-runtime-5.0.0-recovered` (2026-09-12). The `tasks/native-runtime/`
   4.9.0–4.9.4 and 5.0.0 plan files describe cutover work that has no
   corresponding commits here. Cutover gates referenced as "4.9.2" / "4.9.3"
   in `release/native_runtime_ownership_v1.json` are **unmet on this tree** —
   every `current_owner` is still `python`/`typescript` for the migration targets.
2. **`4.4.15-foundation-slice.patch` at the repo root is stale and must not be
   applied.** It deletes `VERSION` and `deepseek_infra/core/config.py`. Verified:
   `VERSION` is tracked with **zero** staged or unstaged modification
   (`git diff --cached --stat VERSION` empty, 0 staged files), so the patch
   contradicts the current tree. It is dated to the 4.4.14 → 4.4.15 era.
   Treat it as an abandoned artifact, not a pending change.
3. **`scripts/native_codegen.py --check` cannot run here**:
   `TOOL_NOT_FOUND: protoc-gen-go`. The pinned protoc/plugin toolchain is not
   installed in this environment. The generated-code drift gate is therefore
   **unverified locally** and needs `scripts/` bootstrap or CI. Do not claim it
   passed. `native_runtime_contract.py --check` is a separate check and did pass.
4. **The public edge is genuinely unwired, not merely unqualified.** Verified in
   `rust/crates/deepseek-gateway/src/lib.rs` (1338 lines): `/v1/chat/completions`,
   `/v1/models`, `/mcp`, `/a2a`, `/.well-known/agent-card.json` are **registered**
   routes, but the crate exposes no production listener binding outside
   `docker-compose.native.yml`. `grep` for `deepseek-edge` in non-doc YAML/Python/shell
   finds only that Compose file. `docker-compose.native.yml` itself runs
   `deepseekd` with `DEEPSEEKD_MODE: shadow` and a partial worker env, and its
   comment states plaintext gRPC stays loopback-only "until an authenticated
   authority synchronization channel is selected". No default launcher starts
   the native topology: `launch.py` still imports `deepseek_infra` and defaults
   to `--app`.
5. **The working tree contains a second, unrelated in-flight slice.** The
   modified files include `deepseek_infra/infra/tool_runtime/ocr.py`,
   `media/{ingestion,processors}.py`, `rag/files.py`, `web/routes/media.py`,
   the Android `AndroidOcrBridge.java`, and the untracked `ocr_trace.py`,
   `tests/test_ocr_trace.py`, `RUST_SIDECAR_ANDROID_OCR_TRANSPORT_ANALYSIS.md`.
   That is the OCR correlation/timing work recorded in the 2026-09-13 session
   log — **not** native migration work. It must not be committed, reverted, or
   conflated with this task.

### Consequence for the ordered chain

Sections 1–4 of "Ordered remaining chain" in `migration-matrix.md` are largely
satisfied on this HEAD; section 5 onward (edge behaviour, Go `/api`, per-domain
cutover, launchers/images/Android, exact-head CI) is where the tree actually
stands, and items 6–9 are entirely unimplemented. The migration matrix overstates
progress by marking several domains "shadow"/"qualification" when the corresponding
production entry is still the Python HTTP server.

This session does **not** change `release/native_runtime_5_0_evidence_v1.json`;
it stays `NOT_READY`, which is correct.
