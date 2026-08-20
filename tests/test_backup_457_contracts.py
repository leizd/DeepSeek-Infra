"""Contract test suite for Topology Safety, Capacity Governance & Bandwidth QoS."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_drain,
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_scheduler,
    backup_targets,
    backup_write_continuity,
)
from deepseek_infra.infra.workspace.backup_transfer_budget import TrafficClass, TransferBudgetManager


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))
    from deepseek_infra.infra.workspace import backup_control

    monkeypatch.setattr(backup_control, "CONTROL_DIR", tmp_path / ".backup-control")
    monkeypatch.setattr(backup_control, "CONTROL_DB", tmp_path / ".backup-control" / "control.sqlite3")


# ── Gate F: Bandwidth QoS & TransferBudgetManager ───────────────────────────


def test_transfer_budget_qos_traffic_classes() -> None:
    assert TrafficClass.P0_DISASTER_RECOVERY.priority == 0
    assert TrafficClass.P1_ACTIVE_BACKUP_PUBLISH.priority == 1
    assert TrafficClass.P2_REQUIRED_REPLICA_REPAIR.priority == 2
    assert TrafficClass.P3_REQUIRED_REPLICATION.priority == 3
    assert TrafficClass.P4_SCRUB_AND_DRILL.priority == 4
    assert TrafficClass.P5_REBALANCE_AND_DRAIN.priority == 5
    assert TrafficClass.P6_BEST_EFFORT.priority == 6


def test_transfer_budget_token_bucket_and_dr_reservation() -> None:
    mgr = TransferBudgetManager(
        global_bandwidth_bytes_per_sec=10_000_000,  # 10 MB/s
        reserved_dr_bandwidth_bytes_per_sec=2_000_000,  # 2 MB/s reserved for P0
        max_burst_bytes=20_000_000,
    )

    # Acquire DR bandwidth (P0)
    grant_p0 = mgr.acquire_bandwidth(1_000_000, traffic_class=TrafficClass.P0_DISASTER_RECOVERY)
    assert grant_p0 >= 1_000_000

    # Acquire background bandwidth (P5)
    grant_p5 = mgr.acquire_bandwidth(1_000_000, traffic_class=TrafficClass.P5_REBALANCE_AND_DRAIN)
    assert grant_p5 >= 1_000_000

    # Test concurrency tracking
    with mgr.track_transfer("t-dest", "t-src", TrafficClass.P2_REQUIRED_REPLICA_REPAIR):
        summary = mgr.transfer_control_summary()
        assert summary["activeRepairTransfers"] == 1
        assert summary["activeRecoveryTransfers"] == 0

    summary_after = mgr.transfer_control_summary()
    assert summary_after["activeRepairTransfers"] == 0


def test_transfer_budget_throttled_generator() -> None:
    mgr = TransferBudgetManager(
        global_bandwidth_bytes_per_sec=1_000_000_000,
        reserved_dr_bandwidth_bytes_per_sec=0,
    )
    chunks = [b"chunk1_12345", b"chunk2_67890", b"chunk3_abcde"]
    gen = mgr.throttled_generator(
        iter(chunks),
        traffic_class=TrafficClass.P3_REQUIRED_REPLICATION,
        dest_target_id="target-1",
        source_target_id="target-2",
    )
    collected = list(gen)
    assert collected == chunks


# ── Gate E: Capacity Governance & Size Admission ────────────────────────────


def test_capacity_admission_watermarks(tmp_settings: Path) -> None:
    target_id = "target_cap_test"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "cap_root", failure_domain="zone-a")

    policy = {
        "policyId": "pol-cap",
        "placement": {
            "softWatermarkPercent": 80.0,
            "hardWatermarkPercent": 90.0,
            "minFreeBytes": 1000,
        },
    }

    # Probing target capacity returns free bytes and percentage
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 10000, "freeBytes": 5000, "freePercent": 50.0}):
        admitted, reason = backup_capacity.check_target_capacity_admission(target_id, 1000, policy=policy)
        assert admitted is True
        assert reason == "admitted"

    # When free percent is below hard watermark (e.g. only 5% free -> 95% used)
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 10000, "freeBytes": 500, "freePercent": 5.0}):
        admitted, reason = backup_capacity.check_target_capacity_admission(target_id, 100, policy=policy)
        assert admitted is False
        assert "hard-watermark-exceeded" in reason


def test_predict_next_backup_bytes_p90(tmp_settings: Path) -> None:
    # Empty history remains unavailable instead of pretending a small P90.
    val_default = backup_capacity.predict_next_backup_bytes("empty-pol", snapshot_kind="full")
    assert val_default is None

    # With historical ledger data
    policy_id = "p90-pol"
    for i in range(10):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id="managed-local",
            policy_id=policy_id,
            backup_id=f"bk-{i}",
            committed_at=f"2026-08-18T10:0{i}:00Z",
            state="healthy",
            recoverable=True,
            metadata={"physicalBytes": 1000 + i * 100, "snapshotKind": "full"},
        )

    p90_val = backup_capacity.predict_next_backup_bytes(policy_id, snapshot_kind="full")
    assert p90_val is not None
    assert p90_val >= 1800  # P90 of 1000..1900


def test_estimate_target_exhaustion_horizon(tmp_settings: Path) -> None:
    target_id = "target_horizon_test"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "horizon_root")

    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 100_000_000, "freeBytes": 10_000_000, "freePercent": 10.0}):
        res = backup_capacity.estimate_target_exhaustion_horizon(target_id, policy_id="test-pol")
        assert res["status"] in {"critical", "degraded", "healthy"}
        assert "estimatedDaysToFull" in res


# ── Gate B: Copy Retirement & Reference-Counted GC ──────────────────────────


def test_copy_retirement_lifecycle_and_gc(tmp_settings: Path) -> None:
    policy_id = "pol-retire"
    backup_id = "bk-retire-1"
    target_id = "target_retire"

    t_dir = tmp_settings / "retire_store"
    t_dir.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target(target_id, path=t_dir, failure_domain="zone-a")

    # Create dummy objects on target
    c_dir = t_dir / "ciphertext" / "sha256"
    c_dir.mkdir(parents=True, exist_ok=True)
    obj1 = c_dir / "1111111111111111111111111111111111111111111111111111111111111111"
    obj2 = c_dir / "2222222222222222222222222222222222222222222222222222222222222222"
    obj1.write_bytes(b"ciphertext1")
    obj2.write_bytes(b"ciphertext2")

    # Record receipt with obj1 and obj2
    rec_dir = t_dir / "receipts" / policy_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schemaVersion": 4,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "components": [
            {"digest": obj1.name, "byteSize": 11},
            {"digest": obj2.name, "byteSize": 11},
        ],
    }
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (rec_dir / f"{backup_id}.receipt.json").write_bytes(receipt_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-18T10:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    commit_path = t_dir / "commits" / policy_id / f"{backup_id}.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_text(json.dumps(commit), encoding="utf-8")

    # Another retained backup references obj1
    (rec_dir / "bk-other.receipt.json").write_text(
        json.dumps(
            {
                "schemaVersion": "receipt-v4",
                "policyId": policy_id,
                "backupId": "bk-other",
                "components": [
                    {"digest": obj1.name, "byteSize": 11},
                ],
            }
        )
    )

    # Record healthy copy in ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    # Mock simulate_copy_removal to allow retirement
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        job = backup_retirement.create_copy_retirement_job(
            policy_id=policy_id,
            backup_id=backup_id,
            target_id=target_id,
            reason="unit-test-retirement",
        )
        assert job["phase"] == "requested"

        res = backup_retirement.execute_copy_retirement_job(job["jobId"])
        assert res["phase"] == "reclaimed"

        # obj1 must be preserved because it is still referenced by bk-other!
        assert obj1.is_file()
        # obj2 was only referenced by bk-retire-1, so it is physically reclaimed!
        assert not obj2.is_file()


# ── Gate D: Autonomous Target Drain ─────────────────────────────────────────


def test_target_drain_lifecycle(tmp_settings: Path) -> None:
    target_id = "target_drain_1"
    alt_target_id = "target_drain_2"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "d1", failure_domain="zone-a")
    backup_targets.register_filesystem_target(alt_target_id, path=tmp_settings / "d2", failure_domain="zone-b")

    policy_id = "drain-pol"
    backup_policies.create_policy(
        {
            "name": "Drain Policy",
            "policyId": policy_id,
            "targetId": target_id,
            "replication": {
                "enabled": True,
                "minCommittedCopies": 1,
                "minFailureDomains": 1,
                "targets": [{"targetId": alt_target_id, "mode": "required"}],
            },
        }
    )

    # Initiate drain
    job = backup_drain.initiate_target_drain(target_id, reason="test-drain")
    assert job["phase"] == "draining"

    target_meta = backup_targets.get_target(target_id)
    assert target_meta["drainState"] == "draining"

    # Cancel drain
    cancelled = backup_drain.cancel_target_drain(target_id, reason="cancel-test")
    assert cancelled["phase"] == "cancelled"

    target_meta_after = backup_targets.get_target(target_id)
    assert target_meta_after["drainState"] == "active"


# ── Gate A: Incremental Parent Authentication ───────────────────────────────


def test_authenticate_transition_parent_exact_commitments(tmp_settings: Path) -> None:
    target_id = "target_parent"
    p_dir = tmp_settings / "parent_target"
    backup_targets.register_filesystem_target(target_id, path=p_dir)
    target = backup_publish.resolve_target(target_id)

    policy_id = "pol-parent"
    parent_id = "parent-bk-1"

    # When target does not have parent copy
    ok, reason = backup_replication.authenticate_transition_parent(
        target,
        policy_id,
        expected_parent_backup_id=parent_id,
    )
    assert ok is False
    assert "parent-copy-status-missing" in reason

    # Create matching parent receipt and commit
    with patch.object(
        backup_replication,
        "authenticate_recovery_copy",
        return_value=(
            "authenticated",
            {"backupId": parent_id, "receiptDigest": "rec_digest_123", "objectSetDigest": "osd_123", "lineageId": "lin_1"},
            {"backupId": parent_id, "commitHash": "com_hash_123", "receiptDigest": "rec_digest_123", "lineageId": "lin_1", "objectSetDigest": "osd_123"},
        ),
    ):
        # All match
        ok, reason = backup_replication.authenticate_transition_parent(
            target,
            policy_id,
            expected_parent_backup_id=parent_id,
            expected_receipt_digest="rec_digest_123",
            expected_commit_hash="com_hash_123",
            expected_lineage_id="lin_1",
            expected_object_set_digest="osd_123",
        )
        assert ok is True
        assert reason == "authenticated"

        # Receipt digest mismatch
        ok_rec, reason_rec = backup_replication.authenticate_transition_parent(
            target,
            policy_id,
            expected_parent_backup_id=parent_id,
            expected_receipt_digest="wrong_rec_digest",
        )
        assert ok_rec is False
        assert reason_rec == "parent-receipt-digest-mismatch"

        # Commit hash mismatch
        ok_com, reason_com = backup_replication.authenticate_transition_parent(
            target,
            policy_id,
            expected_parent_backup_id=parent_id,
            expected_commit_hash="wrong_commit_hash",
        )
        assert ok_com is False
        assert reason_com == "parent-commit-hash-mismatch"


# ── Gate C: Deterministic Failure-Domain Placement Planner ──────────────────


def test_plan_target_placement_ranking(tmp_settings: Path) -> None:
    t1 = "target_fd_1"
    t2 = "target_fd_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "t1", failure_domain="zone-a", priority=10)
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "t2", failure_domain="zone-b", priority=20)

    policy = {
        "policyId": "pol-placement",
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "maxCopiesPerFailureDomain": 2,
        },
        "placement": {
            "softWatermarkPercent": 80.0,
            "hardWatermarkPercent": 90.0,
        },
    }

    # Record existing copy in zone-a
    logical_id = backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1,
        policy_id="pol-placement",
        backup_id="bk-existing",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    # Plan placement: t2 provides failure-domain diversity gain (gain = -1) compared to t1 (gain = 0)
    with patch.object(backup_capacity, "check_target_capacity_admission", return_value=(True, "admitted")), \
         patch.object(backup_replication, "calculate_replica_lag", return_value={"lagRecoveryPoints": 0}):
        scored = backup_scheduler.plan_target_placement(
            policy,
            candidate_target_ids=[t1, t2],
            primary_target_id="managed-local",
            logical_recovery_point_id=logical_id,
            required_bytes=1024,
        )
    assert [target_id for _, target_id in scored] == [t2]


# ── Gate D: Primary Promotion with Global Latest Point ──────────────────────


def test_promote_primary_target_with_global_latest(tmp_settings: Path) -> None:
    t1 = "target_primary_1"
    t2 = "target_primary_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "pt1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "pt2")

    policy_id = "pol-promote"
    backup_policies.create_policy(
        {
            "name": "Promote Policy",
            "policyId": policy_id,
            "targetId": t1,
            "replication": {
                "enabled": True,
                "targets": [{"targetId": t2, "mode": "required"}],
            },
        }
    )

    # Record global latest copy on t1 only
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1,
        policy_id=policy_id,
        backup_id="bk-latest-99",
        committed_at="2026-08-18T12:00:00Z",
        state="healthy",
        recoverable=True,
    )

    # Promoting t2 must fail because t2 does not possess the latest logical recovery point!
    with pytest.raises(AppError) as exc_info:
        backup_write_continuity.promote_primary_target(policy_id, t2)
    assert exc_info.value.status == 409
    assert "primary-promotion-rejected" in str(exc_info.value)

    # Replicating latest point to t2 allows promotion
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t2,
        policy_id=policy_id,
        backup_id="bk-latest-99",
        committed_at="2026-08-18T12:00:00Z",
        state="healthy",
        recoverable=True,
    )

    promoted = backup_write_continuity.promote_primary_target(policy_id, t2)
    assert promoted["promotedPrimaryTargetId"] == t2
