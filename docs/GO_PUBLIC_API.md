# Go public `/api` (control-plane edge)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **control-plane subset on `deepseekd`.** Production HTTP is still
Python. The native gateway forwards `/api/*` only when `GO_CONTROL_ADDR` /
`DEEPSEEK_GO_CONTROL_URL` is set.

## What Go serves

| Path | Method | Behavior |
| --- | --- | --- |
| `/healthz` | any | Shadow status (`ok`, `mode`, `mutationAuthority`, `productionMutation`, `shadowStore`) |
| `/api/control/status` | GET | Same JSON as `/healthz` |
| `/api/config` | GET | Go-owned subset: `owner=go`, version, runtime, `hasServerKey`/`hasSearch` booleans, default model, searchModes, mcp/a2a hub flags. **Not** Python's OCR/RAG/budget/toolPolicy blob. Never echoes tokens. |
| `/api/mcp` | GET | Native hub flags (`protocolVersion` 2025-06-18, `externalBridge: false`) |
| `/api/a2a` | GET | Native hub flags (`protocolVersion` 0.3.0, `streaming: true`); restart-safe tasks require the separate mTLS A2A control configuration described in `A2A_HUB.md` |
| `/api/cutover/status` | GET | Cutover record for `?domain=`; `domain` required |
| `/api/workspace/backup-policies` | GET | Existing `{policies, nextRuns}` browser shape from the Go-owned policy table. Requires an allowed Host and the public `AUTH_TOKEN` bearer or `auth_token` cookie (unless `AUTH_DISABLED`), and a durably Go-authoritative `policy` cutover. An unpromoted domain returns `503 GO_CONTROL_NOT_AUTHORITATIVE`; a corrupt history or unavailable store fails closed. |
| `/api/workspace/backup-policies` | POST | Create a policy after durable policy promotion. Server-generated action ID and live execution epoch bind the Go control operation; validation and the record/event commit run against the Go-owned state. |
| `/api/workspace/backup-policies/{policy_id}` | PATCH, DELETE | Update or terminally delete a promoted policy with fenced control-operation admission. Replay does not resurrect the `DELETED` tombstone. |
| `/api/workspace/backup-targets` | GET | Existing `{targets, health}` shape, with both snapshots read in the same Go transaction. Requires public Host/token admission, signed target cutover and the v2 fenced scheduler-health transfer. Historical health rows and null detail are retained. A v1 target import returns `503 TARGET_HEALTH_NOT_TRANSFERRED`; only an attested empty source returns empty arrays. Native provider probes and health refresh remain unimplemented. |
| other `/api/*` | any | `501` `GO_API_NOT_IMPLEMENTED` |

Mutating cutover (`POST /internal/cutover/transition`) stays on `/internal`.
Public `/api/cutover/transition` is **not** registered.

## Internal control plane (`/internal/*`)

The versioned `control/v1.ControlPlane` gRPC service shares this same Go listener
using authenticated h2c; public HTTP/1 routes keep their existing behavior. Every
RPC checks the actual loopback peer, exactly one bearer matching
`DEEPSEEKD_INTERNAL_BEARER` (minimum 32 characters), and a 1 MiB message limit.
`GetBackupPolicyRecipients` reads the durably Go-owned policy inventory and returns
the recipient union plus one typed group per enabled policy, retaining empty groups
so the Rust sealer can refuse them. Shadow ownership returns `FailedPrecondition`,
an absent store `Unavailable`, and an unreadable store `DataLoss`.
The Rust mirror client uses `DEEPSEEK_INTERNAL_BEARER`; it has no HTTP/JSON or Python
fallback. `Health` reports global qualification and `ShadowEvaluate` is a pure,
non-persisting read; individual domain ownership does not claim global cutover.

The internal plane is the operator/control-plane surface: shadow persistence and
snapshot, action dispatch, the mutation-denial probe, authority claim and
head read, cutover status and transition, and production mutation apply. It is
**never anonymous**.

| Requirement | Value |
| --- | --- |
| Credential | `Authorization: Bearer <DEEPSEEKD_INTERNAL_BEARER>`, at least 32 characters |
| Peer | loopback only, independently of `DEEPSEEKD_LISTEN` |
| Comparison | constant time |
| Unconfigured | the routes stay mounted and every request answers `401 INTERNAL_API_UNAUTHORIZED` |
| Refusal | `401` with `WWW-Authenticate: Bearer` and `{"error":"INTERNAL_API_UNAUTHORIZED"}` — the same answer for a missing, malformed, wrong, or unconfigured credential |

