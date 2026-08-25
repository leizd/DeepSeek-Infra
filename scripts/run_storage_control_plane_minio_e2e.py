#!/usr/bin/env python3
"""Run the real three-MinIO, real-Age Storage Control Plane Evidence gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402
from scripts.release_evidence import stamp_release_report  # noqa: E402

REAL_SCENARIO = "real-three-minio-storage-control-plane"
TIER_SCENARIO = "real-three-minio-tiering-control-recovery"
PLACE_SCENARIO = "real-three-minio-autonomous-placement-control"
TXGC_SCENARIO = "real-three-minio-transactional-gc-fencing"
AUTH_DR_SCENARIO = "real-three-minio-control-authority-disaster-recovery"
FS_FRESH_AUTH_SCENARIO = "fresh-process-filesystem-authority-recovery"
FRESH_AUTH_SCENARIO = "real-three-minio-fresh-process-authority-recovery"
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
    "realFreshProcessRestoresPreDisasterBackup": FRESH_AUTH_SCENARIO,
    "realFreshProcessCreatesPostRecoveryBackup": FRESH_AUTH_SCENARIO,
    "realFreshProcessBootEpochStrictlyIncreases": FRESH_AUTH_SCENARIO,
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
        for scenario, node_ids in SCENARIOS.items():
            command = [sys.executable, "-m", "pytest", "--no-cov", "-q", *node_ids]
            completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
            results[scenario] = {"nodeIds": list(node_ids), "exitCode": completed.returncode}

    checks = {
        check: "PASS" if results[scenario]["exitCode"] == 0 else "FAIL"
        for check, scenario in CHECK_SCENARIOS.items()
    }
    report = stamp_release_report(
        {
            "ok": all(value == "PASS" for value in checks.values()),
            "status": "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL",
            "title": "Real Three-MinIO Storage Control Plane, Tiering, Placement, and Control Recovery E2E",
            "checks": checks,
            "checkProvenance": dict(CHECK_SCENARIOS),
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
