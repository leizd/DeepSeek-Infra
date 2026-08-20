"""Cross-process authority for storage-control policy and topology mutations.

Human-readable JSON files remain projections, while this SQLite database owns
CAS revisions, topology generations, maintenance leases/cursors, capacity
evidence, and shared transfer-budget state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

CONTROL_DIR = config.ROOT / ".backup-control"
CONTROL_DB = CONTROL_DIR / "control.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_policies (
    policy_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    topology_generation INTEGER NOT NULL DEFAULT 0,
    promotion_epoch INTEGER NOT NULL DEFAULT 0,
    drain_generation INTEGER NOT NULL DEFAULT 0,
    placement_generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_targets (
    target_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_leases (
    worker_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    lease_until TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (worker_kind, scope_id)
);

CREATE TABLE IF NOT EXISTS maintenance_cursors (
    worker_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    cursor_json TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (worker_kind, scope_id)
);

CREATE TABLE IF NOT EXISTS qos_buckets (
    bucket_key TEXT PRIMARY KEY,
    tokens REAL NOT NULL,
    last_refill_at REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qos_transfers (
    transfer_id TEXT PRIMARY KEY,
    owner_instance_id TEXT NOT NULL,
    traffic_class INTEGER NOT NULL,
    source_target_id TEXT,
    dest_target_id TEXT,
    estimated_bytes INTEGER NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    backup_id TEXT,
    snapshot_kind TEXT NOT NULL,
    physical_bytes INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capacity_policy_kind_time
ON capacity_evidence(policy_id, snapshot_kind, observed_at DESC);
"""


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CONTROL_DB, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    return conn


