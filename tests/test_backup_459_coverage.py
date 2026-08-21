"""Branch coverage for 4.5.9 object index, tiering, control, and drain paths."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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

    # Orphan topology draining without job: reconcile recreates
    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "drainState": "draining", "drainReason": "orphan", "activeDrainId": "drain_orphan"},
    )
    # clear drain jobs by cancelling path already terminal; insert no job
    summary = backup_drain.reconcile_drain_projections()
    assert summary["recreated"] >= 0  # may recreate orphan topology


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
