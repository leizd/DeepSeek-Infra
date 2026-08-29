#!/usr/bin/env python3
"""Run the real three-MinIO, real-Age Storage Control Plane Evidence gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402
from deepseek_infra.infra.workspace import evidence_proof  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_manifest import sha256_of  # noqa: E402
from scripts.release_evidence import stamp_release_report  # noqa: E402

REAL_SCENARIO = "real-three-minio-storage-control-plane"
TIER_SCENARIO = "real-three-minio-tiering-control-recovery"
PLACE_SCENARIO = "real-three-minio-autonomous-placement-control"
TXGC_SCENARIO = "real-three-minio-transactional-gc-fencing"
AUTH_DR_SCENARIO = "real-three-minio-control-authority-disaster-recovery"
FS_FRESH_AUTH_SCENARIO = "fresh-process-filesystem-authority-recovery"
FRESH_AUTH_SCENARIO = "real-three-minio-fresh-process-authority-recovery"
PROCESS_REPLACE_SCENARIO = "real-three-minio-process-replacement-authority-recovery"
AUTONOMOUS_REMEDIATION_SCENARIO = "real-three-minio-autonomous-remediation"
DURABLE_FLEET_SCENARIO = "durable-fleet-scheduler-slo-correctness"
WIRE_FREEZE_SCENARIO = "storage-wire-freeze-contracts"
AUTONOMOUS_PROOF_TEMPLATE = "docs/evidence/storage-control-plane-autonomous-proof-v{version}.json"
SCENARIOS: dict[str, tuple[str, ...]] = {
    REAL_SCENARIO: (
        "tests/test_backup_458_real_storage_control_plane_e2e.py::test_real_three_minio_storage_control_plane_e2e",
    ),
    TIER_SCENARIO: (
        "tests/test_backup_459_real_tiering_control_e2e.py::test_real_three_minio_tiering_and_control_recovery_e2e",
    ),
    PLACE_SCENARIO: (
        "tests/test_backup_460_real_placement_control_e2e.py::test_real_three_minio_autonomous_placement_control_e2e",
    ),
    TXGC_SCENARIO: (
        "tests/test_backup_462_real_transactional_gc_e2e.py::test_real_three_minio_transactional_gc_fencing_e2e",
    ),
    AUTH_DR_SCENARIO: (
        "tests/test_backup_463_real_control_authority_disaster_e2e.py::test_real_three_minio_control_authority_disaster_recovery_e2e",
    ),
    FS_FRESH_AUTH_SCENARIO: (
        "tests/test_backup_465_fresh_process_authority.py::test_subprocess_fresh_process_detects_remote_authority",
        "tests/test_backup_465_fresh_process_authority.py::test_subprocess_crash_after_prepared_recovers_exactly_once",
    ),
    FRESH_AUTH_SCENARIO: (
        "tests/test_backup_466_real_fresh_process_minio_e2e.py::test_real_three_minio_fresh_process_authority_recovery_e2e",
    ),
    PROCESS_REPLACE_SCENARIO: (
        "tests/test_backup_468_real_backup_dr_sigkill_e2e.py::test_real_three_minio_sigkill_backup_disaster_recovery_e2e",
    ),
    AUTONOMOUS_REMEDIATION_SCENARIO: (
        "tests/test_backup_472_real_three_minio_remediation_e2e.py::test_real_three_minio_autonomous_remediation_e2e",
    ),
    DURABLE_FLEET_SCENARIO: (
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
    ),
    WIRE_FREEZE_SCENARIO: (
        "tests/test_backup_452_replica_failover.py::test_wire_format_constants_unchanged",
        "tests/test_backup_4410_contracts.py::test_cdc_v3_is_explicit_and_normalized",
        "tests/test_backup_463_control_authority.py::test_control_authority_schema_constant_and_keys",
        "tests/test_backup_463_control_authority.py::test_authority_checkpoint_is_hash_chained_and_secretless",
    ),
}
CHECK_SCENARIOS = {
    "realThreeMinioEndpoints": REAL_SCENARIO,
    "productionSchedulerBackupExecutorAndMaintenanceSupervisor": REAL_SCENARIO,
    "realAgeRandomizedEncryption": REAL_SCENARIO,
    "realServerStopFailoverAndRestartCatchup": REAL_SCENARIO,
    "providerBackedRepairReconciliation": REAL_SCENARIO,
    "autonomousDrainRetirementGcRestore": REAL_SCENARIO,
    "formalReceiptCommitHistoryPreserved": REAL_SCENARIO,
    "fakeS3AndStubCryptoForbidden": REAL_SCENARIO,
    # 4.5.9 gates
    "realThreeMinioTierMigrationE2E": TIER_SCENARIO,
    "realControlDbCrashRecoveryE2E": TIER_SCENARIO,
    "tierMigrationPreservesBackupIdAndObjectSetDigest": TIER_SCENARIO,
    "tierMigrationDoesNotInvokeAgeEncryption": TIER_SCENARIO,
    "referenceIndexRebuildsFromFormalTargetTruth": TIER_SCENARIO,
    "maintenanceScopesProgressIndependently": TIER_SCENARIO,
    "objectSetV1WireFormatUnchanged": TIER_SCENARIO,
    # 4.6.0 gates (Gate G planner-mandatory Evidence)
    "realThreeMinioAutonomousPlacementE2E": PLACE_SCENARIO,
    "incompleteLineageBlocksChainMigration": PLACE_SCENARIO,
    "indexCoverageBlocksGcUntilComplete": PLACE_SCENARIO,
    "placementControllerCorrectnessOrder": PLACE_SCENARIO,
    "targetShardedRepairScopesProgressIndependently": PLACE_SCENARIO,
    "capacityReadinessZeroRemoteIo": PLACE_SCENARIO,
    "randomizedAgeAndObjectSetWireFreeze": PLACE_SCENARIO,
    # 4.6.2 gates (transactional GC fencing effect on real MinIO)
    "realThreeMinioTransactionalGcFencingE2E": TXGC_SCENARIO,
    "expiredMetadataFenceNeverDeletesAfterTakeover": TXGC_SCENARIO,
    "realMinioPublishGcLeaseExpiryRaceNeverLosesCiphertext": TXGC_SCENARIO,
    # 4.6.3 gates (control authority disaster recovery on real MinIO)
    "realThreeMinioControlAuthorityDisasterRecoveryE2E": AUTH_DR_SCENARIO,
    "policyRevisionSurvivesTotalControlDbLoss": AUTH_DR_SCENARIO,
    "targetTopologySurvivesTotalControlDbLoss": AUTH_DR_SCENARIO,
    "freshNodeReconstructsControlAuthority": AUTH_DR_SCENARIO,
    "authenticatedCommittedReceiptsAreRecoveryTruth": AUTH_DR_SCENARIO,
    "expiredLeasesAreNeverResurrected": AUTH_DR_SCENARIO,
    "recoveryAdvancesControlBootEpoch": AUTH_DR_SCENARIO,
    "controlAuthorityCheckpointContainsNoSecrets": AUTH_DR_SCENARIO,
    "controlRecoveryPerformsZeroAgeEncryption": AUTH_DR_SCENARIO,
    # 4.6.5 filesystem fresh-process (not MinIO)
    "freshProcessFilesystemAuthorityRecovery": FS_FRESH_AUTH_SCENARIO,
    "realProcessCrashAfterPreparedMutationRecoversExactlyOnce": FS_FRESH_AUTH_SCENARIO,
    # 4.6.6 genuine three-MinIO fresh-process DR
    "realThreeMinioFreshProcessAuthorityRecoveryE2E": FRESH_AUTH_SCENARIO,
    "realFreshProcessUsesProductionS3Bootstrap": FRESH_AUTH_SCENARIO,
    "realFreshProcessHasZeroInheritedS3Handles": FRESH_AUTH_SCENARIO,
    "realFreshProcessIsReadOnlyBeforeFormalTruth": FRESH_AUTH_SCENARIO,
    "realFreshProcessRestoresPreDisasterBackup": PROCESS_REPLACE_SCENARIO,
    "realFreshProcessCreatesPostRecoveryBackup": PROCESS_REPLACE_SCENARIO,
    "realFreshProcessBootEpochStrictlyIncreases": PROCESS_REPLACE_SCENARIO,
    # 4.6.8 process-replacement + evidence-proof-v2 claims
    "realThreeMinioProcessReplacementE2E": PROCESS_REPLACE_SCENARIO,
    "freshProcessAAndBHaveDifferentPids": PROCESS_REPLACE_SCENARIO,
    "processAIsDeadBeforeProcessBStarts": PROCESS_REPLACE_SCENARIO,
    "freshProcessBUsesProductionAuthorityStoreFactory": PROCESS_REPLACE_SCENARIO,
    "realPreDisasterBackupIsActuallyRestored": PROCESS_REPLACE_SCENARIO,
    "restoredWorkspaceDigestMatchesPreDisasterDigest": PROCESS_REPLACE_SCENARIO,
    "realPostRecoveryBackupHasValidCommit": PROCESS_REPLACE_SCENARIO,
    "realPostRecoveryBackupHasValidReceiptBinding": PROCESS_REPLACE_SCENARIO,
    "processAExitedBySigkill": PROCESS_REPLACE_SCENARIO,
    "evidenceCheckCannotPassWithoutStructuredProof": PROCESS_REPLACE_SCENARIO,
    # 4.7.2 / 4.7.3 gates
    "realThreeMinioAutonomousRemediationE2E": AUTONOMOUS_REMEDIATION_SCENARIO,
    "coordinatedAutonomousRemediationGraph": AUTONOMOUS_REMEDIATION_SCENARIO,
    "verifiedScopedRiskReduction": AUTONOMOUS_REMEDIATION_SCENARIO,
    "crashRecoverableExactlyOnceExecution": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realThreeMinioAutonomousRepairE2E": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realThreeMinioAutonomousRebalanceE2E": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realThreeMinioAutonomousDrillE2E": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realReplicaTransferUsesEndpointAAndB": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realRebalanceUsesEndpointAAndC": AUTONOMOUS_REMEDIATION_SCENARIO,
    "destinationReceiptAuthenticated": AUTONOMOUS_REMEDIATION_SCENARIO,
    "destinationCommitAuthenticated": AUTONOMOUS_REMEDIATION_SCENARIO,
    "autonomousProofUsesActualReceiptBytes": AUTONOMOUS_REMEDIATION_SCENARIO,
    "autonomousProofUsesActualCommitBytes": AUTONOMOUS_REMEDIATION_SCENARIO,
    "receiptSha256MatchesCommitReceiptDigest": AUTONOMOUS_REMEDIATION_SCENARIO,
    "proofObjectSetDigestMatchesCommit": AUTONOMOUS_REMEDIATION_SCENARIO,
    "proofObjectKeysExistOnExpectedMinioEndpoint": AUTONOMOUS_REMEDIATION_SCENARIO,
    "receiptV4Unchanged": AUTONOMOUS_REMEDIATION_SCENARIO,
    "commitV4Unchanged": AUTONOMOUS_REMEDIATION_SCENARIO,
    "crashRecoveryObservedExistingEffect": AUTONOMOUS_REMEDIATION_SCENARIO,
    "leaseTakeoverUsedNewExecutionEpoch": AUTONOMOUS_REMEDIATION_SCENARIO,
    "realWorkerCrashOccursDuringRemoteRepair": AUTONOMOUS_REMEDIATION_SCENARIO,
    "freshWorkerTakesOverExpiredAction": AUTONOMOUS_REMEDIATION_SCENARIO,
    "takeoverExecutionEpochStrictlyIncreases": AUTONOMOUS_REMEDIATION_SCENARIO,
    "takeoverEntersReconcilingBeforeMutation": AUTONOMOUS_REMEDIATION_SCENARIO,
    "takeoverFindsExistingRemoteEffect": AUTONOMOUS_REMEDIATION_SCENARIO,
    "takeoverDoesNotCreateSecondRepairJob": AUTONOMOUS_REMEDIATION_SCENARIO,
    "blastRadiusInvariantVerified": AUTONOMOUS_REMEDIATION_SCENARIO,
    "degradedFleetCannotBeFurtherDegraded": AUTONOMOUS_REMEDIATION_SCENARIO,
    "runningEffectsParticipateInBlastRadiusSimulation": AUTONOMOUS_REMEDIATION_SCENARIO,
    "atomicBudgetAdmissionVerified": AUTONOMOUS_REMEDIATION_SCENARIO,
    "twoProcessesCannotOversubscribeGlobalBudget": AUTONOMOUS_REMEDIATION_SCENARIO,
    "twoProcessesCannotOversubscribeTargetBudget": AUTONOMOUS_REMEDIATION_SCENARIO,
    "twoProcessesCannotOversubscribePolicyBudget": AUTONOMOUS_REMEDIATION_SCENARIO,
    "twoProcessesCannotOversubscribeFailureDomainBudget": AUTONOMOUS_REMEDIATION_SCENARIO,
    # 4.7.4 durable fleet correctness gates
    "riskFirstSeenPersistsAcrossControlLoops": DURABLE_FLEET_SCENARIO,
    "riskDebtAgeIncreasesAcrossPlannerRuns": DURABLE_FLEET_SCENARIO,
    "clearedRiskStopsAccumulatingDebt": DURABLE_FLEET_SCENARIO,
    "reopenedRiskUsesPersistentLifecycle": DURABLE_FLEET_SCENARIO,
    "productionSchedulerUsesPersistentFairnessHistory": DURABLE_FLEET_SCENARIO,
    "weightedFairSchedulingPreventsPolicyStarvation": DURABLE_FLEET_SCENARIO,
    "allSchedulableActionsReceiveExecutionWave": DURABLE_FLEET_SCENARIO,
    "dependenciesArePreservedAcrossWaves": DURABLE_FLEET_SCENARIO,
    "conflictingActionsAreSeparatedAcrossWaves": DURABLE_FLEET_SCENARIO,
    "unschedulableActionHasTypedReason": DURABLE_FLEET_SCENARIO,
    "rebalanceCannotConsumeRepairReservedBandwidth": DURABLE_FLEET_SCENARIO,
    "repairReserveUsesRealTransferBudget": DURABLE_FLEET_SCENARIO,
    "safePreemptionReleasesBudgetAtomically": DURABLE_FLEET_SCENARIO,
    "unsafePreemptionCannotModifyVictim": DURABLE_FLEET_SCENARIO,
    "fleetSloSamplesPersistAcrossRestart": DURABLE_FLEET_SCENARIO,
    "riskClearLatencyIsMeasured": DURABLE_FLEET_SCENARIO,
    "remediationQueueDelayIsMeasured": DURABLE_FLEET_SCENARIO,
    "leaseTakeoverLatencyIsMeasured": DURABLE_FLEET_SCENARIO,
    "repairTimeIsMeasured": DURABLE_FLEET_SCENARIO,
    "fastAndSlowBurnRatesAreComputed": DURABLE_FLEET_SCENARIO,
    "criticalRepairOverridesMaintenanceWindow": DURABLE_FLEET_SCENARIO,
    "rebalanceRespectsMaintenanceWindow": DURABLE_FLEET_SCENARIO,
    "criticalDrStalenessMayOverrideWindow": DURABLE_FLEET_SCENARIO,
    "fleetReadinessApiIsSourceBacked": DURABLE_FLEET_SCENARIO,
    # Frozen protocols remain explicit gates.
    "fastCdcV3Unchanged": WIRE_FREEZE_SCENARIO,
    "controlAuthorityV1Unchanged": WIRE_FREEZE_SCENARIO,
    "authorityCheckpointV1Unchanged": WIRE_FREEZE_SCENARIO,
    "randomizedAgeUnchanged": REAL_SCENARIO,
}

# Claims that MUST be backed by evidence-proof-v2 (semantic validators; not pytest exit alone).
REQUIRED_PROOF_CHECKS: dict[str, tuple[str, ...]] = {
    PROCESS_REPLACE_SCENARIO: (
        "realPreDisasterBackupIsActuallyRestored",
        "realFreshProcessRestoresPreDisasterBackup",
        "restoredWorkspaceDigestMatchesPreDisasterDigest",
        "realPostRecoveryBackupHasValidCommit",
        "realFreshProcessCreatesPostRecoveryBackup",
        "realPostRecoveryBackupHasValidReceiptBinding",
        "freshProcessAAndBHaveDifferentPids",
        "processAIsDeadBeforeProcessBStarts",
        "processAExitedBySigkill",
        "realFreshProcessBootEpochStrictlyIncreases",
        "realThreeMinioProcessReplacementE2E",
        "freshProcessBUsesProductionAuthorityStoreFactory",
        "evidenceCheckCannotPassWithoutStructuredProof",
    ),
    AUTONOMOUS_REMEDIATION_SCENARIO: (
        "realThreeMinioAutonomousRepairE2E",
        "realThreeMinioAutonomousRebalanceE2E",
        "realThreeMinioAutonomousDrillE2E",
        "realReplicaTransferUsesEndpointAAndB",
        "realRebalanceUsesEndpointAAndC",
        "destinationReceiptAuthenticated",
        "destinationCommitAuthenticated",
        "autonomousProofUsesActualReceiptBytes",
        "autonomousProofUsesActualCommitBytes",
        "receiptSha256MatchesCommitReceiptDigest",
        "proofObjectSetDigestMatchesCommit",
        "proofObjectKeysExistOnExpectedMinioEndpoint",
        "receiptV4Unchanged",
        "commitV4Unchanged",
        "crashRecoveryObservedExistingEffect",
        "leaseTakeoverUsedNewExecutionEpoch",
        "realWorkerCrashOccursDuringRemoteRepair",
        "freshWorkerTakesOverExpiredAction",
        "takeoverExecutionEpochStrictlyIncreases",
        "takeoverEntersReconcilingBeforeMutation",
        "takeoverFindsExistingRemoteEffect",
        "takeoverDoesNotCreateSecondRepairJob",
        "blastRadiusInvariantVerified",
        "degradedFleetCannotBeFurtherDegraded",
        "runningEffectsParticipateInBlastRadiusSimulation",
        "atomicBudgetAdmissionVerified",
        "twoProcessesCannotOversubscribeGlobalBudget",
        "twoProcessesCannotOversubscribeTargetBudget",
        "twoProcessesCannotOversubscribePolicyBudget",
        "twoProcessesCannotOversubscribeFailureDomainBudget",
    ),
}
REQUIRED_ENDPOINTS = (
    "DEEPSEEK_TEST_S3_ENDPOINT_A",
    "DEEPSEEK_TEST_S3_ENDPOINT_B",
    "DEEPSEEK_TEST_S3_ENDPOINT_C",
)
REQUIRED_CONTAINERS = (
    "DEEPSEEK_TEST_MINIO_CONTAINER_A",
    "DEEPSEEK_TEST_MINIO_CONTAINER_B",
    "DEEPSEEK_TEST_MINIO_CONTAINER_C",
)


def _prerequisite_errors() -> list[str]:
    errors: list[str] = []
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in REQUIRED_ENDPOINTS]
    missing_endpoints = [name for name, value in zip(REQUIRED_ENDPOINTS, endpoints, strict=True) if not value]
    if missing_endpoints:
        errors.append(f"missing endpoints: {', '.join(missing_endpoints)}")
    elif len(set(endpoints)) != 3:
        errors.append("three distinct S3 endpoints are required")
    missing_containers = [name for name in REQUIRED_CONTAINERS if not os.environ.get(name)]
    if missing_containers:
        errors.append(f"missing container identities: {', '.join(missing_containers)}")
    if importlib.util.find_spec("boto3") is None:
        errors.append("boto3 is unavailable")
    if backup_crypto.helper_path() is None:
        errors.append("real Age backup-crypto helper is unavailable")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    autonomous_proof_rel = AUTONOMOUS_PROOF_TEMPLATE.format(version=version)
    autonomous_proof_output = ROOT / autonomous_proof_rel
    if autonomous_proof_output.is_file():
        autonomous_proof_output.unlink()
    prerequisite_errors = _prerequisite_errors()
    results: dict[str, dict[str, object]] = {}
    if prerequisite_errors:
        for scenario, node_ids in SCENARIOS.items():
            results[scenario] = {
                "nodeIds": list(node_ids),
                "exitCode": 2,
                "error": "; ".join(prerequisite_errors),
            }
    else:
        environment = os.environ.copy()
        environment["DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E"] = "1"
        proof_dir = ROOT / "artifacts" / "evidence-proofs"
        proof_dir.mkdir(parents=True, exist_ok=True)
        for scenario, node_ids in SCENARIOS.items():
            proof_path = proof_dir / f"evidence-proof-{scenario}.json"
            if proof_path.is_file():
                proof_path.unlink()
            env = environment.copy()
            env[evidence_proof.ENV_EVIDENCE_PROOF_PATH] = str(proof_path)
            command = [sys.executable, "-m", "pytest", "--no-cov", "-q", *node_ids]
            completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
            results[scenario] = {
                "nodeIds": list(node_ids),
                "exitCode": completed.returncode,
                "proofPath": str(proof_path) if proof_path.is_file() else None,
            }

    checks = {
        check: "PASS" if results[scenario]["exitCode"] == 0 else "FAIL"
        for check, scenario in CHECK_SCENARIOS.items()
    }
    # 4.6.8: proof-backed claims cannot PASS from pytest exit alone.
    checks = evidence_proof.merge_checks_from_proof(
        checks=checks,
        check_to_scenario=dict(CHECK_SCENARIOS),
        scenario_results=results,
        required_proof_checks=REQUIRED_PROOF_CHECKS,
    )
    proof_artifact: dict[str, object] | None = None
    autonomous_result = results.get(AUTONOMOUS_REMEDIATION_SCENARIO) or {}
    proof_path_raw = autonomous_result.get("proofPath")
    proof_source = Path(str(proof_path_raw)) if proof_path_raw else None
    required_autonomous_checks = REQUIRED_PROOF_CHECKS[AUTONOMOUS_REMEDIATION_SCENARIO]
    if proof_source is not None and proof_source.is_file() and all(checks.get(name) == "PASS" for name in required_autonomous_checks):
        autonomous_proof_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(proof_source, autonomous_proof_output)
        proof_artifact = {
            "path": autonomous_proof_rel,
            "sha256": sha256_of(autonomous_proof_output),
            "bytes": autonomous_proof_output.stat().st_size,
            "scenario": AUTONOMOUS_REMEDIATION_SCENARIO,
        }
    checks["autonomousProofArtifactIsUploaded"] = "PASS" if proof_artifact is not None else "FAIL"
    check_provenance = dict(CHECK_SCENARIOS)
    check_provenance["autonomousProofArtifactIsUploaded"] = "exact-proof-artifact-copy"
    report = stamp_release_report(
        {
            "ok": all(value == "PASS" for value in checks.values()),
            "status": "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL",
            "title": "Real Three-MinIO Storage Control Plane, Tiering, Placement, and Control Recovery E2E",
            "checks": checks,
            "checkProvenance": check_provenance,
            "requiredProofChecks": {k: list(v) for k, v in REQUIRED_PROOF_CHECKS.items()},
            "proofArtifact": proof_artifact,
            "scenarios": results,
            "endpoints": {name: os.environ.get(name) for name in REQUIRED_ENDPOINTS},
        },
        root=ROOT,
    )
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
