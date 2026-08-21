# Implementation Plan: 4.5.9 Indexed Lifecycle Economics & SLO-Aware Storage Tiering

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

Upgrade the 4.5.8 Storage Control Plane from “scan history and coordinate
multiple local job DBs” to a scale-ready control plane driven by a durable
Lifecycle Transaction Journal and a rebuildable Ciphertext Reference Index.
Wire protocols stay frozen: object-set-v1, Receipt v4, Commit v4, FastCDC v3,
Projection, randomized Age.

## Dependency graph

```text
Frozen wire contracts
        |
Lifecycle Intent Journal (control.sqlite3)
        |
Ciphertext Reference Index (rebuildable)
        |-------------------------------|
Physical accounting + growth TS         Fail-closed retirement/drain
        |                               |
Honest cost + capacity forecast         RecoveryChainPlacementUnit
        |                               |
        +------ Sharded Maintenance ----+
                       |
              Hot/Warm/Archive tiering
                       |
             Three-MinIO Evidence (tier + control recovery)
```

## Architecture decisions

- Lifecycle intents live in `control.sqlite3` under `BEGIN IMMEDIATE`. Drain /
  retirement / rebalance topology mutations commit an intent before or with the
  topology change; job DBs are projections that can be rebuilt.
- Receipt / Commit remain final truth. The object reference index is a
  rebuildable accelerator only.
- Physical quota and admission use distinct object bytes (`physicalStoredBytes`),
  not Σ logical recovery-point sizes.
- Capacity forecast uses elapsed-time observations; missing evidence reports
  `unavailable` rather than a fake large horizon.
- Cost rates require operator provenance; unknown rates never default to
  AWS-like prices.
- Tiering moves whole Recovery Chain Placement Units (full baseline + required
  ancestry), never a lone incremental leaf that depends on archive-only ancestors.
- Maintenance planner stays short-lived; execution leases shard by
  `(workerKind, targetId|policyId)`.
- Control DB uses `PRAGMA user_version`, quick_check fail-closed, and classifies
  tables as rebuildable vs non-rebuildable.

## Phase 1: Correctness hotfixes + journal (Gates A, I partial)

### Task 1: Versioned control schema + lifecycle intents
### Task 2: Drain start journals intent; reconcile missing DrainJob
### Task 3: Retirement dependency queries fail closed at page limit

## Phase 2: Reference index + physical accounting (Gates B, C)

### Task 4: target_objects / recovery_object_refs tables + index API
### Task 5: Index from formal receipts / retirement markers; GC uses index
### Task 6: S3 quota uses physical object bytes + pending-GC accounting

## Phase 3: Forecast + cost (Gates D, E)

### Task 7: Timestamped physical growth observations + EWMA forecast
### Task 8: Remove implicit cost defaults; expose costStatus provenance

## Phase 4: Tiering + sharding (Gates F, G, H)

### Task 9: Target storageTier metadata + RecoveryChainPlacementUnit
### Task 10: Tier migration preserves backupId/objectSetDigest (no re-encrypt)
### Task 11: Shard maintenance execution leases by worker/target

## Phase 5: Evidence + release

### Task 12: Unit/integration tests for all Evidence gates
### Task 13: Real three-MinIO tiering + control-recovery e2e hooks
### Task 14: Version surfaces, CHANGELOG, docs/releases/4.5.9.md

## Risks

| Risk | Mitigation |
|------|------------|
| Index drift vs S3 | Bounded inventory reconciliation; GC fail-closed on unknown |
| Tiering splits chains | Placement unit = full restore closure |
| Migration breaks old DB | CREATE IF NOT EXISTS + user_version migrations only |
| Cost API breaks callers | Keep fields; set null + costStatus=unavailable |

## Out of scope (4.6.0+)

Multi-site DR quorum, provider-native archive APIs, multi-source restore striping,
convergent encryption, object-level cloud lifecycle rules.
