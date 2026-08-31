from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_control_plane_runner_owns_458_459_and_460_real_minio_scenarios() -> None:
    runner = _load(ROOT / "scripts" / "run_storage_control_plane_minio_e2e.py", "storage_control_runner")
    node_458 = "tests/test_backup_458_real_storage_control_plane_e2e.py::test_real_three_minio_storage_control_plane_e2e"
    node_459 = "tests/test_backup_459_real_tiering_control_e2e.py::test_real_three_minio_tiering_and_control_recovery_e2e"
    node_460 = (
        "tests/test_backup_460_real_placement_control_e2e.py::test_real_three_minio_autonomous_placement_control_e2e"
    )
    node_462 = (
        "tests/test_backup_462_real_transactional_gc_e2e.py::test_real_three_minio_transactional_gc_fencing_e2e"
    )
    node_463 = (
        "tests/test_backup_463_real_control_authority_disaster_e2e.py::"
        "test_real_three_minio_control_authority_disaster_recovery_e2e"
    )
    node_465_a = (
        "tests/test_backup_465_fresh_process_authority.py::"
        "test_subprocess_fresh_process_detects_remote_authority"
    )
    node_465_b = (
        "tests/test_backup_465_fresh_process_authority.py::"
        "test_subprocess_crash_after_prepared_recovers_exactly_once"
    )
    node_466 = (
        "tests/test_backup_466_real_fresh_process_minio_e2e.py::"
        "test_real_three_minio_fresh_process_authority_recovery_e2e"
    )
    node_468 = (
        "tests/test_backup_468_real_backup_dr_sigkill_e2e.py::"
        "test_real_three_minio_sigkill_backup_disaster_recovery_e2e"
    )
    node_472 = (
        "tests/test_backup_472_real_three_minio_remediation_e2e.py::"
        "test_real_three_minio_autonomous_remediation_e2e"
    )
    nodes_durable_fleet = (
        "tests/test_backup_474_risk_fairness.py::test_risk_first_seen_persists_and_debt_ages_across_planner_runs",
        "tests/test_backup_474_risk_fairness.py::test_cleared_risk_stops_debt_and_reopen_uses_new_open_interval",
        "tests/test_backup_474_risk_fairness.py::test_production_scheduler_uses_and_updates_persistent_fairness",
        "tests/test_backup_474_scheduler_correctness.py::test_all_schedulable_actions_are_partitioned_into_dependency_waves",
        "tests/test_backup_474_scheduler_correctness.py::test_missing_dependency_is_typed_unschedulable",
        "tests/test_backup_474_scheduler_correctness.py::test_rebalance_is_deferred_to_next_wave_by_real_transfer_reserve",
        "tests/test_backup_474_scheduler_correctness.py::test_runtime_rebalance_cannot_consume_active_repair_reserved_tokens",
        "tests/test_backup_474_scheduler_correctness.py::test_safe_preemption_releases_victim_and_claims_repair_atomically",
        "tests/test_backup_474_scheduler_correctness.py::test_unsafe_preemption_cannot_modify_executing_victim",
        "tests/test_backup_474_slo_readiness.py::test_fleet_slo_samples_persist_and_compute_percentiles",
        "tests/test_backup_474_slo_readiness.py::test_fast_and_slow_error_budget_burn_rates_are_computed",
        "tests/test_backup_474_slo_readiness.py::test_risk_clear_and_action_claim_takeover_are_measured",
        "tests/test_backup_474_slo_readiness.py::test_maintenance_windows_block_background_work_and_allow_critical_overrides",
        "tests/test_backup_474_slo_readiness.py::test_terminal_repair_records_duration_and_remediation_outcome",
        "tests/test_backup_474_slo_readiness.py::test_fleet_readiness_api_is_authenticated_and_source_backed",
        "tests/test_backup_474_slo_readiness.py::test_risk_control_loop_persists_dr_freshness_without_operator_read",
    )
    nodes_wire_freeze = (
        "tests/test_backup_452_replica_failover.py::test_wire_format_constants_unchanged",
        "tests/test_backup_4410_contracts.py::test_cdc_v3_is_explicit_and_normalized",
        "tests/test_backup_463_control_authority.py::test_control_authority_schema_constant_and_keys",
        "tests/test_backup_463_control_authority.py::test_authority_checkpoint_is_hash_chained_and_secretless",
    )
    nodes_predictive_fleet = (
        "tests/test_backup_475_risk_lifecycle.py::test_superseded_backup_risk_cannot_remain_open",
        "tests/test_backup_475_risk_lifecycle.py::test_unknown_coverage_does_not_implicitly_clear",
        "tests/test_backup_475_fair_service.py::test_schedule_result_reserves_without_consuming",
        "tests/test_backup_475_fair_service.py::test_completed_action_charges_observed_bytes_exactly_once",
        "tests/test_backup_475_fair_service.py::test_preempted_action_releases_reservation",
        "tests/test_backup_475_wave_executor.py::test_wave_one_cannot_start_before_wave_zero_verified",
        "tests/test_backup_475_wave_executor.py::test_failed_wave_pauses_downstream_and_stale_requires_replan",
        "tests/test_backup_475_slo_windows.py::test_fleet_slo_exposes_named_windows_and_insufficient_data",
        "tests/test_backup_475_forecast.py::test_forecast_uses_durable_observations_and_insufficient_fails_closed",
        "tests/test_backup_475_forecast.py::test_forecast_backtest_persists_error_and_lowers_confidence",
        "tests/test_backup_475_optimizer.py::test_cost_model_includes_storage_egress_and_unknown_is_not_zero",
        "tests/test_backup_475_optimizer.py::test_optimizer_rejects_unsafe_cheaper_plan_and_is_deterministic",
        "tests/test_backup_475_whatif.py::test_what_if_is_zero_mutation_and_binds_snapshot",
        "tests/test_backup_475_federation.py::test_federation_snapshot_is_digest_bound_and_credential_free",
        "tests/test_backup_475_federation.py::test_incompatible_wire_and_credentials_fail_closed",
    )
    node_476_predictive = (
        "tests/test_backup_476_real_three_minio_predictive_e2e.py::test_real_three_minio_predictive_planning_e2e"
    )
    assert runner.SCENARIOS == {
        "real-three-minio-storage-control-plane": (node_458,),
        "real-three-minio-tiering-control-recovery": (node_459,),
        "real-three-minio-autonomous-placement-control": (node_460,),
        "real-three-minio-transactional-gc-fencing": (node_462,),
        "real-three-minio-control-authority-disaster-recovery": (node_463,),
        "fresh-process-filesystem-authority-recovery": (node_465_a, node_465_b),
        "real-three-minio-fresh-process-authority-recovery": (node_466,),
        "real-three-minio-process-replacement-authority-recovery": (node_468,),
        "real-three-minio-autonomous-remediation": (node_472,),
        "durable-fleet-scheduler-slo-correctness": nodes_durable_fleet,
        "predictive-fleet-planning-verified-optimization": nodes_predictive_fleet,
        "real-three-minio-predictive-planning": (node_476_predictive,),
        "storage-wire-freeze-contracts": nodes_wire_freeze,
    }
    assert set(runner.REQUIRED_ENDPOINTS) == {
        "DEEPSEEK_TEST_S3_ENDPOINT_A",
        "DEEPSEEK_TEST_S3_ENDPOINT_B",
        "DEEPSEEK_TEST_S3_ENDPOINT_C",
    }
    assert runner.CHECK_SCENARIOS["realThreeMinioEndpoints"] == "real-three-minio-storage-control-plane"
    assert runner.CHECK_SCENARIOS["realAgeRandomizedEncryption"] == "real-three-minio-storage-control-plane"
    assert runner.CHECK_SCENARIOS["autonomousDrainRetirementGcRestore"] == "real-three-minio-storage-control-plane"
    assert runner.CHECK_SCENARIOS["realThreeMinioTierMigrationE2E"] == "real-three-minio-tiering-control-recovery"
    assert runner.CHECK_SCENARIOS["realControlDbCrashRecoveryE2E"] == "real-three-minio-tiering-control-recovery"
    assert runner.CHECK_SCENARIOS["tierMigrationPreservesBackupIdAndObjectSetDigest"] == (
        "real-three-minio-tiering-control-recovery"
    )
    assert runner.CHECK_SCENARIOS["realThreeMinioAutonomousPlacementE2E"] == (
        "real-three-minio-autonomous-placement-control"
    )
    assert runner.CHECK_SCENARIOS["incompleteLineageBlocksChainMigration"] == (
        "real-three-minio-autonomous-placement-control"
    )
    assert runner.CHECK_SCENARIOS["targetShardedRepairScopesProgressIndependently"] == (
        "real-three-minio-autonomous-placement-control"
    )
    assert runner.CHECK_SCENARIOS["realThreeMinioTransactionalGcFencingE2E"] == (
        "real-three-minio-transactional-gc-fencing"
    )
    assert runner.CHECK_SCENARIOS["expiredMetadataFenceNeverDeletesAfterTakeover"] == (
        "real-three-minio-transactional-gc-fencing"
    )
    assert runner.CHECK_SCENARIOS["realMinioPublishGcLeaseExpiryRaceNeverLosesCiphertext"] == (
        "real-three-minio-transactional-gc-fencing"
    )
    assert runner.CHECK_SCENARIOS["realThreeMinioControlAuthorityDisasterRecoveryE2E"] == (
        "real-three-minio-control-authority-disaster-recovery"
    )
    assert runner.CHECK_SCENARIOS["policyRevisionSurvivesTotalControlDbLoss"] == (
        "real-three-minio-control-authority-disaster-recovery"
    )
    assert runner.CHECK_SCENARIOS["freshNodeReconstructsControlAuthority"] == (
        "real-three-minio-control-authority-disaster-recovery"
    )
    assert runner.CHECK_SCENARIOS["freshProcessFilesystemAuthorityRecovery"] == (
        "fresh-process-filesystem-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realProcessCrashAfterPreparedMutationRecoversExactlyOnce"] == (
        "fresh-process-filesystem-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realThreeMinioFreshProcessAuthorityRecoveryE2E"] == (
        "real-three-minio-fresh-process-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realFreshProcessCreatesPostRecoveryBackup"] == (
        "real-three-minio-process-replacement-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realPreDisasterBackupIsActuallyRestored"] == (
        "real-three-minio-process-replacement-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["processAExitedBySigkill"] == (
        "real-three-minio-process-replacement-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realPostRecoveryBackupHasValidReceiptBinding"] == (
        "real-three-minio-process-replacement-authority-recovery"
    )
    required_remediation = runner.REQUIRED_PROOF_CHECKS["real-three-minio-autonomous-remediation"]
    assert "realThreeMinioAutonomousRepairE2E" in required_remediation
    assert "realReplicaTransferUsesEndpointAAndB" in required_remediation
    assert "destinationReceiptAuthenticated" in required_remediation
    required_predictive = runner.REQUIRED_PROOF_CHECKS["real-three-minio-predictive-planning"]
    assert "realThreeMinioPredictivePlanningE2E" in required_predictive
    assert "predictiveProofRejectsSelfReportedZeroMutation" in required_predictive
    assert "whatIfProducesNoStorageWrites" in required_predictive
    assert runner.CHECK_SCENARIOS["whatIfProducesNoStorageWrites"] == runner.REAL_PREDICTIVE_SCENARIO


