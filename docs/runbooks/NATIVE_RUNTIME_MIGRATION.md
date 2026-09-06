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
