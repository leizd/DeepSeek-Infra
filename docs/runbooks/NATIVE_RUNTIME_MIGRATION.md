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

- Go shadow evaluation may persist qualification records in the Go-owned store.
  `PutShadow` checks each domain in the write transaction and refuses writes
  after its durable cutover to Go. `/internal/action/execute` remains
  `MUTATION_DENIED`; promoted non-fenced control records use signed v2 apply.
- Shadow mode rejects a configured production store path.
- Do not point Go or Rust at Python SQLite files.

## Mirror candidate transfer and cancellation

This offline flow transfers a settled mirror inventory into a disjoint candidate.
It does not admit production mutations or transfer ownership after native effects.
Stop the original writer first and use explicit disposable or approved migration
roots. The source must exist; empty inventory requires an explicit `--allow-empty`.
Neither root may overlap the other. Restore locks/fences, changed bytes, unknown
candidate contents, symlinks and unfinished generations deny the operation.

```powershell
python scripts/native_mirror_handoff.py --source D:/migration/python-mirror --target D:/migration/rust-mirror --transfer-id mirror-transfer-001 --output D:/migration/mirror-transfer-001.json
mirror-inventory-import --manifest D:/migration/mirror-transfer-001.json --source D:/migration/python-mirror --target D:/migration/rust-mirror
```

Export durably fences the original Python writer before publishing the manifest.
Rust independently rehashes the source and copied bytes and persists its candidate
receipt. The imported candidate refuses both Python and native writes pending a
separate production admission protocol. Interrupted copy can resume only against
the same manifest, fence and unchanged inventory.

To cancel before admission, revoke the unchanged candidate first, then return only
the original source writer through the offline verifier:

```powershell
mirror-inventory-import --revoke --manifest D:/migration/mirror-transfer-001.json --source D:/migration/python-mirror --target D:/migration/rust-mirror
python scripts/native_mirror_handoff.py --handback --manifest D:/migration/mirror-transfer-001.json --source D:/migration/python-mirror --target D:/migration/rust-mirror
```

Handback independently checks both inventories and the terminal native receipt,
archives the exact source fence, persists native denial and handback history, and
only then releases the original Python fence. The revoked target remains denied.
Do not delete sidecars or edit receipts to lift a fence. Replays validate durable
history and preserve subsequent original-writer progress. Real Age and interrupted
transfer evidence is recorded in `tasks/native-runtime/continuation.md`; production
Go action/epoch admission and post-effect handback remain unfinished.

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
does not authorize public exposure or production cutover. On `deepseekd` itself
every `/internal/*` request must present `Authorization: Bearer
$DEEPSEEKD_INTERNAL_BEARER` (at least 32 characters) **from a loopback peer**;
with no bearer configured the routes stay mounted and answer `401
INTERNAL_API_UNAUTHORIZED`. `DEEPSEEKD_CONTROL_AUTHORITY=1` — the flag that lets a
control domain be promoted past `dual_evaluate` — is refused by config unless that
bearer is configured, so the migration authority cannot be claimed over an
unauthenticated channel. A configured deployment can inspect the live tip at
`GET /internal/authority/head` and submit the checked `control-authority-v1`
checkpoint to `POST /internal/authority/claim`; an exact replay advances no state.
Promotion past `dual_evaluate` additionally requires a canonical, externally
signed `control-domain-promotion-v1` document in the transition's `promotion`
field. `DEEPSEEKD_PROMOTION_SIGNER_KEY` pins its Ed25519 public key on the Go
deployment; an empty key refuses promotion. The signed document binds the
domain, transfer/action ID, live epoch, transition states, revision/fence CAS,
authority tip, Fleet/environment and expiry. Go stores the exact document and
SHA-256 in `control_promotion_artifacts` in the cutover transaction. Schema v10
refuses to label an existing v9 unsigned promotion history as signed, and
schema-0 rollback refuses retained promotion history. A replay must present
the exact signed bytes already journaled. An isolated Go-store test confirms
those bytes survive restart and writer fencing advances; it does not verify
Python-owned policy or target data import. These are local safety
checks; an isolated export/import and ownership-transfer recovery procedure
still needs evidence. See `docs/GO_PUBLIC_API.md`.

