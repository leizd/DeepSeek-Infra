"""Cross-process authority for storage-control policy and topology mutations.

Human-readable JSON files remain projections, while this SQLite database owns
CAS revisions, topology generations, maintenance leases/cursors, capacity
evidence, shared transfer-budget state, lifecycle intents, and rebuildable
object-reference indexes (4.5.9).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

CONTROL_DIR = config.ROOT / ".backup-control"
CONTROL_DB = CONTROL_DIR / "control.sqlite3"

# Non-rebuildable authority lives at schema version >= 2 (4.5.9 journal + index).
# v3 adds canonical physical object identity for scale-safe capacity/GC (4.6.0).
# v4 adds index coverage, recovery lineage graph, chain migration jobs (4.6.0 B/C/D).
# v5 adds fail-closed index coverage evidence + formal receipt mutation generation (4.6.1).
CONTROL_SCHEMA_VERSION = 5

REBUILDABLE_TABLES = frozenset(
    {
        "target_objects",
        "recovery_object_refs",
        "capacity_evidence",
        "target_capacity_observations",
        "capacity_growth_observations",
        "qos_buckets",
        "qos_transfers",
        "maintenance_cursors",
        "target_index_coverage",
        "recovery_lineage",
        "capacity_forecast_projections",
    }
)
NON_REBUILDABLE_TABLES = frozenset(
    {
        "control_policies",
        "control_targets",
        "lifecycle_intents",
        "maintenance_leases",
        "schema_migrations",
        "chain_migration_jobs",
        "target_receipt_mutations",
    }
)

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

CREATE TABLE IF NOT EXISTS target_capacity_observations (
    target_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_intents (
    intent_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id TEXT,
    policy_id TEXT,
    backup_id TEXT,
    expected_generation INTEGER,
    phase TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_objects (
    target_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    ciphertext_digest TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    live_ref_count INTEGER NOT NULL DEFAULT 0,
    retired_ref_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'unknown',
    etag TEXT,
    is_physical INTEGER NOT NULL DEFAULT 1,
    canonical_object_key TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target_id, object_key)
);

CREATE TABLE IF NOT EXISTS recovery_object_refs (
    target_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    ref_state TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    ciphertext_digest TEXT,
    is_physical INTEGER NOT NULL DEFAULT 1,
    canonical_object_key TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target_id, policy_id, backup_id, object_key)
);

CREATE TABLE IF NOT EXISTS capacity_growth_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    physical_stored_bytes INTEGER NOT NULL,
    live_referenced_bytes INTEGER NOT NULL DEFAULT 0,
    retired_pending_gc_bytes INTEGER NOT NULL DEFAULT 0,
    new_committed_bytes INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capacity_policy_kind_time
ON capacity_evidence(policy_id, snapshot_kind, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_lifecycle_intents_kind_phase
ON lifecycle_intents(kind, phase);

CREATE INDEX IF NOT EXISTS idx_lifecycle_intents_target
ON lifecycle_intents(target_id, phase);

CREATE INDEX IF NOT EXISTS idx_target_objects_live
ON target_objects(target_id, live_ref_count);

CREATE INDEX IF NOT EXISTS idx_recovery_refs_backup
ON recovery_object_refs(target_id, policy_id, backup_id);

CREATE INDEX IF NOT EXISTS idx_growth_target_time
ON capacity_growth_observations(target_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS target_index_coverage (
    target_id TEXT PRIMARY KEY,
    index_generation INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'empty',
    formal_receipt_count INTEGER NOT NULL DEFAULT 0,
    last_receipt_cursor TEXT,
    source_head_generation INTEGER,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    enumerated_receipts INTEGER NOT NULL DEFAULT 0,
    parsed_receipts INTEGER NOT NULL DEFAULT 0,
    indexed_receipts INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0,
    read_failures INTEGER NOT NULL DEFAULT 0,
    source_receipt_mutation_generation INTEGER,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS target_receipt_mutations (
    target_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_lineage (
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL DEFAULT 'full',
    parent_backup_id TEXT,
    base_backup_id TEXT,
    chain_depth INTEGER NOT NULL DEFAULT 0,
    object_set_digest TEXT,
    committed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, backup_id)
);

CREATE INDEX IF NOT EXISTS idx_lineage_parent
ON recovery_lineage(policy_id, parent_backup_id);

CREATE TABLE IF NOT EXISTS capacity_forecast_projections (
    target_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_migration_jobs (
    migration_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    anchor_backup_id TEXT NOT NULL,
    desired_tier TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chain_migration_phase
ON chain_migration_jobs(phase, updated_at);
"""


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _retry_locked(operation: Callable[[], Any], *, timeout_seconds: float = 30.0) -> Any:
    """Retry SQLite startup work while another process initializes the DB."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    delay = 0.01
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            if "locked" not in message and "busy" not in message:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.25, delay * 2)


def _apply_schema_migrations(conn: sqlite3.Connection) -> None:
    """Create tables and advance PRAGMA user_version with an explicit ledger."""
    conn.executescript(_SCHEMA)
    row = conn.execute("PRAGMA user_version").fetchone()
    current = int(row[0] if row is not None else 0)
    if current > CONTROL_SCHEMA_VERSION:
        raise AppError(
            f"storage control schema version {current} is newer than supported {CONTROL_SCHEMA_VERSION}",
            code=ErrorCode.INTERNAL,
            status=500,
        )
    if current < CONTROL_SCHEMA_VERSION:
        now = _utc_iso()
        # Additive columns for pre-v3 DBs (CREATE IF NOT EXISTS leaves old tables intact).
        if current < 3:
            for stmt in (
                "ALTER TABLE target_objects ADD COLUMN is_physical INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE target_objects ADD COLUMN canonical_object_key TEXT",
                "ALTER TABLE recovery_object_refs ADD COLUMN is_physical INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE recovery_object_refs ADD COLUMN canonical_object_key TEXT",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already present
        if current < 5:
            for stmt in (
                "ALTER TABLE target_index_coverage ADD COLUMN enumerated_receipts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_index_coverage ADD COLUMN parsed_receipts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_index_coverage ADD COLUMN indexed_receipts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_index_coverage ADD COLUMN parse_failures INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_index_coverage ADD COLUMN read_failures INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_index_coverage ADD COLUMN source_receipt_mutation_generation INTEGER",
                "ALTER TABLE target_index_coverage ADD COLUMN reason TEXT",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
        for version in range(max(1, current + 1), CONTROL_SCHEMA_VERSION + 1):
            description = {
                1: "4.5.8-baseline-control-authority",
                2: "4.5.9-lifecycle-intents-and-object-index",
                3: "4.6.0-canonical-physical-object-identity",
                4: "4.6.0-lineage-index-coverage-chain-migration",
                5: "4.6.1-fail-closed-index-coverage-and-receipt-mutation",
            }.get(version, f"schema-v{version}")
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at, description)
                VALUES (?, ?, ?)
                """,
                (version, now, description),
            )
        conn.execute(f"PRAGMA user_version = {CONTROL_SCHEMA_VERSION}")


