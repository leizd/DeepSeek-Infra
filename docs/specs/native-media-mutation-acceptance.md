# Native media mutation acceptance

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

This is an implementation acceptance slice of
[ADR-0049](../adr/ADR-0049-native-runtime-ownership-inversion.md) and the
[5.0 native runtime specification](5.0-native-rust-go-runtime.md). It retains
the already requested Go control/Rust data boundary and the existing HTTP
contract. It neither authorizes production cutover nor narrows the full migration.

## Existing contract and objective

`deepseek_infra/web/routes/media.py` accepts authenticated
`PATCH /api/media/{media_id}` with an object body of at most 1,000,000 bytes.
Only present `title`, `projectId` and `metadata` fields form the patch. A body
with none of those fields is invalid. The original library updates the first
valid matching record, preserves fields outside the patch, merges metadata,
normalizes the record and returns `{"ok":true,"media":...}` with HTTP 200.
Missing/invalid IDs, malformed bodies and unsupported-field-only patches retain
their existing error semantics. The timestamp advances for a new accepted
operation; replay of the same admitted operation returns its retained result.

The media read dispatcher currently forwards nonreads to the existing Go control
proxy. The source inventory must continue to list media POST/PATCH/DELETE as
unfinished until their actual native implementations and acceptance pass.

## Concrete first counterexample

`scripts/verify_native_media_patch.py` must run only in GitHub Actions on Linux.
It builds the pinned Go control/launcher against the exact cloud source, uses
the actual Rust worker/edge binaries and starts the normal authenticated native
launcher. It observes a fresh native profile and native process tree, stops
its owned processes, then explicitly provisions the existing retained-media
fixture in its disposable profile. It never writes Go control databases,
operation journals, counters, transport identities or custody material.

An offline original-library oracle patches an independent mirror of that
fixture. A normal authenticated native GET must first read the original media;
the public control status must be healthy and an unauthenticated PATCH must be
denied. The same valid title/metadata patch then runs through actual public TCP.
A native refusal is an observed compatibility failure only after these runtime
preconditions and the successful original operation are established. Compile,
startup, fixture and authentication failures are not that semantic counterexample.

For a successful native response, compare the entire public media record except
its dynamically measured `updatedAt`, validate that timestamp against the request
interval, and restart the whole launcher to verify the retained public record.
The first probe covers this one controlled workload; it cannot qualify authority,
loss recovery, all patches, media creation/import/processing or the whole product.

## Required production implementation

* Rust keeps the public listener and read path; the existing Go control path
  admits the write. There is no Python fallback, external task program,
  optional media delegate or cloud execution substitute in the production chain.
* Go alone owns durable admission, operation identity, action leases and writer
  fencing. Rust alone writes media data and a separate durable effect journal.
  Neither side writes the other's SQLite database or journal.
* The internal mutation and reconciliation boundary uses additive versioned
  Protobuf/gRPC messages. Patch-field presence, metadata values, media identity,
  immutable request digest, operation ID and `actionId + executionEpoch` are
  explicitly bound; no new ad-hoc JSON bridge or cgo binding is permitted.
* Reuse the deployment-pinned public identity and authenticated transport.
  Rust custody independently validates the live Go claim and admitted scope.
  Rust private keys never enter Go configuration, heap or wire messages.
* Missing, stale, future or mismatched grants fail closed. A takeover queries
  the original operation and original epoch; it cannot grant a fresh write
  merely because a reply or journal lookup is absent.
* Interrupted or unconfirmed effects remain `EFFECT_UNKNOWN` until the original
  operation is conclusively reconciled. Durable publication and result proof
  must survive loss before/after the data write and before/after reply delivery,
  without a duplicate write, fabricated receipt or lost reservation.
* Concurrency, file confinement, symlink/reparse races, input/resource bounds,
  cancellation, process death and Android/Windows differences are part of the
  write implementation. A local rename alone is not a cross-store transaction
  or proof that Go has durably settled an operation.

## Verification and commands

