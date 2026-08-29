"""Persistent reserved-versus-consumed Fleet Scheduler service state (4.7.5 Gate B)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

SCHEDULER_SERVICE_DIR = config.ROOT / ".resilience-scheduler"
SCHEDULER_SERVICE_DB = SCHEDULER_SERVICE_DIR / "service.sqlite3"

RESERVATION_RESERVED = "RESERVED"
RESERVATION_CONSUMING = "CONSUMING"
RESERVATION_CONSUMED = "CONSUMED"
RESERVATION_RELEASED = "RELEASED"
RESERVATION_EXPIRED = "EXPIRED"
RESERVATION_RECONCILING = "RECONCILING"
ACTIVE_RESERVATION_STATUSES = frozenset({RESERVATION_RESERVED, RESERVATION_CONSUMING, RESERVATION_RECONCILING})
RELEASE_REASONS = frozenset({"PREEMPTED", "STALE", "REPLAN", "EXPIRED", "FAILED", "SUPERSEDED", "UNEXECUTED"})

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
CREATE TABLE IF NOT EXISTS resilience_scheduler_runs (
    schedule_id TEXT PRIMARY KEY,
    schedule_json TEXT NOT NULL,
    scheduled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_scheduler_runs_time
ON resilience_scheduler_runs(scheduled_at);
CREATE TABLE IF NOT EXISTS resilience_service_reservations (
    action_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    execution_epoch INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    estimated_bytes INTEGER NOT NULL,
    reserved_units REAL NOT NULL,
    status TEXT NOT NULL,
    actual_bytes INTEGER,
    actual_duration_ms REAL,
    actual_traffic_class TEXT,
    outcome TEXT,
    release_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_service_reservations_schedule
ON resilience_service_reservations(schedule_id, status);
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


def _service_units(byte_count: int) -> float:
    return 1.0 + (float(max(0, byte_count)) / float(1024**3))


def _wave_index(action: dict[str, Any]) -> int:
    raw = action.get("waveIndex")
    if raw is None:
        raw_params = action.get("parameters")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw = params.get("waveIndex")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _execution_epoch(action: dict[str, Any]) -> int:
    try:
        return max(0, int(action.get("executionEpoch") or 0))
    except (TypeError, ValueError):
        return 0


def _reservation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "actionId": str(row["action_id"]),
        "scheduleId": str(row["schedule_id"]),
        "waveIndex": int(row["wave_index"]),
        "executionEpoch": int(row["execution_epoch"]),
        "policyId": str(row["policy_id"]),
        "estimatedBytes": int(row["estimated_bytes"]),
        "reservedUnits": float(row["reserved_units"]),
        "status": str(row["status"]),
        "actualBytes": row["actual_bytes"],
        "actualDurationMs": row["actual_duration_ms"],
        "actualTrafficClass": row["actual_traffic_class"],
        "outcome": row["outcome"],
        "releaseReason": row["release_reason"],
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


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


def get_reservation(action_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
    return _reservation_from_row(row) if row is not None else None


def list_reservations(
    *,
    schedule_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if schedule_id:
        clauses.append("schedule_id = ?")
        params.append(schedule_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    query = "SELECT * FROM resilience_service_reservations"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY schedule_id, wave_index, action_id"
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_reservation_from_row(row) for row in rows]


def _charge_consumed(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    policy_id: str,
    byte_count: int,
    timestamp: str,
) -> bool:
    service_units = _service_units(byte_count)
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO resilience_scheduler_service_events (
            action_id, policy_id, service_units, bytes_served, scheduled_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (action_id, policy_id, service_units, byte_count, timestamp),
    )
    if inserted.rowcount != 1:
        return False
    current = conn.execute(
        "SELECT virtual_runtime, virtual_finish, actions_served, bytes_served FROM resilience_scheduler_service WHERE policy_id = ?",
        (policy_id,),
    ).fetchone()
    virtual_runtime = float(current[0]) if current is not None else 0.0
    virtual_finish = float(current[1]) if current is not None else 0.0
    actions_served = int(current[2]) if current is not None else 0
    bytes_served = int(current[3]) if current is not None else 0
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
            virtual_runtime + service_units,
            max(virtual_finish, virtual_runtime) + service_units,
            actions_served + 1,
            bytes_served + byte_count,
            timestamp,
        ),
    )
    return True


def _reserve_action(
    conn: sqlite3.Connection,
    action: dict[str, Any],
    *,
    schedule_id: str,
    timestamp: str,
) -> None:
    action_id = str(action.get("actionId") or "")
    if not action_id:
        return
    policy_id = _policy_id(action)
    byte_count = _estimated_bytes(action)
    conn.execute(
        """
        INSERT OR IGNORE INTO resilience_service_reservations (
            action_id, schedule_id, wave_index, execution_epoch, policy_id,
            estimated_bytes, reserved_units, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            schedule_id,
            _wave_index(action),
            _execution_epoch(action),
            policy_id,
            byte_count,
            _service_units(byte_count),
            RESERVATION_RESERVED,
            timestamp,
            timestamp,
        ),
    )