def test_real_evidence_sources_forbid_fake_s3_stub_crypto_and_resolver_monkeypatch() -> None:
    for rel in (
        "tests/test_backup_458_real_storage_control_plane_e2e.py",
        "tests/test_backup_459_real_tiering_control_e2e.py",
        "tests/test_backup_460_real_placement_control_e2e.py",
        "tests/test_backup_462_real_transactional_gc_e2e.py",
        "tests/test_backup_463_real_control_authority_disaster_e2e.py",
        "tests/test_backup_472_real_three_minio_remediation_e2e.py",
        "tests/test_backup_476_real_three_minio_predictive_e2e.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "ProductionFakeS3Client" not in source
        assert "stub_crypto" not in source
        assert "monkeypatch.setattr(backup_publish" not in source
        assert "monkeypatch.setattr(backup_executor" not in source
        assert "register_filesystem_target" not in source



def test_legacy_runner_no_longer_claims_real_minio_evidence() -> None:
    legacy = _load(ROOT / "scripts" / "run_replica_healing_s3_e2e.py", "legacy_replica_runner")
    assert "realDualMinioE2EIntegration" not in legacy.CHECK_SCENARIOS
    assert "dual-minio-s3-e2e" not in legacy.SCENARIOS


def test_ci_runs_three_independent_minio_servers_and_requires_new_producer() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "  storage-control-plane-minio-e2e:\n" in workflow
    assert "--publish 9000:9000" in workflow
    assert "--publish 9001:9001" in workflow
    assert "--publish 9002:9002" in workflow
    assert "python scripts/run_storage_control_plane_minio_e2e.py" in workflow
    assert "--producer storage-control-plane-minio-e2e" in workflow
    assert "storage-control-plane-predictive-proof-v${{ env.RELEASE_VERSION }}.json" in workflow
    assert workflow.count("      - storage-control-plane-minio-e2e\n") == 2
    assert "RC_CI_STORAGE_CONTROL_PLANE_MINIO_E2E" in workflow
