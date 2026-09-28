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
| other `/api/*` | any | `501` `GO_API_NOT_IMPLEMENTED` |

Mutating cutover (`POST /internal/cutover/transition`) stays on `/internal`.
Public `/api/cutover/transition` is **not** registered.

## Internal control plane (`/internal/*`)

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

The public plane (`/healthz`, `/api/control/status`, `/api/*`) keeps answering
without a credential; only `/internal/*` is gated.

`DEEPSEEKD_CONTROL_AUTHORITY=1` lets the process become a production control
authority (a control domain may then be promoted past `dual_evaluate` once a
`control-authority-v1` claim is installed). **`config.Load` refuses that flag
unless an internal bearer is configured**, so the migration authority cannot be
claimed over an unauthenticated channel. Both default to off.
The checkpoint's digest and chain are checked by the store; this local claim
endpoint does not replace the still-needed externally signed per-domain
promotion artifact and provider-backed cutover evidence.

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
- The internal plane authenticates **this** control plane; a per-domain,
  externally signed promotion request is still outstanding.
- No provider-backed process-kill/takeover reconciliation evidence.
