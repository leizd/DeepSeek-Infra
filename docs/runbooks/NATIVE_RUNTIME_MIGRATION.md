# Native Runtime Migration Runbook

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

Applies to 4.8.1 contract freeze through 5.0.0. Production mutation authority
in 4.8.1 remains Python.

## Ownership at a glance

| Plane | 4.8.1 authority | 5.0 authority |
| --- | --- | --- |
| Public HTTP / data / crypto / transfer | Python | Rust |
| Scheduler / journal / federation control | Python | Go |
| Offline oracle / eval / release tooling | Python | Python (non-production) |

Machine contract: `release/native_runtime_ownership_v1.json`.

## Prove who owns state

1. Read `current_production_authority` in the ownership contract. For 4.8.1 it
   is `python`.
2. Confirm default Compose still starts only the Python service.
3. Confirm `deepseekd` is `DEEPSEEKD_MODE=shadow` and
   `DEEPSEEKD_PRODUCTION_STORE` is unset.
4. Confirm Rust workers are not scheduled against production targets.

## 4.8.2 control-plane shadow

Go evaluates scheduler, risk, wave, and federation decisions only. Compare

`pythonDecisionDigest == goDecisionDigest`

via `python scripts/control_plane_shadow.py --check` and `go test ./internal/shadow`.
Do not cut over mutation until that gate stays green.

## Shadow safety

- Go shadow evaluation writes only decision digests. `ExecuteRepair` and every
  other mutation entry return `MUTATION_DENIED`.
- Shadow mode rejects a configured production store path.
- Do not point Go or Rust at Python SQLite files.

## Go writer lease and runtime shutdown

When `DEEPSEEKD_SHADOW_STORE` is configured, `deepseekd` retains its existing
30-second Go writer lease by renewing every 10 seconds, including during idle
periods. Renewal uses the same fenced `BEGIN IMMEDIATE` transaction and exact-schema
checks as the store. It does not claim an expired lease, increase the writer token,
alter action epochs or domain records, or grant production mutation authority.

The lifecycle supervisor allows at most one renewal attempt at a time. A failed
renewal or a 5-second renewal watchdog stops HTTP admission and cancels request
contexts. HTTP drain is limited to 5 seconds, after which active connections are
closed. Database cleanup may still wait for an outstanding SQLite call; the HTTP
drain limit is not a claimed whole-process termination bound.

`deepseekd` observes `lifecycle.Start(...).Done()` and reports runtime failures as
non-zero exit errors, after resource cleanup. Context cancellation releases the
store before normal exit. Library callers needing terminal status should use
`Start`; the existing address-only `Listen` helper is retained for compatibility.
Neither a successful renewal nor `/healthz` is proof of production ownership.

On renewal failure, investigate the state directory, disk and SQLite lock holder.
Preserve the database and fencing history; do not delete it or silently revive an
expired owner. A new owner must pass the normal fenced claim path. This lifecycle
work does not provide renewable action/resource leases or Go-to-Rust authentication.