| Route | Method | Behavior |
| --- | --- | --- |
| `/internal/shadow/evaluate` | POST | Persist a shadow evaluation only for domains Python still owns; writes to a promoted domain are refused by the durable cutover check. |
| `/internal/shadow/snapshot` | GET | The fenced control snapshot. |
| `/internal/action/execute` | any | Always `403 MUTATION_DENIED`: the unauthenticated in-process mutation path stays mechanically denied. |
| `/internal/action/dispatch` | POST | Plan a native action dispatch (`FENCE_MISMATCH` unless the durable epoch matches). |
| `/internal/cutover/status` | GET | Cutover record for `?domain=`. |
| `/internal/cutover/transition` | POST | The authorized cutover transition. |
| `/internal/authority/head` | GET | Persisted `control-authority-v1` head; `404 CONTROL_AUTHORITY_NOT_CLAIMED` before the first claim. |
| `/internal/authority/claim` | POST | Accept a `control-authority-v1` checkpoint, up to 16 MiB. The store verifies integrity, the live chain, writer lease and deployment cutover capability. Returns `{head, advanced}`; exact replay has `advanced: false`. Invalid JSON is `400`, oversized input is `413`, and an invalid chain or disabled capability is `409`. |
| `/internal/mutation/apply` | POST | **Production apply.** The body is the exact canonical `control-mutation-request-v2` document. `200` with the operation result; a retry is `ALREADY_APPLIED` and never applies twice; `409` with the store refusal code (for example `CUTOVER_NOT_AUTHORIZED`); `413` when the body exceeds the request bound; `503 MUTATION_SIGNER_NOT_CONFIGURED` when the deployment configured no signer. |

The apply route's trust material is **deployment configuration, never the request**:
`DEEPSEEKD_MUTATION_SIGNER_KEY` (the base64url Ed25519 public key), `DEEPSEEKD_FLEET_ID`
and `DEEPSEEKD_ENVIRONMENT`. A caller therefore cannot nominate the signer that authorizes
its own mutation, and an unconfigured deployment refuses rather than accepting anything.

The status/config public subset keeps its existing credential behavior. For
policy, target and mirror paths, the Rust production edge checks the **original**
Host and token before forwarding or serving a native route. Go independently
checks its public Host and `AUTH_TOKEN`, so direct access does not bypass
admission. The `403` Host and `401` auth responses retain the Python error shape.
Policy create/update/delete are implemented against the durably promoted Go
domain; policy run scheduling and execution remain unqualified.

The October 2 isolated browser run created, disabled and deleted a policy through
the real Rust and Go processes. Screenshots and public-list evidence are under
`artifacts/native-20261002-browser-policy-*`. The frontend keeps policies visible
when a separate target or mirror request fails and reports unavailable state.
The target GET now reads the separately imported and fenced Go health snapshot;
native provider probes and periodic refresh remain open.

Rust mirror list/status/upload routes are mounted when the declared data owner
and `DEEPSEEK_RUNTIME_MODE=python_disabled` gate hold. Status/upload obtain typed
policy recipients from `control/v1`; unavailable authority returns `503
NATIVE_MIRROR_RECIPIENT_SOURCE_UNAVAILABLE`. Upload holds the workspace mutation
lock while checking the restore fence, sealing and publishing HEAD. Outside
that mount gate the request reaches the Go proxy's unsupported surface. A mode
flag and store declaration do not qualify mirror ownership: durable source
fencing, attested separate-store import, handback, native restore-consumer and
action/epoch mutation admission are still required.

Current authoritative cutover reads also require the matching immutable
authorization and signed artifact rows, and recheck the artifact SHA-256.
Deleting a signed artifact or changing its digest after disabling and restoring
the database trigger now fails closed. The local browser run used an isolated
store and loopback auth-disabled observation mode; the normal process test
retains Host and token refusal checks. This is not default auth configuration.

