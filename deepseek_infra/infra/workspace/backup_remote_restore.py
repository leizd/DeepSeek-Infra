"""Stream encrypted restores from remote backup targets (4.4.6).

Downloads Age ciphertext via range GET into local restore staging, verifies
SHA-256, then hands off to the existing federated restore transaction. Recovery
Identities never leave the local process; the target only ever sees ciphertext.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
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


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def restore_from_target(
    *,
    target_id: str,
    backup_id: str,
    client: Any | None = None,
) -> dict[str, Any]:
    """Download a committed backup object and stage it for inspect/unlock."""
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
    receipt = read_json(store, receipt_key(backup_id))
    if receipt is None:
        raise AppError("Backup receipt not found on target", code=ErrorCode.NOT_FOUND, status=404)
    digest = str(receipt.get("objectDigest") or receipt.get("ciphertextSha256") or "")
    if len(digest) != 64:
        raise AppError("Backup receipt is missing object digest", code=ErrorCode.INVALID_REQUEST, status=409)
    # Confirm a commit marker references this backup.
    markers = []
    if target.root is not None:
        markers = backup_publish.read_commit_markers(target.root)
    else:
        latest = backup_publish.latest_commit_store(store)
        if latest is not None:
            markers = [latest]
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
    except AppError:  # pragma: no cover - hold already present is fine
        pass

    staging_root = backups.RESTORE_DIR / restore_id
    staging_root.mkdir(parents=True, exist_ok=True)
    ciphertext_path = staging_root / str(receipt.get("filename") or f"{backup_id}.age")
    obj = object_key(digest)
    meta = store.stat(obj)
    if meta is None:
        raise AppError("Backup ciphertext object is missing on target", code=ErrorCode.NOT_FOUND, status=404)

    offset = ciphertext_path.stat().st_size if ciphertext_path.is_file() else 0
    mode = "ab" if offset else "wb"
    hasher = hashlib.sha256()
    if offset:
        with ciphertext_path.open("rb") as existing:
            while True:
                chunk = existing.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
    with ciphertext_path.open(mode) as handle:
        remaining = max(0, int(meta.size) - offset)
        position = offset
        while remaining > 0:
            piece = store.get_bytes(obj, offset=position, length=min(1024 * 1024, remaining))
            if not piece:
                break
            handle.write(piece)
            hasher.update(piece)
            position += len(piece)
            remaining -= len(piece)
        handle.flush()
        os.fsync(handle.fileno())
    if hasher.hexdigest() != digest:
        raise AppError("Downloaded backup digest mismatch", code=ErrorCode.INTERNAL, status=500)

    # Hand off into existing inspect flow via archive path bookkeeping.
    session = {
        "schemaVersion": 1,
        "restoreId": restore_id,
        "source": "remote-target",
        "targetId": target_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "filename": ciphertext_path.name,
        "size": ciphertext_path.stat().st_size,
        "ciphertextPath": str(ciphertext_path),
        "holdKey": restore_hold_key(restore_id),
        "createdAt": _utc_iso(),
    }
    session_path = staging_root / "remote-source.json"
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "restoreId": restore_id,
        "targetId": target_id,
        "backupId": backup_id,
        "filename": ciphertext_path.name,
        "size": session["size"],
        "objectDigest": digest,
        "path": str(ciphertext_path),
        "hold": hold,
        "next": "inspect-or-unlock",
    }


def release_restore_hold(store: Any, restore_id: str) -> None:
    try:
        store.delete_if_match(restore_hold_key(restore_id))
    except AppError:  # pragma: no cover
        pass