Local work on this host is limited to source review, formatting, typing,
installed code generation and offline oracles; native compilation/tests and
containers stay in GitHub Actions. The exact-source cloud command is:

```sh
python scripts/verify_native_media_patch.py --worker "$CARGO_TARGET_DIR/debug/deepseek-worker" --edge "$CARGO_TARGET_DIR/debug/deepseek-gateway" --go-cache "$RUNNER_TEMP/agent-signing-go-cache" --out artifacts/rust-cloud/media-patch-process
```

Keep all existing full workspace checks and original Rust80/Go95/Python95 floors.
Collect only bounded reports, never executable packages or compiler caches.
Required write acceptance adds public original/native input/error parity,
independent Go/Rust journal and proof validation, stale/future fencing refusal,
concurrent writes, interrupted delivery, worker/controller takeover and restart.
Coverage, source workflow-dispatch, Rust/browser push, platform and any PR merge
candidate evidence must be kept separate and bound to the exact tested revision.

## Remaining whole-product scope

This PATCH workload is the first write slice. Media create/upload/delete,
processing/indexing and all retained server, desktop, Android, background,
provider, Fleet, recovery, upgrade, sandbox, resource, environment, path-race,
packaging and release requirements remain mandatory. Original arbitrary pytest
and zero production Python remain required together; `start_test_run` remains
unfinished per the human decision. Keep `NOT_READY` until the full objective is
proved. Standing reviewed-upload authorization applies; PR work occurs only at
the scheduled once-daily 09:00 Asia/Shanghai run, with no automatic main merge,
release or real-data operation.


## 2026-10-26 media PATCH implementation candidate — qualification pending

The original authenticated launcher counterexample is retained at source
`b61ac48b49cf13bbb9ef8e33cb2ff504ca2fd19f`: actual native PATCH was HTTP 501 after
healthy control, authentication and media-read prerequisites; the independent
original library accepted the same request. That refusal is not acceptance.

The cumulative `codex/rust-cloud/media-patch-implementation-20261026` candidate
adds typed media v1 gRPC, Go schema-19 operation admission and collection fencing,
Rust-owned media normalization/publication/effect custody, independently verified
purpose-bound Rust receipts and original-operation queries. Go never opens the
media library; Rust never opens the Go control database. Python's retained media
library writer is mechanically denied in `python_disabled` mode. Production
execution has no Python, task-program or optional media delegate.

This is an implementation candidate, not authority/recovery or media completion.
Cloud compilation, regression and actual launcher results remain pending. In
particular an interrupted APPLYING operation without its exact publication
witness remains EFFECT_UNKNOWN, holds the collection and cannot be retried from
an absent/old file. Complete recovery for that case, OS-level path/reparse races,
Windows directory durability, real process-kill/fault/cancellation/takeover tests,
all body/error/large-record parity and Android execution remain open. The bounded
implementation currently limits a library read to 128 MiB and typed messages to
2 MiB; these bounds require compatibility/resource acceptance rather than a
blanket retained-functionality claim. Create/upload/delete/process/index remain
unfinished. Keep all media/product readiness claims false until those pass.

Separately, parent runtime source `b01de5e5ebd6a989c662b23ffff2b3a8feb2aefa`
completed all original 37 CI jobs in workflow_dispatch run 40261365572. Reports
independently retain default Go 95.02168583820479% and Android Go 95.00830922631872%
against their unchanged original 95% floors. Its previously qualified Rust and
platform reports stay bound to that source; they do not qualify this candidate,
a PR merge candidate or 5.0 release. Python/Go/Android-Go 95% and Rust 80% floors
are unchanged. No skip/threshold/ownership/readiness evidence was relaxed.

Native compilation, Go tests and verification containers remain in GitHub Actions;
only reports return locally. Keep primary checkout bytes/index/dirty files intact.
No PR outside the daily 09:00 Asia/Shanghai batch, no main merge, release or real
user/provider-data operation. Full server/desktop/Android/background migration
stays NOT_READY. The original arbitrary pytest engine requirement remains held
unfinished per the human decision; no Python exception or feature reduction.

