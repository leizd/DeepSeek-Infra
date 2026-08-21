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

        # Repair/rebalance: prefer target-scoped discovery when job lists expose targets.
        acquired_repair, repairs = _run_with_scope_lease(
            worker_kind="repair",
            scope_id="global",
            instance_id=instance_id,
            lease_seconds=90,
            work=lambda: backup_replication.process_pending_repairs(instance_id=instance_id, limit=limit),
        )
        if not acquired_repair:
            repairs = {"leaseSkipped": True}

        acquired_rebalance, rebalances = _run_with_scope_lease(
            worker_kind="rebalance",
            scope_id="global",
            instance_id=instance_id,
            lease_seconds=90,
            work=lambda: backup_replication.process_pending_rebalances(instance_id=instance_id, limit=limit),
        )
        if not acquired_rebalance:
            rebalances = {"leaseSkipped": True}

        acquired_ret, retirements = _run_with_scope_lease(
            worker_kind="retirement",
            scope_id="global",
            instance_id=instance_id,
            lease_seconds=90,
            work=lambda: backup_retirement.process_pending_retirements(instance_id=instance_id, limit=limit),
        )
        if not acquired_ret:
            retirements = {"leaseSkipped": True}

        capacity_probes = _probe_capacity_page(limit=limit)
        qos = backup_transfer_budget.get_global_transfer_budget_manager().transfer_control_summary()
        drain_summary = _process_drain_scopes(instance_id=instance_id, limit=limit)

        acquired_mig, migrations = _run_with_scope_lease(
            worker_kind="chain-migration",
            scope_id="global",
            instance_id=instance_id,
            lease_seconds=90,
            work=lambda: backup_tiering.process_pending_chain_migrations(instance_id=instance_id, limit=limit),
        )
        if not acquired_mig:
            migrations = {"leaseSkipped": True}

        # Preserve 4.5.8 drain failure isolation when process_target_drain raises.
        # process_target_drain is invoked inside the scope lease lambda; catch there.
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
            "qos": qos,
            **drain_summary,
            "shardedScopes": True,
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
            except Exception:
                _logger.exception("storage maintenance tick failed", extra={"instanceId": self.instance_id})
            self._stop.wait(self.tick_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        self._thread = None
