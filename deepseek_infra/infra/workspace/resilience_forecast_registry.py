"""Durable forecast records and automatic due-date calibration (4.7.6 Gates F-G)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import resilience_capacity_forecast, resilience_capacity_history, resilience_forecast_backtest

CAPACITY_HISTORY_DIR = config.ROOT / ".resilience-capacity"
CAPACITY_HISTORY_DB = CAPACITY_HISTORY_DIR / "capacity.sqlite3"

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_forecast_records (
    forecast_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    target_incarnation TEXT NOT NULL,
    capacity_revision TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    forecasted_at TEXT NOT NULL,
    evaluation_due_at TEXT NOT NULL,
    p50_free_bytes INTEGER NOT NULL,
    p90_free_bytes INTEGER NOT NULL,
    forecast_digest TEXT NOT NULL,
    observation_set_digest TEXT NOT NULL,
    forecast_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'DUE', 'BACKTESTED')),
    actual_observation_key TEXT,
    backtest_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_forecast_active
ON resilience_forecast_records(target_id, target_incarnation, capacity_revision, horizon_days, status);
CREATE INDEX IF NOT EXISTS idx_resilience_forecast_due
ON resilience_forecast_records(status, evaluation_due_at);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    CAPACITY_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(CAPACITY_HISTORY_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        forecast = json.loads(str(row["forecast_json"]))
    except json.JSONDecodeError:
        forecast = {}
    return {
        "forecastId": str(row["forecast_id"]),
        "targetId": str(row["target_id"]),
        "targetIncarnation": str(row["target_incarnation"]),
        "capacityRevision": str(row["capacity_revision"]),
        "horizonDays": int(row["horizon_days"]),
        "forecastedAt": str(row["forecasted_at"]),
        "evaluationDueAt": str(row["evaluation_due_at"]),
        "p50FreeBytes": int(row["p50_free_bytes"]),
        "p90FreeBytes": int(row["p90_free_bytes"]),
        "forecastDigest": str(row["forecast_digest"]),
        "capacityObservationSetDigest": str(row["observation_set_digest"]),
        "status": str(row["status"]),
        "actualObservationKey": str(row["actual_observation_key"] or ""),
        "backtestKey": str(row["backtest_key"] or ""),
        "forecast": forecast if isinstance(forecast, dict) else {},
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def get_forecast_record(forecast_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM resilience_forecast_records WHERE forecast_id = ?", (forecast_id,)).fetchone()
    return _row_to_record(row) if row is not None else None


def list_forecast_records(
    *,
    target_id: str | None = None,
    horizon_days: int | None = None,
    status: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if horizon_days is not None:
        clauses.append("horizon_days = ?")
        params.append(max(1, int(horizon_days)))
    if status is not None:
        normalized = str(status).upper()
        if normalized not in {"ACTIVE", "DUE", "BACKTESTED"}:
            raise ValueError("invalid forecast status")
        clauses.append("status = ?")
        params.append(normalized)
    query = "SELECT * FROM resilience_forecast_records"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY forecasted_at ASC, forecast_id ASC LIMIT ?"
    params.append(max(1, min(int(limit), 100000)))
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_record(row) for row in rows]


def _active_record(
    *,
    target_id: str,
    target_incarnation: str,
    capacity_revision: str,
    horizon_days: int,
) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM resilience_forecast_records
            WHERE target_id = ? AND target_incarnation = ? AND capacity_revision = ?
              AND horizon_days = ? AND status IN ('ACTIVE', 'DUE')
            ORDER BY forecasted_at DESC, forecast_id DESC
            LIMIT 1
            """,
            (target_id, target_incarnation, capacity_revision, horizon_days),
        ).fetchone()
    return _row_to_record(row) if row is not None else None


