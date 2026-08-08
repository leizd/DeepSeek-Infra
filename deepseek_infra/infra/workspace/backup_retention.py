"""Retention governance for scheduled backups (4.4.5).

Grandfather-father-son retention buckets backups in the *policy* timezone,
never deletes pinned, restore-referenced or minimum-healthy copies, runs only
after a successful publish, and deletes in two phases with a trash grace
period so a retention bug can be undone. Callers pass a lease ``checkpoint``
that runs before every trash/delete move so a worker that lost its lease
mid-sweep stops before the next visible mutation.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_catalog, backup_crypto, backup_publish, backup_reconcile, backup_scheduler, backup_unattended, backups, backup_writer_lease
from deepseek_infra.infra.workspace.backup_cron import load_timezone

RETENTION_SCHEMA_VERSION = 1
BACKUP_RETENTION_DIR = config.ROOT / ".backup-retention"
DEFAULT_RETENTION_POLICY_ID = "default"

_DEFAULTS: dict[str, Any] = {
    "keepLast": 3,
    "keepHourly": 24,
    "keepDaily": 14,
    "keepWeekly": 8,
    "keepMonthly": 12,
    "maxAgeDays": None,
    "maxTotalBytes": None,
    "trashGraceHours": 24,
    "protectPinned": True,
    "minimumHealthyCopies": 2,
}

_BOUNDS: dict[str, tuple[int, int]] = {
    "keepLast": (0, 1000),
    "keepHourly": (0, 24 * 366),
    "keepDaily": (0, 3660),
    "keepWeekly": (0, 520),
    "keepMonthly": (0, 240),
    "trashGraceHours": (1, 24 * 90),
    "minimumHealthyCopies": (1, 100),
}


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _policy_path(retention_policy_id: str) -> Path:
    return BACKUP_RETENTION_DIR / f"{retention_policy_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def normalize_retention_policy(payload: dict[str, Any], *, retention_policy_id: str = DEFAULT_RETENTION_POLICY_ID) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Retention policy must be an object", code=ErrorCode.INVALID_PAYLOAD)
    normalized: dict[str, Any] = {"schemaVersion": RETENTION_SCHEMA_VERSION, "retentionPolicyId": retention_policy_id}
    for key, default in _DEFAULTS.items():
        value = payload.get(key, default)
        if key in {"maxAgeDays", "maxTotalBytes"}:
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise AppError(f"Retention field {key} must be a positive integer or null", code=ErrorCode.INVALID_PAYLOAD)
            normalized[key] = value
        elif key == "protectPinned":
            normalized[key] = bool(value)
        else:
            minimum, maximum = _BOUNDS[key]
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise AppError(f"Retention field {key} must be between {minimum} and {maximum}", code=ErrorCode.INVALID_PAYLOAD)
            normalized[key] = value
    return normalized


def get_retention_policy(retention_policy_id: str) -> dict[str, Any]:
    path = _policy_path(str(retention_policy_id or DEFAULT_RETENTION_POLICY_ID))
    if not path.is_file():
        if retention_policy_id in {"", DEFAULT_RETENTION_POLICY_ID}:
            return normalize_retention_policy({}, retention_policy_id=DEFAULT_RETENTION_POLICY_ID)
        raise AppError("Retention policy not found", code=ErrorCode.NOT_FOUND, status=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Retention policy store is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
    return normalize_retention_policy(data, retention_policy_id=str(data.get("retentionPolicyId") or retention_policy_id))


def put_retention_policy(retention_policy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe = str(retention_policy_id or "").strip()
    if not safe or len(safe) > 64 or not all(char.isalnum() or char in "._-" for char in safe):
        raise AppError("Invalid retention policy id", code=ErrorCode.INVALID_PAYLOAD)
    normalized = normalize_retention_policy(payload, retention_policy_id=safe)
    _atomic_write_json(_policy_path(safe), normalized)
    return normalized


def list_retention_policies() -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {DEFAULT_RETENTION_POLICY_ID: normalize_retention_policy({}, retention_policy_id=DEFAULT_RETENTION_POLICY_ID)}
    if BACKUP_RETENTION_DIR.is_dir():
        for path in sorted(BACKUP_RETENTION_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                policy = normalize_retention_policy(data, retention_policy_id=str(data.get("retentionPolicyId") or path.stem))
                by_id[policy["retentionPolicyId"]] = policy
    return [by_id[key] for key in sorted(by_id)]


def _bucket_keys(local: datetime) -> dict[str, str]:
    iso = local.isocalendar()
    return {
        "hourly": local.strftime("%Y-%m-%dT%H"),
        "daily": local.strftime("%Y-%m-%d"),
        "weekly": f"{iso.year}-W{iso.week:02d}",
        "monthly": local.strftime("%Y-%m"),
    }


def _ordered_buckets(now_local: datetime, kind: str, count: int) -> list[str]:
    buckets: list[str] = []
    current = now_local
    for _ in range(count):
        buckets.append(_bucket_keys(current)[kind])
        if kind == "hourly":
            current -= timedelta(hours=1)
        elif kind == "daily":
            current -= timedelta(days=1)
        elif kind == "weekly":
            current -= timedelta(weeks=1)
        else:
            month = current.month - 1 or 12
            year = current.year - (1 if current.month == 1 else 0)
            current = current.replace(year=year, month=month, day=1)
    return buckets


def _restore_references() -> set[str]:
    """Filenames and digests referenced by active restores or safety backups."""
    references: set[str] = set()
    restore_dir = backups.RESTORE_DIR
    if not restore_dir.is_dir():
        return references
    keys = {"filename", "ciphertextSha256", "archiveSha256", "safetyBackupId", "backupId", "sourceBackupId"}

    def _scan(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item:
                    references.add(item)
                else:
                    _scan(item)
        elif isinstance(value, list):
            for item in value:
                _scan(item)

    for metadata in restore_dir.glob("*/"):
        for name in ("upload.json", "plan.json", "transaction.json", "metadata.json"):
            path = metadata / name
            if not path.is_file():
                continue
            try:
                _scan(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                references.add(path.stem)
    return references


def _active_run_backup_ids() -> set[str]:
    active: set[str] = set()
    for run in backup_scheduler.list_runs(limit=500):
        if run.get("phase") in backup_scheduler.ACTIVE_PHASES and run.get("backupId"):
            active.add(str(run["backupId"]))
    return active


def _policy_digest(retention: dict[str, Any]) -> str:
    import hashlib

    body = {key: value for key, value in retention.items() if key != "retentionPolicyId"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _preview_snapshot(target_root: Path) -> dict[str, Any]:
    precondition = backup_catalog.catalog_precondition(target_root)
    return {"targetGeneration": int(precondition.expected_target_generation or 0), "catalogHeadHash": str(precondition.expected_head_hash or backup_catalog.GENESIS_HASH)}


def _validate_preview_snapshot(preview: dict[str, Any], retention: dict[str, Any], target_root: Path) -> None:
    snapshot = _preview_snapshot(target_root)
    if str(preview.get("catalogHeadHash") or "") != snapshot["catalogHeadHash"]:
        raise AppError("retention-stale-snapshot: catalog head changed since preview", code=ErrorCode.INVALID_REQUEST, status=409)
    if int(preview.get("targetGeneration") or 0) != int(snapshot["targetGeneration"]):
        raise AppError("retention-stale-snapshot: target generation changed since preview", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(preview.get("policyDigest") or "") != _policy_digest(retention):
        raise AppError("retention-stale-snapshot: retention policy changed since preview", code=ErrorCode.INVALID_REQUEST, status=409)


def _healthy_records(records: list[dict[str, Any]], target_root: Path) -> list[dict[str, Any]]:
    """Records that count toward ``minimumHealthyCopies``.

    Schema-2 records must have an existing object, a receipt whose bytes match
    the slot marker's receipt digest, a valid commit marker, a readable age
    header and a recent successful scrub or creation verification. Legacy
    records keep the file-existence check plus the same scrub/creation rule.
    """
    markers = {str(marker.get("backupId") or ""): marker for marker in backup_publish.read_commit_markers(target_root)}
    healthy: list[dict[str, Any]] = []
    for record in records:
        backup_id = str(record.get("backupId") or "")
        if record.get("scrubOk") is not True and record.get("creationVerified") is not True:
            continue
        candidate = next((path for path in backup_publish.backup_file_candidates(target_root, record) if path.is_file()), None)
        if candidate is None:
            continue
        if int(record.get("schemaVersion") or 0) < 2:
            healthy.append(record)
            continue
        marker = markers.get(backup_id)
        if marker is None or not backup_publish.commit_marker_valid(marker):
            continue
        digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
        if not digest or str(marker.get("objectDigest") or "") != digest:
            continue
        receipt_path = target_root / "receipts" / f"{backup_id}.json"
        if not receipt_path.is_file():
            continue
        if backup_unattended.sha256_file(receipt_path) != str(marker.get("receiptDigest") or ""):
            continue
        try:
            header = backup_crypto.inspect_header(candidate)
        except AppError:
            continue
        if not header.get("age"):
            continue
        healthy.append(record)
    return healthy


def preview_retention(
    retention: dict[str, Any],
    target_root: Path,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the keep/trash/protected sets without touching the filesystem."""
    current = now or datetime.now(tz=timezone.utc)
    tz = load_timezone(policy_timezone)
    now_local = current.astimezone(tz)
    records = [
        record
        for record in backup_catalog.catalog_state(target_root).values()
        if not record.get("deleted") and not record.get("trashed")
    ]
    records.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    keep: set[str] = set()
    protected: dict[str, str] = {}
    latest = records[0] if records else None
    if latest is not None:
        protected[str(latest["backupId"])] = "latest-successful-backup"
        keep.add(str(latest["backupId"]))
    for record in records[: int(retention["keepLast"])]:
        keep.add(str(record["backupId"]))
    bucketed: dict[str, set[str]] = {"hourly": set(), "daily": set(), "weekly": set(), "monthly": set()}
    for record in records:
        created = _parse_iso(record.get("createdAt"))
        if created is None:
            continue
        keys = _bucket_keys(created.astimezone(tz))
        for kind in bucketed:
            bucketed[kind].add(keys[kind])
    for kind, limit in (("hourly", retention["keepHourly"]), ("daily", retention["keepDaily"]), ("weekly", retention["keepWeekly"]), ("monthly", retention["keepMonthly"])):
        wanted = set(_ordered_buckets(now_local, kind, int(limit)))
        for record in records:
            created = _parse_iso(record.get("createdAt"))
            if created is None:
                continue
            if _bucket_keys(created.astimezone(tz))[kind] in wanted:
                keep.add(str(record["backupId"]))
    if retention.get("maxAgeDays"):
        cutoff = current - timedelta(days=int(retention["maxAgeDays"]))
        for record in records:
            created = _parse_iso(record.get("createdAt"))
            if created is not None and created >= cutoff:
                keep.add(str(record["backupId"]))
    references = _restore_references()
    active_runs = _active_run_backup_ids()
    healthy = _healthy_records(records, target_root)
    for record in healthy[: int(retention["minimumHealthyCopies"])]:
        protected.setdefault(str(record["backupId"]), "minimum-healthy-copies")
        keep.add(str(record["backupId"]))
    for record in records:
        backup_id = str(record["backupId"])
        filename = str(record.get("filename") or "")
        if retention.get("protectPinned", True) and record.get("pinned"):
            protected.setdefault(backup_id, "pinned")
            keep.add(backup_id)
        if backup_id in active_runs:
            protected.setdefault(backup_id, "active-run")
            keep.add(backup_id)
        if backup_id in references or filename in references or str(record.get("ciphertextSha256") or "") in references:
            protected.setdefault(backup_id, "restore-referenced")
            keep.add(backup_id)
    trash: list[dict[str, Any]] = []
    if retention.get("maxTotalBytes"):
        total = sum(int(record.get("size") or 0) for record in records if str(record["backupId"]) in keep)
        budget = int(retention["maxTotalBytes"])
        for record in records:
            backup_id = str(record["backupId"])
            if backup_id in keep and backup_id not in protected and total > budget:
                keep.discard(backup_id)
                total -= int(record.get("size") or 0)
    for record in records:
        backup_id = str(record["backupId"])
        if backup_id not in keep and backup_id not in protected:
            trash.append(record)
    snapshot = _preview_snapshot(target_root)
    return {
        "retentionRunId": f"rr_{uuid.uuid4().hex[:12]}",
        "targetId": str(target_root),
        "targetGeneration": snapshot["targetGeneration"],
        "catalogHeadHash": snapshot["catalogHeadHash"],
        "policyDigest": _policy_digest(retention),
        "policyTimezone": policy_timezone,
        "evaluatedAt": _utc_iso(current),
        "keep": sorted(keep),
        "trash": [str(record["backupId"]) for record in trash],
        "trashRecords": trash,
        "protected": [{"backupId": key, "reason": reason} for key, reason in sorted(protected.items())],
    }


