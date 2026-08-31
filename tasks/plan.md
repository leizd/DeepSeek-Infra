# Implementation Plan: 4.8.0 Signed Federation & Cross-Fleet Disaster Recovery

## Overview

DeepSeek Infra 4.8.0 establishes verifiable cooperation between two sovereign
Fleets without shared Authority, global consensus, or remote policy ownership.
It adds dedicated federation signing identity, operator-pinned peer trust,
receiver-controlled ciphertext custody, signed remote replica attestations, and
production federated disaster-recovery drills. Existing storage, encryption,
control, and Evidence wire contracts remain unchanged.

Implementation starts from exact merge commit
`a79c90d237db9323d464b284950a4401dc632d15`. Federation write paths are blocked
until Gate A proves immutable schedule identity, renewable schedule/wave leases,
and provider-backed process takeover without duplicate effects.

## Architecture Decisions

- Each Fleet owns an independent root directory, Authority log, federation root,
  online signer, journals, HTTP service, storage credentials, and failure policy.
- `FleetIdentity v1` uses a dedicated Ed25519 federation root. Age identities and
  Control Authority private state are never federation signing material.
- Peer roots require operator pinning. TOFU, self-declared trust, and automated
  trust or routing decisions are rejected.
- Every signed document uses a versioned domain separator and one repository-wide
  canonical byte encoding. IDs and digests are recomputed by the receiver; caller
  supplied values are never authoritative.
- Transfer identity is the SHA-256 of a domain-separated canonical tuple containing
  source Fleet, destination Fleet, backup ID, and object-set digest. Raw string
  concatenation is forbidden.
- Ingress grants authorize only one transfer identity, prefix, byte ceiling, and
  validity window. Reuse may resume the same durable transfer journal but can
  never create a second effect, enlarge scope, or reset consumed capacity.
- Unknown remote outcomes are reconciled by transfer ID before any write retry.
- Federation transfers existing randomized-Age ciphertext and `object-set-v1`.
  Receiver commits use the existing production Receipt v4 and Commit v4 paths.
- Federated and local durability objectives are independent. A remote copy cannot
  reduce local copy/domain requirements, promote a primary, mutate policy, prune
  replicas, or authorize deletion.
- Historical signature validation and current authorization are distinct:
  revocation blocks new operations immediately, while proof validation applies an
  explicit root/signer certificate validity and `revokedAt` policy.
- Semantic proof validators consume canonical Receipt/Commit documents or an
  equivalent verifiable proof bundle; bare self-declared digests are insufficient.

## Dependency Graph

```text
4.8.0 version surface
        |
        v
Immutable schedule identity -> renewable schedule/wave leases -> real process takeover
        |                                                          |
        +---------------- Gate A hard barrier ----------------------+
                                                                   |
Dedicated Fleet root -> pinned peer trust -> signer rotation/revocation
        |                                      |
        +-> signed readiness -> challenge/response session --------+
                                                                   |
Receiver ingress grant -> immutable transfer journal -> ciphertext custody
                                                        |          |
                                                        v          v
                                                Receipt v4 -> Commit v4
                                                        |
                                                        v
                                             signed replica attestation
                                                        |
                  independent federated durability + pinned failure domains
                                                        |
                     custody mode -> production restore -> signed DR attestation
                                                        |
                                                        v
                         typed proofs -> two-Fleet/four-MinIO exact Evidence
```

## Task List

### Phase 0: Release baseline

#### Task 1: Prepare the 4.8.0 version surface

**Acceptance criteria:** executable version surfaces resolve to 4.8.0; the release
name is Signed Federation & Cross-Fleet Disaster Recovery; frozen protocol
identifiers and fixtures are byte-for-byte unchanged.

**Verification:** `python scripts/check_release_version.py`; focused version and
wire-freeze tests; `git diff --check`.

**Dependencies:** None. **Estimated scope:** Medium.

### Phase 1: Gate A correctness closure

#### Task 2: Make Wave Schedule identity immutable

**Acceptance criteria:** a server-computed `scheduleDigest` binds the complete
canonical immutable schedule; same ID plus same digest is a non-mutating idempotent
result; same ID plus different digest raises `SCHEDULE_IDENTITY_CONFLICT`; running
and terminal schedules cannot be rewritten; existing databases are migrated
without resetting state, waves, actions, timestamps, leases, or epochs.

