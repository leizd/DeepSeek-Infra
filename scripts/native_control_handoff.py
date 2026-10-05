"""Offline, fenced export of one Python-owned control inventory domain.

This tool never opens a Go store. It operates on an explicitly named source
SQLite file, and is intended to run while its Python service is stopped. The
SQLite triggers make a subsequent Python write to the exported table fail even
if the old service is accidentally restarted. Import and final ownership
transfer require separate verification and are not implied by this export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_control_authority

EXPORT_SCHEMA = "python-control-inventory-export-v1"
TARGET_EXPORT_SCHEMA = "python-control-inventory-export-v2"
TARGET_HEALTH_SCHEMA = "python-backup-target-health-v1"
HANDBACK_SCHEMA = "control-inventory-handback-v1"
RECEIPT_SCHEMA = "python-control-inventory-handback-receipt-v1"
SOURCE_SCHEMA_VERSION = 8
_MAXIMUM_DOCUMENT_BYTES = 16 << 20
_TRANSFER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DOMAINS = {
    "policy": (
        "control_policies",
        "policy_id",
        "policyId",
        "policies",
        "policy_id, revision, payload_json, topology_generation, promotion_epoch, "
        "drain_generation, placement_generation, updated_at",
    ),
    "target": (
        "control_targets",
        "target_id",
        "targetId",
        "targets",
        "target_id, generation, payload_json, updated_at",
    ),
}
# The canonical sibling projection directory for each domain, relative to the
# standard `.backup-control` control directory.
_PROJECTION_DIR_NAMES = {"policy": ".backup-policies", "target": ".backup-targets"}
_FENCE_TABLE = """CREATE TABLE native_control_handoff_fences (
    domain TEXT PRIMARY KEY, transfer_id TEXT NOT NULL, authority_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL, created_at INTEGER NOT NULL
)"""
_FENCE_TRIGGERS = {
    "native_control_handoff_no_update": "CREATE TRIGGER native_control_handoff_no_update "
    "BEFORE UPDATE ON native_control_handoff_fences "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
    "native_control_handoff_no_delete": "CREATE TRIGGER native_control_handoff_no_delete "
    "BEFORE DELETE ON native_control_handoff_fences "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
    "native_control_handoff_no_replace": "CREATE TRIGGER native_control_handoff_no_replace "
    "BEFORE INSERT ON native_control_handoff_fences "
    "WHEN EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain=NEW.domain) "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
}
_REVOCATION_TABLE = """CREATE TABLE native_control_handoff_revocations (
    domain TEXT NOT NULL, transfer_id TEXT NOT NULL, manifest_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL, handback_digest TEXT NOT NULL, revoked_at INTEGER NOT NULL,
    UNIQUE(domain, transfer_id)
)"""
_REVOCATION_TRIGGERS = {
    "native_control_handoff_revocations_no_update": "CREATE TRIGGER native_control_handoff_revocations_no_update "
    "BEFORE UPDATE ON native_control_handoff_revocations "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDBACK_IMMUTABLE'); END",
    "native_control_handoff_revocations_no_delete": "CREATE TRIGGER native_control_handoff_revocations_no_delete "
    "BEFORE DELETE ON native_control_handoff_revocations "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDBACK_IMMUTABLE'); END",
    "native_control_handoff_revocations_no_replace": "CREATE TRIGGER native_control_handoff_revocations_no_replace "
    "BEFORE INSERT ON native_control_handoff_revocations "
    "WHEN EXISTS(SELECT 1 FROM native_control_handoff_revocations WHERE domain=NEW.domain AND transfer_id=NEW.transfer_id) "
    "BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDBACK_IMMUTABLE'); END",
}
_HANDBACK_FIELDS = frozenset({
    "schema", "domain", "transferId", "manifestDigest", "sourceDigest", "authorityGeneration",
    "authorityDigest", "rolledBackRecords", "rolledBackEvents", "cutoverRevision", "cutoverEpoch",
    "writerFence", "recordedAt", "handbackDigest",
})
# The transfer binds more than the exported inventory rows. The authority tip and
# its journals, the boot epoch the Go attestation compares, and the linked
# lifecycle intent and target receipt generations are all part of the proof, so a
# fence must freeze them too: while any fence is held the Python writer is
# mechanically denied on every table below. Tables with an empty mapping are
# global control state and are frozen by any fence; linked tables are frozen only
# for the row's own domain.
_LINKED_FENCE_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "control_authority_head": (),
    "control_authority_outbox": (),
    "control_authority_mutations": (),
    "control_boot_state": (),
    "lifecycle_intents": (("policy_id", "policy"), ("target_id", "target")),
    "target_receipt_mutations": (("target_id", "target"),),
}
_FENCE_OPERATIONS = ("INSERT", "UPDATE", "DELETE")


def _linked_fence_trigger_sql(table: str, operation: str, links: tuple[tuple[str, str], ...]) -> str:
    if not links:
        condition = "EXISTS(SELECT 1 FROM native_control_handoff_fences)"
    else:
        parts: list[str] = []
        for column, domain in links:
            fence = f"EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain='{domain}')"
            if operation in ("INSERT", "UPDATE"):
                parts.append(f"(NEW.{column} IS NOT NULL AND {fence})")
            if operation in ("UPDATE", "DELETE"):
                parts.append(f"(OLD.{column} IS NOT NULL AND {fence})")
        condition = " OR ".join(parts)
    return (
        f"CREATE TRIGGER native_control_fence_{table}_no_{operation.lower()} "
        f"BEFORE {operation} ON {table} WHEN {condition} "
        "BEGIN SELECT RAISE(ABORT,'PYTHON_CONTROL_SOURCE_FENCED'); END"
    )


def linked_fence_objects() -> dict[str, str]:
    objects: dict[str, str] = {}
    for table, links in _LINKED_FENCE_TABLES.items():
        for operation in _FENCE_OPERATIONS:
            objects[f"native_control_fence_{table}_no_{operation.lower()}"] = _linked_fence_trigger_sql(
                table, operation, links,
            )
    return objects


class HandoffError(RuntimeError):
    """The source cannot be exported without risking an unsafe ownership change."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


