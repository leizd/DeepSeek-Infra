# Go durable storage dispatch intent

Status: Go schema v4 and coordinator dispatch/recovery are implemented locally;
production authentication and authority remain disabled. This is a dependency of
full native ownership, not the 5.0 completion gate.

## Contract

- Go owns its control database and a new append-only `storage_dispatches` table.
  Rust still exclusively owns its worker journal. No cross-runtime database access.
- Schema v4 adds the table and guards without changing frozen v1-v3 domain/history
  tables or inventing bindings for historical actions. The metadata version ceiling
  must be expanded transactionally, preserving its identity row exactly.
- A dispatch intent contains action/epoch, exact operation ID, mutation kind,
  provider placement identity, key, SHA-256, exact length, condition/ETag, request
  and nonce identities, protocol schema version, and authorization-document digest.
  No payload bytes, private keys, raw credentials or bearer token are persisted.
- The CLAIMED -> EXECUTING revision, corresponding control event, and immutable
  dispatch intent commit together under the existing live Go writer fence. Only
  the caller that wins this transaction may send the mutation RPC. A failed final
  insert must roll back the state, event, intent and lease renewal together.
- Recovery reads the persisted operation and exact action/epoch. Absent or changed
  associations do not authorize a query under an invented ID or blind redispatch.
  A worker response must match the durable binding before terminal settlement.
  `ReconcileStorageAction` with an empty operation argument derives the stored ID;
  a nonempty argument is an exact assertion. Missing/substituted bindings return
  `STORAGE_DISPATCH_UNBOUND` before any query or state change. Legacy rows are not
  adopted, and unreadable/corrupted history fails closed.
- Metadata validation is not signed operation authorization. Current qualification
  RPCs remain unqualified for production until authenticated transport, signed
  operation grants, renewable action/resource leases and takeover gates are complete.
- Stop all controllers before schema upgrade. Old binaries reject v4; mixed-version
  execution is unsupported. Failed upgrades roll back. Downgrade cannot erase dispatch
  history or reset its replay binding. Original recovery plans remain untouched.

## Verification

1. Actual SQLite additive migration and failure rollback, including retained legacy
   records/history and downgrade denial once dispatch history exists.
2. Intent/state/event atomicity, immutable update/delete/replace guards, validation,
   exact identity and byte-free metadata, stale writer and concurrent claim races.
3. Coordinator dispatch/recovery uses the stored binding and never adopts legacy
   EXECUTING/EFFECT_UNKNOWN actions. Real reopen and killed Go controller tests.
4. Actual Rust boundary and provider-backed process-kill/takeover qualification;
   unit doubles or manual journal fixtures are not provider evidence.
5. Go format, vet, full tests, coverage >=95%, frozen contract/codegen checks, then
   exact-head CI evidence before any release/production PASS claim.

## Local evidence (2026-09-08)

- Reproduced the prior coordinator sending RPC before any durable intent, mutating
  caller-owned requests, accepting a substituted recovery operation, and changing
  legacy EXECUTING state before querying. Real SQLite regressions now pass.
- Schema migration preserves v3 records/history and rolls back both partial DDL and
  a failed successor writer claim. Existing v1/v2 migration fixtures were updated
  for the current version without changing the frozen corpus or transition rules.
- Claim/state/event/intent/lease commit together. Final-insert failure and expiry at
  commit roll back together; concurrent claims have exactly one winner. Update,
  delete, composite/rowid replacement, corrupted intent/digest/parent/history and
  weakened guard regressions pass. Dispatch history cannot be erased by downgrade.
- A separate Go test process runs the real coordinator/store, reaches a committed
  dispatch barrier and is force-killed. A successor is rejected until real lease
  expiry, takes the next writer fence, retains the original dispatch and queries
  instead of redispatching. The worker is a double returning uncertainty: this is
  neither a provider-effect test nor a deepseekd-main action integration test.
  Windows forced termination is not Linux SIGKILL evidence.
- Go 1.27.1 full tests and vet pass. The unchanged internal/pkg coverage gate passes
  at 95.5%; this excludes generated-only packages and cmd, and does not measure the
  killed child's execution coverage. Contract/codegen checks pass (40 corpora,
  29 corpus versions, 43 domains, 7 proto files, 10 generated outputs).
- Rebuilt the actual no-S3 Rust worker with Rust 1.85 GNU. Four real Go/Rust gRPC
  boundary cases pass, including an actual authentication rejection whose ACK is
  discarded before Go reopens and queries using its stored operation. The worker
  stayed uninitialized/mutation-denied; no provider was contacted. It was reaped.
- `go test -race ./...` did not execute tests on this Windows host: every test
  executable failed to load with `0xc0000139`. An isolated race binary reproduces
  the failure. Its PE imports request `WakeByAddressSingle`, `WakeByAddressAll` and
  `WaitOnAddress` from kernel32.dll; direct export lookup confirms all three are
  absent there. The selected GCC 8.1 toolchain reports MinGW runtime 6.0 in
  `_mingw_mac.h`, below the
  [Go race detector's MinGW runtime 8 requirement](https://go.dev/doc/articles/race_detector#Requirements).
  No tests were skipped, no runtime libraries were replaced and no global compiler
  settings were changed. Race PASS remains outstanding pending a compatible
  toolchain or the existing Linux CI gate; no unchanged failing command was retried.
- No provider takeover, production cutover or exact-head CI PASS is claimed.

## Design sources and remaining gates

- Schema ceiling changes follow SQLite's documented
  [create/copy/drop/rename migration](https://www.sqlite.org/lang_altertable.html#otheralter)
  inside the existing Go writer transaction. History is not rebuilt or backfilled.
- Request isolation uses the existing protobuf dependency's
  [deep clone](https://pkg.go.dev/google.golang.org/protobuf/proto#Clone).
  Unknown request/fence/precondition fields are rejected before admission because
  the v1 intent cannot preserve their meaning. Payload length is bounded by Rust's
  8 MiB chunk limit and authorization bytes by its 16 KiB request limit. The Go
  metadata projection does not hash/verify payloads or authenticate grants.
- Production dispatch still needs signed operation grants, renewable action and
  resource leases, atomic admission budgets/locks, proof-qualified settlement, real
  provider-backed controller/worker kill/takeover and approved transport auth.
  Default production images and all server/desktop/Android feature ownership must
  still become zero-Python before the full migration can be declared complete.
