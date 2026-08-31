"""Fail-closed, fenced production Wave execution through the Action Journal (4.7.6 Gates A-C)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

WAVE_EXECUTOR_DIR = config.ROOT / ".resilience-waves"
WAVE_EXECUTOR_DB = WAVE_EXECUTOR_DIR / "waves.sqlite3"

SCHEDULE_PLANNED = "PLANNED"
SCHEDULE_RUNNING = "RUNNING"
SCHEDULE_COMPLETED = "COMPLETED"
SCHEDULE_PAUSED_REPLAN = "PAUSED_REPLAN"
SCHEDULE_FAILED = "FAILED"
SCHEDULE_SUPERSEDED = "SUPERSEDED"
SCHEDULE_STALE = "STALE"

WAVE_PENDING = "PENDING"
WAVE_ADMITTING = "ADMITTING"
WAVE_CLAIMING = "CLAIMING"
WAVE_RUNNING = "RUNNING"
WAVE_EXECUTING = "EXECUTING"
WAVE_VERIFYING = "VERIFYING"
WAVE_COMPLETED = "COMPLETED"

ACTION_PENDING = "PENDING"
ACTION_CLAIMED = "CLAIMED"
ACTION_EXECUTING = "EXECUTING"
ACTION_OUTCOME_VERIFICATION = "OUTCOME_VERIFICATION"
ACTION_VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
ACTION_FAILED = "FAILED"
ACTION_PREEMPTED = "PREEMPTED"
ACTION_STALE = "STALE"

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_wave_schedules (
    schedule_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    risk_digest TEXT NOT NULL,
    authority_head_digest TEXT,
    schedule_json TEXT NOT NULL,
    stale_reason TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resilience_wave_states (
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    revalidation_json TEXT,
    admitted_at TEXT,
    verified_at TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, wave_index)
);
CREATE TABLE IF NOT EXISTS resilience_wave_actions (
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    action_json TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    schedule_execution_epoch INTEGER NOT NULL DEFAULT 0,
    wave_execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    journal_execution_epoch INTEGER NOT NULL DEFAULT 0,
    effect_handle_json TEXT,
    terminal_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_resilience_wave_actions_wave
ON resilience_wave_actions(schedule_id, wave_index, status);
"""

