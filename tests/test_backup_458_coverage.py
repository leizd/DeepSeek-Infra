from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_drain,
    backup_maintenance,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_spool,
    backup_targets,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, MultipartUpload


def _stable_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_control_projection_adoption_lease_collision_and_cursor_cas(tmp_settings: Path) -> None:
    adopted = backup_control.adopt_policy_projection({"policyId": "policy-adopt", "policyRevision": 4})
    assert adopted["policyRevision"] == 4
    target = backup_control.adopt_target_projection({"targetId": "target-adopt", "topologyGeneration": 3})
    assert target["topologyGeneration"] == 3
    assert backup_control.list_targets() == [target]

    now = datetime.now(tz=timezone.utc)
    lease = backup_control.acquire_maintenance_lease(
        "drain",
        "target-adopt",
        owner_instance_id="worker-a",
        lease_seconds=60,
        now=now,
    )
    assert lease is not None
    assert backup_control.acquire_maintenance_lease(
        "drain",
        "target-adopt",
        owner_instance_id="worker-b",
        lease_seconds=60,
        now=now,
    ) is None
    assert backup_control.renew_maintenance_lease(
        "drain",
        "target-adopt",
        owner_instance_id="worker-a",
        fencing_token=int(lease["fencingToken"]),
        lease_seconds=60,
    ) is True
    assert backup_control.get_maintenance_cursor("drain", "target-adopt") == {"cursor": None, "generation": 0}
    with pytest.raises(AppError, match="generation mismatch"):
        backup_control.update_maintenance_cursor(
            "drain",
            "target-adopt",
            cursor={"afterBackupId": "backup-1"},
            expected_generation=1,
        )
    assert backup_control.update_maintenance_cursor(
        "drain",
        "target-adopt",
        {"afterBackupId": "backup-1"},
        expected_generation=0,
    )["generation"] == 1

    backup_control.acquire_qos_transfer(
        transfer_id="transfer-wait",
        traffic_class=5,
        source_target_id="target-adopt",
        dest_target_id="target-dest",
        estimated_bytes=8,
        source_concurrency_limit=1,
        dest_concurrency_limit=1,
    )
    spec = [{"bucketKey": "global", "rateBytesPerSecond": 8, "capacityBytes": 8}]
    assert backup_control.consume_qos_tokens(
        transfer_id="transfer-wait",
        requested_bytes=8,
        bucket_specs=spec,
        traffic_class=5,
        now=1.0,
    )["granted"] is True
    assert backup_control.consume_qos_tokens(
        transfer_id="transfer-wait",
        requested_bytes=8,
        bucket_specs=spec,
        traffic_class=5,
        now=1.0,
    )["granted"] is False
    assert backup_control.database_path() == backup_control.CONTROL_DB


def test_control_authority_covers_stale_missing_and_filtered_mutations(tmp_settings: Path) -> None:
    policy_id = "policy_control_edges"
    policy = {"policyId": policy_id, "policyRevision": 1, "name": "original"}
    assert backup_control.create_policy(policy)["policyRevision"] == 1
    with pytest.raises(AppError, match="collision"):
        backup_control.create_policy(policy)
    assert backup_control.adopt_policy_projection({**policy, "name": "stale-projection"})["name"] == "original"
    assert backup_control.get_policy("missing") is None
    assert backup_control.list_policies()[0]["policyId"] == policy_id

    with pytest.raises(AppError, match="not found"):
        backup_control.mutate_policy("missing", expected_revision=1, mutate=lambda item: item)
    with pytest.raises(AppError, match="CAS mismatch"):
        backup_control.mutate_policy(policy_id, expected_revision=9, mutate=lambda item: item)
    updated = backup_control.mutate_policy(
        policy_id,
        expected_revision=1,
        generation_kind="unknown-kind",
        mutate=lambda item: {**item, "name": "updated"},
    )
    assert updated["policyRevision"] == 2
    with pytest.raises(AppError, match="CAS mismatch"):
        backup_control.delete_policy(policy_id, expected_revision=1)
    assert backup_control.delete_policy(policy_id, expected_revision=2)["name"] == "updated"
    with pytest.raises(AppError, match="not found"):
        backup_control.delete_policy(policy_id)

    target = {"targetId": "target_control_edges", "kind": "s3"}
    with pytest.raises(AppError, match="does not exist"):
        backup_control.upsert_target(target, expected_generation=2)
    created = backup_control.upsert_target(target, expected_generation=0)
    assert created["topologyGeneration"] == 1
    assert backup_control.adopt_target_projection({**target, "kind": "filesystem"})["kind"] == "s3"
    with pytest.raises(AppError, match="CAS mismatch"):
        backup_control.upsert_target(target, expected_generation=3)
    changed = backup_control.upsert_target({**target, "label": "changed"}, expected_generation=1)
    assert changed["topologyGeneration"] == 2
    assert backup_control.list_target_ids_page(limit=1) == [target["targetId"]]
    assert backup_control.list_target_ids_page(after_target_id=target["targetId"], limit=1) == []

    backup_control.record_target_capacity_observation(target["targetId"], {"freeBytes": 4, "observedAt": "now"})
    assert backup_control.get_target_capacity_observation(target["targetId"])["freeBytes"] == 4  # type: ignore[index]
    assert backup_control.get_target_capacity_observation("missing") is None
    with pytest.raises(AppError, match="not found"):
        backup_control.mutate_target("missing", expected_generation=1, mutate=lambda item: item)
    with pytest.raises(AppError, match="CAS mismatch"):
        backup_control.mutate_target(target["targetId"], expected_generation=1, mutate=lambda item: item)
    unchanged_generation = backup_control.mutate_target(
        target["targetId"],
        expected_generation=2,
        mutate=lambda item: {**item, "drainState": "active"},
        bump_generation=False,
    )
    assert unchanged_generation["topologyGeneration"] == 2
    with pytest.raises(AppError, match="CAS mismatch"):
        backup_control.delete_target(target["targetId"], expected_generation=1)
    assert backup_control.delete_target(target["targetId"], expected_generation=2)["targetId"] == target["targetId"]
    with pytest.raises(AppError, match="not found"):
        backup_control.delete_target(target["targetId"])

    backup_control.record_capacity_evidence(
        policy_id="capacity-edge",
        backup_id=None,
        snapshot_kind="full",
        physical_bytes=0,
        confidence="low",
        source="ignored",
    )
    backup_control.record_capacity_evidence(
        policy_id="capacity-edge",
        backup_id="backup-edge",
        snapshot_kind="full",
        physical_bytes=9,
        confidence="high",
        source="test",
    )
    assert backup_control.list_capacity_evidence("capacity-edge", snapshot_kind=None)[0]["physicalBytes"] == 9


