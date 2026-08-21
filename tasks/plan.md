# Implementation Plan: 4.5.8 Durable Storage Control Plane & Geo-Aware Lifecycle

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

Converge the 4.5.7 Repair, Replication, Rebalance, Drain, Retirement, Capacity,
and Transfer QoS primitives into one restart-safe, cross-process storage control
plane. `object-set-v1`, Receipt v4, Commit v4, FastCDC v3, Projection semantics,
and randomized Age encryption remain frozen.

## Dependency graph

```text
Frozen wire contracts
        |
Retirement marker + live-reference GC
        |
SQLite control authority + cross-process CAS
        |-------------------------------|
Logical-point placement + capacity      Multipart reconciliation
        |                               |
Geo/RTO/cost eligibility          Managed production transfers
        |                               |
        +------ Maintenance Supervisor -+
                       |
             Autonomous durable Drain
                       |
            Three-MinIO/real-Age Evidence
```

## Architecture decisions

- Formal Receipt and Commit bytes are immutable history; Copy Retirement adds
  Target-local authenticated lifecycle metadata and only GCs unreferenced
  payload ciphertext.
- `.backup-control/control.sqlite3` is the CAS authority for policy revisions,
  topology generations, maintenance cursors, and shared QoS buckets. Policy and
  Target JSON remain human-readable projections.
- Replica constraints are evaluated against one `logicalId`/backup replica set,
  never against a Policy's historical copy count.
- Unknown full-backup capacity is unavailable and fails closed for Force Full;
  a fixed small fallback is not evidence.
- Every maintenance tick is bounded. Durable keyset cursors and persisted jobs
  provide restart convergence without full-history scans.
- Correctness and recoverability precede diversity, capacity, RTO evidence, and
  operator-supplied cost estimates in placement ordering.

## Phase 1: Chain-preserving retirement (Gate A)

### Task 1: Preserve formal history and authenticate retirement

**Acceptance criteria:**
- Retirement leaves every Receipt v4 and Commit v4 byte intact.
- `retirements/<policyId>/<backupId>.json` binds Target, Policy, Backup,
  Receipt digest, Commit hash, object-set commitment, reason, and timestamp.
- Audit distinguishes a valid marker plus missing payload from ungoverned
  missing-object corruption.

**Verification:** focused retirement/audit tests, Ruff, Mypy.

### Task 2: Make payload GC live-reference safe

**Acceptance criteria:**
- Payload deletion occurs only after the marker is durable and the Ledger copy
  is retired.
- All live non-retired copies and active jobs/holds are included in reference
  analysis; shared ciphertext survives.

**Verification:** shared-object, active-job, interrupted-GC, and idempotency tests.

## Phase 2: Durable control authority (Gate B)

### Task 3: Add cross-process Policy and Topology CAS

**Acceptance criteria:**
- Concurrent writers with the same expected revision cannot both commit.
- Successful mutations advance revision/generation exactly once under
  `BEGIN IMMEDIATE`; JSON projection failure cannot roll back authority.
- Existing JSON state migrates without changing public Policy fields except the
  monotonic revision.

**Verification:** subprocess/multiprocessing race tests and projection recovery.

## Phase 3: Point-scoped placement and capacity (Gate C)

### Task 4: Scope topology to one Logical Recovery Point

**Acceptance criteria:**
- `minCommittedCopies`, `minFailureDomains`,
  `maxCopiesPerFailureDomain`, and `minRegions` use only the selected
  `logicalId`/backup replica set.
- Drain uses the same planner before scheduling a Rebalance.

**Verification:** large Policy history does not poison current placement;
failure-domain/region boundary tests.

### Task 5: Add confidence-aware capacity and geo/RTO/cost objectives

**Acceptance criteria:**
- Physical/ciphertext evidence is preferred over logical bytes.
- Unknown Force Full size is `capacityConfidence=unavailable` and cannot pass
  bounded-capacity admission.
- Region diversity and matching RecoveryClass P90 RTO are independent
  eligibility dimensions; cost remains an operator estimate and a final
  tie-break after correctness.

**Verification:** 600 GiB unknown-full regression, matching/nonmatching RTO
evidence, region and budget tests.

## Phase 4: Multipart and transfer control (Gates D-E)

### Task 6: Reconcile multipart state with provider ListParts

**Acceptance criteria:**
- Remote-equal resumes, verified remote-ahead advances the local checkpoint,
  missing upload restarts from zero, and ETag/size conflict aborts and
  quarantines deterministically.
