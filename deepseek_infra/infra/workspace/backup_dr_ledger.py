"""Durable local read model for disaster recovery evidence (4.5.1).

Maintains an incremental SQLite ledger at .backup-dr/evidence.sqlite3
recording verified backup commits, scrubs, target probes, isolated drills,
recovery stage throughput samples, and remote audit runs.
Readiness queries read exclusively from this local projection with zero remote I/O.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core import config

BACKUP_DR_DIR = config.ROOT / ".backup-dr"
EVIDENCE_DB = BACKUP_DR_DIR / "evidence.sqlite3"

_DB_LOCK = threading.RLock()

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS recovery_points (
    target_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL,
    parent_backup_id TEXT,
    chain_digest TEXT,
    chain_length INTEGER NOT NULL DEFAULT 1,
    ciphertext_bytes INTEGER NOT NULL DEFAULT 0,
    logical_bytes INTEGER NOT NULL DEFAULT 0,
    recoverable INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT NOT NULL,
    storage_protocol TEXT,
    metadata_json TEXT,
    PRIMARY KEY (target_id, policy_id, backup_id)
);

CREATE INDEX IF NOT EXISTS idx_recovery_points_scope_time 
ON recovery_points (target_id, policy_id, committed_at DESC);

CREATE TABLE IF NOT EXISTS scrub_evidence (
    target_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    policy_id TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    result TEXT NOT NULL,
    details_json TEXT,
    PRIMARY KEY (target_id, backup_id, policy_id)
);

CREATE TABLE IF NOT EXISTS logical_recovery_points (
    logical_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_set_digest TEXT,
    committed_at TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL DEFAULT 'full',
    retained INTEGER NOT NULL DEFAULT 1,
    retired_at TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_logical_rp_policy_backup
ON logical_recovery_points (policy_id, backup_id, committed_at DESC);

CREATE TABLE IF NOT EXISTS recovery_point_copies (
    target_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    object_set_digest TEXT,
    committed_at TEXT NOT NULL,
    recoverable INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'primary',
    mode TEXT NOT NULL DEFAULT 'required',
    state TEXT NOT NULL DEFAULT 'healthy',
    last_verified_at TEXT,
    last_scrub_at TEXT,
    last_drill_at TEXT,
    last_repair_at TEXT,
    last_failure TEXT,
    verified_at TEXT,
    metadata_json TEXT,
    PRIMARY KEY (target_id, policy_id, backup_id)
);

CREATE INDEX IF NOT EXISTS idx_rp_copies_logical
ON recovery_point_copies (logical_id, recoverable);

CREATE TABLE IF NOT EXISTS audit_jobs (
    audit_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    cursor TEXT,
    target_generation INTEGER,
    previous_commit_hash TEXT,
    records_checked INTEGER NOT NULL DEFAULT 0,
    anomalies_json TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_jobs_target
ON audit_jobs (target_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS drill_evidence (
    drill_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    result TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_drill_evidence_scope_time 
ON drill_evidence (target_id, policy_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS target_evidence (
    target_id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    scheduled_ready INTEGER NOT NULL DEFAULT 0,
    integrity_mode TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS recovery_stage_samples (
    sample_id TEXT PRIMARY KEY,
    target_id TEXT,
    recovery_class_json TEXT NOT NULL,
    stage TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_stage_samples_stage_time 
ON recovery_stage_samples (stage, observed_at DESC);

CREATE TABLE IF NOT EXISTS audit_evidence (
    audit_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    cursor TEXT,
    records_checked INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_evidence_target_time 
ON audit_evidence (target_id, started_at DESC);
"""


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE logical_recovery_points ADD COLUMN retained INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE logical_recovery_points ADD COLUMN retired_at TEXT")
    except sqlite3.OperationalError:
        pass
    for col, col_type in [
        ("state", "TEXT NOT NULL DEFAULT 'healthy'"),
        ("last_verified_at", "TEXT"),
        ("last_scrub_at", "TEXT"),
        ("last_drill_at", "TEXT"),
        ("last_repair_at", "TEXT"),
        ("last_failure", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE recovery_point_copies ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE recovery_stage_samples ADD COLUMN target_id TEXT")
    except sqlite3.OperationalError:
        pass


def _get_connection() -> sqlite3.Connection:
    BACKUP_DR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(EVIDENCE_DB), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    with conn:
        conn.executescript(_INIT_SQL)
        _migrate_schema(conn)
    return conn


# ── Incremental Evidence Recording ──────────────────────────────────────────


def record_recovery_point(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    committed_at: str,
    snapshot_kind: str = "full",
    parent_backup_id: str | None = None,
    chain_digest: str | None = None,
    chain_length: int = 1,
    ciphertext_bytes: int = 0,
    logical_bytes: int = 0,
    recoverable: bool = True,
    verified_at: str | None = None,
    storage_protocol: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO recovery_points (
                target_id, policy_id, backup_id, committed_at, snapshot_kind,
                parent_backup_id, chain_digest, chain_length, ciphertext_bytes,
                logical_bytes, recoverable, verified_at, storage_protocol, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
                committed_at = excluded.committed_at,
                snapshot_kind = excluded.snapshot_kind,
                parent_backup_id = excluded.parent_backup_id,
                chain_digest = excluded.chain_digest,
                chain_length = excluded.chain_length,
                ciphertext_bytes = excluded.ciphertext_bytes,
                logical_bytes = excluded.logical_bytes,
                recoverable = excluded.recoverable,
                verified_at = excluded.verified_at,
                storage_protocol = excluded.storage_protocol,
                metadata_json = excluded.metadata_json
            """,
            (
                str(target_id),
                str(policy_id),
                str(backup_id),
                str(committed_at),
                str(snapshot_kind),
                str(parent_backup_id) if parent_backup_id else None,
                str(chain_digest) if chain_digest else None,
                max(1, int(chain_length)),
                max(0, int(ciphertext_bytes)),
                max(0, int(logical_bytes)),
                1 if recoverable else 0,
                str(verified_at or _utc_iso()),
                str(storage_protocol) if storage_protocol else None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
            ),
        )


def _ensure_scrub_policy_column(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(scrub_evidence)").fetchall()}
    if "policy_id" not in cols:
        conn.execute("ALTER TABLE scrub_evidence ADD COLUMN policy_id TEXT NOT NULL DEFAULT ''")


def record_scrub_evidence(
    *,
    target_id: str,
    backup_id: str,
    observed_at: str,
    result: str,
    policy_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    with _DB_LOCK, _get_connection() as conn:
        _ensure_scrub_policy_column(conn)
        # Prefer new composite key; fall back for legacy DBs that still use (target, backup)
        try:
            conn.execute(
                """
                INSERT INTO scrub_evidence (target_id, backup_id, policy_id, observed_at, result, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, backup_id, policy_id) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    result = excluded.result,
                    details_json = excluded.details_json
                """,
                (
                    str(target_id),
                    str(backup_id),
                    str(policy_id or ""),
                    str(observed_at),
                    str(result),
                    json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None,
                ),
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                INSERT INTO scrub_evidence (target_id, backup_id, observed_at, result, details_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_id, backup_id) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    result = excluded.result,
                    details_json = excluded.details_json
                """,
                (
                    str(target_id),
                    str(backup_id),
                    str(observed_at),
                    str(result),
                    json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None,
                ),
            )


def record_drill_evidence(
    *,
    drill_id: str | None = None,
    target_id: str,
    policy_id: str = "",
    backup_id: str = "",
    drill_kind: str = "manual",
    observed_at: str,
    result: str,
    duration_ms: int = 0,
    stage_durations: dict[str, Any] | None = None,
    work_class: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    d_id = drill_id or f"drill_{uuid.uuid4().hex[:12]}"
    full_details = dict(details or {})
    if drill_kind:
        full_details["drillKind"] = drill_kind
    if stage_durations:
        full_details["stageDurations"] = stage_durations
    if work_class:
        full_details["workClass"] = work_class
    if duration_ms == 0 and stage_durations and "totalMs" in stage_durations:
        duration_ms = int(stage_durations["totalMs"])

    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO drill_evidence (drill_id, target_id, policy_id, backup_id, observed_at, result, duration_ms, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(drill_id) DO UPDATE SET
                target_id = excluded.target_id,
                policy_id = excluded.policy_id,
                backup_id = excluded.backup_id,
                observed_at = excluded.observed_at,
                result = excluded.result,
                duration_ms = excluded.duration_ms,
                details_json = excluded.details_json
            """,
            (
                str(d_id),
                str(target_id),
                str(policy_id),
                str(backup_id),
                str(observed_at),
                str(result),
                max(0, int(duration_ms)),
                json.dumps(full_details, ensure_ascii=False, sort_keys=True) if full_details else None,
            ),
        )


def record_target_evidence(
    *,
    target_id: str,
    observed_at: str,
    scheduled_ready: bool,
    integrity_mode: str | None = None,
    status: str = "ok",
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO target_evidence (target_id, observed_at, scheduled_ready, integrity_mode, status, reason, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                observed_at = excluded.observed_at,
                scheduled_ready = excluded.scheduled_ready,
                integrity_mode = excluded.integrity_mode,
                status = excluded.status,
                reason = excluded.reason,
                details_json = excluded.details_json
            """,
            (
                str(target_id),
                str(observed_at),
                1 if scheduled_ready else 0,
                str(integrity_mode) if integrity_mode else None,
                str(status),
                str(reason) if reason else None,
                json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None,
            ),
        )


def record_stage_sample(
    *,
    sample_id: str | None = None,
    stage: str,
    bytes_transferred: int = 0,
    bytes_count: int = 0,
    duration_ms: float = 0.0,
    observed_at: str = "",
    result: str = "success",
    recovery_class: Any = None,
    target_id: str | None = None,
) -> None:
    s_id = sample_id or f"sample_{uuid.uuid4().hex[:12]}"
    actual_bytes = bytes_transferred or bytes_count
    rc_data = recovery_class if isinstance(recovery_class, dict) else (recovery_class.to_dict() if hasattr(recovery_class, "to_dict") else {"tag": str(recovery_class or "default")})
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO recovery_stage_samples (sample_id, target_id, recovery_class_json, stage, bytes, duration_ms, observed_at, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sample_id) DO UPDATE SET
                target_id = COALESCE(excluded.target_id, recovery_stage_samples.target_id),
                recovery_class_json = excluded.recovery_class_json,
                stage = excluded.stage,
                bytes = excluded.bytes,
                duration_ms = excluded.duration_ms,
                observed_at = excluded.observed_at,
                result = excluded.result
            """,
            (
                str(s_id),
                str(target_id) if target_id else None,
                json.dumps(rc_data, ensure_ascii=False, sort_keys=True),
                str(stage),
                max(0, int(actual_bytes)),
                max(0, int(duration_ms)),
                str(observed_at or _utc_iso()),
                str(result),
            ),
        )


def record_audit_evidence(
    *,
    audit_id: str | None = None,
    target_id: str,
    started_at: str | None = None,
    observed_at: str | None = None,
    status: str | None = None,
    result: str | None = None,
    completed_at: str | None = None,
    cursor: str | None = None,
    records_checked: int = 0,
    anomalies_count: int = 0,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    a_id = audit_id or f"audit_{uuid.uuid4().hex[:12]}"
    t_start = started_at or observed_at or _utc_iso()
    t_status = status or result or "completed"
    full_details = dict(details or {})
    if anomalies_count:
        full_details["anomaliesCount"] = anomalies_count
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_evidence (audit_id, target_id, started_at, completed_at, status, cursor, records_checked, error, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                target_id = excluded.target_id,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                status = excluded.status,
                cursor = excluded.cursor,
                records_checked = excluded.records_checked,
                error = excluded.error,
                details_json = excluded.details_json
            """,
            (
                str(a_id),
                str(target_id),
                str(t_start),
                str(completed_at) if completed_at else None,
                str(t_status),
                str(cursor) if cursor else None,
                max(0, int(records_checked)),
                str(error) if error else None,
                json.dumps(full_details, ensure_ascii=False, sort_keys=True) if full_details else None,
            ),
        )


# ── Query APIs (Zero Remote I/O) ────────────────────────────────────────────


def list_scopes() -> list[tuple[str, str]]:
    """Return all known (target_id, policy_id) scopes observed in the ledger."""
    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT target_id, policy_id FROM recovery_points ORDER BY target_id, policy_id"
        ).fetchall()
        return [(str(row["target_id"]), str(row["policy_id"])) for row in rows]


def list_recovery_points(
    *,
    target_id: str | None = None,
    policy_id: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM recovery_points"
    params: list[Any] = []
    clauses: list[str] = []
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if policy_id is not None:
        clauses.append("policy_id = ?")
        params.append(policy_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY committed_at DESC LIMIT ?"
    params.append(limit)

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "targetId": str(row["target_id"]),
                "policyId": str(row["policy_id"]),
                "backupId": str(row["backup_id"]),
                "committedAt": str(row["committed_at"]),
                "snapshotKind": str(row["snapshot_kind"]),
                "parentBackupId": str(row["parent_backup_id"]) if row["parent_backup_id"] else None,
                "chainDigest": str(row["chain_digest"]) if row["chain_digest"] else None,
                "chainLength": int(row["chain_length"]),
                "ciphertextBytes": int(row["ciphertext_bytes"]),
                "logicalBytes": int(row["logical_bytes"]),
                "recoverable": bool(row["recoverable"]),
                "verifiedAt": str(row["verified_at"]),
                "storageProtocol": str(row["storage_protocol"]) if row["storage_protocol"] else None,
                "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            }
            for row in rows
        ]


def resolve_recoverable_chain(
    target_id: str,
    policy_id: str,
    head_backup_id: str,
) -> list[dict[str, Any]] | None:
    """Resolve full ancestor chain from the local ledger."""
    all_points = list_recovery_points(target_id=target_id, policy_id=policy_id, limit=2000)
    by_id = {item["backupId"]: item for item in all_points}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_id: str | None = head_backup_id

    while current_id:
        if current_id in seen:
            return None
        seen.add(current_id)
        point = by_id.get(current_id)
        if point is None or not point["recoverable"]:
            return None
        chain.append(point)
        parent_id = point.get("parentBackupId")
        if not parent_id:
            if point.get("snapshotKind") == "incremental":
                return None
            break
        current_id = parent_id

    chain.reverse()
    return chain


def get_latest_recoverable_point(
    target_id: str,
    policy_id: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    current_iso = _utc_iso(now)
    query = "SELECT * FROM recovery_points WHERE target_id = ? AND recoverable = 1 AND committed_at <= ?"
    params: list[Any] = [target_id, current_iso]
    if policy_id:
        query += " AND policy_id = ?"
        params.append(policy_id)
    query += " ORDER BY committed_at DESC LIMIT 50"

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    for row in rows:
        backup_id = str(row["backup_id"])
        row_policy = str(row["policy_id"])
        chain = resolve_recoverable_chain(target_id, row_policy, backup_id)
        if chain is not None:
            point = {
                "targetId": str(row["target_id"]),
                "policyId": row_policy,
                "backupId": backup_id,
                "committedAt": str(row["committed_at"]),
                "snapshotKind": str(row["snapshot_kind"]),
                "chainLength": len(chain),
                "ciphertextBytes": sum(int(item["ciphertextBytes"]) for item in chain),
                "logicalBytes": int(chain[-1]["logicalBytes"]),
                "recoverable": True,
                "storageProtocol": str(row["storage_protocol"]) if row["storage_protocol"] else None,
            }
            return point, chain

    return None, []


def get_scrub_evidence(
    *,
    target_id: str | None = None,
    backup_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM scrub_evidence"
    params: list[Any] = []
    clauses: list[str] = []
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if backup_id is not None:
        clauses.append("backup_id = ?")
        params.append(backup_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "targetId": str(row["target_id"]),
                "backupId": str(row["backup_id"]),
                "observedAt": str(row["observed_at"]),
                "result": str(row["result"]),
                "details": json.loads(row["details_json"]) if row["details_json"] else {},
            }
            for row in rows
        ]


def get_latest_scrub_outcome(
    target_id: str | None = None,
    policy_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current_iso = _utc_iso(now)
    query = "SELECT * FROM scrub_evidence WHERE observed_at <= ?"
    params: list[Any] = [current_iso]
    if target_id is not None:
        query += " AND target_id = ?"
        params.append(target_id)
    # Strict policy scope: when policy_id is provided, only that policy's scrub counts.
    # Legacy rows without policy_id are only used when policy_id is empty/None.
    with _DB_LOCK, _get_connection() as conn:
        _ensure_scrub_policy_column(conn)
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(scrub_evidence)").fetchall()}
        if policy_id is not None and "policy_id" in cols:
            query += " AND policy_id = ?"
            params.append(policy_id)
        query += " ORDER BY observed_at DESC LIMIT 500"
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return None

    latest = rows[0]
    successful = [r for r in rows if str(r["result"]) == "success"]
    details = json.loads(latest["details_json"]) if latest["details_json"] else {}
    latest_policy = ""
    try:
        latest_policy = str(latest["policy_id"] or "")
    except (IndexError, KeyError):
        latest_policy = ""
    return {
        "status": "ok" if str(latest["result"]) == "success" else "error",
        "result": str(latest["result"]),
        "observedAt": str(latest["observed_at"]),
        "latestCheckedAt": str(latest["observed_at"]),
        "latestSuccessfulAt": str(successful[0]["observed_at"]) if successful else None,
        "targetId": str(latest["target_id"]),
        "backupId": str(latest["backup_id"]),
        "policyId": latest_policy,
        "details": details,
        "source": "dr-evidence-ledger",
    }


def get_drill_evidence(
    *,
    target_id: str | None = None,
    policy_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM drill_evidence"
    params: list[Any] = []
    clauses: list[str] = []
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if policy_id is not None:
        clauses.append("policy_id = ?")
        params.append(policy_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "drillId": str(row["drill_id"]),
                "targetId": str(row["target_id"]),
                "policyId": str(row["policy_id"]),
                "backupId": str(row["backup_id"]),
                "observedAt": str(row["observed_at"]),
                "result": str(row["result"]),
                "durationMs": int(row["duration_ms"]),
                "details": json.loads(row["details_json"]) if row["details_json"] else {},
            }
            for row in rows
        ]


def get_latest_drill_outcome(
    target_id: str | None = None,
    policy_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current_iso = _utc_iso(now)
    query = "SELECT * FROM drill_evidence WHERE observed_at <= ?"
    params: list[Any] = [current_iso]
    if target_id is not None:
        query += " AND target_id = ?"
        params.append(target_id)
    if policy_id is not None:
        query += " AND policy_id = ?"
        params.append(policy_id)
    query += " ORDER BY observed_at DESC LIMIT 500"

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return None

    latest = rows[0]
    successful = [r for r in rows if str(r["result"]) == "success"]
    details = json.loads(latest["details_json"]) if latest["details_json"] else {}
    return {
        "status": "ok" if str(latest["result"]) == "success" else "error",
        "result": str(latest["result"]),
        "drillKind": details.get("drillKind", "manual"),
        "observedAt": str(latest["observed_at"]),
        "latestCheckedAt": str(latest["observed_at"]),
        "latestSuccessfulAt": str(successful[0]["observed_at"]) if successful else None,
        "targetId": str(latest["target_id"]),
        "policyId": str(latest["policy_id"]),
        "backupId": str(latest["backup_id"]),
        "durationMs": int(latest["duration_ms"]),
        "stageDurations": details.get("stageDurations", {}),
        "details": details,
        "source": "dr-evidence-ledger",
    }


def get_target_evidence(target_id: str) -> dict[str, Any] | None:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM target_evidence WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "targetId": str(row["target_id"]),
            "observedAt": str(row["observed_at"]),
            "scheduledReady": bool(row["scheduled_ready"]),
            "integrityMode": str(row["integrity_mode"]) if row["integrity_mode"] else None,
            "status": str(row["status"]),
            "reason": str(row["reason"]) if row["reason"] else None,
            "details": json.loads(row["details_json"]) if row["details_json"] else {},
        }


def list_target_evidence() -> list[dict[str, Any]]:
    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute("SELECT * FROM target_evidence ORDER BY target_id").fetchall()
        return [
            {
                "targetId": str(row["target_id"]),
                "observedAt": str(row["observed_at"]),
                "scheduledReady": bool(row["scheduled_ready"]),
                "integrityMode": str(row["integrity_mode"]) if row["integrity_mode"] else None,
                "status": str(row["status"]),
                "reason": str(row["reason"]) if row["reason"] else None,
                "details": json.loads(row["details_json"]) if row["details_json"] else {},
            }
            for row in rows
        ]


def list_stage_samples(
    *,
    stage: str | None = None,
    since_iso: str | None = None,
    target_id: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM recovery_stage_samples"
    params: list[Any] = []
    clauses: list[str] = []
    if stage is not None:
        clauses.append("stage = ?")
        params.append(stage)
    if since_iso is not None:
        clauses.append("observed_at >= ?")
        params.append(since_iso)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)

    with _DB_LOCK, _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "sampleId": str(row["sample_id"]),
                "targetId": str(row["target_id"]) if "target_id" in row.keys() and row["target_id"] else None,
                "recoveryClass": json.loads(row["recovery_class_json"]) if row["recovery_class_json"] else {},
                "stage": str(row["stage"]),
                "bytes": int(row["bytes"]),
                "durationMs": int(row["duration_ms"]),
                "observedAt": str(row["observed_at"]),
                "result": str(row["result"]),
            }
            for row in rows
        ]


def get_latest_audit_evidence(target_id: str) -> dict[str, Any] | None:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM audit_evidence WHERE target_id = ? ORDER BY started_at DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "auditId": str(row["audit_id"]),
            "targetId": str(row["target_id"]),
            "startedAt": str(row["started_at"]),
            "completedAt": str(row["completed_at"]) if row["completed_at"] else None,
            "status": str(row["status"]),
            "cursor": str(row["cursor"]) if row["cursor"] else None,
            "recordsChecked": int(row["records_checked"]),
            "error": str(row["error"]) if row["error"] else None,
            "details": json.loads(row["details_json"]) if row["details_json"] else {},
        }


def get_audit_job(audit_id: str) -> dict[str, Any] | None:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute("SELECT * FROM audit_jobs WHERE audit_id = ?", (str(audit_id),)).fetchone()
        if row is None:
            return None
        return _audit_job_row(row)


def get_open_audit_job(target_id: str) -> dict[str, Any] | None:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM audit_jobs
            WHERE target_id = ? AND phase NOT IN ('completed', 'failed')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (str(target_id),),
        ).fetchone()
        if row is None:
            return None
        return _audit_job_row(row)


def _audit_job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "auditId": str(row["audit_id"]),
        "targetId": str(row["target_id"]),
        "phase": str(row["phase"]),
        "cursor": str(row["cursor"]) if row["cursor"] else None,
        "targetGeneration": int(row["target_generation"]) if row["target_generation"] is not None else None,
        "previousCommitHash": str(row["previous_commit_hash"]) if row["previous_commit_hash"] else None,
        "recordsChecked": int(row["records_checked"] or 0),
        "anomalies": json.loads(row["anomalies_json"]) if row["anomalies_json"] else [],
        "startedAt": str(row["started_at"]),
        "updatedAt": str(row["updated_at"]),
        "completedAt": str(row["completed_at"]) if row["completed_at"] else None,
        "details": json.loads(row["details_json"]) if row["details_json"] else {},
    }


