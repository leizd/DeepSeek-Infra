"""Persistent weighted-fair Fleet Scheduler service state (4.7.4 Gate F)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

SCHEDULER_SERVICE_DIR = config.ROOT / ".resilience-scheduler"
SCHEDULER_SERVICE_DB = SCHEDULER_SERVICE_DIR / "service.sqlite3"

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_scheduler_service (
    policy_id TEXT PRIMARY KEY,
    virtual_runtime REAL NOT NULL,
    virtual_finish REAL NOT NULL,
    actions_served INTEGER NOT NULL,
    bytes_served INTEGER NOT NULL,
    last_scheduled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resilience_scheduler_service_events (
    action_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    service_units REAL NOT NULL,
    bytes_served INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL
);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    SCHEDULER_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(SCHEDULER_SERVICE_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _policy_id(action: dict[str, Any]) -> str:
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    return str(params.get("policyId") or action.get("policyId") or "default")


def _estimated_bytes(action: dict[str, Any]) -> int:
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    return max(0, int(params.get("estimatedBytes") or action.get("estimatedBytes") or 0))


def get_policy_service(policy_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM resilience_scheduler_service WHERE policy_id = ?",
            (policy_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "policyId": str(row["policy_id"]),
        "virtualRuntime": float(row["virtual_runtime"]),
        "virtualFinish": float(row["virtual_finish"]),
        "actionsServed": int(row["actions_served"]),
        "bytesServed": int(row["bytes_served"]),
        "lastScheduledAt": str(row["last_scheduled_at"]),
    }


def list_policy_service() -> dict[str, dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM resilience_scheduler_service ORDER BY policy_id").fetchall()
    return {
        str(row["policy_id"]): {
            "policyId": str(row["policy_id"]),
            "virtualRuntime": float(row["virtual_runtime"]),
            "virtualFinish": float(row["virtual_finish"]),
            "actionsServed": int(row["actions_served"]),
            "bytesServed": int(row["bytes_served"]),
            "lastScheduledAt": str(row["last_scheduled_at"]),
        }
        for row in rows
    }


def record_scheduled_actions(
    actions: list[dict[str, Any]],
    *,
    scheduled_at: datetime | None = None,
) -> None:
    """Idempotently charge each admitted action to its policy virtual service."""
    timestamp = _utc_iso(scheduled_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for action in actions:
            action_id = str(action.get("actionId") or "")
            if not action_id:
                continue
            policy_id = _policy_id(action)
            byte_count = _estimated_bytes(action)
            service_units = 1.0 + (float(byte_count) / float(1024**3))
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO resilience_scheduler_service_events (
                    action_id, policy_id, service_units, bytes_served, scheduled_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (action_id, policy_id, service_units, byte_count, timestamp),
            )
            if inserted.rowcount != 1:
                continue
            current = conn.execute(
                "SELECT virtual_runtime, virtual_finish, actions_served, bytes_served FROM resilience_scheduler_service WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            virtual_runtime = float(current[0]) if current is not None else 0.0
            virtual_finish = float(current[1]) if current is not None else 0.0
            actions_served = int(current[2]) if current is not None else 0
            bytes_served = int(current[3]) if current is not None else 0
            next_runtime = virtual_runtime + service_units
            next_finish = max(virtual_finish, virtual_runtime) + service_units
            conn.execute(
                """
                INSERT INTO resilience_scheduler_service (
                    policy_id, virtual_runtime, virtual_finish, actions_served,
                    bytes_served, last_scheduled_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    virtual_runtime = excluded.virtual_runtime,
                    virtual_finish = excluded.virtual_finish,
                    actions_served = excluded.actions_served,
                    bytes_served = excluded.bytes_served,
                    last_scheduled_at = excluded.last_scheduled_at
                """,
                (
                    policy_id,
                    next_runtime,
                    next_finish,
                    actions_served + 1,
                    bytes_served + byte_count,
                    timestamp,
                ),
            )