def test_control_qos_concurrency_and_expiry_branches(tmp_settings: Path) -> None:
    backup_control.acquire_qos_transfer(
        transfer_id="qos-one",
        traffic_class=5,
        source_target_id="target-qos-source",
        dest_target_id=None,
        estimated_bytes=-1,
        source_concurrency_limit=1,
        dest_concurrency_limit=1,
    )
    with pytest.raises(AppError, match="concurrency-exceeded"):
        backup_control.acquire_qos_transfer(
            transfer_id="qos-two",
            traffic_class=5,
            source_target_id="target-qos-source",
            dest_target_id=None,
            estimated_bytes=1,
            source_concurrency_limit=1,
            dest_concurrency_limit=1,
        )
    backup_control.acquire_qos_transfer(
        transfer_id="qos-p0",
        traffic_class=0,
        source_target_id="target-qos-source",
        dest_target_id=None,
        estimated_bytes=1,
        source_concurrency_limit=1,
        dest_concurrency_limit=1,
    )
    assert {item["transferId"] for item in backup_control.list_qos_transfers(now=0)} == {"qos-one", "qos-p0"}
    backup_control.release_qos_transfer("qos-one")
    backup_control.release_qos_transfer("qos-p0")
    assert backup_control.list_qos_transfers(now=10**20) == []


def test_capacity_prediction_admission_horizon_and_summary_edges(tmp_settings: Path) -> None:
    with (
        patch.object(
            backup_capacity.backup_control,
            "list_capacity_evidence",
            return_value=[
                {"backupId": "same", "physicalBytes": True},
                {"backupId": "same", "physicalBytes": 99},
            ],
        ),
        patch.object(
            backup_capacity.backup_dr_ledger,
            "list_recovery_points",
            return_value=[
                {"backupId": "wrong-kind", "snapshotKind": "incremental", "ciphertextBytes": 1},
                {"backupId": "full-a", "snapshotKind": "full", "ciphertextBytes": 20},
                {"backupId": "full-a", "snapshotKind": "full", "ciphertextBytes": 30},
            ],
        ),
    ):
        prediction = backup_capacity.predict_next_backup_size("policy-capacity-edges")
    assert prediction["predictedBytes"] == 20
    assert prediction["sampleCount"] == 1

    with (
        patch.object(backup_capacity.backup_control, "list_capacity_evidence", return_value=[]),
        patch.object(backup_capacity.backup_dr_ledger, "list_recovery_points", return_value=[]),
        patch.object(backup_capacity.backup_dr_ledger, "list_logical_recovery_copies", return_value=[]),
    ):
        estimated = backup_capacity.predict_next_backup_size("policy-workspace-estimate", workspace_physical_bytes=100)
    assert estimated["predictedBytes"] == 120
    assert backup_capacity.predict_next_backup_bytes("policy-no-estimate", workspace_physical_bytes=-1) is None

    def _admit(capacity: dict[str, Any], required: int | None, policy: dict[str, Any] | None = None) -> tuple[bool, str]:
        with patch.object(backup_capacity, "get_target_capacity", return_value=capacity):
            return backup_capacity.check_target_capacity_admission("target", required, policy=policy)

    assert _admit({"freeBytes": None}, 1) == (True, "unconstrained")
    assert _admit({"freeBytes": 10, "totalBytes": 100}, None) == (False, "capacity-evidence-unavailable")
    assert _admit({"freeBytes": 10, "totalBytes": 100}, -1) == (False, "invalid-required-bytes")
    assert "hard-watermark" in _admit({"freeBytes": 5, "usedBytes": 95, "totalBytes": 100}, 1)[1]
    assert "insufficient-space" in _admit({"freeBytes": 5, "usedBytes": 0, "totalBytes": 0}, 6, {"placement": {"minFreeBytes": 0}})[1]
    assert "min-free-bytes" in _admit({"freeBytes": 20, "usedBytes": 0, "totalBytes": 0}, 5)[1]
    assert "min-free-percent" in _admit(
        {"freeBytes": 20, "usedBytes": 80, "totalBytes": 100},
        15,
        {"placement": {"hardWatermarkPercent": 99, "minFreeBytes": 1, "minFreePercent": 10}},
    )[1]
    assert _admit(
        {"freeBytes": 80, "usedBytes": 20, "totalBytes": 100},
        10,
        {"placement": {"minFreeBytes": 1, "minFreePercent": 1}},
    ) == (True, "admitted")

    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None, "totalBytes": None}):
        assert backup_capacity.estimate_target_exhaustion_horizon("target", "policy")["status"] == "unconstrained"
    with (
        patch.object(
            backup_capacity,
            "get_target_capacity",
            side_effect=[
                {"freeBytes": 1, "totalBytes": 100, "freePercent": 5.0},
                {"freeBytes": 600 * 1024 * 1024, "totalBytes": 4 * 1024**3, "freePercent": 15.0},
                {"freeBytes": 900 * 1024 * 1024, "totalBytes": 1024**3, "freePercent": 90.0},
            ],
        ),
        patch.object(
            backup_capacity.backup_dr_ledger,
            "list_logical_recovery_copies",
            return_value=[{"physicalBytes": 20 * 1024 * 1024}, {"metadata": {"ciphertextBytes": 40 * 1024 * 1024}}],
        ),
    ):
        assert backup_capacity.estimate_target_exhaustion_horizon("critical", "policy")["status"] == "critical"
        assert backup_capacity.estimate_target_exhaustion_horizon("degraded", "policy")["status"] == "degraded"
        assert backup_capacity.estimate_target_exhaustion_horizon("healthy", "policy")["status"] == "healthy"

    targets = [{"targetId": "critical", "label": "C", "kind": "s3"}, {"targetId": "degraded", "label": "D", "kind": "s3"}]
    with (
        patch.object(backup_capacity.backup_targets, "list_targets", return_value=targets),
        patch.object(backup_capacity.backup_targets, "probe_target_capacity", return_value={"freeBytes": 1}),
        patch.object(
            backup_capacity,
            "estimate_target_exhaustion_horizon",
            side_effect=[{"status": "critical"}, {"status": "degraded"}],
        ),
    ):
        assert backup_capacity.capacity_summary()["overallStatus"] == "critical"


def test_maintenance_lease_failure_isolation_and_supervisor_lifecycle(tmp_settings: Path) -> None:
    with patch.object(backup_maintenance.backup_control, "acquire_maintenance_lease", return_value=None):
        assert backup_maintenance.maintenance_tick(instance_id="busy") == {"leaseAcquired": False, "drainsProcessed": 0}

    stop = MagicMock()
    stop.wait.return_value = False
    with patch.object(backup_maintenance.backup_control, "renew_maintenance_lease", return_value=False) as renew:
        backup_maintenance._lease_heartbeat(stop, instance_id="heartbeat", fencing_token=7)
    renew.assert_called_once()

    common = (
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
        patch.object(backup_maintenance.backup_drain, "list_target_drain_jobs", return_value=[{"targetId": "bad-drain"}]),
        patch.object(backup_maintenance.backup_drain, "process_target_drain", side_effect=RuntimeError("drain failed")),
    )
    with common[0], common[1], common[2], common[3], common[4], common[5], common[6], common[7], common[8]:
        summary = backup_maintenance.maintenance_tick(instance_id="failure-isolation", limit_per_worker=500)
    assert summary["drainFailures"] == 1
    assert summary["drainsProcessed"] == 0

    supervisor = backup_maintenance.StorageMaintenanceSupervisor(instance_id="supervisor", tick_seconds=0, limit_per_worker=0)
    assert supervisor.tick_seconds == 0.1
    assert supervisor.limit_per_worker == 1
    fake_thread = MagicMock()
    with patch.object(backup_maintenance.threading, "Thread", return_value=fake_thread):
        supervisor.start()
        supervisor.start()
        supervisor.stop(timeout=-1)
    fake_thread.start.assert_called_once()
    fake_thread.join.assert_called_once_with(timeout=0.0)

    supervisor._stop = MagicMock()
    supervisor._stop.is_set.side_effect = [False, True]
    with patch.object(supervisor, "tick", side_effect=RuntimeError("tick failed")):
        supervisor._loop()
    supervisor._stop.wait.assert_called_once_with(supervisor.tick_seconds)


