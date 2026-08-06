"""Filesystem backup target registry (4.4.4).

Targets are recognized by a marker file (``.deepseek-infra-backup-target.json``)
rather than by drive letter or mount path, so a USB disk that moves from
``E:`` to ``F:`` is still the same target. Location safety rules keep backups
out of the runtime root, the source repository, contributor roots, restore
staging, temp dirs and other targets; the marker is re-verified before every
publish and retention pass.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backups

TARGET_SCHEMA_VERSION = 1
TARGET_MARKER_NAME = ".deepseek-infra-backup-target.json"
BACKUP_TARGET_DIR = config.ROOT / ".backup-targets"

_TARGET_ID = re.compile(r"^target_[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_REPARSE_POINT = 0x400


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _registry_path(target_id: str) -> Path:
    return BACKUP_TARGET_DIR / f"{target_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def installation_id() -> str:
    path = BACKUP_TARGET_DIR / "installation.id"
    try:
        existing = path.read_text(encoding="ascii").strip()
        if existing:
            return existing
    except OSError:
        pass
    value = f"inst_{secrets.token_hex(8)}"
    BACKUP_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")
    return value


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            attributes = os.lstat(path).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attributes & _WINDOWS_REPARSE_POINT)
    return False


def _has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        if _is_reparse_point(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _containment_violation(resolved: Path, *, exclude_target_id: str | None = None) -> str | None:
    runtime_root = config.ROOT.resolve()
    forbidden: list[tuple[str, Path]] = [
        ("runtime root", runtime_root),
        ("source repository", _repo_root()),
        ("restore staging", backups.RESTORE_DIR.resolve()),
        ("managed backup directory", backups.BACKUP_DIR.resolve()),
        ("system temporary directory", Path(tempfile.gettempdir()).resolve()),
    ]
    for name, base in forbidden:
        if resolved == base or resolved.is_relative_to(base):
            return f"target is inside the {name}"
    for contributor in backups._registered_contributors():
        path_getter = getattr(contributor, "path_getter", None)
        if path_getter is None:
            continue
        root = Path(path_getter()).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return f"target is inside contributor root {contributor.contributor_id}"
    for other in list_targets():
        if exclude_target_id is not None and other.get("targetId") == exclude_target_id:
            continue
        other_path = Path(str(other.get("path") or "")).resolve()
        if resolved == other_path or resolved.is_relative_to(other_path) or other_path.is_relative_to(resolved):
            return f"target overlaps registered target {other.get('targetId')}"
    return None


def _resolve_basic(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise AppError("Backup target path must be absolute", code=ErrorCode.INVALID_PAYLOAD)
    if not candidate.is_dir():
        raise AppError("Backup target directory does not exist", code=ErrorCode.INVALID_PAYLOAD)
    if _has_reparse_component(candidate):
        raise AppError("Backup target must not sit behind a symlink or reparse point", code=ErrorCode.INVALID_PAYLOAD)
    return candidate.resolve()


def validate_target_location(path: Path, *, exclude_target_id: str | None = None) -> Path:
    """Resolve and validate a target directory; raises AppError on violations."""
    resolved = _resolve_basic(Path(path))
    violation = _containment_violation(resolved, exclude_target_id=exclude_target_id)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    return resolved


def init_target(path: Path | str, *, label: str = "") -> dict[str, Any]:
    """Initialize a directory as a backup target and register it."""
    resolved = _resolve_basic(Path(path))
    marker = resolved / TARGET_MARKER_NAME
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError("Backup target marker is unreadable", code=ErrorCode.INVALID_PAYLOAD) from exc
        if isinstance(existing, dict) and existing.get("targetId"):
            target_id = str(existing["targetId"])
            violation = _containment_violation(resolved, exclude_target_id=target_id)
            if violation:
                raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
            return _register(resolved, target_id, str(existing.get("targetNonce") or ""), label=label, created_at=str(existing.get("createdAt") or ""))
        raise AppError("Backup target marker is invalid", code=ErrorCode.INVALID_PAYLOAD)
    violation = _containment_violation(resolved)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    target_id = f"target_{secrets.token_hex(6)}"
    nonce = secrets.token_hex(16)
    marker_payload = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "targetNonce": nonce,
        "createdAt": _utc_iso(),
        "ownerInstallationId": installation_id(),
    }
    _atomic_write_json(marker, marker_payload)
    return _register(resolved, target_id, nonce, label=label, created_at=marker_payload["createdAt"])


def _register(resolved: Path, target_id: str, nonce: str, *, label: str, created_at: object = "") -> dict[str, Any]:
    if not _TARGET_ID.match(target_id):
        raise AppError("Backup target marker carries an invalid target id", code=ErrorCode.INVALID_PAYLOAD)
    record = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "path": str(resolved),
        "targetNonce": nonce,
        "label": label,
        "createdAt": str(created_at or "") or _utc_iso(),
        "registeredAt": _utc_iso(),
    }
    _atomic_write_json(_registry_path(target_id), record)
    return record


def get_target(target_id: str) -> dict[str, Any]:
    path = _registry_path(str(target_id or ""))
    if not path.is_file():
        raise AppError("Backup target not found", code=ErrorCode.NOT_FOUND, status=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Backup target registry is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
    return data


def list_targets() -> list[dict[str, Any]]:
    if not BACKUP_TARGET_DIR.is_dir():
        return []
    targets: list[dict[str, Any]] = []
    for path in sorted(BACKUP_TARGET_DIR.glob("target_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("targetId"):
            targets.append(data)
    return targets


def delete_target(target_id: str) -> dict[str, Any]:
    record = get_target(target_id)
    _registry_path(target_id).unlink(missing_ok=True)
    return {"deleted": True, "targetId": record["targetId"]}


def probe_target(target_id: str) -> dict[str, Any]:
    """Re-verify marker and location; never raises for offline targets."""
    try:
        path = verify_target_ready(target_id)
    except AppError as exc:
        return {"targetId": target_id, "ready": False, "status": "blocked-target-unavailable", "detail": str(exc)}
    return {"targetId": target_id, "ready": True, "status": "ok", "path": str(path)}


def verify_target_ready(target_id: str) -> Path:
    """Return the validated target directory or raise blocked-target-unavailable.

    Called before every publish and retention pass so a swapped mount or marker
    stops writes immediately.
    """
    record = get_target(target_id)
    registered_path = Path(str(record.get("path") or ""))
    try:
        resolved = validate_target_location(registered_path, exclude_target_id=str(record.get("targetId") or ""))
    except AppError as exc:
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    marker = resolved / TARGET_MARKER_NAME
    if not marker.is_file():
        raise AppError("blocked-target-unavailable: target marker is missing", code=ErrorCode.INVALID_REQUEST, status=409)
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("blocked-target-unavailable: target marker is unreadable", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    if not isinstance(marker_data, dict) or marker_data.get("targetId") != record.get("targetId") or marker_data.get("targetNonce") != record.get("targetNonce"):
        raise AppError("blocked-target-unavailable: target marker was replaced", code=ErrorCode.INVALID_REQUEST, status=409)
    return resolved
