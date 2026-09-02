# ADR-0048: Signed Federation and Cross-Fleet Disaster Recovery

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Accepted; release qualification pending exact-head CI
- Date: 2026-09-02
- Applies to: v4.8.0

## Context

4.7.x closes the single-Fleet Observe -> Predict -> Plan -> Execute -> Prove
loop. Site loss still requires a separately governed Fleet to hold and prove an
offsite ciphertext replica without sharing Authority, storage credentials, or Age
private identity.

The main correctness hazards are implicit trust, mutable schedule/transfer
identity, replay after an unknown remote result, Receiver credential leakage,
self-declared failure domains, duplicate commits after process death, and allowing
a remote copy to weaken local durability.

## Decision

1. Gate Federation writes behind immutable Wave Schedule identity, renewable
   schedule/wave leases, and real process takeover with one underlying effect.
2. Give every Fleet a dedicated offline Ed25519 federation root and short-lived,
   root-certified online signers. These identities are distinct from Age and
   Control Authority.
3. Require operators to pin exact peer root fingerprints and known
   provider/region/jurisdiction/siteClass metadata. TOFU is rejected.
4. Sign full canonical readiness, challenge/response, ingress grant, replica
   attestation, and DR drill documents with explicit identity, sequence/nonce,
   validity, and content-digest bindings.
5. Make ingress Receiver-controlled and single-purpose. Sender never receives
   Receiver long-lived storage credentials.
6. Derive immutable transfer identity from source Fleet, destination Fleet,
   backup ID, and object-set digest. Unknown outcomes are reconciled by transfer
   ID before any retry.
7. Transfer the existing randomized-Age `object-set-v1`; Receiver uses the
   production storage path to create unchanged Receipt v4 and Commit v4.
8. Count federated durability separately from local durability. Federated copies
   cannot lower local copies/domains or authorize mutation, promotion, pruning,
   or deletion.
9. Distinguish `COLD_CUSTODY` from `RECOVERY_CAPABLE`. Recovery identity is
   preprovisioned out of band and never crosses the federation protocol.
10. Require typed semantic proof in the unchanged `evidence-proof-v2` envelope,
    including real independent Fleet processes, four logical MinIO targets,
    Receiver process kill/resume, replay/tamper/revocation rejection, credential
    isolation, and a production restore drill.

## Consequences

- Fleets cooperate through signed documents and durable effects, not distributed
  consensus. A partition cannot transfer policy ownership.
- Root provisioning and peer activation require explicit operator work. This is
  deliberate operational friction.
- Online signer rotation is monotonic and auditable; root replacement requires
  explicit trust re-establishment rather than transparent rollover.
- Receiver-mediated upload is preferred in 4.8.0. Throughput is secondary to a
  small credential and authorization surface.
- A successful remote commit is insufficient until Sender validates the signed
  attestation and all Receipt/Commit/object-set and pinned-metadata bindings.
- Release qualification remains tied to exact CI artifacts; local tests do not
  promote the version by themselves.

## Frozen compatibility surface

`object-set-v1`, Receipt v4, Commit v4, FastCDC v3, Projection semantics,
randomized Age, `control-authority-v1`, AuthorityCheckpoint v1,
`dr-readiness-proof-v1`, the `evidence-proof-v2` envelope, and
`predictive-planning-proof-v1` remain unchanged.

The new readiness, ingress, replica, and DR attestation schemas are federation
control/evidence documents, not storage wire revisions.
