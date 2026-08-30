"""Persist forecast-versus-later-observation calibration (4.7.6 Gate G)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from deepseek_infra.core import config

CAPACITY_HISTORY_DIR = config.ROOT / ".resilience-capacity"
CAPACITY_HISTORY_DB = CAPACITY_HISTORY_DIR / "capacity.sqlite3"

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS resilience_forecast_backtests (
    backtest_key TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    forecasted_at TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    predicted_p50_free INTEGER NOT NULL,
    predicted_p90_free INTEGER NOT NULL,
    actual_free INTEGER NOT NULL,
    absolute_error REAL NOT NULL,
    percent_error REAL,
    bias REAL NOT NULL,
    interval_hit INTEGER NOT NULL,
    forecast_id TEXT NOT NULL DEFAULT '',
    target_incarnation TEXT NOT NULL DEFAULT '',
    capacity_revision TEXT NOT NULL DEFAULT '',
    forecast_digest TEXT NOT NULL DEFAULT '',
    actual_observation_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resilience_forecast_backtests_target
ON resilience_forecast_backtests(target_id, evaluated_at);
"""

_MIGRATION_COLUMNS = {
    "forecast_id": "TEXT NOT NULL DEFAULT ''",
    "target_incarnation": "TEXT NOT NULL DEFAULT ''",
    "capacity_revision": "TEXT NOT NULL DEFAULT ''",
    "forecast_digest": "TEXT NOT NULL DEFAULT ''",
    "actual_observation_key": "TEXT NOT NULL DEFAULT ''",
}


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(resilience_forecast_backtests)").fetchall()}
    for name, definition in _MIGRATION_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE resilience_forecast_backtests ADD COLUMN {name} {definition}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    CAPACITY_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(CAPACITY_HISTORY_DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _ensure_schema(conn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def record_forecast_backtest(
    target_id: str,
    *,
    predicted_p50_free: int,
    predicted_p90_free: int,
    actual_free: int,
    horizon_days: int,
    forecasted_at: datetime | None = None,
    evaluated_at: datetime | None = None,
    backtest_key: str | None = None,
    forecast_id: str | None = None,
    target_incarnation: str | None = None,
    capacity_revision: str | None = None,
    forecast_digest: str | None = None,
    actual_observation_key: str | None = None,
) -> dict[str, Any]:
    tid = str(target_id or "").strip()
    if not tid:
        raise ValueError("targetId is required")
    predicted_p50 = int(predicted_p50_free)
    predicted_p90 = int(predicted_p90_free)
    actual = int(actual_free)
    absolute_error = abs(predicted_p50 - actual)
    percent_error = None if actual == 0 else (absolute_error / abs(actual))
    bias = float(predicted_p50 - actual)
    interval_hit = 1 if actual >= predicted_p90 else 0
    forecasted = _utc_iso(forecasted_at)
    evaluated = _utc_iso(evaluated_at)
    if target_incarnation is None or capacity_revision is None:
        from deepseek_infra.infra.workspace import resilience_capacity_history

        series = resilience_capacity_history.latest_capacity_series(tid) or {}
        if target_incarnation is None:
            target_incarnation = str(series.get("targetIncarnation") or "")
        if capacity_revision is None:
            capacity_revision = str(series.get("capacityRevision") or "")
    bound_forecast_id = str(forecast_id or "")
    key = str(backtest_key or (f"backtest:{bound_forecast_id}" if bound_forecast_id else f"backtest:{tid}:{forecasted}:{horizon_days}"))
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_forecast_backtests (
                backtest_key, target_id, forecasted_at, evaluated_at, horizon_days,
                predicted_p50_free, predicted_p90_free, actual_free,
                absolute_error, percent_error, bias, interval_hit,
                forecast_id, target_incarnation, capacity_revision,
                forecast_digest, actual_observation_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                tid,
                forecasted,
                evaluated,
                int(horizon_days),
                predicted_p50,
                predicted_p90,
                actual,
                absolute_error,
                percent_error,
                bias,
                interval_hit,
                bound_forecast_id,
                str(target_incarnation or ""),
                str(capacity_revision or ""),
                str(forecast_digest or ""),
                str(actual_observation_key or ""),
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_forecast_backtests WHERE backtest_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        return {
            "backtestKey": str(row["backtest_key"]),
            "forecastId": str(row["forecast_id"]),
            "targetId": str(row["target_id"]),
            "forecastedAt": str(row["forecasted_at"]),
            "evaluatedAt": str(row["evaluated_at"]),
            "horizonDays": int(row["horizon_days"]),
            "predictedP50FreeBytes": int(row["predicted_p50_free"]),
            "predictedP90FreeBytes": int(row["predicted_p90_free"]),
            "actualFreeBytes": int(row["actual_free"]),
            "mae": float(row["absolute_error"]),
            "mape": row["percent_error"],
            "bias": float(row["bias"]),
            "intervalHit": bool(row["interval_hit"]),
            "targetIncarnation": str(row["target_incarnation"]),
            "capacityRevision": str(row["capacity_revision"]),
            "forecastDigest": str(row["forecast_digest"]),
            "actualObservationKey": str(row["actual_observation_key"]),
        }


def summarize_backtest(
    target_id: str,
    *,
    target_incarnation: str | None = None,
    capacity_revision: str | None = None,
) -> dict[str, Any]:
    clauses = ["target_id = ?"]
    params: list[Any] = [target_id]
    if target_incarnation is not None:
        clauses.append("target_incarnation = ?")
        params.append(target_incarnation)
    if capacity_revision is not None:
        clauses.append("capacity_revision = ?")
        params.append(capacity_revision)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resilience_forecast_backtests WHERE " + " AND ".join(clauses) + " ORDER BY evaluated_at ASC",
            tuple(params),
        ).fetchall()
    if not rows:
        return {
            "targetId": target_id,
            "samples": 0,
            "mae": None,
            "mape": None,
            "bias": None,
            "intervalCoverage": None,
            "overoptimistic": False,
            "calibrationDigest": _digest({"targetId": target_id, "samples": []}),
        }
    mae = sum(float(row["absolute_error"]) for row in rows) / len(rows)
    mape_values = [float(row["percent_error"]) for row in rows if row["percent_error"] is not None]
    mape = sum(mape_values) / len(mape_values) if mape_values else None
    bias = sum(float(row["bias"]) for row in rows) / len(rows)
    coverage = sum(int(row["interval_hit"]) for row in rows) / len(rows)
    result = {
        "targetId": target_id,
        "samples": len(rows),
        "mae": round(mae, 3),
        "mape": None if mape is None else round(mape, 6),
        "bias": round(bias, 3),
        "intervalCoverage": round(coverage, 6),
        "overoptimistic": bias > 0,
    }
    result["calibrationDigest"] = _digest(
        {
            **result,
            "backtests": [
                {
                    "backtestKey": str(row["backtest_key"]),
                    "forecastDigest": str(row["forecast_digest"]),
                    "actualObservationKey": str(row["actual_observation_key"]),
                }
                for row in rows
            ],
        }
    )
    return result
