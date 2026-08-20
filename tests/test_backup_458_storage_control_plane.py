from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_drain,
    backup_dr_audit,
    backup_dr_ledger,
    backup_maintenance,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_scheduler,
    backup_targets,
    backup_transfer_budget,
    backup_write_continuity,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, object_key, put_json_if_absent


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake-system-temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _stable_json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_storage_control_connection_closes_after_transaction_context(tmp_settings: Path) -> None:
    connection: sqlite3.Connection | None = None
    with backup_control._connect() as active_connection:
        connection = active_connection
        assert active_connection.execute("SELECT 1").fetchone()[0] == 1
    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_maintenance_lease_and_cursor_are_durable_cas(tmp_settings: Path) -> None:
    lease = backup_control.acquire_maintenance_lease(
        "target-drain", "target-a", owner_instance_id="worker-a", lease_seconds=60
    )
    assert lease is not None
    assert backup_control.acquire_maintenance_lease(
        "target-drain", "target-a", owner_instance_id="worker-b", lease_seconds=60
    ) is None
    assert backup_control.renew_maintenance_lease(
        "target-drain",
        "target-a",
        owner_instance_id="worker-a",
        fencing_token=int(lease["fencingToken"]),
        lease_seconds=60,
    )
    assert not backup_control.renew_maintenance_lease(
        "target-drain", "target-a", owner_instance_id="worker-b", fencing_token=999, lease_seconds=60
    )

    initial = backup_control.get_maintenance_cursor("target-drain", "target-a")
    assert initial == {"cursor": None, "generation": 0}
    updated = backup_control.update_maintenance_cursor(
        "target-drain",
        "target-a",
        {"committedAt": "2026-08-20T00:00:00Z", "logicalId": "logical-500"},
        expected_generation=0,
    )
    assert updated["generation"] == 1
    with pytest.raises(AppError) as exc:
        backup_control.update_maintenance_cursor(
            "target-drain", "target-a", {"logicalId": "stale"}, expected_generation=0
        )
    assert exc.value.status == 409
    backup_control.release_maintenance_lease(
        "target-drain", "target-a", owner_instance_id="worker-a", fencing_token=int(lease["fencingToken"])
    )


def test_drain_keyset_cursor_covers_more_than_500_recovery_points(tmp_settings: Path) -> None:
    target_id = "target_drain_many"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "drain-many")
    for index in range(525):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=target_id,
            policy_id="policy-many",
            backup_id=f"backup-{index:04d}",
            committed_at=f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
            state="healthy",
            recoverable=True,
        )

    seen: set[str] = set()

    def _plan(_policy: dict[str, object], **kwargs: object) -> list[tuple[tuple[int], str]]:
        seen.add(str(kwargs["logical_recovery_point_id"]))
        return []

    backup_drain.start_target_drain(target_id)
    with patch.object(backup_scheduler, "plan_target_placement", side_effect=_plan):
        for _ in range(3):
            backup_drain.process_target_drain(
                target_id,
                instance_id="cursor-worker-a",
                scan_page_size=100,
                max_rebalances_per_step=100,
            )
        for _ in range(3):
            backup_drain.process_target_drain(
                target_id,
                instance_id="cursor-worker-b",
                scan_page_size=100,
                max_rebalances_per_step=100,
            )

    assert len(seen) == 525
    cursor = backup_control.get_maintenance_cursor("target-drain", target_id)
    assert cursor["cursor"] is None
    assert backup_drain.get_target_drain_job(target_id=target_id)["phase"] == "evacuating"  # type: ignore[index]


def test_drain_uses_placement_planner_and_only_enqueues_work(tmp_settings: Path) -> None:
    source_id = "target_drain_planner_source"
    first_id = "target_drain_planner_first"
    chosen_id = "target_drain_planner_chosen"
    backup_targets.register_filesystem_target(source_id, path=tmp_settings / "drain-planner-source")
    backup_targets.register_filesystem_target(first_id, path=tmp_settings / "drain-planner-first")
    backup_targets.register_filesystem_target(chosen_id, path=tmp_settings / "drain-planner-chosen")
    policy = backup_policies.create_policy(
        {
            "policyId": "policy-drain-planner",
            "name": "Drain planner",
            "targetId": source_id,
            "replication": {"enabled": True, "targets": [{"targetId": first_id}, {"targetId": chosen_id}]},
        }
    )
    logical_id = backup_dr_ledger.record_logical_recovery_copy(
        target_id=source_id,
        policy_id=str(policy["policyId"]),
        backup_id="backup-drain-planner",
        committed_at="2026-08-20T01:00:00Z",
        state="healthy",
        recoverable=True,
    )
    backup_drain.start_target_drain(source_id)
    with (
        patch.object(backup_scheduler, "plan_target_placement", return_value=[((0,), chosen_id)]) as planner,
        patch.object(backup_replication, "create_rebalance_job", return_value={"jobId": "rebalance-planned"}) as create,
        patch.object(backup_replication, "execute_rebalance_job") as execute,
    ):
        result = backup_drain.process_target_drain(source_id, scan_page_size=25)

    assert result["rebalancesTriggered"] == 1
    assert planner.call_args.kwargs["logical_recovery_point_id"] == logical_id
    assert create.call_args.kwargs["dest_target_id"] == chosen_id
    execute.assert_not_called()


