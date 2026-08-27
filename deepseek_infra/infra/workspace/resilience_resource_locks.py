"""Global Recovery Intelligence - Transactional Resource Locks (4.7.2 Gate I).

Guarantees mutual exclusion across conflicting resilience actions.
Prevents concurrent repairs/rebalances on the same backup copy or target drain/repair collisions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

LOCKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_resource_locks (
    lock_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    owner_instance_id TEXT,
    acquired_at TEXT NOT NULL,
    lease_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_locks_action ON resilience_resource_locks(action_id);
"""


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_locks_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LOCKS_SCHEMA)


def derive_resource_locks_for_action(action: dict[str, Any]) -> list[str]:
    """Derive all required resource lock keys for an action intent."""
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    pid = str(params.get("policyId") or action.get("policyId") or "")
    bid = str(params.get("backupId") or action.get("backupId") or "")
    src = str(params.get("sourceTargetId") or action.get("source") or "")
    dst = str(params.get("destTargetId") or action.get("destination") or action.get("target") or "")

    locks: list[str] = []
    if pid and bid:
        locks.append(f"backup:{pid}:{bid}")
    if src:
        locks.append(f"target:{src}")
    if dst and dst != src:
        locks.append(f"target:{dst}")
    if pid and not bid:
        locks.append(f"policy:{pid}")
    return sorted(list(set(locks)))


def acquire_action_locks(
    conn: sqlite3.Connection,
    action_id: str,
    lock_keys: list[str],
    *,
    owner_instance_id: str = "resilience-worker",
    lease_until: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Atomically acquire a set of resource locks for an action."""
    ensure_locks_schema(conn)
    now_iso = _utc_iso(now)

    for key in lock_keys:
        row = conn.execute(
            "SELECT action_id, lease_until FROM resilience_resource_locks WHERE lock_key = ?",
            (key,),
        ).fetchone()
        if row:
            held_action_id, held_lease_until = row[0], row[1]
            if held_action_id != action_id and held_lease_until > now_iso:
                return False, f"resource-locked:{key}:held-by:{held_action_id}:until:{held_lease_until}"

    # Acquire/renew all locks
    for key in lock_keys:
        conn.execute(
            """
            INSERT INTO resilience_resource_locks (lock_key, action_id, owner_instance_id, acquired_at, lease_until)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lock_key) DO UPDATE SET
                action_id = excluded.action_id,
                owner_instance_id = excluded.owner_instance_id,
                acquired_at = excluded.acquired_at,
                lease_until = excluded.lease_until
            """,
            (key, action_id, owner_instance_id, now_iso, lease_until),
        )

    return True, "locks-acquired"


def release_action_locks(conn: sqlite3.Connection, action_id: str) -> None:
    """Release all resource locks held by an action."""
    ensure_locks_schema(conn)
    conn.execute("DELETE FROM resilience_resource_locks WHERE action_id = ?", (action_id,))


def list_active_locks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List all active resource locks."""
    ensure_locks_schema(conn)
    rows = conn.execute(
        "SELECT lock_key, action_id, owner_instance_id, acquired_at, lease_until FROM resilience_resource_locks"
    ).fetchall()
    return [
        {
            "lockKey": r[0],
            "actionId": r[1],
            "ownerInstanceId": r[2],
            "acquiredAt": r[3],
            "leaseUntil": r[4],
        }
        for r in rows
    ]