def _decode_payload(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(str(row["payload_json"]))
    if not isinstance(value, dict):  # pragma: no cover - only internal writes reach this table
        raise AppError("storage control authority contains invalid JSON", code=ErrorCode.INTERNAL, status=500)
    return value


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def create_policy(policy: dict[str, Any]) -> dict[str, Any]:
    policy_id = str(policy["policyId"])
    revision = max(1, int(policy.get("policyRevision") or 1))
    stored = {**policy, "policyRevision": revision}
    with _connect() as conn:
        _begin_immediate(conn)
        try:
            conn.execute(
                """
                INSERT INTO control_policies(
                    policy_id, revision, payload_json, topology_generation,
                    promotion_epoch, drain_generation, placement_generation, updated_at
                ) VALUES (?, ?, ?, 0, 0, 0, 0, ?)
                """,
                (policy_id, revision, json.dumps(stored, ensure_ascii=False, sort_keys=True), _utc_iso()),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise AppError("Backup policy id collision; retry", code=ErrorCode.INVALID_REQUEST, status=409) from exc
        conn.execute("COMMIT")
    return stored


def adopt_policy_projection(policy: dict[str, Any]) -> dict[str, Any]:
    """Import a pre-control-plane JSON policy exactly once."""
    policy_id = str(policy["policyId"])
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_policies WHERE policy_id = ?", (policy_id,)).fetchone()
        if row is None:
            revision = max(1, int(policy.get("policyRevision") or 1))
            stored = {**policy, "policyRevision": revision}
            conn.execute(
                """
                INSERT INTO control_policies(
                    policy_id, revision, payload_json, topology_generation,
                    promotion_epoch, drain_generation, placement_generation, updated_at
                ) VALUES (?, ?, ?, 0, 0, 0, 0, ?)
                """,
                (policy_id, revision, json.dumps(stored, ensure_ascii=False, sort_keys=True), _utc_iso()),
            )
        else:
            stored = _decode_payload(row)
        conn.execute("COMMIT")
    return stored


def get_policy(policy_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM control_policies WHERE policy_id = ?", (policy_id,)).fetchone()
    return _decode_payload(row) if row is not None else None


def list_policies() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM control_policies ORDER BY policy_id").fetchall()
    return [_decode_payload(row) for row in rows]


def mutate_policy(
    policy_id: str,
    *,
    expected_revision: int | None,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    generation_kind: str = "placement",
) -> dict[str, Any]:
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_policies WHERE policy_id = ?", (policy_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("Backup policy not found", code=ErrorCode.NOT_FOUND, status=404)
        current = _decode_payload(row)
        revision = int(row["revision"])
        if expected_revision is not None and revision != int(expected_revision):
            conn.execute("ROLLBACK")
            raise AppError(
                f"CAS mismatch on policyRevision: expected {expected_revision}, actual {revision}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )
        updated = mutate(dict(current))
        next_revision = revision + 1
        updated["policyId"] = policy_id
        updated["policyRevision"] = next_revision
        generation_column = {
            "topology": "topology_generation",
            "promotion": "promotion_epoch",
            "drain": "drain_generation",
            "placement": "placement_generation",
        }.get(generation_kind, "placement_generation")
        conn.execute(
            f"""
            UPDATE control_policies
            SET revision = ?, payload_json = ?, {generation_column} = {generation_column} + 1, updated_at = ?
            WHERE policy_id = ? AND revision = ?
            """,
            (next_revision, json.dumps(updated, ensure_ascii=False, sort_keys=True), _utc_iso(), policy_id, revision),
        )
        conn.execute("COMMIT")
    return updated


def delete_policy(policy_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_policies WHERE policy_id = ?", (policy_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("Backup policy not found", code=ErrorCode.NOT_FOUND, status=404)
        revision = int(row["revision"])
        if expected_revision is not None and revision != int(expected_revision):
            conn.execute("ROLLBACK")
            raise AppError(
                f"CAS mismatch on policyRevision: expected {expected_revision}, actual {revision}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )
        payload = _decode_payload(row)
        conn.execute("DELETE FROM control_policies WHERE policy_id = ?", (policy_id,))
        conn.execute("COMMIT")
    return payload


def upsert_target(target: dict[str, Any], *, expected_generation: int | None = None) -> dict[str, Any]:
    target_id = str(target["targetId"])
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_targets WHERE target_id = ?", (target_id,)).fetchone()
        if row is None:
            if expected_generation not in (None, 0):
                conn.execute("ROLLBACK")
                raise AppError("CAS mismatch on topologyGeneration: target does not exist", code=ErrorCode.INVALID_REQUEST, status=412)
            generation = max(1, int(target.get("topologyGeneration") or 1))
            stored = {**target, "topologyGeneration": generation}
            conn.execute(
                "INSERT INTO control_targets(target_id, generation, payload_json, updated_at) VALUES (?, ?, ?, ?)",
                (target_id, generation, json.dumps(stored, ensure_ascii=False, sort_keys=True), _utc_iso()),
            )
        else:
            current_generation = int(row["generation"])
            if expected_generation is not None and current_generation != int(expected_generation):
                conn.execute("ROLLBACK")
                raise AppError(
                    f"CAS mismatch on topologyGeneration: expected {expected_generation}, actual {current_generation}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=412,
                )
            generation = current_generation + 1
            stored = {**target, "topologyGeneration": generation}
            conn.execute(
                "UPDATE control_targets SET generation = ?, payload_json = ?, updated_at = ? WHERE target_id = ?",
                (generation, json.dumps(stored, ensure_ascii=False, sort_keys=True), _utc_iso(), target_id),
            )
        conn.execute("COMMIT")
    return stored


def adopt_target_projection(target: dict[str, Any]) -> dict[str, Any]:
    target_id = str(target["targetId"])
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_targets WHERE target_id = ?", (target_id,)).fetchone()
        if row is None:
            generation = max(1, int(target.get("topologyGeneration") or 1))
            stored = {**target, "topologyGeneration": generation}
            conn.execute(
                "INSERT INTO control_targets(target_id, generation, payload_json, updated_at) VALUES (?, ?, ?, ?)",
                (target_id, generation, json.dumps(stored, ensure_ascii=False, sort_keys=True), _utc_iso()),
            )
        else:
            stored = _decode_payload(row)
        conn.execute("COMMIT")
    return stored


def get_target(target_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM control_targets WHERE target_id = ?", (target_id,)).fetchone()
    return _decode_payload(row) if row is not None else None


def list_targets() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM control_targets ORDER BY target_id").fetchall()
    return [_decode_payload(row) for row in rows]


def mutate_target(
    target_id: str,
    *,
    expected_generation: int | None,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    bump_generation: bool = True,
) -> dict[str, Any]:
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_targets WHERE target_id = ?", (target_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("Backup target not found", code=ErrorCode.NOT_FOUND, status=404)
        generation = int(row["generation"])
        if expected_generation is not None and generation != int(expected_generation):
            conn.execute("ROLLBACK")
            raise AppError(
                f"CAS mismatch on topologyGeneration: expected {expected_generation}, actual {generation}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )
        updated = mutate(_decode_payload(row))
        next_generation = generation + 1 if bump_generation else generation
        updated["targetId"] = target_id
        updated["topologyGeneration"] = next_generation
        conn.execute(
            "UPDATE control_targets SET generation = ?, payload_json = ?, updated_at = ? WHERE target_id = ? AND generation = ?",
            (next_generation, json.dumps(updated, ensure_ascii=False, sort_keys=True), _utc_iso(), target_id, generation),
        )
        conn.execute("COMMIT")
    return updated


def delete_target(target_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM control_targets WHERE target_id = ?", (target_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("Backup target not found", code=ErrorCode.NOT_FOUND, status=404)
        generation = int(row["generation"])
        if expected_generation is not None and generation != int(expected_generation):
            conn.execute("ROLLBACK")
            raise AppError(
                f"CAS mismatch on topologyGeneration: expected {expected_generation}, actual {generation}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )
        payload = _decode_payload(row)
        conn.execute("DELETE FROM control_targets WHERE target_id = ?", (target_id,))
        conn.execute("COMMIT")
    return payload


def record_capacity_evidence(
    *,
    policy_id: str,
    backup_id: str | None,
    snapshot_kind: str,
    physical_bytes: int,
    confidence: str,
    source: str,
    observed_at: str | None = None,
) -> None:
    if physical_bytes <= 0:
        return
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute(
            """
            INSERT INTO capacity_evidence(
                policy_id, backup_id, snapshot_kind, physical_bytes,
                confidence, source, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(policy_id),
                str(backup_id) if backup_id else None,
                str(snapshot_kind or "full"),
                int(physical_bytes),
                str(confidence or "low"),
                str(source or "unknown"),
                str(observed_at or _utc_iso()),
            ),
        )
        conn.execute("COMMIT")


def list_capacity_evidence(
    policy_id: str,
    *,
    snapshot_kind: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM capacity_evidence WHERE policy_id = ?"
    params: list[Any] = [str(policy_id)]
    if snapshot_kind is not None:
        query += " AND snapshot_kind = ?"
        params.append(str(snapshot_kind))
    query += " ORDER BY observed_at DESC, evidence_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "evidenceId": int(row["evidence_id"]),
            "policyId": str(row["policy_id"]),
            "backupId": str(row["backup_id"]) if row["backup_id"] else None,
            "snapshotKind": str(row["snapshot_kind"]),
            "physicalBytes": int(row["physical_bytes"]),
            "confidence": str(row["confidence"]),
            "source": str(row["source"]),
            "observedAt": str(row["observed_at"]),
        }
        for row in rows
    ]


def acquire_qos_transfer(
    *,
    transfer_id: str,
    traffic_class: int,
    source_target_id: str | None,
    dest_target_id: str | None,
    estimated_bytes: int,
    source_concurrency_limit: int,
    dest_concurrency_limit: int,
    lease_seconds: float = 60.0,
    owner_instance_id: str | None = None,
) -> None:
    now = time.time()
    owner = owner_instance_id or f"pid-{os.getpid()}"
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute("DELETE FROM qos_transfers WHERE lease_until < ?", (now,))
        if int(traffic_class) != 0:
            for target_id, limit in (
                (source_target_id, source_concurrency_limit),
                (dest_target_id, dest_concurrency_limit),
            ):
                if not target_id:
                    continue
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS active_count
                    FROM qos_transfers
                    WHERE source_target_id = ? OR dest_target_id = ?
                    """,
                    (target_id, target_id),
                ).fetchone()
                active_count = int(row["active_count"] if row is not None else 0)
                if active_count >= max(1, int(limit)):
                    conn.execute("ROLLBACK")
                    raise AppError(
                        f"target-transfer-concurrency-exceeded: target {target_id} has {active_count} active transfers (max {limit})",
                        code=ErrorCode.RATE_LIMITED,
                        status=429,
                    )
        conn.execute(
            """
            INSERT INTO qos_transfers(
                transfer_id, owner_instance_id, traffic_class,
                source_target_id, dest_target_id, estimated_bytes,
                bytes_transferred, lease_until, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(transfer_id) DO UPDATE SET
                owner_instance_id = excluded.owner_instance_id,
                traffic_class = excluded.traffic_class,
                source_target_id = excluded.source_target_id,
                dest_target_id = excluded.dest_target_id,
                estimated_bytes = excluded.estimated_bytes,
                lease_until = excluded.lease_until,
                updated_at = excluded.updated_at
            """,
            (
                transfer_id,
                owner,
                int(traffic_class),
                source_target_id,
                dest_target_id,
                max(0, int(estimated_bytes)),
                now + max(5.0, float(lease_seconds)),
                _utc_iso(),
            ),
        )
        conn.execute("COMMIT")


def release_qos_transfer(transfer_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM qos_transfers WHERE transfer_id = ?", (str(transfer_id),))


def list_qos_transfers(*, now: float | None = None) -> list[dict[str, Any]]:
    current = time.time() if now is None else float(now)
    with _connect() as conn:
        conn.execute("DELETE FROM qos_transfers WHERE lease_until < ?", (current,))
        rows = conn.execute("SELECT * FROM qos_transfers ORDER BY transfer_id").fetchall()
    return [
        {
            "transferId": str(row["transfer_id"]),
            "ownerInstanceId": str(row["owner_instance_id"]),
            "trafficClass": int(row["traffic_class"]),
            "sourceTargetId": str(row["source_target_id"]) if row["source_target_id"] else None,
            "destTargetId": str(row["dest_target_id"]) if row["dest_target_id"] else None,
            "estimatedBytes": int(row["estimated_bytes"]),
            "bytesTransferred": int(row["bytes_transferred"]),
            "leaseUntil": float(row["lease_until"]),
            "updatedAt": str(row["updated_at"]),
        }
        for row in rows
    ]


def consume_qos_tokens(
    *,
    transfer_id: str,
    requested_bytes: int,
    bucket_specs: list[dict[str, Any]],
    traffic_class: int,
    reserved_global_tokens: int = 0,
    now: float | None = None,
    lease_seconds: float = 60.0,
) -> dict[str, Any]:
    """Atomically reserve one chunk from every hierarchical QoS bucket."""
    current = time.time() if now is None else float(now)
    lease_clock = time.time()
    amount = max(1, int(requested_bytes))
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute("DELETE FROM qos_transfers WHERE lease_until < ?", (lease_clock,))
        p0_row = conn.execute(
            "SELECT COUNT(*) AS active_count FROM qos_transfers WHERE traffic_class = 0"
        ).fetchone()
        active_recovery = int(p0_row["active_count"] if p0_row is not None else 0) > 0

        states: list[dict[str, Any]] = []
        max_wait = 0.0
        for spec in bucket_specs:
            key = str(spec["bucketKey"])
            rate = max(1.0, float(spec["rateBytesPerSecond"]))
            capacity = max(float(amount), float(spec.get("capacityBytes") or rate))
            row = conn.execute("SELECT tokens, last_refill_at FROM qos_buckets WHERE bucket_key = ?", (key,)).fetchone()
            if row is None:
                tokens = capacity
                last_refill = current
            else:
                last_refill = float(row["last_refill_at"])
                tokens = min(capacity, float(row["tokens"]) + max(0.0, current - last_refill) * rate)
            floor = 0.0
            if key == "global" and int(traffic_class) != 0 and active_recovery:
                floor = min(capacity, max(0.0, float(reserved_global_tokens)))
            available = max(0.0, tokens - floor)
            if available < amount:
                max_wait = max(max_wait, (amount - available) / rate)
            states.append({"key": key, "tokens": tokens, "rate": rate, "capacity": capacity})

        for state in states:
            next_tokens = float(state["tokens"]) if max_wait > 0 else float(state["tokens"]) - amount
            conn.execute(
                """
                INSERT INTO qos_buckets(bucket_key, tokens, last_refill_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bucket_key) DO UPDATE SET
                    tokens = excluded.tokens,
                    last_refill_at = excluded.last_refill_at,
                    updated_at = excluded.updated_at
                """,
                (state["key"], next_tokens, current, _utc_iso()),
            )

        if max_wait <= 0:
            conn.execute(
                """
                UPDATE qos_transfers
                SET bytes_transferred = bytes_transferred + ?, lease_until = ?, updated_at = ?
                WHERE transfer_id = ?
                """,
                (amount, lease_clock + max(5.0, float(lease_seconds)), _utc_iso(), transfer_id),
            )
        else:
            conn.execute(
                "UPDATE qos_transfers SET lease_until = ?, updated_at = ? WHERE transfer_id = ?",
                (lease_clock + max(5.0, float(lease_seconds)), _utc_iso(), transfer_id),
            )
        conn.execute("COMMIT")
    return {
        "granted": max_wait <= 0,
        "waitSeconds": max(0.0, max_wait),
        "activeRecovery": active_recovery,
        "requestedBytes": amount,
    }


def database_path() -> Path:
    return CONTROL_DB