def test_drain_completion_fails_closed_on_all_active_dependencies(tmp_settings: Path) -> None:
    target_id = "target_drain_blocked"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "drain-blocked")
    backup_drain.start_target_drain(target_id)
    with (
        patch.object(backup_drain.backup_writer_lease, "active_writer_lease", return_value=True),
        patch.object(backup_drain, "_active_run_targets", return_value=True),
        patch.object(backup_drain, "_active_recovery_targets", return_value=True),
        patch.object(backup_replication, "has_source_holds_for_target", return_value=True),
        patch.object(backup_replication, "list_repair_jobs", return_value=[{"phase": "transferring"}]),
        patch.object(backup_replication, "list_rebalance_jobs", return_value=[{"phase": "pending"}]),
        patch.object(backup_retirement, "list_copy_retirement_jobs", return_value=[{"phase": "requested"}]),
    ):
        result = backup_drain.process_target_drain(target_id)

    assert result["status"] == "in_progress"
    assert set(result["blockers"]) == {
        "active-writer-lease",
        "active-backup-run",
        "active-recovery",
        "active-source-hold",
        "active-repair-source",
        "active-rebalance-source",
        "pending-retirement",
    }
    assert backup_targets.get_target_drain_state(target_id) == "draining"


def test_rebalance_prune_creates_chain_preserving_retirement_job(tmp_settings: Path) -> None:
    job = {
        "jobId": "rebalance-retire-source",
        "policyId": "policy-rebalance-retire",
        "backupId": "backup-rebalance-retire",
        "sourceTargetId": "source-retire",
        "destTargetId": "dest-retire",
        "pruneSourceAfter": True,
        "phase": "pending",
    }
    with (
        patch.object(backup_replication, "read_rebalance_job", return_value=job),
        patch.object(backup_replication, "_set_rebalance_phase", side_effect=lambda current, phase, **extra: {**current, **extra, "phase": phase}),
        patch.object(backup_replication, "execute_replica_repair", return_value={"status": "success", "bytesRepaired": 1}),
        patch.object(backup_replication, "authenticate_committed_copy", return_value=("authenticated", {}, {})),
        patch.object(backup_replication.backup_publish, "resolve_target", return_value=object()),
        patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}),
        patch.object(backup_retirement, "create_copy_retirement_job", return_value={"jobId": "retirement-source"}) as retire,
        patch.object(backup_dr_ledger, "record_logical_recovery_copy") as ledger_write,
    ):
        result = backup_replication.execute_rebalance_job(str(job["jobId"]))

    assert result["status"] == "success"
    retire.assert_called_once_with(
        "policy-rebalance-retire",
        "backup-rebalance-retire",
        "source-retire",
        reason="rebalance-prune-source",
    )
    ledger_write.assert_not_called()


def test_failover_publish_enqueues_required_primary_catchup(tmp_settings: Path) -> None:
    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "policy-failover-catchup",
            "primaryTargetId": "target_primary_catchup",
            "targetId": "target_primary_catchup",
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_failover_active", "mode": "required"}],
            },
        },
        primary_target_id="target_failover_active",
        backup_id="backup-failover-catchup",
        package=SimpleNamespace(),
        run_id="run-failover-catchup",
        schedule_slot="slot-failover-catchup",
        slot_digest="digest-failover-catchup",
        primary_receipt={"objectSetDigest": "a" * 64, "objects": []},
    )

    assert len(jobs) == 1
    assert jobs[0]["replicaTargetId"] == "target_primary_catchup"
    assert jobs[0]["mode"] == "required"


def test_storage_maintenance_supervisor_advances_drains(tmp_settings: Path) -> None:
    capacity_target = "target_maintenance_capacity"
    backup_targets.register_filesystem_target(capacity_target, path=tmp_settings / "maintenance-capacity")
    with (
        patch.object(backup_maintenance.backup_recovery_keeper, "reconcile_durable_recovery_leases", return_value={"renewed": 0}),
        patch.object(backup_maintenance.backup_replication, "process_pending_jobs", return_value={"processed": 0}),
        patch.object(backup_maintenance.backup_replication, "process_pending_repairs", return_value={"processed": 0}),
        patch.object(backup_maintenance.backup_replication, "process_pending_rebalances", return_value={"processed": 0}),
        patch.object(backup_maintenance.backup_retirement, "process_pending_retirements", return_value={"processed": 0}),
        patch.object(backup_maintenance.backup_drain, "list_target_drain_jobs", return_value=[{"targetId": "target-supervised"}]),
        patch.object(backup_maintenance.backup_drain, "process_target_drain", return_value={"status": "in_progress"}) as drain,
    ):
        summary = backup_maintenance.maintenance_tick(instance_id="maintenance-test", limit_per_worker=3)

    assert summary["leaseAcquired"] is True
    assert summary["drainsProcessed"] == 1
    assert summary["capacityProbes"] == 1
    assert backup_control.get_target_capacity_observation(capacity_target)["source"] == "filesystem"  # type: ignore[index]
    drain.assert_called_once()