def test_drain_active_dependencies_scan_bounds_and_terminal_paths(tmp_settings: Path) -> None:
    target_id = "target_drain_edges"
    with (
        patch.object(backup_drain.backup_scheduler, "list_active_runs", return_value=[{"policyId": "p", "scheduleSlot": "slot"}]),
        patch.object(backup_drain.backup_run_plan, "read_run_plan", return_value={"selectedWriteTargetId": target_id}),
    ):
        assert backup_drain._active_run_targets(target_id)
    with (
        patch.object(backup_drain.backup_scheduler, "list_active_runs", return_value=[{"policyId": "p", "scheduleSlot": ""}]),
        patch.object(backup_drain.backup_policies, "get_policy", return_value={"targetId": target_id}),
    ):
        assert backup_drain._active_run_targets(target_id)
    with (
        patch.object(backup_drain.backup_scheduler, "list_active_runs", return_value=[{"policyId": "missing", "scheduleSlot": ""}]),
        patch.object(backup_drain.backup_policies, "get_policy", side_effect=AppError("missing", status=404)),
    ):
        assert not backup_drain._active_run_targets(target_id)

    sessions = {
        "done": {"phase": next(iter(backup_drain.backup_recovery_keeper.TERMINAL_PHASES)), "targetId": target_id},
        "live": {"phase": "restoring", "holds": [{"targetId": target_id}, "invalid"]},
    }
    with patch.object(backup_drain.backup_recovery_keeper, "scan_durable_recovery_sessions", return_value=sessions):
        assert backup_drain._active_recovery_targets(target_id)

    with patch.object(backup_drain.backup_publish, "resolve_target", side_effect=RuntimeError("offline")):
        assert backup_drain._drain_completion_blockers(target_id) == ["target-unavailable"]
    terminal_repairs = [{"phase": next(iter(backup_replication.REPAIR_TERMINAL_PHASES))}] * 500
    terminal_retirements = [{"phase": next(iter(backup_retirement.RETIREMENT_TERMINAL_PHASES))}] * 500
    with (
        patch.object(backup_drain.backup_publish, "resolve_target", return_value=SimpleNamespace()),
        patch.object(backup_drain.backup_writer_lease, "active_writer_lease", return_value=False),
        patch.object(backup_drain, "_active_run_targets", return_value=False),
        patch.object(backup_drain, "_active_recovery_targets", return_value=False),
        patch.object(backup_drain.backup_replication, "has_source_holds_for_target", return_value=False),
        patch.object(backup_drain.backup_replication, "list_repair_jobs", return_value=terminal_repairs),
        patch.object(backup_drain.backup_replication, "list_rebalance_jobs", return_value=[{"phase": "complete"}] * 500),
        patch.object(backup_drain.backup_retirement, "list_copy_retirement_jobs", return_value=terminal_retirements),
    ):
        assert backup_drain._drain_completion_blockers(target_id) == [
            "repair-scan-incomplete",
            "rebalance-scan-incomplete",
            "retirement-scan-incomplete",
        ]

    with patch.object(backup_drain, "get_target_drain_job", return_value=None):
        assert backup_drain.process_target_drain(target_id)["reason"] == "no-drain-job"
    with patch.object(backup_drain, "get_target_drain_job", return_value={"phase": "drained"}):
        assert backup_drain.process_target_drain(target_id)["status"] == "completed"
    with (
        patch.object(backup_drain, "get_target_drain_job", return_value={"phase": "requested"}),
        patch.object(backup_drain.backup_control, "acquire_maintenance_lease", return_value=None),
    ):
        assert backup_drain.process_target_drain(target_id)["reason"] == "drain-owned-by-another-worker"

    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "drain-completes")
    backup_drain.start_target_drain(target_id)
    with patch.object(backup_drain, "_drain_completion_blockers", return_value=[]):
        assert backup_drain.process_target_drain(target_id)["status"] == "drained"


def _write_formal_history(
    root: Path,
    *,
    target_id: str = "target_formal_edges",
    policy_id: str = "policy_formal_edges",
    backup_id: str = "backup_formal_edges",
    receipt_updates: dict[str, Any] | None = None,
    commit_updates: dict[str, Any] | None = None,
) -> tuple[SimpleNamespace, bytes, dict[str, Any], dict[str, Any]]:
    receipt: dict[str, Any] = {
        "schemaVersion": 4,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": "object-set-edge",
    }
    receipt.update(receipt_updates or {})
    receipt_bytes = _stable_bytes(receipt)
    commit: dict[str, Any] = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt.get("objectSetDigest"),
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    commit.update(commit_updates or {})
    commit["commitHash"] = backup_publish._commit_hash(commit)
    receipt_path = root / "receipts" / f"{backup_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)
    commit_path = root / "commits" / policy_id / f"{backup_id}.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_bytes(_stable_bytes(commit))
    return SimpleNamespace(root=root, store=None, target_id=target_id), receipt_bytes, receipt, commit