- Completion uses the reconciled remote part list.

**Verification:** crash-before-checkpoint, provider abort, part mutation, and
pagination tests.

### Task 7: Enforce production ManagedTransfer QoS

**Acceptance criteria:**
- P0-P6 traffic wraps actual publish, restore, repair, replication, scrub/drill,
  rebalance, and drain data streams.
- Each chunk acquires shared global, source-read, and destination-write tokens.
- Independent Target buckets and cross-process DR reservation are observable in
  elapsed transfer behavior, not only a summary projection.

**Verification:** fake-clock unit tests, two-process budget tests, and a real
restore-vs-background throttling integration test.

## Phase 5: Supervisor and autonomous Drain (Gate F)

### Task 8: Add StorageMaintenanceSupervisor

**Acceptance criteria:**
- One bounded tick coordinates lease keeping, repair, replication, drain,
  rebalance, retirement, multipart reconciliation, capacity probes, and QoS
  lease cleanup.
- Durable worker cursors/fencing make ticks restart-safe and prevent duplicate
  ownership across processes.

**Verification:** crash/restart, lease expiry, bounded-work, and idempotency tests.

### Task 9: Complete Drain safely over arbitrary history

**Acceptance criteria:**
- Durable keyset pagination covers more than 500 retained points.
- Completion blocks on unscanned/live copies, writer leases, active Backup Runs,
  Recovery/Restore/Drill holds, Repair/Rebalance dependencies, and pending
  Retirements.
- Destination selection passes placement and capacity before transfer.

**Verification:** >500-copy cursor test plus every completion blocker.

## Phase 6: Evidence and release (Gates G-H)

### Task 10: Add real three-MinIO storage-control-plane runner

**Acceptance criteria:**
- The new runner uses boto3, `S3TargetStore`, production Policy/Scheduler/
  BackupExecutor/Supervisor, real Age, and three independent endpoints.
- Real process/server stop and restart proves failover, catch-up, drain,
  retirement marker, payload GC, and restore.
- Fake S3 and stub crypto cannot satisfy any real Evidence field; the legacy
  runner no longer owns `realDualMinioE2EIntegration`.

**Verification:** dedicated CI job and version-derived evidence artifact.

### Task 11: Release surfaces and full gates

**Acceptance criteria:**
- All product/version/docs surfaces agree on 4.5.8 and describe only behavior
  backed by tests or real Evidence.
- Python/frontend/lint/type/eval/security/release gates pass in repository order.

**Verification:** AGENTS.md command sequence and release preflight; CI-only
evidence remains pending unless its artifact is actually available.

## Checkpoints

- After Tasks 1-3: formal history and cross-process authority tests green.
- After Tasks 4-7: placement/capacity/multipart/QoS focused suites green.
- After Tasks 8-9: autonomous restart-safe Drain integration green.
- After Tasks 10-11: full local gates green; real Evidence status reported
  without fabricated digests or PASS values.

## Gate matrix

| Gate | Proof required | Fail-closed condition |
|---|---|---|
| A Retirement | immutable Commit/Receipt + authenticated marker | no marker or live reference |
| B CAS | same-revision process race | stale expected revision |
| C Placement | one logical replica set + region/FD objectives | missing scope or capacity evidence |
| D Multipart | provider ListParts reconciliation | missing/conflicting upload state |
| E QoS | measured production traffic throttling | unmanaged data stream |
| F Drain | supervisor + durable cursor + completion blockers | any active/unscanned dependency |
| G Evidence | three real MinIO + real Age | fake/stub/test-only path |
| H Compatibility | frozen byte-level protocol fixtures | wire-format drift |

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| SQLite authority and JSON projection diverge | stale operator view | authority-first commit plus repairable atomic projection |
| Marker is self-asserted rather than commitment-bound | forged governance deletion | digest/commit/target binding and fail-closed audit |
| QoS sleeps while holding locks | deadlock/poor concurrency | reserve atomically, sleep outside SQLite/thread locks |
| Cursor advances before work is durable | skipped Recovery Point | persist job before cursor advancement; convergence rescan |
| Real MinIO unavailable locally | false release confidence | keep local contracts distinct; mark CI Evidence pending |

## Review checkpoint

The user supplied and approved the 4.5.8 invariants, dependency direction, and
ten hard Gates on 2026-08-20. Any change to the frozen wire formats, plaintext
fallback behavior, or Evidence definition requires renewed approval.