def test_legacy_job_workers_advance_durable_bounded_cursors(tmp_settings: Path) -> None:
    backup_replication.REPLICATION_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        job = {
            "jobId": f"replication-{index:03d}",
            "phase": "queued" if index == 11 else "committed",
        }
        (backup_replication.REPLICATION_DIR / f"replication-{index:03d}.json").write_text(
            json.dumps(job),
            encoding="utf-8",
        )
    with patch.object(
        backup_replication,
        "execute_replication_job",
        return_value={"phase": "committed"},
    ) as execute_replication:
        assert backup_replication.process_pending_jobs(limit=1)["processed"] == 0
        first_cursor = backup_control.get_maintenance_cursor("replication-jobs", "global")
        assert first_cursor["cursor"] == {"fileName": "replication-009.json"}
        assert backup_replication.process_pending_jobs(limit=1)["processed"] == 1
    execute_replication.assert_called_once_with("replication-011", instance_id="repl-worker")
    assert backup_control.get_maintenance_cursor("replication-jobs", "global")["cursor"] is None

    backup_replication.REPAIRS_DIR.mkdir(parents=True, exist_ok=True)
    backup_replication.REBALANCE_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        repair = {
            "repairId": f"repair-{index:03d}",
            "phase": "queued" if index == 11 else "healthy",
            "attempt": 0,
            "maxAttempts": 5,
        }
        rebalance = {
            "jobId": f"rebalance-{index:03d}",
            "phase": "pending" if index == 11 else "complete",
        }
        (backup_replication.REPAIRS_DIR / f"repair-{index:03d}.json").write_text(json.dumps(repair), encoding="utf-8")
        (backup_replication.REBALANCE_DIR / f"rebalance-{index:03d}.json").write_text(
            json.dumps(rebalance),
            encoding="utf-8",
        )

    with (
        patch.object(backup_replication, "execute_repair_job_instance", return_value={"status": "success"}) as execute_repair,
        patch.object(backup_replication, "execute_rebalance_job", return_value={"status": "success"}) as execute_rebalance,
    ):
        assert backup_replication.process_pending_repairs(limit=1)["processed"] == 0
        assert backup_replication.process_pending_rebalances(limit=1)["processed"] == 0
        assert backup_replication.process_pending_repairs(limit=1)["processed"] == 1
        assert backup_replication.process_pending_rebalances(limit=1)["processed"] == 1
    execute_repair.assert_called_once_with("repair-011", instance_id="healer-worker")
    execute_rebalance.assert_called_once_with("rebalance-011", instance_id="rebalance-worker")


