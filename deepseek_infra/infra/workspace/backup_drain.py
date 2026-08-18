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
    backup_dr_ledger,
    backup_replication,
    backup_targets,
)

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


def process_target_drain(
    target_id: str,
    *,
    instance_id: str = "drain-supervisor",
    max_rebalances_per_step: int = 5,
) -> dict[str, Any]:
    """Execute autonomous evacuation step for a draining target."""
    job = get_target_drain_job(target_id=target_id)
    if not job:
        return {"status": "skipped", "reason": "no-drain-job"}

    if job["phase"] in DRAIN_TERMINAL_PHASES:
        return {"status": "completed", "job": job}

    _update_drain_state(target_id, "evacuating")

    # List all copies held on target
    target_copies = backup_dr_ledger.list_logical_recovery_copies(target_id=target_id, limit=500)
    live_copies = [c for c in target_copies if c.get("recoverable") and c.get("state") == "healthy"]

    if not live_copies:
        # Check active writers or holds on target
        has_active_holds = any(
            backup_replication.is_source_held(target_id, str(c.get("policyId")), str(c.get("backupId")))
            for c in target_copies
        )
        if not has_active_holds:
            # All copies evacuated and no active holds -> marked drained
            backup_targets.mark_target_drained(target_id)
            updated = _update_drain_state(
                target_id,
                "drained",
                protected_points=0,
                remaining_copies=0,
                active_rebalances=0,
                bytes_remaining=0,
            )
            return {"status": "drained", "job": updated}

    all_targets = {t["targetId"]: t for t in backup_targets.list_targets()}
    active_cand_ids = [
        tid for tid, t in all_targets.items()
        if tid != target_id and t.get("drainState") not in {"draining", "drained"}
    ]

    rebalances_triggered = 0
    remaining_bytes = sum(int(c.get("logicalBytes") or c.get("physicalBytes") or 50 * 1024 * 1024) for c in live_copies)

    for copy in live_copies:
        if rebalances_triggered >= max_rebalances_per_step:
            break

        policy_id = str(copy.get("policyId"))
        backup_id = str(copy.get("backupId"))

        # Find destination target not in draining state
        existing_copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
        existing_target_ids = {str(c.get("targetId")) for c in existing_copies if c.get("recoverable")}

        for cand_id in active_cand_ids:
            if cand_id not in existing_target_ids:
                # Enqueue rebalance job with prune_source_after=True
                r_job = backup_replication.create_rebalance_job(
                    policy_id=policy_id,
                    backup_id=backup_id,
                    dest_target_id=cand_id,
                    source_target_id=target_id,
                    reason="autonomous-drain-evacuation",
                    prune_source_after=True,
                )
                backup_replication.execute_rebalance_job(str(r_job["jobId"]), instance_id=instance_id)
                rebalances_triggered += 1
                break

    updated = _update_drain_state(
        target_id,
        "evacuating" if len(live_copies) > 0 else "waiting-for-gc",
        protected_points=len(live_copies),
        remaining_copies=len(live_copies),
        active_rebalances=rebalances_triggered,
        bytes_remaining=remaining_bytes,
    )
    return {"status": "in_progress", "job": updated, "rebalancesTriggered": rebalances_triggered}