def reserve_scheduled_actions(
    actions: list[dict[str, Any]],
    *,
    schedule_id: str,
    scheduled_at: datetime | None = None,
) -> None:
    """Create fair-service reservations without charging persistent share."""
    if not schedule_id:
        raise ValueError("scheduleId is required")
    timestamp = _utc_iso(scheduled_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for action in actions:
            _reserve_action(conn, action, schedule_id=schedule_id, timestamp=timestamp)


def record_scheduled_actions(
    actions: list[dict[str, Any]],
    *,
    scheduled_at: datetime | None = None,
    schedule_id: str = "manual",
) -> None:
    """Reserve admitted actions. Persistent fair share is charged only on consume."""
    timestamp = _utc_iso(scheduled_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for action in actions:
            _reserve_action(conn, action, schedule_id=schedule_id, timestamp=timestamp)


def consume_action_service(
    action: dict[str, Any] | str,
    *,
    actual_bytes: int | None = None,
    actual_duration_ms: float | int | None = None,
    actual_traffic_class: str | None = None,
    outcome: str = "SUCCEEDED",
    consumed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Charge observed terminal effect exactly once. Reservations are not fair-share."""
    payload: dict[str, Any] = action if isinstance(action, dict) else {"actionId": action}
    action_id = str(payload.get("actionId") or "")
    if not action_id:
        return None
    timestamp = _utc_iso(consumed_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        policy_id = str(existing["policy_id"]) if existing is not None else _policy_id(payload)
        estimated = int(existing["estimated_bytes"]) if existing is not None else _estimated_bytes(payload)
        byte_count = estimated if actual_bytes is None else max(0, int(actual_bytes))
        if existing is None:
            _reserve_action(
                conn,
                {**payload, "actionId": action_id, "policyId": policy_id, "estimatedBytes": byte_count},
                schedule_id=str(payload.get("scheduleId") or "manual"),
                timestamp=timestamp,
            )
        else:
            status = str(existing["status"])
            if status == RESERVATION_CONSUMED:
                return _reservation_from_row(existing)
            if status in {RESERVATION_RELEASED, RESERVATION_EXPIRED}:
                return _reservation_from_row(existing)
        charged = _charge_consumed(
            conn,
            action_id=action_id,
            policy_id=policy_id,
            byte_count=byte_count,
            timestamp=timestamp,
        )
        duration = None if actual_duration_ms is None else float(actual_duration_ms)
        conn.execute(
            """
            UPDATE resilience_service_reservations
            SET status = ?, actual_bytes = ?, actual_duration_ms = ?,
                actual_traffic_class = ?, outcome = ?, updated_at = ?
            WHERE action_id = ?
            """,
            (
                RESERVATION_CONSUMED if charged or (existing is not None and str(existing["status"]) == RESERVATION_CONSUMED) else RESERVATION_CONSUMED,
                byte_count,
                duration,
                actual_traffic_class,
                str(outcome or "SUCCEEDED"),
                timestamp,
                action_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row is not None
        return _reservation_from_row(row)


def record_consumed_service(
    actions: list[dict[str, Any]],
    *,
    consumed_at: datetime | None = None,
) -> None:
    """Seed or record terminal consumption for actions that already produced effects."""
    for action in actions:
        consume_action_service(action, actual_bytes=_estimated_bytes(action), consumed_at=consumed_at, outcome="SUCCEEDED")


def release_action_reservation(
    action_id: str,
    *,
    reason: str,
    released_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Release an unused reservation so it cannot consume persistent fair share."""
    normalized = str(reason or "UNEXECUTED").upper()
    if normalized not in RELEASE_REASONS:
        normalized = "UNEXECUTED"
    timestamp = _utc_iso(released_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if existing is None:
            return None
        status = str(existing["status"])
        if status == RESERVATION_CONSUMED:
            return _reservation_from_row(existing)
        next_status = RESERVATION_EXPIRED if normalized == "EXPIRED" else RESERVATION_RELEASED
        conn.execute(
            """
            UPDATE resilience_service_reservations
            SET status = ?, release_reason = ?, updated_at = ?
            WHERE action_id = ? AND status IN ('RESERVED', 'CONSUMING', 'RECONCILING')
            """,
            (next_status, normalized, timestamp, action_id),
        )
        row = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row is not None
        return _reservation_from_row(row)


def release_schedule_reservations(
    schedule_id: str,
    *,
    reason: str,
    released_at: datetime | None = None,
) -> int:
    released = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT action_id FROM resilience_service_reservations WHERE schedule_id = ? AND status IN ('RESERVED', 'CONSUMING', 'RECONCILING')",
            (schedule_id,),
        ).fetchall()
    for row in rows:
        if release_action_reservation(str(row[0]), reason=reason, released_at=released_at) is not None:
            released += 1
    return released


def _render_schedule(schedule: dict[str, Any]) -> tuple[str, str]:
    schedule_id = str(schedule.get("scheduleId") or "")
    if not schedule_id:
        raise ValueError("scheduleId is required")
    return schedule_id, json.dumps(schedule, ensure_ascii=False, sort_keys=True)


def _record_schedule_snapshot(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    rendered_schedule: str,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO resilience_scheduler_runs (
            schedule_id, schedule_json, scheduled_at
        ) VALUES (?, ?, ?)
        """,
        (schedule_id, rendered_schedule, timestamp),
    )


def record_schedule_snapshot(schedule: dict[str, Any], *, scheduled_at: datetime | None = None) -> None:
    """Persist the exact latest wave/unschedulable operator projection."""
    schedule_id, rendered_schedule = _render_schedule(schedule)
    with _connect() as conn:
        _record_schedule_snapshot(
            conn,
            schedule_id=schedule_id,
            rendered_schedule=rendered_schedule,
            timestamp=_utc_iso(scheduled_at),
        )


def record_schedule_result(
    schedule: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    scheduled_at: datetime | None = None,
) -> None:
    """Persist a schedule projection and reserve — not consume — fair service."""
    schedule_id, rendered_schedule = _render_schedule(schedule)
    timestamp = _utc_iso(scheduled_at)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for action in actions:
            _reserve_action(conn, action, schedule_id=schedule_id, timestamp=timestamp)
        _record_schedule_snapshot(
            conn,
            schedule_id=schedule_id,
            rendered_schedule=rendered_schedule,
            timestamp=timestamp,
        )


def get_latest_schedule_snapshot() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT schedule_json FROM resilience_scheduler_runs ORDER BY scheduled_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    parsed = json.loads(str(row[0]))
    return parsed if isinstance(parsed, dict) else None
