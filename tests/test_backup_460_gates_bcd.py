"""4.6.0 Gates B/C/D — coverage, lineage, chain migration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_object_index,
    backup_targets,
    backup_tiering,
)


def test_incomplete_index_does_not_self_deadlock_retirement_gc(tmp_settings: Path) -> None:
    """Retirement may write partial index rows; GC must still proceed via receipt path."""
    from deepseek_infra.infra.workspace import backup_retirement

    tid = "target_ret_gc"
    root = tmp_settings / tid
    root.mkdir(parents=True)
    backup_targets.register_filesystem_target(tid, path=root)
    # Partial index without coverage complete (what retirement writes).
    backup_object_index.index_receipt_objects(
        target_id=tid,
        policy_id="p",
        backup_id="drop",
        receipt={"policyId": "p", "backupId": "drop", "objects": [{"path": "objects/sha256/aa/x.age", "size": 1}]},
        ref_state="retired",
    )
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is False
    assert "incomplete" in reason or "missing" in reason
    # Incomplete index still over-retains known live keys.
    target = type("T", (), {"target_id": tid, "root": root, "store": None})()
    backup_object_index.index_receipt_objects(
        target_id=tid,
        policy_id="p",
        backup_id="keep",
        receipt={"policyId": "p", "backupId": "keep", "objects": [{"path": "objects/sha256/bb/y.age", "size": 1}]},
        ref_state="live",
    )
    assert backup_retirement._payload_key_is_retained(target, "objects/sha256/bb/y.age", retiring_backup_id="drop")
    # Unknown key with no receipts on disk is not retained (safe to GC candidate).
    assert not backup_retirement._payload_key_is_retained(target, "objects/sha256/cc/z.age", retiring_backup_id="drop")


def test_index_coverage_blocks_gc_until_complete(tmp_settings: Path) -> None:
    tid = "target_cov_gc"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    backup_control.put_recovery_object_ref(
        target_id=tid,
        policy_id="p",
        backup_id="b",
        object_key="objects/sha256/aa/x.age",
        ref_state="retired",
        size_bytes=10,
        physical=True,
    )
    # Index rows exist but coverage incomplete → GC blocked
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is False
    assert "incomplete" in reason or "missing" in reason
    assert backup_object_index.gc_candidate_keys(tid) == []

    backup_control.set_target_index_coverage(tid, state="complete", formal_receipt_count=1)
    allowed2, reason2 = backup_object_index.gc_allowed(tid)
    assert allowed2 is True
    assert reason2 == "ok"
    assert "objects/sha256/aa/x.age" in backup_object_index.gc_candidate_keys(tid)


def test_rebuild_sets_coverage_complete(tmp_settings: Path) -> None:
    tid = "target_rebuild_cov"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "b1.json").write_text(
        '{"policyId":"p","backupId":"b1","objects":[{"digest":"' + "aa" * 32 + '","size":5}]}',
        encoding="utf-8",
    )
    backup_targets.register_filesystem_target(tid, path=root)
    target = SimpleNamespace(target_id=tid, root=root, store=None)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        result = backup_object_index.rebuild_index_from_target(target)
    assert result["coverageState"] == "complete"
    assert result["scannedReceipts"] >= 1
    cov = backup_control.get_target_index_coverage(tid)
    assert cov is not None and cov["state"] == "complete"


def test_capacity_forecast_persists_and_readiness_reads_it(tmp_settings: Path) -> None:
    tid = "target_fcst"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    # Probe path records projection
    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={
            "freeBytes": 1000,
            "totalBytes": 2000,
            "freePercent": 50.0,
            "physicalStoredBytes": 1000,
            "observedAt": "2026-01-01T00:00:00Z",
        },
    ):
        backup_control.record_capacity_growth_observation(
            target_id=tid, physical_stored_bytes=100, observed_at="2026-01-01T00:00:00Z"
        )
        backup_control.record_capacity_growth_observation(
            target_id=tid, physical_stored_bytes=200, observed_at="2026-01-10T00:00:00Z"
        )
        h1 = backup_capacity.estimate_target_exhaustion_horizon(tid, "", probe=True, record_observation=True)
    assert h1.get("forecastStatus") in {"ok", "stable-or-shrinking", "unavailable"}
    # Pure read uses projection without probe
    with patch.object(backup_targets, "probe_target_capacity") as probe:
        h2 = backup_capacity.estimate_target_exhaustion_horizon(tid, "", probe=False, record_observation=False)
        probe.assert_not_called()
    assert h2.get("targetId") == tid


def test_lineage_rebuild_and_chain_from_graph(tmp_settings: Path) -> None:
    points = [
        {"backupId": "F0", "snapshotKind": "full", "parentBackupId": None, "committedAt": "t0"},
        {"backupId": "I1", "snapshotKind": "incremental", "parentBackupId": "F0", "committedAt": "t1"},
        {"backupId": "I2", "snapshotKind": "incremental", "parentBackupId": "I1", "committedAt": "t2"},
    ]
    with patch.object(backup_tiering.backup_dr_ledger, "list_recovery_points", return_value=points):
        written = backup_tiering.rebuild_recovery_lineage("pol")
    assert written["written"] == 3
    lin = backup_control.get_recovery_lineage("pol", "I2")
    assert lin is not None and lin["parentBackupId"] == "I1"
    # Chain builder uses lineage graph first
    unit = backup_tiering.build_recovery_chain_placement_unit("pol", "I2")
    assert unit["closureComplete"] is True
    assert unit["memberBackupIds"] == ["F0", "I1", "I2"]


def test_chain_migration_job_converges_with_per_member_sources(tmp_settings: Path) -> None:
    src = "target_cm_src"
    dst = "target_cm_dst"
    backup_targets.register_filesystem_target(src, path=tmp_settings / src, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(dst, path=tmp_settings / dst, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(dst, storage_tier="warm")

    unit = {
        "closureComplete": True,
        "memberBackupIds": ["F0", "I1"],
        "anchorBackupId": "I1",
        "baselineBackupId": "F0",
    }
    copies = [
        {"backupId": "F0", "targetId": src, "recoverable": True, "state": "healthy"},
        {"backupId": "I1", "targetId": src, "recoverable": True, "state": "healthy"},
    ]
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_tiering.backup_dr_ledger, "list_logical_recovery_copies", return_value=copies
    ):
        plan = backup_tiering.plan_chain_migration(
            "pol", "I1", desired_tier="warm", preferred_source_target_id=src
        )
    assert plan["status"] == "planned"
    mid = str(plan["migrationId"])
    assert len(plan["members"]) == 2
    assert all(m["sourceTargetId"] == src for m in plan["members"])

    with patch.object(
        backup_tiering,
        "execute_tier_migration",
        return_value={"status": "success", "intentPhase": "executed", "objectSetDigest": "d"},
    ), patch.object(
        backup_tiering.backup_replication,
        "authenticate_committed_copy",
        return_value=("authenticated", {}, {}),
    ), patch.object(
        backup_tiering.backup_publish,
        "resolve_target",
        return_value=SimpleNamespace(root=None, store=None),
    ), patch.object(
        backup_tiering,
        "chain_satisfies_tier",
        return_value=(True, "ok"),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[
            {"backupId": "F0", "targetId": dst, "recoverable": True, "state": "healthy"},
            {"backupId": "I1", "targetId": dst, "recoverable": True, "state": "healthy"},
        ],
    ):
        done = backup_tiering.execute_chain_migration(mid)
    assert done["phase"] == "converged"
    assert done.get("retirementEligible") is True


def test_chain_migration_never_marks_executed_on_failure(tmp_settings: Path) -> None:
    src = "target_cm_fail_s"
    dst = "target_cm_fail_d"
    backup_targets.register_filesystem_target(src, path=tmp_settings / src, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(dst, path=tmp_settings / dst, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(dst, storage_tier="archive")
    job = backup_control.create_chain_migration_job(
        {
            "policyId": "pol",
            "anchorBackupId": "I1",
            "desiredTier": "archive",
            "destTargetId": dst,
            "unit": {"memberBackupIds": ["I1"], "closureComplete": True},
            "members": [
                {"backupId": "I1", "sourceTargetId": src, "destTargetId": dst, "state": "planned", "noop": False}
            ],
            "phase": "planned",
        }
    )
    with patch.object(
        backup_tiering,
        "execute_tier_migration",
        return_value={"status": "failed", "error": "boom", "intentPhase": "failed-terminal"},
    ):
        result = backup_tiering.execute_chain_migration(str(job["migrationId"]))
    assert result["phase"] == "failed-terminal"
    assert result.get("error")


def test_process_pending_chain_migrations(tmp_settings: Path) -> None:
    job = backup_control.create_chain_migration_job(
        {
            "policyId": "pol",
            "anchorBackupId": "b",
            "desiredTier": "warm",
            "members": [],
            "phase": "planned",
            "unit": {"memberBackupIds": [], "closureComplete": True},
        }
    )
    with patch.object(
        backup_tiering,
        "execute_chain_migration",
        return_value={**job, "phase": "converged"},
    ):
        summary = backup_tiering.process_pending_chain_migrations(limit=5)
    assert summary["processed"] >= 1
    assert summary["converged"] >= 1