**Verification:** RED/GREEN unit and SQLite restart tests, including legacy rows,
action-set mutation, authority/risk mutation, and terminal-state replay.

**Dependencies:** Task 1. **Estimated scope:** Medium.

#### Task 3: Add renewable schedule and wave runner leases

**Acceptance criteria:** schedule and wave leases renew together with CAS checks on
both epochs and the runner token; stale owners cannot renew or commit; heartbeat
loss fences the runner before another effect can be created; action-journal lease
fencing remains authoritative for the underlying effect.

**Verification:** deterministic clock tests, long-running executor tests, stale
token/epoch tests, heartbeat shutdown tests, and Action Journal reconciliation
regressions.

**Dependencies:** Task 2. **Estimated scope:** Medium.

#### Task 4: Prove real Wave Runner process takeover

**Acceptance criteria:** Worker A starts a real MinIO-backed effect and is SIGKILLed;
Worker B takes schedule, wave, and action ownership at higher epochs; both workers
refer to one underlying effect ID; no duplicate repair/rebalance effect or terminal
settlement is produced.

**Verification:** dedicated two-OS-process scenario, process/PID assertions,
provider state inspection, typed proof output, and repeatable Windows/Linux CI
execution with no fake S3 or stub crypto.

**Dependencies:** Task 3. **Estimated scope:** Medium.

### Checkpoint: Federation write barrier

- [ ] Schedule identity is immutable and migration-safe.
- [ ] Long effects renew schedule and wave leases.
- [ ] Lease loss fences local commits and reconciles the existing effect.
- [ ] Real process death produces higher epochs and one provider effect.
- [ ] No Federation write API or credential path exists before this checkpoint.

### Phase 2: Fleet identity and explicit trust

#### Task 5: Add dedicated Fleet signing identity

**Acceptance criteria:** create/load an offline-capable Ed25519 federation root and
root-certified online signer; Fleet ID and key IDs are canonical and collision
checked; tests prove key material differs from Age and Authority identities.

**Verification:** key lifecycle, permission, corruption, collision, and identity
separation tests.

**Dependencies:** Task 4. **Estimated scope:** Medium.

#### Task 6: Add the pinned Peer Trust Registry

**Acceptance criteria:** operator-pinned root fingerprint is required; state follows
`PENDING -> VERIFIED -> ACTIVE -> SUSPENDED -> REVOKED`; unknown roots and TOFU
activation fail closed; provider/region/jurisdiction/siteClass are pinned locally.

**Verification:** state-transition, restart, TOFU rejection, metadata mismatch, and
Fleet identity collision tests.

**Dependencies:** Task 5. **Estimated scope:** Medium.

#### Task 7: Add signer rotation and revocation semantics

**Acceptance criteria:** online signers require a valid pinned-root certificate;
certificate time bounds and purpose constraints are enforced; signer/root
revocation blocks new sessions; historical proof behavior follows explicit
`issuedAt`, `notBefore`, `expiresAt`, and `revokedAt` rules.

**Verification:** rotation overlap, expired/future certificate, revoked signer,
revoked root, wrong purpose, and historical-proof tests.

**Dependencies:** Task 6. **Estimated scope:** Medium.

### Phase 3: Signed readiness and session establishment

#### Task 8: Add signed readiness attestations

**Acceptance criteria:** `federation-readiness-attestation-v1` signs the full
canonical readiness snapshot and binds Fleet ID, sequence, digest, signer,
timestamps, and expiry; durable per-peer sequence high-water marks reject replay;
clock skew fails closed.

**Verification:** full-payload binding, risk projection substitution, replay,
expiry, future time, tamper, wrong Fleet, and restart tests.

**Dependencies:** Task 7. **Estimated scope:** Medium.

#### Task 9: Add challenge/response sessions

**Acceptance criteria:** challenges bind nonce, both Fleet IDs, signer, timestamp,
and session purpose; nonces are random, durable for their validity window, and
single-use; replay, reflection, wrong Fleet, expired signer, revoked peer, and
future timestamp fail closed.

