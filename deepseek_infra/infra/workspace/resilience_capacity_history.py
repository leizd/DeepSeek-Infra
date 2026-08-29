"""Durable normalized fleet capacity observations (4.7.5 Gate F)."""

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
CREATE TABLE IF NOT EXISTS resilience_capacity_observations (
    observation_key TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    used_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    backup_bytes_written INTEGER NOT NULL DEFAULT 0,
    replication_bytes_in INTEGER NOT NULL DEFAULT 0,
    replication_bytes_out INTEGER NOT NULL DEFAULT 0,
    rebalance_bytes_in INTEGER NOT NULL DEFAULT 0,
    rebalance_bytes_out INTEGER NOT NULL DEFAULT 0,
    active_policies INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_resilience_capacity_target_time
ON resilience_capacity_observations(target_id, observed_at);
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


def _row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "observationKey": str(row["observation_key"]),
        "targetId": str(row["target_id"]),
        "observedAt": str(row["observed_at"]),
        "usedBytes": int(row["used_bytes"]),
        "freeBytes": int(row["free_bytes"]),
        "totalBytes": int(row["total_bytes"]),
        "backupBytesWritten": int(row["backup_bytes_written"]),
        "replicationBytesIn": int(row["replication_bytes_in"]),
        "replicationBytesOut": int(row["replication_bytes_out"]),
        "rebalanceBytesIn": int(row["rebalance_bytes_in"]),
        "rebalanceBytesOut": int(row["rebalance_bytes_out"]),
        "activePolicies": int(row["active_policies"]),
    }


def record_capacity_observation(
    target_id: str,
    *,
    used_bytes: int,
    free_bytes: int,
    total_bytes: int,
    observed_at: datetime | None = None,
    backup_bytes_written: int = 0,
    replication_bytes_in: int = 0,
    replication_bytes_out: int = 0,
    rebalance_bytes_in: int = 0,
    rebalance_bytes_out: int = 0,
    active_policies: int = 0,
    observation_key: str | None = None,
) -> dict[str, Any]:
    tid = str(target_id or "").strip()
    if not tid:
        raise ValueError("targetId is required")
    timestamp = _utc_iso(observed_at)
    key = str(observation_key or f"capacity:{tid}:{timestamp}")
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_capacity_observations (
                observation_key, target_id, observed_at, used_bytes, free_bytes, total_bytes,
                backup_bytes_written, replication_bytes_in, replication_bytes_out,
                rebalance_bytes_in, rebalance_bytes_out, active_policies
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                tid,
                timestamp,
                max(0, int(used_bytes)),
                max(0, int(free_bytes)),
                max(0, int(total_bytes)),
                max(0, int(backup_bytes_written)),
                max(0, int(replication_bytes_in)),
                max(0, int(replication_bytes_out)),
                max(0, int(rebalance_bytes_in)),
                max(0, int(rebalance_bytes_out)),
                max(0, int(active_policies)),
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_capacity_observations WHERE observation_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        return _row_to_observation(row)


def list_capacity_observations(
    target_id: str | None = None,
    *,
    since: datetime | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if target_id:
        clauses.append("target_id = ?")
        params.append(target_id)
    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(_utc_iso(since))
    query = "SELECT * FROM resilience_capacity_observations"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at ASC, observation_key ASC LIMIT ?"
    params.append(max(1, min(int(limit), 100000)))
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_observation(row) for row in rows]
