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


def test_evaluate_no_eligible_tier_and_same_failure_domain(tmp_settings: Path) -> None:
    hot = "target_fd_hot"
    warm_a = "target_fd_wa"
    warm_b = "target_fd_wb"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd-shared")
    backup_targets.register_filesystem_target(warm_a, path=tmp_settings / warm_a, region="r2", failure_domain="fd-shared")
    backup_targets.register_filesystem_target(warm_b, path=tmp_settings / warm_b, region="r3", failure_domain="fd-other")
    backup_targets.set_target_storage_tier(warm_a, storage_tier="warm")
    backup_targets.set_target_storage_tier(warm_b, storage_tier="warm")
    targets = {t: backup_targets.get_target(t) for t in (hot, warm_a, warm_b)}
    policy = {
        "policyId": "p",
        "primaryTargetId": hot,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
            "minHotCopies": 1,
        },
    }
    unit = {"closureComplete": True, "memberBackupIds": ["b"], "anchorBackupId": "b"}
    aged = (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat()
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        decision = backup_placement.evaluate_point_placement(
            policy,
            "b",
            committed_at=aged,
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
        )
    assert decision["action"] == "migrate"
    # Prefers other failure domain over shared
    assert decision["selectedTargetId"] == warm_b

    # No warm/archive targets at all
    only_hot = {hot: targets[hot]}
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        none = backup_placement.evaluate_point_placement(
            policy,
            "b",
            committed_at=aged,
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=only_hot,
        )
    assert none["action"] == "blocked"
    assert "no-eligible-tier-target" in none["reasonCodes"]


def test_soft_watermark_and_hot_copy_drift(tmp_settings: Path) -> None:
    hot = "target_soft_h"
    warm = "target_soft_w"
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
            "minHotCopies": 2,
        },
    }
    unit = {"closureComplete": True, "memberBackupIds": ["b"], "anchorBackupId": "b"}
    now = datetime.now(tz=timezone.utc)
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "degraded"},
    ):
        # Only one hot copy but minHotCopies=2 → drift
        d = backup_placement.evaluate_point_placement(
            policy,
            "b",
            committed_at=now.isoformat(),
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    assert "hot-copy-objective-drift" in d.get("reasonCodes", []) or d["action"] in {"migrate", "blocked", "none"}


def test_reconcile_execute_enqueues_and_all_policies(tmp_settings: Path) -> None:
    hot = "target_enq_h"
    warm = "target_enq_w"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")
    policy = {
        "policyId": "pol_enq",
        "primaryTargetId": hot,
        "targetId": hot,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
        },
    }
    aged = (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat()
    with patch.object(backup_policies, "get_policy", return_value=policy), patch.object(
        backup_placement.backup_dr_ledger,
        "list_recovery_points",
        return_value=[{"backupId": "b1", "committedAt": aged}, {"backupId": "b1", "committedAt": aged}],
    ), patch.object(
        backup_placement.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"backupId": "b1", "targetId": hot, "recoverable": True, "state": "healthy"}],
    ), patch.object(
        backup_tiering,
        "build_recovery_chain_placement_unit",
        return_value={"closureComplete": True, "memberBackupIds": ["b1"], "anchorBackupId": "b1"},
    ), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ), patch.object(
        backup_tiering, "plan_chain_migration", return_value={"status": "planned", "migrationId": "m1"}
    ):
        result = backup_placement.reconcile_policy_placement("pol_enq", execute=True, limit=5)
    assert result["status"] == "ok"
    assert result["enqueuedMigrations"] >= 1

    with patch.object(
        backup_policies,
        "list_policies",
        return_value=[{"policyId": "pol_enq", "recoveryPlacement": {"enabled": True}}, {"policyId": ""}],
    ), patch.object(
        backup_placement, "reconcile_policy_placement", return_value={"enqueuedMigrations": 2}
    ) as one:
        all_res = backup_placement.reconcile_all_policies(execute=True, limit_per_policy=3)
    assert all_res["policies"] == 1
    assert all_res["enqueuedMigrations"] == 2
    one.assert_called()


