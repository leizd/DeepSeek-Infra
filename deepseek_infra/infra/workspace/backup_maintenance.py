"""Durable, bounded storage maintenance orchestration for 4.5.8."""

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
    backup_transfer_budget,
)

_logger = logging.getLogger("deepseek_infra.storage_maintenance")


def _lease_heartbeat(stop: threading.Event, *, instance_id: str, fencing_token: int) -> None:
    while not stop.wait(30.0):
        if not backup_control.renew_maintenance_lease(
            "storage-maintenance",
            "global",
            owner_instance_id=instance_id,
            fencing_token=fencing_token,
            lease_seconds=120,
        ):
            return


def _probe_capacity_page(*, limit: int) -> int:
    cursor_state = backup_control.get_maintenance_cursor("capacity-probe", "global")
    cursor = cursor_state.get("cursor")
    after_target_id = str(cursor.get("targetId") or "") if isinstance(cursor, dict) else ""
    target_ids = backup_control.list_target_ids_page(after_target_id=after_target_id or None, limit=limit)
    for target_id in target_ids:
        observation = backup_targets.probe_target_capacity(target_id)
        backup_control.record_target_capacity_observation(target_id, observation)
    next_cursor = {"targetId": target_ids[-1]} if len(target_ids) >= limit else None
    backup_control.update_maintenance_cursor(
        "capacity-probe",
        "global",
        next_cursor,
        expected_generation=int(cursor_state["generation"]),
    )
    return len(target_ids)


def maintenance_tick(*, instance_id: str, limit_per_worker: int = 5) -> dict[str, Any]:
    """Advance each maintenance queue once under one cross-process lease."""
    lease = backup_control.acquire_maintenance_lease(
        "storage-maintenance", "global", owner_instance_id=instance_id, lease_seconds=120
    )
    if lease is None:
        return {"leaseAcquired": False, "drainsProcessed": 0}
    limit = max(1, min(int(limit_per_worker), 100))
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_lease_heartbeat,
        kwargs={
            "stop": heartbeat_stop,
            "instance_id": instance_id,
            "fencing_token": int(lease["fencingToken"]),
        },
        name="storage-maintenance-lease-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        recovery = backup_recovery_keeper.reconcile_durable_recovery_leases()
        replication = backup_replication.process_pending_jobs(instance_id=instance_id, limit=limit)
        repairs = backup_replication.process_pending_repairs(instance_id=instance_id, limit=limit)
        rebalances = backup_replication.process_pending_rebalances(instance_id=instance_id, limit=limit)
        retirements = backup_retirement.process_pending_retirements(instance_id=instance_id, limit=limit)
        capacity_probes = _probe_capacity_page(limit=limit)
        qos = backup_transfer_budget.get_global_transfer_budget_manager().transfer_control_summary()
        drains_processed = 0
        drain_failures = 0
        jobs = [
            job
            for job in backup_drain.list_target_drain_jobs(limit=max(limit * 10, 100))
            if str(job.get("phase") or "") not in backup_drain.DRAIN_TERMINAL_PHASES
        ][:limit]
        for job in jobs:
            try:
                backup_drain.process_target_drain(str(job["targetId"]), instance_id=instance_id)
                drains_processed += 1
            except Exception:
                drain_failures += 1
                _logger.exception("target drain maintenance failed", extra={"targetId": job.get("targetId")})
        return {
            "leaseAcquired": True,
            "recovery": recovery,
            "replication": replication,
            "repairs": repairs,
            "rebalances": rebalances,
            "retirements": retirements,
            "capacityProbes": capacity_probes,
            "qos": qos,
            "drainsProcessed": drains_processed,
            "drainFailures": drain_failures,
        }
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2.0)
        backup_control.release_maintenance_lease(
            "storage-maintenance",
            "global",
            owner_instance_id=instance_id,
            fencing_token=int(lease["fencingToken"]),
        )


class StorageMaintenanceSupervisor:
    """One process-local loop backed by a cross-process durable lease."""

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
