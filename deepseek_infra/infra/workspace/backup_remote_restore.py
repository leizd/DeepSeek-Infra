"""Stream encrypted restores from remote backup targets (4.4.7).

Downloads Age ciphertext via range GET into a durable restore session that
survives process restarts. Callers create a session once, then call
:func:`fetch_restore_session` until phase becomes ``fetched``. Recovery
Identities never leave the local process.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_publish, backup_targets, backups
from deepseek_infra.infra.workspace.backup_target_store import (
    object_key,
    put_json_if_absent,
    read_json,
    receipt_key,
    restore_hold_key,
)

HOLD_TTL_SECONDS = 6 * 3600
SESSION_SCHEMA_VERSION = 1


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _session_dir(restore_id: str) -> Path:
    return backups.RESTORE_DIR / restore_id


def _session_path(restore_id: str) -> Path:
    return _session_dir(restore_id) / "remote-fetch.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_restore_session(restore_id: str) -> dict[str, Any] | None:
    path = _session_path(restore_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def create_restore_from_target(*, target_id: str, backup_id: str, client: Any | None = None) -> dict[str, Any]:
    """Create a durable remote restore session (phase=fetching)."""
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
    receipt = read_json(store, receipt_key(backup_id))
    if receipt is None:
        raise AppError("Backup receipt not found on target", code=ErrorCode.NOT_FOUND, status=404)
    digest = str(receipt.get("objectDigest") or receipt.get("ciphertextSha256") or "")
    if len(digest) != 64:
        raise AppError("Backup receipt is missing object digest", code=ErrorCode.INVALID_REQUEST, status=409)
    markers: list[dict[str, Any]] = []
    if target.root is not None:
        markers = backup_publish.read_commit_markers(target.root)
    else:
        cursor = None
        while True:
            page = store.list_objects("commits/", cursor=cursor)
            for item in page.objects:
                if item.key.endswith(".json"):
                    data = read_json(store, item.key)
                    if isinstance(data, dict):
                        markers.append(data)
            if not page.cursor:
                break
            cursor = page.cursor
    if not any(str(marker.get("backupId") or "") == backup_id and str(marker.get("objectDigest") or "") == digest for marker in markers):
        raise AppError("Backup has no formal slot commit on target", code=ErrorCode.INVALID_REQUEST, status=409)
    obj = object_key(digest)
    meta = store.stat(obj)
    if meta is None:
        raise AppError("Backup ciphertext object is missing on target", code=ErrorCode.NOT_FOUND, status=404)

    restore_id = f"restore_{uuid.uuid4().hex[:12]}"
    hold = {
        "schemaVersion": 1,
        "restoreId": restore_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "targetId": target_id,
        "createdAt": _utc_iso(),
        "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
    }
    try:
        put_json_if_absent(store, restore_hold_key(restore_id), hold)
    except AppError:  # pragma: no cover
        pass

    staging_root = _session_dir(restore_id)
    staging_root.mkdir(parents=True, exist_ok=True)
    filename = str(receipt.get("filename") or f"{backup_id}.age")
    session = {
        "schemaVersion": SESSION_SCHEMA_VERSION,
        "restoreId": restore_id,
        "source": "remote-target",
        "targetId": target_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "filename": filename,
        "expectedBytes": int(meta.size),
        "downloadedBytes": 0,
        "remoteETag": meta.etag,
        "remoteVersionId": meta.version_id,
        "holdKey": restore_hold_key(restore_id),
        "ciphertextPath": str(staging_root / filename),
        "phase": "fetching",
        "createdAt": _utc_iso(),
        "updatedAt": _utc_iso(),
    }
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "phase": "fetching",
        "downloadedBytes": 0,
        "expectedBytes": int(meta.size),
        "targetId": target_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "hold": hold,
    }


def fetch_restore_session(restore_id: str, *, client: Any | None = None, max_bytes: int | None = None) -> dict[str, Any]:
    """Idempotently continue a durable remote download until complete."""
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    if str(session.get("phase") or "") == "fetched":
        return {
            "restoreId": restore_id,
            "phase": "fetched",
            "downloadedBytes": int(session.get("downloadedBytes") or 0),
            "expectedBytes": int(session.get("expectedBytes") or 0),
            "path": str(session.get("ciphertextPath") or ""),
            "next": "inspect-or-unlock",
        }
    target_id = str(session.get("targetId") or "")
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
    digest = str(session.get("objectDigest") or "")
    obj = object_key(digest)
    meta = store.stat(obj)
    if meta is None:
        raise AppError("Backup ciphertext object is missing on target", code=ErrorCode.NOT_FOUND, status=404)
    expected_etag = str(session.get("remoteETag") or "")
    if expected_etag and meta.etag and meta.etag != expected_etag:
        raise AppError("remote object changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
    expected_version = session.get("remoteVersionId")
    if expected_version and meta.version_id and meta.version_id != expected_version:
        raise AppError("remote object version changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)

    ciphertext_path = Path(str(session.get("ciphertextPath") or (_session_dir(restore_id) / str(session.get("filename") or "package.age"))))
    ciphertext_path.parent.mkdir(parents=True, exist_ok=True)
    offset = ciphertext_path.stat().st_size if ciphertext_path.is_file() else 0
    hasher = hashlib.sha256()
    if offset:
        with ciphertext_path.open("rb") as existing:
            while True:
                chunk = existing.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
    mode = "ab" if offset else "wb"
    expected = int(session.get("expectedBytes") or meta.size)
    remaining = max(0, expected - offset)
    budget = remaining if max_bytes is None else min(remaining, max(0, int(max_bytes)))
    with ciphertext_path.open(mode) as handle:
        position = offset
        consumed = 0
        while consumed < budget:
            chunk_len = min(1024 * 1024, budget - consumed)
            piece = store.get_bytes(obj, offset=position, length=chunk_len)
            if not piece:
                break
            handle.write(piece)
            hasher.update(piece)
            position += len(piece)
            consumed += len(piece)
        handle.flush()
        os.fsync(handle.fileno())
    downloaded = ciphertext_path.stat().st_size if ciphertext_path.is_file() else 0
    session["downloadedBytes"] = downloaded
    session["updatedAt"] = _utc_iso()
    if downloaded >= expected:
        if hasher.hexdigest() != digest:
            raise AppError("Downloaded backup digest mismatch", code=ErrorCode.INTERNAL, status=500)
        session["phase"] = "fetched"
        _atomic_write_json(_session_path(restore_id), session)
        return {
            "restoreId": restore_id,
            "phase": "fetched",
            "downloadedBytes": downloaded,
            "expectedBytes": expected,
            "path": str(ciphertext_path),
            "objectDigest": digest,
            "next": "inspect-or-unlock",
        }
    session["phase"] = "fetching"
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "phase": "fetching",
        "downloadedBytes": downloaded,
        "expectedBytes": expected,
        "path": str(ciphertext_path),
        "objectDigest": digest,
    }


def restore_from_target(*, target_id: str, backup_id: str, client: Any | None = None) -> dict[str, Any]:
    """Compatibility helper: create session and fetch to completion in one call."""
    created = create_restore_from_target(target_id=target_id, backup_id=backup_id, client=client)
    restore_id = str(created["restoreId"])
    while True:
        result = fetch_restore_session(restore_id, client=client)
        if str(result.get("phase") or "") == "fetched":
            return {
                **result,
                "targetId": target_id,
                "backupId": backup_id,
                "filename": Path(str(result.get("path") or "")).name,
                "size": int(result.get("downloadedBytes") or 0),
                "hold": {"restoreId": restore_id},
            }


def release_restore_hold(store: Any, restore_id: str) -> None:
    try:
        store.delete_if_match(restore_hold_key(restore_id))
    except AppError:  # pragma: no cover
        pass