## Workspace generation and interrupted publication


Media publication must participate in the existing workspace mutation scope.
After validating the retained claim and current library revision, acquire that
scope before entering `APPLYING` or publishing bytes. Its opening and closing
generation changes let a backup reject a snapshot that crossed this mutation.
Failure to persist the opening generation must leave the public library and the
`PREPARED` phase unchanged. Preparation and terminal receipt replay do not publish
bytes and must not advance the generation.

Reconciliation of an `APPLYING` operation with its exact retained publication
witness must enter the same scope before settling its original receipt. This
also invalidates a backup crossing recovery of an interrupted or older writer.
A failed opening generation leaves the operation unsettled; repairing the
counter permits querying the same original operation. Old or absent media bytes
still do not prove non-application or permit redispatch.

Four Rust journal regressions cover these transitions and failures in isolated
fixtures. The normal-auth launcher probe independently retains before/after and
post-restart counter values and hashes alongside the actual raw HTTP replies;
one successful publication must advance the counter twice, while a denied
request, read and whole-launcher restart leave it unchanged. These observations
do not qualify every backup, hostile path, closing-fsync failure, power-loss
recovery, all media operations or the whole product. Existing readiness limits
and original coverage floors remain required.

## Ordered input values and bounded media transport

The public PATCH keeps its original1,000,000-byte JSON bound. Go parses each
supported field directly from its original JSON bytes into a typed value tree.
Object members stay unique and sorted by UTF-8 key bytes on the wire, while
`MediaMember.source_ordinal` records their one-based input positions. Nonzero
positions must form a complete permutation; duplicate/gapped/mixed-zero sets
are rejected before admission. All-zero objects retain legacy sorted-key
semantics. JSON duplicate keys keep their first position and last value.

