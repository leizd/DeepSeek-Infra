# Slice plan — authorized control cutover (`control-authority-v1` claim), Go schema v8

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


Status at 2026-09-28: **slices 1-4 and the internal authority-claim transport are
implemented and locally verified; the blocker they target is partially cleared.**
The isolated HTTP chain now claims the authority head, promotes a domain and
applies a signed v2 mutation. Production ownership and release evidence are still
outstanding. Blocker targeted: `CONTROL_CUTOVER_INCOMPLETE` and the
`production authority` half of `EFFECT_RECONCILIATION_UNPROVEN` in
[`release/native_runtime_5_0_evidence_v1.json`](../../release/native_runtime_5_0_evidence_v1.json).

## Why this is the first executable slice

Phase B of the migration order (Go control state machine, unique writer, migration,
shadow parity) comes before the Rust worker/edge slices, and this slice is the
mechanical precondition for **every** Go-owned domain cutover: today
`store.TransitionCutover` returns `ErrCutoverNotAuthorized` for every state that
requires production authorization (`go_authoritative`, `python_shadow`,
`python_disabled`), so no Go control domain can ever become an authoritative owner.
Measured before this slice: `go/internal/store/cutover.go:209-211` refuses
unconditionally, and `release/native_runtime_go_control_store_v1.json` records
`"go_authoritative": "cutover-not-authorized"`.

Dependencies are met: `go/internal/store/authority.go` already implements the frozen
`control-authority-v1` / `AuthorityCheckpoint v1` verification (integrity, chain,
monotonic head CAS) as a pure library, and `checkpointDocument` covers additive
checkpoint fields on both the Go and Python sides (`_payload_for_digest` in
`deepseek_infra/infra/workspace/backup_control_authority.py` hashes every key except
`digest`/`previousDigest`/`payloadDigest`), so no frozen format changes.

## What is built

1. **Schema v8** (`go/internal/store/authority_state_schema.go`):
   - `control_authority_head` — single row: `authority_generation`, `digest`, `schema`,
     `writer_fencing_token`, `updated_at`; no-delete trigger.
   - `control_authority_checkpoints` — append-only journal: generation, digest,
     previous digest, payload digest, canonical `document`, writer fence, recorded_at;
     no-update/no-delete triggers.
   - `control_cutover_authorizations` — append-only journal binding a promotion to the
     authority tip it consumed: domain, transfer id, authority generation + digest,
     from/to state, previous/next revision, epoch and fencing token, writer fence;
     no-update/no-delete triggers.
2. **Authority claim flow** (`go/internal/store/authority_state.go`):
   `ClaimControlAuthority(checkpoint)` is the only path that may advance the authority
   head. It requires the writer fence, verifies checkpoint integrity, applies the
   monotonic `VerifyAuthorityHeadTransition` CAS, journals the checkpoint and advances
   the head in one transaction. An exact replay of the current tip is idempotent
   (`advanced=false`, no write). `ControlAuthorityHead()` is the read path.
3. **Authorized cutover** (`go/internal/store/cutover.go`): `CutoverTransition` gains
   `Authority *AuthorityCheckpoint`. For a transition whose target is an authoritative
   state, the store now requires **all** of:
   - the deployment opened the store with `OpenOptions.AuthorizeCutover` (default
     `false`; `deepseekd` sets it only with `DEEPSEEKD_CONTROL_AUTHORITY`), and
   - a presented checkpoint that is byte-identically the current persisted authority
     tip (generation **and** digest), and
   - the ordinary legal-transition, revision, epoch and fencing-token CAS.

   The domain's epoch/revision/fencing token advance only inside that transaction, and
   the consumed authority is journaled in `control_cutover_authorizations`.
   De-promotion (`python_shadow` → `go_authoritative`, `go_authoritative` → `shadow`)
   deliberately keeps needing **no** authority, so ownership can always be rolled back.
4. **Default stays fail-closed.** A store opened without `AuthorizeCutover` — which is
   the default `deepseekd` deployment — still answers
   `ErrCutoverNotAuthorized`, so the loopback `/internal/cutover/transition` endpoint
   gains no remotely reachable capability in shadow deployments.

## Acceptance conditions