**Verification:** bilateral process tests and a complete negative matrix.

**Dependencies:** Task 8. **Estimated scope:** Medium.

### Phase 4: Receiver-controlled remote custody

#### Task 10: Add signed scoped ingress grants

**Acceptance criteria:** `federation-ingress-grant-v1` binds grant/transfer/policy/
backup/object-set/Fleet IDs, object prefix, byte ceiling, nonce, issue time, and
expiry; Receiver authorization is checked on every write; no long-lived Receiver
S3 credential crosses the boundary.

**Verification:** source/destination/binding tamper, prefix escape, byte overflow,
expiry, revocation, replay-after-completion, and same-transfer resume tests.

**Dependencies:** Task 9. **Estimated scope:** Medium.

#### Task 11: Add immutable federated transfer identity and journal

**Acceptance criteria:** Receiver recomputes transfer ID from the canonical tuple;
same identity and content resumes one journal; conflicting fields return
`FEDERATION_TRANSFER_IDENTITY_CONFLICT`; journal states are durable and unknown
results always reconcile before retry.

**Verification:** concurrent proposal, restart, conflicting body, partial upload,
committed-but-response-lost, and blind-retry rejection tests.

**Dependencies:** Task 10. **Estimated scope:** Medium.

#### Task 12: Receive existing ciphertext through production storage semantics

**Acceptance criteria:** Receiver accepts only existing randomized-Age ciphertext
and `object-set-v1`; validates bounded components; commits through production
storage to unchanged Receipt v4 and Commit v4; transfer provenance is external and
does not alter storage wire documents.

**Verification:** wire fixtures, randomized ciphertext, component tamper, provider
restart, Receipt/Commit semantic validation, and no-v2/v5 assertions.

**Dependencies:** Task 11. **Estimated scope:** Medium.

#### Task 13: Add signed federated replica attestations

**Acceptance criteria:** `federated-replica-attestation-v1` is issued only after a
durable commit and binds transfer, Fleets, backup, object set, target, Receipt,
Commit, committed time, sequence, and signer; Sender verifies trust, signature,
transfer identity, canonical Receipt/Commit/object-set binding, and pinned failure
domain before recording `FEDERATED_COMMITTED`.

**Verification:** digest substitution, sequence replay, target metadata mismatch,
tamper, revoked signer, response-loss reconciliation, and delayed-local-recording
tests.

**Dependencies:** Task 12. **Estimated scope:** Medium.

### Phase 5: Federated durability and disaster recovery

#### Task 14: Add independent federated durability objectives

**Acceptance criteria:** support `minFederatedCopies`, `minDistinctFleets`,
`maxFederatedCopyAge`, `allowedPeerFleets`, and `allowedJurisdictions`; local
`minCommittedCopies` and `minFailureDomains` remain unchanged and cannot be reduced
by remote evidence; remote copies cannot promote, mutate, delete, or prune.

**Verification:** policy boundary, stale copy, jurisdiction, local durability
regression, promotion denial, and deletion denial tests.

**Dependencies:** Task 13. **Estimated scope:** Medium.

#### Task 15: Add cold-custody and recovery-capable peer modes

**Acceptance criteria:** COLD_CUSTODY can prove ciphertext custody but cannot claim
plaintext recovery or RTO; RECOVERY_CAPABLE requires an independently
preprovisioned Age identity; Federation APIs and artifacts never transmit Age
private identity.

**Verification:** capability matrix, missing recipient, secret scanning, request/
artifact boundary, and downgrade tests.

**Dependencies:** Task 14. **Estimated scope:** Medium.

#### Task 16: Add the production federated DR drill

**Acceptance criteria:** a recovery-capable Receiver restores through the production
path into an isolated workspace; `federated-dr-drill-attestation-v1` binds transfer,
backup, object set, remote Receipt/Commit, restore/workspace digests, timing/RTO,
cleanup, and source revision; Sender semantically validates every claim.

**Verification:** real restore, corrupted component, wrong lineage, false success,
RTO tamper, cleanup failure, and isolated-workspace tests.

**Dependencies:** Task 15. **Estimated scope:** Medium.

### Phase 6: Typed Evidence and exact real topology

