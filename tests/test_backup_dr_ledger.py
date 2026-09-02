"""Unit tests for DR Evidence Ledger (Recovery Assurance Gate C)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import backup_dr_ledger


def test_dr_ledger_schema_and_empty(tmp_settings: Path) -> None:
    assert backup_dr_ledger.list_scopes() == []
    assert backup_dr_ledger.get_latest_recoverable_point("target_1") == (None, [])
    assert backup_dr_ledger.get_latest_scrub_outcome("target_1") is None
    assert backup_dr_ledger.get_latest_drill_outcome("target_1") is None
    assert backup_dr_ledger.get_target_evidence("target_1") is None
    assert backup_dr_ledger.list_target_evidence() == []
    assert backup_dr_ledger.list_stage_samples() == []
    assert backup_dr_ledger.get_latest_audit_evidence("target_1") is None


def test_dr_ledger_connection_context_closes_database(tmp_settings: Path) -> None:
    with backup_dr_ledger._get_connection() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_dr_ledger_recovery_points(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="policy_prod",
        backup_id="bk_001",
        committed_at="2026-08-15T00:00:00Z",
        snapshot_kind="full",
        parent_backup_id=None,
        chain_digest="hash001",
        chain_length=1,
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
        verified_at="2026-08-15T00:00:05Z",
        storage_protocol="object-set-v1",
        metadata={"custom": "data1"},
    )
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="policy_prod",
        backup_id="bk_002",
        committed_at="2026-08-15T01:00:00Z",
        snapshot_kind="incremental",
        parent_backup_id="bk_001",
        chain_digest="hash002",
        chain_length=2,
        ciphertext_bytes=500,
        logical_bytes=2500,
        recoverable=True,
        verified_at="2026-08-15T01:00:05Z",
        storage_protocol="object-set-v1",
        metadata={"custom": "data2"},
    )

    scopes = backup_dr_ledger.list_scopes()
    assert ("target_1", "policy_prod") in scopes

    latest, chain = backup_dr_ledger.get_latest_recoverable_point("target_1", "policy_prod")
    assert latest is not None
    assert latest["backupId"] == "bk_002"
    assert latest["chainLength"] == 2
    assert latest["snapshotKind"] == "incremental"
    assert latest["logicalBytes"] == 2500
    assert len(chain) == 2

    all_pts = backup_dr_ledger.list_recovery_points(target_id="target_1", policy_id="policy_prod")
    assert len(all_pts) == 2
    assert all_pts[0]["backupId"] == "bk_002"
    assert all_pts[1]["backupId"] == "bk_001"

    # Broken chain resolution
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="policy_prod",
        backup_id="bk_orphan",
        committed_at="2026-08-15T02:00:00Z",
        snapshot_kind="incremental",
        parent_backup_id="bk_nonexistent",
        recoverable=True,
    )
    assert backup_dr_ledger.resolve_recoverable_chain("target_1", "policy_prod", "bk_orphan") is None


def test_dr_ledger_scrub_evidence(tmp_settings: Path) -> None:
    backup_dr_ledger.record_scrub_evidence(
        target_id="target_s3",
        backup_id="bk_100",
        observed_at="2026-08-15T02:00:00Z",
        result="success",
        details={"checks": {"format": "PASS"}},
    )
    backup_dr_ledger.record_scrub_evidence(
        target_id="target_s3",
        backup_id="bk_101",
        observed_at="2026-08-15T03:00:00Z",
        result="failed",
        details={"error": "corrupt"},
    )
    scrub = backup_dr_ledger.get_latest_scrub_outcome("target_s3")
    assert scrub is not None
    assert scrub["targetId"] == "target_s3"
    assert scrub["result"] == "failed"
    assert scrub["status"] == "error"
    assert scrub["latestSuccessfulAt"] == "2026-08-15T02:00:00Z"

    items = backup_dr_ledger.get_scrub_evidence(target_id="target_s3", backup_id="bk_100")
    assert len(items) == 1
    assert items[0]["backupId"] == "bk_100"


def test_dr_ledger_drill_evidence(tmp_settings: Path) -> None:
    backup_dr_ledger.record_drill_evidence(
        target_id="target_s3",
        policy_id="p1",
        backup_id="bk_100",
        drill_kind="automated",
        observed_at="2026-08-15T03:00:00Z",
        result="success",
        work_class="recovery-drill",
        stage_durations={"fetchMs": 150, "materializeMs": 200, "totalMs": 350},
        details={"verified": True},
    )
    drill = backup_dr_ledger.get_latest_drill_outcome("target_s3", "p1")
    assert drill is not None
    assert drill["drillKind"] == "automated"
    assert drill["result"] == "success"
    assert drill["stageDurations"]["totalMs"] == 350

    drills = backup_dr_ledger.get_drill_evidence(target_id="target_s3", policy_id="p1")
    assert len(drills) == 1
    assert drills[0]["backupId"] == "bk_100"


def test_dr_ledger_target_evidence(tmp_settings: Path) -> None:
    backup_dr_ledger.record_target_evidence(
        target_id="target_s3",
        observed_at="2026-08-15T04:00:00Z",
        scheduled_ready=True,
        integrity_mode="strong-provider-checksum",
        status="ok",
        reason=None,
        details={"region": "us-east-1"},
    )
    t_ev = backup_dr_ledger.get_target_evidence("target_s3")
    assert t_ev is not None
    assert t_ev["scheduledReady"] is True
    assert t_ev["integrityMode"] == "strong-provider-checksum"

    all_t = backup_dr_ledger.list_target_evidence()
    assert len(all_t) == 1
    assert all_t[0]["targetId"] == "target_s3"


def test_dr_ledger_stage_samples_and_audit(tmp_settings: Path) -> None:
    backup_dr_ledger.record_stage_sample(
        stage="transfer",
        bytes_transferred=10 * 1024 * 1024,
        duration_ms=250.0,
        result="success",
        observed_at="2026-08-15T05:00:00Z",
        recovery_class="s3:object-set:medium:shallow",
    )
    backup_dr_ledger.record_stage_sample(
        stage="crypto",
        bytes_transferred=10 * 1024 * 1024,
        duration_ms=100.0,
        result="success",
        observed_at="2026-08-15T05:00:01Z",
        recovery_class="s3:object-set:medium:shallow",
    )

    samples = backup_dr_ledger.list_stage_samples()
    assert len(samples) == 2

    transfer_samples = backup_dr_ledger.list_stage_samples(stage="transfer", since_iso="2026-08-15T04:00:00Z")
    assert len(transfer_samples) == 1
    assert transfer_samples[0]["stage"] == "transfer"
    assert transfer_samples[0]["recoveryClass"]["tag"] == "s3:object-set:medium:shallow"

    # Audit evidence
    backup_dr_ledger.record_audit_evidence(
        target_id="target_s3",
        observed_at="2026-08-15T06:00:00Z",
        result="success",
        anomalies_count=0,
        records_checked=42,
        details={"objectsAudited": 42},
    )

    audit = backup_dr_ledger.get_latest_audit_evidence("target_s3")
    assert audit is not None
    assert audit["targetId"] == "target_s3"
    assert audit["status"] == "success"
    assert audit["recordsChecked"] == 42