def ensure_active_forecasts(
    observation: dict[str, Any],
    *,
    horizons: Sequence[int] = (30, 90),
) -> list[dict[str, Any]]:
    target_id = str(observation.get("targetId") or "")
    incarnation = str(observation.get("targetIncarnation") or "")
    revision = str(observation.get("capacityRevision") or "")
    forecasted_at = _parse_iso(observation.get("observedAt"))
    if not target_id or not incarnation or not revision:
        raise ValueError("capacity observation is missing its series binding")
    records: list[dict[str, Any]] = []
    for horizon in sorted({max(1, int(value)) for value in horizons}):
        existing = _active_record(
            target_id=target_id,
            target_incarnation=incarnation,
            capacity_revision=revision,
            horizon_days=horizon,
        )
        if existing is not None:
            records.append(existing)
            continue
        forecast = resilience_capacity_forecast.forecast_target_capacity(
            target_id,
            horizon_days=horizon,
            now=forecasted_at,
            target_incarnation=incarnation,
            capacity_revision=revision,
        )
        if forecast.get("forecastStatus") != "OK":
            continue
        due_at = forecasted_at + timedelta(days=horizon)
        binding = {
            "targetId": target_id,
            "targetIncarnation": incarnation,
            "capacityRevision": revision,
            "horizonDays": horizon,
            "forecastedAt": _utc_iso(forecasted_at),
            "evaluationDueAt": _utc_iso(due_at),
            "forecastDigest": str(forecast["forecastDigest"]),
            "capacityObservationSetDigest": str(forecast["capacityObservationSetDigest"]),
        }
        forecast_id = f"forecast:{_digest(binding)}"
        now_iso = _utc_iso()
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO resilience_forecast_records (
                    forecast_id, target_id, target_incarnation, capacity_revision,
                    horizon_days, forecasted_at, evaluation_due_at,
                    p50_free_bytes, p90_free_bytes, forecast_digest,
                    observation_set_digest, forecast_json, status,
                    actual_observation_key, backtest_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', NULL, NULL, ?, ?)
                """,
                (
                    forecast_id,
                    target_id,
                    incarnation,
                    revision,
                    horizon,
                    binding["forecastedAt"],
                    binding["evaluationDueAt"],
                    int(forecast["p50FreeBytes"]),
                    int(forecast["p90FreeBytes"]),
                    binding["forecastDigest"],
                    binding["capacityObservationSetDigest"],
                    json.dumps(forecast, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute("SELECT * FROM resilience_forecast_records WHERE forecast_id = ?", (forecast_id,)).fetchone()
            assert row is not None
            records.append(_row_to_record(row))
    return records


def evaluate_due_forecasts(observation: dict[str, Any]) -> list[dict[str, Any]]:
    target_id = str(observation.get("targetId") or "")
    incarnation = str(observation.get("targetIncarnation") or "")
    revision = str(observation.get("capacityRevision") or "")
    observed_at = _parse_iso(observation.get("observedAt"))
    observed_iso = _utc_iso(observed_at)
    if not target_id or not incarnation or not revision:
        raise ValueError("capacity observation is missing its series binding")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM resilience_forecast_records
            WHERE target_id = ? AND target_incarnation = ? AND capacity_revision = ?
              AND status IN ('ACTIVE', 'DUE') AND evaluation_due_at <= ?
            ORDER BY evaluation_due_at ASC, forecast_id ASC
            """,
            (target_id, incarnation, revision, observed_iso),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        record = _row_to_record(row)
        now_iso = _utc_iso()
        with _connect() as conn:
            conn.execute(
                """
                UPDATE resilience_forecast_records
                SET status = 'DUE', updated_at = ?
                WHERE forecast_id = ? AND status = 'ACTIVE'
                """,
                (now_iso, record["forecastId"]),
            )
        backtest = resilience_forecast_backtest.record_forecast_backtest(
            target_id,
            predicted_p50_free=int(record["p50FreeBytes"]),
            predicted_p90_free=int(record["p90FreeBytes"]),
            actual_free=int(observation["freeBytes"]),
            horizon_days=int(record["horizonDays"]),
            forecasted_at=_parse_iso(record["forecastedAt"]),
            evaluated_at=observed_at,
            backtest_key=f"backtest:{record['forecastId']}",
            forecast_id=str(record["forecastId"]),
            target_incarnation=incarnation,
            capacity_revision=revision,
            forecast_digest=str(record["forecastDigest"]),
            actual_observation_key=str(observation.get("observationKey") or ""),
        )
        with _connect() as conn:
            conn.execute(
                """
                UPDATE resilience_forecast_records
                SET status = 'BACKTESTED', actual_observation_key = ?, backtest_key = ?, updated_at = ?
                WHERE forecast_id = ? AND status = 'DUE'
                """,
                (
                    str(observation.get("observationKey") or ""),
                    str(backtest["backtestKey"]),
                    _utc_iso(),
                    record["forecastId"],
                ),
            )
        results.append(backtest)
    return results


def process_capacity_observation(
    observation: dict[str, Any],
    *,
    horizons: Sequence[int] = (30, 90),
) -> dict[str, Any]:
    backtests = evaluate_due_forecasts(observation)
    forecasts = ensure_active_forecasts(observation, horizons=horizons)
    return {"backtests": backtests, "forecasts": forecasts}


def forecast_registry_snapshot(*, horizon_days: int = 90) -> dict[str, Any]:
    horizon = max(1, int(horizon_days))
    records = list_forecast_records(horizon_days=horizon, limit=100000)
    latest_by_target: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["status"] not in {"ACTIVE", "DUE"}:
            continue
        series = resilience_capacity_history.latest_capacity_series(str(record["targetId"]))
        if series is None:
            continue
        if (
            record["targetIncarnation"] != series["targetIncarnation"]
            or record["capacityRevision"] != series["capacityRevision"]
        ):
            continue
        latest_by_target[record["targetId"]] = record
    return {
        "horizonDays": horizon,
        "targets": [latest_by_target[target_id] for target_id in sorted(latest_by_target)],
        "generatedAt": _utc_iso(),
        "source": "durable-forecast-registry",
    }
