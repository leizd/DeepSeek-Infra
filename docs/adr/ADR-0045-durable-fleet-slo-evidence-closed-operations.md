# ADR-0045: Durable Fleet SLO and evidence-closed autonomous operations

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-28
- Target: `4.7.4`
- Approver: leizd

## Context

4.7.3 executes genuine MinIO repair and rebalance effects and provides fenced
action execution, but its autonomous report can still be satisfied by proof
fields constructed by the scenario. Risk age and scheduler fairness are derived
from ephemeral action/caller state, only the first execution batch is emitted,
and transfer reservation and preemption helpers are not consistently enforced.
These gaps prevent forensic reproduction and durable Fleet SLO claims.

## Decision

1. Read Receipt and Commit objects back from the exact resolved target store and
   bind proof to endpoint, bucket, object keys, raw-object hashes, Receipt v4,
   Commit v4, backup identity, action identity, and object-set digest.
2. Make the exact autonomous proof a required artifact owned by the Storage
   Control Plane producer. The report names its path and SHA-256; producer
   staging and Evidence Assembly independently validate the same bytes.
3. Persist exact RiskSubject observations, policy virtual service history, and
   Fleet SLO samples in additive SQLite tables under `.resilience-journal`.
4. Derive risk debt from the current unresolved observation interval and derive
   weighted fairness from persisted service, never caller-supplied history in
   production.
5. Partition the complete dependency graph into sequential, conflict-free waves.
   Every candidate is assigned once or receives a typed unschedulable reason.
6. Reuse the existing P0-P6 transfer budget as the enforcement authority. P2
   repair reserve cannot be consumed by P5 rebalance traffic.
7. Permit preemption only for `PENDING` or `CLAIMED + NO_EFFECT` victims, and
   commit victim transition, lock/budget release, and critical-repair claim in
   one action-journal transaction.
8. Simulate running effects and proposed actions together. A healthy baseline
   retains policy minima; an already-degraded baseline cannot lose another
   committed copy or failure domain.
9. Add an authenticated, read-only Fleet Readiness endpoint assembled from
   durable ledgers. Existing API response shapes remain unchanged.

## Alternatives considered

### Keep proof inside the summary report

Rejected. A report that merely repeats validator inputs cannot reproduce the
exact proof bytes that caused PASS, and it cannot be independently hashed or
revalidated by Evidence Assembly.

### Use action creation time as risk age

Rejected. Planner runs create new action identities, resetting age while the
same exact RiskSubject remains unresolved.

### Keep scheduler state in memory

Rejected. Process restart and separate control loops would erase historical
service and make weighted fairness a per-call sort rather than a fleet property.

### Encode scheduler/SLO state in Receipt or Commit

Rejected. These are control-plane concerns and do not justify a backup wire
format migration.

## Consequences

- Autonomous claims become larger but forensically reproducible.
- Scheduler previews must explicitly opt out of recording service if a pure dry
  run is required; production scheduling records history by default.
- New SQLite tables require test path isolation and additive schema migration.
- Exact provider/process Gates can only be satisfied in the dedicated CI job;
  local unit tests do not promote those claims.

## Compatibility and non-goals

No object-set-v2, Receipt v5, Commit v5, FastCDC change, deterministic Age,
Projection change, authority protocol change, LLM decision, automatic primary
promotion, automatic deletion, replication weakening, Raft, or multi-primary
authority is introduced.