Rust restores input positions before original media normalization. This matters
when sanitization or80-character truncation makes different keys collide, also
inside nested objects and arrays. The immutable request digest binds positions.
Normalized public outcomes retain the old zero-position encoding, and old
retained values/claims/outcomes stay readable without rewriting signed bytes.
The old frozen descriptor baseline and all existing field numbers remain.
[Protobuf evolution guidance](https://protobuf.dev/programming-guides/proto3/#updating)
requires application compatibility checks in addition to wire compatibility;
these legacy bytes and malformed-order refusals are explicit regressions.

A legal999,994-byte JSON fixture expands to2,499,955 typed bytes. Media-only
RPCs therefore use a4 MiB request/response bound; every other control method
retains its original1 MiB limit and unchanged oversized-message regression.
This changes neither the public HTTP cap nor any original coverage floor.

The actual authenticated launcher probe adds top-level, nested, array,
truncation and maximum-array workloads, each with an independent original
mirror, complete raw public replies and whole-launcher restart. Request hashes
bind compact bytes without sorting. Read-only observations bind the original
Rust effect identity, claim length/hash, APPLIED phase and retained workspace
generation. The maximum-array case must actually persist a claim larger than
2 MiB, proving that the value crossed the typed native boundary. Only reports
return from cloud builds. These checks remain pending for this candidate and
cannot qualify all media APIs, numeric/record-size boundaries, hostile paths,
complete interruption/takeover recovery or whole-product release readiness.

### Schema-20 media persistence capacity

Real native HTTP qualification exposed a separate schema-19 SQLite2 MiB intent
constraint after the media RPC limits had been expanded. A legal JSON scalar
array could therefore fail before admission despite fitting every RPC bound.
Schema20 aligns only media intent/outcome persistence to4 MiB. Schema19 remains
an immutable historical definition, and opening an old store validates it before
migration. The fenced Go writer performs the rebuild, guard recreation, metadata
and migration-ledger update in its existing SQLite transaction. It copies every
original rowid and column without rewriting protobufs or signatures, preserves
all six states and unresolved collection barriers, and fails closed on schema
tampering. Transaction rollback restores the complete old store. Older binaries
must reject the newer version; no destructive downgrade is authorized.

Go regression gates exercise genuine version19 fixtures, signed historical
terminal settlement after reopen, the legal scalar array beyond2 MiB, exact4 MiB
SQL limits, immutability, rollback and retirement denial. The unchanged actual
maximum-array HTTP probe must still succeed, advance generation and retain the
original Rust APPLIED claim across whole-launcher restart. Offline SQL checks
and partial Go success cannot replace that workload or original full-suite and
platform qualification. Whole-product readiness remains NOT_READY.

### Real process-crash and autonomous original-operation recovery

The focused Linux regression holds file-sync return after the real media-library rename,
then observes Rust APPLYING, Go DISPATCHED and the exact planned after-bytes.
[strace's documented path filters and exit delays](https://lists.strace.io/pipermail/strace-devel/2020-March/009399.html)
provide the external fault boundary. A separate tool fixture must first prove
that unrelated paths are not held, the target rename really executes, and the
caller has not returned to userspace. No success/errno substitution, application
test hooks, journal edits or manual transport/key provisioning are permitted.

The original four owned native processes are stopped/killed with PID/start-time
checks. Go and the launcher are stopped before the worker dies, preventing an
in-flight response fallback or supervisor restart from settling the operation
before the controlled restart. Terminal/zombie processes cannot execute late
effects; the launcher child is also reaped. The interrupted request is never
repeated. After a normal authenticated restart, no write request is sent.

Acceptance requires automatic original-operation settlement in both stores,
authentic matching signed outcome bytes, original action/epoch/digest/claim and
writer identity retained, a newer current Go writer, exactly one media operation,
and unchanged library bytes/inode/modification time. Public reads must match the
independent original-library oracle. Pending/unknown or rebound results fail.
The initial test-only source intentionally leaves production unchanged; an
expected red result requires every real crash/restart precondition to pass.
Tracer/setup/build failures do not reproduce the recovery defect. Host power
loss, durable directory recovery, before-publication uncertainty, Windows and
other media operations remain separate incomplete requirements.


The initial tracing fixture failed closed on Ubuntu strace6.8: no rename was
selected by the destination-only path filter. The official implementation
[handles legacy rename through its generic first-path fallback](https://github.com/strace/strace/blob/v6.8/src/pathtrace.c),
while descriptor matching follows an open file's current pathname. The revised
boundary therefore holds the real post-rename fsync/fdatasync return on the
library descriptor. The independent fixture mimics both temporary-file and
post-rename synchronization and requires unrelated paths to remain unheld.
The original file bytes/inode/mtime are now reobserved after restart before
classifying any unsettled operation; lost/changed witnesses are a different
failure and cannot reproduce missing autonomous reconciliation.


## Autonomous recovery of the original published operation

Run40473482975 at df26c547bf2806b652afcaa837c45109f3f3de23 reproduced a process-recovery defect: one authenticated PATCH actually replaced the library, then all owned native roles were SIGKILLed before the signed terminal result. Normal restart advanced the writer fence from2 to3 and retained exact library bytes/inode/mtime, but Go stayed DISPATCHED and Rust APPLYING with no outcome. The probe sent no further write. This is application RED, unlike the earlier setup/tracing failures.

The shared authoritative Go lifecycle now starts a separate media recovery loop and drains it before closing the store. It queries the original operation/fence after writer succession or same-writer claim expiry; it skips a still-live same-writer request. The query-only interface cannot execute or admit a replacement. Existing transactional writer checks and signed-original-outcome verification remain mandatory; absence or unsigned/unknown evidence cannot retire a dispatched operation. Only an expired durable pre-dispatch claim can be cancelled using the existing guarded path. Every query is bounded by the parent cancellation and a five-second timeout.

Cloud unit/race tests and the unchanged actual process probe must qualify this implementation before it is accepted. Proof must retain one operation, original claim/action/epoch/digests, equal Go/Rust signed outcome bytes, unchanged publication and independent Python-oracle public read after restart. Power loss/directory durability, every other media mutation/processing workload and the whole migration remain unqualified.