_SCHEMA_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "resilience_wave_schedules": (
        ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("owner_instance_id", "TEXT"),
        ("lease_until", "TEXT"),
        ("runner_token", "TEXT"),
    ),
    "resilience_wave_states": (
        ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("owner_instance_id", "TEXT"),
        ("lease_until", "TEXT"),
        ("runner_token", "TEXT"),
    ),
    "resilience_wave_actions": (
        ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("schedule_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("wave_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("owner_instance_id", "TEXT"),
        ("lease_until", "TEXT"),
        ("runner_token", "TEXT"),
        ("journal_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("effect_handle_json", "TEXT"),
        ("terminal_json", "TEXT"),
    ),
}


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    for table, definitions in _SCHEMA_COLUMNS.items():
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in definitions:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    WAVE_EXECUTOR_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(WAVE_EXECUTOR_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _ensure_schema_columns(conn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _schedule_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scheduleId": str(row["schedule_id"]),
        "status": str(row["status"]),
        "riskDigest": str(row["risk_digest"]),
        "authorityHeadDigest": row["authority_head_digest"],
        "schedule": json.loads(str(row["schedule_json"])),
        "staleReason": row["stale_reason"],
        "scheduleExecutionEpoch": int(row["execution_epoch"] or 0),
        "ownerInstanceId": row["owner_instance_id"],
        "leaseUntil": row["lease_until"],
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def _wave_record(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["revalidation_json"]
    return {
        "scheduleId": str(row["schedule_id"]),
        "waveIndex": int(row["wave_index"]),
        "status": str(row["status"]),
        "revalidation": json.loads(str(raw)) if raw else None,
        "admittedAt": row["admitted_at"],
        "verifiedAt": row["verified_at"],
        "waveExecutionEpoch": int(row["execution_epoch"] or 0),
        "ownerInstanceId": row["owner_instance_id"],
        "leaseUntil": row["lease_until"],
        "updatedAt": str(row["updated_at"]),
    }


def persist_planned_schedule(
    schedule: dict[str, Any],
    *,
    authority_head_digest: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Materialize a planner schedule as PLANNED waves that cannot execute yet."""
    schedule_id = str(schedule.get("scheduleId") or "")
    if not schedule_id:
        raise ValueError("scheduleId is required")
    timestamp = _utc_iso(now)
    raw_waves = schedule.get("executionWaves")
    waves = raw_waves if isinstance(raw_waves, list) else []
    rendered = json.dumps(schedule, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO resilience_wave_schedules (
                schedule_id, status, risk_digest, authority_head_digest,
                schedule_json, stale_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(schedule_id) DO UPDATE SET
                schedule_json = excluded.schedule_json,
                risk_digest = excluded.risk_digest,
                authority_head_digest = excluded.authority_head_digest,
                updated_at = excluded.updated_at
            """,
            (
                schedule_id,
                SCHEDULE_PLANNED,
                str(schedule.get("riskDigest") or ""),
                authority_head_digest,
                rendered,
                timestamp,
                timestamp,
            ),
        )
        for wave in waves:
            if not isinstance(wave, dict):
                continue
            raw_index = wave.get("waveIndex")
            if raw_index is None:
                continue
            try:
                wave_index = int(raw_index)
            except (TypeError, ValueError):
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO resilience_wave_states (
                    schedule_id, wave_index, status, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (schedule_id, wave_index, WAVE_PENDING, timestamp),
            )
            raw_actions = wave.get("actions")
            actions = raw_actions if isinstance(raw_actions, list) else []
            for action in actions:
                if not isinstance(action, dict):
                    continue
                action_id = str(action.get("actionId") or "")
                if not action_id:
                    continue
                bound = dict(action)
                bound["waveIndex"] = wave_index
                conn.execute(
                    """
                    INSERT OR IGNORE INTO resilience_wave_actions (
                        schedule_id, wave_index, action_id, action_json, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        schedule_id,
                        wave_index,
                        action_id,
                        json.dumps(bound, ensure_ascii=False, sort_keys=True),
                        ACTION_PENDING,
                        timestamp,
                    ),
                )
        row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        assert row is not None
        return _schedule_record(row)


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
    return _schedule_record(row) if row is not None else None


def list_waves(schedule_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resilience_wave_states WHERE schedule_id = ? ORDER BY wave_index",
            (schedule_id,),
        ).fetchall()
    return [_wave_record(row) for row in rows]


def list_wave_actions(schedule_id: str, wave_index: int | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM resilience_wave_actions WHERE schedule_id = ?"
    params: list[Any] = [schedule_id]
    if wave_index is not None:
        query += " AND wave_index = ?"
        params.append(wave_index)
    query += " ORDER BY wave_index, action_id"
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "scheduleId": str(row["schedule_id"]),
            "waveIndex": int(row["wave_index"]),
            "actionId": str(row["action_id"]),
            "action": json.loads(str(row["action_json"])),
            "status": str(row["status"]),
            "outcome": row["outcome"],
            "actionExecutionEpoch": int(row["execution_epoch"] or 0),
            "scheduleExecutionEpoch": int(row["schedule_execution_epoch"] or 0),
            "waveExecutionEpoch": int(row["wave_execution_epoch"] or 0),
            "ownerInstanceId": row["owner_instance_id"],
            "leaseUntil": row["lease_until"],
            "journalExecutionEpoch": int(row["journal_execution_epoch"] or 0),
            "effectHandle": json.loads(str(row["effect_handle_json"])) if row["effect_handle_json"] else None,
            "terminal": json.loads(str(row["terminal_json"])) if row["terminal_json"] else None,
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def _predecessors_verified(conn: sqlite3.Connection, schedule_id: str, wave_index: int) -> bool:
    if wave_index <= 0:
        return True
    pending = conn.execute(
        """
        SELECT COUNT(*) FROM resilience_wave_actions
        WHERE schedule_id = ? AND wave_index < ? AND status != ?
        """,
        (schedule_id, wave_index, ACTION_VERIFIED_SUCCESS),
    ).fetchone()
    incomplete_waves = conn.execute(
        """
        SELECT COUNT(*) FROM resilience_wave_states
        WHERE schedule_id = ? AND wave_index < ? AND status != ?
        """,
        (schedule_id, wave_index, WAVE_COMPLETED),
    ).fetchone()
    return int(pending[0]) == 0 and int(incomplete_waves[0]) == 0


def revalidate_wave(
    schedule: dict[str, Any],
    fresh_state_bundle: dict[str, Any],
) -> dict[str, Any]:
    """Compare a source-backed fresh-state bundle with the immutable plan binding."""
    reasons: list[str] = []
    planned_risk = str(schedule.get("riskDigest") or "")
    fresh_risk = str(fresh_state_bundle.get("riskDigest") or "")
    if not planned_risk:
        reasons.append("PLANNED_RISK_BINDING_MISSING")
    elif fresh_risk != planned_risk:
        reasons.append("RISK_SNAPSHOT_STALE")
    planned_authority = str(schedule.get("authorityHeadDigest") or "")
    fresh_authority = str(fresh_state_bundle.get("authorityHeadDigest") or "")
    if not planned_authority:
        reasons.append("PLANNED_AUTHORITY_BINDING_MISSING")
    elif fresh_authority != planned_authority:
        reasons.append("AUTHORITY_HEAD_STALE")
    authority_state = fresh_state_bundle.get("authorityState")
    if not isinstance(authority_state, dict) or authority_state.get("workersAllowed") is not True or authority_state.get("mutationsAllowed") is not True:
        reasons.append("AUTHORITY_MUTATIONS_BLOCKED")
    maintenance = fresh_state_bundle.get("maintenanceDecisions")
    if not isinstance(maintenance, list) or any(
        not isinstance(item, dict) or item.get("allowed") is not True for item in maintenance
    ):
        reasons.append("MAINTENANCE_WINDOW_BLOCKED")
    budgets = fresh_state_bundle.get("budgets")
    if not isinstance(budgets, dict) or budgets.get("admitted") is not True:
        reasons.append("RESOURCE_OR_TRANSFER_BUDGET_DENIED")
    blast = fresh_state_bundle.get("blastSimulation")
    if not isinstance(blast, dict) or blast.get("passed") is not True:
        reasons.append("BLAST_RADIUS_REVALIDATION_FAILED")
    return {
        "fresh": not reasons,
        "reasons": reasons,
        "authorityHeadDigest": fresh_authority,
        "riskDigest": fresh_risk,
        "freshStateBundle": fresh_state_bundle,
    }


def admit_wave(
    schedule_id: str,
    wave_index: int | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Admit a wave from production truth only; missing sources leave it PENDING."""
    timestamp = _utc_iso(now)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        schedule_row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if schedule_row is None:
            raise ValueError(f"unknown schedule: {schedule_id}")
        schedule_status = str(schedule_row["status"])
        if schedule_status in {SCHEDULE_FAILED, SCHEDULE_SUPERSEDED, SCHEDULE_PAUSED_REPLAN, SCHEDULE_STALE}:
            return {**_schedule_record(schedule_row), "admitted": False, "reason": schedule_status}
        if wave_index is None:
            pending = conn.execute(
                """
                SELECT wave_index FROM resilience_wave_states
                WHERE schedule_id = ? AND status = ?
                ORDER BY wave_index LIMIT 1
                """,
                (schedule_id, WAVE_PENDING),
            ).fetchone()
            if pending is None:
                return {**_schedule_record(schedule_row), "admitted": False, "reason": "NO_PENDING_WAVE"}
            wave_index = int(pending[0])
        wave_row = conn.execute(
            "SELECT * FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
            (schedule_id, wave_index),
        ).fetchone()
        if wave_row is None:
            raise ValueError(f"unknown wave: {schedule_id}/{wave_index}")
        if str(wave_row["status"]) != WAVE_PENDING:
            return {**_wave_record(wave_row), "admitted": False, "reason": str(wave_row["status"])}
        if not _predecessors_verified(conn, schedule_id, wave_index):
            return {
                **_wave_record(wave_row),
                "admitted": False,
                "reason": "PREDECESSOR_WAVE_NOT_VERIFIED",
            }
        schedule_payload = _schedule_record(schedule_row)
        action_rows = conn.execute(
            "SELECT action_json FROM resilience_wave_actions WHERE schedule_id = ? AND wave_index = ? ORDER BY action_id",
            (schedule_id, wave_index),
        ).fetchall()
        wave_actions = [json.loads(str(row["action_json"])) for row in action_rows]
        claimed = conn.execute(
            """
            UPDATE resilience_wave_states
            SET status = ?, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ? AND status = ?
            """,
            (WAVE_ADMITTING, timestamp, schedule_id, wave_index, WAVE_PENDING),
        )
        if claimed.rowcount != 1:
            current = conn.execute(
                "SELECT * FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
                (schedule_id, wave_index),
            ).fetchone()
            assert current is not None
            return {**_wave_record(current), "admitted": False, "reason": str(current["status"])}

    from deepseek_infra.infra.workspace import resilience_fresh_state

    try:
        fresh_state_bundle = resilience_fresh_state.build_fresh_state_bundle(
            schedule_payload,
            wave_actions,
            now=now,
        )
    except resilience_fresh_state.FreshStateUnavailable as exc:
        revalidation: dict[str, Any] = {
            "fresh": False,
            "reasons": [exc.reason],
            "sourceUnavailable": exc.source,
            "detail": exc.detail,
        }
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE resilience_wave_states
                SET status = ?, revalidation_json = ?, updated_at = ?
                WHERE schedule_id = ? AND wave_index = ? AND status = ?
                """,
                (
                    WAVE_PENDING,
                    json.dumps(revalidation, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    schedule_id,
                    wave_index,
                    WAVE_ADMITTING,
                ),
            )
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "admitted": False,
            "status": "WAVE_NOT_ADMITTED",
            "reason": exc.reason,
            "revalidation": revalidation,
        }
    except Exception as exc:
        revalidation = {
            "fresh": False,
            "reasons": ["FRESH_STATE_BUILD_FAILED"],
            "sourceUnavailable": "fresh-state-bundle",
            "detail": type(exc).__name__,
        }
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE resilience_wave_states
                SET status = ?, revalidation_json = ?, updated_at = ?
                WHERE schedule_id = ? AND wave_index = ? AND status = ?
                """,
                (
                    WAVE_PENDING,
                    json.dumps(revalidation, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    schedule_id,
                    wave_index,
                    WAVE_ADMITTING,
                ),
            )
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "admitted": False,
            "status": "WAVE_NOT_ADMITTED",
            "reason": "FRESH_STATE_BUILD_FAILED",
            "revalidation": revalidation,
        }

    revalidation = revalidate_wave(schedule_payload, fresh_state_bundle)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        wave_row = conn.execute(
            "SELECT * FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
            (schedule_id, wave_index),
        ).fetchone()
        if wave_row is None:
            raise ValueError(f"unknown wave: {schedule_id}/{wave_index}")
        if str(wave_row["status"]) != WAVE_ADMITTING:
            return {**_wave_record(wave_row), "admitted": False, "reason": str(wave_row["status"])}
        rendered_revalidation = json.dumps(revalidation, ensure_ascii=False, sort_keys=True)
        if not revalidation["fresh"]:
            conn.execute(
                """
                UPDATE resilience_wave_schedules
                SET status = ?, stale_reason = ?, updated_at = ?
                WHERE schedule_id = ?
                """,
                (SCHEDULE_PAUSED_REPLAN, ",".join(str(item) for item in revalidation["reasons"]), timestamp, schedule_id),
            )
            conn.execute(
                """
                UPDATE resilience_wave_states
                SET status = ?, revalidation_json = ?, updated_at = ?
                WHERE schedule_id = ? AND wave_index = ?
                """,
                (WAVE_PENDING, rendered_revalidation, timestamp, schedule_id, wave_index),
            )
            stale_result = {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "admitted": False,
                "status": SCHEDULE_PAUSED_REPLAN,
                "reason": "STALE",
                "revalidation": revalidation,
            }
        else:
            conn.execute(
                """
                UPDATE resilience_wave_states
                SET status = ?, revalidation_json = ?, admitted_at = ?, updated_at = ?
                WHERE schedule_id = ? AND wave_index = ?
                """,
                (WAVE_CLAIMING, rendered_revalidation, timestamp, timestamp, schedule_id, wave_index),
            )
            conn.execute(
                """
                UPDATE resilience_wave_schedules
                SET status = ?, updated_at = ?
                WHERE schedule_id = ?
                """,
                (SCHEDULE_RUNNING, timestamp, schedule_id),
            )
            wave = conn.execute(
                "SELECT * FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
                (schedule_id, wave_index),
            ).fetchone()
            assert wave is not None
            admitted_result = {**_wave_record(wave), "admitted": True, "revalidation": revalidation}
    if not revalidation["fresh"]:
        from deepseek_infra.infra.workspace import resilience_scheduler_service

        resilience_scheduler_service.release_schedule_reservations(schedule_id, reason="STALE", released_at=now)
        return stale_result
    return admitted_result


def _lease_is_active(lease_until: Any, *, now_iso: str) -> bool:
    return bool(lease_until) and str(lease_until) >= now_iso


def _claim_runner_epochs(
    schedule_id: str,
    wave_index: int,
    *,
    instance_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim schedule and wave leases together and advance both fencing epochs."""
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    current = now or datetime.now(tz=timezone.utc)
    timestamp = _utc_iso(current)
    lease_until = _utc_iso(current + timedelta(seconds=lease_seconds))
    runner_token = uuid.uuid4().hex
    runnable_wave_states = {WAVE_CLAIMING, WAVE_RUNNING, WAVE_EXECUTING, WAVE_VERIFYING}
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        schedule_row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if schedule_row is None:
            raise ValueError(f"unknown schedule: {schedule_id}")
        if str(schedule_row["status"]) in {
            SCHEDULE_COMPLETED,
            SCHEDULE_FAILED,
            SCHEDULE_SUPERSEDED,
            SCHEDULE_PAUSED_REPLAN,
            SCHEDULE_STALE,
        }:
            return {"claimed": False, "reason": str(schedule_row["status"])}
        if schedule_row["runner_token"] and _lease_is_active(schedule_row["lease_until"], now_iso=timestamp):
            return {"claimed": False, "reason": "SCHEDULE_RUNNER_LEASE_HELD"}

        wave_row = conn.execute(
            "SELECT * FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
            (schedule_id, wave_index),
        ).fetchone()
        if wave_row is None:
            raise ValueError(f"unknown wave: {schedule_id}/{wave_index}")
        if str(wave_row["status"]) not in runnable_wave_states:
            return {"claimed": False, "reason": str(wave_row["status"])}
        if wave_row["runner_token"] and _lease_is_active(wave_row["lease_until"], now_iso=timestamp):
            return {"claimed": False, "reason": "WAVE_RUNNER_LEASE_HELD"}

        conn.execute(
            """
            UPDATE resilience_wave_schedules
            SET status = ?, execution_epoch = execution_epoch + 1,
                owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
            WHERE schedule_id = ?
            """,
            (SCHEDULE_RUNNING, instance_id, lease_until, runner_token, timestamp, schedule_id),
        )
        conn.execute(
            """
            UPDATE resilience_wave_states
            SET status = ?, execution_epoch = execution_epoch + 1,
                owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ?
            """,
            (WAVE_CLAIMING, instance_id, lease_until, runner_token, timestamp, schedule_id, wave_index),
        )
        claimed_schedule = conn.execute(
            "SELECT execution_epoch FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        claimed_wave = conn.execute(
            "SELECT execution_epoch FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
            (schedule_id, wave_index),
        ).fetchone()
        assert claimed_schedule is not None and claimed_wave is not None
        return {
            "claimed": True,
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "scheduleExecutionEpoch": int(claimed_schedule[0]),
            "waveExecutionEpoch": int(claimed_wave[0]),
            "runnerToken": runner_token,
            "leaseUntil": lease_until,
        }


def _claim_wave_action(
    schedule_id: str,
    wave_index: int,
    action_id: str,
    *,
    instance_id: str,
    lease_until: str,
    schedule_execution_epoch: int,
    wave_execution_epoch: int,
    runner_token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _utc_iso(now)
    active_states = {ACTION_CLAIMED, ACTION_EXECUTING, ACTION_OUTCOME_VERIFICATION}
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM resilience_wave_actions WHERE schedule_id = ? AND action_id = ? AND wave_index = ?",
            (schedule_id, action_id, wave_index),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown wave action: {schedule_id}/{action_id}")
        status = str(row["status"])
        if status == ACTION_VERIFIED_SUCCESS:
            return {"claimed": False, "terminal": True, "reason": status}
        if status in {ACTION_FAILED, ACTION_PREEMPTED, ACTION_STALE}:
            return {"claimed": False, "terminal": True, "reason": status}
        if status in active_states and row["runner_token"] and _lease_is_active(row["lease_until"], now_iso=timestamp):
            return {"claimed": False, "terminal": False, "reason": "ACTION_RUNNER_LEASE_HELD"}
        cursor = conn.execute(
            """
            UPDATE resilience_wave_actions
            SET status = ?, execution_epoch = execution_epoch + 1,
                schedule_execution_epoch = ?, wave_execution_epoch = ?,
                owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ? AND action_id = ?
              AND status IN (?, ?, ?, ?)
            """,
            (
                ACTION_CLAIMED,
                schedule_execution_epoch,
                wave_execution_epoch,
                instance_id,
                lease_until,
                runner_token,
                timestamp,
                schedule_id,
                wave_index,
                action_id,
                ACTION_PENDING,
                ACTION_CLAIMED,
                ACTION_EXECUTING,
                ACTION_OUTCOME_VERIFICATION,
            ),
        )
        if cursor.rowcount != 1:
            return {"claimed": False, "terminal": False, "reason": "ACTION_CLAIM_CONFLICT"}
        claimed = conn.execute(
            "SELECT execution_epoch FROM resilience_wave_actions WHERE schedule_id = ? AND action_id = ?",
            (schedule_id, action_id),
        ).fetchone()
        assert claimed is not None
        return {
            "claimed": True,
            "actionExecutionEpoch": int(claimed[0]),
        }


def _set_wave_state_with_fence(
    schedule_id: str,
    wave_index: int,
    *,
    status: str,
    schedule_execution_epoch: int,
    wave_execution_epoch: int,
    runner_token: str,
    now: datetime | None = None,
) -> bool:
    timestamp = _utc_iso(now)
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE resilience_wave_states
            SET status = ?, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ?
              AND execution_epoch = ? AND runner_token = ?
              AND EXISTS (
                  SELECT 1 FROM resilience_wave_schedules AS schedule
                  WHERE schedule.schedule_id = resilience_wave_states.schedule_id
                    AND schedule.execution_epoch = ?
                    AND schedule.runner_token = ?
              )
            """,
            (
                status,
                timestamp,
                schedule_id,
                wave_index,
                wave_execution_epoch,
                runner_token,
                schedule_execution_epoch,
                runner_token,
            ),
        )
        return cursor.rowcount == 1


def _set_action_state_with_fence(
    schedule_id: str,
    wave_index: int,
    action_id: str,
    *,
    status: str,
    action_execution_epoch: int,
    schedule_execution_epoch: int,
    wave_execution_epoch: int,
    runner_token: str,
    journal_action: dict[str, Any] | None = None,
    outcome: str | None = None,
    terminal: bool = False,
    now: datetime | None = None,
) -> bool:
    timestamp = _utc_iso(now)
    journal_epoch = int((journal_action or {}).get("executionEpoch") or 0)
    effect_handle = (journal_action or {}).get("effectHandle")
    rendered_effect = json.dumps(effect_handle, ensure_ascii=False, sort_keys=True) if isinstance(effect_handle, dict) else None
    rendered_terminal = json.dumps(journal_action, ensure_ascii=False, sort_keys=True) if journal_action is not None else None
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE resilience_wave_actions
            SET status = ?, outcome = COALESCE(?, outcome),
                journal_execution_epoch = CASE WHEN ? > 0 THEN ? ELSE journal_execution_epoch END,
                effect_handle_json = COALESCE(?, effect_handle_json),
                terminal_json = COALESCE(?, terminal_json),
                owner_instance_id = CASE WHEN ? THEN NULL ELSE owner_instance_id END,
                lease_until = CASE WHEN ? THEN NULL ELSE lease_until END,
                runner_token = CASE WHEN ? THEN NULL ELSE runner_token END,
                updated_at = ?
            WHERE schedule_id = ? AND wave_index = ? AND action_id = ?
              AND execution_epoch = ?
              AND schedule_execution_epoch = ?
              AND wave_execution_epoch = ?
              AND runner_token = ?
              AND EXISTS (
                  SELECT 1 FROM resilience_wave_schedules AS schedule
                  WHERE schedule.schedule_id = resilience_wave_actions.schedule_id
                    AND schedule.execution_epoch = ?
                    AND schedule.runner_token = ?
              )
              AND EXISTS (
                  SELECT 1 FROM resilience_wave_states AS wave
                  WHERE wave.schedule_id = resilience_wave_actions.schedule_id
                    AND wave.wave_index = resilience_wave_actions.wave_index
                    AND wave.execution_epoch = ?
                    AND wave.runner_token = ?
              )
            """,
            (
                status,
                outcome,
                journal_epoch,
                journal_epoch,
                rendered_effect,
                rendered_terminal,
                terminal,
                terminal,
                terminal,
                timestamp,
                schedule_id,
                wave_index,
                action_id,
                action_execution_epoch,
                schedule_execution_epoch,
                wave_execution_epoch,
                runner_token,
                schedule_execution_epoch,
                runner_token,
                wave_execution_epoch,
                runner_token,
            ),
        )
        return cursor.rowcount == 1


def _complete_wave_with_fence(
    schedule_id: str,
    wave_index: int,
    *,
    schedule_execution_epoch: int,
    wave_execution_epoch: int,
    runner_token: str,
    now: datetime | None = None,
) -> bool:
    timestamp = _utc_iso(now)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        schedule_owner = conn.execute(
            """
            SELECT 1 FROM resilience_wave_schedules
            WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ?
            """,
            (schedule_id, schedule_execution_epoch, runner_token),
        ).fetchone()
        wave_owner = conn.execute(
            """
            SELECT 1 FROM resilience_wave_states
            WHERE schedule_id = ? AND wave_index = ?
              AND execution_epoch = ? AND runner_token = ?
            """,
            (schedule_id, wave_index, wave_execution_epoch, runner_token),
        ).fetchone()
        if schedule_owner is None or wave_owner is None:
            return False
        incomplete = conn.execute(
            """
            SELECT COUNT(*) FROM resilience_wave_actions
            WHERE schedule_id = ? AND wave_index = ? AND status != ?
            """,
            (schedule_id, wave_index, ACTION_VERIFIED_SUCCESS),
        ).fetchone()
        assert incomplete is not None
        if int(incomplete[0]) != 0:
            return False
        wave_cursor = conn.execute(
            """
            UPDATE resilience_wave_states
            SET status = ?, verified_at = ?, owner_instance_id = NULL,
                lease_until = NULL, runner_token = NULL, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ?
              AND execution_epoch = ? AND runner_token = ?
            """,
            (
                WAVE_COMPLETED,
                timestamp,
                timestamp,
                schedule_id,
                wave_index,
                wave_execution_epoch,
                runner_token,
            ),
        )
        if wave_cursor.rowcount != 1:
            return False
        remaining = conn.execute(
            "SELECT COUNT(*) FROM resilience_wave_states WHERE schedule_id = ? AND status != ?",
            (schedule_id, WAVE_COMPLETED),
        ).fetchone()
        assert remaining is not None
        schedule_status = SCHEDULE_COMPLETED if int(remaining[0]) == 0 else SCHEDULE_RUNNING
        schedule_cursor = conn.execute(
            """
            UPDATE resilience_wave_schedules
            SET status = ?, owner_instance_id = NULL, lease_until = NULL,
                runner_token = NULL, updated_at = ?
            WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ?
            """,
            (schedule_status, timestamp, schedule_id, schedule_execution_epoch, runner_token),
        )
        if schedule_cursor.rowcount != 1:
            return False
        return True


def _fail_action_with_fence(
    schedule_id: str,
    wave_index: int,
    action_id: str,
    *,
    journal_action: dict[str, Any],
    action_execution_epoch: int,
    schedule_execution_epoch: int,
    wave_execution_epoch: int,
    runner_token: str,
    now: datetime | None = None,
) -> bool:
    timestamp = _utc_iso(now)
    terminal_state = str(journal_action.get("state") or "FAILED")
    updated = _set_action_state_with_fence(
        schedule_id,
        wave_index,
        action_id,
        status=ACTION_FAILED,
        outcome=terminal_state,
        journal_action=journal_action,
        action_execution_epoch=action_execution_epoch,
        schedule_execution_epoch=schedule_execution_epoch,
        wave_execution_epoch=wave_execution_epoch,
        runner_token=runner_token,
        terminal=True,
        now=now,
    )
    if not updated:
        return False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        wave_cursor = conn.execute(
            """
            UPDATE resilience_wave_states
            SET status = ?, owner_instance_id = NULL, lease_until = NULL,
                runner_token = NULL, updated_at = ?
            WHERE schedule_id = ? AND wave_index = ?
              AND execution_epoch = ? AND runner_token = ?
            """,
            (WAVE_VERIFYING, timestamp, schedule_id, wave_index, wave_execution_epoch, runner_token),
        )
        schedule_cursor = conn.execute(
            """
            UPDATE resilience_wave_schedules
            SET status = ?, owner_instance_id = NULL, lease_until = NULL,
                runner_token = NULL, updated_at = ?
            WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ?
            """,
            (SCHEDULE_FAILED, timestamp, schedule_id, schedule_execution_epoch, runner_token),
        )
        return wave_cursor.rowcount == 1 and schedule_cursor.rowcount == 1


_JOURNAL_ACTIVE_STATES = {"CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT"}
_JOURNAL_FAILURE_STATES = {
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


def _journal_success_is_verified(action: dict[str, Any]) -> bool:
    return str(action.get("state") or "") == "SUCCEEDED" and isinstance(action.get("verificationResult"), dict)


def run_next_wave(
    schedule_id: str,
    *,
    instance_id: str = "resilience-wave-worker",
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Admit and execute one complete Wave through the production Action Journal."""
    current = now or datetime.now(tz=timezone.utc)
    timestamp = _utc_iso(current)
    with _connect() as conn:
        schedule_row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        if schedule_row is None:
            raise ValueError(f"unknown schedule: {schedule_id}")
        schedule_status = str(schedule_row["status"])
        if schedule_status in {SCHEDULE_COMPLETED, SCHEDULE_FAILED, SCHEDULE_SUPERSEDED, SCHEDULE_PAUSED_REPLAN, SCHEDULE_STALE}:
            return {**_schedule_record(schedule_row), "ran": False, "reason": schedule_status}
        active = conn.execute(
            """
            SELECT wave_index FROM resilience_wave_states
            WHERE schedule_id = ? AND status IN (?, ?, ?, ?)
            ORDER BY wave_index LIMIT 1
            """,
            (schedule_id, WAVE_CLAIMING, WAVE_RUNNING, WAVE_EXECUTING, WAVE_VERIFYING),
        ).fetchone()
        if active is not None:
            wave_index = int(active[0])
        else:
            pending = conn.execute(
                """
                SELECT wave_index FROM resilience_wave_states
                WHERE schedule_id = ? AND status = ?
                ORDER BY wave_index LIMIT 1
                """,
                (schedule_id, WAVE_PENDING),
            ).fetchone()
            if pending is None:
                return {**_schedule_record(schedule_row), "ran": False, "reason": "NO_RUNNABLE_WAVE"}
            wave_index = int(pending[0])

    if active is None:
        admission = admit_wave(schedule_id, wave_index, now=now)
        if admission.get("admitted") is not True:
            return admission

    claim = _claim_runner_epochs(
        schedule_id,
        wave_index,
        instance_id=instance_id,
        lease_seconds=lease_seconds,
        now=now,
    )
    if claim.get("claimed") is not True:
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": False,
            "status": "WAVE_NOT_CLAIMED",
            "reason": claim.get("reason"),
        }
    schedule_epoch = int(claim["scheduleExecutionEpoch"])
    wave_epoch = int(claim["waveExecutionEpoch"])
    runner_token = str(claim["runnerToken"])
    lease_until = str(claim["leaseUntil"])

    with _connect() as conn:
        schedule_row = conn.execute(
            "SELECT * FROM resilience_wave_schedules WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()
        action_rows = conn.execute(
            """
            SELECT * FROM resilience_wave_actions
            WHERE schedule_id = ? AND wave_index = ?
            ORDER BY action_id
            """,
            (schedule_id, wave_index),
        ).fetchall()
    assert schedule_row is not None
    schedule = _schedule_record(schedule_row)
    action_results: list[dict[str, Any]] = []

    from deepseek_infra.infra.workspace import resilience_action_journal, resilience_scheduler_service

    if not _set_wave_state_with_fence(
        schedule_id,
        wave_index,
        status=WAVE_EXECUTING,
        schedule_execution_epoch=schedule_epoch,
        wave_execution_epoch=wave_epoch,
        runner_token=runner_token,
        now=now,
    ):
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": False,
            "status": "RUNNER_FENCED_OUT",
        }

    for row in action_rows:
        action_id = str(row["action_id"])
        if str(row["status"]) == ACTION_VERIFIED_SUCCESS:
            continue
        action_claim = _claim_wave_action(
            schedule_id,
            wave_index,
            action_id,
            instance_id=instance_id,
            lease_until=lease_until,
            schedule_execution_epoch=schedule_epoch,
            wave_execution_epoch=wave_epoch,
            runner_token=runner_token,
            now=now,
        )
        if action_claim.get("claimed") is not True:
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "ACTION_NOT_CLAIMED",
                "actionId": action_id,
                "reason": action_claim.get("reason"),
            }
        action_epoch = int(action_claim["actionExecutionEpoch"])
        action_payload = json.loads(str(row["action_json"]))
        try:
            journal_action = resilience_action_journal.record_action_intent(
                action_payload,
                created_by="resilience-wave-runner",
                plan_id=schedule_id,
                input_risk_digest=str(schedule.get("riskDigest") or ""),
                plan_digest=str(schedule["schedule"].get("planDigest") or ""),
                now=now,
            )
        except Exception as exc:
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "ACTION_JOURNAL_UNAVAILABLE",
                "actionId": action_id,
                "reason": type(exc).__name__,
            }

        if _journal_success_is_verified(journal_action):
            terminal_action = journal_action
        elif str(journal_action.get("state") or "") in _JOURNAL_FAILURE_STATES:
            _fail_action_with_fence(
                schedule_id,
                wave_index,
                action_id,
                journal_action=journal_action,
                action_execution_epoch=action_epoch,
                schedule_execution_epoch=schedule_epoch,
                wave_execution_epoch=wave_epoch,
                runner_token=runner_token,
                now=now,
            )
            resilience_scheduler_service.release_action_reservation(action_id, reason="FAILED", released_at=now)
            resilience_scheduler_service.release_schedule_reservations(schedule_id, reason="FAILED", released_at=now)
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": True,
                "status": "FAILED",
                "actionId": action_id,
                "outcome": journal_action.get("state"),
            }
        else:
            journal_state = str(journal_action.get("state") or "")
            if journal_state in _JOURNAL_ACTIVE_STATES and _lease_is_active(journal_action.get("leaseUntil"), now_iso=timestamp):
                _set_action_state_with_fence(
                    schedule_id,
                    wave_index,
                    action_id,
                    status=ACTION_EXECUTING,
                    action_execution_epoch=action_epoch,
                    schedule_execution_epoch=schedule_epoch,
                    wave_execution_epoch=wave_epoch,
                    runner_token=runner_token,
                    journal_action=journal_action,
                    now=now,
                )
                return {
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": False,
                    "status": "ACTION_EFFECT_IN_PROGRESS",
                    "actionId": action_id,
                }
            if not _set_action_state_with_fence(
                schedule_id,
                wave_index,
                action_id,
                status=ACTION_EXECUTING,
                action_execution_epoch=action_epoch,
                schedule_execution_epoch=schedule_epoch,
                wave_execution_epoch=wave_epoch,
                runner_token=runner_token,
                journal_action=journal_action,
                now=now,
            ):
                return {
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": False,
                    "status": "RUNNER_FENCED_OUT",
                    "actionId": action_id,
                }
            try:
                terminal_action = resilience_action_journal.execute_autonomous_action(
                    action_id,
                    instance_id=instance_id,
                    lease_seconds=lease_seconds,
                )
            except Exception as exc:
                observed = resilience_action_journal.get_action(action_id) or {}
                if _journal_success_is_verified(observed):
                    terminal_action = observed
                elif str(observed.get("state") or "") in _JOURNAL_FAILURE_STATES:
                    _fail_action_with_fence(
                        schedule_id,
                        wave_index,
                        action_id,
                        journal_action=observed,
                        action_execution_epoch=action_epoch,
                        schedule_execution_epoch=schedule_epoch,
                        wave_execution_epoch=wave_epoch,
                        runner_token=runner_token,
                        now=now,
                    )
                    resilience_scheduler_service.release_action_reservation(action_id, reason="FAILED", released_at=now)
                    resilience_scheduler_service.release_schedule_reservations(schedule_id, reason="FAILED", released_at=now)
                    return {
                        "scheduleId": schedule_id,
                        "waveIndex": wave_index,
                        "ran": True,
                        "status": "FAILED",
                        "actionId": action_id,
                        "outcome": observed.get("state"),
                    }
                else:
                    return {
                        "scheduleId": schedule_id,
                        "waveIndex": wave_index,
                        "ran": False,
                        "status": "ACTION_EXECUTION_RETRY_REQUIRED",
                        "actionId": action_id,
                        "reason": type(exc).__name__,
                    }

        if not _journal_success_is_verified(terminal_action):
            failure = terminal_action if isinstance(terminal_action, dict) else {"state": "INVALID_TERMINAL_RESULT"}
            _fail_action_with_fence(
                schedule_id,
                wave_index,
                action_id,
                journal_action=failure,
                action_execution_epoch=action_epoch,
                schedule_execution_epoch=schedule_epoch,
                wave_execution_epoch=wave_epoch,
                runner_token=runner_token,
                now=now,
            )
            resilience_scheduler_service.release_action_reservation(action_id, reason="FAILED", released_at=now)
            resilience_scheduler_service.release_schedule_reservations(schedule_id, reason="FAILED", released_at=now)
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": True,
                "status": "FAILED",
                "actionId": action_id,
                "outcome": failure.get("state"),
            }
        if not _set_action_state_with_fence(
            schedule_id,
            wave_index,
            action_id,
            status=ACTION_OUTCOME_VERIFICATION,
            action_execution_epoch=action_epoch,
            schedule_execution_epoch=schedule_epoch,
            wave_execution_epoch=wave_epoch,
            runner_token=runner_token,
            journal_action=terminal_action,
            now=now,
        ):
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "RUNNER_FENCED_OUT",
                "actionId": action_id,
            }
        try:
            settlement = resilience_scheduler_service.settle_action_from_effect(action_id, consumed_at=now)
        except Exception as exc:
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "FAIR_SERVICE_SETTLEMENT_REQUIRED",
                "actionId": action_id,
                "reason": type(exc).__name__,
            }
        if not isinstance(settlement, dict) or settlement.get("status") != "CONSUMED":
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "FAIR_SERVICE_SETTLEMENT_REQUIRED",
                "actionId": action_id,
            }
        if not _set_action_state_with_fence(
            schedule_id,
            wave_index,
            action_id,
            status=ACTION_VERIFIED_SUCCESS,
            outcome="SUCCEEDED",
            action_execution_epoch=action_epoch,
            schedule_execution_epoch=schedule_epoch,
            wave_execution_epoch=wave_epoch,
            runner_token=runner_token,
            journal_action=terminal_action,
            terminal=True,
            now=now,
        ):
            return {
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": False,
                "status": "RUNNER_FENCED_OUT",
                "actionId": action_id,
            }
        action_results.append(
            {
                "actionId": action_id,
                "status": ACTION_VERIFIED_SUCCESS,
                "journalExecutionEpoch": int(terminal_action.get("executionEpoch") or 0),
                "effectHandle": terminal_action.get("effectHandle"),
                "fairServiceSettlement": settlement,
            }
        )

    if not _set_wave_state_with_fence(
        schedule_id,
        wave_index,
        status=WAVE_VERIFYING,
        schedule_execution_epoch=schedule_epoch,
        wave_execution_epoch=wave_epoch,
        runner_token=runner_token,
        now=now,
    ):
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": False,
            "status": "RUNNER_FENCED_OUT",
        }
    completed = _complete_wave_with_fence(
        schedule_id,
        wave_index,
        schedule_execution_epoch=schedule_epoch,
        wave_execution_epoch=wave_epoch,
        runner_token=runner_token,
        now=now,
    )
    if not completed:
        return {
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": False,
            "status": "RUNNER_FENCED_OUT",
        }
    return {
        "scheduleId": schedule_id,
        "waveIndex": wave_index,
        "ran": True,
        "status": WAVE_COMPLETED,
        "scheduleExecutionEpoch": schedule_epoch,
        "waveExecutionEpoch": wave_epoch,
        "actions": action_results,
    }


def preempt_wave_action(
    schedule_id: str,
    action_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _utc_iso(now)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE resilience_wave_actions
            SET status = ?, outcome = ?, updated_at = ?
            WHERE schedule_id = ? AND action_id = ? AND status = ?
            """,
            (ACTION_PREEMPTED, "PREEMPTED", timestamp, schedule_id, action_id, ACTION_PENDING),
        )
    from deepseek_infra.infra.workspace import resilience_scheduler_service

    reservation = resilience_scheduler_service.release_action_reservation(action_id, reason="PREEMPTED", released_at=now)
    return {"scheduleId": schedule_id, "actionId": action_id, "status": ACTION_PREEMPTED, "reservation": reservation}