def test_retirement_formal_bindings_marker_conflicts_and_payload_inventory(tmp_settings: Path) -> None:
    empty_target = SimpleNamespace(root=tmp_settings / "formal-missing", store=None, target_id="target")
    with pytest.raises(AppError, match="receipt-missing"):
        backup_retirement._read_formal_metadata(empty_target, "policy", "backup")

    cases: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        ({}, {"receiptDigest": "bad"}, "receipt-commit-binding"),
        ({"backupId": "other"}, {}, "backup-binding"),
        ({"policyId": "other"}, {}, "policy-binding"),
        ({"targetId": "other"}, {}, "target-binding"),
        ({}, {"objectSetDigest": "other"}, "object-set-binding"),
    ]
    for index, (receipt_updates, commit_updates, message) in enumerate(cases):
        root = tmp_settings / f"formal-case-{index}"
        target, _, _, _ = _write_formal_history(root, receipt_updates=receipt_updates, commit_updates=commit_updates)
        with pytest.raises(AppError, match=message):
            backup_retirement._read_formal_metadata(target, "policy_formal_edges", "backup_formal_edges")

    target, receipt_bytes, receipt, commit = _write_formal_history(tmp_settings / "formal-valid")
    marker = backup_retirement.build_retirement_marker(
        target_id=target.target_id,
        policy_id=receipt["policyId"],
        backup_id=receipt["backupId"],
        receipt_bytes=receipt_bytes,
        receipt=receipt,
        commit=commit,
        retirement_job_id="retire-marker-edge",
        reason="coverage",
    )
    assert not backup_retirement.retirement_marker_valid(marker, receipt_bytes=b"not-json", commit=commit)
    assert not backup_retirement.retirement_marker_valid({**marker, "schemaVersion": 99}, receipt_bytes=receipt_bytes, commit=commit)
    assert not backup_retirement.retirement_marker_valid({**marker, "markerHash": "bad"}, receipt_bytes=receipt_bytes, commit=commit)
    with patch.object(backup_retirement.backup_publish, "commit_marker_valid", return_value=False):
        assert not backup_retirement.retirement_marker_valid(marker, receipt_bytes=receipt_bytes, commit=commit)
    assert not backup_retirement.retirement_marker_valid({**marker, "backupId": "other", "markerHash": marker["markerHash"]}, receipt_bytes=receipt_bytes, commit=commit)

    marker_path = target.root / backup_retirement.retirement_marker_key(receipt["policyId"], receipt["backupId"])
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("{}", encoding="utf-8")
    with pytest.raises(AppError, match="marker-conflict"):
        backup_retirement._write_retirement_marker(target, marker, receipt_bytes=receipt_bytes, commit=commit)
    marker_path.write_bytes(_stable_bytes(marker))
    conflicting = {**marker, "targetId": "different"}
    with pytest.raises(AppError, match="marker-conflict"):
        backup_retirement._write_retirement_marker(target, conflicting, receipt_bytes=receipt_bytes, commit=commit)

    digest = "a" * 64
    keys = backup_retirement._receipt_payload_keys(
        {
            "filename": "legacy.age",
            "objectDigest": digest,
            "objects": [{"path": "custom/object.age", "digest": digest}, "invalid"],
            "components": [{"key": "control/object.age", "ciphertextDigest": digest}],
        }
    )
    assert {"legacy.age", "custom/object.age", "control/object.age", f"ciphertext/sha256/{digest}"} <= keys


def test_retirement_dependency_rejections_failures_and_batch_classification(tmp_settings: Path) -> None:
    policy_id = "policy_retirement_edges"
    backup_id = "backup_retirement_edges"
    target_id = "target_retirement_edges"
    terminal = next(iter(backup_replication.TERMINAL_PHASES))
    with patch.object(
        backup_replication,
        "list_jobs",
        return_value=[{"phase": terminal, "primaryTargetId": target_id}, {"phase": "pending", "replicaTargetId": target_id}],
    ):
        assert backup_retirement.has_active_copy_dependency(target_id, policy_id, backup_id)
    with (
        patch.object(backup_replication, "list_jobs", return_value=[]),
        patch.object(
            backup_replication,
            "list_repair_jobs",
            return_value=[{"backupId": backup_id, "phase": "pending", "sourceTargetId": target_id}],
        ),
    ):
        assert backup_retirement.has_active_copy_dependency(target_id, policy_id, backup_id)
    with (
        patch.object(backup_replication, "list_jobs", return_value=[]),
        patch.object(backup_replication, "list_repair_jobs", return_value=[]),
        patch.object(
            backup_replication,
            "list_rebalance_jobs",
            return_value=[{"backupId": backup_id, "phase": "pending", "destTargetId": target_id}],
        ),
    ):
        assert backup_retirement.has_active_copy_dependency(target_id, policy_id, backup_id)

    base_job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id)
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": False}):
        assert backup_retirement.execute_copy_retirement_job(base_job["jobId"])["phase"] == "rejected"
    protected = backup_retirement.create_copy_retirement_job(policy_id, "backup-protected", target_id)
    with (
        patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": True}),
        patch.object(backup_replication, "is_source_held", return_value=False),
    ):
        assert backup_retirement.execute_copy_retirement_job(protected["jobId"])["phase"] == "rejected"
    waiting = backup_retirement.create_copy_retirement_job(policy_id, "backup-waiting", target_id)
    with (
        patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}),
        patch.object(backup_replication, "is_source_held", return_value=False),
        patch.object(backup_retirement, "has_active_copy_dependency", return_value=True),
    ):
        assert backup_retirement.execute_copy_retirement_job(waiting["jobId"])["phase"] == "waiting-for-dependencies"
    failed = backup_retirement.create_copy_retirement_job(policy_id, "backup-failed", target_id)
    with (
        patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}),
        patch.object(backup_replication, "is_source_held", return_value=False),
        patch.object(backup_retirement, "has_active_copy_dependency", return_value=False),
        patch.object(backup_publish, "resolve_target", side_effect=RuntimeError("target unavailable")),
    ):
        assert backup_retirement.execute_copy_retirement_job(failed["jobId"])["phase"] == "failed"

    with pytest.raises(AppError, match="not found"):
        backup_retirement.cancel_copy_retirement_job("missing")
    assert backup_retirement.cancel_copy_retirement_job(base_job["jobId"])["phase"] == "rejected"
    cancellable = backup_retirement.create_copy_retirement_job(policy_id, "backup-cancel", target_id)
    assert backup_retirement.cancel_copy_retirement_job(cancellable["jobId"], reason="stop")["phase"] == "cancelled"

    jobs = [
        {"jobId": "reclaimed", "phase": "requested"},
        {"jobId": "waiting", "phase": "requested"},
        {"jobId": "failed", "phase": "requested"},
        {"jobId": "terminal", "phase": "reclaimed"},
    ]
    with (
        patch.object(backup_retirement, "list_copy_retirement_jobs", return_value=jobs),
        patch.object(
            backup_retirement,
            "execute_copy_retirement_job",
            side_effect=[{"phase": "reclaimed"}, {"phase": "waiting-for-dependencies"}, {"phase": "failed"}],
        ),
    ):
        assert backup_retirement.process_pending_retirements(limit=3) == {
            "processed": 3,
            "reclaimed": 1,
            "waiting": 1,
            "failed": 1,
        }


def test_retirement_remote_metadata_paging_and_store_marker_conflict(tmp_settings: Path) -> None:
    target_id = "target_retirement_remote_edges"
    policy_id = "policy_retirement_remote_edges"
    backup_id = "backup_retirement_remote_edges"
    receipt = {"schemaVersion": 4, "targetId": target_id, "policyId": policy_id, "backupId": backup_id}
    receipt_bytes = _stable_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    store = MemoryTargetStore()
    store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
    store.put_if_absent(f"commits/{policy_id}/commit.json", _stable_bytes(commit))
    target = SimpleNamespace(root=None, store=store, target_id=target_id)
    loaded_bytes, loaded_receipt, loaded_commit = backup_retirement._read_formal_metadata(target, policy_id, backup_id)
    assert loaded_bytes == receipt_bytes
    assert loaded_receipt == receipt
    assert loaded_commit["commitHash"] == commit["commitHash"]

    marker = backup_retirement.build_retirement_marker(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        receipt_bytes=receipt_bytes,
        receipt=receipt,
        commit=commit,
        retirement_job_id="remote-marker",
        reason="coverage",
    )
    store.put_if_absent(backup_retirement.retirement_marker_key(policy_id, backup_id), b"{}")
    with pytest.raises(AppError, match="marker-conflict"):
        backup_retirement._write_retirement_marker(target, marker, receipt_bytes=receipt_bytes, commit=commit)


