"""Autonomous Target Drain and Copy Evacuation (4.5.7).

Manages the lifecycle of target decommissioning:
draining -> evacuating -> waiting-for-gc -> drained.
Coordinates with placement and rebalance to safely evacuate all required
and retained copies before declaring a target fully drained.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_recovery_keeper,
    backup_replication,
    backup_retirement,
    backup_run_plan,
    backup_scheduler,
    backup_targets,
    backup_writer_lease,
)
from deepseek_infra.infra.workspace.backup_target_store import commit_slot_digest

DRAIN_DIR = config.ROOT / ".backup-drains"
DRAIN_DB = DRAIN_DIR / "drains.sqlite3"
DRAINS_DIR = DRAIN_DIR
DRAINS_DB = DRAIN_DB

DRAIN_TERMINAL_PHASES = frozenset({"drained", "failed", "cancelled"})


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    DRAIN_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DRAIN_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS target_drain_jobs (
            drain_id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL,
            reason TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            protected_recovery_points INTEGER DEFAULT 0,
            remaining_required_copies INTEGER DEFAULT 0,
            active_rebalances INTEGER DEFAULT 0,
            bytes_remaining INTEGER DEFAULT 0,
            error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_drain_phase ON target_drain_jobs(phase)")
    return conn


def start_target_drain(
    target_id: str,
    *,
    reason: str = "administrative-drain",
    force: bool = False,
) -> dict[str, Any]:
    """Initiate a durable drain job and set target registry state to draining."""
    target_info = backup_targets.get_target(target_id)
    if not target_info:
        raise AppError(f"Target {target_id} not found in registry", code=ErrorCode.NOT_FOUND, status=404)

    # Update target registry drainState
    backup_targets.drain_target(target_id, reason=reason)

    drain_id = f"drain_{secrets.token_hex(8)}"
    now = _utc_iso()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO target_drain_jobs(drain_id, target_id, phase, reason, started_at, updated_at, protected_recovery_points, remaining_required_copies, active_rebalances, bytes_remaining, error)
            VALUES (?, ?, 'draining', ?, ?, ?, 0, 0, 0, 0, NULL)
            ON CONFLICT(target_id) DO UPDATE SET
                phase = 'draining',
                reason = excluded.reason,
                updated_at = excluded.updated_at,
                error = NULL
            """,
            (drain_id, target_id, reason, now, now),
        )

    return get_target_drain_job(target_id) or {}


initiate_target_drain = start_target_drain


def cancel_target_drain(target_id: str, *, reason: str = "operator-cancelled") -> dict[str, Any]:
    """Cancel a target drain job and return the target to active status."""
    backup_targets.activate_target(target_id)
    return _update_drain_state(target_id, "cancelled", error=reason)


def get_target_drain_job(target_id: str | None = None, *, drain_id: str | None = None) -> dict[str, Any] | None:
    with _connect() as conn:
        if drain_id:
            row = conn.execute("SELECT * FROM target_drain_jobs WHERE drain_id = ?", (drain_id,)).fetchone()
        elif target_id:
            row = conn.execute("SELECT * FROM target_drain_jobs WHERE target_id = ?", (target_id,)).fetchone()
        else:
            return None

        if not row:
            return None
        return {
            "drainId": row["drain_id"],
            "targetId": row["target_id"],
            "phase": row["phase"],
            "drainState": row["phase"],
            "reason": row["reason"],
            "startedAt": row["started_at"],
            "updatedAt": row["updated_at"],
            "protectedRecoveryPoints": row["protected_recovery_points"],
            "remainingRequiredCopies": row["remaining_required_copies"],
            "activeRebalances": row["active_rebalances"],
            "bytesRemaining": row["bytes_remaining"],
            "error": row["error"],
        }


def list_target_drain_jobs(*, phase: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        query = "SELECT * FROM target_drain_jobs WHERE 1=1"
        params: list[Any] = []
        if phase:
            query += " AND phase = ?"
            params.append(phase)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "drainId": row["drain_id"],
                "targetId": row["target_id"],
                "phase": row["phase"],
                "drainState": row["phase"],
                "reason": row["reason"],
                "startedAt": row["started_at"],
                "updatedAt": row["updated_at"],
                "protectedRecoveryPoints": row["protected_recovery_points"],
                "remainingRequiredCopies": row["remaining_required_copies"],
                "activeRebalances": row["active_rebalances"],
                "bytesRemaining": row["bytes_remaining"],
                "error": row["error"],
            }
            for row in rows
        ]


