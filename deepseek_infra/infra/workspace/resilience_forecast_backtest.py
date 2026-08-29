"""Persist forecast-versus-reality error so confidence can be lowered (4.7.5 Gate H)."""

from __future__ import annotations

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
    interval_hit INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_forecast_backtests_target
ON resilience_forecast_backtests(target_id, evaluated_at);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    key = str(backtest_key or f"backtest:{tid}:{forecasted}:{horizon_days}")
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_forecast_backtests (
                backtest_key, target_id, forecasted_at, evaluated_at, horizon_days,
                predicted_p50_free, predicted_p90_free, actual_free,
                absolute_error, percent_error, bias, interval_hit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_forecast_backtests WHERE backtest_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        return {
            "backtestKey": str(row["backtest_key"]),
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
        }


def summarize_backtest(target_id: str) -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resilience_forecast_backtests WHERE target_id = ? ORDER BY evaluated_at ASC",
            (target_id,),
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
        }
    mae = sum(float(row["absolute_error"]) for row in rows) / len(rows)
    mape_values = [float(row["percent_error"]) for row in rows if row["percent_error"] is not None]
    mape = sum(mape_values) / len(mape_values) if mape_values else None
    bias = sum(float(row["bias"]) for row in rows) / len(rows)
    coverage = sum(int(row["interval_hit"]) for row in rows) / len(rows)
    return {
        "targetId": target_id,
        "samples": len(rows),
        "mae": round(mae, 3),
        "mape": None if mape is None else round(mape, 6),
        "bias": round(bias, 3),
        "intervalCoverage": round(coverage, 6),
        "overoptimistic": bias > 0,
    }
