# 4.8.0 Todo — Signed Federation & Cross-Fleet Disaster Recovery

## Phase 0 — Baseline

- [x] Verify exact PR #160 merge commit `a79c90d2`
- [x] Create `codex/release-4.8.0-signed-federation` from exact merge
- [x] Lock the dependency-ordered Gate matrix
- [x] Prepare the 4.8.0 version surface
- [x] Prove all frozen protocol identifiers remain unchanged

## Gate A — Correctness closure before Federation writes

- [x] Add canonical server-computed `scheduleDigest`
- [x] Make same schedule ID and digest non-mutating/idempotent
- [x] Reject same schedule ID with different digest as `SCHEDULE_IDENTITY_CONFLICT`
- [x] Prevent running and terminal schedule rewrites
- [x] Migrate legacy schedule rows without resetting state or epochs
- [x] Add CAS renewal for schedule and wave leases
- [x] Add bounded Wave Runner heartbeat and fence on lease loss
- [x] Prove stale runner token/epoch cannot renew or commit
- [x] Prove real Worker A SIGKILL and Worker B higher-epoch takeover
- [x] Prove one real MinIO effect ID and no duplicate effect/settlement

## Fleet identity and trust

- [ ] Add dedicated Ed25519 Fleet federation root and online signer
- [ ] Prove federation keys differ from Age and Authority identities
- [ ] Add operator-pinned Peer Trust Registry without TOFU
- [ ] Add pinned provider/region/jurisdiction/siteClass metadata
- [ ] Add online signer rotation certificates
- [ ] Add signer/root revocation and explicit historical-proof semantics

## Signed readiness and sessions

- [ ] Add `federation-readiness-attestation-v1`
- [ ] Bind the complete canonical readiness snapshot
- [ ] Persist and enforce per-peer readiness sequence high-water marks
- [ ] Reject replay, expiry, excessive future skew, tamper, and wrong Fleet
- [ ] Add bilateral challenge/response with durable single-use nonces
- [ ] Reject reflection, replay, wrong Fleet, revoked peer, and invalid signer time

## Receiver-controlled custody

- [ ] Add signed `federation-ingress-grant-v1`
- [ ] Bind both Fleets, transfer, policy, backup, object set, prefix, bytes, and time
- [ ] Keep Receiver long-lived S3 credentials private
- [ ] Add domain-separated immutable transfer identity
- [ ] Add durable transfer journal and query/reconcile API
- [ ] Make same transfer resume one effect and conflicting transfer fail closed
- [ ] Receive existing randomized-Age ciphertext and `object-set-v1`
- [ ] Produce unchanged Receipt v4 and Commit v4 through production storage
- [ ] Add `federated-replica-attestation-v1`
- [ ] Record `FEDERATED_COMMITTED` only after independent verification

## Durability and federated DR

- [ ] Add independent federated durability objective fields
- [ ] Prove remote copies never reduce local copies or failure domains
- [ ] Prove remote copies cannot promote, mutate, prune, or delete
- [ ] Add COLD_CUSTODY and RECOVERY_CAPABLE modes
- [ ] Require independently preprovisioned Age identity for recovery-capable peers
- [ ] Prove Age private identity never crosses Federation boundaries
- [ ] Run production remote restore into an isolated workspace
- [ ] Add and semantically validate `federated-dr-drill-attestation-v1`
- [ ] Require successful cleanup before DR proof passes

## Typed Evidence and real topology

- [ ] Add `federation-trust-proof-v1`
- [ ] Add `federated-replica-proof-v1`
- [ ] Add `federated-dr-proof-v1`
- [ ] Start two independent Fleet processes and MinIO A1/A2/B1/B2
- [ ] Prove receiver SIGKILL/restart resumes the same transfer ID
- [ ] Prove only one remote commit exists
- [ ] Reject replayed grants and tampered attestations
- [ ] Block new transfers after peer revocation
- [ ] Prove production federated DR restore and signed proof
- [ ] Require all three exact typed artifacts in Evidence Assembly

## Release closure

- [ ] Document trust bootstrap, key rotation, revocation, partitions, and incidents
- [ ] Document cold custody and recovery-capable operational requirements
- [ ] Update ADR, architecture, API, Evidence index, release notes, and runbooks
- [ ] Run frontend, Ruff, Mypy, full 95.0% coverage, eval, security, and release gates
- [ ] Run real two-Fleet/four-MinIO exact Evidence producers and semantic assembly
- [ ] Review intended paths and preserve unrelated worktree assets
- [ ] Do not merge automatically