_HEALTH_FENCE_TABLE = """CREATE TABLE native_target_health_handoff_fence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), transfer_id TEXT NOT NULL,
    authority_digest TEXT NOT NULL, target_source_digest TEXT NOT NULL,
    health_digest TEXT NOT NULL, created_at INTEGER NOT NULL
)"""


def target_health_fence_objects() -> dict[str, str]:
    objects = {"native_target_health_handoff_fence": _HEALTH_FENCE_TABLE}
    for operation in _FENCE_OPERATIONS:
        objects[f"native_target_health_no_{operation.lower()}"] = (
            f"CREATE TRIGGER native_target_health_no_{operation.lower()} BEFORE {operation} ON backup_target_health "
            "WHEN EXISTS(SELECT 1 FROM native_target_health_handoff_fence) "
            "BEGIN SELECT RAISE(ABORT,'NATIVE_TARGET_HEALTH_HANDOFF'); END"
        )
        objects[f"native_target_health_fence_no_{operation.lower()}"] = (
            f"CREATE TRIGGER native_target_health_fence_no_{operation.lower()} BEFORE {operation} ON native_target_health_handoff_fence "
            "BEGIN SELECT RAISE(ABORT,'NATIVE_TARGET_HEALTH_HANDOFF_IMMUTABLE'); END"
        )
    return objects


def validate_target_health(binding: Any) -> None:
    if not isinstance(binding, dict) or set(binding) != {"schema", "sourceDigest", "rows"} or binding["schema"] != TARGET_HEALTH_SCHEMA:
        raise HandoffError("invalid target health binding")
    rows = binding["rows"]
    if not isinstance(rows, list) or _digest(rows) != binding["sourceDigest"]:
        raise HandoffError("target health digest differs")
    previous = ""
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"target_id", "status", "checked_at", "detail"}:
            raise HandoffError("invalid target health row")
        if (
            not isinstance(row["target_id"], str) or not _TRANSFER_ID.fullmatch(row["target_id"]) or row["target_id"] in {".", ".."}
            or row["target_id"] <= previous
            or not isinstance(row["status"], str) or not row["status"]
            or not isinstance(row["checked_at"], str) or not row["checked_at"]
            or (row["detail"] is not None and not isinstance(row["detail"], str))
        ):
            raise HandoffError("invalid target health row values or order")
        previous = row["target_id"]


def _target_health_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = connection.execute("PRAGMA table_info(backup_target_health)").fetchall()
    actual = [(row["name"], row["type"], row["notnull"], row["pk"]) for row in columns]
    if actual != [("target_id", "TEXT", 0, 1), ("status", "TEXT", 1, 0), ("checked_at", "TEXT", 1, 0), ("detail", "TEXT", 0, 0)]:
        raise HandoffError("scheduler target health schema differs")
    rows = connection.execute("SELECT target_id,status,checked_at,detail FROM backup_target_health ORDER BY target_id").fetchall()
    result = [dict(row) for row in rows]
    try:
        digest = _digest(result)
    except (TypeError, ValueError) as error:
        raise HandoffError("scheduler target health contains non-JSON SQLite values") from error
    validate_target_health({"schema": TARGET_HEALTH_SCHEMA, "sourceDigest": digest, "rows": result})
    return result


