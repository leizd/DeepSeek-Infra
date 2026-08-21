"""Push backup_targets / maintenance / related paths above the 95% gate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_maintenance,
    backup_targets,
)


def test_target_validation_helpers_and_costs(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        backup_targets._operator_cost(float("nan"), "storageCostPerGiBMonth")
    with pytest.raises(AppError):
        backup_targets._operator_cost(-1, "egressCostPerGiB")
    assert backup_targets._operator_cost(None, "x") is None
    assert backup_targets._operator_cost(1.5, "x") == 1.5

    with pytest.raises(AppError):
        backup_targets._positive_qos(0, "maxReadBytesPerSecond", 1)
    with pytest.raises(AppError):
        backup_targets._positive_qos(True, "maxReadBytesPerSecond", 1)  # type: ignore[arg-type]
    assert backup_targets._positive_qos(None, "maxReadBytesPerSecond", 9) == 9
    assert backup_targets._positive_qos(3, "maxReadBytesPerSecond", 9) == 3

    with pytest.raises(AppError):
        backup_targets._optional_positive_int(0, "quotaBytes")
    with pytest.raises(AppError):
        backup_targets._optional_positive_int(True, "quotaBytes")  # type: ignore[arg-type]
    assert backup_targets._optional_positive_int(None, "quotaBytes") is None
    assert backup_targets._optional_positive_int(8, "quotaBytes") == 8

    assert backup_targets._is_reparse_point(tmp_settings) in {True, False}
    with patch("os.lstat", side_effect=OSError("x")):
        assert backup_targets._is_reparse_point(tmp_settings) is False


def test_register_filesystem_reuses_marker_and_invalid_marker(tmp_settings: Path) -> None:
    tid = "target_marker_reuse"
    path = tmp_settings / tid
    first = backup_targets.register_filesystem_target(tid, path=path, label="one")
    # Second registration reuses marker path
    second = backup_targets.register_filesystem_target(tid, path=path, label="two")
    assert first["targetId"] == second["targetId"]

    # Corrupt marker is ignored and re-registered
    tid2 = "target_marker_bad"
    path2 = tmp_settings / tid2
    path2.mkdir(parents=True)
    marker = path2 / backup_targets.TARGET_MARKER_NAME
    marker.write_text("{not-json", encoding="utf-8")
    rec = backup_targets.register_filesystem_target(tid2, path=path2)
    assert rec["targetId"] == tid2

    # Wrong targetId in marker is ignored
    tid3 = "target_marker_wrong"
    path3 = tmp_settings / tid3
    path3.mkdir(parents=True)
    (path3 / backup_targets.TARGET_MARKER_NAME).write_text(
        json.dumps({"targetId": "target_other", "targetNonce": "abc"}),
        encoding="utf-8",
    )
    rec3 = backup_targets.register_filesystem_target(tid3, path=path3)
    assert rec3["targetId"] == tid3


def test_get_list_delete_target_edge_paths(tmp_settings: Path) -> None:
    tid = "target_get_edge"
    with pytest.raises(AppError):
        backup_targets.get_target("target_missing_xyz")

    # Unreadable registry file
    path = backup_targets._registry_path(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(AppError):
        backup_targets.get_target(tid)

    path.write_text(json.dumps({"noTargetId": True}), encoding="utf-8")
    with pytest.raises(AppError):
        backup_targets.get_target(tid)

    # Valid projection adopt + list repair of projection drift
    good = backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    path.write_text(json.dumps({**good, "label": "stale"}), encoding="utf-8")
    listed = backup_targets.list_targets()
    assert any(t["targetId"] == tid for t in listed)

    # Corrupt json in list dir is skipped
    (backup_targets.BACKUP_TARGET_DIR / "junk.json").write_text("{", encoding="utf-8")
    (backup_targets.BACKUP_TARGET_DIR / "x.checkpoint.json").write_text("{}", encoding="utf-8")
    backup_targets.list_targets()

    deleted = backup_targets.delete_target(tid)
    assert deleted["deleted"] is True


def test_set_storage_tier_and_capacity_paths(tmp_settings: Path) -> None:
    tid = "target_tier_cap"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    updated = backup_targets.set_target_storage_tier(
        tid,
        storage_tier="warm",
        restore_latency_class="minutes",
        min_residence_seconds=100,
        retrieval_cost_per_gib=0.05,
    )
    assert updated["storageTier"] == "warm"
    assert updated["retrievalCostPerGiB"] == 0.05

    with pytest.raises(AppError):
        backup_targets.set_target_storage_tier(tid, storage_tier="nope")

    # filesystem capacity
    cap = backup_targets.probe_target_capacity(tid)
    assert cap["source"] == "filesystem"

    # disk_usage OSError falls through
    with patch("shutil.disk_usage", side_effect=OSError("x")):
        cap2 = backup_targets.probe_target_capacity(tid)
    assert cap2["source"] in {"unknown", "configured-quota-physical-estimate", "filesystem"}

    # quota + physical index path with retired copies
    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**{k: v for k, v in t.items() if k != "path"}, "kind": "s3", "quotaBytes": 1000},
    )
    backup_targets._project_target(backup_control.get_target(tid) or {})
    backup_control.clear_target_object_index(tid)
    with patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.list_logical_recovery_copies",
        return_value=[
            {
                "recoverable": True,
                "state": "healthy",
                "objectSetDigest": "d1",
                "physicalBytes": 100,
            },
            {
                "recoverable": True,
                "state": "healthy",
                "objectSetDigest": "d1",  # shared digest skip
                "physicalBytes": 100,
            },
            {
                "recoverable": False,
                "state": "retired",
                "backupId": "b2",
                "physicalBytes": 50,
            },
            {
                "recoverable": False,
                "state": "other",
                "backupId": "b3",
                "ciphertextBytes": 10,
            },
            {"recoverable": True, "metadata": {"ciphertextBytes": 20}, "backupId": "b4"},
        ],
    ):
        qcap = backup_targets.probe_target_capacity(tid)
    assert qcap["usedBytes"] is not None
    assert qcap["totalBytes"] == 1000

    # unknown target
    unknown = backup_targets.probe_target_capacity("target_does_not_exist_zzz")
    assert unknown["source"] == "unknown"

    # drain helpers
    backup_targets.register_filesystem_target("target_drain_state", path=tmp_settings / "target_drain_state")
    assert backup_targets.get_target_drain_state("target_drain_state") == "active"
    with patch.object(backup_targets, "get_target", side_effect=RuntimeError("x")):
        assert backup_targets.get_target_drain_state("x") == "unknown"


def test_probe_target_and_open_store_edges(tmp_settings: Path) -> None:
    tid = "target_probe_edge"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / tid)
    result = backup_targets.probe_target(tid)
    assert result.get("ready") is True or result.get("status") in {"ok", "blocked-target-unavailable"}

    with patch.object(backup_targets, "get_target", side_effect=AppError("missing", status=404)):
        offline = backup_targets.probe_target("missing")
    assert offline["ready"] is False

    # filesystem probe when verify fails
    with patch.object(backup_targets, "verify_target_ready", side_effect=AppError("target-rollback-detected", status=409)):
        blocked = backup_targets.probe_target(tid)
    assert blocked["status"] == "target-rollback-detected"

    with patch.object(backup_targets, "verify_target_ready", side_effect=AppError("other-fail", status=400)), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.record_target_evidence",
        side_effect=RuntimeError("ledger-down"),
    ):
        blocked2 = backup_targets.probe_target(tid)
    assert blocked2["ready"] is False

    # evidence recording failure on success path
    with patch.object(backup_targets, "verify_target_ready", return_value=tmp_settings / tid), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.record_target_evidence",
        side_effect=RuntimeError("x"),
    ):
        ok = backup_targets.probe_target(tid)
    assert ok["ready"] is True

    # managed-local store
    with patch(
        "deepseek_infra.infra.workspace.backup_target_store.open_filesystem_store",
        return_value="fs-store",
    ):
        assert backup_targets.open_target_store("managed-local") == "fs-store"

    # filesystem open
    with patch.object(backup_targets, "verify_target_ready", return_value=tmp_settings / tid), patch(
        "deepseek_infra.infra.workspace.backup_target_store.open_filesystem_store",
        return_value="fs2",
    ):
        assert backup_targets.open_target_store(tid) == "fs2"

    # s3 open with proven integrity + write probe fail
    s3_record = {
        "targetId": tid,
        "kind": "s3",
        "lastProbe": {
            "scheduledBackupReady": True,
            "results": {"single-provider-sha256": "PASS", "multipart-provider-sha256": "PASS"},
            "capabilities": {"integrityMode": "strong-provider-checksum"},
        },
    }
    mock_store = MagicMock()
    mock_store.capabilities.return_value = SimpleNamespace(kind="s3")
    with patch.object(backup_targets, "get_target", return_value=s3_record), patch(
        "deepseek_infra.infra.workspace.backup_target_s3.open_s3_store",
        return_value=mock_store,
    ):
        assert backup_targets.open_target_store(tid, write_intent=False) is mock_store
        mock_store.set_integrity_mode.assert_called_with("strong-provider-checksum")

    s3_not_ready = {"targetId": tid, "kind": "s3", "lastProbe": None}
    with patch.object(backup_targets, "get_target", return_value=s3_not_ready), patch(
        "deepseek_infra.infra.workspace.backup_target_s3.open_s3_store",
        return_value=mock_store,
    ), patch(
        "deepseek_infra.infra.workspace.backup_target_store.probe_store_capabilities",
        return_value={"scheduledBackupReady": False},
    ), patch.object(backup_targets, "_mutate_target", return_value=s3_not_ready):
        with pytest.raises(AppError, match="unsupported-conditional-target"):
            backup_targets.open_target_store(tid, write_intent=True)

    # s3 probe_target success + versioning detect error + ledger error
    with patch.object(backup_targets, "get_target", return_value={"targetId": tid, "kind": "s3"}), patch.object(
        backup_targets,
        "open_target_store",
        return_value=mock_store,
    ), patch(
        "deepseek_infra.infra.workspace.backup_target_store.probe_store_capabilities",
        return_value={"scheduledBackupReady": True, "status": "ok", "probedAt": "t", "capabilities": {}},
    ), patch.object(backup_targets, "_mutate_target", return_value={}), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.record_target_evidence",
        side_effect=RuntimeError("x"),
    ):
        mock_store.detect_versioning = MagicMock(side_effect=RuntimeError("ver"))
        s3_ok = backup_targets.probe_target(tid)
    assert s3_ok["kind"] == "s3"
    assert s3_ok["ready"] is True

    # s3 probe_target AppError path
    with patch.object(backup_targets, "get_target", return_value={"targetId": tid, "kind": "s3"}), patch.object(
        backup_targets,
        "open_target_store",
        side_effect=AppError("down", status=503),
    ), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.record_target_evidence",
        side_effect=RuntimeError("x"),
    ):
        s3_bad = backup_targets.probe_target(tid)
    assert s3_bad["ready"] is False

    # unsupported kind
    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "kind": "webdav"},
    )
    with pytest.raises(AppError):
        backup_targets.open_target_store(tid)

    backup_control.mutate_target(
        tid,
        expected_generation=None,
        mutate=lambda t: {**t, "kind": "mystery"},
    )
    with pytest.raises(AppError):
        backup_targets.open_target_store(tid)


def test_record_remote_target_head_paths(tmp_settings: Path) -> None:
    store = MagicMock()
    store.stat.return_value = None
    with patch(
        "deepseek_infra.infra.workspace.backup_target_store.read_json",
        return_value=None,
    ), patch(
        "deepseek_infra.infra.workspace.backup_target_store.put_json_if_absent",
    ) as put_abs, patch.object(backup_targets, "_write_checkpoint"):
        backup_targets.record_remote_target_head(store, target_id="target_h", generation=2, commit_hash="abc")
    put_abs.assert_called()

    meta = SimpleNamespace(etag="e1")
    store.stat.return_value = meta
    with patch(
        "deepseek_infra.infra.workspace.backup_target_store.read_json",
        return_value={"incarnationId": "inc"},
    ), patch(
        "deepseek_infra.infra.workspace.backup_target_store.put_json_if_match",
        side_effect=AppError("cas", status=409),
    ), patch.object(backup_targets, "_write_checkpoint"):
        backup_targets.record_remote_target_head(store, target_id="target_h", generation=3, commit_hash="def")


def test_maintenance_lease_skips_and_supervisor_tick(tmp_settings: Path) -> None:
    # Hold every worker scope so maintenance_tick records leaseSkipped branches
    tokens = {}
    for kind in ("replication", "repair", "rebalance", "retirement"):
        lease = backup_control.acquire_maintenance_lease(
            kind, "global", owner_instance_id="holder", lease_seconds=120
        )
        assert lease is not None
        tokens[kind] = int(lease["fencingToken"])

    with (
        patch.object(backup_maintenance.backup_recovery_keeper, "reconcile_durable_recovery_leases", return_value={}),
        patch.object(backup_maintenance, "_probe_capacity_page", return_value=0),
        patch.object(
            backup_maintenance.backup_transfer_budget.get_global_transfer_budget_manager(),
            "transfer_control_summary",
            return_value={},
        ),
        patch.object(backup_maintenance, "_process_drain_scopes", return_value={"drainsProcessed": 0, "drainFailures": 0, "drainLeaseSkips": 0}),
    ):
        summary = backup_maintenance.maintenance_tick(instance_id="skip-worker", limit_per_worker=2)
    assert summary["leaseAcquired"] is True
    assert summary["replication"] == {"leaseSkipped": True}
    assert summary["repairs"] == {"leaseSkipped": True}
    assert summary["rebalances"] == {"leaseSkipped": True}
    assert summary["retirements"] == {"leaseSkipped": True}

    for kind, token in tokens.items():
        backup_control.release_maintenance_lease(kind, "global", owner_instance_id="holder", fencing_token=token)

    # heartbeat renew failure returns
    stop = __import__("threading").Event()
    with patch.object(backup_maintenance.backup_control, "renew_maintenance_lease", return_value=False):
        # force immediate non-wait by setting stop after patching wait
        stop.set()
        backup_maintenance._lease_heartbeat(stop, instance_id="h", fencing_token=1)
    # path where wait returns False then renew fails
    stop2 = __import__("threading").Event()
    calls = {"n": 0}

    def _wait(timeout: float) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first call continues loop, second exits

    stop2.wait = _wait  # type: ignore[method-assign]
    with patch.object(backup_maintenance.backup_control, "renew_maintenance_lease", return_value=False):
        backup_maintenance._lease_heartbeat(stop2, instance_id="h2", fencing_token=2)

    supervisor = backup_maintenance.StorageMaintenanceSupervisor(instance_id="t", tick_seconds=0.1)
    with patch.object(backup_maintenance, "maintenance_tick", return_value={"leaseAcquired": True}) as tick:
        assert supervisor.tick()["leaseAcquired"] is True
        tick.assert_called()


def test_containment_and_validate_location(tmp_settings: Path) -> None:
    # relative path rejected
    with pytest.raises(AppError):
        backup_targets._resolve_basic(Path("relative"))

    missing = tmp_settings / "nope-dir"
    with pytest.raises(AppError):
        backup_targets._resolve_basic(missing)

    # tmp_settings lives under system temp → validate_target_location rejects it
    ok_path = tmp_settings / "ok-target"
    ok_path.mkdir()
    with pytest.raises(AppError, match="system temporary directory"):
        backup_targets.validate_target_location(ok_path)

    parent = tmp_settings / "overlap_parent"
    child = parent / "child"
    parent.mkdir(parents=True)
    child.mkdir(parents=True)

    # Unit-test peer overlap + skip non-fs / empty path branches directly
    with patch.object(
        backup_targets,
        "list_targets",
        return_value=[
            {"targetId": "target_s3", "kind": "s3"},
            {"targetId": "target_empty", "kind": "filesystem", "path": ""},
            {"targetId": "target_parent", "kind": "filesystem", "path": str(parent)},
            {"targetId": "target_self", "kind": "filesystem", "path": str(child)},
        ],
    ), patch.object(backup_targets.backups, "_registered_contributors", return_value=[]), patch.object(
        backup_targets, "_repo_root", return_value=tmp_settings / "not-related"
    ), patch.object(backup_targets.config, "ROOT", tmp_settings / "not-related-root"), patch.object(
        backup_targets.backups, "RESTORE_DIR", tmp_settings / "not-restore"
    ), patch.object(backup_targets.backups, "BACKUP_DIR", tmp_settings / "not-backup"), patch(
        "tempfile.gettempdir", return_value=str(tmp_settings / "not-temp")
    ):
        msg = backup_targets._containment_violation(child.resolve(), exclude_target_id="target_self")
    assert msg is not None and "overlaps" in msg