Implementation follows Go's [HTTP shutdown contract](https://pkg.go.dev/net/http#Server.Shutdown)
and [ticker behavior](https://pkg.go.dev/time#NewTicker). Local regressions cover a
whole default lease with no requests, real SQLite lock contention, stale/expired
owners, rollback before renewal commit, and an incomplete real HTTP upload during
shutdown. They are not provider-backed action takeover evidence.

`go/cmd/deepseekd/process_test.go` additionally builds and runs the actual Go main
binary against temporary Go-owned state directories. One test acknowledges a
policy over HTTP, leaves the process idle for a full default lease, force-kills
it, and verifies that a replacement is rejected until the persisted lease expires.
The replacement then advances the fence, recovers the exact record digest/history,
and acknowledges a new policy record. The test reads the lease after kill; it does not
edit the journal or advance a fake clock. `Process.Kill` is a forced termination
on Windows and SIGKILL on Unix; local Windows results do not establish Unix results.

A second real-process test holds an actual SQLite write transaction without
changing rows. The listener must close while the lock is held. Once the lock is
released, main must exit with a renewal error and a replacement must acquire the
released fence immediately. These tests run in `go test ./...`; the focused command
from `go/` is `go test ./cmd/deepseekd -run '^TestDeepseekd' -count=1 -v`.
This proves Go process lifecycle and shadow-state recovery only, not a Rust worker
storage effect, remote-write fencing, a production action takeover, authenticated
execution, or release readiness.

## Public Edge to Go API isolation

The Rust Edge forwards only `/api/*` to the operator-configured root origin in
`GO_CONTROL_ADDR` (or `DEEPSEEK_GO_CONTROL_URL`). The origin must use HTTP or HTTPS
and must not include credentials, a path prefix, query, or fragment. HTTP is for
the explicitly configured private development/Compose network, not a public
transport-security guarantee. Do not publish the Go port.

`/internal`, `/internal/`, and `/internal/*` return 404 on both development and
production Edge routers, without contacting Go or falling through to the SPA.
Go's shadow and cutover handlers remain private management APIs; their existence
does not authorize public exposure or production cutover.

Forwarding uses the original encoded path and rejects URL normalization that
changes it. Query order, duplicate parameters and existing escapes are retained;
URL-standard escaping may encode characters such as apostrophes (`'` to `%27`)
without changing decoded query values. CONNECT is rejected before dispatch.
Redirects are returned without being followed. Ambient proxies, automatic retries,
HTTP/2 and idle connection reuse are disabled; there is no Python fallback.
Request and response hop-by-hop headers, including Connection-nominated fields,
are removed. Request bodies remain subject to the Edge body limit; response bodies
are streamed with backpressure and read errors, not buffered into an empty success.
The current bridge has a 5-second connect timeout and 10-second total timeout;
it is not a long-lived SSE or WebSocket transport.

`GO_CONTROL_UNREACHABLE` does not prove that a mutation was unapplied. Callers must
retain action/epoch identity and reconcile an uncertain effect before retrying.
This isolation fix is not a substitute for native authentication, full public API
parity, authenticated inter-service transport or the frozen gRPC/Protobuf boundary.

Verification: `cargo test --locked -p deepseek-gateway --all-targets` includes
loopback HTTP tests for traversal aliases, query/method/body/header fidelity,
redirect confinement, proxy bypass and a real TCP truncated response. These tests
are protocol-boundary regressions, not real-provider or release evidence.

Implementation references: [Axum OriginalUri](https://docs.rs/axum/0.7.9/axum/extract/struct.OriginalUri.html)
and [reqwest ClientBuilder](https://docs.rs/reqwest/0.12.28/reqwest/struct.ClientBuilder.html).

## Current Go-to-Rust worker boundary

- `deepseek-worker` exposes the generated Tonic `Worker` service on
  `DEEPSEEK_WORKER_LISTEN` (default `127.0.0.1:50052`). While transport is
  plaintext, both the Rust listener and Go client accept only IP-literal
  loopback addresses with a nonzero port. Public, wildcard, and hostname
  targets fail before any RPC is attempted.
- The Go client never populates request `live_epoch`; that field is not an
  authority input. It validates the action fence and command family locally,
  accepts only the frozen rejection-code set, and treats nil, malformed, or
  unknown responses as `WORKER_RESPONSE_INVALID`. `QueryEffect` additionally
  requires the returned fence to match exactly and currently exposes only
  `UNKNOWN` with `EFFECT_UNKNOWN` or `PROOF_NOT_AUTHORITATIVE`; unvalidated
  positive or negative effect claims fail closed.
- The checked-in worker process starts with authority uninitialized unless the
  dedicated `DEEPSEEK_WORKER_AUTHORITY_*` public signer/fleet/environment/fencing
  configuration is complete. Configured binaries also require
  `DEEPSEEK_WORKER_STATE_ROOT` and persist epoch/replay state in the Rust-only
  `rust-worker/authority.sqlite3` child path; there is no memory fallback on
  configuration or database failure. See the [worker journal runbook](../NATIVE_WORKER_AUTHORITY_STORE.md).
  Unconfigured workers reject command admission with
  `FENCE_MISMATCH` and reject `InstallAuthoritativeEpoch` with
  `AUTHORITY_REQUEST_SIGNER_MISMATCH`. A valid signed `control-authority-request-v1`
  document is required before a live epoch can be installed. That channel does
  not authorize production mutation, durable effect execution, or leaving
  shadow mode.
- Do not expose this plaintext listener beyond loopback. Production transport
  security, durable replay journals, effect reconciliation, and proof-bound
  execution remain prerequisites for any cutover.

## Go storage dispatch journal (schema v4)

The Go-only control database now retains an append-only `storage_dispatches`
association alongside its existing action history. Rust continues to own its
separate worker database; neither runtime reads or writes the other's journal.

`ClaimStorageDispatch` commits CLAIMED -> EXECUTING, the corresponding event and
the exact action/epoch/operation/placement/condition metadata in one writer-fenced
transaction. It stores no payload, raw authorization, credentials or bearer token.
Only the successful claim caller may send the RPC; a retry or successor must query.

Recovery obtains the exact operation from this journal. An empty operation argument
to `ReconcileStorageAction` means derive it from persisted state; a nonempty argument
must match exactly. Missing legacy bindings, substitutions, unreadable/corrupted
intent or action history do not authorize a query or a replacement mutation.

Stop all controllers before upgrade and retain a consistent backup. The v4 upgrade
preserves v1-v3 history; it does not infer old operation IDs. An older binary rejects
v4, mixed-version execution is unsupported, and migration failure rolls back the
schema and writer claim. `Rollback(0)` rejects nonempty dispatch history with
`DISPATCH_HISTORY_RETAINED`. Do not delete rows or restore an older snapshot to
bypass replay/fencing checks. A future production rollback requires coordinated,
fenced export/import, not a destructive schema downgrade.

See the [dispatch implementation and evidence plan](../../tasks/native-runtime/go-storage-dispatch-plan.md)
for current verification. These changes do not enable production authentication,
signed operation authorization, action/resource leases or ownership cutover.

## Unknown effect

If a Rust worker or remote provider result is missing, malformed, or
`EFFECT_STATE_UNSPECIFIED`, treat it as `EFFECT_UNKNOWN`. Never interpret that
as `NOT_APPLIED` and never retry a replacement side effect until the original
`actionId + executionEpoch` is reconciled.

## Execution fence

Rust and Go effect admission reject `execution_epoch == 0`, empty `action_id`,
and any command whose epoch is not exactly the locally resolved live epoch. A
lower command epoch returns `STALE_EXECUTION_EPOCH`; a missing authority record
or a command that attempts to advance itself returns `FENCE_MISMATCH`. Only the
Go claim/takeover transaction may establish or advance authority, after which a
Rust worker installs that authenticated epoch through its separate authority
update path. Never derive authority from the command's own `live_epoch` field.
Lost Go leases do not authorize a late Rust commit.

## Corpus correction

Canonical corpora are immutable after freeze. To correct a fixture:

1. Do not edit the hashed file in place to make a new implementation pass.
2. Add a new corpus version and record the compatibility reason.
3. Re-run `python scripts/native_runtime_contract.py --check`.

## Rollback

4.8.1 does not cut over production owners. Rollback is `git revert` of the
foundation commits. After a later data-owner cutover, rollback is a fenced
export/import, not dual-write and not automatic Python fallback.

## Commands

The optional [Rust S3 transport](../NATIVE_S3_TRANSPORT.md) now has a separate real-MinIO
byte gate. It does not bypass worker authority barriers or enable production writes.
Run `python scripts/run_native_s3_e2e.py` to provision isolated providers and execute it;
do not treat that transport PASS as a Go ownership cutover or effect-journal proof.

```text
python scripts/native_runtime_contract.py --check
cargo test --manifest-path rust/Cargo.toml -p deepseek-protocol -p deepseek-worker
go test ./...
go test -race ./...
```

Windows race builds require a compatible C runtime, not just a recent Go binary.
Use the [isolated Windows Go toolchain runbook](../NATIVE_WINDOWS_GO_TOOLCHAIN.md)
for the verified compiler pin, checksum, local commands and result limitations.
This does not replace the Linux CI gate or change production toolchain ownership.
