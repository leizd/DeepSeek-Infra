"""Gates E/F — autonomous placement SLO controller + target-sharded maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_maintenance,
    backup_placement,
    backup_policies,
    backup_targets,
    backup_tiering,
)


def test_desired_tier_for_age_windows() -> None:
    placement = {
        "hotWindowSeconds": 100,
        "warmWindowSeconds": 1000,
        "archiveAfterSeconds": 5000,
    }
    assert backup_placement.desired_tier_for_age(0, placement) == "hot"
    assert backup_placement.desired_tier_for_age(99, placement) == "hot"
    assert backup_placement.desired_tier_for_age(100, placement) == "warm"
    assert backup_placement.desired_tier_for_age(999, placement) == "warm"
    assert backup_placement.desired_tier_for_age(5000, placement) == "archive"


def test_normalize_recovery_placement_defaults() -> None:
    disabled = backup_placement.normalize_recovery_placement(None)
    assert disabled["enabled"] is False
    enabled = backup_placement.normalize_recovery_placement({"enabled": True, "hotWindowSeconds": 60})
    assert enabled["enabled"] is True
    assert enabled["hotWindowSeconds"] == 60


def test_policy_includes_recovery_placement(tmp_settings: Path) -> None:
    policy = backup_policies.normalize_policy(
        {
            "name": "place-pol",
            "enabled": True,
            "recoveryPlacement": {
                "enabled": True,
                "hotWindowSeconds": 3600,
                "warmWindowSeconds": 86400,
                "archiveAfterSeconds": 604800,
            },
        }
    )
    assert policy["recoveryPlacement"]["enabled"] is True
    assert policy["recoveryPlacement"]["hotWindowSeconds"] == 3600


def test_evaluate_blocks_on_incomplete_lineage(tmp_settings: Path) -> None:
    hot = "target_e_hot"
    warm = "target_e_warm"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")

    policy = {
        "policyId": "pol_e",
        "primaryTargetId": hot,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
            "minHotCopies": 1,
        },
    }
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat()
    with patch.object(
        backup_tiering,
        "build_recovery_chain_placement_unit",
        return_value={"closureComplete": False, "reason": "missing-parent", "missingBackupId": "F0"},
    ):
        decision = backup_placement.evaluate_point_placement(
            policy,
            "I1",
            committed_at=old,
            copies=[{"backupId": "I1", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id={
                hot: backup_targets.get_target(hot),
                warm: backup_targets.get_target(warm),
            },
        )
    assert decision["action"] == "blocked"
    assert "lineage-incomplete" in decision["reasonCodes"]


def test_evaluate_migrate_on_tier_drift(tmp_settings: Path) -> None:
    hot = "target_e_hot2"
    warm = "target_e_warm2"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")

    policy = {
        "policyId": "pol_e2",
        "primaryTargetId": hot,
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
            "minHotCopies": 1,
        },
    }
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat()
    unit = {
        "closureComplete": True,
        "memberBackupIds": ["F0", "I1"],
        "anchorBackupId": "I1",
        "baselineBackupId": "F0",
    }
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        decision = backup_placement.evaluate_point_placement(
            policy,
            "I1",
            committed_at=old,
            copies=[
                {"backupId": "I1", "targetId": hot, "recoverable": True, "state": "healthy"},
                {"backupId": "F0", "targetId": hot, "recoverable": True, "state": "healthy"},
            ],
            targets_by_id={
                hot: backup_targets.get_target(hot),
                warm: backup_targets.get_target(warm),
            },
        )
    assert decision["action"] == "migrate"
    assert decision["desiredTier"] == "warm"
    assert decision["selectedTargetId"] == warm
    assert decision["correctnessOrder"][-1] == "cost"
    assert decision["correctnessOrder"][0] == "recoverability"


def test_cost_never_selects_unknown_rate_when_required(tmp_settings: Path) -> None:
    hot = "target_e_hot3"
    warm = "target_e_warm3"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")
    # Explicitly ensure no storage cost rate
    warm_t = backup_targets.get_target(warm)
    warm_t = {**warm_t, "storageCostPerGiBMonth": None}

    policy = {
        "policyId": "pol_e3",
        "primaryTargetId": hot,
        "costObjectives": {"requireKnownRates": True},
        "recoveryPlacement": {
            "enabled": True,
            "hotWindowSeconds": 1,
            "warmWindowSeconds": 10,
            "archiveAfterSeconds": 100,
        },
    }
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat()
    unit = {"closureComplete": True, "memberBackupIds": ["I1"], "anchorBackupId": "I1"}
    with patch.object(backup_tiering, "build_recovery_chain_placement_unit", return_value=unit), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ):
        decision = backup_placement.evaluate_point_placement(
            policy,
            "I1",
            committed_at=old,
            copies=[{"backupId": "I1", "targetId": hot, "recoverable": True, "state": "healthy"}],
            targets_by_id={hot: backup_targets.get_target(hot), warm: warm_t},
        )
    assert decision["action"] == "blocked"
    assert "all-candidates-rejected" in decision["reasonCodes"]
    assert decision["rejectedTargets"].get(warm) == "cost-rate-unavailable"


def test_reconcile_enqueues_chain_migration(tmp_settings: Path) -> None:
    hot = "target_e_hot4"
    warm = "target_e_warm4"
    backup_targets.register_filesystem_target(hot, path=tmp_settings / hot, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(warm, path=tmp_settings / warm, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(warm, storage_tier="warm")

    policy = backup_policies.normalize_policy(
        {
            "name": "recon",
            "enabled": True,
            "primaryTargetId": hot,
            "targetId": hot,
            "recoveryPlacement": {
                "enabled": True,
                "hotWindowSeconds": 1,
                "warmWindowSeconds": 10,
                "archiveAfterSeconds": 100,
            },
        }
    )
    # Persist policy via control plane if available
    with patch.object(backup_policies, "get_policy", return_value=policy), patch.object(
        backup_placement.backup_dr_ledger,
        "list_recovery_points",
        return_value=[
            {
                "backupId": "I1",
                "committedAt": (datetime.now(tz=timezone.utc) - timedelta(seconds=50)).isoformat(),
            }
        ],
    ), patch.object(
        backup_placement.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"backupId": "I1", "targetId": hot, "recoverable": True, "state": "healthy"}],
    ), patch.object(
        backup_tiering,
        "build_recovery_chain_placement_unit",
        return_value={"closureComplete": True, "memberBackupIds": ["I1"], "anchorBackupId": "I1"},
    ), patch.object(
        backup_placement.backup_capacity,
        "estimate_target_exhaustion_horizon",
        return_value={"status": "healthy"},
    ), patch.object(
        backup_tiering,
        "plan_chain_migration",
        return_value={"status": "planned", "migrationId": "mig_test"},
    ) as plan:
        result = backup_placement.reconcile_policy_placement(str(policy["policyId"]), execute=True)
    assert result["status"] == "ok"
    assert result["enqueuedMigrations"] == 1
    plan.assert_called_once()
    assert result["decisions"][0]["action"] == "migrate"


def test_repair_scopes_shard_by_dest_target(tmp_settings: Path) -> None:
    jobs = [
        {"repairId": "r1", "destTargetId": "t_a", "phase": "queued"},
        {"repairId": "r2", "destTargetId": "t_b", "phase": "queued"},
        {"repairId": "r3", "destTargetId": "t_a", "phase": "queued"},
    ]
    seen_scopes: list[str] = []

    def fake_lease(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        seen_scopes.append(f"{worker_kind}:{scope_id}")
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    with patch.object(backup_maintenance.backup_replication, "list_repair_jobs", return_value=jobs), patch.object(
        backup_maintenance.backup_replication,
        "execute_repair_job_instance",
        return_value={"status": "success"},
    ) as exec_fn, patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=fake_lease):
        summary = backup_maintenance._process_repair_scopes(instance_id="i1", limit=10)

    assert summary["shardedBy"] == "destTargetId"
    assert summary["scopes"] == 2
    assert summary["processed"] == 3
    assert exec_fn.call_count == 3
    assert "repair:t_a" in seen_scopes
    assert "repair:t_b" in seen_scopes


def test_retirement_scopes_shard_by_target(tmp_settings: Path) -> None:
    jobs = [
        {"jobId": "j1", "targetId": "t_x", "phase": "requested"},
        {"jobId": "j2", "targetId": "t_y", "phase": "gc-pending"},
    ]
    seen: list[str] = []

    def fake_lease(worker_kind: str, scope_id: str, **kwargs: object) -> tuple[bool, object]:
        seen.append(f"{worker_kind}:{scope_id}")
        work = kwargs["work"]
        assert callable(work)
        return True, work()

    with patch.object(
        backup_maintenance.backup_retirement, "list_copy_retirement_jobs", return_value=jobs
    ), patch.object(
        backup_maintenance.backup_retirement,
        "execute_copy_retirement_job",
        return_value={"phase": "reclaimed"},
    ), patch.object(backup_maintenance, "_run_with_scope_lease", side_effect=fake_lease):
        summary = backup_maintenance._process_retirement_scopes(instance_id="i1", limit=5)

    assert summary["shardedBy"] == "targetId"
    assert summary["scopes"] == 2
    assert "retirement:t_x" in seen
    assert "retirement:t_y" in seen


def test_maintenance_tick_reports_target_sharding(tmp_settings: Path) -> None:
    with patch.object(backup_control, "acquire_maintenance_lease", return_value={
        "workerKind": "storage-maintenance-planner",
        "fencingToken": 1,
    }), patch.object(backup_control, "release_maintenance_lease", return_value=True), patch.object(
        backup_control, "renew_maintenance_lease", return_value=True
    ), patch.object(
        backup_maintenance.backup_recovery_keeper, "reconcile_durable_recovery_leases", return_value={}
    ), patch.object(
        backup_maintenance, "_run_with_scope_lease", return_value=(True, {"processed": 0})
    ), patch.object(
        backup_maintenance, "_process_repair_scopes", return_value={"processed": 0, "shardedBy": "destTargetId"}
    ), patch.object(
        backup_maintenance, "_process_rebalance_scopes", return_value={"processed": 0, "shardedBy": "destTargetId"}
    ), patch.object(
        backup_maintenance, "_process_retirement_scopes", return_value={"processed": 0, "shardedBy": "targetId"}
    ), patch.object(
        backup_maintenance, "_process_chain_migration_scopes", return_value={"processed": 0, "shardedBy": "destTargetId"}
    ), patch.object(
        backup_maintenance, "_probe_capacity_page", return_value=0
    ), patch.object(
        backup_maintenance, "_process_drain_scopes", return_value={"drainsProcessed": 0, "drainFailures": 0, "drainLeaseSkips": 0}
    ), patch.object(
        backup_maintenance.backup_placement, "reconcile_all_policies", return_value={"policies": 0}
    ), patch.object(
        backup_maintenance.backup_transfer_budget,
        "get_global_transfer_budget_manager",
        return_value=type("M", (), {"transfer_control_summary": staticmethod(lambda: {})})(),
    ):
        summary = backup_maintenance.maintenance_tick(instance_id="tick-test", limit_per_worker=2)

    assert summary["leaseAcquired"] is True
    assert summary["shardedScopes"] is True
    assert summary["shardedByTarget"] is True
    assert "placement" in summary
