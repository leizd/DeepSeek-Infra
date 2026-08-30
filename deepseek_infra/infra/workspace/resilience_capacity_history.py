"""Durable, provenance-bound fleet capacity observations (4.7.6 Gate E)."""

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
    active_policies INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    probe_source TEXT NOT NULL DEFAULT 'caller',
    target_incarnation TEXT NOT NULL DEFAULT '',
    capacity_revision TEXT NOT NULL DEFAULT '',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resilience_capacity_target_time
ON resilience_capacity_observations(target_id, observed_at);
"""

_MIGRATION_COLUMNS = {
    "source": "TEXT NOT NULL DEFAULT 'manual'",
    "probe_source": "TEXT NOT NULL DEFAULT 'caller'",
    "target_incarnation": "TEXT NOT NULL DEFAULT ''",
    "capacity_revision": "TEXT NOT NULL DEFAULT ''",
    "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
    "observation_digest": "TEXT NOT NULL DEFAULT ''",
}


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(resilience_capacity_observations)").fetchall()}
    for name, definition in _MIGRATION_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE resilience_capacity_observations ADD COLUMN {name} {definition}")
    conn.execute(
        "UPDATE resilience_capacity_observations SET target_incarnation = 'legacy:' || target_id WHERE target_incarnation = ''"
    )
    conn.execute("UPDATE resilience_capacity_observations SET capacity_revision = 'legacy' WHERE capacity_revision = ''")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_resilience_capacity_series_time
        ON resilience_capacity_observations(target_id, target_incarnation, capacity_revision, observed_at)
        """
    )


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


def _row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
    try:
        provenance = json.loads(str(row["provenance_json"] or "{}"))
    except json.JSONDecodeError:
        provenance = {}
    payload = {
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
        "source": str(row["source"] or "manual"),
        "probeSource": str(row["probe_source"] or "caller"),
        "targetIncarnation": str(row["target_incarnation"] or f"legacy:{row['target_id']}"),
        "capacityRevision": str(row["capacity_revision"] or "legacy"),
        "provenance": provenance if isinstance(provenance, dict) else {},
    }
    payload["observationDigest"] = str(row["observation_digest"] or _digest(payload))
    return payload


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
    source: str = "manual",
    probe_source: str = "caller",
    target_incarnation: str | None = None,
    capacity_revision: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tid = str(target_id or "").strip()
    if not tid:
        raise ValueError("targetId is required")
    timestamp = _utc_iso(observed_at)
    incarnation = str(target_incarnation or f"legacy:{tid}").strip()
    normalized_source = str(source or "").strip()
    normalized_probe_source = str(probe_source or "").strip()
    if not incarnation:
        raise ValueError("targetIncarnation is required")
    if not normalized_source:
        raise ValueError("source is required")
    if not normalized_probe_source:
        raise ValueError("probeSource is required")
    revision = str(capacity_revision or "").strip()
    if not revision:
        revision = _digest(
            {
                "targetId": tid,
                "targetIncarnation": incarnation,
                "source": normalized_source,
                "probeSource": normalized_probe_source,
                "totalBytes": max(0, int(total_bytes)),
            }
        )
    normalized_provenance = dict(provenance or {})
    key = str(observation_key or f"capacity:{tid}:{incarnation}:{timestamp}")
    payload = {
        "observationKey": key,
        "targetId": tid,
        "observedAt": timestamp,
        "usedBytes": max(0, int(used_bytes)),
        "freeBytes": max(0, int(free_bytes)),
        "totalBytes": max(0, int(total_bytes)),
        "backupBytesWritten": max(0, int(backup_bytes_written)),
        "replicationBytesIn": max(0, int(replication_bytes_in)),
        "replicationBytesOut": max(0, int(replication_bytes_out)),
        "rebalanceBytesIn": max(0, int(rebalance_bytes_in)),
        "rebalanceBytesOut": max(0, int(rebalance_bytes_out)),
        "activePolicies": max(0, int(active_policies)),
        "source": normalized_source,
        "probeSource": normalized_probe_source,
        "targetIncarnation": incarnation,
        "capacityRevision": revision,
        "provenance": normalized_provenance,
    }
    observation_digest = _digest(payload)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO resilience_capacity_observations (
                observation_key, target_id, observed_at, used_bytes, free_bytes, total_bytes,
                backup_bytes_written, replication_bytes_in, replication_bytes_out,
                rebalance_bytes_in, rebalance_bytes_out, active_policies,
                source, probe_source, target_incarnation, capacity_revision,
                provenance_json, observation_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                tid,
                timestamp,
                payload["usedBytes"],
                payload["freeBytes"],
                payload["totalBytes"],
                payload["backupBytesWritten"],
                payload["replicationBytesIn"],
                payload["replicationBytesOut"],
                payload["rebalanceBytesIn"],
                payload["rebalanceBytesOut"],
                payload["activePolicies"],
                normalized_source,
                normalized_probe_source,
                incarnation,
                revision,
                json.dumps(normalized_provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                observation_digest,
            ),
        )
        row = conn.execute(
            "SELECT * FROM resilience_capacity_observations WHERE observation_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        observed = _row_to_observation(row)
        if observed["observationDigest"] != observation_digest:
            raise ValueError("observationKey already binds different capacity data")
        return observed


def list_capacity_observations(
    target_id: str | None = None,
    *,
    since: datetime | None = None,
    limit: int = 10000,
    target_incarnation: str | None = None,
    capacity_revision: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if target_id:
        clauses.append("target_id = ?")
        params.append(target_id)
    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(_utc_iso(since))
    if target_incarnation is not None:
        clauses.append("target_incarnation = ?")
        params.append(target_incarnation)
    if capacity_revision is not None:
        clauses.append("capacity_revision = ?")
        params.append(capacity_revision)
    query = "SELECT * FROM resilience_capacity_observations"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at ASC, observation_key ASC LIMIT ?"
    params.append(max(1, min(int(limit), 100000)))
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_row_to_observation(row) for row in rows]


def latest_capacity_series(target_id: str) -> dict[str, str] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT target_incarnation, capacity_revision, observed_at, observation_key
            FROM resilience_capacity_observations
            WHERE target_id = ?
            ORDER BY observed_at DESC, observation_key DESC
            LIMIT 1
            """,
            (target_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "targetIncarnation": str(row["target_incarnation"] or f"legacy:{target_id}"),
        "capacityRevision": str(row["capacity_revision"] or "legacy"),
        "observedAt": str(row["observed_at"]),
        "observationKey": str(row["observation_key"]),
    }
