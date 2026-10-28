# Native media DELETE lifecycle

The native HTTP controller accepts `DELETE /api/media/{media_id}` only with the
original host/authentication checks, authoritative Go mode and
`DEEPSEEK_RUNTIME_MODE=python_disabled`. It uses the pinned authenticated Rust
worker channel. The public PATCH allowlist remains `title`, `projectId` and
`metadata`; the wider internal Python library is not the public PATCH contract.

## Admission and evidence

DELETE and PATCH share the existing Go media-operation table and its single
unresolved-operation barrier. The v1 envelope retains its legacy `MediaPatch*`
names to preserve stored claims and signed outcomes. A zero `UNSPECIFIED` kind
is accepted only with the original required patch message. DELETE explicitly
selects kind 1, carries no patch and uses a `media-delete-` operation identity.
Unknown and mixed kinds fail closed. No existing request is re-encoded with a
different kind, action identity or execution epoch.

The distinct `ExecuteDelete` RPC admits no caller-supplied state. Rust obtains
the original immutable claim and its dispatch permit from Go, checks the
request digest, identity, epoch, writer fence, lease, fleet and environment,
then uses the same data journal and root mutation gate as PATCH. A signed
successful DELETE has no media object and a deleted count of zero or one;
PATCH, refused and unknown results cannot carry a deletion count. Private keys
remain in Rust. Go tests use ephemeral simulated keys only.

## Data effects and recovery

A missing record returns zero without rewriting the library, advancing its
workspace generation or deleting orphan files. An existing record is removed
from the normalized library, with the original operation's durable publication
witness. Before signing success Rust removes its segment file, object tree and
all `media` collection RAG rows for that source across project IDs. Other media
and a same-ID row in another collection remain intact. Existing indexes are
cleaned independently of search enablement, so changing a configuration flag
on restart cannot waive a pending original deletion.

RAG cleanup opens only the existing profile-owned database and uses a FULL
synchronous transaction. The ordinary row deletion and corresponding `vec0`
row removals commit together. The checksum-pinned `sqlite-vec` Rust dependency
statically links the native extension; no Python package or arbitrary shared
library is loaded. Existing native readers can open a real vector table with
the same module. Opening an ordinary index does not create a vector table or
change its scoring branch. Untrusted symlink paths, views and write triggers
cannot expand the cleanup scope.

An error after library publication leaves `APPLYING` and no signed success.
Reconciliation must match the original authenticated Go claim and the retained
after-image, synchronize that publication, finish only its original local
cleanup and settle the same operation. Missing or old bytes remain
`EFFECT_UNKNOWN`; they never authorize a new publication or fresh operation.
A terminal query replays the original signed result without deleting later
bytes that reuse a former ID. Unix parent directories are synchronized;
portable Windows directory power-loss durability is not inferred.

## Qualification status

This implementation is under cloud qualification. Focused checks retain the
original PATCH journal tests and add real vector cleanup, cross-project
isolation, missing-record behavior, interrupted publication, cleanup failure,
cross-kind admission and original-claim binding. They do not establish whole
product readiness or replace actual authenticated launcher HTTP, crash and
restart probes, all original coverage floors, complete source CI, platform
checks, optional configured backends, backup/restore interaction, physical
power-loss, migration or release evidence. The full product remains
`NOT_READY`. Rust/Go compilation, tests, containers and caches remain in the
cloud; only reports return to the disk-constrained Windows host.
