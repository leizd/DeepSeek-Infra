"""Durable Copy Retirement and Physical Garbage Collection (4.5.7).

Provides formal CopyRetirementJob lifecycle, post-delete topology simulation,
active hold checks, and reference-counted physical GC that never deletes
ciphertext shared by multiple retained receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_publish,
)

RETIREMENT_DIR = config.ROOT / ".backup-retirements"
RETIREMENT_DB = RETIREMENT_DIR / "retirements.sqlite3"
RETIREMENTS_DIR = RETIREMENT_DIR
RETIREMENTS_DB = RETIREMENT_DB

RETIREMENT_TERMINAL_PHASES = frozenset({"reclaimed", "rejected", "failed", "superseded", "cancelled"})
RETIREMENT_MARKER_SCHEMA_VERSION = 1


class GcReferenceScanIndeterminate(Exception):
    """Raised when retained-ref scan cannot determine safety for destructive GC."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def retirement_marker_key(policy_id: str, backup_id: str) -> str:
    return f"retirements/{policy_id}/{backup_id}.json"


def _marker_hash(marker: dict[str, Any]) -> str:
    body = {key: value for key, value in marker.items() if key != "markerHash"}
    return hashlib.sha256(_stable_json(body)).hexdigest()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    RETIREMENT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(RETIREMENT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS copy_retirement_jobs (
            job_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            reason TEXT,
            bytes_reclaimed INTEGER DEFAULT 0,
            sim_metadata TEXT
        )
        """
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(copy_retirement_jobs)").fetchall()}
    if "reason" not in columns:
        conn.execute("ALTER TABLE copy_retirement_jobs ADD COLUMN reason TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retirement_phase ON copy_retirement_jobs(phase)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retirement_target ON copy_retirement_jobs(target_id, policy_id)")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_copy_retirement_job(
    policy_id: str,
    backup_id: str,
    target_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a durable CopyRetirementJob in requested phase."""
    job_id = f"retire_{secrets.token_hex(8)}"
    now = _utc_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO copy_retirement_jobs(
                job_id, policy_id, backup_id, target_id, phase, created_at,
                updated_at, error, reason, bytes_reclaimed, sim_metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, '{}')
            """,
            (job_id, policy_id, backup_id, target_id, "requested", now, now, reason),
        )
    return get_copy_retirement_job(job_id) or {}


def get_copy_retirement_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM copy_retirement_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return {
            "jobId": row["job_id"],
            "policyId": row["policy_id"],
            "backupId": row["backup_id"],
            "targetId": row["target_id"],
            "phase": row["phase"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "error": row["error"],
            "reason": row["reason"],
            "bytesReclaimed": row["bytes_reclaimed"],
            "simMetadata": json.loads(row["sim_metadata"] or "{}"),
        }


def list_copy_retirement_jobs(
    *,
    policy_id: str | None = None,
    target_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List copy retirement jobs matching optional filters."""
    with _connect() as conn:
        query = "SELECT * FROM copy_retirement_jobs WHERE 1=1"
        params: list[Any] = []
        if policy_id:
            query += " AND policy_id = ?"
            params.append(policy_id)
        if target_id:
            query += " AND target_id = ?"
            params.append(target_id)
        if phase:
            query += " AND phase = ?"
            params.append(phase)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "jobId": row["job_id"],
                "policyId": row["policy_id"],
                "backupId": row["backup_id"],
                "targetId": row["target_id"],
                "phase": row["phase"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "error": row["error"],
                "reason": row["reason"],
                "bytesReclaimed": row["bytes_reclaimed"],
                "simMetadata": json.loads(row["sim_metadata"] or "{}"),
            }
            for row in rows
        ]