For the source side of that transfer, the offline `python -m
scripts.native_control_handoff` command accepts explicit `--source-db`,
`--checkpoint`, `--output`, `--domain policy|target`, and `--transfer-id` paths.
For the standard `.backup-control/control.sqlite3` layout it also checks the
sibling `.backup-policies` or `.backup-targets` directory before fencing. For a
nonstandard source path, supply `--projection-dir <absolute legacy directory>`
on both export and import commands; the core exporter and Go attester reject
an omitted or relative directory, and the explicit directory must exist. A
policy/target JSON whose ID
is absent from the control table, a malformed file, a symlinked projection, or
a directory whose name ends in `.json` is refused. The scan includes hidden
`*.json` names, matching the Python list routes; target `.checkpoint.json`
sidecars are not inventory records.
The export then binds the directory's exact state into the manifest, and the Go
importer re-derives the same digest from the directory before it accepts an
attested import and independently checks that every JSON file has a matching
ID in the fenced SQLite rows; a nonstandard source path passes that directory
explicitly. Recomputing the manifest digest does not bypass these checks.
A projection directory the SQLite fence cannot reach is therefore **bound and
rechecked, not frozen**: if it appears, vanishes or changes between export and
import, the import fails closed. Schema v13 retains the exact manifest bytes,
source path and chosen projection directory with the attested import. The first
signed promotion rereads the fenced Python source and that directory before its
Go transaction commits; source drift fails closed. An unpromoted v11/v12 import
upgraded without that binding cannot promote: hand it back and export/import
again. A v12 import with promotion history blocks the upgrade and leaves the
old store intact for explicit recovery. Keep
the source service isolated and stopped. The external directory can still
change after the last read and before Go commit, and the two SQLite stores do
not share a transaction, so this is not production transfer evidence.
It requires a stopped Python service, source schema v8, an active recovery
state, a matching live `control-authority-v1` head and inventory, and no
unsettled authority mutation, outbox entry or linked lifecycle intent. In one
`BEGIN IMMEDIATE` transaction it reads the complete source records and installs
SQLite triggers that deny subsequent INSERT/UPDATE/DELETE on that domain. An
append-only fence row binds transfer ID, checkpoint digest and source digest.
The secretless export is published after the fence commits, requesting mode
0600; verify the Windows file ACL before handling sensitive inventories. A
failed file write leaves the source fenced, and the same transfer can republish
it. Embedded private credentials, target payload fields absent from the
checkpoint, and nonempty target receipt-mutation generations are refused until
their native custody and state migration exist.

On an isolated copy, after the Go store has claimed that exact checkpoint and
the domain has entered `dual_evaluate`, the offline Go command can import into
a **fresh** target domain:

```powershell
cd go
go run ./cmd/control-inventory-import --store-dir D:/isolated/go-control --source-db D:/isolated/python-control/control.sqlite3 --manifest D:/isolated/python_policy_inventory_export_v1.json --owner offline-import-1
```

