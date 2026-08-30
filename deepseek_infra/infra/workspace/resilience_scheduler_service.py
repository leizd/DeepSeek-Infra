"""Effect-bound, exactly-once Fleet Scheduler service settlement (4.7.6 Gate D)."""

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
    effect_handle_json TEXT,
    transfer_id TEXT,
    telemetry_digest TEXT,
    settlement_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_service_reservations_schedule
ON resilience_service_reservations(schedule_id, status);
CREATE TABLE IF NOT EXISTS resilience_service_settlement_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    effect_handle_json TEXT,
    transfer_id TEXT,
    telemetry_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_service_settlement_events_action
ON resilience_service_settlement_events(action_id, event_id);
"""

_RESERVATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("effect_handle_json", "TEXT"),
    ("transfer_id", "TEXT"),
    ("telemetry_digest", "TEXT"),
    ("settlement_started_at", "TEXT"),
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(resilience_service_reservations)").fetchall()}
    for column, definition in _RESERVATION_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE resilience_service_reservations ADD COLUMN {column} {definition}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    SCHEDULER_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(SCHEDULER_SERVICE_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _ensure_schema_columns(conn)
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
        "effectHandle": json.loads(str(row["effect_handle_json"])) if row["effect_handle_json"] else None,
        "transferId": row["transfer_id"],
        "telemetryDigest": row["telemetry_digest"],
        "settlementStartedAt": row["settlement_started_at"],
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


def _read_terminal_effect_telemetry(action_id: str) -> dict[str, Any]:
    from deepseek_infra.infra.workspace import resilience_action_journal, resilience_effect_telemetry

    action = resilience_action_journal.get_action(action_id)
    if action is None:
        raise resilience_effect_telemetry.EffectTelemetryUnavailable("Action Journal record is unavailable")
    return resilience_effect_telemetry.read_terminal_effect_telemetry(action)


def _append_settlement_event(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    from_status: str,
    to_status: str,
    telemetry: dict[str, Any],
    timestamp: str,
) -> None:
    effect_handle = telemetry.get("effectHandle")
    conn.execute(
        """
        INSERT INTO resilience_service_settlement_events (
            action_id, from_status, to_status, execution_epoch,
            effect_handle_json, transfer_id, telemetry_digest, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            from_status,
            to_status,
            int(telemetry.get("actionExecutionEpoch") or 0),
            json.dumps(effect_handle, ensure_ascii=False, sort_keys=True) if isinstance(effect_handle, dict) else None,
            str(telemetry.get("transferId") or ""),
            str(telemetry.get("telemetryDigest") or ""),
            timestamp,
        ),
    )


def _binding_matches(row: sqlite3.Row, telemetry: dict[str, Any]) -> bool:
    rendered_handle = json.dumps(telemetry.get("effectHandle"), ensure_ascii=False, sort_keys=True)
    return (
        int(row["execution_epoch"] or 0) == int(telemetry.get("actionExecutionEpoch") or 0)
        and str(row["effect_handle_json"] or "") == rendered_handle
        and str(row["transfer_id"] or "") == str(telemetry.get("transferId") or "")
        and str(row["telemetry_digest"] or "") == str(telemetry.get("telemetryDigest") or "")
    )