def _update_job_phase(
    job_id: str,
    phase: str,
    *,
    error: str | None = None,
    bytes_reclaimed: int | None = None,
    sim_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    with _connect() as conn:
        updates = ["phase = ?", "updated_at = ?"]
        params: list[Any] = [phase, now]
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if bytes_reclaimed is not None:
            updates.append("bytes_reclaimed = ?")
            params.append(bytes_reclaimed)
        if sim_metadata is not None:
            updates.append("sim_metadata = ?")
            params.append(json.dumps(sim_metadata, ensure_ascii=False, sort_keys=True))
        params.append(job_id)
        conn.execute(f"UPDATE copy_retirement_jobs SET {', '.join(updates)} WHERE job_id = ?", params)
    return get_copy_retirement_job(job_id) or {}


def cancel_copy_retirement_job(job_id: str, *, reason: str = "operator-cancelled") -> dict[str, Any]:
    """Cancel a pending copy retirement job."""
    job = get_copy_retirement_job(job_id)
    if not job:
        raise AppError(f"Retirement job {job_id} not found", code=ErrorCode.NOT_FOUND, status=404)
    if job["phase"] in RETIREMENT_TERMINAL_PHASES:
        return job
    return _update_job_phase(job_id, "cancelled", error=reason)


def _read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _filesystem_receipt_path(root: Path, policy_id: str, backup_id: str) -> Path | None:
    candidates = (
        root / "receipts" / f"{backup_id}.json",
        root / "receipts" / f"{backup_id}.receipt.json",
        root / "receipts" / policy_id / f"{backup_id}.receipt.json",
        root / "receipts" / policy_id / f"{backup_id}.json",
    )
    return next((path for path in candidates if path.is_file()), None)


def _filesystem_commit_path(root: Path, policy_id: str, backup_id: str, receipt: dict[str, Any]) -> Path | None:
    schedule_slot = str(receipt.get("scheduleSlot") or "")
    if schedule_slot:
        candidate = backup_publish.find_commit_marker_path(root, policy_id, schedule_slot)
        if candidate is not None:
            return candidate
    candidates = (
        root / "commits" / policy_id / f"{backup_id}.json",
        root / "commits" / policy_id / f"{backup_id}.commit.json",
    )
    direct = next((path for path in candidates if path.is_file()), None)
    if direct is not None:
        return direct
    commits_dir = root / "commits" / policy_id
    if commits_dir.is_dir():
        for path in sorted(commits_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and str(value.get("backupId") or "") == backup_id:
                return path
    return None


def _read_formal_metadata(target: Any, policy_id: str, backup_id: str) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Read the immutable Receipt and Commit that authorize retirement."""
    receipt_bytes: bytes | None = None
    commit: dict[str, Any] | None = None
    if target.root is not None:
        receipt_path = _filesystem_receipt_path(target.root, policy_id, backup_id)
        if receipt_path is not None:
            receipt_bytes = receipt_path.read_bytes()
        receipt = _read_json_bytes(receipt_bytes)
        if receipt is not None:
            commit_path = _filesystem_commit_path(target.root, policy_id, backup_id, receipt)
            if commit_path is not None:
                commit = _read_json_bytes(commit_path.read_bytes())
    elif target.store is not None:
        from deepseek_infra.infra.workspace.backup_target_store import commit_marker_keys, read_json

        receipt_bytes = target.store.get_bytes(f"receipts/{backup_id}.json")
        receipt = _read_json_bytes(receipt_bytes)
        if receipt is not None:
            schedule_slot = str(receipt.get("scheduleSlot") or "")
            if schedule_slot:
                for key in commit_marker_keys(policy_id, schedule_slot):
                    candidate = read_json(target.store, key)
                    if isinstance(candidate, dict) and str(candidate.get("backupId") or "") == backup_id:
                        commit = candidate
                        break
            if commit is None:
                cursor: str | None = None
                while True:
                    page = target.store.list_objects(f"commits/{policy_id}/", cursor=cursor, limit=200)
                    for meta in page.objects:
                        candidate = read_json(target.store, meta.key)
                        if isinstance(candidate, dict) and str(candidate.get("backupId") or "") == backup_id:
                            commit = candidate
                            break
                    if commit is not None or page.cursor is None:
                        break
                    cursor = page.cursor
    else:  # pragma: no cover - ResolvedTarget always has one backend
        receipt = None

    receipt = _read_json_bytes(receipt_bytes)
    if receipt_bytes is None or receipt is None:
        raise AppError("retirement-formal-receipt-missing-or-invalid", code=ErrorCode.INVALID_REQUEST, status=409)
    if commit is None or not backup_publish.commit_marker_valid(commit):
        raise AppError("retirement-formal-commit-missing-or-invalid", code=ErrorCode.INVALID_REQUEST, status=409)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    if str(commit.get("receiptDigest") or "") != receipt_digest:
        raise AppError("retirement-receipt-commit-binding-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(receipt.get("backupId") or "") != backup_id or str(commit.get("backupId") or "") != backup_id:
        raise AppError("retirement-backup-binding-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(receipt.get("policyId") or "") != policy_id or str(commit.get("policyId") or "") != policy_id:
        raise AppError("retirement-policy-binding-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(receipt.get("targetId") or target.target_id) != target.target_id:
        raise AppError("retirement-target-binding-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    receipt_object_set = str(receipt.get("objectSetDigest") or "")
    commit_object_set = str(commit.get("objectSetDigest") or "")
    if receipt_object_set and commit_object_set != receipt_object_set:
        raise AppError("retirement-object-set-binding-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    return receipt_bytes, receipt, commit


def build_retirement_marker(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    receipt_bytes: bytes,
    receipt: dict[str, Any],
    commit: dict[str, Any],
    retirement_job_id: str,
    reason: str,
    retired_at: str | None = None,
) -> dict[str, Any]:
    marker = {
        "schemaVersion": RETIREMENT_MARKER_SCHEMA_VERSION,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "commitHash": str(commit.get("commitHash") or ""),
        "objectSetDigest": str(receipt.get("objectSetDigest") or commit.get("objectSetDigest") or "") or None,
        "retirementJobId": retirement_job_id,
        "reason": reason,
        "retiredAt": retired_at or _utc_iso(),
    }
    marker["markerHash"] = _marker_hash(marker)
    return marker


def retirement_marker_valid(
    marker: dict[str, Any],
    *,
    receipt_bytes: bytes,
    commit: dict[str, Any],
) -> bool:
    receipt = _read_json_bytes(receipt_bytes)
    if receipt is None or int(marker.get("schemaVersion") or 0) != RETIREMENT_MARKER_SCHEMA_VERSION:
        return False
    if str(marker.get("markerHash") or "") != _marker_hash(marker):
        return False
    if not backup_publish.commit_marker_valid(commit):
        return False
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    return (
        str(marker.get("targetId") or "") == str(receipt.get("targetId") or marker.get("targetId") or "")
        and str(marker.get("policyId") or "") == str(receipt.get("policyId") or "") == str(commit.get("policyId") or "")
        and str(marker.get("backupId") or "") == str(receipt.get("backupId") or "") == str(commit.get("backupId") or "")
        and str(marker.get("receiptDigest") or "") == receipt_digest == str(commit.get("receiptDigest") or "")
        and str(marker.get("commitHash") or "") == str(commit.get("commitHash") or "")
        and str(marker.get("objectSetDigest") or "")
        == str(receipt.get("objectSetDigest") or commit.get("objectSetDigest") or "")
    )


def _write_retirement_marker(target: Any, marker: dict[str, Any], *, receipt_bytes: bytes, commit: dict[str, Any]) -> None:
    key = retirement_marker_key(str(marker["policyId"]), str(marker["backupId"]))
    content = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    existing: dict[str, Any] | None = None
    if target.root is not None:
        path = target.root.joinpath(*key.split("/"))
        if path.is_file():
            existing = _read_json_bytes(path.read_bytes())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                existing = _read_json_bytes(path.read_bytes())
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                return
    elif target.store is not None:
        from deepseek_infra.infra.workspace.backup_target_store import put_json_if_absent, read_json

        existing = read_json(target.store, key)
        if existing is None:
            put_json_if_absent(target.store, key, marker)
            return
    if existing is None or not retirement_marker_valid(existing, receipt_bytes=receipt_bytes, commit=commit):
        raise AppError("retirement-marker-conflict-or-invalid", code=ErrorCode.INVALID_REQUEST, status=409)
    stable_fields = ("targetId", "policyId", "backupId", "receiptDigest", "commitHash", "objectSetDigest")
    if any(existing.get(field) != marker.get(field) for field in stable_fields):
        raise AppError("retirement-marker-conflict-or-invalid", code=ErrorCode.INVALID_REQUEST, status=409)


def _receipt_payload_keys(receipt: dict[str, Any]) -> set[str]:
    from deepseek_infra.infra.workspace.backup_target_store import object_key

    keys: set[str] = set()
    filename = receipt.get("filename")
    if filename:
        keys.add(str(filename))
    object_digest = str(receipt.get("objectDigest") or "")
    if object_digest:
        keys.add(object_key(object_digest))
    for item in list(receipt.get("objects") or []) + list(receipt.get("components") or []):
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("key")
        if path:
            keys.add(str(path))
        digest = str(item.get("digest") or item.get("ciphertextDigest") or "")
        if digest:
            keys.add(object_key(digest))
            # Compatibility with pre-object-set layouts retained by older targets.
            keys.add(f"ciphertext/sha256/{digest}")
    return keys


def _payload_key_is_retained(target: Any, object_key: str, *, retiring_backup_id: str) -> bool:
    """Return True if object_key must not be GC'd.

    Prefer SQL-native live-ref checks only when index coverage is complete and
    fresh. When the index is empty/incomplete/stale, fall back to a fail-closed
    full Receipt scan: any unreadable or malformed Receipt makes the scan
    indeterminate and blocks destructive GC for the whole job.
    """
    from deepseek_infra.infra.workspace import backup_object_index

    target_id = str(getattr(target, "target_id", "") or "")
    if target_id:
        index_mode = backup_object_index.retained_payload_keys_from_index(
            target_id, retiring_backup_id=retiring_backup_id
        )
        if index_mode is not None:
            # Complete + fresh coverage: index is authoritative for live-ref.
            return backup_object_index.object_is_live_referenced(
                target_id, object_key, excluding_backup_id=retiring_backup_id
            )
        # Incomplete / empty / stale: over-retain if any indexed live ref exists,
        # then always consult receipts so unindexed live points stay protected.
        if backup_object_index.object_is_live_referenced(
            target_id, object_key, excluding_backup_id=retiring_backup_id
        ):
            return True

    # Fail-closed receipt scan (index empty, incomplete, or stale).
    if target.root is not None:
        receipts_dir = target.root / "receipts"
        candidates = sorted(receipts_dir.rglob("*.json")) if receipts_dir.is_dir() else []
        for path in candidates:
            try:
                receipt_bytes = path.read_bytes()
            except OSError as exc:
                raise GcReferenceScanIndeterminate(f"receipt-read-failure:{path.name}") from exc
            receipt = _read_json_bytes(receipt_bytes)
            if receipt is None:
                raise GcReferenceScanIndeterminate(f"receipt-parse-failure:{path.name}")
            if str(receipt.get("backupId") or path.stem) == retiring_backup_id:
                continue
            if _receipt_has_valid_retirement_marker(target, receipt_bytes, receipt):
                continue
            if object_key in _receipt_payload_keys(receipt):
                return True
    elif target.store is not None:
        cursor: str | None = None
        while True:
            page = target.store.list_objects("receipts/", cursor=cursor, limit=200)
            for meta in page.objects:
                try:
                    receipt_bytes = target.store.get_bytes(meta.key)
                except Exception as exc:
                    raise GcReferenceScanIndeterminate(f"receipt-read-failure:{meta.key}") from exc
                if receipt_bytes is None:
                    raise GcReferenceScanIndeterminate(f"receipt-read-failure:{meta.key}")
                receipt = _read_json_bytes(receipt_bytes)
                if receipt is None:
                    raise GcReferenceScanIndeterminate(f"receipt-parse-failure:{meta.key}")
                if str(receipt.get("backupId") or Path(meta.key).stem) == retiring_backup_id:
                    continue
                if _receipt_has_valid_retirement_marker(target, receipt_bytes, receipt):
                    continue
                if object_key in _receipt_payload_keys(receipt):
                    return True
            if page.cursor is None:
                break
            cursor = page.cursor
    return False


def _retained_payload_keys(target: Any, *, retiring_backup_id: str) -> set[str]:
    """Compatibility helper: full retained set via conservative scan only.

    Prefer :func:`_payload_key_is_retained` for GC. This set builder is used by
    tests and diagnostics; it never uses a truncated index page.
    """
    from deepseek_infra.infra.workspace import backup_object_index

    target_id = str(getattr(target, "target_id", "") or "")
    if target_id and backup_object_index.retained_payload_keys_from_index(
        target_id, retiring_backup_id=retiring_backup_id
    ) is not None:
        # Index present — do not materialize; return empty and force per-key checks.
        return set()

    retained: set[str] = set()
    if target.root is not None:
        receipts_dir = target.root / "receipts"
        candidates = sorted(receipts_dir.rglob("*.json")) if receipts_dir.is_dir() else []
        for path in candidates:
            try:
                receipt_bytes = path.read_bytes()
            except OSError as exc:
                raise GcReferenceScanIndeterminate(f"receipt-read-failure:{path.name}") from exc
            receipt = _read_json_bytes(receipt_bytes)
            if receipt is None:
                raise GcReferenceScanIndeterminate(f"receipt-parse-failure:{path.name}")
            if str(receipt.get("backupId") or path.stem) == retiring_backup_id:
                continue
            if _receipt_has_valid_retirement_marker(target, receipt_bytes, receipt):
                continue
            retained.update(_receipt_payload_keys(receipt))
    elif target.store is not None:
        cursor: str | None = None
        while True:
            page = target.store.list_objects("receipts/", cursor=cursor, limit=200)
            for meta in page.objects:
                try:
                    receipt_bytes = target.store.get_bytes(meta.key)
                except Exception as exc:
                    raise GcReferenceScanIndeterminate(f"receipt-read-failure:{meta.key}") from exc
                if receipt_bytes is None:
                    raise GcReferenceScanIndeterminate(f"receipt-read-failure:{meta.key}")
                receipt = _read_json_bytes(receipt_bytes)
                if receipt is None:
                    raise GcReferenceScanIndeterminate(f"receipt-parse-failure:{meta.key}")
                if str(receipt.get("backupId") or Path(meta.key).stem) == retiring_backup_id:
                    continue
                if _receipt_has_valid_retirement_marker(target, receipt_bytes, receipt):
                    continue
                retained.update(_receipt_payload_keys(receipt))
            if page.cursor is None:
                break
            cursor = page.cursor
    return retained

def _receipt_has_valid_retirement_marker(target: Any, receipt_bytes: bytes, receipt: dict[str, Any]) -> bool:
    policy_id = str(receipt.get("policyId") or "")
    backup_id = str(receipt.get("backupId") or "")
    if not policy_id or not backup_id:
        return False
    try:
        _, _, commit = _read_formal_metadata(target, policy_id, backup_id)
    except (AppError, OSError):
        return False
    key = retirement_marker_key(policy_id, backup_id)
    marker: dict[str, Any] | None = None
    if target.root is not None:
        path = target.root.joinpath(*key.split("/"))
        if path.is_file():
            marker = _read_json_bytes(path.read_bytes())
    elif target.store is not None:
        from deepseek_infra.infra.workspace.backup_target_store import read_json

        marker = read_json(target.store, key)
    return bool(
        marker is not None
        and str(marker.get("targetId") or "") == target.target_id
        and retirement_marker_valid(marker, receipt_bytes=receipt_bytes, commit=commit)
    )


_DEPENDENCY_SCAN_LIMIT = 500


def has_active_copy_dependency(target_id: str, policy_id: str, backup_id: str, *, excluding_job_id: str | None = None) -> bool:
    """Return true while any durable production job can still reference the copy.

    Page-limited scans fail closed: when a listing hits the scan ceiling without
    proving the absence of an active dependency, treat the copy as still
    referenced (same posture as drain completion blockers).
    """
    from deepseek_infra.infra.workspace import backup_replication

    limit = _DEPENDENCY_SCAN_LIMIT
    jobs = list(backup_replication.list_jobs(policy_id=policy_id, backup_id=backup_id, limit=limit))
    for job in jobs:
        if str(job.get("phase") or "") in backup_replication.TERMINAL_PHASES:
            continue
        if target_id in {str(job.get("primaryTargetId") or ""), str(job.get("replicaTargetId") or "")}:
            return True
    if len(jobs) >= limit:
        return True

    repairs = list(backup_replication.list_repair_jobs(policy_id=policy_id, limit=limit))
    for job in repairs:
        if str(job.get("backupId") or "") != backup_id or str(job.get("phase") or "") in backup_replication.REPAIR_TERMINAL_PHASES:
            continue
        if target_id in {str(job.get("sourceTargetId") or ""), str(job.get("destTargetId") or "")}:
            return True
    if len(repairs) >= limit:
        return True

    rebalances = list(backup_replication.list_rebalance_jobs(policy_id=policy_id, limit=limit))
    for job in rebalances:
        if str(job.get("backupId") or "") != backup_id or str(job.get("phase") or "") in {"complete", "failed"}:
            continue
        if target_id in {str(job.get("sourceTargetId") or ""), str(job.get("destTargetId") or "")}:
            return True
    if len(rebalances) >= limit:
        return True

    retirements = list(list_copy_retirement_jobs(policy_id=policy_id, target_id=target_id, limit=limit))
    for job in retirements:
        if str(job.get("jobId") or "") == str(excluding_job_id or "") or str(job.get("backupId") or "") != backup_id:
            continue
        if str(job.get("phase") or "") not in RETIREMENT_TERMINAL_PHASES:
            return True
    if len(retirements) >= limit:
        return True

    from deepseek_infra.infra.workspace import backup_recovery_job, backups

    if backups.RESTORE_DIR.is_dir():
        for path in backups.RESTORE_DIR.glob("*/remote-fetch.json"):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(session, dict) or str(session.get("phase") or "") in backup_recovery_job.TERMINAL_PHASES:
                continue
            session_target = str(session.get("activeSourceTargetId") or session.get("targetId") or "")
            if session_target == target_id and str(session.get("backupId") or "") == backup_id:
                return True
    return False


def execute_copy_retirement_job(
    job_id: str,
    *,
    instance_id: str = "retirement-worker",
) -> dict[str, Any]:
    """Drive a CopyRetirementJob through its safety checks and GC execution."""
    job = get_copy_retirement_job(job_id)
    if not job:
        raise AppError(f"Retirement job {job_id} not found", code=ErrorCode.NOT_FOUND, status=404)

    if job["phase"] in RETIREMENT_TERMINAL_PHASES:
        return job

    policy_id = job["policyId"]
    backup_id = job["backupId"]
    target_id = job["targetId"]

    # 1. checking-topology
    _update_job_phase(job_id, "checking-topology")
    from deepseek_infra.infra.workspace import backup_replication

    sim = backup_replication.simulate_copy_removal(policy_id, backup_id, target_id)
    if not sim.get("policySafe"):
        reason = "topology-safety-constraint: removal would breach copy, failure-domain, region, or per-domain objectives"
        return _update_job_phase(job_id, "rejected", error=reason, sim_metadata=sim)

    # 2. checking-holds
    _update_job_phase(job_id, "checking-holds")
    if sim.get("protectedByHold") or backup_replication.is_source_held(target_id, policy_id, backup_id):
        reason = "copy-protected-by-hold: active drill, recovery, or lease hold on target"
        return _update_job_phase(job_id, "rejected", error=reason, sim_metadata=sim)
    if has_active_copy_dependency(target_id, policy_id, backup_id, excluding_job_id=job_id):
        reason = "copy-protected-by-active-job: replication, repair, rebalance, recovery, or retirement still references the copy"
        return _update_job_phase(job_id, "waiting-for-dependencies", error=reason, sim_metadata=sim)

    bytes_reclaimed = 0
    marker_committed = False
    try:
        target = backup_publish.resolve_target(target_id)
        receipt_bytes, receipt, commit = _read_formal_metadata(target, policy_id, backup_id)

        # 3. Commit the immutable lifecycle marker before changing the Ledger or
        # reclaiming payload. A crash after this point is safely resumable.
        _update_job_phase(job_id, "committing-retirement-marker")
        marker = build_retirement_marker(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt_bytes=receipt_bytes,
            receipt=receipt,
            commit=commit,
            retirement_job_id=job_id,
            reason=str(job.get("reason") or "copy-retirement"),
        )
        _write_retirement_marker(target, marker, receipt_bytes=receipt_bytes, commit=commit)
        marker_committed = True

        # 4. Project lifecycle state locally. Formal Receipt/Commit bytes remain
        # untouched on the Target and continue to participate in hash-chain audit.
        _update_job_phase(job_id, "retiring-ledger-copy")
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
        target_copy = next((copy for copy in copies if str(copy.get("targetId")) == target_id), None)
        committed_at = str((target_copy or {}).get("committedAt") or commit.get("committedAt") or _utc_iso())
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            committed_at=committed_at,
            object_set_digest=str(receipt.get("objectSetDigest") or commit.get("objectSetDigest") or "") or None,
            state="retired",
            recoverable=False,
            last_verified_at=_utc_iso(),
            metadata={"retirementMarkerHash": marker["markerHash"], "retiredAt": marker["retiredAt"]},
        )
        from deepseek_infra.infra.workspace import backup_object_index

        backup_object_index.index_receipt_objects(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
            ref_state="live",
        )
        backup_object_index.apply_retirement_to_index(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
        )

        # 5. Physical GC is payload-only and fail-closed. Candidate keys come from
        # the retiring receipt only. Live-ref checks use a complete index when
        # available, otherwise a conservative full Receipt scan. Incomplete
        # coverage must not hard-block this path (retirement itself may have
        # written partial index rows for the retiring point).
        _update_job_phase(job_id, "gc-pending")
        _update_job_phase(job_id, "gc-running")
        try:
            for component in sorted(_receipt_payload_keys(receipt)):
                if _payload_key_is_retained(target, component, retiring_backup_id=backup_id):
                    continue
                if target.root is not None:
                    path = target.root.joinpath(*component.split("/"))
                    if path.is_file():
                        size = path.stat().st_size
                        path.unlink()
                        bytes_reclaimed += size
                elif target.store is not None:
                    meta = target.store.stat(component)
                    if meta is not None:
                        if not target.store.delete_if_match(component, expected_etag=meta.etag):
                            raise AppError(
                                f"retirement-payload-delete-cas-mismatch:{component}",
                                code=ErrorCode.INVALID_REQUEST,
                                status=409,
                            )
                        bytes_reclaimed += int(meta.size or 0)
        except GcReferenceScanIndeterminate as exc:
            # Marker stays; payload is retained until receipt truth is repaired.
            return _update_job_phase(
                job_id,
                "gc-reconciliation-required",
                error=f"gc-scan-indeterminate:{exc.reason}",
                bytes_reclaimed=bytes_reclaimed,
                sim_metadata={**sim, "retirementMarker": marker},
            )

        return _update_job_phase(job_id, "reclaimed", bytes_reclaimed=bytes_reclaimed, sim_metadata={**sim, "retirementMarker": marker})
    except Exception as exc:
        # Once the authenticated marker exists, keep the job resumable. The
        # next maintenance tick revalidates the marker and retries payload-only
        # GC without touching the preserved Receipt/Commit bytes.
        retry_phase = "gc-pending" if marker_committed else "failed"
        return _update_job_phase(job_id, retry_phase, error=str(exc), bytes_reclaimed=bytes_reclaimed)


def process_pending_retirements(
    *,
    instance_id: str = "retirement-worker",
    limit: int = 5,
) -> dict[str, int]:
    """Retry a bounded set of non-terminal CopyRetirementJobs."""
    candidates = [
        job
        for job in list_copy_retirement_jobs(limit=max(1, min(limit * 10, 500)))
        if str(job.get("phase") or "") not in RETIREMENT_TERMINAL_PHASES
    ][: max(1, limit)]
    reclaimed = 0
    waiting = 0
    failed = 0
    for job in candidates:
        result = execute_copy_retirement_job(str(job["jobId"]), instance_id=instance_id)
        phase = str(result.get("phase") or "")
        if phase == "reclaimed":
            reclaimed += 1
        elif phase in {
            "waiting-for-dependencies",
            "requested",
            "checking-topology",
            "checking-holds",
            "committing-retirement-marker",
            "retiring-ledger-copy",
            "gc-pending",
            "gc-running",
            "gc-reconciliation-required",
        }:
            waiting += 1
        else:
            failed += 1
    return {"processed": len(candidates), "reclaimed": reclaimed, "waiting": waiting, "failed": failed}