| Condition | Verification |
| --- | --- |
| A legal promotion genuinely succeeds (not only refusals) | new store test: shadow → dual_evaluate → `go_authoritative` with a valid claim |
| Illegal promotions are refused and write nothing | no claim, wrong digest, stale generation, fork, non-authoritative store, illegal state order |
| Epoch advances only through the authority path | promotion is the only transition that consumes a claim; a larger `ExpectedEpoch` still returns `ErrStaleEpoch` |
| Authority chain is durable and single-writer | head + checkpoint + authorization rows survive `Close`/`OpenControl`; concurrent claims resolve to one advance |
| Movement is auditable and immutable | append-only triggers reject `UPDATE`/`DELETE` on both journals |
| Migration preserves history | v7 → v8 fixture upgrade is atomic, keeps records, and a failed migration rolls back |

## Verification commands

```powershell
cd go
gofmt -l .
go vet ./...
go test ./internal/store/ -run 'Authority|Cutover' -count=1
go test -race ./internal/store/ -count=1
go test ./... -count=1
```

Python-side documentation parity (catalog + its test) is updated in the same slice:
`release/native_runtime_go_control_store_v1.json`,
`tests/test_native_runtime_go_control_store.py`.

```powershell
python -m pytest tests/test_native_runtime_go_control_store.py tests/test_native_runtime_foundation.py -q -p no:cacheprovider
```

## Slice 2 — the internal control plane is never anonymous

Slice 1 deliberately left the cutover capability off because `/internal/*` had no caller
authentication. Slice 2 removes that gap, so the capability can be enabled safely.

1. **`go/internal/api/auth.go`** — `RequireInternalBearer(handler, bearer)`:
   - `Authorization: Bearer <token>` parsed exactly (scheme case-sensitive, a bare
     scheme or a raw token is not a credential), compared with
     `crypto/subtle.ConstantTimeCompare`;
   - **loopback peer required**, independently of `DEEPSEEKD_LISTEN`, so widening the
     listener cannot expose the control plane;
   - a blank configured bearer serves **no** control plane: the routes stay mounted and
     answer `401 INTERNAL_API_UNAUTHORIZED` with a `WWW-Authenticate: Bearer` challenge.
     Missing, malformed, wrong and unconfigured credentials all get that same answer.
2. **`go/internal/api/shadow.go`** — the six then-existing `/internal/*` handlers move onto a dedicated
   sub-mux mounted as `mux.Handle("/internal/", RequireInternalBearer(internal, bearer))`.
   `Register(mux, control, bearer)` makes the credential a required argument, so no caller
   can mount the control plane unauthenticated by omission. `Handler()` (bearer `""`) now
   means "public plane only".
3. **`go/internal/config/config.go`** — `DEEPSEEKD_INTERNAL_BEARER` (minimum 32
   characters) and `DEEPSEEKD_CONTROL_AUTHORITY`. `Load` **refuses** control authority
   without a bearer, so the migration authority can never be claimed over an
   unauthenticated channel. Both default to off.
4. **`go/internal/lifecycle/lifecycle.go`** — passes the bearer to `api.Register` and
   `AuthorizeCutover: cfg.ControlAuthority` to the store. The daemon cannot promote a
   domain unless an operator configured both.

Acceptance: a started runtime serves the control plane with the credential (written
shadow state is observable back through the authenticated snapshot, with the expected
writer identity) and answers `401` without it; a runtime with no configured bearer serves
no control plane at all while `/healthz` and `/api/*` keep working; every route is refused
for every HTTP method without a credential, and the handler is provably never reached.

## Slice 3 — production authority is durable, not self-asserted

Slices 1-2 left two guards that refused the production path exactly when a caller
*claimed* production authority, without consulting durable state. Slice 3 replaces both
with one durable gate.

1. **`store.IsGoAuthoritative(domain)`** — reads the `control_cutover` row inside a
   transaction that verifies the schema, so a closed store, an inactive schema, an
   unknown domain, a broken schema object and a corrupt or missing row are all errors
   rather than a silent "not authoritative".
2. **`Coordinator.assertProductionAuthority()`** gates all four worker
   execution/recovery entry points (`ExecuteStorageAction`, `ReconcileStorageAction`,
   `ReconcileClaimedStorageAction`, `ExecuteClaimedStorageAction`). The
   `WithAuthoritative(true)` flag is a *claim*: it is honoured only when the `action`
   domain's durable record says Go owns it. No claim ⇒ the unchanged qualification path,
   which never consults the record.