def test_retirement_retained_reference_scans_and_active_job_sources(tmp_settings: Path) -> None:
    digest = "b" * 64
    retiring = {"backupId": "backup-retiring", "policyId": "policy-retained", "filename": "retiring.age"}
    retained = {"backupId": "backup-retained", "policyId": "policy-retained", "objects": [{"digest": digest}]}
    root = tmp_settings / "retained-filesystem"
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "retiring.json").write_bytes(_stable_bytes(retiring))
    (receipts / "retained.json").write_bytes(_stable_bytes(retained))
    (receipts / "invalid.json").write_bytes(b"not-json")
    filesystem_target = SimpleNamespace(root=root, store=None, target_id="target-retained-filesystem")
    # Malformed formal receipt must fail closed — never treat as "unreferenced".
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate):
        backup_retirement._retained_payload_keys(
            filesystem_target,
            retiring_backup_id="backup-retiring",
        )
    (receipts / "invalid.json").unlink()
    filesystem_keys = backup_retirement._retained_payload_keys(
        filesystem_target,
        retiring_backup_id="backup-retiring",
    )
    assert any(digest in key for key in filesystem_keys)

    store = MemoryTargetStore()
    store.put_if_absent("receipts/retiring.json", _stable_bytes(retiring))
    store.put_if_absent("receipts/retained.json", _stable_bytes(retained))
    store.put_if_absent("receipts/invalid.json", b"[]")  # JSON array is not a receipt object
    remote_target = SimpleNamespace(root=None, store=store, target_id="target-retained-remote")
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate):
        backup_retirement._retained_payload_keys(remote_target, retiring_backup_id="backup-retiring")
    store = MemoryTargetStore()
    store.put_if_absent("receipts/retiring.json", _stable_bytes(retiring))
    store.put_if_absent("receipts/retained.json", _stable_bytes(retained))
    remote_target = SimpleNamespace(root=None, store=store, target_id="target-retained-remote")
    remote_keys = backup_retirement._retained_payload_keys(remote_target, retiring_backup_id="backup-retiring")
    assert any(digest in key for key in remote_keys)

    policy_id = "policy-active-retirement"
    backup_id = "backup-active-retirement"
    target_id = "target-active-retirement"
    with (
        patch.object(backup_replication, "list_jobs", return_value=[]),
        patch.object(backup_replication, "list_repair_jobs", return_value=[]),
        patch.object(backup_replication, "list_rebalance_jobs", return_value=[]),
        patch.object(
            backup_retirement,
            "list_copy_retirement_jobs",
            return_value=[
                {"jobId": "excluded", "backupId": backup_id, "phase": "requested"},
                {"jobId": "other", "backupId": "other", "phase": "requested"},
                {"jobId": "active", "backupId": backup_id, "phase": "requested"},
            ],
        ),
    ):
        assert backup_retirement.has_active_copy_dependency(
            target_id,
            policy_id,
            backup_id,
            excluding_job_id="excluded",
        )

    from deepseek_infra.infra.workspace import backup_recovery_job, backups

    restore_dir = backups.RESTORE_DIR / "restore-active"
    restore_dir.mkdir(parents=True)
    (backups.RESTORE_DIR / "restore-invalid").mkdir(parents=True)
    (backups.RESTORE_DIR / "restore-invalid" / "remote-fetch.json").write_text("not-json", encoding="utf-8")
    (restore_dir / "remote-fetch.json").write_text(
        json.dumps(
            {
                "phase": "fetching",
                "activeSourceTargetId": target_id,
                "backupId": backup_id,
            }
        ),
        encoding="utf-8",
    )
    with (
        patch.object(backup_replication, "list_jobs", return_value=[]),
        patch.object(backup_replication, "list_repair_jobs", return_value=[]),
        patch.object(backup_replication, "list_rebalance_jobs", return_value=[]),
        patch.object(backup_retirement, "list_copy_retirement_jobs", return_value=[]),
    ):
        assert "complete" in backup_recovery_job.TERMINAL_PHASES
        assert backup_retirement.has_active_copy_dependency(target_id, policy_id, backup_id)


def test_retirement_valid_remote_marker_and_filesystem_commit_scan(tmp_settings: Path) -> None:
    target_id = "target-valid-retired"
    policy_id = "policy-valid-retired"
    backup_id = "backup-valid-retired"
    receipt = {
        "schemaVersion": 4,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": "set-valid-retired",
    }
    receipt_bytes = _stable_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    marker = backup_retirement.build_retirement_marker(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        receipt_bytes=receipt_bytes,
        receipt=receipt,
        commit=commit,
        retirement_job_id="retire-valid-marker",
        reason="coverage",
    )
    store = MemoryTargetStore()
    store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
    store.put_if_absent(f"commits/{policy_id}/commit.json", _stable_bytes(commit))
    store.put_if_absent(backup_retirement.retirement_marker_key(policy_id, backup_id), _stable_bytes(marker))
    target = SimpleNamespace(root=None, store=store, target_id=target_id)
    assert backup_retirement._receipt_has_valid_retirement_marker(target, receipt_bytes, receipt)
    backup_retirement._write_retirement_marker(target, marker, receipt_bytes=receipt_bytes, commit=commit)
    assert not backup_retirement._receipt_has_valid_retirement_marker(
        target,
        receipt_bytes,
        {"policyId": "", "backupId": backup_id},
    )

    root = tmp_settings / "commit-scan"
    commits = root / "commits" / policy_id
    commits.mkdir(parents=True)
    (commits / "invalid.json").write_text("not-json", encoding="utf-8")
    (commits / "unrelated.json").write_text(json.dumps({"backupId": "other"}), encoding="utf-8")
    matching = commits / "matching.json"
    matching.write_text(json.dumps(commit), encoding="utf-8")
    assert backup_retirement._filesystem_commit_path(root, policy_id, backup_id, receipt) == matching


def _multipart_source(tmp_settings: Path, name: str, data: bytes) -> SimpleNamespace:
    root = tmp_settings / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "ciphertext.age").write_bytes(data)
    return SimpleNamespace(root=root, store=None)


