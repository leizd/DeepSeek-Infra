# ADR-0044: Production recovery orchestration without a wire-format change

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-13
- Target: `4.5.0`
- Approver: leizd

## Context

ADR-0043 established independently encrypted object sets and true selective fetch. The 4.4.15 Foundation Slice introduced a bounded Component Scheduler and wired it into filesystem object-set publish, but remote publish/download, per-Component recovery, cache, hold renewal, verified-plan reuse, prepared object sets, and disaster-recovery operations remain incomplete.

The current restore session is already the durable identity shared by remote fetch, materialization, safety backup, federated prepare/commit, browser coordination, rollback, and recovery-required handling. Introducing a separate orchestration identifier or a second state database would create two authorities for the same recovery.

## Decision

1. Keep object-set-v1, Receipt v4, Commit v4, randomized Age encryption, Projection semantics, and legacy Whole-Age restore unchanged.
2. Upgrade the existing remote restore session additively into the durable Recovery Job. `restoreId` remains the only job/transaction identity and existing API routes remain supported.
3. Make every ciphertext Component an independently journaled transfer unit. Replace the object-set `componentFetchIndex` write path with digest-keyed states while retaining read migration for existing sessions.
4. Apply one worker/byte/FD/priority scheduler contract to Component uploads and downloads. Completion order remains irrelevant to sorted commitments.
5. Defer Payload HEAD/GET until authenticated Control planning determines the required closure. Report Projection recoverability separately from whole-snapshot Scrub/Drill health.
6. Add a local content-addressed encrypted cache as a verified performance layer. Cache entries never replace Receipt/Control commitments as the trust root, and active/recovery-required jobs pin their required entries.
7. Persist an authenticated-input-bound Projection Plan so materialization can reuse Preview work without redecrypting Controls.
8. Replace fixed recovery holds with renewable, generationed leases retained through pause and recovery-required.
9. Build object-set incremental candidates as Prepared Object Sets, use exact prepared bytes for Adaptive Full, and encrypt prepared Components directly.
10. Trust a provider checksum only after a capability probe proves authoritative full-object SHA-256 semantics; otherwise retain full readback. ETag is never a checksum.
11. Add read-only preflight, readiness, and isolated manual Recovery Drill capabilities. Drills reuse the production recovery engine but have no live Workspace commit capability.

## Alternatives considered

### Introduce object-set-v2 with orchestration fields

Rejected. Job control, cache, leases, and telemetry are local/control-plane concerns. Encoding them in Receipt/Commit would create an unnecessary compatibility migration and would not solve process orchestration.

### Create a separate `recoveryJobId`

Rejected. It would duplicate phase, protection, and transaction ownership already keyed by `restoreId`, introducing split-brain reconciliation after crashes.

### Treat cache presence or provider ETag as proof

Rejected. Cache corruption and multipart ETag semantics make both unsafe. Expected ciphertext SHA-256 and size remain the integrity authority.

### Validate every Payload with HEAD at restore creation

Rejected. It makes selective restore startup proportional to total snapshot Component count and conflates one Projection's recoverability with whole-snapshot health. Scrub and Drill own full-health validation.

## Consequences

- Existing clients and committed backups remain compatible.
- Restore session schema and local runtime state gain additive fields and read migrations.
- Operators obtain pause/resume/preflight/readiness/drill controls without a second recovery authority.
- Retention depends on renewable lease health; lease renewal failure must halt unsafe progress and become visible.
- Cache and parallelism improve performance but add bounded runtime state, GC, telemetry, and fault-injection obligations.
- Release readiness now requires behavioral Evidence from real MinIO, real Age, process restart, and faults; function-existence checks are insufficient.

## Security and privacy

Cache names and metrics expose ciphertext identity/size only. Logical paths, Project/Contributor identity, plaintext hashes, secrets, and credentials remain excluded. Cached and downloaded ciphertext is always reverified; plaintext ZIPs are short-lived and scrubbed immediately after extraction. Recovery Drill roots are isolated and cannot call the live federated commit path.

## Compatibility and non-goals

No object-set-v2, Receipt v5, Commit v5, convergent/deterministic Age, plaintext CAS, cross-snapshot Component reference, privacy-padding protocol, or automatic weekly Drill is introduced in 4.5.0.