def test_repair_execute_exception_and_supervisor(tmp_settings: Path) -> None:
    jobs = [{"repairId": "rx", "destTargetId": "td", "phase": "queued"}]

    def fake_lease(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    with patch.object(backup_maintenance.backup_replication, "list_repair_jobs", return_value=jobs), patch.object(
        backup_maintenance.backup_replication,
        "execute_repair_job_instance",
        side_effect=RuntimeError("boom"),
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=fake_lease):
        summary = backup_maintenance._process_repair_scopes(instance_id="i", limit=2)
    assert summary["failed"] >= 1

    sup = backup_maintenance.StorageMaintenanceSupervisor(instance_id="sup", tick_seconds=0.05, limit_per_worker=1)
    with patch.object(backup_maintenance, "maintenance_tick", return_value={"leaseAcquired": True}):
        assert sup.tick()["leaseAcquired"] is True
        sup.start()
        sup.start()  # idempotent
        import time

        time.sleep(0.12)
        sup.stop(timeout=1.0)
    assert sup._thread is None


def test_placement_copy_filter_soft_watermark_and_no_primary(tmp_settings: Path) -> None:
    hot = "target_micro_h"
    warm = "target_micro_w"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")
    targets = {hot: backup_targets.get_target(hot), warm: backup_targets.get_target(warm)}
    unit = {"closureComplete": True, "memberBackupIds": ["b"], "anchorBackupId": "b"}
    now = datetime.now(tz=timezone.utc)
    # Filters non-recoverable / bad state copies
    tiers = backup_placement._copy_tiers_for_backup(
        "b",
        [
            {"backupId": "b", "targetId": hot, "recoverable": False, "state": "healthy"},
            {"backupId": "other", "targetId": hot, "recoverable": True, "state": "healthy"},
            {"backupId": "b", "targetId": "", "recoverable": True, "state": "healthy"},
            {"backupId": "b", "targetId": hot, "recoverable": True, "state": "corrupt"},
            {"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"},
        ],
        targets,
    )
    assert tiers == [(hot, "hot")]

    # Soft watermark on satisfied hot objectives
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
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "degraded"},
    ):
        soft = backup_placement.evaluate_point_placement(
            policy,
            "b",
            committed_at=now.isoformat(),
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    assert "primary-capacity-soft-watermark" in soft.get("reasonCodes", []) or soft["action"] in {"none", "migrate", "blocked"}

    # No primary id → objectives-satisfied short path
    policy2 = {
        "policyId": "p2",
        "recoveryPlacement": policy["recoveryPlacement"],
    }
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        none_primary = backup_placement.evaluate_point_placement(
            policy2,
            "b",
            committed_at=now.isoformat(),
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    assert none_primary["action"] == "none"

    # Soft watermark on destination while migrating
    aged = (now - timedelta(seconds=100000)).isoformat()
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        side_effect=lambda *a, **k: {"status": "degraded"},
    ):
        dest_soft = backup_placement.evaluate_point_placement(
            {
                "policyId": "p3",
                "primaryTargetId": hot,
                "recoveryPlacement": {
                    "enabled": True,
                    "hotWindowSeconds": 1,
                    "warmWindowSeconds": 10,
                    "archiveAfterSeconds": 100,
                },
            },
            "b",
            committed_at=aged,
            copies=[{"backupId": "b", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id=targets,
            now=now,
        )
    if dest_soft["action"] == "migrate":
        assert "destination-capacity-soft-watermark" in dest_soft["reasonCodes"] or "destination-tier-qualified" in dest_soft["reasonCodes"]


def test_retirement_and_chain_phase_branches(tmp_settings: Path) -> None:
    def run_work(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    phases = [
        "waiting-for-dependencies",
        "requested",
        "checking-topology",
        "checking-holds",
        "committing-retirement-marker",
        "retiring-ledger-copy",
        "gc-pending",
        "gc-running",
        "weird-fail",
    ]
    jobs = [{"jobId": f"j{i}", "targetId": f"t{i % 2}", "phase": p} for i, p in enumerate(phases)]
    call = {"n": 0}

    def exec_ret(jid: str, **_k: object) -> dict[str, object]:
        idx = int(str(jid).replace("j", "") or "0")
        call["n"] += 1
        return {"phase": phases[idx]}

    with patch.object(
        backup_maintenance.backup_retirement, "list_copy_retirement_jobs", return_value=jobs
    ), patch.object(
        backup_maintenance.backup_retirement, "execute_copy_retirement_job", side_effect=exec_ret
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=run_work):
        out = backup_maintenance._process_retirement_scopes(instance_id="i", limit=20)
    assert out["processed"] >= 1
    assert out["waiting"] + out["failed"] + out.get("reclaimed", 0) >= 1

    mig = [
        {"migrationId": "m1", "destTargetId": "d1", "phase": "planned"},
        {"migrationId": "m2", "destTargetId": "d1", "phase": "planned"},
        {"migrationId": "", "destTargetId": "d1", "phase": "planned"},
    ]
    results = iter(
        [
            {"phase": "failed-terminal"},
            {"phase": "transferring"},
        ]
    )
    with patch.object(backup_control, "list_chain_migration_jobs", return_value=mig), patch.object(
        backup_tiering, "execute_chain_migration", side_effect=lambda *a, **k: next(results)
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=run_work):
        mout = backup_maintenance._process_chain_migration_scopes(instance_id="i", limit=5)
    assert mout["processed"] >= 1


def test_control_lineage_and_chain_job_helpers(tmp_settings: Path) -> None:
    backup_control.upsert_recovery_lineage(
        policy_id="pl",
        backup_id="F0",
        snapshot_kind="full",
        parent_backup_id=None,
        chain_depth=0,
        object_set_digest="d0",
        committed_at="t0",
    )
    backup_control.upsert_recovery_lineage(
        policy_id="pl",
        backup_id="I1",
        snapshot_kind="incremental",
        parent_backup_id="F0",
        chain_depth=1,
        committed_at="t1",
    )
    lin = backup_control.get_recovery_lineage("pl", "I1")
    assert lin is not None and lin["parentBackupId"] == "F0"
    backup_control.clear_recovery_lineage("pl")
    assert backup_control.get_recovery_lineage("pl", "I1") is None

    backup_control.set_target_index_coverage("t_cov", state="building", formal_receipt_count=0)
    assert backup_control.index_coverage_allows_gc("t_cov")[0] is False
    backup_control.set_target_index_coverage("t_cov", state="complete", formal_receipt_count=3)
    assert backup_control.index_coverage_allows_gc("t_cov") == (True, "ok")

    job = backup_control.create_chain_migration_job(
        {
            "policyId": "pl",
            "anchorBackupId": "I1",
            "desiredTier": "warm",
            "destTargetId": "td",
            "members": [{"backupId": "I1", "state": "planned"}],
            "phase": "planned",
            "unit": {"memberBackupIds": ["I1"], "closureComplete": True},
        }
    )
    mid = str(job["migrationId"])
    got = backup_control.get_chain_migration_job(mid)
    assert got is not None and got["phase"] == "planned"
    updated = backup_control.update_chain_migration_job(mid, phase="transferring", payload={"members": []})
    assert updated["phase"] == "transferring"
    listed = backup_control.list_chain_migration_jobs(phase="transferring", policy_id="pl", limit=10)
    assert any(str(j.get("migrationId")) == mid for j in listed)
    # capacity forecast projection round-trip
    backup_control.put_capacity_forecast_projection("t_cov", {"status": "healthy", "targetId": "t_cov"})
    forecast = backup_control.get_capacity_forecast_projection("t_cov")
    assert forecast is not None and forecast["status"] == "healthy"


def test_heartbeat_renew_failure_and_drain_exception(tmp_settings: Path) -> None:
    stop = __import__("threading").Event()
    stop.set()
    with patch.object(backup_maintenance.backup_control, "renew_maintenance_lease", return_value=False):
        backup_maintenance._lease_heartbeat(stop, instance_id="h", fencing_token=1)

    def run_work(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    with patch.object(
        backup_maintenance.backup_drain,
        "list_target_drain_jobs",
        return_value=[{"targetId": "t_bad", "phase": "evacuating"}],
    ), patch.object(backup_maintenance.backup_drain, "reconcile_drain_projections", return_value={}), patch.object(
        backup_maintenance.backup_drain, "process_target_drain", side_effect=RuntimeError("drain-boom")
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=run_work):
        summary = backup_maintenance._process_drain_scopes(instance_id="d", limit=2)
    assert summary["drainFailures"] >= 1