def _policy_cas_worker(
    policy_dir: str,
    control_dir: str,
    suffix: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    from pathlib import Path

    from deepseek_infra.core.errors import AppError
    from deepseek_infra.infra.workspace import backup_policies

    backup_policies.BACKUP_POLICY_DIR = Path(policy_dir)
    try:
        from deepseek_infra.infra.workspace import backup_control

        backup_control.CONTROL_DIR = Path(control_dir)
        backup_control.CONTROL_DB = Path(control_dir) / "control.sqlite3"
    except ImportError:
        pass
    ready.put(suffix)  # type: ignore[attr-defined]
    start.wait(timeout=10)  # type: ignore[attr-defined]
    try:
        updated = backup_policies.update_policy(
            "policy_process_cas",
            {"name": f"winner-{suffix}"},
            expected_revision=1,
        )
        results.put(("ok", suffix, int(updated["policyRevision"])))  # type: ignore[attr-defined]
    except AppError as exc:
        results.put(("error", suffix, int(exc.status)))  # type: ignore[attr-defined]


def _target_cas_worker(
    registry_dir: str,
    control_dir: str,
    target_id: str,
    suffix: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    from pathlib import Path

    from deepseek_infra.core.errors import AppError
    from deepseek_infra.infra.workspace import backup_control, backup_targets

    backup_targets.BACKUP_TARGET_DIR = Path(registry_dir)
    backup_control.CONTROL_DIR = Path(control_dir)
    backup_control.CONTROL_DB = Path(control_dir) / "control.sqlite3"
    ready.put(suffix)  # type: ignore[attr-defined]
    start.wait(timeout=10)  # type: ignore[attr-defined]
    try:
        updated = backup_targets.drain_target(
            target_id,
            reason=f"drain-{suffix}",
            expected_generation=1,
        )
        results.put(("ok", suffix, int(updated["topologyGeneration"])))  # type: ignore[attr-defined]
    except AppError as exc:
        results.put(("error", suffix, int(exc.status)))  # type: ignore[attr-defined]
    except Exception as exc:
        results.put(("exception", suffix, type(exc).__name__))  # type: ignore[attr-defined]


def _qos_consume_worker(
    control_dir: str,
    suffix: str,
    ready: object,
    start: object,
    results: object,
) -> None:
    from pathlib import Path

    from deepseek_infra.infra.workspace import backup_control, backup_transfer_budget

    backup_control.CONTROL_DIR = Path(control_dir)
    backup_control.CONTROL_DB = Path(control_dir) / "control.sqlite3"
    manager = backup_transfer_budget.TransferBudgetManager(
        global_bytes_per_second=1024 * 1024,
        reserved_recovery_bytes_per_sec=256 * 1024,
        background_max_bytes_per_sec=1024 * 1024,
        max_burst_bytes=1024 * 1024,
    )
    transfer_id = f"qos-process-{suffix}"
    manager.acquire_transfer_token(transfer_id, backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN)
    ready.put(suffix)  # type: ignore[attr-defined]
    start.wait(timeout=10)  # type: ignore[attr-defined]
    wait_seconds = manager.consume_bandwidth(transfer_id, 1024 * 1024, now=1000.0)
    results.put((suffix, wait_seconds))  # type: ignore[attr-defined]
    manager.release_transfer_token(transfer_id)


def test_copy_retirement_preserves_formal_history_and_writes_bound_marker(tmp_settings: Path) -> None:
    target_id = "target_retirement_history"
    policy_id = "policy_retirement_history"
    backup_id = "backup_retirement_history"
    target_root = tmp_settings / "retirement-history-target"
    backup_targets.register_filesystem_target(target_id, path=target_root)

    payload_rel = "objects/sha256/aa/aabbcc.age"
    payload_path = target_root / payload_rel
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(b"retirable-ciphertext")

    receipt = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": "object-set-digest-1",
        "filename": payload_rel,
        "components": [{"path": payload_rel, "digest": "aabbcc"}],
    }
    receipt_bytes = _stable_json_bytes(receipt)
    receipt_path = target_root / "receipts" / f"{backup_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)

    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-20T00:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    commit_bytes = _stable_json_bytes(commit)
    commit_path = target_root / "commits" / policy_id / f"{backup_id}.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_bytes(commit_bytes)

    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest=str(receipt["objectSetDigest"]),
        state="healthy",
        recoverable=True,
    )

    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id, reason="drain-complete")
        result = backup_retirement.execute_copy_retirement_job(job["jobId"])

    assert result["phase"] == "reclaimed", result
    assert receipt_path.read_bytes() == receipt_bytes
    assert commit_path.read_bytes() == commit_bytes
    assert not payload_path.exists()

    marker_path = target_root / "retirements" / policy_id / f"{backup_id}.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["targetId"] == target_id
    assert marker["policyId"] == policy_id
    assert marker["backupId"] == backup_id
    assert marker["receiptDigest"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert marker["commitHash"] == commit["commitHash"]
    assert marker["objectSetDigest"] == receipt["objectSetDigest"]
    assert marker["reason"] == "drain-complete"
    assert backup_retirement.retirement_marker_valid(marker, receipt_bytes=receipt_bytes, commit=commit)


def test_remote_retirement_retries_when_payload_delete_cas_loses_race(tmp_settings: Path) -> None:
    target_id = "target_retirement_cas"
    policy_id = "policy_retirement_cas"
    backup_id = "backup_retirement_cas"
    payload = b"retirement-cas-payload"
    digest = hashlib.sha256(payload).hexdigest()
    payload_key = object_key(digest)

    class RejectingDeleteStore(MemoryTargetStore):
        reject_delete = True

        def delete_if_match(self, key: str, *, expected_etag: str | None = None) -> bool:
            if self.reject_delete and key == payload_key:
                return False
            return super().delete_if_match(key, expected_etag=expected_etag)

    store = RejectingDeleteStore()
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": "retirement-cas-object-set",
        "objects": [{"digest": digest, "size": len(payload)}],
    }
    receipt_bytes = _stable_json_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-20T00:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    store.put_if_absent(payload_key, payload)
    store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
    put_json_if_absent(store, f"commits/{policy_id}/0001.json", commit)
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest=str(receipt["objectSetDigest"]),
        state="healthy",
        recoverable=True,
    )
    resolved = SimpleNamespace(target_id=target_id, root=None, store=store)
    with (
        patch.object(backup_publish, "resolve_target", return_value=resolved),
        patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}),
    ):
        job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id)
        first = backup_retirement.execute_copy_retirement_job(job["jobId"])
        assert first["phase"] == "gc-pending"
        assert first["bytesReclaimed"] == 0
        assert store.stat(payload_key) is not None

        store.reject_delete = False
        second = backup_retirement.execute_copy_retirement_job(job["jobId"])

    assert second["phase"] == "reclaimed"
    assert second["bytesReclaimed"] == len(payload)
    assert store.stat(payload_key) is None
    assert store.get_bytes(f"receipts/{backup_id}.json") == receipt_bytes
    assert store.get_bytes(f"commits/{policy_id}/0001.json") is not None


def _remote_history_fixture(target_id: str, *, with_retirement_marker: bool) -> tuple[MemoryTargetStore, str, str]:
    policy_id = f"policy_{target_id}"
    backup_id = f"backup_{target_id}"
    missing_digest = hashlib.sha256(f"missing-{target_id}".encode()).hexdigest()
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": hashlib.sha256(f"set-{target_id}".encode()).hexdigest(),
        "controlObjectDigest": missing_digest,
        "objects": [{"digest": missing_digest, "size": 123}],
        "createdAt": "2026-08-20T00:00:00Z",
    }
    receipt_bytes = _stable_json_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "controlObjectDigest": missing_digest,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-20T00:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)

    store = MemoryTargetStore()
    store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
    put_json_if_absent(store, f"commits/{policy_id}/0001.json", commit)
    put_json_if_absent(
        store,
        "control/head.json",
        {"schemaVersion": 1, "targetGeneration": 1, "latestCommitHash": commit["commitHash"]},
    )
    assert store.stat(object_key(missing_digest)) is None

    if with_retirement_marker:
        marker = backup_retirement.build_retirement_marker(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt_bytes=receipt_bytes,
            receipt=receipt,
            commit=commit,
            retirement_job_id="retire_audit_fixture",
            reason="retention-policy",
            retired_at="2026-08-20T01:00:00Z",
        )
        put_json_if_absent(store, backup_retirement.retirement_marker_key(policy_id, backup_id), marker)
    return store, policy_id, backup_id


