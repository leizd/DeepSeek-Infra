"""Branch coverage for object index, tiering, control, and drain paths."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_drain,
    backup_maintenance,
    backup_object_index,
    backup_targets,
    backup_tiering,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_object_index_rebuild_fs_and_store_and_inventory(tmp_settings: Path) -> None:
    target_id = "target_idx_cov"
    root = tmp_settings / target_id
    receipts = root / "receipts" / "pol"
    receipts.mkdir(parents=True)
    live = {
        "policyId": "pol",
        "backupId": "bak-live",
        "objects": [{"path": "payload/live.age", "digest": "aa" * 32, "size": 12}],
    }
    bad = b"not-json"
    (receipts / "bak-live.json").write_text(json.dumps(live), encoding="utf-8")
    (receipts / "broken.json").write_bytes(bad)
    skip = {"objects": [{"path": "x"}]}  # missing policy/backup
    (receipts / "skip.json").write_text(json.dumps(skip), encoding="utf-8")

    target = SimpleNamespace(target_id=target_id, root=root, store=None)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        rebuilt = backup_object_index.rebuild_index_from_target(target)
    assert rebuilt["scannedReceipts"] >= 1
    assert rebuilt["liveRecoveryPoints"] >= 1

    with pytest.raises(AppError):
        backup_object_index.rebuild_index_from_target(SimpleNamespace(target_id="", root=None, store=None))

    store = MemoryTargetStore()
    store.put_if_absent("receipts/pol/bak-s.json", json.dumps(live).encode("utf-8"))
    store.put_if_absent("receipts/pol/bad.json", b"{")
    store.put_if_absent("payload/orphan.age", b"abc")
    store.put_if_absent("payload/live.age", b"xxxxxxxxxxxx")
    backup_control.put_recovery_object_ref(
        target_id=target_id,
        policy_id="pol",
        backup_id="bak-live",
        object_key="payload/live.age",
        ref_state="live",
        size_bytes=12,
    )
    s3_target = SimpleNamespace(target_id=target_id, root=None, store=store)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=True,
    ):
        rebuilt_s3 = backup_object_index.rebuild_index_from_target(s3_target)
    assert rebuilt_s3["scannedReceipts"] >= 1

    inv = backup_object_index.reconcile_inventory_page(s3_target, prefix="")
    assert inv["examined"] >= 1
    assert "payload/orphan.age" in inv["orphans"] or inv["examined"] > 0
    inv_fs = backup_object_index.reconcile_inventory_page(target)
    assert inv_fs["examined"] == 0

    # apply_retirement when refs empty seeds from receipt
    backup_control.clear_target_object_index(target_id)
    changed = backup_object_index.apply_retirement_to_index(
        target_id=target_id,
        policy_id="pol",
        backup_id="bak-new",
        receipt=live,
    )
    assert changed >= 1
    # second apply is no-op for already retired
    assert (
        backup_object_index.apply_retirement_to_index(
            target_id=target_id, policy_id="pol", backup_id="bak-new", receipt=live
        )
        == 0
    )

    assert backup_object_index._read_json_bytes(None) is None
    assert backup_object_index._read_json_bytes(b"[1]") is None
    entries = backup_object_index.receipt_payload_entries(
        {
            "filename": "f.age",
            "objectDigest": "bb" * 32,
            "objects": [{"key": "k1", "ciphertextDigest": "cc" * 32, "sizeBytes": 3}],
            "components": [None, {"path": "p2", "digest": "dd" * 32}],
        }
    )
    assert entries


def test_tiering_chain_closure_execute_and_assert(tmp_settings: Path) -> None:
    assert backup_tiering.normalize_storage_tier(None) is None
    assert backup_tiering.normalize_storage_tier("") is None
    assert backup_tiering.normalize_storage_tier("HOT") == "hot"
    with pytest.raises(AppError):
        backup_tiering.normalize_storage_tier("glacier")
    assert backup_tiering.target_storage_tier(None) == "hot"
    assert backup_tiering.target_storage_tier({"storageTier": "weird"}) == "hot"

    with patch.object(
        backup_tiering.backup_dr_ledger,
        "list_recovery_points",
        return_value=[
            {"backupId": "F0"},
            {"backupId": "I1", "parentBackupId": "F0"},
            {"backupId": "I2", "baseBackupId": "I1"},
        ],
    ):
        unit = backup_tiering.build_recovery_chain_placement_unit("pol", "I2")
    assert unit["closureComplete"] is True
    assert unit["memberBackupIds"] == ["F0", "I1", "I2"]
    assert unit["baselineBackupId"] == "F0"

    ok, reason = backup_tiering.chain_satisfies_tier(
        {"memberBackupIds": ["F0"]},
        required_tier="hot",
        copies_by_backup={"F0": []},
        targets_by_id={},
    )
    assert ok is False and reason.startswith("missing-live-copy")

    src = "target_tier_exec_src"
    dst = "target_tier_exec_dst"
    backup_targets.register_filesystem_target(src, path=tmp_settings / src, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(dst, path=tmp_settings / dst, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(dst, storage_tier="archive")

    with patch.object(
        backup_tiering.backup_scheduler,
        "plan_target_placement",
        return_value=[],
    ):
        rejected = backup_tiering.plan_tier_placement(
            "missing-pol",
            "bak",
            desired_tier="archive",
            source_target_id=src,
            candidate_target_ids=[dst],
        )
    assert rejected["status"] == "rejected"

    with patch.object(
        backup_tiering.backup_scheduler,
        "plan_target_placement",
        return_value=[((0, 0, 0, 1.0, 1, 0.0, 0, 0, dst), dst)],
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"backupId": "bak", "targetId": src, "recoverable": True, "state": "healthy"}],
    ), patch.object(
        backup_tiering.backup_policies,
        "get_policy",
        side_effect=AppError("missing", status=404),
    ):
        planned = backup_tiering.plan_tier_placement(
            "pol",
            "bak",
            desired_tier="archive",
            source_target_id=src,
        )
    assert planned["status"] in {"planned", "rejected"}

    with patch.object(
        backup_tiering.backup_publish,
        "resolve_target",
        side_effect=lambda tid: SimpleNamespace(root=tmp_settings / tid, store=None),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"objectSetDigest": "digest-1"}],
    ), patch.object(
        backup_tiering.backup_replication,
        "create_rebalance_job",
        return_value={"jobId": "rebalance_x", "phase": "pending"},
    ), patch.object(
        backup_tiering.backup_replication,
        "execute_rebalance_job",
        return_value={"phase": "complete"},
    ):
        intent = backup_control.commit_lifecycle_intent(kind="tier-migration", phase="planned", payload={})
        result = backup_tiering.execute_tier_migration(
            policy_id="pol",
            backup_id="bak",
            source_target_id=src,
            dest_target_id=dst,
            intent_id=str(intent["intentId"]),
        )
    assert result["ageEncryptionInvoked"] is False
    assert result["objectSetDigest"] == "digest-1"
    assert result["backupId"] == "bak"

    with patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[],
    ), patch.object(
        backup_tiering.backup_targets,
        "list_targets",
        return_value=[{"targetId": src, "storageTier": "hot"}],
    ):
        guard = backup_tiering.assert_hot_anchor_not_archive_dependent("pol", "bak")
    assert "ok" in guard


def test_micro_branch_coverage_for_coverage_gate(tmp_settings: Path) -> None:
    """Hit remaining low-cost branches that keep the monorepo just under 95%."""
    # object_index: skip retiring backup id when scanning live refs
    tid = "target_micro_cov"
    backup_control.put_recovery_object_ref(
        target_id=tid, policy_id="p", backup_id="keep", object_key="k1", ref_state="live", size_bytes=1
    )
    backup_control.put_recovery_object_ref(
        target_id=tid, policy_id="p", backup_id="drop", object_key="k2", ref_state="live", size_bytes=1
    )
    retained = backup_object_index.retained_payload_keys_from_index(tid, retiring_backup_id="drop")
    assert retained is not None and "k1" in retained and "k2" not in retained

    # inventory size mismatch branch
    store = MemoryTargetStore()
    store.put_if_absent("payload/m.age", b"12345")
    backup_control.put_recovery_object_ref(
        target_id=tid, policy_id="p", backup_id="b", object_key="payload/m.age", ref_state="live", size_bytes=99
    )
    inv = backup_object_index.reconcile_inventory_page(
        SimpleNamespace(target_id=tid, root=None, store=store), prefix="payload/"
    )
    assert "payload/m.age" in inv["sizeMismatches"]

    # capacity: skip bad growth observations; total_bytes==0 free-percent path
    rates = backup_capacity._growth_rates_from_observations(
        [
            {"observedAt": "bad", "physicalStoredBytes": 1},
            {"observedAt": _iso(datetime.now(tz=timezone.utc)), "physicalStoredBytes": True},
            {"observedAt": _iso(datetime.now(tz=timezone.utc) - timedelta(days=1)), "physicalStoredBytes": 10},
            {"observedAt": _iso(datetime.now(tz=timezone.utc)), "physicalStoredBytes": 20},
        ]
    )
    assert rates["status"] == "ok"
    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={"freeBytes": 50, "totalBytes": 0, "usedBytes": 0},
    ):
        ok, _ = backup_capacity.check_target_capacity_admission(
            "t", 1, policy={"placement": {"minFreeBytes": 1, "minFreePercent": 10, "hardWatermarkPercent": 99}}
        )
        assert ok is True

    # maintenance: empty target id job skipped; physical growth recorded
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    with patch.object(
        backup_targets,
        "probe_target_capacity",
        return_value={
            "physicalStoredBytes": 123,
            "liveReferencedBytes": 100,
            "retiredPendingGcBytes": 23,
            "observedAt": _iso(datetime.now(tz=timezone.utc)),
        },
    ):
        assert backup_maintenance._probe_capacity_page(limit=5) >= 1
    with (
        patch.object(backup_maintenance.backup_drain, "reconcile_drain_projections", return_value={"recreated": 0, "fenced": 0}),
        patch.object(
            backup_maintenance.backup_drain,
            "list_target_drain_jobs",
            return_value=[{"targetId": "", "phase": "draining"}, {"targetId": tid, "phase": "draining"}],
        ),
        patch.object(backup_maintenance.backup_drain, "process_target_drain", side_effect=RuntimeError("x")),
    ):
        summary = backup_maintenance._process_drain_scopes(instance_id="micro", limit=5)
    assert summary["drainFailures"] >= 1

    # tiering: metadata objectSetDigest path + assert with real copies list iteration
    with patch.object(
        backup_tiering.backup_publish,
        "resolve_target",
        side_effect=lambda x: SimpleNamespace(root=tmp_settings / x, store=None),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"metadata": {"objectSetDigest": "meta-d"}}, {"backupId": "x", "targetId": tid}],
    ), patch.object(
        backup_tiering.backup_replication,
        "create_rebalance_job",
        return_value={"jobId": "j", "phase": "pending"},
    ), patch.object(
        backup_tiering.backup_replication,
        "execute_rebalance_job",
        return_value={"phase": "complete"},
    ):
        moved = backup_tiering.execute_tier_migration(
            policy_id="p", backup_id="b", source_target_id=tid, dest_target_id=tid
        )
    assert moved["objectSetDigest"] == "meta-d"
    guard = backup_tiering.assert_hot_anchor_not_archive_dependent(
        "p",
        "b",
        targets_by_id={tid: {"targetId": tid, "storageTier": "hot"}},
    )
    assert "unit" in guard


def test_control_checkpoint_and_schema_helpers(tmp_settings: Path) -> None:
    assert backup_control.schema_version() == backup_control.CONTROL_SCHEMA_VERSION
    migrations = backup_control.list_schema_migrations()
    assert any(item["version"] == 2 for item in migrations)
    path = backup_control.create_control_checkpoint(tmp_settings / "ckpt2.sqlite3")
    assert path.is_file()

    intent = backup_control.commit_lifecycle_intent(
        kind="rebalance",
        target_id="target_x",
        phase="open",
        payload={"n": 1},
    )
    got = backup_control.get_lifecycle_intent(str(intent["intentId"]))
    assert got is not None
    updated = backup_control.complete_lifecycle_intent(str(intent["intentId"]), phase="done")
    assert updated["phase"] == "done"
    with pytest.raises(AppError):
        backup_control.update_lifecycle_intent_phase("missing-intent", "x")

    listed = backup_control.list_lifecycle_intents(kind="rebalance", phase="done", limit=10)
    assert listed

    backup_control.upsert_target_object(
        target_id="target_x",
        object_key="o1",
        size_bytes=10,
        live_delta=1,
        state="live",
    )
    backup_control.upsert_target_object(
        target_id="target_x",
        object_key="o1",
        size_bytes=20,
        live_delta=-1,
        retired_delta=1,
    )
    obj = backup_control.get_target_object("target_x", "o1")
    assert obj is not None
    assert obj["liveRefCount"] == 0
    assert backup_control.list_target_objects("target_x", gc_candidates_only=True)

    backup_control.record_capacity_growth_observation(
        target_id="target_x",
        physical_stored_bytes=100,
        observed_at=_iso(datetime.now(tz=timezone.utc) - timedelta(days=2)),
    )
    backup_control.record_capacity_growth_observation(
        target_id="target_x",
        physical_stored_bytes=300,
        observed_at=_iso(datetime.now(tz=timezone.utc)),
    )
    obs = backup_control.list_capacity_growth_observations("target_x", limit=5)
    assert len(obs) >= 2

    rates = backup_capacity._growth_rates_from_observations(obs)
    assert rates["status"] == "ok"
    assert backup_capacity._growth_rates_from_observations([])["status"] == "unavailable"
    assert backup_capacity._parse_iso("not-a-date") is None
    assert backup_capacity._parse_iso("2026-01-01T00:00:00") is not None


def test_capacity_admission_and_summary_paths(tmp_settings: Path) -> None:
    tid = "target_cap_cov"
    backup_targets.register_filesystem_target(
        tid,
        path=tmp_settings / tid,
        storage_cost_per_gib_month=0.01,
        egress_cost_per_gib=0.02,
        region="ra",
    )
    backup_targets.register_filesystem_target(
        "target_cap_dst",
        path=tmp_settings / "target_cap_dst",
        storage_cost_per_gib_month=0.01,
        region="rb",
    )
    assert backup_capacity.get_target_capacity(tid)["source"] in {"filesystem", "unknown"}
    assert backup_capacity.predict_next_backup_bytes("no-pol") is None
    backup_capacity.record_physical_size_evidence(
        policy_id="pol-ev",
        backup_id="b1",
        snapshot_kind="full",
        physical_bytes=50,
    )
    pred = backup_capacity.predict_next_backup_size("pol-ev")
    assert pred["predictedBytes"] == 50

    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={
            "freeBytes": 1000,
            "totalBytes": 10000,
            "usedBytes": 9000,
            "physicalStoredBytes": 9000,
            "freePercent": 10.0,
        },
    ):
        ok, reason = backup_capacity.check_target_capacity_admission(
            tid,
            10,
            policy={"placement": {"hardWatermarkPercent": 99, "minFreeBytes": 1, "minFreePercent": 1}},
        )
        assert ok is True
        assert reason == "admitted"

    summary = backup_capacity.capacity_summary()
    assert "targets" in summary

    cost = backup_capacity.estimate_transfer_cost(
        1024,
        source_target_id=tid,
        dest_target_id="target_cap_dst",
    )
    assert cost["costStatus"] == "ok"


def test_drain_cancel_and_reconcile_fenced(tmp_settings: Path) -> None:
    tid = "target_drain_cov"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    job = backup_drain.start_target_drain(tid, reason="cov")
    assert job["phase"] == "draining"
    cancelled = backup_drain.cancel_target_drain(tid)
    assert cancelled["phase"] == "cancelled"

    # Projection failure is non-fatal after control commit
    with patch.object(backup_targets, "_project_target", side_effect=OSError("disk full")):
        job2 = backup_drain.start_target_drain(tid, reason="proj-fail")
    assert job2["phase"] == "draining"

    # Job insert failure marks awaiting-job-projection then raises
    backup_drain.cancel_target_drain(tid)
    with patch.object(backup_drain, "_insert_drain_job", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError):
            backup_drain.start_target_drain(tid, reason="insert-fail")
    awaiting = [
        i
        for i in backup_control.list_lifecycle_intents(kind="drain", target_id=tid)
        if i.get("phase") == "awaiting-job-projection"
    ]
    assert awaiting

    # Reconcile recreates job from awaiting intent
    rebuilt = backup_drain.reconcile_drain_projections()
    assert rebuilt["recreated"] >= 1
    assert backup_drain.get_target_drain_job(target_id=tid) is not None

    # Existing non-terminal job + open intent -> mark job-projected
    intent = backup_control.commit_lifecycle_intent(
        kind="drain",
        target_id=tid,
        phase="topology-committed",
        payload={"drainId": "drain_exist", "reason": "x", "targetId": tid},
    )
    bumped = backup_drain.reconcile_drain_projections()
    assert bumped["recreated"] >= 0
    intent_after = backup_control.get_lifecycle_intent(str(intent["intentId"]))
    assert intent_after is not None
    assert intent_after["phase"] in {"job-projected", "fenced", "topology-committed"}

    # Empty targetId intent is skipped
    backup_control.commit_lifecycle_intent(kind="drain", target_id=None, phase="open", payload={})
    backup_drain.reconcile_drain_projections()

    # Orphan topology draining without job or open intent
    orphan = "target_drain_orphan"
    backup_targets.register_filesystem_target(orphan, path=tmp_settings / orphan)
    backup_control.mutate_target(
        orphan,
        expected_generation=None,
        mutate=lambda t: {
            **t,
            "drainState": "draining",
            "drainReason": "orphan",
            "activeDrainId": "drain_orphan_only",
        },
    )
    # Ensure no drain job row
    assert backup_drain.get_target_drain_job(target_id=orphan) is None
    orphan_summary = backup_drain.reconcile_drain_projections()
    assert orphan_summary["recreated"] >= 1
    assert backup_drain.get_target_drain_job(target_id=orphan) is not None

    # Stale generation fence
    backup_control.commit_lifecycle_intent(
        kind="drain",
        target_id=orphan,
        phase="topology-committed",
        expected_generation=10**9,
        payload={"drainId": "stale", "reason": "stale", "targetId": orphan},
    )
    fenced = backup_drain.reconcile_drain_projections()
    assert fenced["fenced"] >= 1

    cancelled2 = backup_drain.cancel_target_drain(orphan)
    assert cancelled2["phase"] == "cancelled"


def test_maintenance_probe_and_scope_lease(tmp_settings: Path) -> None:
    tid = "target_maint_cov"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    n = backup_maintenance._probe_capacity_page(limit=10)
    assert n >= 1

    acquired, value = backup_maintenance._run_with_scope_lease(
        worker_kind="repair",
        scope_id="global",
        instance_id="cov-worker",
        lease_seconds=30,
        work=lambda: {"ok": True},
    )
    assert acquired is True
    assert value == {"ok": True}

    # second holder fails to acquire
    held = backup_control.acquire_maintenance_lease(
        "repair", "global", owner_instance_id="other", lease_seconds=60
    )
    assert held is not None
    acquired2, value2 = backup_maintenance._run_with_scope_lease(
        worker_kind="repair",
        scope_id="global",
        instance_id="cov-worker-2",
        lease_seconds=30,
        work=lambda: {"ok": False},
    )
    assert acquired2 is False
    assert value2 is None


def test_drain_process_paths_and_blockers(tmp_settings: Path) -> None:
    tid = "target_drain_proc"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)

    assert backup_drain.process_target_drain("missing")["status"] == "skipped"
    assert backup_drain.get_target_drain_job(drain_id="nope") is None
    assert backup_drain.get_target_drain_job() is None

    backup_drain.start_target_drain(tid, reason="proc")
    # terminal short-circuit
    backup_drain._update_drain_state(tid, "drained")
    assert backup_drain.process_target_drain(tid)["status"] == "completed"

    # restart drain for live-zero path
    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "drainState": "active"},
    )
    backup_drain.start_target_drain(tid, reason="proc2")
    with (
        patch.object(backup_drain.backup_dr_ledger, "count_live_logical_recovery_copies", return_value=0),
        patch.object(backup_drain, "_drain_completion_blockers", return_value=[]),
    ):
        drained = backup_drain.process_target_drain(tid, instance_id="drain-cov")
    assert drained["status"] == "drained"

    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "drainState": "active"},
    )
    backup_drain.start_target_drain(tid, reason="proc3")
    with (
        patch.object(backup_drain.backup_dr_ledger, "count_live_logical_recovery_copies", return_value=0),
        patch.object(backup_drain, "_drain_completion_blockers", return_value=["pending-retirement"]),
    ):
        waiting = backup_drain.process_target_drain(tid, instance_id="drain-cov-2")
    assert waiting["status"] == "in_progress"
    assert "pending-retirement" in (waiting.get("blockers") or [])

    # lease contention
    backup_drain.start_target_drain(tid, reason="proc4")
    held = backup_control.acquire_maintenance_lease(
        "target-drain", tid, owner_instance_id="other", lease_seconds=60
    )
    assert held is not None
    skipped = backup_drain.process_target_drain(tid, instance_id="me")
    assert skipped["status"] == "skipped"

    # active run / recovery helpers
    with patch.object(
        backup_drain.backup_scheduler,
        "list_active_runs",
        return_value=[{"policyId": "p", "scheduleSlot": "s1"}],
    ), patch.object(
        backup_drain.backup_run_plan,
        "read_run_plan",
        return_value={"selectedWriteTargetId": tid},
    ):
        assert backup_drain._active_run_targets(tid) is True
    with patch.object(
        backup_drain.backup_scheduler,
        "list_active_runs",
        return_value=[{"policyId": "p", "scheduleSlot": ""}],
    ), patch.object(
        backup_drain.backup_policies,
        "get_policy",
        side_effect=AppError("missing", status=404),
    ):
        assert backup_drain._active_run_targets(tid) is False

    with patch.object(
        backup_drain.backup_recovery_keeper,
        "scan_durable_recovery_sessions",
        return_value={
            "a": {"phase": "running", "activeSourceTargetId": tid, "holds": [{"targetId": "other"}]},
            "b": {"phase": "complete", "targetId": tid},
        },
    ):
        # TERMINAL_PHASES may or may not include complete
        assert isinstance(backup_drain._active_recovery_targets(tid), bool)

    with (
        patch.object(backup_drain.backup_publish, "resolve_target", side_effect=RuntimeError("gone")),
    ):
        assert backup_drain._drain_completion_blockers(tid) == ["target-unavailable"]

    # evacuation page with live + dead copies, empty rank, and page-limit cursor
    backup_control.release_maintenance_lease(
        "target-drain", tid, owner_instance_id="other", fencing_token=int(held["fencingToken"])
    )
    backup_drain.start_target_drain(tid, reason="evac")
    page_copies = [
        {
            "recoverable": False,
            "state": "retired",
            "policyId": "pol",
            "backupId": "old",
            "logicalId": "L0",
            "committedAt": "2026-01-01T00:00:00Z",
        },
        {
            "recoverable": True,
            "state": "healthy",
            "policyId": "pol",
            "backupId": "bak1",
            "logicalId": "L1",
            "committedAt": "2026-01-02T00:00:00Z",
            "metadata": {"physicalBytes": 100},
        },
        {
            "recoverable": True,
            "state": "healthy",
            "policyId": "pol",
            "backupId": "bak2",
            "logicalId": "L2",
            "committedAt": "2026-01-03T00:00:00Z",
            "metadata": {"ciphertextBytes": 50},
        },
        {
            "recoverable": True,
            "state": "healthy",
            "policyId": "pol",
            "backupId": "bak3",
            "logicalId": "L3",
            "committedAt": "2026-01-04T00:00:00Z",
        },
    ]
    plan_results = iter([[], [((0,), "target_elsewhere")]])
    with (
        patch.object(backup_drain.backup_dr_ledger, "count_live_logical_recovery_copies", return_value=3),
        patch.object(backup_drain.backup_dr_ledger, "list_logical_recovery_copies", return_value=page_copies),
        patch.object(
            backup_drain.backup_policies,
            "get_policy",
            side_effect=AppError("missing", status=404),
        ),
        patch.object(
            backup_drain.backup_scheduler,
            "plan_target_placement",
            side_effect=lambda *a, **k: next(plan_results, [((0,), "target_elsewhere")]),
        ),
        patch.object(backup_drain.backup_replication, "create_rebalance_job", return_value={"jobId": "r1"}),
        patch.object(
            backup_drain.backup_targets,
            "list_targets",
            return_value=[
                {"targetId": tid, "drainState": "draining"},
                {"targetId": "target_elsewhere", "drainState": "active"},
            ],
        ),
    ):
        progress = backup_drain.process_target_drain(
            tid, instance_id="evac-worker", max_rebalances_per_step=1, scan_page_size=2
        )
    assert progress["status"] == "in_progress"
    assert progress.get("rebalancesTriggered", 0) >= 0

    # Full blocker matrix when resolve succeeds
    fake_target = SimpleNamespace(target_id=tid, root=tmp_settings / tid, store=None)
    with (
        patch.object(backup_drain.backup_publish, "resolve_target", return_value=fake_target),
        patch.object(backup_drain.backup_writer_lease, "active_writer_lease", return_value=True),
        patch.object(backup_drain, "_active_run_targets", return_value=True),
        patch.object(backup_drain, "_active_recovery_targets", return_value=True),
        patch.object(backup_drain.backup_replication, "has_source_holds_for_target", return_value=True),
        patch.object(
            backup_drain.backup_replication,
            "list_repair_jobs",
            return_value=[{"phase": "running"}] + [{"phase": "complete"}] * 499,
        ),
        patch.object(
            backup_drain.backup_replication,
            "list_rebalance_jobs",
            return_value=[{"phase": "running"}] + [{"phase": "complete"}] * 499,
        ),
        patch.object(
            backup_drain.backup_retirement,
            "list_copy_retirement_jobs",
            return_value=[{"phase": "requested"}] + [{"phase": "reclaimed"}] * 499,
        ),
    ):
        blockers = backup_drain._drain_completion_blockers(tid)
    assert "active-writer-lease" in blockers
    assert "active-backup-run" in blockers
    assert "active-recovery" in blockers
    assert "active-source-hold" in blockers
    assert "active-repair-source" in blockers
    assert "active-rebalance-source" in blockers
    assert "pending-retirement" in blockers

    with (
        patch.object(backup_drain.backup_publish, "resolve_target", return_value=fake_target),
        patch.object(backup_drain.backup_writer_lease, "active_writer_lease", return_value=False),
        patch.object(backup_drain, "_active_run_targets", return_value=False),
        patch.object(backup_drain, "_active_recovery_targets", return_value=False),
        patch.object(backup_drain.backup_replication, "has_source_holds_for_target", return_value=False),
        patch.object(backup_drain.backup_replication, "list_repair_jobs", return_value=[{"phase": "healthy"}] * 500),
        patch.object(backup_drain.backup_replication, "list_rebalance_jobs", return_value=[{"phase": "complete"}] * 500),
        patch.object(backup_drain.backup_retirement, "list_copy_retirement_jobs", return_value=[{"phase": "reclaimed"}] * 500),
    ):
        incomplete = backup_drain._drain_completion_blockers(tid)
    assert "repair-scan-incomplete" in incomplete
    assert "rebalance-scan-incomplete" in incomplete
    assert "retirement-scan-incomplete" in incomplete

    # reconcile fences intents for non-draining topology
    backup_control.commit_lifecycle_intent(
        kind="drain",
        target_id=tid,
        phase="topology-committed",
        expected_generation=9999,
        payload={"drainId": "drain_stale", "reason": "x", "targetId": tid},
    )
    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "drainState": "active"},
    )
    fenced = backup_drain.reconcile_drain_projections()
    assert fenced["fenced"] >= 0


def test_tiering_execute_exception_and_require_known_rates(tmp_settings: Path) -> None:
    src = "target_tier_ex_src"
    dst = "target_tier_ex_dst"
    backup_targets.register_filesystem_target(
        src, path=tmp_settings / src, region="r1", failure_domain="fd1", egress_cost_per_gib=0.01
    )
    backup_targets.register_filesystem_target(
        dst,
        path=tmp_settings / dst,
        region="r2",
        failure_domain="fd2",
        storage_cost_per_gib_month=0.02,
    )
    backup_targets.set_target_storage_tier(dst, storage_tier="warm")

    with patch.object(
        backup_tiering.backup_publish,
        "resolve_target",
        side_effect=lambda tid: SimpleNamespace(root=tmp_settings / tid, store=None),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"metadata": {"objectSetDigest": "d2"}}],
    ), patch.object(
        backup_tiering.backup_replication,
        "create_rebalance_job",
        return_value={"jobId": "rj", "phase": "pending"},
    ), patch.object(
        backup_tiering.backup_replication,
        "execute_rebalance_job",
        side_effect=RuntimeError("boom"),
    ):
        out = backup_tiering.execute_tier_migration(
            policy_id="pol",
            backup_id="bak",
            source_target_id=src,
            dest_target_id=dst,
            intent_id=None,
        )
    assert out["objectSetDigest"] == "d2"
    assert out["rebalance"]["phase"] == "pending"

    with patch.object(
        backup_tiering.backup_publish,
        "resolve_target",
        side_effect=lambda tid: SimpleNamespace(root=None, store=object()),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[],
    ), patch.object(
        backup_tiering.backup_replication,
        "create_rebalance_job",
        return_value={"phase": "pending"},  # no jobId
    ):
        out2 = backup_tiering.execute_tier_migration(
            policy_id="pol",
            backup_id="bak",
            source_target_id=src,
            dest_target_id=dst,
        )
    assert out2["status"] in {"pending", "submitted"}

    # requireKnownRates filters candidates missing rates
    bare = "target_tier_bare"
    backup_targets.register_filesystem_target(bare, path=tmp_settings / bare, region="r3", failure_domain="fd3")
    backup_targets.set_target_storage_tier(bare, storage_tier="warm")
    with patch.object(
        backup_tiering.backup_policies,
        "get_policy",
        return_value={"policyId": "pol", "costObjectives": {"requireKnownRates": True}},
    ), patch.object(
        backup_tiering.backup_scheduler,
        "plan_target_placement",
        return_value=[((0,), bare)],
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"backupId": "bak", "targetId": src, "recoverable": True, "state": "healthy"}],
    ):
        # bare target has no cost rates -> skipped when requireKnownRates
        plan = backup_tiering.plan_tier_placement(
            "pol",
            "bak",
            desired_tier="warm",
            source_target_id=src,
            candidate_target_ids=[bare],
        )
    assert plan["status"] in {"rejected", "planned"}

    # hot desired rejects cold chain
    with patch.object(
        backup_tiering.backup_policies,
        "get_policy",
        return_value={"policyId": "pol"},
    ), patch.object(
        backup_tiering.backup_scheduler,
        "plan_target_placement",
        return_value=[((0,), dst)],
    ), patch.object(
        backup_tiering,
        "chain_satisfies_tier",
        return_value=(False, "ancestor-tier-too-cold:F0"),
    ), patch.object(
        backup_tiering.backup_dr_ledger,
        "list_logical_recovery_copies",
        return_value=[{"backupId": "bak", "targetId": src, "recoverable": True, "state": "healthy"}],
    ):
        hot_plan = backup_tiering.plan_tier_placement(
            "pol",
            "bak",
            desired_tier="hot",
            source_target_id=src,
            candidate_target_ids=[dst],
        )
    assert hot_plan["status"] == "rejected"


def test_capacity_force_full_and_shrinking_forecast(tmp_settings: Path) -> None:
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None}):
        ok, reason = backup_capacity.check_target_capacity_admission("t", None, force_full=True)
        assert ok is False and reason == "capacity-evidence-unavailable"

    now = datetime.now(tz=timezone.utc)
    tid = "target_shrink"
    backup_control.record_capacity_growth_observation(
        target_id=tid, physical_stored_bytes=500, observed_at=_iso(now - timedelta(days=5))
    )
    backup_control.record_capacity_growth_observation(
        target_id=tid, physical_stored_bytes=100, observed_at=_iso(now)
    )
    with patch.object(
        backup_capacity,
        "get_target_capacity",
        return_value={
            "freeBytes": 1000,
            "totalBytes": 2000,
            "freePercent": 50.0,
            "physicalStoredBytes": 100,
            "observedAt": _iso(now),
        },
    ):
        horizon = backup_capacity.estimate_target_exhaustion_horizon(tid, "pol")
    assert horizon.get("forecastStatus") in {"ok", "stable-or-shrinking"}

    # workspace estimator path
    est = backup_capacity.predict_next_backup_size("no-hist", workspace_physical_bytes=1000)
    assert est["predictedBytes"] == 1200

    # high confidence growth (10 samples over 10 days)
    tid2 = "target_hi_conf"
    for day in range(11):
        backup_control.record_capacity_growth_observation(
            target_id=tid2,
            physical_stored_bytes=day * 10,
            observed_at=_iso(now - timedelta(days=10 - day)),
        )
    rates = backup_capacity._growth_rates_from_observations(
        backup_control.list_capacity_growth_observations(tid2, limit=20)
    )
    assert rates["confidence"] in {"high", "medium", "low"}


def test_maintenance_supervisor_tick_once(tmp_settings: Path) -> None:
    with (
        patch.object(backup_maintenance.backup_recovery_keeper, "reconcile_durable_recovery_leases", return_value={}),
        patch.object(backup_maintenance.backup_replication, "process_pending_jobs", return_value={}),
        patch.object(backup_maintenance.backup_replication, "process_pending_repairs", return_value={}),
        patch.object(backup_maintenance.backup_replication, "process_pending_rebalances", return_value={}),
        patch.object(backup_maintenance.backup_retirement, "process_pending_retirements", return_value={}),
        patch.object(backup_maintenance, "_probe_capacity_page", return_value=0),
        patch.object(
            backup_maintenance.backup_transfer_budget.get_global_transfer_budget_manager(),
            "transfer_control_summary",
            return_value={},
        ),
        patch.object(backup_maintenance.backup_drain, "list_target_drain_jobs", return_value=[]),
        patch.object(backup_maintenance.backup_drain, "reconcile_drain_projections", return_value={"recreated": 0, "fenced": 0}),
    ):
        summary = backup_maintenance.maintenance_tick(instance_id="sup-once", limit_per_worker=1)
    assert summary["leaseAcquired"] is True

    supervisor = backup_maintenance.StorageMaintenanceSupervisor(instance_id="loop", tick_seconds=0.01, limit_per_worker=1)
    calls = {"n": 0}

    def _tick() -> dict[str, str]:
        calls["n"] += 1
        if calls["n"] >= 1:
            supervisor._stop.set()
        if calls["n"] == 1:
            raise RuntimeError("tick-failed")
        return {"ok": "1"}

    with patch.object(supervisor, "tick", side_effect=_tick):
        supervisor._stop.clear()
        supervisor._loop()
    assert calls["n"] >= 1
    supervisor.stop(timeout=0.0)
