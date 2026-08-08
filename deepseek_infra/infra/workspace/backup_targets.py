"""Filesystem backup target registry with lineage checkpoints (4.4.5).

Targets are recognized by a marker file (``.deepseek-infra-backup-target.json``)
rather than by drive letter or mount path, so a USB disk that moves from
``E:`` to ``F:`` is still the same target. Location safety rules keep backups
out of the runtime root, the source repository, contributor roots, restore
staging, temp dirs and other targets; the marker is re-verified before every
publish and retention pass.

Marker v2 carries an ``incarnationId``, ``ownerInstallationId`` and the
target's commit head (``targetGeneration`` / ``latestCommitHash``). A trusted
checkpoint in ``.backup-targets/<targetId>.checkpoint.json`` records what this
installation last saw, so reconnecting a rolled-back disk raises
``target-rollback-detected``, a same-generation head or incarnation change
raises ``target-fork-detected``, and one target id alive at two locations
raises ``target-clone-detected``. Anomalies must be resolved explicitly —
adopt the branch or register it as new — before writes resume; read-only
listing and scrubbing stay allowed.
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

TARGET_SCHEMA_VERSION = 2
TARGET_MARKER_NAME = ".deepseek-infra-backup-target.json"
TARGET_GENESIS_HASH = "0" * 64
BACKUP_TARGET_DIR = config.ROOT / ".backup-targets"

_TARGET_ID = re.compile(r"^target_[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_REPARSE_POINT = 0x400


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _registry_path(target_id: str) -> Path:
    return BACKUP_TARGET_DIR / f"{target_id}.json"


def _checkpoint_path(target_id: str) -> Path:
    return BACKUP_TARGET_DIR / f"{target_id}.checkpoint.json"


def _read_checkpoint(target_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_checkpoint_path(target_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_checkpoint(target_id: str, marker_data: dict[str, Any]) -> None:
    _atomic_write_json(
        _checkpoint_path(target_id),
        {
            "incarnationId": str(marker_data.get("incarnationId") or ""),
            "lastSeenGeneration": int(marker_data.get("targetGeneration") or 0),
            "lastSeenCommitHash": str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH),
            "lastSeenAt": _utc_iso(),
        },
    )


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
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError:
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
            try:
                registration = get_target(target_id)
            except AppError:
                registration = None
            if registration is not None:
                registered_path = Path(str(registration.get("path") or ""))
                if registered_path != resolved and registered_path.exists():
                    raise AppError(
                        "target-clone-detected: target id is registered at another location that still exists; adopt one branch or register it as new",
                        code=ErrorCode.INVALID_REQUEST,
                        status=409,
                    )
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
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "createdAt": _utc_iso(),
    }
    _atomic_write_json(marker, marker_payload)
    record = _register(resolved, target_id, nonce, label=label, created_at=marker_payload["createdAt"])
    _write_checkpoint(target_id, marker_payload)
    return record


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
    """Re-verify marker, location and lineage; never raises for offline targets."""
    try:
        path = verify_target_ready(target_id)
    except AppError as exc:
        detail = str(exc)
        status = "blocked-target-unavailable"
        for code in ("target-rollback-detected", "target-fork-detected", "target-clone-detected"):
            if code in detail:
                status = code
                break
        return {"targetId": target_id, "ready": False, "status": status, "detail": detail}
    return {"targetId": target_id, "ready": True, "status": "ok", "path": str(path)}


def verify_target_ready(target_id: str, *, write_intent: bool = True) -> Path:
    """Return the validated target directory or raise blocked-target-unavailable.

    Called before every publish and retention pass so a swapped mount or marker
    stops writes immediately. With ``write_intent`` the target lineage is also
    checked against the trusted checkpoint — rollbacks, forks and incarnation
    changes raise their explicit anomalies — while read-only callers get the
    marker-validated path without checkpoint side effects.
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
    if not write_intent:
        return resolved
    return _check_lineage(str(record["targetId"]), resolved, marker_data)