def test_audit_distinguishes_governed_retirement_from_missing_payload_corruption(tmp_settings: Path) -> None:
    retired_target = "target_audit_retired"
    retired_store, retired_policy, retired_backup = _remote_history_fixture(retired_target, with_retirement_marker=True)
    with patch.object(backup_targets, "open_target_store", return_value=retired_store):
        retired_result = backup_dr_audit.audit_remote_target(retired_target)

    assert not any(str(item).startswith("missing-payload:") for item in retired_result["anomalies"])
    retired_copy = backup_dr_ledger.list_logical_recovery_copies(
        target_id=retired_target,
        policy_id=retired_policy,
        backup_id=retired_backup,
    )[0]
    assert retired_copy["state"] == "retired"
    assert retired_copy["recoverable"] is False

    corrupt_target = "target_audit_corrupt"
    corrupt_store, corrupt_policy, corrupt_backup = _remote_history_fixture(corrupt_target, with_retirement_marker=False)
    with patch.object(backup_targets, "open_target_store", return_value=corrupt_store):
        corrupt_result = backup_dr_audit.audit_remote_target(corrupt_target)

    assert any(str(item).startswith(f"missing-payload:{corrupt_backup}:") for item in corrupt_result["anomalies"])
    corrupt_copies = backup_dr_ledger.list_logical_recovery_copies(
        target_id=corrupt_target,
        policy_id=corrupt_policy,
        backup_id=corrupt_backup,
    )
    assert not corrupt_copies or corrupt_copies[0]["recoverable"] is False


def test_copy_retirement_waits_while_rebalance_still_references_source(tmp_settings: Path) -> None:
    target_id = "target_retirement_busy"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "retirement-busy-target")
    policy_id = "policy_retirement_busy"
    backup_id = "backup_retirement_busy"
    active_job = {
        "jobId": "rebalance_busy",
        "policyId": policy_id,
        "backupId": backup_id,
        "sourceTargetId": target_id,
        "destTargetId": "target_elsewhere",
        "phase": "pending",
    }

    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        with patch.object(backup_replication, "list_rebalance_jobs", return_value=[active_job]):
            job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id)
            result = backup_retirement.execute_copy_retirement_job(job["jobId"])

    assert result["phase"] == "waiting-for-dependencies"
    assert "active-job" in str(result["error"])


def test_policy_revision_cas_allows_only_one_cross_process_winner(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_policies

    created = backup_policies.create_policy(
        {
            "policyId": "policy_process_cas",
            "name": "process-cas",
            "targetId": "managed-local",
        }
    )
    assert created["policyRevision"] == 1

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    control_dir = tmp_settings / ".backup-control"
    workers = [
        context.Process(
            target=_policy_cas_worker,
            args=(str(backup_policies.BACKUP_POLICY_DIR), str(control_dir), suffix, ready, start, results),
        )
        for suffix in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"a", "b"}
    start.set()
    outcomes = [results.get(timeout=10), results.get(timeout=10)]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("error") == 1
    assert next(outcome[2] for outcome in outcomes if outcome[0] == "ok") == 2
    assert next(outcome[2] for outcome in outcomes if outcome[0] == "error") == 412
    final = backup_policies.get_policy("policy_process_cas")
    assert final["policyRevision"] == 2
    assert final["name"] in {"winner-a", "winner-b"}


def test_target_topology_generation_allows_only_one_cross_process_winner(tmp_settings: Path) -> None:
    target_id = "target_process_cas"
    created = backup_targets.register_filesystem_target(target_id, path=tmp_settings / "target-process-cas")
    assert created.get("topologyGeneration", 1) == 1

    from deepseek_infra.infra.workspace import backup_control

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_target_cas_worker,
            args=(
                str(backup_targets.BACKUP_TARGET_DIR),
                str(backup_control.CONTROL_DIR),
                target_id,
                suffix,
                ready,
                start,
                results,
            ),
        )
        for suffix in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"a", "b"}
    start.set()
    outcomes = [results.get(timeout=10), results.get(timeout=10)]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("error") == 1
    assert next(outcome[2] for outcome in outcomes if outcome[0] == "ok") == 2
    assert next(outcome[2] for outcome in outcomes if outcome[0] == "error") == 412
    final = backup_targets.get_target(target_id)
    assert final["topologyGeneration"] == 2
    assert final["drainReason"] in {"drain-a", "drain-b"}


def test_placement_counts_only_the_selected_logical_recovery_point(tmp_settings: Path) -> None:
    primary_id = "target_scope_primary"
    candidate_id = "target_scope_candidate"
    backup_targets.register_filesystem_target(
        primary_id,
        path=tmp_settings / "scope-primary",
        region="region-a",
        failure_domain="region-a-1",
    )
    backup_targets.register_filesystem_target(
        candidate_id,
        path=tmp_settings / "scope-candidate",
        region="region-b",
        failure_domain="region-b-1",
    )
    policy_id = "policy_scope_current_lrp"

    # Historical copies in the candidate failure domain must not consume the
    # current replica set's maxCopiesPerFailureDomain budget.
    for index in range(5):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=candidate_id,
            policy_id=policy_id,
            backup_id=f"backup_historical_{index}",
            committed_at=f"2026-08-{10 + index:02d}T00:00:00Z",
            object_set_digest=f"historical-{index}",
            state="healthy",
            recoverable=True,
        )
    current_logical_id = backup_dr_ledger.record_logical_recovery_copy(
        target_id=primary_id,
        policy_id=policy_id,
        backup_id="backup_current",
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest="current-object-set",
        state="healthy",
        recoverable=True,
    )
    policy = {
        "policyId": policy_id,
        "replication": {"minFailureDomains": 2, "maxCopiesPerFailureDomain": 1},
        "placement": {"maxCopiesPerFailureDomain": 1, "minFreeBytes": 0, "minFreePercent": 0},
    }

    ranked = backup_scheduler.plan_target_placement(
        policy,
        candidate_target_ids=[candidate_id],
        primary_target_id=primary_id,
        logical_recovery_point_id=current_logical_id,
        required_bytes=1024,
    )

    assert [target_id for _, target_id in ranked] == [candidate_id]


