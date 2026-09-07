# Native worker authority journal

This is a Rust worker restart-safety substrate, not production storage authority.
`control-authority-request-v1` and the existing Protobuf messages are unchanged.

## Ownership and startup

An authority-configured `deepseek-worker` requires `DEEPSEEK_WORKER_STATE_ROOT`.
It exclusively owns `<state-root>/rust-worker/authority.sqlite3`; Python and Go
must not open or write that database. An unconfigured worker remains denied and
does not create state. The binary never falls back to an authority-enabled memory
store when the directory, database, or configuration is unavailable.

Only an atomic, exclusive file creator may initialize a database; another first
opener cannot reuse a stale observation that the file was absent.
The state directory and its ancestors must be controlled by the trusted operator
and inaccessible to untrusted local writers. Symlinks and Windows reparse points
are rejected. A worker-specific SQLite application ID is inspected before an
existing file is opened writable for hot-journal recovery. After recovery the
exact versioned schema and every retained signed installation are verified.
Foreign, unmarked, empty existing, unknown-version, and corrupted databases are
rejected, not automatically initialized or repaired. An interrupted first-time
initialization can therefore require operator investigation.

## Atomic installation and fencing

`BEGIN IMMEDIATE` serializes installation against other connections. Within one
transaction Rust reads the live writer token, latest action epoch, and indexed
request/nonce reservations, then runs the existing canonical/signature verifier.
The signed request must match the RPC's `actionId` and `executionEpoch`. One
append-only insert records the request, epoch, nonce, writer token, and acceptance
time. A response is returned only after commit. There is no separate in-memory
epoch update that could succeed while the database write failed.

The journal uses SQLite `synchronous=FULL`. Transaction behavior and durability
follow the [SQLite transaction contract](https://www.sqlite.org/lang_transaction.html)
and [synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).
The Rust binding is the workspace-pinned `rusqlite` 0.37.0.

Every durable admission reads the current database token and latest installed
epoch. A higher operator-configured fencing token revokes old worker handles;
it does not authorize that successor to use installations from an older token.
New signed installations must still increase the action's historical epoch.
Request IDs and nonces are retained across restarts and token changes.

The public signer, Fleet, and environment binding cannot be silently changed.
Key rotation needs a future explicitly authenticated migration. Unsigned local
epoch installation is denied on a durable worker. The in-memory constructor is
retained for isolated library/compatibility tests, not selected by the configured
binary.

## Recovery and limits

Installation-request expiry limits acceptance, not the lifetime of a persisted
epoch. On restart signatures are checked at their recorded acceptance time.
That epoch is still not a renewable execution lease or permission for provider
effects. Storage, transfer, signing, and unbound proof commands retain their
existing not-authoritative errors; unknown effects stay UNKNOWN.

Do not roll back this database to an older snapshot, delete replay history, or
replace it with an empty file as a rollback procedure. Stop the worker and
preserve the complete database/journal for diagnosis. Removing production
authority and handing ownership back requires the separate control-plane cutover
protocol; this store does not implement it.

Startup verifies retained requests incrementally, one bounded document at a time,
but total verification time grows with history. Retention/compaction, authenticated
signer rotation, effect persistence, renewable leases, provider fencing, production
cutover, and the full startup/performance SLO remain open.

## Verification scope

Tests cover real SQLite, independent connections, concurrent installation,
request/nonce reuse, stale epochs/tokens, late-statement rollback, foreign schema,
signed-record corruption, and an actual Rust binary killed after its gRPC
acknowledgment then restarted. A separate terminated SQLite subprocess forces
dirty-page spill and proves hot-journal rollback preserves the prior committed
epoch/token. Its uncommitted padding is fault injection, not effect evidence.

These tests neither contact MinIO nor prove exactly-once storage effects. Real
Three-MinIO and two-Fleet/four-MinIO execution gates remain required.

## Storage reconciliation: negative observations are not terminal proof

The optional S3 worker journal must keep `EFFECT_UNKNOWN` when HEAD finds no
object or observes metadata belonging to a different write. A pending request can
still arrive after that observation. Strong consistency concerns completed writes,
not cancellation of in-flight writes; see the [S3 consistency contract](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel).
Such observations are recorded as diagnostics, not `REJECTED`, and the original
action/epoch remains blocked from blind retry.

`authorized_storage_provider.rs` now exercises this with a TCP relay that captures
the real signed worker PUT, disconnects the worker, waits for HEAD/reconciliation,
then delivers the unchanged request to real MinIO and verifies its 200 ACK and
downloaded bytes. It covers both an absent target and an old object awaiting a
conditional overwrite. A separate relay case drops a real successful ACK.
These cases do not seed the effect journal manually. Reopening a worker handle is
not a process-kill/takeover test and must not be described as one.

Matching user metadata alone does not prove stored byte integrity. The optional
S3 path now requires the placement binding and conditional byte verification below;
this is still not qualified production reconciliation. Renewable authority,
operation-specific signed admission and real process-kill/takeover evidence remain
prerequisites. Existing terminal records from unqualified builds must not be
silently accepted as release proof or rewritten as a rollback shortcut.

## Additive v2 storage intent binding

Schema v2 retains the exact v1 tables and adds `storage_effect_bindings` and
immutability/dispatch triggers inside the same startup `BEGIN IMMEDIATE` transaction.
Identity or journal verification failure rolls back the entire extension, including
`user_version`. The v1 migration regression preserves the original signed request
bytes and effect row. It constructs an isolated historical fixture, not provider
evidence or a production downgrade mechanism.

Every new `execute_storage_put` reserves the transport placement fingerprint
(canonical endpoint, region, bucket, prefix) and exact condition atomically with
the parent key, SHA-256, length, action/epoch and signer identity. Conditional create
has no expected ETag; conditional replacement stores its exact strong If-Match ETag.
Length is exact, including zero. Credentials are excluded from the fingerprint;
see `NATIVE_S3_TRANSPORT.md` for its encoding. The fingerprint does not authenticate
the provider's bucket owner or make an install-epoch request an operation grant.

Both binding and parent identity fields reject changes. Duplicate-key BEFORE INSERT
guards cover both composite keys and explicit rowid conflicts to reject `INSERT OR
REPLACE`, which can bypass UPDATE/DELETE triggers under SQLite's default
recursive-trigger setting ([SQLite conflict handling](https://www.sqlite.org/lang_conflict.html)).
No binding is backfilled for historical
rows. Legacy unbound rows cannot enter DISPATCHING or positively reconcile, even if
their old terminal state says CONFIRMED. Their history remains available for diagnosis.

Stop all workers before upgrading the journal. Older binaries reject the v2 schema
on startup; mixed-version live execution is not supported or qualified by these guards.
There is no downgrade that deletes binding or replay history.

Reconciliation checks placement before returning any historical terminal result.
For UNKNOWN, a matching HEAD is followed by a conditional GET bound to ETag, optional
version ID and all six operation/digest metadata fields. The complete stream must
match the reserved length and SHA-256 before CONFIRMED is recorded. GET errors retain
EFFECT_UNKNOWN. `bytesVerified` is recorded only after this verification, using JSON
serialization (quoted ETags must remain valid JSON). A previously confirmed record is
a historical effect observation, not a claim of current object availability or lease.

Fault relays keep the same configured origin for PUT, HEAD and GET; switching to a
direct provider alias for reconciliation would violate the placement binding. They
forward real signed requests and provider responses unchanged and count observed
GETs and unexpected writes. Counts describe observed traffic, not absence of future
requests. This remains library/provider development evidence,
not a worker process-kill/Go takeover or exactly-once proof.