def _check_lineage(target_id: str, resolved: Path, marker_data: dict[str, Any]) -> Path:
    marker = resolved / TARGET_MARKER_NAME
    if not marker_data.get("incarnationId"):
        from deepseek_infra.infra.workspace import backup_publish

        latest = backup_publish.latest_commit(resolved)
        upgraded = {
            **marker_data,
            "schemaVersion": TARGET_SCHEMA_VERSION,
            "incarnationId": f"inc_{secrets.token_hex(8)}",
            "ownerInstallationId": str(marker_data.get("ownerInstallationId") or installation_id()),
            "targetGeneration": int(latest["targetGeneration"]) if latest is not None else 0,
            "latestCommitHash": str(latest["commitHash"]) if latest is not None else TARGET_GENESIS_HASH,
        }
        _atomic_write_json(marker, upgraded)
        _write_checkpoint(target_id, upgraded)
        return resolved
    checkpoint = _read_checkpoint(target_id)
    if checkpoint is None:
        _write_checkpoint(target_id, marker_data)
        return resolved
    generation = int(marker_data.get("targetGeneration") or 0)
    seen_generation = int(checkpoint.get("lastSeenGeneration") or 0)
    if generation < seen_generation:
        raise AppError(
            f"target-rollback-detected: target generation {generation} is behind the trusted generation {seen_generation}; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    head = str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH)
    seen_head = str(checkpoint.get("lastSeenCommitHash") or TARGET_GENESIS_HASH)
    if str(marker_data.get("incarnationId") or "") != str(checkpoint.get("incarnationId") or ""):
        raise AppError(
            "target-fork-detected: target incarnation changed; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    if generation == seen_generation and head != seen_head:
        raise AppError(
            "target-fork-detected: same target generation but a different commit head; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    _write_checkpoint(target_id, marker_data)
    return resolved


def record_target_head(root: Path, *, target_id: str, generation: int, commit_hash: str) -> None:
    """Advance the marker's commit head and the trusted checkpoint after a slot commit."""
    marker = root / TARGET_MARKER_NAME
    if not marker.is_file():
        return
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(marker_data, dict):
        return
    marker_data = {
        **marker_data,
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "incarnationId": str(marker_data.get("incarnationId") or f"inc_{secrets.token_hex(8)}"),
        "ownerInstallationId": str(marker_data.get("ownerInstallationId") or installation_id()),
        "targetGeneration": int(generation),
        "latestCommitHash": commit_hash,
    }
    _atomic_write_json(marker, marker_data)
    _write_checkpoint(target_id, marker_data)


def adopt_target_incarnation(target_id: str) -> dict[str, Any]:
    """Accept the currently attached disk as the authoritative branch of a fork."""
    record = get_target(target_id)
    resolved = validate_target_location(Path(str(record.get("path") or "")), exclude_target_id=target_id)
    marker = resolved / TARGET_MARKER_NAME
    if not marker.is_file():
        raise AppError("blocked-target-unavailable: target marker is missing", code=ErrorCode.INVALID_REQUEST, status=409)
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("blocked-target-unavailable: target marker is unreadable", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    if not isinstance(marker_data, dict) or marker_data.get("targetId") != record.get("targetId") or marker_data.get("targetNonce") != record.get("targetNonce"):
        raise AppError("blocked-target-unavailable: target marker was replaced", code=ErrorCode.INVALID_REQUEST, status=409)
    adopted = {
        **marker_data,
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": int(marker_data.get("targetGeneration") or 0),
        "latestCommitHash": str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH),
    }
    _atomic_write_json(marker, adopted)
    _write_checkpoint(target_id, adopted)
    return {"targetId": target_id, "adopted": True, "incarnationId": adopted["incarnationId"]}


def reinitialize_target(path: Path | str, *, label: str = "") -> dict[str, Any]:
    """Register a directory as a brand-new target with a fresh lineage."""
    resolved = _resolve_basic(Path(path))
    marker = resolved / TARGET_MARKER_NAME
    previous_id: str | None = None
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict) and existing.get("targetId"):
            previous_id = str(existing["targetId"])
    violation = _containment_violation(resolved, exclude_target_id=previous_id)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    target_id = f"target_{secrets.token_hex(6)}"
    nonce = secrets.token_hex(16)
    marker_payload = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "targetNonce": nonce,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "createdAt": _utc_iso(),
    }
    _atomic_write_json(marker, marker_payload)
    record = _register(resolved, target_id, nonce, label=label, created_at=marker_payload["createdAt"])
    _write_checkpoint(target_id, marker_payload)
    return record