def test_next_full_failover_can_reuse_target_holding_parent_copy(tmp_settings: Path) -> None:
    primary_id = "target_next_full_primary"
    candidate_id = "target_next_full_candidate"
    policy_id = "policy_next_full_failover"
    backup_targets.register_filesystem_target(
        primary_id,
        path=tmp_settings / "next-full-primary",
        region="region-a",
        failure_domain="region-a-1",
    )
    backup_targets.register_filesystem_target(
        candidate_id,
        path=tmp_settings / "next-full-candidate",
        region="region-b",
        failure_domain="region-b-1",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=candidate_id,
        policy_id=policy_id,
        backup_id="backup_parent_on_candidate",
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest="parent-object-set",
        state="healthy",
        recoverable=True,
    )
    backup_capacity.record_physical_size_evidence(
        policy_id=policy_id,
        backup_id="backup_parent_on_candidate",
        snapshot_kind="full",
        physical_bytes=1024,
    )
    policy = {
        "policyId": policy_id,
        "targetId": primary_id,
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [{"targetId": candidate_id, "mode": "required"}],
        },
    }

    def _resolve(target_id: str) -> SimpleNamespace:
        if target_id == primary_id:
            raise AppError("primary unavailable", status=503)
        return SimpleNamespace(store=None)

    with (
        patch.object(backup_publish, "resolve_target", side_effect=_resolve),
        patch.object(
            backup_write_continuity,
            "perform_liveness_preflight",
            return_value={"status": "available"},
        ),
    ):
        placement = backup_scheduler.evaluate_write_placement(policy)

    assert placement["selectedWriteTargetId"] == candidate_id
    assert placement["isFailover"] is True
    assert placement["forceFull"] is True


def test_placement_enforces_region_and_failure_domain_independently(tmp_settings: Path) -> None:
    primary_id = "target_region_primary"
    same_region_id = "target_region_same"
    second_region_id = "target_region_second"
    backup_targets.register_filesystem_target(
        primary_id,
        path=tmp_settings / "region-primary",
        region="region-a",
        failure_domain="region-a-1",
    )
    backup_targets.register_filesystem_target(
        same_region_id,
        path=tmp_settings / "region-same",
        region="region-a",
        failure_domain="region-a-2",
    )
    backup_targets.register_filesystem_target(
        second_region_id,
        path=tmp_settings / "region-second",
        region="region-b",
        failure_domain="region-b-1",
    )
    policy_id = "policy_region_scope"
    logical_id = backup_dr_ledger.record_logical_recovery_copy(
        target_id=primary_id,
        policy_id=policy_id,
        backup_id="backup_region_current",
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest="region-current-object-set",
        state="healthy",
        recoverable=True,
    )
    policy = {
        "policyId": policy_id,
        "replication": {"minFailureDomains": 2, "minRegions": 2, "maxCopiesPerFailureDomain": 2},
        "placement": {"minFreeBytes": 0, "minFreePercent": 0},
    }

    ranked = backup_scheduler.plan_target_placement(
        policy,
        candidate_target_ids=[same_region_id, second_region_id],
        primary_target_id=primary_id,
        logical_recovery_point_id=logical_id,
        required_bytes=1024,
    )

    assert [target_id for _, target_id in ranked] == [second_region_id]


def test_capacity_prediction_prefers_physical_evidence_and_force_full_unknown_fails_closed(tmp_settings: Path) -> None:
    unknown = backup_capacity.predict_next_backup_size("policy_capacity_unknown", snapshot_kind="full")
    assert unknown == {
        "predictedBytes": None,
        "capacityConfidence": "unavailable",
        "source": "no-physical-evidence",
        "isEstimate": True,
        "snapshotKind": "full",
        "sampleCount": 0,
    }

    admitted, reason = backup_capacity.check_target_capacity_admission(
        "managed-local",
        None,
        force_full=True,
    )
    assert admitted is False
    assert reason == "capacity-evidence-unavailable"

    policy_id = "policy_capacity_physical"
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_capacity_physical",
        policy_id=policy_id,
        backup_id="backup_capacity_physical",
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest="capacity-physical-object-set",
        state="healthy",
        recoverable=True,
        metadata={"logicalBytes": 600 * 1024**3, "physicalBytes": 120 * 1024**3},
    )
    prediction = backup_capacity.predict_next_backup_size(policy_id, snapshot_kind="full")
    assert prediction["predictedBytes"] == 120 * 1024**3
    assert prediction["source"] == "historical-physical-p90"
    assert prediction["capacityConfidence"] == "low"

    backup_capacity.record_physical_size_evidence(
        policy_id="policy_capacity_durable",
        backup_id="backup_capacity_durable",
        snapshot_kind="full",
        physical_bytes=321 * 1024**2,
        observed_at="2026-08-20T02:00:00Z",
    )
    durable_prediction = backup_capacity.predict_next_backup_size("policy_capacity_durable", snapshot_kind="full")
    assert durable_prediction["predictedBytes"] == 321 * 1024**2
    assert durable_prediction["capacityConfidence"] == "low"


