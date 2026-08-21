# 4.6.0 Todo — Autonomous Recovery Placement & Scale-Safe Storage Control

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Gate A — Scale-Safe Correctness (in progress)
- [x] Canonical physical ciphertext identity (aliases size=0 / is_physical=0)
- [x] physical_usage_summary counts unique digest once
- [x] GC uses SQL-native object_has_live_ref (no 20k set materialization)
- [x] list_recovery_object_refs_complete keyset scan for retirement apply
- [x] Capacity probe vs read projection split (probe=False for readiness)
- [x] DR readiness capacity uses probe=False / record_observation=False
- [x] Recovery chain exact parent walk; missing parent fail-closed
- [x] Tier planner requires exact storageTier + hot/warm ancestor eligibility
- [x] tests/test_backup_460_scale_safe_correctness.py
- [ ] Version surface 4.6.0
- [ ] Full suite green + coverage gate

## Gate B–G (pending)
- [ ] Index coverage generation / incomplete index blocks GC
- [ ] RecoveryChainMigrationJob durable SM
- [ ] Autonomous SLO controller
- [ ] Truly sharded maintenance (repair/rebalance/retirement per target)
- [ ] Real MinIO planner-mandatory Evidence (no manual fallback)