`DEEPSEEKD_CONTROL_AUTHORITY=1` lets the process become a production control
authority (a control domain may then be promoted past `dual_evaluate` once a
`control-authority-v1` claim is installed). **`config.Load` refuses that flag
unless an internal bearer is configured**, so the migration authority cannot be
claimed over an unauthenticated channel. Both default to off.
The checkpoint's digest and chain are checked by the store. A promotion now
also requires a canonical `control-domain-promotion-v1` artifact signed by the
separately configured `DEEPSEEKD_PROMOTION_SIGNER_KEY` Ed25519 public key. The
artifact binds the domain, transfer/action ID, live execution epoch, source and
target states, revision/fence CAS, authority tip, Fleet/environment, and a
five-minute validity window. The same transaction stores its exact bytes and
SHA-256 in an append-only v10 journal. An absent key or unsigned request cannot
promote a domain. A retry of an authoritative transition must supply the exact
signed artifact originally journaled, even when the original validity window
has expired. This has local started-process HTTP and store tests; it is not an approved
production cutover or provider-backed cutover evidence.

## Production authority of the worker execution plane

A coordinator that claims production authority (`WithAuthoritative(true)`) does
**not** get it from that flag. All four execution/recovery entry points
(`ExecuteStorageAction`, `ReconcileStorageAction`, `ReconcileClaimedStorageAction`,
`ExecuteClaimedStorageAction`) read the durable `control_cutover` record for the
`action` domain through `store.IsGoAuthoritative`. A claim without that durable
record — or with a larger epoch — is refused with `CUTOVER_NOT_AUTHORIZED`
*before* any durable write, and the cutover record is left untouched. A
coordinator that does not claim production authority keeps the unchanged
non-authoritative qualification path.

## Honest gaps

- Python still owns production `/api/*` (full config, chat, tools, …).
- Native gateway without `GO_CONTROL_ADDR` still answers
  `GO_CONTROL_PROXY_NOT_READY`.
- Unimplemented public paths are 501, not a fake success.
- The policy list has local tests for a nonempty Python-fenced inventory import,
  an attested empty import followed by a signed Go write, shadow refusal,
  authentication and cron/DST/jitter projection against a frozen Python oracle.
  Python's current list also adopts legacy `.backup-policies/*.json` projections
  into its control table. The offline exporter refuses an unadopted projection
  before fencing SQLite, then binds the projection directory's exact state into
  the export manifest (`legacyProjection`: the file count plus a canonical digest
  over each file's name, size and SHA-256). Go re-derives that digest from the
  directory before an attested import, so a directory the SQLite fence cannot
  reach still fails closed when it appears, vanishes or changes. The directory
  scan includes hidden `*.json` names and rejects `.json` directories, as the
  Python list routes do. Go also rechecks JSON validity and that each file's
  ID is present in the fenced SQLite rows, so resealing an invalid projection
  manifest does not bypass source validation. The directory is bound and
  rechecked, not frozen: the source service must stay stopped. Schema v13 stores
  the attested manifest and source/projection locations, then reattests both at
  the first signed promotion. Upgraded unpromoted v11/v12 imports have no
  invented binding and cannot promote until handed back and reexported; an
  already promoted older import blocks the upgrade. The external directory
  can still race the final check, so production ownership transfer is unproven.
  A source outside `.backup-control` now requires an explicit absolute
  projection directory in both the Python exporter and Go attester.
  The `native_integration` Go test applies a signed mutation to an isolated Go
  store, closes it, then starts built `deepseekd` and Rust gateway executables.
  The policy remains visible through the Rust HTTP edge after the Go process
  restart, while foreign Host and missing token requests are refused. This is
  a local binary process test, not installer or browser qualification.
  The public write,
  run, continuity and scheduler paths remain Python-owned.
- **Production mutation application is implemented, proven and reachable.** The approved
  `control-mutation-request-v2` (`apply-mutation`) is verified with full three-language parity
  (Python oracle, Go, Rust; 34 frozen cases) and applied atomically by `Control.ApplyMutation`
  under schema v9, which admits an `APPLIED` journal result. It is served by
  `POST /internal/mutation/apply` behind the internal bearer, with the signer key taken from
  deployment configuration. It refuses a domain that is not durably Go-authoritative, a fenced
  domain (`action`/`scheduler_run`/`wave`/`transfer`), a stale cutover
  revision/epoch/fencing token, and a record revision that is not the next one.
- The frozen `control-mutation-request-v1` still cannot authorize a production mutation, and
  it is not reinterpreted to do so.
- The signed promotion request is locally integrated; independent export/import,
  ownership fencing, rollback and provider-backed cutover evidence remain open.
- No provider-backed process-kill/takeover reconciliation evidence.
