# 4.5.9 Todo

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Phase 1 — Journal + P0 fixes
- [x] Control DB: user_version, schema_migrations, lifecycle_intents
- [x] start_target_drain journals intent before/with topology mutation
- [x] reconcile draining targets missing DrainJob from intents
- [x] has_active_copy_dependency fail-closed when list hits page limit

## Phase 2 — Reference index + physical bytes
- [x] target_objects + recovery_object_refs tables and API
- [x] Index live refs from formal receipts; apply retirement markers
- [x] GC candidates from index (shared ciphertext safe)
- [x] Rebuild index from Target formal truth
- [x] probe_target_capacity physicalStoredBytes / retiredPendingGcBytes

## Phase 3 — Forecast + cost
- [x] capacity_growth_observations time series
- [x] estimate_target_exhaustion_horizon elapsed-time + confidence
- [x] estimate_transfer_cost no implicit defaults; costStatus provenance

## Phase 4 — Tiering + sharding
- [x] Target storageTier / restoreLatencyClass metadata
- [x] RecoveryChainPlacementUnit builder
- [x] Tier plan + migrate (ciphertext only, digest stable)
- [x] Hot leaf cannot depend on archive-only ancestor
- [x] maintenance_tick sharded worker leases

## Phase 5 — Tests + release
- [x] tests/test_backup_459_*.py covering Evidence gates
- [ ] Update real MinIO e2e hooks for tiering/control recovery (CI producer follow-up)
- [x] VERSION 4.5.9 + all surfaces + CHANGELOG + release notes
- [x] ruff / mypy / focused pytest green (local unit contracts)