Before opening the Go store, this command opens the explicit Python source
SQLite file **read-only**. It checks schema v8 and SQLite integrity, the live
authority head, the append-only transfer marker, the exact source-table and
marker denial triggers, unsettled outbox/mutation/lifecycle work, and each
actual source row against the export. Both the Python exporter and Go verifier
also reject extra or missing policy/target table columns, so a table that
reports v8 cannot silently drop unrepresented sidecar state. The tests use a
complete source database
created and fenced by Python; a legal command imports one nonempty policy and
leaves the source DB bytes unchanged. The attested import rechecks that source
after opening the Go store and before starting the Go write transaction. It
then checks Python-canonical
manifest/source digests, every row against the live Go checkpoint and generation
maps, a live Go writer lease, the dual-evaluation state, and empty target
record/event history; all Go records and events are written in one transaction.
Schema v11 writes an immutable `control_inventory_imports` row in that same
transaction, including the transfer, authority, source boot epoch, manifest/source digests, row
count, writer fence and whether the live Python source was attested. Schema v13
also stores the exact attested manifest and source/projection locations. The offline
command uses the attested path; a direct manifest-only import records an
unattested source and cannot promote. Imported Go record/event revisions start
at the Python `policyRevision` or target `topologyGeneration` CAS value, with
the first event bound to the import journal. Subsequent writes in an imported
domain must keep the payload CAS field equal to the Go revision. The signed
first promotion of either policy or target must carry the exact manifest and
source digests, use the same transfer ID and authority tip, find no later Go
shadow events, and pass fresh source/projection attestation before commit. This
applies to an empty domain too: the Go store refuses a
signed promotion without a Python-created, fenced and read-only attested empty
source import. Later signed authoritative-state transitions retain the source
digest binding while allowing legitimate Go record changes after the first
promotion. The isolated empty fixture covers a successful signed Go write and
subsequent signed transitions; it is test data, not a production transfer.
A nonzero source
`topology_generation` is refused because the checkpoint does not retain that
independent column. Promotion, drain and placement generations are verified
against and retained in the installed checkpoint, but their native business
consumers are not yet switched. The tests include
source exports generated by the real Python SQLite writer, but no production
source or provider. The exporter also fences the bound authority head, boot
epoch and linked lifecycle/receipt tables; Go verifies all 18 linked trigger
definitions against the Python source before accepting the import. An
unpromoted, unchanged import can be handed back using the Go v12 handback
document and Python's append-only revocation receipt. The two SQLite stores
still have no atomic cross-store transfer, existing Go shadow history cannot
be imported, and state outside the fenced domain remains Python-owned. The
source service-stop precondition remains procedural. These isolated checks do
not promote ownership or authorize a production promotion.

Production authority is durable everywhere it is consumed. The Go coordinator's
four worker execution/recovery entry points (`ExecuteStorageAction`,
`ReconcileStorageAction`, `ReconcileClaimedStorageAction`,
`ExecuteClaimedStorageAction`) read the `action` domain's cutover record; a
coordinator that claims production authority without that durable record — or that
presents a larger epoch — is refused with `CUTOVER_NOT_AUTHORIZED` before any
durable write, and the cutover record is left untouched. Do not treat
`WithAuthoritative(true)` as authority: it is a claim that must be backed by the
record. The frozen `control-mutation-request-v1` still carries only the
`shadow-compare` intent and cannot authorize production mutation. The approved
`control-mutation-request-v2` can apply a signed control mutation through
`POST /internal/mutation/apply`, but only after the target domain is durably
Go-authoritative; its signer public key is deployment configuration. A reused
operation ID from a different domain is a replay conflict, not a reported apply.

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

The default worker build includes the native S3 transport. The binary loads it
before opening its Rust-only journal; incomplete, non-Unicode, invalid credential
or endpoint configuration terminates startup with redacted diagnostics. An absent
S3 configuration retains explicit `STORAGE_TRANSPORT_UNAVAILABLE` responses.
This transport configuration supplies no admission or mutation authority.

| Worker environment variable | Required / default |
| --- | --- |
| `DEEPSEEK_WORKER_S3_ENDPOINT` | Required when any S3 setting is present; exact HTTPS origin |
| `DEEPSEEK_WORKER_S3_BUCKET` | Required; validated S3 bucket name |
| `DEEPSEEK_WORKER_S3_ACCESS_KEY`, `DEEPSEEK_WORKER_S3_SECRET_KEY` | Required; exact credential bytes, never logged |
| `DEEPSEEK_WORKER_S3_PREFIX` | Optional; empty prefix by default |
| `DEEPSEEK_WORKER_S3_REGION` | Optional; `us-east-1` by default |
| `DEEPSEEK_WORKER_S3_SESSION_TOKEN` | Optional; a present empty token is rejected |
| `DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK` | Exact `true` or `false`; default `false`. HTTP is accepted only for IP-literal loopback |

