"""Immutable object publication with fenced commit markers (4.4.5).

A backup becomes visible only when its schedule slot's commit marker is
created. The ciphertext is first stored as a content-addressed object under
``objects/sha256/<prefix>/<digest>.age`` and re-hashed on the target; the
intent is journaled under ``transactions/<runId>.json``; the immutable receipt
lives at ``receipts/<backupId>.json``; and finally the slot marker under
``commits/<policyId>/<slotHash>.json`` is created with ``O_EXCL``. A second
publisher for the same slot converges when the object digest matches and is
rejected with ``slot-commit-conflict`` otherwise, so a schedule slot can only
ever produce one formal commit. Callers pass a lease ``checkpoint`` that runs
at every chunk boundary and right before the marker — the first visible step.
Legacy 4.4.4 targets whose ciphertext still lives under ``backups/<filename>``
stay readable through :func:`backup_file_candidates`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_scheduler, backup_targets, backup_unattended, backups

RECEIPT_SCHEMA_VERSION = 2
COMMIT_SCHEMA_VERSION = 2
GENESIS_COMMIT_HASH = "0" * 64

LAYOUT_DIRS = ("objects", "transactions", "receipts", "commits", "catalog", ".partial", ".trash", ".orphaned")


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
    commit: dict[str, Any]
    converged: bool


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def resolve_target(target_id: str, *, write_intent: bool = True) -> ResolvedTarget:
    """Resolve and re-verify a publish target; raises blocked-target-unavailable."""
    if target_id == "managed-local":
        root = backups.BACKUP_DIR
        root.mkdir(parents=True, exist_ok=True)
        return ResolvedTarget(target_id=target_id, root=root, managed=True)
    root = backup_targets.verify_target_ready(target_id, write_intent=write_intent)
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


def object_path(root: Path, digest: str) -> Path:
    return root / "objects" / "sha256" / digest[:2] / f"{digest}.age"


def backup_file_candidates(root: Path, record: dict[str, Any]) -> list[Path]:
    """Existing-layout then legacy locations for a catalog/receipt record."""
    candidates: list[Path] = []
    digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
    if len(digest) == 64:
        candidates.append(object_path(root, digest))
    filename = str(record.get("filename") or "")
    if filename and "/" not in filename and "\\" not in filename:
        candidates.append(root / "backups" / filename)
    return candidates


def commit_marker_path(root: Path, policy_id: str, schedule_slot: str) -> Path:
    slot_hash = hashlib.sha256(schedule_slot.encode("utf-8")).hexdigest()[:16]
    return root / "commits" / policy_id / f"{slot_hash}.json"


def read_commit_markers(root: Path) -> list[dict[str, Any]]:
    commits_dir = root / "commits"
    markers: list[dict[str, Any]] = []
    if not commits_dir.is_dir():
        return markers
    for path in sorted(commits_dir.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("commitHash"):
            markers.append(data)
    return markers


def latest_commit(root: Path) -> dict[str, Any] | None:
    markers = read_commit_markers(root)
    if not markers:
        return None
    return max(markers, key=lambda marker: int(marker.get("targetGeneration") or 0))


def _commit_hash(marker: dict[str, Any]) -> str:
    body = {key: value for key, value in marker.items() if key != "commitHash"}
    return hashlib.sha256(_stable_json(body)).hexdigest()


def commit_marker_valid(marker: dict[str, Any]) -> bool:
    commit_hash = str(marker.get("commitHash") or "")
    if not commit_hash:
        return False
    return commit_hash == _commit_hash(marker)


def _journal_path(root: Path, run_id: str) -> Path:
    return root / "transactions" / f"{run_id}.json"


def _write_journal(root: Path, journal: dict[str, Any]) -> None:
    path = _journal_path(root, str(journal["runId"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_journal(root: Path, run_id: str) -> dict[str, Any] | None:
    path = _journal_path(root, run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if path.read_bytes() != content:
            raise AppError("Immutable target file already exists with different content", code=ErrorCode.INVALID_REQUEST, status=409)
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _create_exclusive(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return True


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
        "objectDigest": package.ciphertext_sha256,
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
    fencing_token: int,
    receipt: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> PublishResult:
    """Publish a verified package as an immutable object plus slot commit."""
    root = target.root
    _ensure_layout(root)
    digest = str(package.ciphertext_sha256)
    obj = object_path(root, digest)
    partial = root / ".partial" / f"{run_id}.part"

    def _checkpoint() -> None:
        if checkpoint is not None:
            checkpoint()

    journal: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "fencingToken": int(fencing_token),
        "backupId": package.backup_id,
        "objectDigest": digest,
        "filename": package.filename,
        "size": package.size,
        "phase": "started",
        "updatedAt": _utc_iso(),
    }
    try:
        _write_journal(root, journal)
        if not obj.is_file():
            with package.path.open("rb") as source, partial.open("wb") as output:
                while True:
                    _checkpoint()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if backup_unattended.sha256_file(partial, checkpoint=checkpoint) != digest:
                raise AppError("Target-side backup digest mismatch after copy", code=ErrorCode.INTERNAL, status=500)
            obj.parent.mkdir(parents=True, exist_ok=True)
            _checkpoint()
            os.replace(partial, obj)
            _fsync_dir(obj.parent)
        elif backup_unattended.sha256_file(obj, checkpoint=checkpoint) != digest:
            raise AppError("Content-addressed object on target fails its digest", code=ErrorCode.INTERNAL, status=500)
        journal.update(phase="object-published", updatedAt=_utc_iso())
        _write_journal(root, journal)

        receipt_data = dict(receipt) if receipt is not None else receipt_for(package, run_id=run_id, policy_id=policy_id, target_id=target.target_id, schedule_slot=schedule_slot)
        receipt_data["objectDigest"] = digest
        receipt_bytes = (json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        receipt_path = root / "receipts" / f"{package.backup_id}.json"
        marker_path = commit_marker_path(root, policy_id, schedule_slot)

        def _converge_or_conflict(existing: dict[str, Any]) -> PublishResult:
            if str(existing.get("objectDigest") or "") == digest:
                existing_receipt_path = root / "receipts" / f"{existing.get('backupId')}.json"
                existing_receipt = json.loads(existing_receipt_path.read_text(encoding="utf-8"))
                journal.update(phase="converged", convergedToRunId=str(existing.get("runId") or ""), updatedAt=_utc_iso())
                _write_journal(root, journal)
                backup_scheduler.record_target_health(target.target_id, "ok", None)
                return PublishResult(receipt=existing_receipt, path=obj, receipt_path=existing_receipt_path, commit=existing, converged=True)
            detail = "stale-fencing-token" if int(existing.get("fencingToken") or 0) > int(fencing_token) else "different-object"
            journal.update(phase="slot-commit-conflict", conflict=detail, updatedAt=_utc_iso())
            _write_journal(root, journal)
            raise AppError(f"slot-commit-conflict ({detail}): schedule slot is already committed", code=ErrorCode.INVALID_REQUEST, status=409)

        _checkpoint()
        if marker_path.is_file():
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))

        _write_immutable(receipt_path, receipt_bytes)
        journal.update(phase="receipt-published", receiptDigest=receipt_digest, receipt=receipt_data, updatedAt=_utc_iso())
        _write_journal(root, journal)

        _checkpoint()
        latest = latest_commit(root)
        generation = int(latest["targetGeneration"]) + 1 if latest is not None else 1
        marker: dict[str, Any] = {
            "schemaVersion": COMMIT_SCHEMA_VERSION,
            "policyId": policy_id,
            "scheduleSlot": schedule_slot,
            "runId": run_id,
            "fencingToken": int(fencing_token),
            "backupId": package.backup_id,
            "objectDigest": digest,
            "receiptDigest": receipt_digest,
            "targetGeneration": generation,
            "previousCommitHash": str(latest["commitHash"]) if latest is not None else GENESIS_COMMIT_HASH,
        }
        marker["commitHash"] = _commit_hash(marker)
        marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if not _create_exclusive(marker_path, marker_bytes):
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))
        _fsync_dir(marker_path.parent)
        journal.update(phase="committed", commitHash=marker["commitHash"], updatedAt=_utc_iso())
        _write_journal(root, journal)
        if not target.managed:
            backup_targets.record_target_head(root, target_id=target.target_id, generation=int(marker["targetGeneration"]), commit_hash=str(marker["commitHash"]))
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        return PublishResult(receipt=receipt_data, path=obj, receipt_path=receipt_path, commit=marker, converged=False)
    except AppError:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "blocked", str(exc)[:200])
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


INCOMPLETE_JOURNAL_PHASES = ("started", "object-published", "receipt-published")


def slot_has_incomplete_journal(root: Path, *, policy_id: str, schedule_slot: str, exclude_run_id: str | None = None) -> bool:
    """True when another run left an uncommitted transaction for this slot."""
    if commit_marker_path(root, policy_id, schedule_slot).is_file():
        return False
    transactions = root / "transactions"
    if not transactions.is_dir():
        return False
    for path in sorted(transactions.glob("*.json")):
        journal = read_journal(root, path.name[: -len(".json")])
        if not journal:
            continue
        if exclude_run_id is not None and str(journal.get("runId") or "") == exclude_run_id:
            continue
        if str(journal.get("policyId") or "") != policy_id or str(journal.get("scheduleSlot") or "") != schedule_slot:
            continue
        if str(journal.get("phase") or "") in INCOMPLETE_JOURNAL_PHASES:
            return True
    return False


def cleanup_partial(root: Path, run_id: str) -> None:
    (root / ".partial" / f"{run_id}.part").unlink(missing_ok=True)