#### Task 17: Add three typed Federation proof validators

**Acceptance criteria:** unchanged `evidence-proof-v2` envelopes carry
`federation-trust-proof-v1`, `federated-replica-proof-v1`, and
`federated-dr-proof-v1`; validators independently check trust chain, rotation,
revocation, sequence/nonce/expiry, grant, transfer, storage bindings, drill
semantics, and non-regression of local durability.

**Verification:** construction plus per-field tamper/recomputation tests.

**Dependencies:** Tasks 7, 13, and 16. **Estimated scope:** Medium.

#### Task 18: Prove two real Fleets and four real MinIO instances

**Acceptance criteria:** independent roots/processes/journals/HTTP servers and
MinIO A1/A2/B1/B2 prove pinned trust, challenge, grant, ciphertext transfer,
Receipt v4, Commit v4, signed commit, Receiver SIGKILL/restart on one transfer ID,
grant replay rejection, attestation tamper rejection, revocation, and production
federated restore; only one committed remote replica exists.

**Verification:** dedicated exact artifact set with PID, endpoint, provider object,
journal, signature, digest, and cleanup evidence; semantic validators must pass.

**Dependencies:** Task 17. **Estimated scope:** Medium per scenario, split into
replication, crash recovery, adversarial, and DR increments.

#### Task 19: Integrate Evidence, CI, runbooks, and release closure

**Acceptance criteria:** Evidence Assembly requires all three typed exact artifacts;
runbooks cover root protection, signer rotation, peer suspension/revocation,
partition handling, incident response, and restore; release docs preserve explicit
non-goals and frozen contracts.

**Verification:** frontend check, Ruff, Mypy, full Pytest with >=95.0% coverage and
margin on Python 3.10/3.11/3.12, retained JavaScript syntax, offline evals,
security gates, release-version check, real topology producers, semantic Evidence
Assembly, `git diff --check`, and clean intended-path review.

**Dependencies:** Task 18. **Estimated scope:** Medium per increment.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Schedule replay mutates active work | effect identity changes under a live runner | immutable canonical digest plus create-once transaction |
| Heartbeat survives ownership loss | stale runner commits after takeover | CAS renewal and commit fencing on schedule, wave, and action epochs |
| Signature ambiguity | valid signature is reinterpreted as another document | one canonical encoding and versioned domain separation |
| Grant replay creates duplicate effects | duplicate remote replica or excess bytes | one durable transfer journal and monotonic consumed-byte accounting |
| Commit succeeds but response is lost | blind retry uploads again | query transfer ID and verify signed committed attestation first |
| Peer self-declares a failure domain | false offsite durability credit | operator-pinned provider/region/jurisdiction/siteClass constraints |
| Remote copy weakens local safety | site-local resilience regresses | separate policy fields and semantic non-regression validator |
| Revocation invalidates evidence unpredictably | incident response or audit ambiguity | explicit authorization vs historical-proof time semantics |
| One-process tests impersonate Federation | false sovereignty claims | independent roots, OS processes, journals, HTTP, and four real MinIO instances |
| Frozen wire drifts accidentally | incompatible restore/storage path | byte-level fixtures and release-wide wire-freeze Evidence |

## Frozen Contracts

Unchanged throughout 4.8.0:

- `object-set-v1`
- Receipt v4
- Commit v4
- FastCDC v3 and Projection semantics
- randomized Age
- `control-authority-v1` and AuthorityCheckpoint v1
- `dr-readiness-proof-v1`
- `evidence-proof-v2` envelope
- `predictive-planning-proof-v1`

New Federation control/evidence documents are not storage wire revisions:

- `federation-readiness-attestation-v1`
- `federation-ingress-grant-v1`
- `federated-replica-attestation-v1`
- `federated-dr-drill-attestation-v1`

## Explicit Non-goals

No automatic cross-Fleet primary promotion, shared Authority log, multi-primary
Authority, Raft/global consensus, cross-Fleet policy mutation, cross-Fleet delete,
automatic local replica pruning, remote replacement of local durability, Age
private-identity transfer, TOFU, LLM trust/routing decisions, Receipt v5, Commit
v5, `object-set-v2`, or `control-authority-v2`.