def _fence_target_health(
    source: Path, transfer_id: str, authority_digest: str, target_source_digest: str,
) -> dict[str, Any]:
    if not source.is_absolute() or not source.is_file() or source.is_symlink():
        raise HandoffError("scheduler source must be an explicit absolute regular file")
    connection = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='native_target_health_handoff_revocations'").fetchone():
            if connection.execute("SELECT 1 FROM native_target_health_handoff_revocations WHERE transfer_id=?", (transfer_id,)).fetchone():
                raise HandoffError("scheduler transfer was already revoked")
        rows = _target_health_rows(connection)
        digest = _digest(rows)
        objects = target_health_fence_objects()
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='native_target_health_handoff_fence'"
        ).fetchone() is not None
        _ensure_fence_object(connection, "native_target_health_handoff_fence", _HEALTH_FENCE_TABLE, require_existing=table_exists)
        existing = connection.execute("SELECT * FROM native_target_health_handoff_fence WHERE singleton=1").fetchone()
        for name, sql in objects.items():
            if name != "native_target_health_handoff_fence":
                # Install the immutable INSERT guard only after the first row.
                if existing is None and name == "native_target_health_fence_no_insert":
                    continue
                _ensure_fence_object(connection, name, sql, require_existing=existing is not None)
        if existing is None:
            if table_exists:
                raise HandoffError("scheduler fence lost its binding row")
            connection.execute(
                "INSERT INTO native_target_health_handoff_fence VALUES(1,?,?,?,?,strftime('%s','now'))",
                (transfer_id, authority_digest, target_source_digest, digest),
            )
            _ensure_fence_object(connection, "native_target_health_fence_no_insert", objects["native_target_health_fence_no_insert"], require_existing=False)
        elif (existing["transfer_id"], existing["authority_digest"], existing["target_source_digest"], existing["health_digest"]) != (
            transfer_id, authority_digest, target_source_digest, digest
        ):
            raise HandoffError("scheduler source was fenced for another transfer or changed")
        connection.execute("COMMIT")
        return {"schema": TARGET_HEALTH_SCHEMA, "sourceDigest": digest, "rows": rows}
    except sqlite3.DatabaseError as exc:
        raise HandoffError("scheduler source cannot be safely fenced") from exc
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


_HEALTH_REVOCATION_TABLE = """CREATE TABLE native_target_health_handoff_revocations (
    transfer_id TEXT PRIMARY KEY, manifest_digest TEXT NOT NULL,
    handback_digest TEXT NOT NULL, health_digest TEXT NOT NULL, revoked_at INTEGER NOT NULL
)"""


def _health_revocation_objects() -> dict[str, str]:
    objects = {"native_target_health_handoff_revocations": _HEALTH_REVOCATION_TABLE}
    for operation in _FENCE_OPERATIONS:
        name = f"native_target_health_revocations_no_{operation.lower()}"
        condition = "WHEN EXISTS(SELECT 1 FROM native_target_health_handoff_revocations WHERE transfer_id=NEW.transfer_id) " if operation == "INSERT" else ""
        objects[name] = (
            f"CREATE TRIGGER {name} BEFORE {operation} ON native_target_health_handoff_revocations {condition}"
            "BEGIN SELECT RAISE(ABORT,'NATIVE_TARGET_HEALTH_HANDBACK_IMMUTABLE'); END"
        )
    return objects


def _revoke_target_health(source: Path, manifest: dict[str, Any], handback: dict[str, Any]) -> None:
    if not source.is_absolute() or not source.is_file() or source.is_symlink():
        raise HandoffError("scheduler source must be an explicit absolute regular file")
    conn = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        objects = _health_revocation_objects()
        exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='native_target_health_handoff_revocations'").fetchone() is not None
        for name, statement in objects.items():
            _ensure_fence_object(conn, name, statement, require_existing=exists)
        completed = conn.execute(
            "SELECT manifest_digest,handback_digest,health_digest FROM native_target_health_handoff_revocations WHERE transfer_id=?",
            (manifest["transferId"],),
        ).fetchone()
        expected = (manifest["manifestDigest"], handback["handbackDigest"], manifest["targetHealth"]["sourceDigest"])
        if completed is not None:
            if tuple(completed) != expected:
                raise HandoffError("scheduler revocation conflicts with this handback")
            conn.execute("COMMIT")
            return
        for name, statement in target_health_fence_objects().items():
            _ensure_fence_object(conn, name, statement, require_existing=True)
        fence = conn.execute("SELECT transfer_id,authority_digest,target_source_digest,health_digest FROM native_target_health_handoff_fence WHERE singleton=1").fetchone()
        if fence is None or tuple(fence) != (
            manifest["transferId"], manifest["authorityDigest"], manifest["sourceDigest"], manifest["targetHealth"]["sourceDigest"]
        ) or _digest(_target_health_rows(conn)) != manifest["targetHealth"]["sourceDigest"]:
            raise HandoffError("scheduler source changed or belongs to another handback")
        conn.execute("INSERT INTO native_target_health_handoff_revocations VALUES(?,?,?,?,strftime('%s','now'))", (manifest["transferId"], *expected))
        for name in target_health_fence_objects():
            if name != "native_target_health_handoff_fence":
                conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE native_target_health_handoff_fence")
        conn.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        raise HandoffError("scheduler source cannot be safely handed back") from exc
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _existing_control_revocation(source: Path, manifest: dict[str, Any], handback: dict[str, Any]) -> int | None:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='native_control_handoff_revocations'").fetchone() is None:
            return None
        _ensure_fence_object(conn, "native_control_handoff_revocations", _REVOCATION_TABLE, require_existing=True)
        for name, statement in _REVOCATION_TRIGGERS.items():
            _ensure_fence_object(conn, name, statement, require_existing=True)
        row = conn.execute(
            "SELECT manifest_digest,source_digest,handback_digest,revoked_at FROM native_control_handoff_revocations WHERE domain=? AND transfer_id=?",
            (manifest["domain"], manifest["transferId"]),
        ).fetchone()
        if row is None:
            return None
        if (row["manifest_digest"], row["source_digest"], row["handback_digest"]) != (manifest["manifestDigest"], manifest["sourceDigest"], handback["handbackDigest"]):
            raise HandoffError("control revocation conflicts with this handback")
        return int(row["revoked_at"])


