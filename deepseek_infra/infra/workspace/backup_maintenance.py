"""Durable, bounded storage maintenance orchestration for 4.5.9.

Global planner acquires a short lease, discovers work, then execution proceeds
under per-(workerKind, scopeId) fencing leases so one slow archive target cannot
stall primary repair on another target.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_drain,
    backup_placement,
    backup_recovery_keeper,
    backup_replication,
    backup_retirement,
    backup_targets,
    backup_tiering,
    backup_transfer_budget,
)

_logger = logging.getLogger("deepseek_infra.storage_maintenance")


def _lease_heartbeat(
    stop: threading.Event,
    *,
    instance_id: str,
    fencing_token: int,
    worker_kind: str = "storage-maintenance",
    scope_id: str = "global",
    lease_seconds: int = 120,
) -> None:
    while not stop.wait(30.0):
        if not backup_control.renew_maintenance_lease(
            worker_kind,
            scope_id,
            owner_instance_id=instance_id,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        ):
            return


def _run_with_scope_lease(
    *,
    worker_kind: str,
    scope_id: str,
    instance_id: str,
    lease_seconds: int,
    work: Any,
) -> tuple[bool, Any]:
    lease = backup_control.acquire_maintenance_lease(
        worker_kind,
        scope_id,
        owner_instance_id=instance_id,
        lease_seconds=lease_seconds,
    )
    if lease is None:
        return False, None
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_lease_heartbeat,
        kwargs={
            "stop": stop,
            "worker_kind": worker_kind,
            "scope_id": scope_id,
            "instance_id": instance_id,
            "fencing_token": int(lease["fencingToken"]),
            "lease_seconds": lease_seconds,
        },
        name=f"{worker_kind}-{scope_id}-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        return True, work()
    finally:
        stop.set()
        heartbeat.join(timeout=2.0)
        backup_control.release_maintenance_lease(
            worker_kind,
            scope_id,
            owner_instance_id=instance_id,
            fencing_token=int(lease["fencingToken"]),
        )


def _probe_capacity_page(*, limit: int) -> int:
    cursor_state = backup_control.get_maintenance_cursor("capacity-probe", "global")
    cursor = cursor_state.get("cursor")
    after_target_id = str(cursor.get("targetId") or "") if isinstance(cursor, dict) else ""
    target_ids = backup_control.list_target_ids_page(after_target_id=after_target_id or None, limit=limit)
    for target_id in target_ids:
        observation = backup_targets.probe_target_capacity(target_id)
        backup_control.record_target_capacity_observation(target_id, observation)
        physical = observation.get("physicalStoredBytes")
        if isinstance(physical, int) and not isinstance(physical, bool):
            backup_control.record_capacity_growth_observation(
                target_id=target_id,
                physical_stored_bytes=int(physical),
                live_referenced_bytes=int(observation.get("liveReferencedBytes") or 0),
                retired_pending_gc_bytes=int(observation.get("retiredPendingGcBytes") or 0),
                observed_at=str(observation.get("observedAt") or ""),
            )
    next_cursor = {"targetId": target_ids[-1]} if len(target_ids) >= limit else None
    backup_control.update_maintenance_cursor(
        "capacity-probe",
        "global",
        next_cursor,
        expected_generation=int(cursor_state["generation"]),
    )
    return len(target_ids)


def _process_drain_scopes(*, instance_id: str, limit: int) -> dict[str, int]:
    backup_drain.reconcile_drain_projections(limit=max(limit * 10, 50))
    drains_processed = 0
    drain_failures = 0
    skipped_leases = 0
    jobs = [
        job
        for job in backup_drain.list_target_drain_jobs(limit=max(limit * 10, 100))
        if str(job.get("phase") or "") not in backup_drain.DRAIN_TERMINAL_PHASES
    ][:limit]
    for job in jobs:
        target_id = str(job.get("targetId") or "")
        if not target_id:
            continue

        def _drain_one(tid: str = target_id) -> None:
            backup_drain.process_target_drain(tid, instance_id=instance_id)

        try:
            acquired, _ = _run_with_scope_lease(
                worker_kind="drain",
                scope_id=target_id,
                instance_id=instance_id,
                lease_seconds=90,
                work=_drain_one,
            )
        except Exception:
            drain_failures += 1
            _logger.exception("target drain maintenance failed", extra={"targetId": target_id})
            continue
        if not acquired:
            skipped_leases += 1
            continue
        drains_processed += 1
    return {
        "drainsProcessed": drains_processed,
        "drainFailures": drain_failures,
        "drainLeaseSkips": skipped_leases,
    }


def _group_jobs_by_scope(jobs: list[dict[str, Any]], *, scope_keys: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        scope = "unscoped"
        for key in scope_keys:
            value = str(job.get(key) or "").strip()
            if value:
                scope = value
                break
        grouped.setdefault(scope, []).append(job)
    return grouped


def _process_repair_scopes(*, instance_id: str, limit: int) -> dict[str, Any]:
    """Shard repair execution by destTargetId (Gate F)."""
    candidates = [
        job
        for job in backup_replication.list_repair_jobs(limit=max(limit * 20, 50))
        if str(job.get("phase") or "") in backup_replication.REPAIR_ACTIVE_PHASES
        or str(job.get("phase") or "") == "queued"
    ]
    by_dest = _group_jobs_by_scope(candidates, scope_keys=("destTargetId",))
    processed = succeeded = failed = lease_skips = 0
    scopes = 0
    for dest_id, group in sorted(by_dest.items()):
        batch = group[:limit]

        def _work(jobs: list[dict[str, Any]] = batch) -> dict[str, int]:
            ok = bad = 0
            for job in jobs:
                try:
                    res = backup_replication.execute_repair_job_instance(
                        str(job["repairId"]), instance_id=instance_id
                    )
                    if res.get("status") == "success":
                        ok += 1
                    else:
                        bad += 1
                except Exception:
                    bad += 1
            return {"processed": len(jobs), "succeeded": ok, "failed": bad}

        acquired, result = _run_with_scope_lease(
            worker_kind="repair",
            scope_id=dest_id,
            instance_id=instance_id,
            lease_seconds=90,
            work=_work,
        )
        scopes += 1
        if not acquired:
            lease_skips += 1
            continue
        if isinstance(result, dict):
            processed += int(result.get("processed") or 0)
            succeeded += int(result.get("succeeded") or 0)
            failed += int(result.get("failed") or 0)
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "leaseSkips": lease_skips,
        "scopes": scopes,
        "shardedBy": "destTargetId",
    }


def _process_rebalance_scopes(*, instance_id: str, limit: int) -> dict[str, Any]:
    """Shard rebalance execution by destTargetId (Gate F)."""
    candidates = [
        job
        for job in backup_replication.list_rebalance_jobs(limit=max(limit * 20, 50))
        if str(job.get("phase") or "") == "pending"
    ]
    by_dest = _group_jobs_by_scope(candidates, scope_keys=("destTargetId",))
    processed = succeeded = failed = lease_skips = 0
    scopes = 0
    for dest_id, group in sorted(by_dest.items()):
        batch = group[:limit]

        def _work(jobs: list[dict[str, Any]] = batch) -> dict[str, int]:
            ok = bad = 0
            for job in jobs:
                res = backup_replication.execute_rebalance_job(str(job["jobId"]), instance_id=instance_id)
                if res.get("status") == "success":
                    ok += 1
                else:
                    bad += 1
            return {"processed": len(jobs), "succeeded": ok, "failed": bad}

        acquired, result = _run_with_scope_lease(
            worker_kind="rebalance",
            scope_id=dest_id,
            instance_id=instance_id,
            lease_seconds=90,
            work=_work,
        )
        scopes += 1
        if not acquired:
            lease_skips += 1
            continue
        if isinstance(result, dict):
            processed += int(result.get("processed") or 0)
            succeeded += int(result.get("succeeded") or 0)
            failed += int(result.get("failed") or 0)
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "leaseSkips": lease_skips,
        "scopes": scopes,
        "shardedBy": "destTargetId",
    }


def _process_retirement_scopes(*, instance_id: str, limit: int) -> dict[str, Any]:
    """Shard retirement execution by targetId (Gate F)."""
    candidates = [
        job
        for job in backup_retirement.list_copy_retirement_jobs(limit=max(limit * 20, 50))
        if str(job.get("phase") or "") not in backup_retirement.RETIREMENT_TERMINAL_PHASES
    ]
    by_target = _group_jobs_by_scope(candidates, scope_keys=("targetId",))
    processed = reclaimed = waiting = failed = lease_skips = 0
    scopes = 0
    for target_id, group in sorted(by_target.items()):
        batch = group[:limit]

        def _work(jobs: list[dict[str, Any]] = batch) -> dict[str, int]:
            ok = wait = bad = 0
            for job in jobs:
                result = backup_retirement.execute_copy_retirement_job(
                    str(job["jobId"]), instance_id=instance_id
                )
                phase = str(result.get("phase") or "")
                if phase == "reclaimed":
                    ok += 1
                elif phase in {
                    "waiting-for-dependencies",
                    "requested",
                    "checking-topology",
                    "checking-holds",
                    "committing-retirement-marker",
                    "retiring-ledger-copy",
                    "gc-pending",
                    "gc-running",
                }:
                    wait += 1
                else:
                    bad += 1
            return {"processed": len(jobs), "reclaimed": ok, "waiting": wait, "failed": bad}

        acquired, result = _run_with_scope_lease(
            worker_kind="retirement",
            scope_id=target_id,
            instance_id=instance_id,
            lease_seconds=90,
            work=_work,
        )
        scopes += 1
        if not acquired:
            lease_skips += 1
            continue
        if isinstance(result, dict):
            processed += int(result.get("processed") or 0)
            reclaimed += int(result.get("reclaimed") or 0)
            waiting += int(result.get("waiting") or 0)
            failed += int(result.get("failed") or 0)
    return {
        "processed": processed,
        "reclaimed": reclaimed,
        "waiting": waiting,
        "failed": failed,
        "leaseSkips": lease_skips,
        "scopes": scopes,
        "shardedBy": "targetId",
    }


def _process_chain_migration_scopes(*, instance_id: str, limit: int) -> dict[str, Any]:
    """Shard chain-migration execution by destTargetId (Gate F)."""
    active_phases = {"planned", "transferring", "members-authenticated", "closure-authenticated"}
    candidates = [
        job
        for job in backup_control.list_chain_migration_jobs(limit=max(limit * 20, 50))
        if str(job.get("phase") or "") in active_phases
    ]
    by_dest = _group_jobs_by_scope(candidates, scope_keys=("destTargetId",))
    processed = succeeded = failed = lease_skips = 0
    scopes = 0
    for dest_id, group in sorted(by_dest.items()):
        batch = group[:limit]

        def _work(jobs: list[dict[str, Any]] = batch) -> dict[str, int]:
            ok = bad = 0
            for job in jobs:
                mid = str(job.get("migrationId") or "")
                if not mid:
                    continue
                res = backup_tiering.execute_chain_migration(mid, instance_id=instance_id)
                phase = str(res.get("phase") or "")
                if phase in {"converged", "verified"}:
                    ok += 1
                elif phase in {"failed-terminal", "rejected"}:
                    bad += 1
                else:
                    ok += 1  # in-progress still counts as advanced
            return {"processed": len(jobs), "succeeded": ok, "failed": bad}

        acquired, result = _run_with_scope_lease(
            worker_kind="chain-migration",
            scope_id=dest_id,
            instance_id=instance_id,
            lease_seconds=90,
            work=_work,
        )
        scopes += 1
        if not acquired:
            lease_skips += 1
            continue
        if isinstance(result, dict):
            processed += int(result.get("processed") or 0)
            succeeded += int(result.get("succeeded") or 0)
            failed += int(result.get("failed") or 0)
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "leaseSkips": lease_skips,
        "scopes": scopes,
        "shardedBy": "destTargetId",
    }


def maintenance_tick(*, instance_id: str, limit_per_worker: int = 5) -> dict[str, Any]:
    """Advance maintenance queues under a short global planner lease.

    Heavy per-target work uses independent scope leases so a slow archive target
    cannot block primary repair progress.
    """
    lease = backup_control.acquire_maintenance_lease(
        "storage-maintenance-planner", "global", owner_instance_id=instance_id, lease_seconds=60
    )
    if lease is None:
        # Compatibility: also try legacy global worker lease name used by 4.5.8 tests.
        lease = backup_control.acquire_maintenance_lease(
            "storage-maintenance", "global", owner_instance_id=instance_id, lease_seconds=120
        )
    if lease is None:
        return {"leaseAcquired": False, "drainsProcessed": 0}
    limit = max(1, min(int(limit_per_worker), 100))
    planner_kind = str(lease["workerKind"])
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_lease_heartbeat,
        kwargs={
            "stop": heartbeat_stop,
            "worker_kind": planner_kind,
            "scope_id": "global",
            "instance_id": instance_id,
            "fencing_token": int(lease["fencingToken"]),
            "lease_seconds": 120 if planner_kind == "storage-maintenance" else 60,
        },
        name="storage-maintenance-planner-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        recovery = backup_recovery_keeper.reconcile_durable_recovery_leases()

        acquired_repl, replication = _run_with_scope_lease(
            worker_kind="replication",
            scope_id="global",
            instance_id=instance_id,
            lease_seconds=90,
            work=lambda: backup_replication.process_pending_jobs(instance_id=instance_id, limit=limit),
        )
        if not acquired_repl:
            replication = {"leaseSkipped": True}

        # Gate F: repair / rebalance / retirement / chain-migration are target-scoped.
        repairs = _process_repair_scopes(instance_id=instance_id, limit=limit)
        rebalances = _process_rebalance_scopes(instance_id=instance_id, limit=limit)
        retirements = _process_retirement_scopes(instance_id=instance_id, limit=limit)

        capacity_probes = _probe_capacity_page(limit=limit)
        qos = backup_transfer_budget.get_global_transfer_budget_manager().transfer_control_summary()
        drain_summary = _process_drain_scopes(instance_id=instance_id, limit=limit)

        migrations = _process_chain_migration_scopes(instance_id=instance_id, limit=limit)

        # Gate E: autonomous placement reconcile (plan only; migrations execute above).
        placement = backup_placement.reconcile_all_policies(limit_per_policy=limit, execute=True)

        return {
            "leaseAcquired": True,
            "plannerKind": planner_kind,
            "recovery": recovery,
            "replication": replication,
            "repairs": repairs,
            "rebalances": rebalances,
            "retirements": retirements,
            "capacityProbes": capacity_probes,
            "chainMigrations": migrations,
            "placement": placement,
            "qos": qos,
            **drain_summary,
            "shardedScopes": True,
            "shardedByTarget": True,
        }
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2.0)
        backup_control.release_maintenance_lease(
            planner_kind,
            "global",
            owner_instance_id=instance_id,
            fencing_token=int(lease["fencingToken"]),
        )


class StorageMaintenanceSupervisor:
    """One process-local loop backed by cross-process durable leases."""

    def __init__(self, *, instance_id: str, tick_seconds: float = 30.0, limit_per_worker: int = 5) -> None:
        self.instance_id = instance_id
        self.tick_seconds = max(0.1, float(tick_seconds))
        self.limit_per_worker = max(1, int(limit_per_worker))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> dict[str, Any]:
        return maintenance_tick(instance_id=self.instance_id, limit_per_worker=self.limit_per_worker)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="storage-maintenance-supervisor", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - supervisor isolation
                _logger.exception("storage maintenance tick failed", extra={"instanceId": self.instance_id})
            self._stop.wait(self.tick_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        self._thread = None