TRASH_JOURNAL_NAME = "trash.journal.json"


def _trash_journal_write(destination: Path, journal: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / TRASH_JOURNAL_NAME
    tmp = destination / f".{TRASH_JOURNAL_NAME}.{os.getpid()}.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _trash_candidates(target_root: Path, record: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    backup_id = str(record["backupId"])
    filename = str(record.get("filename") or "")
    digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
    state = backup_catalog.catalog_state(target_root)
    shared = bool(digest) and any(
        other_id != backup_id
        and not other.get("trashed")
        and not other.get("deleted")
        and str(other.get("objectDigest") or other.get("ciphertextSha256") or "") == digest
        for other_id, other in state.items()
    )
    payloads = [
        candidate
        for candidate in backup_publish.backup_file_candidates(target_root, record)
        if candidate.is_file() and not (candidate.parent.name != "backups" and shared)
    ]
    receipts = [path for path in (target_root / "receipts" / f"{backup_id}.json", target_root / "receipts" / f"{filename}.receipt.json") if path.is_file()]
    return payloads, receipts


def _execute_trash_move(
    target_root: Path,
    record: dict[str, Any],
    *,
    retention_run_id: str,
    at: str,
    writer: backup_writer_lease.TargetWriterLease | None,
) -> bool:
    """Journaled trash move: intent → payload-moved → receipt-moved → event-committed."""
    backup_id = str(record["backupId"])
    filename = str(record.get("filename") or "")
    if not filename or "/" in filename or "\\" in filename:
        return False
    destination = target_root / ".trash" / backup_id
    digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
    payloads, receipts = _trash_candidates(target_root, record)
    journal = {
        "schemaVersion": 1,
        "backupId": backup_id,
        "retentionRunId": retention_run_id,
        "filename": filename,
        "objectDigest": digest,
        "payloadNames": [path.name for path in payloads],
        "receiptNames": [path.name for path in receipts],
        "phase": "intent",
        "recordedAt": at,
    }
    _trash_journal_write(destination, journal)
    for payload in payloads:
        os.replace(payload, destination / payload.name)
    journal["phase"] = "payload-moved"
    _trash_journal_write(destination, journal)
    for receipt in receipts:
        os.replace(receipt, destination / receipt.name)
    journal["phase"] = "receipt-moved"
    _trash_journal_write(destination, journal)
    backup_catalog.record_trash(target_root, backup_id, retention_run_id=retention_run_id, at=at, writer=writer)
    journal["phase"] = "event-committed"
    _trash_journal_write(destination, journal)
    return True


def _recover_trash_journals(target_root: Path, *, at: str, writer: backup_writer_lease.TargetWriterLease | None) -> list[str]:
    """Roll interrupted trash transactions forward to their committed event."""
    trash_dir = target_root / ".trash"
    if not trash_dir.is_dir():
        return []
    recovered: list[str] = []
    state = backup_catalog.catalog_state(target_root)
    for entry in sorted(trash_dir.iterdir()):
        if not entry.is_dir():
            continue
        journal_path = entry / TRASH_JOURNAL_NAME
        if not journal_path.is_file():
            continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        phase = str(journal.get("phase") or "")
        if phase == "event-committed":
            continue
        backup_id = str(journal.get("backupId") or entry.name)
        record = state.get(backup_id)
        candidates = backup_publish.backup_file_candidates(target_root, record or journal)
        for payload in candidates:
            if payload.is_file() and payload.name in set(journal.get("payloadNames") or [payload.name]):
                os.replace(payload, entry / payload.name)
        for name in set(journal.get("receiptNames") or []):
            for base in (target_root / "receipts",):
                source = base / str(name)
                if source.is_file():
                    os.replace(source, entry / str(name))
        if record is not None and not record.get("trashed"):
            backup_catalog.record_trash(target_root, backup_id, retention_run_id=str(journal.get("retentionRunId") or ""), at=at, writer=writer)
        journal["phase"] = "event-committed"
        _trash_journal_write(entry, journal)
        recovered.append(backup_id)
    return recovered


def apply_retention(
    retention: dict[str, Any],
    target_root: Path,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
    checkpoint: Callable[[], None] | None = None,
    preview: dict[str, Any] | None = None,
    writer: backup_writer_lease.TargetWriterLease | None = None,
) -> dict[str, Any]:
    """Phase one: move prune candidates into ``.trash`` and record intent."""
    backup_reconcile.assert_catalog_committed(target_root)
    if writer is not None:
        writer.assert_owned()
    if preview is None:
        preview = preview_retention(retention, target_root, policy_timezone=policy_timezone, now=now)
    else:
        if "trashRecords" not in preview or "keep" not in preview:
            raise AppError("retention-stale-snapshot: preview is incomplete", code=ErrorCode.INVALID_REQUEST, status=409)
        _validate_preview_snapshot(preview, retention, target_root)
    retention_run_id = str(preview.get("retentionRunId") or f"rr_{uuid.uuid4().hex[:12]}")
    backup_scheduler.record_retention_run(
        retention_run_id,
        policy_id=str(retention.get("retentionPolicyId") or ""),
        target_id=str(target_root),
        status="preview",
        preview={key: preview[key] for key in ("keep", "trash", "protected")},
    )
    recovered = _recover_trash_journals(target_root, at=_utc_iso(now), writer=writer)
    moved: list[str] = []
    for record in preview["trashRecords"]:
        if checkpoint is not None:
            checkpoint()
        if writer is not None:
            writer.assert_owned()
        backup_id = str(record["backupId"])
        if _execute_trash_move(target_root, record, retention_run_id=retention_run_id, at=_utc_iso(now), writer=writer):
            moved.append(backup_id)
    backup_scheduler.record_retention_run(
        retention_run_id,
        policy_id=str(retention.get("retentionPolicyId") or ""),
        target_id=str(target_root),
        status="trashed",
        preview={"trashed": moved},
    )
    return {"retentionRunId": retention_run_id, "trashed": moved, "recoveredTrash": recovered, "preview": {key: preview[key] for key in ("keep", "protected")}}


def finalize_retention(
    retention: dict[str, Any],
    target_root: Path,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
    checkpoint: Callable[[], None] | None = None,
    writer: backup_writer_lease.TargetWriterLease | None = None,
) -> dict[str, Any]:
    """Phase two: permanently delete grace-expired trash after re-checking protections."""
    backup_reconcile.assert_catalog_committed(target_root)
    if writer is not None:
        writer.assert_owned()
    recovered = _recover_trash_journals(target_root, at=_utc_iso(now), writer=writer)
    current = now or datetime.now(tz=timezone.utc)
    grace = timedelta(hours=int(retention["trashGraceHours"]))
    state = backup_catalog.catalog_state(target_root)
    trash_dir = target_root / ".trash"
    deleted: list[str] = []
    kept: list[str] = []
    if trash_dir.is_dir():
        preview = preview_retention(retention, target_root, policy_timezone=policy_timezone, now=current)
        still_protected = {item["backupId"] for item in preview["protected"]} | set(preview["keep"])
        references = _restore_references()
        for entry in sorted(trash_dir.iterdir()):
            if checkpoint is not None:
                checkpoint()
            if not entry.is_dir():
                continue
            backup_id = entry.name
            record = state.get(backup_id)
            if record is None or not record.get("trashed") or record.get("deleted"):
                continue
            trashed_at = _parse_iso(record.get("trashedAt"))
            if trashed_at is None or current - trashed_at < grace:
                kept.append(backup_id)
                continue
            rescued = (
                backup_id in still_protected
                or bool(record.get("pinned"))
                or str(record.get("filename") or "") in references
                or str(record.get("ciphertextSha256") or "") in references
            )
            if rescued:
                backup_catalog.record_restore_from_trash(target_root, backup_id, at=_utc_iso(current), writer=writer)
                _restore_trash_entry(target_root, backup_id)
                kept.append(backup_id)
                continue
            shutil.rmtree(entry, ignore_errors=True)
            backup_catalog.record_delete(target_root, backup_id, retention_run_id=str(record.get("retentionRunId") or ""), at=_utc_iso(current), writer=writer)
            deleted.append(backup_id)
    return {"deleted": deleted, "kept": kept, "recoveredTrash": recovered}


def _restore_trash_entry(target_root: Path, backup_id: str) -> None:
    entry = target_root / ".trash" / backup_id
    if not entry.is_dir():
        return
    for path in entry.iterdir():
        name = path.name
        if name == TRASH_JOURNAL_NAME:
            continue
        stem = name[: -len(".age")] if name.endswith(".age") else ""
        if len(stem) == 64 and all(char in "0123456789abcdef" for char in stem):
            destination = backup_publish.object_path(target_root, stem)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                path.unlink()
            else:
                os.replace(path, destination)
        elif name.endswith(".json"):
            (target_root / "receipts").mkdir(parents=True, exist_ok=True)
            os.replace(path, target_root / "receipts" / name)
        else:
            (target_root / "backups").mkdir(parents=True, exist_ok=True)
            os.replace(path, target_root / "backups" / name)
    (entry / TRASH_JOURNAL_NAME).unlink(missing_ok=True)
    entry.rmdir()


def restore_from_trash(target_root: Path, backup_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Undo a grace-period trash move."""
    state = backup_catalog.catalog_state(target_root)
    record = state.get(backup_id)
    if record is None or not record.get("trashed"):
        raise AppError("Backup is not in the retention trash", code=ErrorCode.NOT_FOUND, status=404)
    _restore_trash_entry(target_root, backup_id)
    backup_catalog.record_restore_from_trash(target_root, backup_id, at=_utc_iso(now))
    return {"restored": True, "backupId": backup_id}


def apply_retention_store(
    retention: dict[str, Any],
    store: Any,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
    checkpoint: Callable[[], None] | None = None,
    writer: backup_writer_lease.TargetWriterLease | None = None,
) -> dict[str, Any]:
    """Logical trash for remote targets: catalog hide only, no object copy."""
    if writer is not None:
        writer.assert_owned()
    current = now or datetime.now(tz=timezone.utc)
    state = backup_catalog.catalog_state_store(store)
    # Build a lightweight preview against store state.
    records = [record for record in state.values() if not record.get("deleted") and not record.get("trashed")]
    records.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    keep_last = int(retention.get("keepLast") or 0)
    keep_ids = {str(item.get("backupId")) for item in records[:keep_last]}
    for record in records:
        if record.get("pinned"):
            keep_ids.add(str(record.get("backupId")))
    trash_ids = [str(item.get("backupId")) for item in records if str(item.get("backupId")) not in keep_ids]
    retention_run_id = f"rr_{uuid.uuid4().hex[:12]}"
    moved: list[str] = []
    for backup_id in trash_ids:
        if checkpoint is not None:
            checkpoint()
        if writer is not None:
            writer.assert_owned()
        backup_catalog._append_entry_store(
            store,
            "trash",
            {"backupId": backup_id, "retentionRunId": retention_run_id, "trashedAt": _utc_iso(current)},
            writer=writer,
        )
        moved.append(backup_id)
    return {"retentionRunId": retention_run_id, "trashed": moved, "recoveredTrash": [], "preview": {"keep": sorted(keep_ids), "protected": []}}


def finalize_retention_store(
    retention: dict[str, Any],
    store: Any,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
    checkpoint: Callable[[], None] | None = None,
    writer: backup_writer_lease.TargetWriterLease | None = None,
) -> dict[str, Any]:
    """Physical GC of unreferenced Age objects after trash grace on remote targets."""
    del policy_timezone
    if writer is not None:
        writer.assert_owned()
    current = now or datetime.now(tz=timezone.utc)
    grace = timedelta(hours=int(retention.get("trashGraceHours") or 24))
    state = backup_catalog.catalog_state_store(store)
    deleted: list[str] = []
    kept: list[str] = []
    live_digests = {
        str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
        for record in state.values()
        if not record.get("deleted") and not record.get("trashed")
    }
    # Protect objects held by active restores.
    hold_digests = _restore_hold_digests(store)
    for backup_id, record in list(state.items()):
        if checkpoint is not None:
            checkpoint()
        if not record.get("trashed") or record.get("deleted"):
            continue
        trashed_at = _parse_iso(record.get("trashedAt"))
        if trashed_at is None or current - trashed_at < grace:
            kept.append(backup_id)
            continue
        digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
        if digest and digest in live_digests:
            kept.append(backup_id)
            continue
        if digest and digest in hold_digests:
            kept.append(backup_id)
            continue
        if writer is not None:
            writer.assert_owned()
        if digest:
            from deepseek_infra.infra.workspace.backup_target_store import object_key

            try:
                store.delete_if_match(object_key(digest))
            except AppError:
                pass
        backup_catalog._append_entry_store(
            store,
            "delete",
            {"backupId": backup_id, "retentionRunId": str(record.get("retentionRunId") or ""), "deletedAt": _utc_iso(current)},
            writer=writer,
        )
        deleted.append(backup_id)
    return {"deleted": deleted, "kept": kept, "recoveredTrash": []}


def _restore_hold_digests(store: Any) -> set[str]:
    from deepseek_infra.infra.workspace.backup_target_store import read_json

    digests: set[str] = set()
    cursor = None
    while True:
        page = store.list_objects("holds/restore/", cursor=cursor)
        for meta in page.objects:
            if not str(meta.key).endswith(".json"):
                continue
            data = read_json(store, meta.key)
            if not isinstance(data, dict):
                continue
            digest = str(data.get("objectDigest") or "")
            if digest:
                digests.add(digest)
        if not page.cursor:
            break
        cursor = page.cursor
    return digests
