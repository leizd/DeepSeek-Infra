"""Atomic backup publication protocol (4.4.4).

Every publish flows through ``.partial`` staging: copy, fsync, target-side
SHA-256 recomputation, receipt write, atomic rename into ``backups/``, atomic
receipt publication and a directory fsync. A blocked or swapped target never
falls back to another disk; the run is recorded as blocked and retried by its
policy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_scheduler, backup_targets, backup_unattended, backups

RECEIPT_SCHEMA_VERSION = 1

LAYOUT_DIRS = ("backups", "receipts", "catalog", ".partial", ".trash")


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    target_id: str
    root: Path
    managed: bool


@dataclass(frozen=True, slots=True)
class PublishResult:
    receipt: dict[str, Any]
    path: Path
    receipt_path: Path


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_target(target_id: str) -> ResolvedTarget:
    """Resolve and re-verify a publish target; raises blocked-target-unavailable."""
    if target_id == "managed-local":
        root = backups.BACKUP_DIR
        root.mkdir(parents=True, exist_ok=True)
        return ResolvedTarget(target_id=target_id, root=root, managed=True)
    root = backup_targets.verify_target_ready(target_id)
    return ResolvedTarget(target_id=target_id, root=root, managed=False)


def _ensure_layout(root: Path) -> None:
    for name in LAYOUT_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def receipt_for(
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    target_id: str,
    schedule_slot: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": RECEIPT_SCHEMA_VERSION,
        "backupId": package.backup_id,
        "runId": run_id,
        "policyId": policy_id,
        "targetId": target_id,
        "scheduleSlot": schedule_slot,
        "filename": package.filename,
        "size": package.size,
        "ciphertextSha256": package.ciphertext_sha256,
        "manifestDigest": package.manifest_digest,
        "coverageDigest": package.coverage_digest,
        "creationVerified": package.creation_verified,
        "createdAt": _utc_iso(),
        "pinned": False,
    }


def publish_backup(
    target: ResolvedTarget,
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    receipt: dict[str, Any] | None = None,
) -> PublishResult:
    """Atomically publish a verified package into the target layout."""
    root = target.root
    _ensure_layout(root)
    partial = root / ".partial" / f"{run_id}.part"
    final = root / "backups" / package.filename
    try:
        with package.path.open("rb") as source, partial.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if backup_unattended.sha256_file(partial) != package.ciphertext_sha256:
            raise AppError("Target-side backup digest mismatch after copy", code=ErrorCode.INTERNAL, status=500)
        receipt_data = receipt or receipt_for(package, run_id=run_id, policy_id=policy_id, target_id=target.target_id, schedule_slot=schedule_slot)
        receipt_path = root / "receipts" / f"{package.filename}.receipt.json"
        receipt_tmp = root / "receipts" / f".{package.filename}.receipt.json.tmp"
        with receipt_tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, final)
        os.replace(receipt_tmp, receipt_path)
        _fsync_dir(root / "backups")
        _fsync_dir(root / "receipts")
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        return PublishResult(receipt=receipt_data, path=final, receipt_path=receipt_path)
    except AppError:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "blocked", str(exc)[:200])
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


def cleanup_partial(root: Path, run_id: str) -> None:
    (root / ".partial" / f"{run_id}.part").unlink(missing_ok=True)