3. **`AcceptMutation` keeps refusing post-cutover**, now with the reason written at the
   refusal site: the frozen `control-mutation-request-v1` has one intent,
   `shadow-compare`, with no record body, so it cannot authorize production apply.

Acceptance: with the `action` domain durably promoted, production execution really runs
(durable dispatch binding, provider effect identity, terminal `SUCCEEDED`); without the
promotion every entry point refuses before any durable write and the cutover record does
not move.

## Slice 4 — production apply on a versioned v2 request (APPROVED by the maintainer)

**Decision (2026-09-27):** the maintainer approved adding a production
intent/operation on a **versioned new revision** — `control-mutation-request-v2` with an
`apply-mutation` operation — while **v1 semantics stay byte-identical**. Do not edit v1,
its digest rules, its field list, or the v17 compat corpus.

**Progress: steps 1-5 are implemented locally.** The authenticated apply route
and authority claim/head routes have success and refusal tests. This does not
qualify production cutover.

### Step 3 — DONE: Rust reaches v2 parity

`rust/crates/deepseek-worker/src/mutation_request.rs` gained the same `MutationRequestSpec`
split (`verify_mutation_request_v2_document`, `SIGNATURE_DOMAIN_V2`, `PAYLOAD_FIELDS_V2`),
and `tests/frozen_mutation_request_v32.rs` replays the corpus: all 34 cases, the frozen
digest, and revision disjointness. The v17 suite is untouched and still passes.

Two things worth carrying forward:

- **The float fail-open existed in all three implementations.** Rust's canonical encoder is
  `serde_json::to_vec(sorted(value))`, so Rust would also have accepted a `1.5` record body
  the oracle refuses. `validate_record_body` enforces the oracle's primitive set.
- **Rust's secret rule was already correct** (no safe-suffix exemption, applied to the whole
  document), so the new secret case passed without a change. The three implementations did
  not share one bug; they shared one *class* of bug — and only a cross-language corpus finds
  that.
- One **fail-closed divergence**: Rust refuses an integer outside `i64`/`u64` while Python
  and Go accept it. Stricter, never looser; no frozen case covers it.

### Step 2 — DONE: Go verifies v2 and applies it atomically

