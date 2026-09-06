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