def upsert_audit_job(
    *,
    audit_id: str,
    target_id: str,
    phase: str,
    cursor: str | None = None,
    target_generation: int | None = None,
    previous_commit_hash: str | None = None,
    records_checked: int = 0,
    anomalies: list[Any] | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    started = started_at or now
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_jobs (
                audit_id, target_id, phase, cursor, target_generation, previous_commit_hash,
                records_checked, anomalies_json, started_at, updated_at, completed_at, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                phase = excluded.phase,
                cursor = excluded.cursor,
                target_generation = excluded.target_generation,
                previous_commit_hash = excluded.previous_commit_hash,
                records_checked = excluded.records_checked,
                anomalies_json = excluded.anomalies_json,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at,
                details_json = excluded.details_json
            """,
            (
                str(audit_id),
                str(target_id),
                str(phase),
                str(cursor) if cursor else None,
                int(target_generation) if target_generation is not None else None,
                str(previous_commit_hash) if previous_commit_hash else None,
                max(0, int(records_checked)),
                json.dumps(anomalies or [], ensure_ascii=False, sort_keys=True),
                str(started),
                now,
                str(completed_at) if completed_at else None,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True) if details else None,
            ),
        )
    job = get_audit_job(audit_id)
    assert job is not None
    return job


def record_logical_recovery_copy(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    committed_at: str,
    object_set_digest: str | None = None,
    recoverable: bool = True,
    role: str = "primary",
    mode: str = "required",
    snapshot_kind: str = "full",
    state: str = "healthy",
    last_verified_at: str | None = None,
    last_scrub_at: str | None = None,
    last_drill_at: str | None = None,
    last_repair_at: str | None = None,
    last_failure: str | None = None,
    verified_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Record one target-local copy of a logical recovery point (same backupId + commitment)."""
    digest = str(object_set_digest or "")
    logical_id = f"lrp_{policy_id}_{backup_id}_{digest[:16] if digest else 'na'}"
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO logical_recovery_points (
                logical_id, policy_id, backup_id, object_set_digest, committed_at, snapshot_kind, retained, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(logical_id) DO UPDATE SET
                committed_at = CASE
                    WHEN excluded.committed_at > logical_recovery_points.committed_at
                    THEN excluded.committed_at ELSE logical_recovery_points.committed_at END,
                metadata_json = COALESCE(excluded.metadata_json, logical_recovery_points.metadata_json)
            """,
            (
                logical_id,
                str(policy_id),
                str(backup_id),
                digest or None,
                str(committed_at),
                str(snapshot_kind or "full"),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
            ),
        )
        conn.execute(
            """
            INSERT INTO recovery_point_copies (
                target_id, policy_id, backup_id, logical_id, object_set_digest, committed_at,
                recoverable, role, mode, state, last_verified_at, last_scrub_at, last_drill_at,
                last_repair_at, last_failure, verified_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
                logical_id = excluded.logical_id,
                object_set_digest = excluded.object_set_digest,
                committed_at = excluded.committed_at,
                recoverable = excluded.recoverable,
                role = excluded.role,
                mode = excluded.mode,
                state = excluded.state,
                last_verified_at = COALESCE(excluded.last_verified_at, recovery_point_copies.last_verified_at),
                last_scrub_at = COALESCE(excluded.last_scrub_at, recovery_point_copies.last_scrub_at),
                last_drill_at = COALESCE(excluded.last_drill_at, recovery_point_copies.last_drill_at),
                last_repair_at = COALESCE(excluded.last_repair_at, recovery_point_copies.last_repair_at),
                last_failure = COALESCE(excluded.last_failure, recovery_point_copies.last_failure),
                verified_at = excluded.verified_at,
                metadata_json = excluded.metadata_json
            """,
            (
                str(target_id),
                str(policy_id),
                str(backup_id),
                logical_id,
                digest or None,
                str(committed_at),
                1 if recoverable else 0,
                str(role or "primary"),
                str(mode or "required"),
                str(state or "healthy"),
                str(last_verified_at) if last_verified_at else None,
                str(last_scrub_at) if last_scrub_at else None,
                str(last_drill_at) if last_drill_at else None,
                str(last_repair_at) if last_repair_at else None,
                str(last_failure) if last_failure else None,
                str(verified_at or _utc_iso()),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
            ),
        )
    return logical_id


def list_logical_recovery_copies(
    *,
    target_id: str | None = None,
    policy_id: str | None = None,
    backup_id: str | None = None,
    object_set_digest: str | None = None,
    logical_id: str | None = None,
    after_committed_at: str | None = None,
    after_logical_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM recovery_point_copies"
    params: list[Any] = []
    clauses: list[str] = []
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(target_id)
    if logical_id is not None:
        clauses.append("logical_id = ?")
        params.append(logical_id)
    if policy_id is not None:
        clauses.append("policy_id = ?")
        params.append(policy_id)
    if backup_id is not None:
        clauses.append("backup_id = ?")
        params.append(backup_id)
    if object_set_digest is not None:
        clauses.append("object_set_digest = ?")
        params.append(object_set_digest)
    if after_committed_at is not None:
        clauses.append("(committed_at < ? OR (committed_at = ? AND logical_id < ?))")
        params.extend((after_committed_at, after_committed_at, str(after_logical_id or "")))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY committed_at DESC, logical_id DESC LIMIT ?"
    params.append(limit)
    with _DB_LOCK, _get_connection() as conn:
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "targetId": str(row["target_id"]),
                "policyId": str(row["policy_id"]),
                "backupId": str(row["backup_id"]),
                "logicalId": str(row["logical_id"]),
                "objectSetDigest": str(row["object_set_digest"]) if row["object_set_digest"] else None,
                "committedAt": str(row["committed_at"]),
                "recoverable": bool(row["recoverable"]),
                "role": str(row["role"] or "primary"),
                "mode": str(row["mode"] or "required"),
                "state": str(row["state"] if "state" in row.keys() and row["state"] else "healthy"),
                "lastVerifiedAt": str(row["last_verified_at"]) if "last_verified_at" in row.keys() and row["last_verified_at"] else None,
                "lastScrubAt": str(row["last_scrub_at"]) if "last_scrub_at" in row.keys() and row["last_scrub_at"] else None,
                "lastDrillAt": str(row["last_drill_at"]) if "last_drill_at" in row.keys() and row["last_drill_at"] else None,
                "lastRepairAt": str(row["last_repair_at"]) if "last_repair_at" in row.keys() and row["last_repair_at"] else None,
                "lastFailure": str(row["last_failure"]) if "last_failure" in row.keys() and row["last_failure"] else None,
                "verifiedAt": str(row["verified_at"]) if row["verified_at"] else None,
                "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            }
            for row in rows
        ]


def count_live_logical_recovery_copies(*, target_id: str) -> int:
    """Count current healthy/recoverable copies without scanning Target history."""
    with _DB_LOCK, _get_connection() as conn:
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS copy_count
                FROM recovery_point_copies
                WHERE target_id = ? AND recoverable = 1 AND state = 'healthy'
                """,
                (target_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
    return int(row["copy_count"] if row is not None else 0)


def update_recovery_copy_state(
    target_id: str,
    policy_id: str,
    backup_id: str,
    *,
    state: str,
    recoverable: bool | None = None,
    last_verified_at: str | None = None,
    last_scrub_at: str | None = None,
    last_drill_at: str | None = None,
    last_repair_at: str | None = None,
    last_failure: str | None = None,
) -> None:
    with _DB_LOCK, _get_connection() as conn:
        updates: list[str] = ["state = ?"]
        params: list[Any] = [str(state)]
        if recoverable is not None:
            updates.append("recoverable = ?")
            params.append(1 if recoverable else 0)
        if last_verified_at is not None:
            updates.append("last_verified_at = ?")
            params.append(last_verified_at)
        if last_scrub_at is not None:
            updates.append("last_scrub_at = ?")
            params.append(last_scrub_at)
        if last_drill_at is not None:
            updates.append("last_drill_at = ?")
            params.append(last_drill_at)
        if last_repair_at is not None:
            updates.append("last_repair_at = ?")
            params.append(last_repair_at)
        if last_failure is not None:
            updates.append("last_failure = ?")
            params.append(last_failure)
        params.extend([str(target_id), str(policy_id), str(backup_id)])
        conn.execute(
            f"UPDATE recovery_point_copies SET {', '.join(updates)} WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
            params,
        )


def mark_logical_recovery_point_retired(
    policy_id: str,
    backup_id: str,
    *,
    retired_at: str | None = None,
) -> None:
    """Mark a logical recovery point as retired, so it is never repaired or resurrected."""
    at = retired_at or _utc_iso()
    with _DB_LOCK, _get_connection() as conn:
        conn.execute(
            "UPDATE logical_recovery_points SET retained = 0, retired_at = ? WHERE policy_id = ? AND backup_id = ?",
            (at, str(policy_id), str(backup_id)),
        )
        conn.execute(
            "UPDATE recovery_point_copies SET state = 'retired', recoverable = 0 WHERE policy_id = ? AND backup_id = ?",
            (str(policy_id), str(backup_id)),
        )


def is_logical_recovery_point_retired(policy_id: str, backup_id: str) -> bool:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute(
            "SELECT retained, retired_at FROM logical_recovery_points WHERE policy_id = ? AND backup_id = ? LIMIT 1",
            (str(policy_id), str(backup_id)),
        ).fetchone()
        if row is None:
            return False
        return int(row["retained"] or 0) == 0 or row["retired_at"] is not None


def get_logical_recovery_point(policy_id: str, backup_id: str) -> dict[str, Any] | None:
    with _DB_LOCK, _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM logical_recovery_points WHERE policy_id = ? AND backup_id = ? LIMIT 1",
            (str(policy_id), str(backup_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "logicalId": str(row["logical_id"]),
            "policyId": str(row["policy_id"]),
            "backupId": str(row["backup_id"]),
            "objectSetDigest": str(row["object_set_digest"]) if row["object_set_digest"] else None,
            "committedAt": str(row["committed_at"]),
            "snapshotKind": str(row["snapshot_kind"]),
            "retained": bool(row["retained"]) if "retained" in row.keys() else True,
            "retiredAt": str(row["retired_at"]) if "retired_at" in row.keys() and row["retired_at"] else None,
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