- `go/internal/store/mutation_request.go` gained a `mutationRequestSpec` and the v2 path
  (`SignMutationRequestV2`, `VerifyMutationRequestV2Document`) with v1's own entry points
  and bytes unchanged. All **34 frozen cases now replay in Go** through a dedicated
  v32 loader (decoded with `UseNumber`, so a case's numeric replacements behave like the
  frozen document's numbers), plus the cross-revision disjointness test.
- **Two cross-language divergence fixes, both found by the corpus work:**
  1. Go's canonical encoder is plain `json.Marshal`, so Go would have *accepted* a float
     record body that the Python oracle refuses. `validateMutationRecordPayload` now
     enforces the oracle's exact primitive set (`null`/string/bool/integer/list/object)
     and the signed bytes stay reproducible in every implementation.
  2. Go's shared control-record secret scan exempts keys ending in
     `digest`/`reference`/`ref`/`id`/`type`/`provider`; the oracle's mutation-channel rule
     does **not**. That made Go *looser* than the oracle — a fail-open — so
     `rejectMutationBodySecretKeys` applies the oracle's exact rule to the record body,
     and a new frozen case (`secret-suffixed-key-in-record-body`, `myTokenDigest`) now
     pins it for every implementation, Rust included.
- **Schema v9** (`operation_status_schema.go`): through v8 the journal froze
  `result_status = 'PROPOSED'`, which literally could not record an applied mutation. v9
  widens the CHECK to `PROPOSED|APPLIED`, preserves every row, recreates the frozen
  immutability triggers, and is verified at open — a proposed-only journal is refused
  rather than served. The V8→V9 upgrade test proves an operation row survives (losing one
  would permit a double-apply) and that the same request is still an idempotent no-op
  after the upgrade.
- **`Control.ApplyMutation`** is the production channel. It requires the deployment cutover
  capability, a durably Go-authoritative domain, the live cutover revision/epoch/fencing
  token, the request's `actionId + executionEpoch`, the exact body the signer committed to,
  and it refuses a **fenced** domain (action/scheduler_run/wave/transfer) whose mutations
  belong to the lease and admission path. The record write, the operation journal row
  (`result_status = APPLIED`) and the control event are one transaction: a rejected journal
  insert rolls the record back, which a test proves.
- Shadow persistence uses `PutShadow`, with the cutover check inside the write
  transaction for all domains. Direct `Put` refuses unsigned writes to promoted
  non-fenced control domains. This closes a bypass from the shadow evaluation
  route and keeps the signed apply channel authoritative for those records.
- `release/native_runtime_go_control_store_v1.json` and its Python gate now state the new
  truth: v1 still cannot authorize production apply and v2 does. The final local
  scope includes `POST /internal/mutation/apply` and Rust v2 parity.

### Steps 3 and 5 — DONE locally

3. **Rust parity — DONE** (see above).
5. **Apply and authority transport — DONE locally.** The internal plane exposes
   loopback-bearer protected claim/head, cutover and apply routes. The signer
   public key comes from deployment configuration; the request cannot nominate
   its own verifier.

### Step 1 — DONE: the shape is frozen, and the corpus came from the oracle

- `deepseek_infra/infra/native_runtime/mutation_request.py` gained a
  `MutationRequestSpec` and a v2 path (`sign_mutation_request_v2`,
  `verify_mutation_request_v2_document`) while **v1 keeps its own entry points and
  behavior**. The v2 differences are exactly four: the schema identity, the operation,
  the payload field set (adds `recordPayload`), and a **distinct signature domain**
  (`…-v2\0`) so a v1 signature can never be replayed as a v2 document.
- `recordPayload` must be a JSON **object** and is covered by `payloadDigest`, so a v2
  apply can only write bytes the signer committed to. The canonical encoder accepts only
  `null`/string/bool/int/list/object-with-string-keys — **no floats** — which is what
  keeps those bytes identical across Python, Go and Rust; a float body is a frozen
  negative case.
- `compat/native-runtime/v32/` (`manifest.json`, `README.md`,
  `control/mutation_request_v2_vector.json`) freezes the v2 document plus **34 negative
  cases**, all of which were **executed against the oracle before being committed** (the
  vector is generated, not hand-written). SHA-256
  `a650c633430705db871f2d27023f82bff3185e3ef6a0d44ad710f0cb164eb8e6`, `sensitivity:
  public`, RFC 8032 test key only. The 34th case is the secret-key parity case added in
  step 2.
- v32 is registered in `CORPUS_MANIFESTS`, and the corpus gate now pins the new corpus
  **by id and by disjointness from v17** rather than only bumping the manifest count.
- v1 is provably unchanged: the v17 vector and the existing v1 tests still pass, and the
  new test asserts both cross-refusals (v1 verifier refuses a v2 document and vice versa).

### Step 4 — DONE at the store level

A signed v2 `apply-mutation` on a durably promoted `policy` domain produces a real domain
row, an `APPLIED` operation result and a journal event; without the promotion it refuses
and writes nothing; the journal survives a restart and the same request is then an
idempotent no-op rather than a second row (proven by the V8→V9 upgrade test, which replays
after reopening). The transport and its successful HTTP path are covered by
`authority_claim_route_test.go` and `mutation_apply_route_test.go`.

## Open decision for the maintainer — RESOLVED

Production mutation application was blocked by a contract question, not by missing code;
the maintainer approved slice 4, and all five steps have local implementations.
Per-domain external promotion authorization and release evidence remain open.

## Explicitly out of scope (next slices, recorded in continuation.md)

- **Per-domain operator authorization artifact.** The claim authorizes *the deployment*;
  a per-domain, externally signed promotion request is the follow-on design.
- **Unfenced generic mutation.** `MutateProduction` stays mechanically denied.
  Production control mutations use only the signed v2 apply channel on a
  durably Go-authoritative domain.
- Flipping `current_owner` in `release/native_runtime_ownership_v1.json`. That file is a
  frozen 4.8.0 baseline record whose validator pins `current_production_authority` to
  `python`; cutting a domain over is a new accepted contract revision with evidence.
- A deployment surface that *sets* `DEEPSEEKD_INTERNAL_BEARER` / `DEEPSEEKD_CONTROL_AUTHORITY`
  (`docker-compose.native.yml` sets neither, so its control plane is intentionally
  unreachable — including from sibling containers, which is why `DEEPSEEKD_LISTEN=0.0.0.0`
  there is now harmless rather than a hazard).
