"""Unit tests for RecoveryClass classification and RTO calibration (Recovery Assurance Gate F)."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_recovery_class,
)


def test_size_and_chain_depth_buckets() -> None:
    assert backup_recovery_class.size_bucket(500 * 1024) == "small"
    assert backup_recovery_class.size_bucket(50 * 1024 * 1024) == "medium"
    assert backup_recovery_class.size_bucket(500 * 1024 * 1024) == "large"

    assert backup_recovery_class.chain_depth_bucket(1) == "shallow"
    assert backup_recovery_class.chain_depth_bucket(3) == "shallow"
    assert backup_recovery_class.chain_depth_bucket(4) == "moderate"
    assert backup_recovery_class.chain_depth_bucket(10) == "moderate"
    assert backup_recovery_class.chain_depth_bucket(15) == "deep"


def test_classify_recovery() -> None:
    rclass = backup_recovery_class.classify_recovery(
        target_kind="s3",
        storage_protocol="object-set-v1",
        logical_bytes=100 * 1024 * 1024,
        chain_length=5,
    )
    assert rclass.target_kind == "s3"
    assert rclass.format_kind == "object-set-v1"
    assert rclass.size_category == "large"
    assert rclass.chain_depth == "moderate"
    assert rclass.key == "s3:object-set-v1:large:moderate"
    assert str(rclass) == "s3:object-set-v1:large:moderate"
    d = rclass.to_dict()
    assert d["targetKind"] == "s3"
    assert d["formatKind"] == "object-set-v1"

    # Default classify
    c_default = backup_recovery_class.classify_recovery()
    assert c_default.target_kind == "filesystem"
    assert c_default.format_kind == "single-file"


def test_percentile_calculations() -> None:
    assert backup_recovery_class._percentile([], 0.5) == 0.0
    assert backup_recovery_class._percentile([10.0], 0.5) == 10.0
    assert backup_recovery_class._percentile([10.0, 20.0, 30.0], 0.5) == 20.0
    assert backup_recovery_class._percentile([10.0, 20.0], 0.5) == 15.0


def test_calibrate_rto_no_samples(tmp_settings: Path) -> None:
    rclass = backup_recovery_class.classify_recovery(
        target_kind="s3",
        storage_protocol="object-set-v1",
        logical_bytes=10 * 1024 * 1024,
        chain_length=2,
    )
    estimate = backup_recovery_class.calibrate_rto(
        target_id="target_s3",
        logical_bytes=10 * 1024 * 1024,
        chain_length=2,
        recovery_class=rclass,
    )
    assert estimate["isSla"] is False
    assert estimate["confidence"] == "low"
    assert estimate["sampleCount"] == 0
    assert estimate["p50Seconds"] > 0
    assert estimate["p90Seconds"] >= estimate["p50Seconds"]
    assert estimate["recoveryClass"] == "s3:object-set-v1:medium:shallow"


def test_calibrate_rto_with_historical_samples(tmp_settings: Path) -> None:
    rclass = backup_recovery_class.classify_recovery(
        target_kind="s3",
        storage_protocol="object-set-v1",
        logical_bytes=10 * 1024 * 1024,
        chain_length=2,
    )
    # Record 10 stage samples in ledger to test high confidence
    for duration_ms in [400.0, 450.0, 500.0, 550.0, 600.0, 650.0, 700.0, 750.0, 800.0, 900.0]:
        backup_dr_ledger.record_stage_sample(
            stage="transfer",
            bytes_transferred=10 * 1024 * 1024,
            duration_ms=duration_ms,
            result="success",
            observed_at="2026-08-15T00:00:00Z",
            recovery_class=rclass.key,
        )
        backup_dr_ledger.record_stage_sample(
            stage="crypto",
            bytes_transferred=10 * 1024 * 1024,
            duration_ms=duration_ms / 2,
            result="success",
            observed_at="2026-08-15T00:00:00Z",
            recovery_class=rclass.key,
        )
        backup_dr_ledger.record_stage_sample(
            stage="materialize",
            bytes_transferred=10 * 1024 * 1024,
            duration_ms=duration_ms / 4,
            result="success",
            observed_at="2026-08-15T00:00:00Z",
            recovery_class=rclass.key,
        )

    estimate = backup_recovery_class.calibrate_rto(
        target_id="target_s3",
        logical_bytes=10 * 1024 * 1024,
        chain_length=2,
        recovery_class=rclass,
    )
    assert estimate["isSla"] is False
    assert estimate["confidence"] == "high"
    assert estimate["sampleCount"] == 10
    assert estimate["p50Seconds"] > 0
    assert estimate["p90Seconds"] >= estimate["p50Seconds"]
    assert "stageEstimates" in estimate
