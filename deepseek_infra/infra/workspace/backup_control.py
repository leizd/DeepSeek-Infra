"""Cross-process authority for storage-control policy and topology mutations.

Human-readable JSON files remain projections, while this SQLite database owns
CAS revisions, topology generations, maintenance leases/cursors, capacity
evidence, and shared transfer-budget state.
"""

from __future__ import annotations

import json
import sqlite3
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


def database_path() -> Path:
    return CONTROL_DB
