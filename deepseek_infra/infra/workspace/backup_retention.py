"""Retention governance for scheduled backups (4.4.4).

Grandfather-father-son retention buckets backups in the *policy* timezone,
never deletes pinned, restore-referenced or minimum-healthy copies, runs only
after a successful publish, and deletes in two phases with a trash grace
period so a retention bug can be undone.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_catalog, backup_scheduler, backups
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
    healthy = [record for record in records if (target_root / "backups" / str(record.get("filename") or "")).is_file()]
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
    return {
        "policyTimezone": policy_timezone,
        "evaluatedAt": _utc_iso(current),
        "keep": sorted(keep),
        "trash": [str(record["backupId"]) for record in trash],
        "trashRecords": trash,
        "protected": [{"backupId": key, "reason": reason} for key, reason in sorted(protected.items())],
    }


def apply_retention(
    retention: dict[str, Any],
    target_root: Path,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Phase one: move prune candidates into ``.trash`` and record intent."""
    preview = preview_retention(retention, target_root, policy_timezone=policy_timezone, now=now)
    retention_run_id = f"rr_{uuid.uuid4().hex[:12]}"
    backup_scheduler.record_retention_run(
        retention_run_id,
        policy_id=str(retention.get("retentionPolicyId") or ""),
        target_id=str(target_root),
        status="preview",
        preview={key: preview[key] for key in ("keep", "trash", "protected")},
    )
    trash_dir = target_root / ".trash"
    moved: list[str] = []
    for record in preview["trashRecords"]:
        backup_id = str(record["backupId"])
        filename = str(record.get("filename") or "")
        if not filename or "/" in filename or "\\" in filename:
            continue
        destination = trash_dir / backup_id
        destination.mkdir(parents=True, exist_ok=True)
        for base, suffix in ((target_root / "backups", ""), (target_root / "receipts", ".receipt.json")):
            source = base / f"{filename}{suffix}"
            if source.is_file():
                os.replace(source, destination / f"{filename}{suffix}")
        backup_catalog.record_trash(target_root, backup_id, retention_run_id=retention_run_id, at=_utc_iso(now))
        moved.append(backup_id)
    backup_scheduler.record_retention_run(
        retention_run_id,
        policy_id=str(retention.get("retentionPolicyId") or ""),
        target_id=str(target_root),
        status="trashed",
        preview={"trashed": moved},
    )
    return {"retentionRunId": retention_run_id, "trashed": moved, "preview": {key: preview[key] for key in ("keep", "protected")}}


def finalize_retention(
    retention: dict[str, Any],
    target_root: Path,
    *,
    policy_timezone: str = "UTC",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Phase two: permanently delete grace-expired trash after re-checking protections."""
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
                backup_catalog.record_restore_from_trash(target_root, backup_id, at=_utc_iso(current))
                _restore_trash_entry(target_root, backup_id)
                kept.append(backup_id)
                continue
            shutil.rmtree(entry, ignore_errors=True)
            backup_catalog.record_delete(target_root, backup_id, retention_run_id=str(record.get("retentionRunId") or ""), at=_utc_iso(current))
            deleted.append(backup_id)
    return {"deleted": deleted, "kept": kept}


def _restore_trash_entry(target_root: Path, backup_id: str) -> None:
    entry = target_root / ".trash" / backup_id
    if not entry.is_dir():
        return
    for path in entry.iterdir():
        if path.name.endswith(".receipt.json"):
            os.replace(path, target_root / "receipts" / path.name)
        else:
            os.replace(path, target_root / "backups" / path.name)
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
