"""Indexed Lifecycle Economics & SLO-Aware Storage Tiering contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_drain,
    backup_object_index,
    backup_retirement,
    backup_targets,
    backup_tiering,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_control_schema_versioned_and_migrations_recorded(tmp_settings: Path) -> None:
    assert backup_control.schema_version() == backup_control.CONTROL_SCHEMA_VERSION
    migrations = backup_control.list_schema_migrations()
    assert migrations
    assert migrations[-1]["version"] == backup_control.CONTROL_SCHEMA_VERSION
    checkpoint = backup_control.create_control_checkpoint(tmp_settings / "ctrl-ckpt.sqlite3")
    assert checkpoint.is_file()


def test_lifecycle_intent_survives_topology_before_drain_job(tmp_settings: Path) -> None:
    target_id = "target_drain_journal"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / target_id)
    drain_id = "drain_forced_half"
    started = backup_control.begin_target_drain_intent(
        target_id,
        reason="crash-between-topology-and-job",
        drain_id=drain_id,
    )
    assert started["target"]["drainState"] == "draining"
    assert backup_drain.get_target_drain_job(target_id=target_id) is None

    summary = backup_drain.reconcile_drain_projections()
    assert summary["recreated"] >= 1
    job = backup_drain.get_target_drain_job(target_id=target_id)
    assert job is not None
    assert job["phase"] == "draining"
    assert job["drainId"] == drain_id


def test_start_target_drain_journals_intent_and_projects_job(tmp_settings: Path) -> None:
    target_id = "target_drain_full"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / target_id)
    job = backup_drain.start_target_drain(target_id, reason="operator-drain")
    assert job["phase"] == "draining"
    target = backup_targets.get_target(target_id)
    assert target["drainState"] == "draining"
    intents = backup_control.list_lifecycle_intents(kind="drain", target_id=target_id)
    assert intents
    assert intents[0]["phase"] in {"job-projected", "topology-committed"}


def test_retirement_dependency_never_fails_open_at_page_limit(tmp_settings: Path) -> None:
    target_id = "t-dep"
    policy_id = "p-dep"
    backup_id = "b-dep"

    class _Page:
        def __init__(self, n: int) -> None:
            self._n = n

        def __iter__(self):
            for i in range(self._n):
                yield {"phase": "complete", "primaryTargetId": "other", "replicaTargetId": "other2", "backupId": backup_id}

        def __len__(self) -> int:
            return self._n

    with (
        patch(
            "deepseek_infra.infra.workspace.backup_replication.list_jobs",
            return_value=list(_Page(500)),
        ),
        patch(
            "deepseek_infra.infra.workspace.backup_replication.list_repair_jobs",
            return_value=[],
        ),
        patch(
            "deepseek_infra.infra.workspace.backup_replication.list_rebalance_jobs",
            return_value=[],
        ),
        patch.object(backup_retirement, "list_copy_retirement_jobs", return_value=[]),
    ):
        assert backup_retirement.has_active_copy_dependency(target_id, policy_id, backup_id) is True


def test_reference_index_shared_ciphertext_and_retirement(tmp_settings: Path) -> None:
    target_id = "target_idx"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / target_id)
    receipt_a = {
        "policyId": "pol",
        "backupId": "bak-a",
        "objectDigest": "aa" * 32,
        "objects": [{"path": "ciphertext/shared.age", "digest": "bb" * 32, "size": 100}],
    }
    receipt_b = {
        "policyId": "pol",
        "backupId": "bak-b",
        "objectDigest": "cc" * 32,
        "objects": [{"path": "ciphertext/shared.age", "digest": "bb" * 32, "size": 100}],
    }
    backup_object_index.index_receipt_objects(
        target_id=target_id, policy_id="pol", backup_id="bak-a", receipt=receipt_a, ref_state="live"
    )
    backup_object_index.index_receipt_objects(
        target_id=target_id, policy_id="pol", backup_id="bak-b", receipt=receipt_b, ref_state="live"
    )
    obj = backup_control.get_target_object(target_id, "ciphertext/shared.age")
    assert obj is not None
    assert obj["liveRefCount"] >= 2

    backup_object_index.apply_retirement_to_index(
        target_id=target_id, policy_id="pol", backup_id="bak-a", receipt=receipt_a
    )
    # SQL-native live-ref check: bak-b still holds the shared object
    assert backup_object_index.object_is_live_referenced(
        target_id, "ciphertext/shared.age", excluding_backup_id="bak-a"
    )
    # Canonical key also remains live
    canon = backup_object_index.canonical_object_key("bb" * 32)
    assert backup_object_index.object_is_live_referenced(target_id, canon, excluding_backup_id="bak-a")

    backup_object_index.apply_retirement_to_index(
        target_id=target_id, policy_id="pol", backup_id="bak-b", receipt=receipt_b
    )
    obj2 = backup_control.get_target_object(target_id, "ciphertext/shared.age")
    assert obj2 is not None
    assert obj2["liveRefCount"] == 0
    assert obj2["state"] in {"retired-pending-gc", "gc-candidate"}
    candidates = backup_object_index.gc_candidate_keys(target_id)
    assert "ciphertext/shared.age" in candidates or canon in candidates

def test_s3_quota_uses_physical_object_bytes(tmp_settings: Path) -> None:
    target_id = "target_quota"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / target_id)
    # Force quota path by mutating target kind/quota.
    def _quota_target(t: dict[str, Any]) -> dict[str, Any]:
        updated = {**t, "kind": "s3", "quotaBytes": 1000}
        updated.pop("path", None)
        return updated

    backup_control.mutate_target(target_id, expected_generation=None, mutate=_quota_target)
    # Keep JSON projection in sync for get_target readers.
    authoritative = backup_control.get_target(target_id)
    assert authoritative is not None
    backup_targets._project_target(authoritative)
    backup_control.put_recovery_object_ref(
        target_id=target_id,
        policy_id="p",
        backup_id="b1",
        object_key="obj/a",
        ref_state="live",
        size_bytes=400,
    )
    backup_control.put_recovery_object_ref(
        target_id=target_id,
        policy_id="p",
        backup_id="b2",
        object_key="obj/a",
        ref_state="live",
        size_bytes=400,
    )
    backup_control.put_recovery_object_ref(
        target_id=target_id,
        policy_id="p",
        backup_id="b3",
        object_key="obj/b",
        ref_state="retired",
        size_bytes=100,
    )
    cap = backup_targets.probe_target_capacity(target_id)
    assert cap["physicalStoredBytes"] == 500
    assert cap["usedBytes"] == 500
    assert cap["retiredPendingGcBytes"] == 100
    assert cap["freeBytes"] == 500
    assert cap["source"] == "physical-object-index"


def test_capacity_forecast_uses_elapsed_time(tmp_settings: Path) -> None:
    target_id = "t-growth"
    now = datetime.now(tz=timezone.utc)
    backup_control.record_capacity_growth_observation(
        target_id=target_id,
        physical_stored_bytes=100 * 1024**3,
        observed_at=_iso(now - timedelta(days=10)),
    )
    backup_control.record_capacity_growth_observation(
        target_id=target_id,
        physical_stored_bytes=200 * 1024**3,
        observed_at=_iso(now),
    )
    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={
            "freeBytes": 300 * 1024**3,
            "totalBytes": 500 * 1024**3,
            "freePercent": 60.0,
            "physicalStoredBytes": 200 * 1024**3,
            "observedAt": _iso(now),
        },
    ):
        horizon = backup_capacity.estimate_target_exhaustion_horizon(target_id, "policy")
    assert horizon["forecastStatus"] == "ok"
    assert horizon["confidence"] in {"low", "medium", "high"}
    # ~10 GiB/day over 10 days from 100->200 GiB; 300 GiB free => ~30 days
    assert horizon["estimatedDaysToFull"] is not None
    assert 20 <= int(horizon["estimatedDaysToFull"]) <= 40
    assert horizon["bytesPerDayP50"] is not None


def test_hourly_backups_do_not_look_like_daily_backups(tmp_settings: Path) -> None:
    target_id = "t-rate"
    now = datetime.now(tz=timezone.utc)
    # 24 hourly points adding 1 GiB each hour => ~24 GiB/day, not 1 GiB/day.
    for hour in range(25):
        backup_control.record_capacity_growth_observation(
            target_id=target_id,
            physical_stored_bytes=hour * 1024**3,
            observed_at=_iso(now - timedelta(hours=24 - hour)),
        )
    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={"freeBytes": 100 * 1024**3, "totalBytes": 200 * 1024**3, "freePercent": 50.0},
    ):
        horizon = backup_capacity.estimate_target_exhaustion_horizon(target_id, "policy")
    assert horizon["forecastStatus"] == "ok"
    assert int(horizon["bytesPerDayP50"] or 0) >= 20 * 1024**3


def test_unknown_cost_rate_never_uses_implicit_price(tmp_settings: Path) -> None:
    backup_targets.register_filesystem_target(
        "target_cost_src",
        path=tmp_settings / "target_cost_src",
        region="r1",
    )
    backup_targets.register_filesystem_target(
        "target_cost_dst",
        path=tmp_settings / "target_cost_dst",
        region="r2",
    )
    cost = backup_capacity.estimate_transfer_cost(
        1024**3,
        source_target_id="target_cost_src",
        dest_target_id="target_cost_dst",
    )
    assert cost["costStatus"] == "unavailable"
    assert cost["estimatedOneTimeTransferCost"] is None
    assert cost["estimatedMonthlyStorageCostDelta"] is None
    assert cost["rateSource"] == "missing-operator-rates"


def test_operator_cost_rates_have_provenance(tmp_settings: Path) -> None:
    backup_targets.register_filesystem_target(
        "target_cost_src2",
        path=tmp_settings / "target_cost_src2",
        region="r1",
        egress_cost_per_gib=0.02,
    )
    backup_targets.register_filesystem_target(
        "target_cost_dst2",
        path=tmp_settings / "target_cost_dst2",
        region="r2",
        storage_cost_per_gib_month=0.01,
    )
    cost = backup_capacity.estimate_transfer_cost(
        1024**3,
        source_target_id="target_cost_src2",
        dest_target_id="target_cost_dst2",
    )
    assert cost["costStatus"] == "ok"
    assert cost["estimatedOneTimeTransferCost"] == 0.02
    assert cost["estimatedMonthlyStorageCostDelta"] == 0.01
    assert cost["rateSource"] == "operator-configured"


def test_recovery_chain_placement_unit_and_hot_archive_guard(tmp_settings: Path) -> None:
    unit = backup_tiering.build_recovery_chain_placement_unit("pol", "I3")
    assert unit["anchorBackupId"] == "I3"
    assert "I3" in unit["memberBackupIds"]

    targets = {
        "hot": {"targetId": "hot", "storageTier": "hot"},
        "archive": {"targetId": "archive", "storageTier": "archive"},
    }
    copies = {
        "F0": [{"targetId": "archive", "recoverable": True, "state": "healthy"}],
        "I1": [{"targetId": "archive", "recoverable": True, "state": "healthy"}],
        "I2": [{"targetId": "archive", "recoverable": True, "state": "healthy"}],
        "I3": [{"targetId": "hot", "recoverable": True, "state": "healthy"}],
    }
    unit_full = {
        "memberBackupIds": ["F0", "I1", "I2", "I3"],
        "anchorBackupId": "I3",
    }
    ok, reason = backup_tiering.chain_satisfies_tier(
        unit_full,
        required_tier="hot",
        copies_by_backup=copies,
        targets_by_id=targets,
    )
    assert ok is False
    assert reason.startswith("ancestor-tier-too-cold")


def test_tier_metadata_on_targets(tmp_settings: Path) -> None:
    target_id = "target_tier_meta"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / target_id)
    updated = backup_targets.set_target_storage_tier(
        target_id,
        storage_tier="archive",
        restore_latency_class="hours",
        min_residence_seconds=30 * 86400,
        retrieval_cost_per_gib=0.03,
    )
    assert updated["storageTier"] == "archive"
    assert updated["restoreLatencyClass"] == "hours"
    assert updated["minResidenceSeconds"] == 30 * 86400
    assert updated["retrievalCostPerGiB"] == 0.03


def test_tier_plan_journals_lifecycle_intent(tmp_settings: Path) -> None:
    src = "target_tier_src"
    dst = "target_tier_dst"
    backup_targets.register_filesystem_target(src, path=tmp_settings / src, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(dst, path=tmp_settings / dst, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(dst, storage_tier="warm")
    with patch.object(
        backup_tiering.backup_scheduler,
        "plan_target_placement",
        return_value=[((0, 1, 1, 1.0, 1, 0.0, 0, 0, dst), dst)],
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[
            {
                "backupId": "bak1",
                "targetId": src,
                "recoverable": True,
                "state": "healthy",
                "policyId": "pol",
            }
        ],
    ), patch.object(
        backup_tiering.backup_policies,
        "get_policy",
        return_value={"policyId": "pol", "targetId": src},
    ), patch.object(
        backup_tiering,
        "build_recovery_chain_placement_unit",
        return_value={
            "closureComplete": True,
            "memberBackupIds": ["bak1"],
            "anchorBackupId": "bak1",
            "baselineBackupId": "bak1",
        },
    ), patch.object(
        backup_tiering,
        "chain_satisfies_tier",
        return_value=(True, "ok"),
    ):
        plan = backup_tiering.plan_tier_placement(
            "pol",
            "bak1",
            desired_tier="warm",
            source_target_id=src,
            candidate_target_ids=[dst],
        )
    assert plan["status"] == "planned"
    assert plan["destTargetId"] == dst
    assert plan.get("intentId")
    intent = backup_control.get_lifecycle_intent(str(plan["intentId"]))
    assert intent is not None
    assert intent["kind"] == "tier-migration"


def test_maintenance_scopes_progress_independently(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_maintenance

    # Hold the archive drain scope; planner + primary repair scope must still run.
    held = backup_control.acquire_maintenance_lease(
        "drain", "archive-target", owner_instance_id="other-worker", lease_seconds=120
    )
    assert held is not None
    with (
        patch.object(backup_maintenance.backup_recovery_keeper, "reconcile_durable_recovery_leases", return_value={"ok": True}),
        patch.object(backup_maintenance.backup_replication, "process_pending_jobs", return_value={"n": 1}) as repl,
        patch.object(backup_maintenance.backup_replication, "process_pending_repairs", return_value={"n": 1}),
        patch.object(backup_maintenance.backup_replication, "process_pending_rebalances", return_value={}),
        patch.object(backup_maintenance.backup_retirement, "process_pending_retirements", return_value={}),
        patch.object(backup_maintenance, "_probe_capacity_page", return_value=0),
        patch.object(
            backup_maintenance.backup_transfer_budget.get_global_transfer_budget_manager(),
            "transfer_control_summary",
            return_value={},
        ),
        patch.object(
            backup_maintenance.backup_drain,
            "list_target_drain_jobs",
            return_value=[{"targetId": "archive-target", "phase": "evacuating"}],
        ),
        patch.object(backup_maintenance.backup_drain, "reconcile_drain_projections", return_value={"recreated": 0, "fenced": 0}),
        patch.object(backup_maintenance.backup_drain, "process_target_drain") as drain_proc,
    ):
        summary = backup_maintenance.maintenance_tick(instance_id="primary-worker", limit_per_worker=5)
    assert summary["leaseAcquired"] is True
    assert summary.get("shardedScopes") is True
    repl.assert_called()
    # archive drain lease held elsewhere => skip, not block planner
    assert summary.get("drainLeaseSkips", 0) >= 1
    drain_proc.assert_not_called()


def test_retained_payload_keys_prefers_index(tmp_settings: Path) -> None:
    target_id = "target_ret_idx"
    root = tmp_settings / target_id
    root.mkdir(parents=True)
    backup_targets.register_filesystem_target(target_id, path=root)
    backup_object_index.index_receipt_objects(
        target_id=target_id,
        policy_id="p",
        backup_id="keep",
        receipt={
            "policyId": "p",
            "backupId": "keep",
            "objects": [{"path": "payload/keep.age", "size": 10}],
        },
        ref_state="live",
    )
    target = type("T", (), {"target_id": target_id, "root": root, "store": None})()
    assert backup_retirement._payload_key_is_retained(target, "payload/keep.age", retiring_backup_id="drop")
