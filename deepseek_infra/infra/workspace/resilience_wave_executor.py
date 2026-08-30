"""Durable multi-wave execution governance with per-wave revalidation (4.7.5 Gates C-D)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
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
WAVE_RUNNING = "RUNNING"
WAVE_VERIFYING = "VERIFYING"
WAVE_COMPLETED = "COMPLETED"

ACTION_PENDING = "PENDING"
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
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_resilience_wave_actions_wave
ON resilience_wave_actions(schedule_id, wave_index, status);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    WAVE_EXECUTOR_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(WAVE_EXECUTOR_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
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
                (WAVE_RUNNING, rendered_revalidation, timestamp, timestamp, schedule_id, wave_index),
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


def verify_wave_action(
    schedule_id: str,
    action_id: str,
    *,
    success: bool,
    outcome: str | None = None,
    actual_bytes: int | None = None,
    actual_duration_ms: float | int | None = None,
    actual_traffic_class: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a verified terminal action and settle fair-service reservation."""
    timestamp = _utc_iso(now)
    action_status = ACTION_VERIFIED_SUCCESS if success else ACTION_FAILED
    terminal_outcome = str(outcome or ("SUCCEEDED" if success else "FAILED"))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM resilience_wave_actions WHERE schedule_id = ? AND action_id = ?",
            (schedule_id, action_id),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown wave action: {schedule_id}/{action_id}")
        conn.execute(
            """
            UPDATE resilience_wave_actions
            SET status = ?, outcome = ?, updated_at = ?
            WHERE schedule_id = ? AND action_id = ?
            """,
            (action_status, terminal_outcome, timestamp, schedule_id, action_id),
        )
        wave_index = int(existing["wave_index"])
        action = json.loads(str(existing["action_json"]))
        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM resilience_wave_actions
            WHERE schedule_id = ? AND wave_index = ? AND status = ?
            """,
            (schedule_id, wave_index, ACTION_PENDING),
        ).fetchone()
        failed = conn.execute(
            """
            SELECT COUNT(*) FROM resilience_wave_actions
            WHERE schedule_id = ? AND wave_index = ? AND status = ?
            """,
            (schedule_id, wave_index, ACTION_FAILED),
        ).fetchone()
        if int(remaining[0]) == 0:
            wave_status = WAVE_COMPLETED if int(failed[0]) == 0 else WAVE_VERIFYING
            conn.execute(
                """
                UPDATE resilience_wave_states
                SET status = ?, verified_at = ?, updated_at = ?
                WHERE schedule_id = ? AND wave_index = ?
                """,
                (wave_status if wave_status == WAVE_COMPLETED else WAVE_VERIFYING, timestamp, timestamp, schedule_id, wave_index),
            )
            if int(failed[0]) > 0:
                conn.execute(
                    """
                    UPDATE resilience_wave_schedules
                    SET status = ?, updated_at = ?
                    WHERE schedule_id = ?
                    """,
                    (SCHEDULE_FAILED, timestamp, schedule_id),
                )
            else:
                pending_waves = conn.execute(
                    """
                    SELECT COUNT(*) FROM resilience_wave_states
                    WHERE schedule_id = ? AND status != ?
                    """,
                    (schedule_id, WAVE_COMPLETED),
                ).fetchone()
                if int(pending_waves[0]) == 0:
                    conn.execute(
                        """
                        UPDATE resilience_wave_schedules
                        SET status = ?, updated_at = ?
                        WHERE schedule_id = ?
                        """,
                        (SCHEDULE_COMPLETED, timestamp, schedule_id),
                    )
        row = conn.execute(
            "SELECT * FROM resilience_wave_actions WHERE schedule_id = ? AND action_id = ?",
            (schedule_id, action_id),
        ).fetchone()
        assert row is not None
    from deepseek_infra.infra.workspace import resilience_scheduler_service

    if success:
        resilience_scheduler_service.consume_action_service(
            action,
            actual_bytes=actual_bytes,
            actual_duration_ms=actual_duration_ms,
            actual_traffic_class=actual_traffic_class,
            outcome=terminal_outcome,
            consumed_at=now,
        )
    else:
        resilience_scheduler_service.release_action_reservation(action_id, reason="FAILED", released_at=now)
        resilience_scheduler_service.release_schedule_reservations(schedule_id, reason="FAILED", released_at=now)
    return {
        "scheduleId": schedule_id,
        "waveIndex": int(row["wave_index"]),
        "actionId": action_id,
        "status": str(row["status"]),
        "outcome": row["outcome"],
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
