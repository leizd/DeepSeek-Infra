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