def _quick_check_or_fail(conn: sqlite3.Connection) -> None:
    """Fail closed when SQLite reports corruption on non-rebuildable authority."""
    row = conn.execute("PRAGMA quick_check").fetchone()
    result = str(row[0] if row is not None else "unknown")
    if result.casefold() != "ok":
        raise AppError(
            f"storage control authority failed integrity check: {result}",
            code=ErrorCode.INTERNAL,
            status=500,
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CONTROL_DB, timeout=30.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
        conn.execute("PRAGMA synchronous=FULL")
        _retry_locked(lambda: _apply_schema_migrations(conn))
        _quick_check_or_fail(conn)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def acquire_maintenance_lease(
    worker_kind: str,
    scope_id: str,
    *,
    owner_instance_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Acquire one cross-process maintenance scope with a fencing token."""
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    current_iso = current.isoformat(timespec="seconds").replace("+00:00", "Z")
    lease_until = (current + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds").replace("+00:00", "Z")
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT * FROM maintenance_leases WHERE worker_kind = ? AND scope_id = ?",
            (worker_kind, scope_id),
        ).fetchone()
        if row is not None and str(row["lease_until"]) > current_iso:
            conn.execute("ROLLBACK")
            return None
        fencing_token = int(row["fencing_token"] if row is not None else 0) + 1
        conn.execute(
            """
            INSERT INTO maintenance_leases(
                worker_kind, scope_id, owner_instance_id, fencing_token, lease_until, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_kind, scope_id) DO UPDATE SET
                owner_instance_id = excluded.owner_instance_id,
                fencing_token = excluded.fencing_token,
                lease_until = excluded.lease_until,
                updated_at = excluded.updated_at
            """,
            (worker_kind, scope_id, owner_instance_id, fencing_token, lease_until, current_iso),
        )
        conn.execute("COMMIT")
    return {
        "workerKind": worker_kind,
        "scopeId": scope_id,
        "ownerInstanceId": owner_instance_id,
        "fencingToken": fencing_token,
        "leaseUntil": lease_until,
    }


def release_maintenance_lease(
    worker_kind: str,
    scope_id: str,
    *,
    owner_instance_id: str,
    fencing_token: int,
) -> bool:
    """Release a lease only when owner and fencing token still match."""
    with _connect() as conn:
        result = conn.execute(
            """
            DELETE FROM maintenance_leases
            WHERE worker_kind = ? AND scope_id = ?
              AND owner_instance_id = ? AND fencing_token = ?
            """,
            (worker_kind, scope_id, owner_instance_id, int(fencing_token)),
        )
    return result.rowcount == 1


def renew_maintenance_lease(
    worker_kind: str,
    scope_id: str,
    *,
    owner_instance_id: str,
    fencing_token: int,
    lease_seconds: int = 60,
) -> bool:
    """Extend a still-owned, unexpired lease; fenced or stale owners fail."""
    current = datetime.now(tz=timezone.utc)
    current_iso = current.isoformat(timespec="seconds").replace("+00:00", "Z")
    lease_until = (current + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="seconds").replace("+00:00", "Z")
    with _connect() as conn:
        result = conn.execute(
            """
            UPDATE maintenance_leases
            SET lease_until = ?, updated_at = ?
            WHERE worker_kind = ? AND scope_id = ?
              AND owner_instance_id = ? AND fencing_token = ? AND lease_until >= ?
            """,
            (
                lease_until,
                current_iso,
                worker_kind,
                scope_id,
                owner_instance_id,
                int(fencing_token),
                current_iso,
            ),
        )
    return result.rowcount == 1


def get_maintenance_cursor(worker_kind: str, scope_id: str) -> dict[str, Any]:
    """Return the durable cursor and its CAS generation."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT cursor_json, generation FROM maintenance_cursors WHERE worker_kind = ? AND scope_id = ?",
            (worker_kind, scope_id),
        ).fetchone()
    if row is None:
        return {"cursor": None, "generation": 0}
    value = json.loads(str(row["cursor_json"])) if row["cursor_json"] else None
    return {"cursor": value if isinstance(value, dict) else None, "generation": int(row["generation"])}


def update_maintenance_cursor(
    worker_kind: str,
    scope_id: str,
    cursor: dict[str, Any] | None,
    *,
    expected_generation: int,
) -> dict[str, Any]:
    """CAS-update a durable cursor so a stale worker cannot rewind progress."""
    now = _utc_iso()
    encoded = json.dumps(cursor, ensure_ascii=False, sort_keys=True) if cursor is not None else None
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT generation FROM maintenance_cursors WHERE worker_kind = ? AND scope_id = ?",
            (worker_kind, scope_id),
        ).fetchone()
        generation = int(row["generation"] if row is not None else 0)
        if generation != int(expected_generation):
            conn.execute("ROLLBACK")
            raise AppError("Maintenance cursor generation mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
        next_generation = generation + 1
        conn.execute(
            """
            INSERT INTO maintenance_cursors(worker_kind, scope_id, cursor_json, generation, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(worker_kind, scope_id) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                generation = excluded.generation,
                updated_at = excluded.updated_at
            """,
            (worker_kind, scope_id, encoded, next_generation, now),
        )
        conn.execute("COMMIT")
    return {"cursor": cursor, "generation": next_generation}


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


def list_target_ids_page(*, after_target_id: str | None = None, limit: int = 100) -> list[str]:
    """Return a keyset page from the authoritative Target registry."""
    with _connect() as conn:
        if after_target_id:
            rows = conn.execute(
                "SELECT target_id FROM control_targets WHERE target_id > ? ORDER BY target_id LIMIT ?",
                (after_target_id, max(1, min(int(limit), 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT target_id FROM control_targets ORDER BY target_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
    return [str(row["target_id"]) for row in rows]


def record_target_capacity_observation(target_id: str, observation: dict[str, Any]) -> None:
    """Persist an operator-inspectable result from the bounded capacity probe worker."""
    observed_at = str(observation.get("observedAt") or _utc_iso())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO target_capacity_observations(target_id, payload_json, observed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                observed_at = excluded.observed_at
            """,
            (target_id, json.dumps(observation, ensure_ascii=False, sort_keys=True), observed_at),
        )


def get_target_capacity_observation(target_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM target_capacity_observations WHERE target_id = ?", (target_id,)
        ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row["payload_json"]))
    return value if isinstance(value, dict) else None


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


def schema_version() -> int:
    with _connect() as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0] if row is not None else 0)


def list_schema_migrations() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT version, applied_at, description FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [
        {
            "version": int(row["version"]),
            "appliedAt": str(row["applied_at"]),
            "description": str(row["description"]),
        }
        for row in rows
    ]


def create_control_checkpoint(destination: Path | None = None) -> Path:
    """Online SQLite backup of the control authority (non-rebuildable + rebuildable)."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    dest = destination or (CONTROL_DIR / f"control-checkpoint-{_utc_iso().replace(':', '')}.sqlite3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as src:
        dest_conn = sqlite3.connect(dest, timeout=30.0)
        try:
            src.backup(dest_conn)
        finally:
            dest_conn.close()
    return dest


def commit_lifecycle_intent(
    *,
    kind: str,
    target_id: str | None = None,
    policy_id: str | None = None,
    backup_id: str | None = None,
    expected_generation: int | None = None,
    phase: str = "committed",
    payload: dict[str, Any] | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Durably record a lifecycle intent under BEGIN IMMEDIATE."""
    now = _utc_iso()
    record_id = intent_id or f"intent_{secrets.token_hex(8)}"
    body = payload or {}
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute(
            """
            INSERT INTO lifecycle_intents(
                intent_id, kind, target_id, policy_id, backup_id,
                expected_generation, phase, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                str(kind),
                str(target_id) if target_id else None,
                str(policy_id) if policy_id else None,
                str(backup_id) if backup_id else None,
                int(expected_generation) if expected_generation is not None else None,
                str(phase),
                json.dumps(body, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute("COMMIT")
    return get_lifecycle_intent(record_id) or {}


def update_lifecycle_intent_phase(intent_id: str, phase: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _utc_iso()
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute("SELECT * FROM lifecycle_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("lifecycle intent not found", code=ErrorCode.NOT_FOUND, status=404)
        next_payload = payload
        if next_payload is None:
            decoded = json.loads(str(row["payload_json"]))
            next_payload = decoded if isinstance(decoded, dict) else {}
        conn.execute(
            """
            UPDATE lifecycle_intents
            SET phase = ?, payload_json = ?, updated_at = ?
            WHERE intent_id = ?
            """,
            (str(phase), json.dumps(next_payload, ensure_ascii=False, sort_keys=True), now, intent_id),
        )
        conn.execute("COMMIT")
    return get_lifecycle_intent(intent_id) or {}


def get_lifecycle_intent(intent_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM lifecycle_intents WHERE intent_id = ?", (intent_id,)).fetchone()
    return _decode_lifecycle_intent(row) if row is not None else None


def list_lifecycle_intents(
    *,
    kind: str | None = None,
    target_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM lifecycle_intents WHERE 1=1"
    params: list[Any] = []
    if kind:
        query += " AND kind = ?"
        params.append(str(kind))
    if target_id:
        query += " AND target_id = ?"
        params.append(str(target_id))
    if phase:
        query += " AND phase = ?"
        params.append(str(phase))
    query += " ORDER BY created_at ASC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_decode_lifecycle_intent(row) for row in rows]


def _decode_lifecycle_intent(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return {
        "intentId": str(row["intent_id"]),
        "kind": str(row["kind"]),
        "targetId": str(row["target_id"]) if row["target_id"] else None,
        "policyId": str(row["policy_id"]) if row["policy_id"] else None,
        "backupId": str(row["backup_id"]) if row["backup_id"] else None,
        "expectedGeneration": int(row["expected_generation"]) if row["expected_generation"] is not None else None,
        "phase": str(row["phase"]),
        "payload": payload,
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def begin_target_drain_intent(
    target_id: str,
    *,
    reason: str,
    drain_id: str,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Atomically journal a drain intent and mark the target draining.

    Job DBs remain projections; crash after this commit still leaves a durable
    intent that startup reconciliation can use to recreate the DrainJob.
    """
    now = _utc_iso()
    intent_id = f"intent_drain_{secrets.token_hex(8)}"
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
        current = _decode_payload(row)
        next_generation = generation + 1
        updated = {
            **current,
            "targetId": target_id,
            "topologyGeneration": next_generation,
            "drainState": "draining",
            "drainReason": reason,
            "drainingAt": now,
            "activeDrainIntentId": intent_id,
            "activeDrainId": drain_id,
        }
        conn.execute(
            "UPDATE control_targets SET generation = ?, payload_json = ?, updated_at = ? WHERE target_id = ? AND generation = ?",
            (next_generation, json.dumps(updated, ensure_ascii=False, sort_keys=True), now, target_id, generation),
        )
        conn.execute(
            """
            INSERT INTO lifecycle_intents(
                intent_id, kind, target_id, policy_id, backup_id,
                expected_generation, phase, payload_json, created_at, updated_at
            ) VALUES (?, 'drain', ?, NULL, NULL, ?, 'topology-committed', ?, ?, ?)
            """,
            (
                intent_id,
                target_id,
                next_generation,
                json.dumps(
                    {"drainId": drain_id, "reason": reason, "targetId": target_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        conn.execute("COMMIT")
    return {"intentId": intent_id, "target": updated, "drainId": drain_id}


def complete_lifecycle_intent(intent_id: str, *, phase: str = "completed") -> dict[str, Any]:
    return update_lifecycle_intent_phase(intent_id, phase)


def upsert_target_object(
    *,
    target_id: str,
    object_key: str,
    size_bytes: int = 0,
    ciphertext_digest: str | None = None,
    etag: str | None = None,
    live_delta: int = 0,
    retired_delta: int = 0,
    state: str | None = None,
) -> dict[str, Any]:
    """Insert or adjust a rebuildable object inventory row."""
    now = _utc_iso()
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT * FROM target_objects WHERE target_id = ? AND object_key = ?",
            (target_id, object_key),
        ).fetchone()
        if row is None:
            live = max(0, int(live_delta))
            retired = max(0, int(retired_delta))
            next_state = state or ("live" if live > 0 else "retired" if retired > 0 else "unknown")
            conn.execute(
                """
                INSERT INTO target_objects(
                    target_id, object_key, ciphertext_digest, size_bytes,
                    live_ref_count, retired_ref_count, state, etag, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    object_key,
                    ciphertext_digest,
                    max(0, int(size_bytes)),
                    live,
                    retired,
                    next_state,
                    etag,
                    now,
                ),
            )
        else:
            live = max(0, int(row["live_ref_count"]) + int(live_delta))
            retired = max(0, int(row["retired_ref_count"]) + int(retired_delta))
            next_size = max(0, int(size_bytes)) if size_bytes else int(row["size_bytes"])
            next_digest = ciphertext_digest if ciphertext_digest is not None else row["ciphertext_digest"]
            next_etag = etag if etag is not None else row["etag"]
            if state is not None:
                next_state = state
            elif live > 0:
                next_state = "live"
            elif retired > 0:
                next_state = "retired-pending-gc"
            else:
                next_state = "gc-candidate"
            conn.execute(
                """
                UPDATE target_objects
                SET ciphertext_digest = ?, size_bytes = ?, live_ref_count = ?,
                    retired_ref_count = ?, state = ?, etag = ?, updated_at = ?
                WHERE target_id = ? AND object_key = ?
                """,
                (next_digest, next_size, live, retired, next_state, next_etag, now, target_id, object_key),
            )
        conn.execute("COMMIT")
    return get_target_object(target_id, object_key) or {}


def get_target_object(target_id: str, object_key: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_objects WHERE target_id = ? AND object_key = ?",
            (target_id, object_key),
        ).fetchone()
    return _decode_target_object(row) if row is not None else None


def list_target_objects(
    target_id: str,
    *,
    gc_candidates_only: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM target_objects WHERE target_id = ?"
    params: list[Any] = [target_id]
    if gc_candidates_only:
        query += " AND live_ref_count = 0 AND state IN ('gc-candidate', 'retired-pending-gc')"
    query += " ORDER BY object_key LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_decode_target_object(row) for row in rows]


def _decode_target_object(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "targetId": str(row["target_id"]),
        "objectKey": str(row["object_key"]),
        "ciphertextDigest": str(row["ciphertext_digest"]) if row["ciphertext_digest"] else None,
        "sizeBytes": int(row["size_bytes"]),
        "liveRefCount": int(row["live_ref_count"]),
        "retiredRefCount": int(row["retired_ref_count"]),
        "state": str(row["state"]),
        "etag": str(row["etag"]) if row["etag"] else None,
        "updatedAt": str(row["updated_at"]),
    }


def put_recovery_object_ref(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    object_key: str,
    ref_state: str,
    size_bytes: int = 0,
    ciphertext_digest: str | None = None,
    physical: bool = True,
    canonical_object_key: str | None = None,
) -> None:
    now = _utc_iso()
    is_physical = 1 if physical else 0
    # Aliases never contribute physical size.
    stored_size = max(0, int(size_bytes)) if physical else 0
    canon = canonical_object_key or (object_key if physical else None)
    with _connect() as conn:
        _begin_immediate(conn)
        existing = conn.execute(
            """
            SELECT ref_state FROM recovery_object_refs
            WHERE target_id = ? AND policy_id = ? AND backup_id = ? AND object_key = ?
            """,
            (target_id, policy_id, backup_id, object_key),
        ).fetchone()
        previous = str(existing["ref_state"]) if existing is not None else None
        conn.execute(
            """
            INSERT INTO recovery_object_refs(
                target_id, policy_id, backup_id, object_key, ref_state,
                size_bytes, ciphertext_digest, is_physical, canonical_object_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id, policy_id, backup_id, object_key) DO UPDATE SET
                ref_state = excluded.ref_state,
                size_bytes = excluded.size_bytes,
                ciphertext_digest = excluded.ciphertext_digest,
                is_physical = excluded.is_physical,
                canonical_object_key = excluded.canonical_object_key,
                updated_at = excluded.updated_at
            """,
            (
                target_id,
                policy_id,
                backup_id,
                object_key,
                str(ref_state),
                stored_size,
                ciphertext_digest,
                is_physical,
                canon,
                now,
            ),
        )
        live_delta = 0
        retired_delta = 0
        if previous != "live" and ref_state == "live":
            live_delta = 1
        elif previous == "live" and ref_state != "live":
            live_delta = -1
        if previous != "retired" and ref_state == "retired":
            retired_delta = 1
        elif previous == "retired" and ref_state != "retired":
            retired_delta = -1
        obj = conn.execute(
            "SELECT * FROM target_objects WHERE target_id = ? AND object_key = ?",
            (target_id, object_key),
        ).fetchone()
        if obj is None:
            live = 1 if ref_state == "live" else 0
            retired = 1 if ref_state == "retired" else 0
            state = "live" if live else ("retired-pending-gc" if retired else "unknown")
            conn.execute(
                """
                INSERT INTO target_objects(
                    target_id, object_key, ciphertext_digest, size_bytes,
                    live_ref_count, retired_ref_count, state, etag,
                    is_physical, canonical_object_key, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    target_id,
                    object_key,
                    ciphertext_digest,
                    stored_size,
                    live,
                    retired,
                    state,
                    is_physical,
                    canon,
                    now,
                ),
            )
        else:
            live = max(0, int(obj["live_ref_count"]) + live_delta)
            retired = max(0, int(obj["retired_ref_count"]) + retired_delta)
            next_size = max(int(obj["size_bytes"]), stored_size) if physical else int(obj["size_bytes"])
            if live > 0:
                state = "live"
            elif retired > 0:
                state = "retired-pending-gc"
            else:
                state = "gc-candidate"
            conn.execute(
                """
                UPDATE target_objects
                SET live_ref_count = ?, retired_ref_count = ?, size_bytes = ?,
                    ciphertext_digest = COALESCE(?, ciphertext_digest),
                    state = ?, is_physical = ?,
                    canonical_object_key = COALESCE(?, canonical_object_key),
                    updated_at = ?
                WHERE target_id = ? AND object_key = ?
                """,
                (live, retired, next_size, ciphertext_digest, state, is_physical, canon, now, target_id, object_key),
            )
        conn.execute("COMMIT")


def list_recovery_object_refs(
    *,
    target_id: str,
    policy_id: str | None = None,
    backup_id: str | None = None,
    ref_state: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Bounded listing for operator UI — not for GC correctness paths."""
    query = "SELECT * FROM recovery_object_refs WHERE target_id = ?"
    params: list[Any] = [target_id]
    if policy_id:
        query += " AND policy_id = ?"
        params.append(policy_id)
    if backup_id:
        query += " AND backup_id = ?"
        params.append(backup_id)
    if ref_state:
        query += " AND ref_state = ?"
        params.append(ref_state)
    query += " ORDER BY policy_id, backup_id, object_key LIMIT ?"
    params.append(max(1, min(int(limit), 20000)))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_decode_recovery_ref(row) for row in rows]


def list_recovery_object_refs_complete(
    *,
    target_id: str,
    policy_id: str | None = None,
    backup_id: str | None = None,
    ref_state: str | None = None,
    page_size: int = 2000,
) -> list[dict[str, Any]]:
    """Keyset-complete scan for correctness paths (retirement apply, rebuilds)."""
    page_size = max(1, min(int(page_size), 5000))
    results: list[dict[str, Any]] = []
    after_policy = ""
    after_backup = ""
    after_key = ""
    while True:
        query = """
            SELECT * FROM recovery_object_refs
            WHERE target_id = ?
              AND (
                    policy_id > ?
                 OR (policy_id = ? AND backup_id > ?)
                 OR (policy_id = ? AND backup_id = ? AND object_key > ?)
              )
        """
        params: list[Any] = [target_id, after_policy, after_policy, after_backup, after_policy, after_backup, after_key]
        if policy_id:
            query += " AND policy_id = ?"
            params.append(policy_id)
        if backup_id:
            query += " AND backup_id = ?"
            params.append(backup_id)
        if ref_state:
            query += " AND ref_state = ?"
            params.append(ref_state)
        query += " ORDER BY policy_id, backup_id, object_key LIMIT ?"
        params.append(page_size)
        with _connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if not rows:
            break
        for row in rows:
            results.append(_decode_recovery_ref(row))
        last = rows[-1]
        after_policy = str(last["policy_id"])
        after_backup = str(last["backup_id"])
        after_key = str(last["object_key"])
        if len(rows) < page_size:
            break
    return results


def _decode_recovery_ref(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "targetId": str(row["target_id"]),
        "policyId": str(row["policy_id"]),
        "backupId": str(row["backup_id"]),
        "objectKey": str(row["object_key"]),
        "refState": str(row["ref_state"]),
        "sizeBytes": int(row["size_bytes"]),
        "ciphertextDigest": str(row["ciphertext_digest"]) if row["ciphertext_digest"] else None,
        "physical": bool(int(row["is_physical"])) if "is_physical" in keys and row["is_physical"] is not None else True,
        "canonicalObjectKey": str(row["canonical_object_key"]) if "canonical_object_key" in keys and row["canonical_object_key"] else None,
        "updatedAt": str(row["updated_at"]),
    }


def object_has_live_ref(
    target_id: str,
    object_key: str,
    *,
    excluding_backup_id: str | None = None,
) -> bool:
    """Return True if any live recovery_object_refs row still points at the key.

    When ``excluding_backup_id`` is set, only the refs table is consulted so a
    retiring backup's own refs cannot keep its objects artificially live via
    the aggregated live_ref_count projection.
    """
    with _connect() as conn:
        if excluding_backup_id:
            row = conn.execute(
                """
                SELECT 1 FROM recovery_object_refs
                WHERE target_id = ? AND object_key = ? AND ref_state = 'live'
                  AND backup_id != ?
                LIMIT 1
                """,
                (target_id, object_key, excluding_backup_id),
            ).fetchone()
            return row is not None
        row = conn.execute(
            """
            SELECT 1 FROM recovery_object_refs
            WHERE target_id = ? AND object_key = ? AND ref_state = 'live'
            LIMIT 1
            """,
            (target_id, object_key),
        ).fetchone()
        if row is not None:
            return True
        obj = conn.execute(
            """
            SELECT live_ref_count FROM target_objects
            WHERE target_id = ? AND object_key = ?
            """,
            (target_id, object_key),
        ).fetchone()
    if obj is None:
        return False
    return int(obj["live_ref_count"]) > 0


def target_object_index_nonempty(target_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM target_objects WHERE target_id = ? LIMIT 1",
            (target_id,),
        ).fetchone()
    return row is not None


def physical_usage_summary(target_id: str) -> dict[str, Any]:
    """Aggregate physical accounting — one ciphertext counted once.

    Only rows with ``is_physical = 1`` contribute size. Compatibility alias
    rows (legacy ciphertext/ paths) carry size_bytes=0 and is_physical=0.
    When multiple physical rows share a ciphertext_digest, the max size is
    taken once per digest.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT object_key, state, live_ref_count, retired_ref_count, size_bytes,
                   ciphertext_digest, COALESCE(is_physical, 1) AS is_physical
            FROM target_objects WHERE target_id = ?
            """,
            (target_id,),
        ).fetchall()
    # digest -> (size, live, retired_pending)
    by_digest: dict[str, tuple[int, bool, bool]] = {}
    physical = 0
    live_bytes = 0
    retired_pending = 0
    physical_rows = 0
    for row in rows:
        if int(row["is_physical"] or 0) != 1:
            continue
        physical_rows += 1
        size = max(0, int(row["size_bytes"]))
        digest = str(row["ciphertext_digest"] or row["object_key"])
        live = int(row["live_ref_count"]) > 0
        retired = (not live) and (
            int(row["retired_ref_count"]) > 0 or str(row["state"]) in {"retired-pending-gc", "gc-candidate"}
        )
        prev = by_digest.get(digest)
        if prev is None:
            by_digest[digest] = (size, live, retired)
        else:
            by_digest[digest] = (
                max(prev[0], size),
                prev[1] or live,
                (prev[2] or retired) and not (prev[1] or live),
            )
    for size, live, retired in by_digest.values():
        physical += size
        if live:
            live_bytes += size
        elif retired:
            retired_pending += size
    confidence = "high" if physical_rows else "unavailable"
    return {
        "targetId": target_id,
        "physicalStoredBytes": physical,
        "liveReferencedBytes": live_bytes,
        "retiredPendingGcBytes": retired_pending,
        "controlPlaneBytes": 0,
        "unknownExternalBytes": None,
        "objectCount": physical_rows,
        "uniqueCiphertextCount": len(by_digest),
        "confidence": confidence,
    }


def clear_target_object_index(target_id: str) -> None:
    """Drop rebuildable index rows for one target (explicit rebuild path)."""
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute("DELETE FROM recovery_object_refs WHERE target_id = ?", (target_id,))
        conn.execute("DELETE FROM target_objects WHERE target_id = ?", (target_id,))
        conn.execute("COMMIT")


def record_capacity_growth_observation(
    *,
    target_id: str,
    physical_stored_bytes: int,
    live_referenced_bytes: int = 0,
    retired_pending_gc_bytes: int = 0,
    new_committed_bytes: int = 0,
    observed_at: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO capacity_growth_observations(
                target_id, physical_stored_bytes, live_referenced_bytes,
                retired_pending_gc_bytes, new_committed_bytes, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                max(0, int(physical_stored_bytes)),
                max(0, int(live_referenced_bytes)),
                max(0, int(retired_pending_gc_bytes)),
                max(0, int(new_committed_bytes)),
                str(observed_at or _utc_iso()),
            ),
        )


def list_capacity_growth_observations(target_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM capacity_growth_observations
            WHERE target_id = ?
            ORDER BY observed_at DESC, observation_id DESC
            LIMIT ?
            """,
            (target_id, max(1, min(int(limit), 1000))),
        ).fetchall()
    return [
        {
            "observationId": int(row["observation_id"]),
            "targetId": str(row["target_id"]),
            "physicalStoredBytes": int(row["physical_stored_bytes"]),
            "liveReferencedBytes": int(row["live_referenced_bytes"]),
            "retiredPendingGcBytes": int(row["retired_pending_gc_bytes"]),
            "newCommittedBytes": int(row["new_committed_bytes"]),
            "observedAt": str(row["observed_at"]),
        }
        for row in rows
    ]


# ── Gate B: index coverage + capacity forecast projections ──────────────────


def set_target_index_coverage(
    target_id: str,
    *,
    state: str,
    index_generation: int | None = None,
    formal_receipt_count: int = 0,
    last_receipt_cursor: str | None = None,
    source_head_generation: int | None = None,
    enumerated_receipts: int = 0,
    parsed_receipts: int = 0,
    indexed_receipts: int = 0,
    parse_failures: int = 0,
    read_failures: int = 0,
    source_receipt_mutation_generation: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    # Backward-compatible complete claims: fill evidence defaults and pin mutation gen.
    enum_n = max(0, int(enumerated_receipts))
    parsed_n = max(0, int(parsed_receipts))
    indexed_n = max(0, int(indexed_receipts))
    parse_n = max(0, int(parse_failures))
    read_n = max(0, int(read_failures))
    formal_n = max(0, int(formal_receipt_count))
    if str(state) == "complete":
        if enum_n == 0 and formal_n > 0:
            enum_n = parsed_n = indexed_n = formal_n
        if source_receipt_mutation_generation is None:
            source_receipt_mutation_generation = get_target_receipt_mutation_generation(target_id)
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT index_generation FROM target_index_coverage WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        gen = int(index_generation) if index_generation is not None else (
            int(row["index_generation"]) + 1 if row is not None else 1
        )
        completed_at = now if state == "complete" else None
        conn.execute(
            """
            INSERT INTO target_index_coverage(
                target_id, index_generation, state, formal_receipt_count,
                last_receipt_cursor, source_head_generation, completed_at, updated_at,
                enumerated_receipts, parsed_receipts, indexed_receipts,
                parse_failures, read_failures, source_receipt_mutation_generation, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                index_generation = excluded.index_generation,
                state = excluded.state,
                formal_receipt_count = excluded.formal_receipt_count,
                last_receipt_cursor = excluded.last_receipt_cursor,
                source_head_generation = excluded.source_head_generation,
                completed_at = excluded.completed_at,
                updated_at = excluded.updated_at,
                enumerated_receipts = excluded.enumerated_receipts,
                parsed_receipts = excluded.parsed_receipts,
                indexed_receipts = excluded.indexed_receipts,
                parse_failures = excluded.parse_failures,
                read_failures = excluded.read_failures,
                source_receipt_mutation_generation = excluded.source_receipt_mutation_generation,
                reason = excluded.reason
            """,
            (
                target_id,
                gen,
                str(state),
                formal_n,
                last_receipt_cursor,
                int(source_head_generation) if source_head_generation is not None else None,
                completed_at,
                now,
                enum_n,
                parsed_n,
                indexed_n,
                parse_n,
                read_n,
                int(source_receipt_mutation_generation) if source_receipt_mutation_generation is not None else None,
                str(reason) if reason else None,
            ),
        )
        conn.execute("COMMIT")
    return get_target_index_coverage(target_id) or {}


def get_target_index_coverage(target_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_index_coverage WHERE target_id = ?",
            (target_id,),
        ).fetchone()
    if row is None:
        return None
    keys = set(row.keys())
    return {
        "targetId": str(row["target_id"]),
        "indexGeneration": int(row["index_generation"]),
        "state": str(row["state"]),
        "formalReceiptCount": int(row["formal_receipt_count"]),
        "lastReceiptCursor": str(row["last_receipt_cursor"]) if row["last_receipt_cursor"] else None,
        "sourceHeadGeneration": int(row["source_head_generation"]) if row["source_head_generation"] is not None else None,
        "completedAt": str(row["completed_at"]) if row["completed_at"] else None,
        "updatedAt": str(row["updated_at"]),
        "enumeratedReceipts": int(row["enumerated_receipts"]) if "enumerated_receipts" in keys else 0,
        "parsedReceipts": int(row["parsed_receipts"]) if "parsed_receipts" in keys else 0,
        "indexedReceipts": int(row["indexed_receipts"]) if "indexed_receipts" in keys else 0,
        "parseFailures": int(row["parse_failures"]) if "parse_failures" in keys else 0,
        "readFailures": int(row["read_failures"]) if "read_failures" in keys else 0,
        "sourceReceiptMutationGeneration": (
            int(row["source_receipt_mutation_generation"])
            if "source_receipt_mutation_generation" in keys and row["source_receipt_mutation_generation"] is not None
            else None
        ),
        "reason": str(row["reason"]) if "reason" in keys and row["reason"] else None,
    }


def get_target_receipt_mutation_generation(target_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT generation FROM target_receipt_mutations WHERE target_id = ?",
            (target_id,),
        ).fetchone()
    return int(row["generation"]) if row is not None else 0


def bump_target_receipt_mutation(target_id: str) -> int:
    """Advance formal-receipt mutation generation and dirty index coverage.

    Must be called *before* remote/local formal Receipt writes so a crash after
    a successful write never leaves a stale complete coverage claim.
    """
    now = _utc_iso()
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT generation FROM target_receipt_mutations WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        gen = int(row["generation"]) + 1 if row is not None else 1
        conn.execute(
            """
            INSERT INTO target_receipt_mutations(target_id, generation, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                generation = excluded.generation,
                updated_at = excluded.updated_at
            """,
            (target_id, gen, now),
        )
        # Dirties coverage without advancing index_generation unless a row exists.
        cov = conn.execute(
            "SELECT index_generation FROM target_index_coverage WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        if cov is not None:
            conn.execute(
                """
                UPDATE target_index_coverage
                SET state = 'incomplete',
                    reason = 'formal-receipt-mutation',
                    completed_at = NULL,
                    updated_at = ?
                WHERE target_id = ?
                """,
                (now, target_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO target_index_coverage(
                    target_id, index_generation, state, formal_receipt_count,
                    completed_at, updated_at, reason
                ) VALUES (?, 0, 'incomplete', 0, NULL, ?, 'formal-receipt-mutation')
                """,
                (target_id, now),
            )
        conn.execute("COMMIT")
    return gen


def note_formal_receipt_mutation(target_id: str | None) -> int | None:
    """Public hook for publish/replication/migration before formal Receipt writes."""
    tid = str(target_id or "").strip()
    if not tid:
        return None
    return bump_target_receipt_mutation(tid)


def index_coverage_allows_gc(target_id: str) -> tuple[bool, str]:
    """Index is GC-authoritative only when complete, clean, and fresh."""
    cov = get_target_index_coverage(target_id)
    if cov is None:
        return False, "object-reference-index-missing"
    if str(cov.get("state") or "") != "complete":
        return False, str(cov.get("reason") or "object-reference-index-incomplete")
    if int(cov.get("parseFailures") or 0) > 0:
        return False, "object-reference-index-parse-failures"
    if int(cov.get("readFailures") or 0) > 0:
        return False, "object-reference-index-read-failures"
    enumerated = int(cov.get("enumeratedReceipts") or 0)
    indexed = int(cov.get("indexedReceipts") or 0)
    parsed = int(cov.get("parsedReceipts") or 0)
    if enumerated != indexed or enumerated != parsed:
        return False, "object-reference-index-count-mismatch"
    current_mut = get_target_receipt_mutation_generation(target_id)
    source_mut = cov.get("sourceReceiptMutationGeneration")
    if source_mut is None or int(source_mut) != int(current_mut):
        return False, "object-reference-index-stale"
    return True, "ok"


def put_capacity_forecast_projection(target_id: str, projection: dict[str, Any]) -> None:
    now = _utc_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO capacity_forecast_projections(target_id, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (target_id, json.dumps(projection, ensure_ascii=False, sort_keys=True), now),
        )


def get_capacity_forecast_projection(target_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM capacity_forecast_projections WHERE target_id = ?",
            (target_id,),
        ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row["payload_json"]))
    return value if isinstance(value, dict) else None


# ── Gate C: recovery lineage graph ──────────────────────────────────────────


def upsert_recovery_lineage(
    *,
    policy_id: str,
    backup_id: str,
    snapshot_kind: str = "full",
    parent_backup_id: str | None = None,
    base_backup_id: str | None = None,
    chain_depth: int = 0,
    object_set_digest: str | None = None,
    committed_at: str | None = None,
) -> None:
    now = _utc_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO recovery_lineage(
                policy_id, backup_id, snapshot_kind, parent_backup_id, base_backup_id,
                chain_depth, object_set_digest, committed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(policy_id, backup_id) DO UPDATE SET
                snapshot_kind = excluded.snapshot_kind,
                parent_backup_id = excluded.parent_backup_id,
                base_backup_id = excluded.base_backup_id,
                chain_depth = excluded.chain_depth,
                object_set_digest = excluded.object_set_digest,
                committed_at = excluded.committed_at,
                updated_at = excluded.updated_at
            """,
            (
                policy_id,
                backup_id,
                str(snapshot_kind or "full"),
                parent_backup_id,
                base_backup_id,
                max(0, int(chain_depth)),
                object_set_digest,
                committed_at,
                now,
            ),
        )


def get_recovery_lineage(policy_id: str, backup_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_lineage WHERE policy_id = ? AND backup_id = ?",
            (policy_id, backup_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "policyId": str(row["policy_id"]),
        "backupId": str(row["backup_id"]),
        "snapshotKind": str(row["snapshot_kind"]),
        "parentBackupId": str(row["parent_backup_id"]) if row["parent_backup_id"] else None,
        "baseBackupId": str(row["base_backup_id"]) if row["base_backup_id"] else None,
        "chainDepth": int(row["chain_depth"]),
        "objectSetDigest": str(row["object_set_digest"]) if row["object_set_digest"] else None,
        "committedAt": str(row["committed_at"]) if row["committed_at"] else None,
        "updatedAt": str(row["updated_at"]),
    }


def clear_recovery_lineage(policy_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM recovery_lineage WHERE policy_id = ?", (policy_id,))


# ── Gate D: chain migration jobs ────────────────────────────────────────────


CHAIN_MIGRATION_TERMINAL = frozenset({"converged", "failed-terminal", "cancelled"})


def create_chain_migration_job(record: dict[str, Any]) -> dict[str, Any]:
    now = _utc_iso()
    migration_id = str(record.get("migrationId") or f"mig_{secrets.token_hex(8)}")
    payload = {**record, "migrationId": migration_id}
    with _connect() as conn:
        _begin_immediate(conn)
        conn.execute(
            """
            INSERT INTO chain_migration_jobs(
                migration_id, policy_id, anchor_backup_id, desired_tier,
                phase, payload_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                migration_id,
                str(record["policyId"]),
                str(record["anchorBackupId"]),
                str(record.get("desiredTier") or "warm"),
                str(record.get("phase") or "planned"),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute("COMMIT")
    return get_chain_migration_job(migration_id) or {}


def get_chain_migration_job(migration_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM chain_migration_jobs WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return {
        **payload,
        "migrationId": str(row["migration_id"]),
        "policyId": str(row["policy_id"]),
        "anchorBackupId": str(row["anchor_backup_id"]),
        "desiredTier": str(row["desired_tier"]),
        "phase": str(row["phase"]),
        "error": str(row["error"]) if row["error"] else None,
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def update_chain_migration_job(
    migration_id: str,
    *,
    phase: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    with _connect() as conn:
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT payload_json FROM chain_migration_jobs WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise AppError("chain migration job not found", code=ErrorCode.NOT_FOUND, status=404)
        body = json.loads(str(row["payload_json"]))
        if not isinstance(body, dict):
            body = {}
        if payload is not None:
            body = {**body, **payload}
        body["phase"] = phase
        if error is not None:
            body["error"] = error
        conn.execute(
            """
            UPDATE chain_migration_jobs
            SET phase = ?, payload_json = ?, error = ?, updated_at = ?
            WHERE migration_id = ?
            """,
            (
                phase,
                json.dumps(body, ensure_ascii=False, sort_keys=True),
                error,
                now,
                migration_id,
            ),
        )
        conn.execute("COMMIT")
    return get_chain_migration_job(migration_id) or {}


def list_chain_migration_jobs(
    *,
    phase: str | None = None,
    policy_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = "SELECT migration_id FROM chain_migration_jobs WHERE 1=1"
    params: list[Any] = []
    if phase:
        query += " AND phase = ?"
        params.append(phase)
    if policy_id:
        query += " AND policy_id = ?"
        params.append(policy_id)
    query += " ORDER BY updated_at ASC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        job = get_chain_migration_job(str(row["migration_id"]))
        if job:
            out.append(job)
    return out

