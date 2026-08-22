"""Coverage booster for placement controller and target-sharded maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_maintenance,
    backup_object_index,
    backup_placement,
    backup_policies,
    backup_retirement,
    backup_targets,
    backup_tiering,
)


def test_normalize_recovery_placement_validation() -> None:
    with pytest.raises(AppError):
        backup_placement.normalize_recovery_placement("bad")
    with pytest.raises(AppError):
        backup_placement.normalize_recovery_placement(
            {"hotWindowSeconds": 100, "warmWindowSeconds": 50, "archiveAfterSeconds": 200}
        )
    ok = backup_placement.normalize_recovery_placement({"enabled": False})
    assert ok["enabled"] is False


def test_parse_iso_and_desired_tier_edges() -> None:
    assert backup_placement._parse_iso(None) is None
    assert backup_placement._parse_iso("not-a-date") is None
    naive = backup_placement._parse_iso("2026-01-01T00:00:00")
    assert naive is not None and naive.tzinfo is not None
    zulu = backup_placement._parse_iso("2026-01-01T00:00:00Z")
    assert zulu is not None
    placement = {"hotWindowSeconds": 10, "warmWindowSeconds": 20, "archiveAfterSeconds": 30}
    assert backup_placement.desired_tier_for_age(15, placement) == "warm"
    assert backup_placement.desired_tier_for_age(25, placement) == "warm"
    assert backup_placement.desired_tier_for_age(30, placement) == "archive"


def test_evaluate_objectives_satisfied_and_capacity_blocks(tmp_settings: Path) -> None:
    hot = "target_cov_hot"
    warm = "target_cov_warm"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")
    targets = {hot: backup_targets.get_target(hot), warm: backup_targets.get_target(warm)}
    policy = {
        "policyId": "p",
        "primaryTargetId": hot,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 86400,
            "warmWindowSeconds": 604800,
            "archiveAfterSeconds": 2592000,
            "minHotCopies": 1,
        },
    }
    unit = {"closureComplete": True, "memberBackupIds": ["b1"], "anchorBackupId": "b1"}
    now = datetime.now(tz=timezone.utc)
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        sat = backup_placement.evaluate_point_placement(
            policy,
            "b1",
            committed_at=now.isoformat(),
            copies=[{"backupId": "b1", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    assert sat["action"] == "none"
    assert "objectives-satisfied" in sat["reasonCodes"]

    # Critical capacity rejects dest
    aged = (now - timedelta(seconds=100000)).isoformat()
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "critical"},
    ):
        blocked = backup_placement.evaluate_point_placement(
            policy,
            "b1",
            committed_at=aged,
            copies=[{"backupId": "b1", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    assert blocked["action"] == "blocked"
    assert "all-candidates-rejected" in blocked["reasonCodes"] or "no-eligible" in str(blocked["reasonCodes"])


def test_reconcile_skips_disabled_and_missing_policy(tmp_settings: Path) -> None:
    missing = backup_placement.reconcile_policy_placement("policy_missing_xyz")
    assert missing["status"] == "error"
    policy = backup_policies.normalize_policy({"name": "dis", "enabled": True, "recoveryPlacement": {"enabled": False}})
    with patch.object(backup_policies, "get_policy", return_value=policy):
        skipped = backup_placement.reconcile_policy_placement(str(policy["policyId"]))
    assert skipped["status"] == "skipped"
    with patch.object(backup_policies, "list_policies", return_value=[{"policyId": "p", "recoveryPlacement": {"enabled": False}}]):
        all_res = backup_placement.reconcile_all_policies()
    assert all_res["policies"] == 0


def test_rebalance_and_chain_migration_scope_sharding(tmp_settings: Path) -> None:
    seen: list[str] = []

    def fake_lease(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        seen.append(f"{worker_kind}:{scope_id}")
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    rebalance_jobs = [
        {"jobId": "rb1", "destTargetId": "d1", "phase": "pending"},
        {"jobId": "rb2", "destTargetId": "d2", "phase": "pending"},
    ]
    with patch.object(
        backup_maintenance.backup_replication, "list_rebalance_jobs", return_value=rebalance_jobs
    ), patch.object(
        backup_maintenance.backup_replication,
        "execute_rebalance_job",
        return_value={"status": "success"},
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=fake_lease):
        rb = backup_maintenance._process_rebalance_scopes(instance_id="i", limit=5)
    assert rb["scopes"] == 2
    assert "rebalance:d1" in seen and "rebalance:d2" in seen

    seen.clear()
    mig_jobs = [
        {"migrationId": "m1", "destTargetId": "md1", "phase": "planned"},
        {"migrationId": "m2", "destTargetId": "md2", "phase": "transferring"},
    ]
    with patch.object(backup_control, "list_chain_migration_jobs", return_value=mig_jobs), patch.object(
        backup_tiering, "execute_chain_migration", return_value={"phase": "converged"}
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=fake_lease):
        mig = backup_maintenance._process_chain_migration_scopes(instance_id="i", limit=5)
    assert mig["scopes"] == 2
    assert mig["processed"] == 2


def test_lease_skip_on_held_rebalance_scope(tmp_settings: Path) -> None:
    held = backup_control.acquire_maintenance_lease(
        "rebalance", "held-dest", owner_instance_id="other", lease_seconds=60
    )
    assert held is not None
    jobs = [{"jobId": "j", "destTargetId": "held-dest", "phase": "pending"}]
    with patch.object(backup_maintenance.backup_replication, "list_rebalance_jobs", return_value=jobs), patch.object(
        backup_maintenance.backup_replication, "execute_rebalance_job"
    ) as exe:
        summary = backup_maintenance._process_rebalance_scopes(instance_id="worker", limit=2)
    assert summary["leaseSkips"] >= 1
    exe.assert_not_called()


def test_incomplete_index_over_retains_then_receipt_scan(tmp_settings: Path) -> None:
    tid = "target_over_retain"
    root = tmp_settings / tid
    root.mkdir(parents=True)
    backup_targets.register_filesystem_target(tid, path=root)
    backup_object_index.index_receipt_objects(
        target_id=tid,
        policy_id="p",
        backup_id="keep",
        receipt={"policyId": "p", "backupId": "keep", "objects": [{"path": "objects/sha256/aa/k.age", "size": 1}]},
        ref_state="live",
    )
    target = type("T", (), {"target_id": tid, "root": root, "store": None})()
    assert backup_retirement._payload_key_is_retained(target, "objects/sha256/aa/k.age", retiring_backup_id="drop")
    # Complete coverage: index authoritative
    backup_control.set_target_index_coverage(tid, state="complete", formal_receipt_count=1)
    assert backup_object_index.retained_payload_keys_from_index(tid, retiring_backup_id="drop") is not None
    assert backup_retirement._payload_key_is_retained(target, "objects/sha256/aa/k.age", retiring_backup_id="drop")


def test_group_jobs_by_scope_prefers_first_key() -> None:
    jobs = [
        {"destTargetId": "a", "targetId": "x"},
        {"targetId": "only"},
        {},
    ]
    grouped = backup_maintenance._group_jobs_by_scope(jobs, scope_keys=("destTargetId", "targetId"))
    assert set(grouped) == {"a", "only", "unscoped"}