def test_policy_and_placement_use_matching_rto_and_operator_cost_evidence(tmp_settings: Path) -> None:
    primary_id = "target_objectives_primary"
    slow_id = "target_objectives_slow"
    eligible_id = "target_objectives_eligible"
    for target_id, region, suffix in (
        (primary_id, "region-a", "primary"),
        (slow_id, "region-b", "slow"),
        (eligible_id, "region-c", "eligible"),
    ):
        backup_targets.register_filesystem_target(
            target_id,
            path=tmp_settings / f"objectives-{suffix}",
            region=region,
            failure_domain=f"{region}-1",
            provider="operator-filesystem",
            jurisdiction=region,
            storage_cost_per_gib_month=0.02,
            egress_cost_per_gib=0.01,
        )

    normalized = backup_policies.normalize_policy(
        {
            "name": "objective-policy",
            "targetId": primary_id,
            "replication": {
                "enabled": True,
                "targets": [
                    {"targetId": slow_id, "mode": "required"},
                    {"targetId": eligible_id, "mode": "required"},
                ],
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "minRegions": 2,
            },
            "recoveryObjectives": {"maxRtoSeconds": 7200},
            "costObjectives": {
                "maxEstimatedMonthlyStorageUsd": 50,
                "maxEstimatedMonthlyEgressUsd": 20,
            },
        }
    )
    assert normalized["replication"]["minRegions"] == 2
    assert normalized["recoveryObjectives"]["maxRtoSeconds"] == 7200
    assert normalized["costObjectives"]["maxEstimatedMonthlyStorageUsd"] == 50.0

    def _rto_for_target(*args: object, **kwargs: object) -> dict[str, object]:
        target_id = str(kwargs.get("target_id") or "")
        return {
            "status": "calibrated",
            "isSla": False,
            "confidence": "medium",
            "sampleCount": 5,
            "p90Seconds": 14_400 if target_id == slow_id else 3600,
        }

    with patch(
        "deepseek_infra.infra.workspace.backup_recovery_class.calibrate_rto",
        side_effect=_rto_for_target,
    ):
        ranked = backup_scheduler.plan_target_placement(
            normalized,
            candidate_target_ids=[slow_id, eligible_id],
            primary_target_id=primary_id,
            logical_recovery_point_id=None,
            required_bytes=10 * 1024**3,
        )

    assert [target_id for _, target_id in ranked] == [eligible_id]


def test_egress_estimate_uses_region_boundary_not_cost_class_label(tmp_settings: Path) -> None:
    source_id = "target_cost_source"
    same_region_id = "target_cost_same_region"
    remote_region_id = "target_cost_remote_region"
    for target_id, region in (
        (source_id, "region-a"),
        (same_region_id, "region-a"),
        (remote_region_id, "region-b"),
    ):
        backup_targets.register_filesystem_target(
            target_id,
            path=tmp_settings / target_id,
            region=region,
            failure_domain=f"{region}-1",
            cost_class="standard",
            storage_cost_per_gib_month=0.02,
            egress_cost_per_gib=0.09,
        )

    same_region = backup_capacity.estimate_transfer_cost(
        1024**3,
        source_target_id=source_id,
        dest_target_id=same_region_id,
    )
    cross_region = backup_capacity.estimate_transfer_cost(
        1024**3,
        source_target_id=source_id,
        dest_target_id=remote_region_id,
    )

    assert same_region["estimatedOneTimeTransferCost"] == 0.0
    assert cross_region["estimatedOneTimeTransferCost"] == 0.09


def test_multipart_reconciliation_adopts_verified_remote_ahead_parts(tmp_settings: Path) -> None:
    data = b"abcdefghijkl"
    source_root = tmp_settings / "multipart-source-ahead"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "ciphertext.age").write_bytes(data)
    source_target = SimpleNamespace(root=source_root, store=None)
    store = MemoryTargetStore()
    upload = store.begin_multipart("ciphertext.age", checksum_sha256=hashlib.sha256(data).hexdigest())
    first = store.upload_part(upload, 1, data[:4], checksum_sha256=hashlib.sha256(data[:4]).hexdigest())
    store.upload_part(upload, 2, data[4:8], checksum_sha256=hashlib.sha256(data[4:8]).hexdigest())
    progress: dict[str, Any] = {
        "multipartUploadId": upload.upload_id,
        "parts": [{"number": 1, "etag": first["etag"], "size": 4, "checksumSha256": hashlib.sha256(data[:4]).hexdigest()}],
        "nextOffset": 4,
    }

    transferred = backup_replication.stream_ciphertext_transfer(
        source_target,
        SimpleNamespace(root=None, store=store),
        "ciphertext.age",
        "ciphertext.age",
        hashlib.sha256(data).hexdigest(),
        chunk_size=4,
        progress_state=progress,
    )

    assert transferred == len(data)
    assert store.get_bytes("ciphertext.age") == data
    assert progress["multipartReconciliation"]["status"] == "remote-ahead-adopted"
    assert progress["nextOffset"] == len(data)
    assert [part["number"] for part in progress["parts"]] == [1, 2, 3]