The Go supervisor forwards these dedicated variables unchanged, including invalid
blank values so Rust can reject them. `DEEPSEEK_NATIVE_S3_ENDPOINTS`,
`DEEPSEEK_NATIVE_S3_BUCKET` and ambient AWS credentials remain test harness inputs.
Native Compose can load an optional ignored `.env.native-worker` file (or the path
specified by `DEEPSEEK_WORKER_ENV_FILE`) and mounts a dedicated worker state volume
at `/data`. Authority and TLS settings must also be configured to execute an
authorized mutation; provider credentials alone leave mutations denied. The
optional-file syntax requires Docker Compose 2.24 or later.

`scripts/run_native_s3_e2e.py` includes an actual worker-child test against all
three isolated providers. It uses ephemeral TLS identities and a test signer with
current timestamps, installs a signed epoch, rejects missing service credentials
and unsigned operations, performs a signed PUT, kills the worker, and verifies
query/replay and unchanged provider ETag after restart. It is a local deployment
and recovery test; it does not attest Go ownership cutover or exact-head release CI.

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

### Reversing a policy/target inventory transfer (handback)

An exported `policy` or `target` inventory can be handed back to Python before
any authoritative Go use. The reversal is two explicit steps and never a silent
fallback:

1. **Go abandons its imported copy.** With the source service stopped:

   ```text
   go run ./cmd/control-inventory-handback \
     --store-dir <absolute go-control dir> --domain policy \
     --transfer-id <exact transfer ID> --output <absolute handback.json> \
     --owner <unique offline process id>
   ```

   Go refuses unless the domain is still in `dual_evaluate`, has no promotion
   artifact or cutover authorization, still holds exactly the imported records
   and one event per record, and no other writer or time touched them. It then
   removes the records, their events and the import provenance row, and appends
   the immutable `control_inventory_handbacks` row in the same transaction. The
   command writes `control-inventory-handback-v1`; it does not touch Python.

2. **Python verifies that document and re-owns its tables.**

   ```text
   python -m scripts.native_control_handoff --source-db <absolute control.sqlite3> \
     --transfer-id <exact transfer ID> --rollback --manifest <export.json> \
     --handback <handback.json> --receipt <receipt.json>
   ```

   Python refuses unless the handback binds the same manifest, source, authority
   tip and transfer, and the fenced rows are still byte-identical to the export.
   It journals the revocation append-only, removes the source fence row and its
   guard triggers in one transaction, and publishes
   `python-control-inventory-handback-receipt-v1`. Only then can the Python
   writer write again, and the revocation is what a later audit reads.

Lifting the fence without a verified handback is a data-ownership change: never
do it by editing SQLite by hand, and never restore `control.sqlite3` from a
pre-fence backup to "unblock" a writer. A domain that was actually promoted must
be demoted through the cutover path, not through this command.

### What the fence freezes

The exporter's fence is not limited to the exported inventory rows. While any
fence is held the Python writer is mechanically denied on the control state the
transfer binds:

- `control_authority_head`, `control_authority_outbox`,
  `control_authority_mutations` and `control_boot_state` are **global**: any
  held fence freezes them, because the transfer binds the installed authority
  tip and the source boot epoch the Go attestation compares.
- `lifecycle_intents` and `target_receipt_mutations` are **linked**: only rows
  that name a fenced domain are frozen.

The Go source attester requires those exact 18
`native_control_fence_<table>_no_<operation>` objects byte for byte before it
reads a single row, so a source whose bound state is not frozen is refused
rather than trusted, and a Python service restarted after the export fails
closed with `PythonWriterMechanicallyDeniedError` instead of resuming ownership.

The linked objects are released only when the **last** fence is lifted: revoking
one domain while another transfer is still held keeps them, and the append-only
`native_control_handoff_revocations` journal stays behind as the audit record.
Never drop these triggers by hand to "unblock" a writer.

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