def _publish_handback_receipt(manifest: dict[str, Any], handback: dict[str, Any], revoked_at: int, receipt: Path) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA, "domain": manifest["domain"], "transferId": manifest["transferId"],
        "manifestDigest": manifest["manifestDigest"], "sourceDigest": manifest["sourceDigest"],
        "authorityGeneration": manifest["authorityGeneration"], "authorityDigest": manifest["authorityDigest"],
        "handbackDigest": handback["handbackDigest"], "rolledBackRecords": handback["rolledBackRecords"],
        "rolledBackEvents": handback["rolledBackEvents"], "revokedAt": revoked_at,
    }
    document["receiptDigest"] = _digest(document)
    _publish(receipt, _canonical(document) + b"\n")
    return document


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise HandoffError("non-finite JSON number")


def _read_canonical_document(path: Path, label: str) -> dict[str, Any]:
    """Read one newline-terminated canonical JSON document published by a peer runtime."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HandoffError(f"{label} cannot be read") from exc
    if not raw.endswith(b"\n") or len(raw) > _MAXIMUM_DOCUMENT_BYTES:
        raise HandoffError(f"invalid {label}")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise HandoffError(f"invalid {label}") from exc
    if not isinstance(document, dict) or _canonical(document) + b"\n" != raw:
        raise HandoffError(f"{label} is not canonical JSON")
    return document


def _read_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_canonical_document(path, "export manifest")
    fields = {
        "schema", "domain", "transferId", "sourceSchemaVersion", "authorityGeneration",
        "authorityDigest", "sourceDigest", "rows", "legacyProjection", "manifestDigest",
    }
    if manifest.get("schema") == TARGET_EXPORT_SCHEMA:
        fields.add("targetHealth")
        if manifest.get("domain") != "target":
            raise HandoffError("health export must belong to the target domain")
        validate_target_health(manifest.get("targetHealth"))
    if set(manifest) != fields:
        raise HandoffError("unexpected export manifest fields")
    if manifest["schema"] not in (EXPORT_SCHEMA, TARGET_EXPORT_SCHEMA) or manifest["domain"] not in _DOMAINS or manifest["sourceSchemaVersion"] != SOURCE_SCHEMA_VERSION:
        raise HandoffError("invalid export manifest")
    projection = manifest["legacyProjection"]
    if (
        not isinstance(projection, dict) or set(projection) != {"fileCount", "digest"}
        or not isinstance(projection["fileCount"], int) or isinstance(projection["fileCount"], bool) or projection["fileCount"] < 0
        or (projection["digest"] is None and projection["fileCount"] != 0)
        or (projection["digest"] is not None and not (isinstance(projection["digest"], str) and _SHA256.fullmatch(projection["digest"])))
    ):
        raise HandoffError("invalid legacy projection binding")
    rows = manifest["rows"]
    if not isinstance(rows, list) or _digest(rows) != manifest["sourceDigest"]:
        raise HandoffError("export manifest rows do not match their digest")
    unsigned = {key: value for key, value in manifest.items() if key != "manifestDigest"}
    if _digest(unsigned) != manifest["manifestDigest"]:
        raise HandoffError("export manifest digest does not match")
    return manifest


def _read_handback(path: Path) -> dict[str, Any]:
    handback = _read_canonical_document(path, "handback document")
    if set(handback) != _HANDBACK_FIELDS:
        raise HandoffError("unexpected handback document fields")
    unsigned = {key: value for key, value in handback.items() if key != "handbackDigest"}
    if handback["schema"] != HANDBACK_SCHEMA or not isinstance(handback["handbackDigest"], str) or \
            _digest(unsigned) != handback["handbackDigest"]:
        raise HandoffError("handback digest does not match")
    return handback


def _read_checkpoint(path: Path) -> dict[str, Any]:
    try:
        checkpoint = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite,
        )
        if not isinstance(checkpoint, dict):
            raise HandoffError("checkpoint must be an object")
        backup_control_authority.verify_authority_checkpoint_integrity(checkpoint)
        return checkpoint
    except (OSError, ValueError, TypeError, backup_control_authority.AppError) as exc:
        raise HandoffError("invalid authority checkpoint") from exc


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in {"credential", "credentials"} or (
                normalized not in {"credentialreference", "credentialprovidertype", "credentialprovider"}
                and any(part in normalized for part in (
                    "secret", "password", "passwd", "privatekey", "apikey", "accesskey", "token", "bearer", "oauth", "ageidentity", "identity",
                ))
            ):
                return True
            if _contains_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _source_rows(conn: sqlite3.Connection, domain: str) -> list[dict[str, Any]]:
    table, id_column, id_field, _, columns = _DOMAINS[domain]
    rows: list[dict[str, Any]] = []
    for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY {id_column}"):
        payload_text = str(row["payload_json"])
        try:
            payload = json.loads(payload_text, object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite)
        except (ValueError, TypeError) as exc:
            raise HandoffError("invalid source payload") from exc
        if not isinstance(payload, dict) or payload.get(id_field) != row[id_column] or _contains_secret(payload):
            raise HandoffError("source payload identity or key custody is not importable")
        encoded_lower = payload_text.casefold()
        if "age-secret-key-" in encoded_lower or "-----begin" in encoded_lower:
            raise HandoffError("source contains private key material")
        if domain == "policy" and int(row["revision"]) != payload.get("policyRevision"):
            raise HandoffError("policy revision differs from source payload")
        if domain == "target" and (
            int(row["generation"]) < 1 or int(row["generation"]) != payload.get("topologyGeneration")
        ):
            raise HandoffError("invalid target generation")
        rows.append({key: row[key] for key in row.keys()})
    return rows


def _bound_projection_directory(source_db: Path, domain: str, projection_dir: Path | None) -> Path | None:
    """The legacy projection directory this export binds, or ``None`` for none.

    The standard `.backup-control` layout is inferred so an operator cannot
    forget it; a nonstandard source path must name the directory explicitly.
    """
    if projection_dir is not None:
        if not projection_dir.is_absolute():
            raise HandoffError("projection directory must be absolute")
        return projection_dir
    if source_db.parent.name != ".backup-control":
        raise HandoffError("nonstandard source path requires an explicit projection directory")
    return source_db.parent.parent / _PROJECTION_DIR_NAMES[domain]


def _bind_legacy_projection(
    source_db: Path, domain: str, rows: list[dict[str, Any]], projection_dir: Path | None,
) -> dict[str, Any]:
    """Refuse an unreconciled projection, then bind its exact directory state.

    Two separate guarantees are produced here. The reconcile check catches
    source rows the Python list route would adopt *after* export. The returned
    digest is what the Go transfer rechecks later: the SQLite fence cannot
    reach a sibling JSON directory, so the directory state has to travel with
    the export manifest and be re-derived before the import is trusted.
    """
    directory = _bound_projection_directory(source_db, domain, projection_dir)
    if directory is None:
        return {"fileCount": 0, "digest": None}
    explicit = projection_dir is not None
    if directory.is_symlink():
        raise HandoffError("legacy projection directory is a symlink")
    if not directory.exists():
        if explicit:
            raise HandoffError("explicit legacy projection directory does not exist")
        return {"fileCount": 0, "digest": None}
    if not directory.is_dir():
        raise HandoffError("legacy projection path is not a directory")
    _, id_column, id_field, _, _ = _DOMAINS[domain]
    imported_ids = {str(row[id_column]) for row in rows}
    try:
        candidates = sorted(directory.glob("*.json"), key=lambda item: item.name.encode("utf-8"))
    except OSError as exc:
        raise HandoffError("legacy projection directory cannot be read") from exc
    files: list[dict[str, Any]] = []
    for path in candidates:
        if domain == "target" and path.name.endswith(".checkpoint.json"):
            continue
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAXIMUM_DOCUMENT_BYTES:
                raise HandoffError("legacy projection is not a bounded regular file")
            content = path.read_bytes()
            document = json.loads(content, object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite)
        except (OSError, ValueError, TypeError) as exc:
            raise HandoffError("legacy projection is unreadable or malformed") from exc
        if not isinstance(document, dict) or document.get(id_field) != path.stem or path.stem not in imported_ids:
            raise HandoffError("legacy projection is not reconciled with source control inventory")
        files.append({"name": path.name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    return {"fileCount": len(files), "digest": _digest(files)}


def _check_source(conn: sqlite3.Connection, checkpoint: dict[str, Any], domain: str, rows: list[dict[str, Any]]) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SOURCE_SCHEMA_VERSION or checkpoint.get("controlSchemaVersion") != version:
        raise HandoffError("unsupported source control schema")
    table, _, _, _, columns = _DOMAINS[domain]
    expected_columns = [column.strip() for column in columns.split(",")]
    actual_columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    if actual_columns != expected_columns:
        raise HandoffError("source inventory table schema drift")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise HandoffError("source SQLite integrity check failed")
    head = conn.execute(
        "SELECT authority_generation, authority_digest, payload_digest, previous_digest "
        "FROM control_authority_head WHERE id=1"
    ).fetchone()
    if head is None or (
        head["authority_generation"] != checkpoint.get("authorityGeneration")
        or head["authority_digest"] != checkpoint.get("digest")
        or head["payload_digest"] != checkpoint.get("payloadDigest")
        or head["previous_digest"] != checkpoint.get("previousDigest")
    ):
        raise HandoffError("checkpoint is not the live source authority head")
    boot = conn.execute("SELECT boot_epoch, recovery_state FROM control_boot_state WHERE id=1").fetchone()
    if boot is None or boot["recovery_state"] != "active" or boot["boot_epoch"] != checkpoint.get("controlBootEpoch"):
        raise HandoffError("source control recovery state is not active")
    if conn.execute("SELECT COUNT(*) FROM control_authority_outbox WHERE state!='durable'").fetchone()[0]:
        raise HandoffError("unsettled authority outbox")
    if conn.execute(
        "SELECT COUNT(*) FROM control_authority_mutations WHERE state NOT IN ('durable','superseded')"
    ).fetchone()[0]:
        raise HandoffError("unsettled authority mutation")
    linked_column = "policy_id" if domain == "policy" else "target_id"
    if conn.execute(
        f"SELECT COUNT(*) FROM lifecycle_intents WHERE {linked_column} IS NOT NULL "
        "AND phase NOT IN ('completed','cancelled')"
    ).fetchone()[0]:
        raise HandoffError("unsettled source lifecycle intent")
    _, _, _, checkpoint_field, _ = _DOMAINS[domain]
    sanitize = (
        backup_control_authority.sanitize_policy_for_authority
        if domain == "policy" else backup_control_authority.sanitize_target_for_authority
    )
    raw_payloads = [
        json.loads(str(row["payload_json"]), object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite)
        for row in rows
    ]
    projected = [sanitize(payload) for payload in raw_payloads]
    if any(_canonical(payload) != _canonical(cleaned) for payload, cleaned in zip(raw_payloads, projected, strict=True)):
        raise HandoffError("source payload has fields absent from the authority checkpoint")
    expected = checkpoint.get(checkpoint_field)
    id_field = _DOMAINS[domain][2]
    if not isinstance(expected, list) or any(not isinstance(item, dict) or id_field not in item for item in expected):
        raise HandoffError("invalid checkpoint inventory")
    if _canonical(projected) != _canonical(sorted(expected, key=lambda item: item[id_field])):
        raise HandoffError("source inventory differs from authority checkpoint")
    if domain == "policy":
        for column, field in (
            ("promotion_epoch", "promotionEpochs"),
            ("drain_generation", "drainGenerations"),
            ("placement_generation", "placementGenerations"),
        ):
            expected_generations = checkpoint.get(field)
            actual_generations = {str(row["policy_id"]): int(row[column]) for row in rows}
            if not isinstance(expected_generations, dict) or actual_generations != expected_generations:
                raise HandoffError("source policy generations differ from authority checkpoint")
    elif conn.execute("SELECT COUNT(*) FROM target_receipt_mutations").fetchone()[0]:
        raise HandoffError("target receipt mutation generations need a separate native import")


def _fence_trigger_sql(domain: str, operation: str) -> str:
    table = _DOMAINS[domain][0]
    return (
        f"CREATE TRIGGER native_control_{domain}_no_{operation.lower()} "
        f"BEFORE {operation} ON {table} "
        f"WHEN EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain='{domain}') "
        "BEGIN SELECT RAISE(ABORT,'PYTHON_CONTROL_SOURCE_FENCED'); END"
    )


def _ensure_fence_object(conn: sqlite3.Connection, name: str, statement: str, *, require_existing: bool) -> None:
    existing = conn.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
    if existing is None:
        if require_existing:
            raise HandoffError("source fence schema is missing after handoff")
        conn.execute(statement)
    elif existing[0] != statement:
        raise HandoffError("foreign or altered source fence schema")


def _publish(output: Path, document: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.is_symlink() or output.read_bytes() != document:
            raise HandoffError("different export already exists")
        return
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".pending-", dir=output.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    except FileExistsError as exc:
        if output.is_symlink() or not output.exists() or output.read_bytes() != document:
            raise HandoffError("different export already exists") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_and_fence(
    source_db: Path, checkpoint_path: Path, output: Path, domain: str, transfer_id: str,
    *, projection_dir: Path | None = None, scheduler_db: Path | None = None,
) -> dict[str, Any]:
    """Fence a stopped Python source and publish a recoverable offline export."""
    if domain not in _DOMAINS or not _TRANSFER_ID.fullmatch(transfer_id):
        raise HandoffError("invalid domain or transfer ID")
    if scheduler_db is not None and domain != "target":
        raise HandoffError("scheduler health can only be transferred with targets")
    if scheduler_db is not None and output.resolve() == scheduler_db.resolve():
        raise HandoffError("export output cannot replace the scheduler source")
    if not source_db.is_file() or source_db.is_symlink() or output.resolve() == source_db.resolve():
        raise HandoffError("source database must be an explicit regular file")
    source_db = source_db.resolve(strict=True)
    checkpoint = _read_checkpoint(checkpoint_path)
    connection = sqlite3.connect(source_db.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = _source_rows(connection, domain)
            _check_source(connection, checkpoint, domain, rows)
            projection = _bind_legacy_projection(source_db, domain, rows, projection_dir)
            source_digest = _digest(rows)
            health = None
            _ensure_fence_object(connection, "native_control_handoff_fences", _FENCE_TABLE, require_existing=False)
            existing = connection.execute(
                "SELECT transfer_id, authority_digest, source_digest FROM native_control_handoff_fences WHERE domain=?",
                (domain,),
            ).fetchone()
            for name, statement in _FENCE_TRIGGERS.items():
                _ensure_fence_object(connection, name, statement, require_existing=existing is not None)
            for operation in ("INSERT", "UPDATE", "DELETE"):
                _ensure_fence_object(
                    connection,
                    f"native_control_{domain}_no_{operation.lower()}",
                    _fence_trigger_sql(domain, operation),
                    require_existing=existing is not None,
                )
            for name, statement in linked_fence_objects().items():
                _ensure_fence_object(connection, name, statement, require_existing=existing is not None)
            if existing is None:
                connection.execute(
                    "INSERT INTO native_control_handoff_fences VALUES(?,?,?,?,strftime('%s','now'))",
                    (domain, transfer_id, checkpoint["digest"], source_digest),
                )
            elif (existing["transfer_id"], existing["authority_digest"], existing["source_digest"]) != (
                transfer_id, checkpoint["digest"], source_digest
            ):
                raise HandoffError("source was fenced for a different handoff or changed after fencing")
            if scheduler_db is not None:
                health = _fence_target_health(scheduler_db, transfer_id, checkpoint["digest"], source_digest)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.DatabaseError as exc:
        raise HandoffError("source database cannot be safely exported") from exc
    finally:
        connection.close()
    manifest: dict[str, Any] = {
        "schema": TARGET_EXPORT_SCHEMA if scheduler_db is not None else EXPORT_SCHEMA,
        "domain": domain,
        "transferId": transfer_id,
        "sourceSchemaVersion": SOURCE_SCHEMA_VERSION,
        "authorityGeneration": checkpoint["authorityGeneration"],
        "authorityDigest": checkpoint["digest"],
        "sourceDigest": source_digest,
        "legacyProjection": projection,
        "rows": rows,
    }
    if health is not None:
        manifest["targetHealth"] = health
    manifest["manifestDigest"] = _digest(manifest)
    _publish(output, _canonical(manifest) + b"\n")
    return manifest


def revoke_handoff(
    source_db: Path, manifest_path: Path, handback_path: Path, receipt: Path, transfer_id: str,
    *, scheduler_db: Path | None = None,
) -> dict[str, Any]:
    """Lift the source fence after Go attests that it abandoned the transfer.

    This is the reverse of :func:`export_and_fence`. It refuses unless the Go
    handback document binds the exact manifest, source, authority tip and
    transfer that this source was fenced for, and unless the fenced rows are
    still byte-identical to the export. The revocation is recorded in an
    append-only table before the fence is lifted, so the transfer history
    survives even though the writer can write again.
    """
    if not _TRANSFER_ID.fullmatch(transfer_id):
        raise HandoffError("invalid transfer ID")
    if not source_db.is_file() or source_db.is_symlink() or receipt.resolve() == source_db.resolve():
        raise HandoffError("source database must be an explicit regular file")
    source_db = source_db.resolve(strict=True)
    manifest = _read_manifest(manifest_path)
    domain = manifest["domain"]
    if manifest["transferId"] != transfer_id:
        raise HandoffError("export manifest does not belong to this transfer")
    handback = _read_handback(handback_path)
    if (handback["domain"], handback["transferId"]) != (domain, transfer_id) or \
            handback["manifestDigest"] != manifest["manifestDigest"] or \
            handback["sourceDigest"] != manifest["sourceDigest"] or \
            handback["authorityDigest"] != manifest["authorityDigest"] or \
            handback["authorityGeneration"] != manifest["authorityGeneration"]:
        raise HandoffError("handback document does not match the fenced export")
    if manifest["schema"] == TARGET_EXPORT_SCHEMA:
        if scheduler_db is None:
            if source_db.parent.name != ".backup-control":
                raise HandoffError("a nonstandard source requires an explicit scheduler source")
            scheduler_db = source_db.parent.parent / ".backup-scheduler" / "scheduler.db"
    elif scheduler_db is not None:
        raise HandoffError("v1 handback does not bind scheduler health")
    inputs = [source_db, manifest_path, handback_path]
    if scheduler_db is not None:
        inputs.append(scheduler_db)
    if receipt.resolve() in [path.resolve() for path in inputs]:
        raise HandoffError("receipt output cannot replace a handback input")
    completed = _existing_control_revocation(source_db, manifest, handback) if manifest["schema"] == TARGET_EXPORT_SCHEMA else None
    if completed is not None:
        if scheduler_db is not None:
            _revoke_target_health(scheduler_db, manifest, handback)
        return _publish_handback_receipt(manifest, handback, completed, receipt)
    connection = sqlite3.connect(source_db.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            fence = connection.execute(
                "SELECT transfer_id, authority_digest, source_digest FROM native_control_handoff_fences WHERE domain=?",
                (domain,),
            ).fetchone()
            if fence is None or (fence["transfer_id"], fence["authority_digest"], fence["source_digest"]) != (
                transfer_id, manifest["authorityDigest"], manifest["sourceDigest"]
            ):
                raise HandoffError("source is not fenced for this handback")
            if _digest(_source_rows(connection, domain)) != manifest["sourceDigest"]:
                raise HandoffError("source changed after it was fenced")
            if scheduler_db is not None:
                _revoke_target_health(scheduler_db, manifest, handback)
            revocations = connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='native_control_handoff_revocations'"
            ).fetchone()
            _ensure_fence_object(
                connection, "native_control_handoff_revocations", _REVOCATION_TABLE,
                require_existing=revocations is not None,
            )
            for name, statement in _REVOCATION_TRIGGERS.items():
                _ensure_fence_object(connection, name, statement, require_existing=revocations is not None)
            if connection.execute(
                "SELECT COUNT(*) FROM native_control_handoff_revocations WHERE domain=? AND transfer_id=?",
                (domain, transfer_id),
            ).fetchone()[0]:
                raise HandoffError("transfer was already revoked")
            connection.execute(
                "INSERT INTO native_control_handoff_revocations VALUES(?,?,?,?,?,strftime('%s','now'))",
                (domain, transfer_id, manifest["manifestDigest"], manifest["sourceDigest"], handback["handbackDigest"]),
            )
            revoked_at = int(connection.execute(
                "SELECT revoked_at FROM native_control_handoff_revocations WHERE domain=? AND transfer_id=?",
                (domain, transfer_id),
            ).fetchone()[0])
            # The fence-table guard triggers have to be lifted to remove this
            # domain's own row. The linked fence objects are global: they stay
            # until the last fence is lifted, so one domain's revocation cannot
            # unfreeze state another held transfer still depends on.
            for name in _FENCE_TRIGGERS:
                connection.execute(f"DROP TRIGGER {name}")
            for operation in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(f"DROP TRIGGER native_control_{domain}_no_{operation.lower()}")
            connection.execute("DELETE FROM native_control_handoff_fences WHERE domain=?", (domain,))
            if connection.execute("SELECT COUNT(*) FROM native_control_handoff_fences").fetchone()[0]:
                for name, statement in _FENCE_TRIGGERS.items():
                    _ensure_fence_object(connection, name, statement, require_existing=False)
            else:
                for name in linked_fence_objects():
                    connection.execute(f"DROP TRIGGER {name}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.DatabaseError as exc:
        raise HandoffError("source database cannot be safely handed back") from exc
    finally:
        connection.close()
    return _publish_handback_receipt(manifest, handback, revoked_at, receipt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--transfer-id", required=True)
    parser.add_argument("--checkpoint", type=Path, help="authority checkpoint; required to fence an export")
    parser.add_argument("--output", type=Path, help="export manifest path; required to fence an export")
    parser.add_argument("--domain", choices=sorted(_DOMAINS), help="exported domain; required to fence an export")
    parser.add_argument("--projection-dir", type=Path, help="legacy .backup-policies or .backup-targets directory for a nonstandard source path")
    parser.add_argument("--scheduler-db", type=Path, help="explicit scheduler SQLite; binds target health in a v2 target export")
    parser.add_argument("--rollback", action="store_true", help="lift the fence after Go attests the handback")
    parser.add_argument("--manifest", type=Path, help="fenced export manifest; required with --rollback")
    parser.add_argument("--handback", type=Path, help="Go control-inventory-handback document; required with --rollback")
    parser.add_argument("--receipt", type=Path, help="where to publish the revocation receipt; required with --rollback")
    args = parser.parse_args()
    if args.rollback:
        missing = [name for name in ("manifest", "handback", "receipt") if getattr(args, name) is None]
        if missing:
            parser.error("--rollback requires " + ", ".join("--" + name for name in missing))
        document = revoke_handoff(args.source_db, args.manifest, args.handback, args.receipt, args.transfer_id, scheduler_db=args.scheduler_db)
        print(json.dumps({
            "domain": document["domain"], "receiptDigest": document["receiptDigest"], "output": str(args.receipt),
        }))
        return
    missing = [name for name in ("checkpoint", "output", "domain") if getattr(args, name) is None]
    if missing:
        parser.error("an export requires " + ", ".join("--" + name for name in missing))
    if args.projection_dir is None and args.source_db.parent.name != ".backup-control":
        parser.error("a nonstandard source path requires --projection-dir to check legacy projections")
    manifest = export_and_fence(
        args.source_db, args.checkpoint, args.output, args.domain, args.transfer_id,
        projection_dir=args.projection_dir,
        scheduler_db=args.scheduler_db,
    )
    print(json.dumps({"domain": manifest["domain"], "manifestDigest": manifest["manifestDigest"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