def test_multipart_reconciliation_restarts_missing_provider_upload(tmp_settings: Path) -> None:
    class MissingUploadStore(MemoryTargetStore):
        def list_multipart_parts(self, upload: object) -> list[dict[str, object]]:
            del upload
            raise AppError("multipart-upload-not-found", code=ErrorCode.NOT_FOUND, status=404)

    data = b"restart-from-zero"
    source_root = tmp_settings / "multipart-source-missing"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "ciphertext.age").write_bytes(data)
    store = MissingUploadStore()
    progress: dict[str, Any] = {
        "multipartUploadId": "provider-aborted-upload",
        "parts": [{"number": 1, "etag": "stale", "size": 4}],
        "nextOffset": 4,
    }

    transferred = backup_replication.stream_ciphertext_transfer(
        SimpleNamespace(root=source_root, store=None),
        SimpleNamespace(root=None, store=store),
        "ciphertext.age",
        "ciphertext.age",
        hashlib.sha256(data).hexdigest(),
        chunk_size=4,
        progress_state=progress,
    )

    assert transferred == len(data)
    assert store.get_bytes("ciphertext.age") == data
    assert progress["multipartRestart"]["previousUploadId"] == "provider-aborted-upload"
    assert progress["multipartUploadId"] != "provider-aborted-upload"


def test_multipart_reconciliation_aborts_and_quarantines_part_conflict(tmp_settings: Path) -> None:
    data = b"abcdefgh"
    source_root = tmp_settings / "multipart-source-conflict"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "ciphertext.age").write_bytes(data)
    store = MemoryTargetStore()
    upload = store.begin_multipart("ciphertext.age", checksum_sha256=hashlib.sha256(data).hexdigest())
    store.upload_part(upload, 1, b"zzzz", checksum_sha256=hashlib.sha256(b"zzzz").hexdigest())
    progress: dict[str, Any] = {
        "multipartUploadId": upload.upload_id,
        "parts": [{"number": 1, "etag": hashlib.sha256(data[:4]).hexdigest(), "size": 4}],
        "nextOffset": 4,
    }

    with pytest.raises(AppError, match="multipart-reconciliation-conflict"):
        backup_replication.stream_ciphertext_transfer(
            SimpleNamespace(root=source_root, store=None),
            SimpleNamespace(root=None, store=store),
            "ciphertext.age",
            "ciphertext.age",
            hashlib.sha256(data).hexdigest(),
            chunk_size=4,
            progress_state=progress,
        )

    assert progress["multipartQuarantine"]["uploadId"] == upload.upload_id
    assert "remote-part-1" in progress["multipartQuarantine"]["reason"]
    assert progress.get("multipartUploadId") is None
    assert store.list_multipart_parts(upload) == []


def test_qos_global_bucket_is_shared_across_processes(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_control

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    workers = [
        context.Process(
            target=_qos_consume_worker,
            args=(str(backup_control.CONTROL_DIR), suffix, ready, start, results),
        )
        for suffix in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"a", "b"}
    start.set()
    outcomes = [results.get(timeout=10), results.get(timeout=10)]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    waits = sorted(float(outcome[1]) for outcome in outcomes)
    assert waits[0] == 0.0
    assert waits[1] >= 1.0


def test_qos_reserves_p0_bandwidth_and_enforces_independent_target_buckets(tmp_settings: Path) -> None:
    manager = backup_transfer_budget.TransferBudgetManager(
        global_bytes_per_second=1024 * 1024,
        reserved_recovery_bytes_per_sec=256 * 1024,
        background_max_bytes_per_sec=1024 * 1024,
        max_burst_bytes=1024 * 1024,
    )
    manager.acquire_transfer_token("qos-p0", backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY)
    manager.acquire_transfer_token("qos-p5", backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN)
    assert manager.consume_bandwidth("qos-p5", 768 * 1024) == 0.0
    started = time.monotonic()
    throttled = b"".join(manager.throttled_generator(iter((b"x" * (64 * 1024),)), transfer_id="qos-p5"))
    elapsed = time.monotonic() - started
    assert len(throttled) == 64 * 1024
    assert elapsed >= 0.04
    assert manager.consume_bandwidth("qos-p0", 256 * 1024) == 0.0
    manager.release_transfer_token("qos-p5")
    manager.release_transfer_token("qos-p0")

    source_id = "target_qos_source"
    dest_id = "target_qos_dest"
    backup_targets.register_filesystem_target(
        source_id,
        path=tmp_settings / "qos-source",
        max_read_bytes_per_second=512 * 1024,
    )
    backup_targets.register_filesystem_target(
        dest_id,
        path=tmp_settings / "qos-dest",
        max_write_bytes_per_second=128 * 1024,
    )
    target_manager = backup_transfer_budget.TransferBudgetManager(
        global_bytes_per_second=4 * 1024 * 1024,
        reserved_recovery_bytes_per_sec=0,
        max_burst_bytes=4 * 1024 * 1024,
    )
    target_manager.acquire_transfer_token(
        "qos-targets",
        backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
        source_target_id=source_id,
        dest_target_id=dest_id,
    )
    target_manager.wait_for_bandwidth("qos-targets", 128 * 1024)
    assert target_manager.consume_bandwidth("qos-targets", 128 * 1024) >= 0.75
    target_manager.release_transfer_token("qos-targets")