def _update_drain_state(
    target_id: str,
    phase: str,
    *,
    protected_points: int = 0,
    remaining_copies: int = 0,
    active_rebalances: int = 0,
    bytes_remaining: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE target_drain_jobs
            SET phase = ?, protected_recovery_points = ?, remaining_required_copies = ?,
                active_rebalances = ?, bytes_remaining = ?, error = ?, updated_at = ?
            WHERE target_id = ?
            """,
            (phase, protected_points, remaining_copies, active_rebalances, bytes_remaining, error, now, target_id),
        )
    return get_target_drain_job(target_id=target_id) or {}


def _active_run_targets(target_id: str) -> bool:
    for run in backup_scheduler.list_active_runs():
        policy_id = str(run.get("policyId") or "")
        slot = str(run.get("scheduleSlot") or "")
        plan = backup_run_plan.read_run_plan(policy_id, commit_slot_digest(slot)) if policy_id and slot else None
        if isinstance(plan, dict):
            selected = str(plan.get("selectedWriteTargetId") or plan.get("targetId") or "")
        else:
            try:
                policy = backup_policies.get_policy(policy_id)
            except AppError:
                policy = {}
            selected = str(policy.get("primaryTargetId") or policy.get("targetId") or "")
        if selected == target_id:
            return True
    return False


def _active_recovery_targets(target_id: str) -> bool:
    for session in backup_recovery_keeper.scan_durable_recovery_sessions().values():
        phase = str(session.get("phase") or "")
        if phase in backup_recovery_keeper.TERMINAL_PHASES:
            continue
        referenced_targets = {
            str(session.get("activeSourceTargetId") or ""),
            str(session.get("targetId") or ""),
        }
        referenced_targets.update(
            str(hold.get("targetId") or "")
            for hold in list(session.get("holds") or [])
            if isinstance(hold, dict)
        )
        if target_id in referenced_targets:
            return True
    return False


def _drain_completion_blockers(target_id: str) -> list[str]:
    """Return fail-closed blockers that prevent the draining -> drained transition."""
    blockers: list[str] = []
    try:
        target = backup_publish.resolve_target(target_id)
    except Exception:
        return ["target-unavailable"]
    if backup_writer_lease.active_writer_lease(target):
        blockers.append("active-writer-lease")
    if _active_run_targets(target_id):
        blockers.append("active-backup-run")
    if _active_recovery_targets(target_id):
        blockers.append("active-recovery")
    if backup_replication.has_source_holds_for_target(target_id, target=target):
        blockers.append("active-source-hold")

    repairs = backup_replication.list_repair_jobs(source_target_id=target_id, limit=500)
    if any(str(item.get("phase") or "") not in backup_replication.REPAIR_TERMINAL_PHASES for item in repairs):
        blockers.append("active-repair-source")
    elif len(repairs) >= 500:
        blockers.append("repair-scan-incomplete")
    rebalances = backup_replication.list_rebalance_jobs(source_target_id=target_id, limit=500)
    if any(str(item.get("phase") or "") not in {"complete", "failed"} for item in rebalances):
        blockers.append("active-rebalance-source")
    elif len(rebalances) >= 500:
        blockers.append("rebalance-scan-incomplete")
    retirements = backup_retirement.list_copy_retirement_jobs(target_id=target_id, limit=500)
    if any(str(item.get("phase") or "") not in backup_retirement.RETIREMENT_TERMINAL_PHASES for item in retirements):
        blockers.append("pending-retirement")
    elif len(retirements) >= 500:
        blockers.append("retirement-scan-incomplete")
    return blockers


def process_target_drain(
    target_id: str,
    *,
    instance_id: str = "drain-supervisor",
    max_rebalances_per_step: int = 5,
    scan_page_size: int = 100,
) -> dict[str, Any]:
    """Execute autonomous evacuation step for a draining target."""
    job = get_target_drain_job(target_id=target_id)
    if not job:
        return {"status": "skipped", "reason": "no-drain-job"}

    if job["phase"] in DRAIN_TERMINAL_PHASES:
        return {"status": "completed", "job": job}

    lease = backup_control.acquire_maintenance_lease(
        "target-drain", target_id, owner_instance_id=instance_id, lease_seconds=60
    )
    if lease is None:
        return {"status": "skipped", "reason": "drain-owned-by-another-worker", "job": job}
    try:
        _update_drain_state(target_id, "evacuating")
        live_total = backup_dr_ledger.count_live_logical_recovery_copies(target_id=target_id)
        if live_total == 0:
            blockers = _drain_completion_blockers(target_id)
            if not blockers:
                backup_targets.mark_target_drained(target_id)
                updated = _update_drain_state(target_id, "drained")
                return {"status": "drained", "job": updated}
            updated = _update_drain_state(target_id, "waiting-for-gc", error=",".join(blockers))
            return {"status": "in_progress", "job": updated, "blockers": blockers, "rebalancesTriggered": 0}

        cursor_state = backup_control.get_maintenance_cursor("target-drain", target_id)
        cursor = cursor_state["cursor"] if isinstance(cursor_state.get("cursor"), dict) else {}
        page_size = max(1, min(int(scan_page_size), 500))
        page = backup_dr_ledger.list_logical_recovery_copies(
            target_id=target_id,
            after_committed_at=str(cursor.get("committedAt")) if cursor.get("committedAt") else None,
            after_logical_id=str(cursor.get("logicalId")) if cursor.get("logicalId") else None,
            limit=page_size,
        )
        all_targets = {str(item["targetId"]): item for item in backup_targets.list_targets()}
        candidate_ids = [
            candidate_id
            for candidate_id, target in all_targets.items()
            if candidate_id != target_id and target.get("drainState") not in {"draining", "drained"}
        ]
        triggered = 0
        examined = 0
        last_copy: dict[str, Any] | None = None
        remaining_bytes = 0
        for copy in page:
            is_live = bool(copy.get("recoverable") and copy.get("state") == "healthy")
            if is_live and triggered >= max(0, max_rebalances_per_step):
                break
            examined += 1
            last_copy = copy
            if not is_live:
                continue
            metadata_value = copy.get("metadata")
            metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
            physical_value = metadata.get("physicalBytes") or metadata.get("ciphertextBytes")
            required_bytes = int(physical_value) if isinstance(physical_value, int) and not isinstance(physical_value, bool) else None
            if required_bytes is not None:
                remaining_bytes += required_bytes
            policy_id = str(copy.get("policyId") or "")
            backup_id = str(copy.get("backupId") or "")
            try:
                policy = backup_policies.get_policy(policy_id)
            except AppError:
                policy = {
                    "policyId": policy_id,
                    "targetId": target_id,
                    "replication": {"enabled": True, "targets": [{"targetId": item} for item in candidate_ids]},
                }
            ranked = backup_scheduler.plan_target_placement(
                policy,
                candidate_target_ids=candidate_ids,
                primary_target_id=target_id,
                logical_recovery_point_id=str(copy.get("logicalId") or ""),
                required_bytes=required_bytes,
                snapshot_kind="full",
                force_full=False,
            )
            if not ranked:
                continue
            backup_replication.create_rebalance_job(
                policy_id=policy_id,
                backup_id=backup_id,
                dest_target_id=str(ranked[0][1]),
                source_target_id=target_id,
                reason="autonomous-drain-evacuation",
                prune_source_after=True,
            )
            triggered += 1

        next_cursor = None
        if last_copy is not None and (examined < len(page) or len(page) >= page_size):
            next_cursor = {
                "committedAt": str(last_copy.get("committedAt") or ""),
                "logicalId": str(last_copy.get("logicalId") or ""),
            }
        backup_control.update_maintenance_cursor(
            "target-drain",
            target_id,
            next_cursor,
            expected_generation=int(cursor_state["generation"]),
        )
        updated = _update_drain_state(
            target_id,
            "evacuating",
            protected_points=live_total,
            remaining_copies=live_total,
            active_rebalances=triggered,
            bytes_remaining=remaining_bytes,
        )
        return {"status": "in_progress", "job": updated, "rebalancesTriggered": triggered}
    finally:
        backup_control.release_maintenance_lease(
            "target-drain",
            target_id,
            owner_instance_id=instance_id,
            fencing_token=int(lease["fencingToken"]),
        )
