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
    assert runner.SCENARIOS == {
        "real-three-minio-storage-control-plane": (node_458,),
        "real-three-minio-tiering-control-recovery": (node_459,),
        "real-three-minio-autonomous-placement-control": (node_460,),
        "real-three-minio-transactional-gc-fencing": (node_462,),
        "real-three-minio-control-authority-disaster-recovery": (node_463,),
        "real-three-minio-fresh-process-authority-recovery": (node_465_a, node_465_b),
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
    assert runner.CHECK_SCENARIOS["realFreshProcessDetectsRemoteAuthority"] == (
        "real-three-minio-fresh-process-authority-recovery"
    )
    assert runner.CHECK_SCENARIOS["realProcessCrashAfterPreparedMutationRecoversExactlyOnce"] == (
        "real-three-minio-fresh-process-authority-recovery"
    )


def test_real_evidence_sources_forbid_fake_s3_stub_crypto_and_resolver_monkeypatch() -> None:
    for rel in (
        "tests/test_backup_458_real_storage_control_plane_e2e.py",
        "tests/test_backup_459_real_tiering_control_e2e.py",
        "tests/test_backup_460_real_placement_control_e2e.py",
        "tests/test_backup_462_real_transactional_gc_e2e.py",
        "tests/test_backup_463_real_control_authority_disaster_e2e.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "ProductionFakeS3Client" not in source
        assert "stub_crypto" not in source
        assert "monkeypatch.setattr(backup_publish" not in source
        assert "monkeypatch.setattr(backup_executor" not in source


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
    assert workflow.count("      - storage-control-plane-minio-e2e\n") == 2
    assert "RC_CI_STORAGE_CONTROL_PLANE_MINIO_E2E" in workflow
