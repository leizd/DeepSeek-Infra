# 4.6.0 Todo — Autonomous Recovery Placement & Scale-Safe Storage Control

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Gate A — Scale-Safe Correctness
- [x] Canonical physical ciphertext identity (aliases size=0 / is_physical=0)
- [x] physical_usage_summary counts unique digest once
- [x] GC uses SQL-native object_has_live_ref (no 20k set materialization)
- [x] list_recovery_object_refs_complete keyset scan for retirement apply
- [x] Capacity probe vs read projection split (probe=False for readiness)
- [x] DR readiness capacity uses probe=False / record_observation=False
- [x] Recovery chain exact parent walk; missing parent fail-closed
- [x] Tier planner requires exact storageTier + hot/warm ancestor eligibility
- [x] tests/test_backup_460_scale_safe_correctness.py

## Gate B — Index coverage + pure capacity projections
- [x] target_index_coverage table + set/get + gc_allowed
- [x] rebuild_index_from_target sets complete coverage generation
- [x] incomplete index blocks GC candidate listing / retirement GC
- [x] capacity_forecast_projections persist on probe; readiness reads projection

## Gate C — Recovery lineage graph
- [x] recovery_lineage table + upsert/get/clear
- [x] rebuild_recovery_lineage from DR ledger
- [x] chain builder prefers lineage graph

## Gate D — RecoveryChainMigrationJob
- [x] chain_migration_jobs durable table
- [x] plan_chain_migration with per-member authenticated sources
- [x] execute_chain_migration phases → converged / failed-terminal
- [x] intent never executed on failed rebalance
- [x] process_pending_chain_migrations + maintenance tick wiring
- [x] tests/test_backup_460_gates_bcd.py

## Gate E — Autonomous Recovery SLO Controller
- [x] recoveryPlacement policy normalization
- [x] backup_placement.py desired tier + evaluate/reconcile
- [x] correctness order: recoverability → lineage → copy/FD → RTO → capacity → cost
- [x] explainable reasonCodes + lifecycle placement-decision intents
- [x] drift enqueues plan_chain_migration when execute=True
- [x] maintenance tick runs reconcile_all_policies
- [x] tests/test_backup_460_gates_ef.py

## Gate F — Truly sharded maintenance by target
- [x] repair leases scoped by destTargetId
- [x] rebalance leases scoped by destTargetId
- [x] retirement leases scoped by targetId
- [x] chain-migration leases scoped by destTargetId
- [x] held dest scope skips only that target; free targets progress
- [x] tests updated for Gate F lease semantics

## Remaining
- [ ] Version surface 4.6.0
- [ ] Gate G Planner-mandatory MinIO Evidence
- [ ] Full suite green + coverage gate