## Typed sealed-mirror recipient boundary (local, 2026-10-02)

The Rust mirror routes read policy recipients through the generated
`control/v1.GetBackupPolicyRecipients` RPC on the existing Go listener. Configure
`GO_CONTROL_ADDR` to a loopback origin and use the same internal credential in
`DEEPSEEK_INTERNAL_BEARER` (Rust) and `DEEPSEEKD_INTERNAL_BEARER` (Go). Go reads its
own authoritative policy records; no Python projection or HTTP/JSON fallback is
consulted. Missing credentials, an unavailable service or an unpromoted policy
domain cause `503 NATIVE_MIRROR_RECIPIENT_SOURCE_UNAVAILABLE` on status/upload,
without sealing a generation. The list endpoint does not need recipient state.

Four October 2 Windows real-process tests pass in
`artifacts/native-20261002-windows-real-process-boundary-fixed.log`. The mirror case
checks actual ciphertext hashes, immutable replay, process kills and persisted
recovery. A killed Go writer retains its recorded lease: immediate restart is
correctly refused as `WRITER_FENCE_HELD`. The test reads that lease in read-only
SQL, waits until it expires, then requires a greater successor fencing token.
It neither rewrites SQL nor reduces the production lease duration.

The October 4 public upload also acquires the workspace OS mutation lock,
rechecks the restore fence under it and holds the lock through HEAD publication.
Sealing runs on a blocking thread so an async runtime can keep serving while the
lock is held by restore. Python's source writer uses that same gate. Isolated
concurrency regressions prove no publication during restore admission, a 423
after its durable fence appears, and a successful seal after recovery clears it
(`native-20261004-mirror-restore-window-fixed.log`,
`native-20261004-mirror-and-evidence-oracles.log`). These are local store checks.

This boundary test does not authorize a mirror data-domain transfer. Durable
source fencing, attested export/import, separate Rust target storage, handback,
restore-consumer and full action/epoch admission remain open. A runtime mode flag
and declaration in the ownership file alone are not acceptance evidence.

## Target health transfer (Go schema v14)

This procedure currently qualifies isolated, stopped source copies only. A target
registry export alone does not include `.backup-scheduler/scheduler.db` health.
Pass `--scheduler-db` to the offline Python exporter to produce
`python-control-inventory-export-v2`; the existing v1 format and frozen v1 corpus
remain unchanged. The new binding includes all health rows, including history
for removed targets, and installs insert/update/delete denial on the Python
health table and its immutable handoff fence.

For a standard `.backup-control/control.sqlite3` layout, the Go importer derives
the sibling scheduler path. For an archived or custom layout, pass both
`--projection-dir` and `--scheduler-db` explicitly to
`go run ./cmd/control-inventory-import` from `go/`. Import does not claim or promote
authority. It verifies both read-only SQLite sources, exact schema and SQL value
types, immutable fencing, authority/transfer/row digests and the projection;
then stores targets, health and provenance in one Go transaction. The first
signed promotion rereads all persisted source bindings. The public target GET
reads only Go state after promotion and refuses older imports without health
proof as `TARGET_HEALTH_NOT_TRANSFERRED`.

Before promotion, `control-inventory-handback` removes targets and health in
one Go transaction and records the original v1 handback document. If output
publication fails after commit, use its explicit `--reexport` option to republish
that exact committed proof. Repeating rollback still fails closed. The offline
Python `--rollback --scheduler-db ...` path verifies the manifest and Go proof,
journals scheduler revocation, then releases control fencing. It recovers an
interrupted second commit or receipt publication from matching append-only
revocation records; changed or mismatched records are refused.

The Python control source, scheduler source, Go store and legacy projection are
not one atomic store. Keep legacy processes stopped throughout transfer and
handback; the local tests do not qualify an online production cutover. Native
provider probes, periodic health writes and mirror transfer remain separate
unfinished capabilities. Read success does not prove provider reachability or
production zero-Python operation.