def _begin_effect_settlement(
    action_id: str,
    telemetry: dict[str, Any],
    *,
    consumed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Durably enter CONSUMING with a fenced terminal-effect binding."""
    if str(telemetry.get("actionId") or "") != action_id:
        raise RuntimeError("terminal telemetry Action binding mismatch")
    timestamp = _utc_iso(consumed_at)
    effect_handle = telemetry.get("effectHandle")
    if not isinstance(effect_handle, dict):
        raise RuntimeError("terminal telemetry effect handle is required")
    rendered_handle = json.dumps(effect_handle, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"fair-service reservation is unavailable for Action '{action_id}'")
        status = str(existing["status"])
        if status in {RESERVATION_CONSUMED, RESERVATION_RELEASED, RESERVATION_EXPIRED}:
            return _reservation_from_row(existing)
        if status in {RESERVATION_CONSUMING, RESERVATION_RECONCILING}:
            if not _binding_matches(existing, telemetry):
                raise RuntimeError("terminal telemetry conflicts with in-flight fair-service settlement")
            return _reservation_from_row(existing)
        if status != RESERVATION_RESERVED:
            raise RuntimeError(f"fair-service reservation cannot settle from state '{status}'")
        cursor = conn.execute(
            """
            UPDATE resilience_service_reservations
            SET status = ?, execution_epoch = ?, effect_handle_json = ?,
                transfer_id = ?, telemetry_digest = ?, settlement_started_at = ?,
                outcome = ?, updated_at = ?
            WHERE action_id = ? AND status = ?
            """,
            (
                RESERVATION_CONSUMING,
                int(telemetry.get("actionExecutionEpoch") or 0),
                rendered_handle,
                str(telemetry.get("transferId") or ""),
                str(telemetry.get("telemetryDigest") or ""),
                timestamp,
                str(telemetry.get("outcome") or "SUCCEEDED"),
                timestamp,
                action_id,
                RESERVATION_RESERVED,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("fair-service reservation settlement claim conflicted")
        _append_settlement_event(
            conn,
            action_id=action_id,
            from_status=RESERVATION_RESERVED,
            to_status=RESERVATION_CONSUMING,
            telemetry=telemetry,
            timestamp=timestamp,
        )
        row = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row is not None
        return _reservation_from_row(row)


def _finish_effect_settlement(
    action_id: str,
    telemetry: dict[str, Any],
    *,
    consumed_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _utc_iso(consumed_at)
    byte_count = max(0, int(telemetry.get("actualBytesTransferred") or 0))
    duration_ms = max(0.0, float(telemetry.get("actualDurationMs") or 0.0))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError(f"fair-service reservation is unavailable for Action '{action_id}'")
        if str(existing["status"]) == RESERVATION_CONSUMED:
            return _reservation_from_row(existing)
        if str(existing["status"]) != RESERVATION_CONSUMING or not _binding_matches(existing, telemetry):
            raise RuntimeError("fair-service settlement lost its terminal-effect binding")
        charged = _charge_consumed(
            conn,
            action_id=action_id,
            policy_id=str(existing["policy_id"]),
            byte_count=byte_count,
            timestamp=timestamp,
        )
        if not charged:
            prior_charge = conn.execute(
                """
                SELECT policy_id, bytes_served FROM resilience_scheduler_service_events
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if (
                prior_charge is None
                or str(prior_charge["policy_id"]) != str(existing["policy_id"])
                or int(prior_charge["bytes_served"]) != byte_count
            ):
                raise RuntimeError("existing fair-service charge conflicts with terminal effect telemetry")
        cursor = conn.execute(
            """
            UPDATE resilience_service_reservations
            SET status = ?, actual_bytes = ?, actual_duration_ms = ?,
                actual_traffic_class = ?, outcome = ?, updated_at = ?
            WHERE action_id = ? AND status = ?
              AND execution_epoch = ? AND telemetry_digest = ?
            """,
            (
                RESERVATION_CONSUMED,
                byte_count,
                duration_ms,
                str(telemetry.get("trafficClass") or ""),
                str(telemetry.get("outcome") or "SUCCEEDED"),
                timestamp,
                action_id,
                RESERVATION_CONSUMING,
                int(telemetry.get("actionExecutionEpoch") or 0),
                str(telemetry.get("telemetryDigest") or ""),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("fair-service terminal settlement conflicted")
        _append_settlement_event(
            conn,
            action_id=action_id,
            from_status=RESERVATION_CONSUMING,
            to_status=RESERVATION_CONSUMED,
            telemetry=telemetry,
            timestamp=timestamp,
        )
        row = conn.execute(
            "SELECT * FROM resilience_service_reservations WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row is not None
        return _reservation_from_row(row)


def list_settlement_events(action_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM resilience_service_settlement_events
            WHERE action_id = ? ORDER BY event_id
            """,
            (action_id,),
        ).fetchall()
    return [
        {
            "eventId": int(row["event_id"]),
            "actionId": str(row["action_id"]),
            "fromStatus": str(row["from_status"]),
            "toStatus": str(row["to_status"]),
            "executionEpoch": int(row["execution_epoch"]),
            "effectHandle": json.loads(str(row["effect_handle_json"])) if row["effect_handle_json"] else None,
            "transferId": row["transfer_id"],
            "telemetryDigest": row["telemetry_digest"],
            "createdAt": str(row["created_at"]),
        }
        for row in rows
    ]


def settle_action_from_effect(
    action_id: str,
    *,
    consumed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Settle one terminal Action from durable effect telemetry; callers cannot report actual service."""
    existing = get_reservation(action_id)
    if existing is None:
        raise RuntimeError(f"fair-service reservation is unavailable for Action '{action_id}'")
    if str(existing["status"]) in {RESERVATION_CONSUMED, RESERVATION_RELEASED, RESERVATION_EXPIRED}:
        return existing

    from deepseek_infra.infra.workspace import resilience_action_journal

    action = resilience_action_journal.get_action(action_id)
    if action is None:
        raise RuntimeError(f"Action Journal record is unavailable for '{action_id}'")
    terminal_state = str(action.get("state") or "")
    if terminal_state != "SUCCEEDED":
        terminal_failures = {
            "COMPENSATED",
            "COMPENSATION_REQUIRED",
            "FAILED_BEFORE_EFFECT",
            "NEEDS_OPERATOR",
            "EFFECT_UNKNOWN",
            "BLOCKED",
            "SKIPPED_NO_LONGER_NEEDED",
            "PREEMPTED",
            "REPLAN_REQUIRED",
        }
        if terminal_state in terminal_failures:
            return release_action_reservation(action_id, reason="FAILED", released_at=consumed_at)
        raise RuntimeError(f"Action '{action_id}' is not terminal")

    telemetry = _read_terminal_effect_telemetry(action_id)
    consuming = _begin_effect_settlement(action_id, telemetry, consumed_at=consumed_at)
    if consuming is None or str(consuming["status"]) in {RESERVATION_RELEASED, RESERVATION_EXPIRED}:
        return consuming
    return _finish_effect_settlement(action_id, telemetry, consumed_at=consumed_at)


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