def _remote_part(number: int, chunk: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(chunk).hexdigest()
    return {"number": number, "etag": digest, "size": len(chunk), "checksumSha256": digest}


def test_publish_multipart_reconciliation_conflict_matrix_and_missing_upload(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_spool, "SPOOL_DIR", tmp_settings / ".backup-spool")
    data = b"abcd"
    source = tmp_settings / "publish-multipart-source.age"
    source.write_bytes(data)
    package = SimpleNamespace(path=source, size=len(data), ciphertext_sha256=hashlib.sha256(data).hexdigest())
    etag = hashlib.sha256(data).hexdigest()
    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
    valid_remote = {"partNumber": 1, "etag": etag, "size": len(data), "checksumSHA256": checksum}
    valid_local = dict(valid_remote)

    cases: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = [
        ([{"partNumber": 2}], [], "non-contiguous-local-part"),
        ([], [{"partNumber": 2}], "non-contiguous-remote-part"),
        ([], [valid_remote, {**valid_remote, "partNumber": 2}], "remote-parts-exceed-source"),
        ([], [{**valid_remote, "size": 3}], "size-mismatch"),
        ([], [{**valid_remote, "checksumSHA256": "bad"}], "checksum-mismatch"),
        ([{**valid_local, "size": 3}], [valid_remote], "local-remote-size-conflict"),
        ([{**valid_local, "etag": "bad"}], [valid_remote], "local-remote-etag-conflict"),
        ([{**valid_local, "checksumSHA256": "bad"}], [valid_remote], "local-remote-checksum-conflict"),
    ]
    for local_parts, remote_parts, expected in cases:
        reason = backup_publish._validate_resumable_remote_parts(
            package,
            size=len(data),
            part_size=len(data),
            local_parts=local_parts,
            remote_parts=remote_parts,
        )
        assert reason is not None and expected in reason
    assert (
        backup_publish._validate_resumable_remote_parts(
            package,
            size=len(data),
            part_size=len(data),
            local_parts=[valid_local],
            remote_parts=[valid_remote],
        )
        is None
    )
    truncated = tmp_settings / "publish-multipart-truncated.age"
    truncated.write_bytes(data[:-1])
    assert backup_publish._validate_resumable_remote_parts(
        SimpleNamespace(path=truncated),
        size=len(data),
        part_size=len(data),
        local_parts=[],
        remote_parts=[valid_remote],
    ) == "source-part-1-truncated"

    class MissingUploadStore(MemoryTargetStore):
        def list_multipart_parts(self, upload: MultipartUpload) -> list[dict[str, Any]]:
            del upload
            raise AppError("multipart-upload-not-found", code=ErrorCode.NOT_FOUND, status=404)

    missing_store = MissingUploadStore()
    backup_spool.write_multipart_state(
        "publish-missing-policy",
        "publish-missing-slot",
        {
            "key": "objects/publish-missing.age",
            "uploadId": "provider-aborted-upload",
            "parts": [],
            "partSize": len(data),
        },
        target_id="target-missing",
    )
    backup_publish._upload_object_resumable(
        missing_store,
        package,
        obj_key="objects/publish-missing.age",
        policy_id="publish-missing-policy",
        slot_digest="publish-missing-slot",
        dest_target_id="target-missing",
    )
    missing_state = backup_spool.read_multipart_state(
        "publish-missing-policy",
        "publish-missing-slot",
        target_id="target-missing",
    )
    assert missing_state is not None
    assert missing_state["multipartRestart"]["reason"] == "provider-upload-not-found"

    backup_spool.write_multipart_state(
        "publish-no-list-policy",
        "publish-no-list-slot",
        {"key": "objects/publish-no-list.age", "uploadId": "upload-no-list", "parts": []},
    )
    no_list_store: Any = SimpleNamespace(list_multipart_parts=None)
    with pytest.raises(AppError, match="cannot reconcile"):
        backup_publish._upload_object_resumable(
            no_list_store,
            package,
            obj_key="objects/publish-no-list.age",
            policy_id="publish-no-list-policy",
            slot_digest="publish-no-list-slot",
        )


def test_multipart_reconciliation_conflict_matrix_and_unexpected_provider_error(tmp_settings: Path) -> None:
    data = b"abcdefgh"
    source = _multipart_source(tmp_settings, "multipart-conflict-matrix", data)
    expected_digest = hashlib.sha256(data).hexdigest()
    first = _remote_part(1, data[:4])
    second = _remote_part(2, data[4:])
    conflict_cases = [
        ([], [first], 4, "remote-part-count-behind"),
        ([{**first, "number": 2}], [], 0, "non-contiguous-remote-part"),
        ([first, second, _remote_part(3, b"extra")], [], 0, "remote-parts-exceed-source"),
        ([first], [{**first, "number": 2}], 4, "non-contiguous-local-part"),
        ([first], [{**first, "size": 3}], 4, "local-remote-size-conflict"),
        ([first], [{**first, "etag": "f" * 64}], 4, "local-remote-etag-conflict"),
        ([first], [{**first, "checksumSha256": "f" * 64}], 4, "local-checksum-conflict"),
        ([first], [first], 3, "local-offset-conflict"),
    ]
    for index, (remote_parts, local_parts, next_offset, reason) in enumerate(conflict_cases):
        store = MemoryTargetStore()
        upload = store.begin_multipart("ciphertext.age", checksum_sha256=expected_digest)
        progress: dict[str, Any] = {
            "multipartUploadId": upload.upload_id,
            "parts": local_parts,
            "nextOffset": next_offset,
        }
        with (
            patch.object(store, "list_multipart_parts", return_value=remote_parts),
            pytest.raises(AppError, match="multipart-reconciliation-conflict"),
        ):
            backup_replication.stream_ciphertext_transfer(
                source,
                SimpleNamespace(root=None, store=store),
                "ciphertext.age",
                "ciphertext.age",
                expected_digest,
                chunk_size=4,
                progress_state=progress,
            )
        assert reason in progress["multipartQuarantine"]["reason"], index

    store = MemoryTargetStore()
    upload = store.begin_multipart("ciphertext.age", checksum_sha256=expected_digest)
    with (
        patch.object(store, "list_multipart_parts", side_effect=RuntimeError("provider permission denied")),
        pytest.raises(RuntimeError, match="permission denied"),
    ):
        backup_replication.stream_ciphertext_transfer(
            source,
            SimpleNamespace(root=None, store=store),
            "ciphertext.age",
            "ciphertext.age",
            expected_digest,
            chunk_size=4,
            progress_state={"multipartUploadId": upload.upload_id, "parts": [], "nextOffset": 0},
        )


def test_multipart_callbacks_empty_single_and_abort_paths(tmp_settings: Path) -> None:
    data = b"abcdefghijkl"
    source = _multipart_source(tmp_settings, "multipart-callbacks", data)
    digest = hashlib.sha256(data).hexdigest()
    store = MemoryTargetStore()
    upload = store.begin_multipart("ciphertext.age", checksum_sha256=digest)
    store.upload_part(upload, 1, data[:4], checksum_sha256=hashlib.sha256(data[:4]).hexdigest())
    store.upload_part(upload, 2, data[4:8], checksum_sha256=hashlib.sha256(data[4:8]).hexdigest())
    first = _remote_part(1, data[:4])
    callbacks: list[tuple[int, str, int]] = []
    progress: dict[str, Any] = {
        "multipartUploadId": upload.upload_id,
        "parts": [first],
        "nextOffset": 4,
    }
    assert backup_replication.stream_ciphertext_transfer(
        source,
        SimpleNamespace(root=None, store=store),
        "ciphertext.age",
        "ciphertext.age",
        digest,
        chunk_size=4,
        progress_state=progress,
        on_part=lambda number, etag, size: callbacks.append((number, etag, size)),
    ) == len(data)
    assert [item[0] for item in callbacks] == [2, 3]

    empty_source = _multipart_source(tmp_settings, "multipart-empty", b"")
    empty_store = MemoryTargetStore()
    assert backup_replication.stream_ciphertext_transfer(
        empty_source,
        SimpleNamespace(root=None, store=empty_store),
        "ciphertext.age",
        "ciphertext.age",
        hashlib.sha256(b"").hexdigest(),
        chunk_size=4,
        progress_state={},
    ) == 0

    single_source = _multipart_source(tmp_settings, "multipart-single-bad", b"abcd")
    with pytest.raises(AppError, match="source component corrupt"):
        backup_replication.stream_ciphertext_transfer(
            single_source,
            SimpleNamespace(root=None, store=MemoryTargetStore()),
            "ciphertext.age",
            "ciphertext.age",
            "0" * 64,
            chunk_size=4,
            progress_state={},
        )

    bad_store = MemoryTargetStore()
    with pytest.raises(AppError, match="source component corrupt"):
        backup_replication.stream_ciphertext_transfer(
            _multipart_source(tmp_settings, "multipart-multi-bad", b"abcdefgh"),
            SimpleNamespace(root=None, store=bad_store),
            "ciphertext.age",
            "ciphertext.age",
            "0" * 64,
            chunk_size=4,
            progress_state={},
        )

    class FailingUploadStore(MemoryTargetStore):
        def upload_part(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            raise RuntimeError("upload failed")

    failing_store = FailingUploadStore()
    with pytest.raises(RuntimeError, match="upload failed"):
        backup_replication.stream_ciphertext_transfer(
            _multipart_source(tmp_settings, "multipart-untracked-failure", b"abcdefgh"),
            SimpleNamespace(root=None, store=failing_store),
            "ciphertext.age",
            "ciphertext.age",
            hashlib.sha256(b"abcdefgh").hexdigest(),
            chunk_size=4,
            progress_state=None,
        )


def test_replication_source_hold_local_remote_and_fail_closed_scans(tmp_settings: Path) -> None:
    target_id = "target-hold-edges"
    policy_id = "policy-hold-edges"
    backup_id = "backup-hold-edges"
    target_root = tmp_settings / "hold-target-root"
    target_root.mkdir()
    hold = backup_replication.acquire_source_hold(
        target_id,
        policy_id,
        backup_id,
        "holder-local",
        target_root=target_root,
        object_set_digest="set-hold",
    )
    assert backup_replication.is_source_held(target_id, policy_id, backup_id)
    assert backup_replication.has_source_holds_for_target(target_id)
    previous_generation = hold.generation
    hold.renew(duration_seconds=1)
    assert hold.generation == previous_generation + 1
    assert backup_replication.is_source_held(
        target_id,
        policy_id,
        backup_id,
        now=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
    )
    hold.release()
    assert not backup_replication.is_source_held(target_id, policy_id, backup_id)

    store = MemoryTargetStore()
    remote_hold = backup_replication.acquire_source_hold(
        target_id,
        policy_id,
        backup_id,
        "holder-remote",
        target_store=store,
    )
    assert remote_hold.etag
    remote_hold.renew()
    assert backup_replication.is_source_held(
        target_id,
        policy_id,
        backup_id,
        target=SimpleNamespace(root=None, store=store),
    )
    assert backup_replication.has_source_holds_for_target(
        target_id,
        target=SimpleNamespace(root=None, store=store),
    )
    remote_hold.release()

    manual_hold = backup_replication.SourceHold(
        "hold-manual-remote",
        target_id,
        policy_id,
        backup_id,
        "holder-manual",
        target_store=store,
        expires_at="2999-01-01T00:00:00Z",
        generation=1,
        etag=None,
    )
    manual_hold.renew()
    manual_hold.renew()
    manual_hold.release()

    invalid_local = backup_replication.HOLDS_DIR / "invalid.json"
    invalid_local.parent.mkdir(parents=True, exist_ok=True)
    invalid_local.write_text("not-json", encoding="utf-8")
    assert backup_replication.has_source_holds_for_target("unrelated")
    invalid_local.unlink()

    root_hold_dir = target_root / "holds" / "repair"
    root_hold_dir.mkdir(parents=True, exist_ok=True)
    (root_hold_dir / "invalid.json").write_text("not-json", encoding="utf-8")
    assert backup_replication.has_source_holds_for_target(
        target_id,
        target=SimpleNamespace(root=target_root, store=None),
    )

    class BrokenHoldStore(MemoryTargetStore):
        def list_objects(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("provider unavailable")

    assert backup_replication.has_source_holds_for_target(
        target_id,
        target=SimpleNamespace(root=None, store=BrokenHoldStore()),
    )
    assert not backup_replication.has_source_holds_for_target(target_id, target=None)


def test_replication_catalog_and_authentication_status_matrix(tmp_settings: Path) -> None:
    policy_id = "policy-auth-matrix"
    backup_id = "backup-auth-matrix"
    digest = "d" * 64
    receipt = {
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": digest,
    }
    receipt_bytes = _stable_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": digest,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
    }

    def _authenticate(receipt_raw: bytes | None, commit_raw: bytes | None, expected: str | None = None) -> str:
        store = MemoryTargetStore()
        if receipt_raw is not None:
            store.put_if_absent(f"receipts/{backup_id}.json", receipt_raw)
        if commit_raw is not None:
            store.put_if_absent(f"commits/{policy_id}/{backup_id}.json", commit_raw)
        status, _, _ = backup_replication.authenticate_recovery_copy(
            SimpleNamespace(root=None, store=store),
            policy_id,
            backup_id,
            expected_object_set_digest=expected,
        )
        return status

    assert _authenticate(None, None) == "missing"
    assert _authenticate(receipt_bytes, None) == "corrupt"
    assert _authenticate(None, _stable_bytes(commit)) == "corrupt"
    assert _authenticate(b"not-json", b"not-json") == "corrupt"
    assert _authenticate(b"[]", b"[]") == "corrupt"
    assert _authenticate(receipt_bytes, _stable_bytes({**commit, "receiptDigest": "bad"})) == "corrupt"
    assert _authenticate(receipt_bytes, _stable_bytes({**commit, "schemaVersion": 99})) == "corrupt"
    assert _authenticate(receipt_bytes, _stable_bytes({**commit, "policyId": "other"})) == "conflicting"
    wrong_receipt = _stable_bytes({**receipt, "backupId": "other"})
    assert _authenticate(wrong_receipt, _stable_bytes({**commit, "receiptDigest": hashlib.sha256(wrong_receipt).hexdigest()})) == "conflicting"
    no_digest_receipt = _stable_bytes({"schemaVersion": 4, "policyId": policy_id, "backupId": backup_id})
    assert _authenticate(
        no_digest_receipt,
        _stable_bytes({**commit, "receiptDigest": hashlib.sha256(no_digest_receipt).hexdigest()}),
    ) == "corrupt"
    assert _authenticate(receipt_bytes, _stable_bytes({**commit, "objectSetDigest": "other"})) == "corrupt"
    assert _authenticate(receipt_bytes, _stable_bytes(commit), expected="other") == "conflicting"
    assert _authenticate(receipt_bytes, _stable_bytes(commit), expected=digest) == "authenticated"

    filesystem_root = tmp_settings / "catalog-filesystem"
    filesystem_root.mkdir()
    filesystem_target = SimpleNamespace(root=filesystem_root, store=None)
    backup_replication.append_target_local_catalog(filesystem_target, {})
    backup_replication.append_target_local_catalog(filesystem_target, receipt)
    assert (filesystem_root / "catalogs" / f"{policy_id}.jsonl").is_file()
    catalog_store = MemoryTargetStore()
    backup_replication.append_target_local_catalog(SimpleNamespace(root=None, store=catalog_store), receipt)
    assert catalog_store.stat(f"catalogs/{policy_id}/{backup_id}.json") is not None


def test_replication_destination_verification_and_part_helper_edges(tmp_settings: Path) -> None:
    payload = b"destination-payload"
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_settings / "destination-verify"
    root.mkdir()
    target = SimpleNamespace(root=root, store=None)
    assert backup_replication._verify_destination_component(target, "missing.age", digest) == (False, False)
    (root / "payload.age").write_bytes(payload)
    assert backup_replication._verify_destination_component(target, "payload.age", digest) == (True, False)
    assert backup_replication._verify_destination_component(target, "payload.age", "0" * 64) == (False, True)

    store = MemoryTargetStore()
    store.put_if_absent("payload.age", payload, checksum_sha256=digest)
    remote = SimpleNamespace(root=None, store=store)
    assert backup_replication._verify_destination_component(remote, "missing.age", digest) == (False, False)
    assert backup_replication._verify_destination_component(remote, "payload.age", digest) == (True, False)
    assert backup_replication._verify_destination_component(SimpleNamespace(root=None, store=None), "x", digest) == (False, False)

    chunk = b"part"
    checksum_b64 = __import__("base64").b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
    assert backup_replication._part_matches_source({"size": len(chunk), "checksumSHA256": checksum_b64}, chunk) == (
        True,
        "provider-checksum",
    )
    assert backup_replication._part_matches_source({"size": len(chunk), "checksumSHA256": "bad"}, chunk)[0] is False
    assert backup_replication._part_matches_source({"size": 1}, chunk)[0] is False
    provider, progress = backup_replication._upload_result_part(
        SimpleNamespace(part_number=2, etag="etag", size=len(chunk), checksum_sha256="sum"),
        1,
        chunk,
    )
    assert provider["partNumber"] == 2
    assert progress["checksumSha256"] == hashlib.sha256(chunk).hexdigest()


def test_replication_committed_copy_and_exact_parent_mismatch_matrix(tmp_settings: Path) -> None:
    policy_id = "policy-parent-matrix"
    backup_id = "backup-parent-matrix"
    digest = "e" * 64
    receipt: dict[str, Any] = {
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": digest,
        "lineageId": "lineage-one",
    }
    receipt_bytes = _stable_bytes(receipt)
    commit: dict[str, Any] = {
        "schemaVersion": 4,
        "targetGeneration": 7,
        "previousCommitHash": "1" * 64,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": digest,
        "lineageId": "lineage-one",
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)

    def _target(commit_value: dict[str, Any]) -> SimpleNamespace:
        store = MemoryTargetStore()
        store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
        store.put_if_absent(f"commits/{policy_id}/{backup_id}.json", _stable_bytes(commit_value))
        return SimpleNamespace(root=None, store=store)

    status, _, _ = backup_replication.authenticate_committed_copy(
        _target(commit),
        policy_id,
        backup_id,
        expected_previous_commit_hash="1" * 64,
        expected_target_generation=7,
    )
    assert status == "authenticated"
    assert backup_replication.authenticate_committed_copy(
        _target({**commit, "commitHash": "bad"}), policy_id, backup_id
    )[0] == "corrupt"
    assert backup_replication.authenticate_committed_copy(
        _target(commit), policy_id, backup_id, expected_previous_commit_hash="2" * 64
    )[0] == "conflicting"
    assert backup_replication.authenticate_committed_copy(
        _target(commit), policy_id, backup_id, expected_target_generation=8
    )[0] == "conflicting"

    missing = SimpleNamespace(root=None, store=MemoryTargetStore())
    assert backup_replication.authenticate_transition_parent(
        missing,
        policy_id,
        expected_parent_backup_id=backup_id,
    )[1] == "parent-copy-status-missing"
    assert backup_replication.authenticate_transition_parent(
        _target(commit),
        policy_id,
        expected_parent_backup_id=backup_id,
        expected_receipt_digest="bad",
    )[1] == "parent-receipt-digest-mismatch"
    assert backup_replication.authenticate_transition_parent(
        _target(commit),
        policy_id,
        expected_parent_backup_id=backup_id,
        expected_commit_hash="bad",
    )[1] == "parent-commit-hash-mismatch"
    assert backup_replication.authenticate_transition_parent(
        _target(commit),
        policy_id,
        expected_parent_backup_id=backup_id,
        expected_lineage_id="lineage-other",
    )[1] == "parent-lineage-mismatch"
    assert backup_replication.authenticate_transition_parent(
        _target(commit),
        policy_id,
        expected_parent_backup_id=backup_id,
        expected_receipt_digest=commit["receiptDigest"],
        expected_commit_hash=commit["commitHash"],
        expected_lineage_id="lineage-one",
        expected_object_set_digest=digest,
    ) == (True, "authenticated")


def test_remote_corrupt_quarantine_multipart_and_conditional_delete_cas(tmp_settings: Path) -> None:
    good = b"good-ciphertext-payload"
    expected_digest = hashlib.sha256(good).hexdigest()
    source = _multipart_source(tmp_settings, "quarantine-source", good)

    class ChunkedStore(MemoryTargetStore):
        def get_stream(self, key: str, *, offset: int = 0) -> Iterator[bytes]:
            raw = self.get_bytes(key) or b""
            return iter(raw[index : index + 3] for index in range(offset, len(raw), 3))

    store = ChunkedStore()
    store.put_if_absent("ciphertext.age", b"corrupt-remote-payload")
    transferred = backup_replication.quarantine_and_replace_corrupt_remote_object(
        SimpleNamespace(root=None, store=store),
        "ciphertext.age",
        expected_digest,
        source,
        "ciphertext.age",
    )
    assert transferred == len(good)
    assert store.get_bytes("ciphertext.age") == good
    assert any(item.key.startswith(".quarantine/") for item in store.list_objects(".quarantine/").objects)

    class QuarantineUploadFailureStore(ChunkedStore):
        def upload_part(
            self,
            upload: MultipartUpload,
            part_number: int,
            data: bytes,
            *,
            checksum_sha256: str | None = None,
        ) -> dict[str, Any]:
            if str(upload.key).startswith(".quarantine/"):
                raise RuntimeError("quarantine upload failed")
            return super().upload_part(upload, part_number, data, checksum_sha256=checksum_sha256)

    failure_store = QuarantineUploadFailureStore()
    failure_store.put_if_absent("ciphertext.age", b"corrupt-remote-payload")
    assert backup_replication.quarantine_and_replace_corrupt_remote_object(
        SimpleNamespace(root=None, store=failure_store),
        "ciphertext.age",
        expected_digest,
        source,
        "ciphertext.age",
    ) == len(good)

    class CasFailureStore(ChunkedStore):
        def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool:
            del key, expected_etag
            return False

    cas_store = CasFailureStore()
    cas_store.put_if_absent("ciphertext.age", b"corrupt-remote-payload")
    with pytest.raises(AppError, match="conditional-delete-corrupt-object-failed"):
        backup_replication.quarantine_and_replace_corrupt_remote_object(
            SimpleNamespace(root=None, store=cas_store),
            "ciphertext.age",
            expected_digest,
            source,
            "ciphertext.age",
        )
