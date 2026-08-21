"""Rebuildable ciphertext reference index for scale-safe retirement/GC (4.5.9).

Formal Receipt v4 / Commit v4 remain the source of truth. This module only
accelerates live-reference queries so retirement does not scan entire remote
receipt history on every GC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control
from deepseek_infra.infra.workspace.backup_target_store import object_key, read_json


def _read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def receipt_payload_entries(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract payload object keys and optional digests/sizes from a Receipt."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(key: str | None, *, digest: str | None = None, size: int | None = None) -> None:
        if not key or key in seen:
            return
        seen.add(key)
        entries.append(
            {
                "objectKey": key,
                "ciphertextDigest": digest,
                "sizeBytes": int(size) if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
            }
        )

    filename = receipt.get("filename")
    if filename:
        _add(str(filename), digest=str(receipt.get("objectDigest") or "") or None)
    object_digest = str(receipt.get("objectDigest") or "")
    if object_digest:
        _add(object_key(object_digest), digest=object_digest)
    for item in list(receipt.get("objects") or []) + list(receipt.get("components") or []):
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("key")
        digest = str(item.get("digest") or item.get("ciphertextDigest") or "") or None
        size = item.get("size") or item.get("sizeBytes") or item.get("ciphertextBytes")
        if path:
            _add(str(path), digest=digest, size=size if isinstance(size, int) else None)
        if digest:
            _add(object_key(digest), digest=digest, size=size if isinstance(size, int) else None)
            _add(f"ciphertext/sha256/{digest}", digest=digest, size=size if isinstance(size, int) else None)
    return entries


def index_receipt_objects(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    receipt: dict[str, Any],
    ref_state: str = "live",
) -> int:
    """Index one formal receipt's payload objects into the control-plane index."""
    count = 0
    for entry in receipt_payload_entries(receipt):
        backup_control.put_recovery_object_ref(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            object_key=str(entry["objectKey"]),
            ref_state=ref_state,
            size_bytes=int(entry.get("sizeBytes") or 0),
            ciphertext_digest=entry.get("ciphertextDigest"),
        )
        count += 1
    return count


def apply_retirement_to_index(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    receipt: dict[str, Any] | None = None,
) -> int:
    """Mark indexed refs for a recovery point as retired (live → retired)."""
    refs = backup_control.list_recovery_object_refs(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        limit=20000,
    )
    if not refs and receipt is not None:
        index_receipt_objects(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
            ref_state="live",
        )
        refs = backup_control.list_recovery_object_refs(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            limit=20000,
        )
    changed = 0
    for ref in refs:
        if str(ref.get("refState") or "") == "retired":
            continue
        backup_control.put_recovery_object_ref(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            object_key=str(ref["objectKey"]),
            ref_state="retired",
            size_bytes=int(ref.get("sizeBytes") or 0),
            ciphertext_digest=ref.get("ciphertextDigest"),
        )
        changed += 1
    return changed


def retained_payload_keys_from_index(target_id: str, *, retiring_backup_id: str) -> set[str] | None:
    """Return live object keys from the index, or None when the index is empty."""
    refs = backup_control.list_recovery_object_refs(target_id=target_id, ref_state="live", limit=20000)
    if not refs:
        return None
    retained: set[str] = set()
    for ref in refs:
        if str(ref.get("backupId") or "") == retiring_backup_id:
            continue
        retained.add(str(ref["objectKey"]))
    return retained


def gc_candidate_keys(target_id: str, *, limit: int = 500) -> list[str]:
    return [str(item["objectKey"]) for item in backup_control.list_target_objects(target_id, gc_candidates_only=True, limit=limit)]


def rebuild_index_from_target(target: Any) -> dict[str, int]:
    """Rebuild the object index for one target from formal Receipt truth.

    When a valid retirement marker is present the ref is stored as retired;
    otherwise it is live. Missing markers keep payload protected.
    """
    from deepseek_infra.infra.workspace import backup_retirement

    target_id = str(getattr(target, "target_id", "") or "")
    if not target_id:
        raise AppError("target_id required for index rebuild", code=ErrorCode.INVALID_REQUEST, status=400)
    backup_control.clear_target_object_index(target_id)
    live = 0
    retired = 0
    scanned = 0

    def _handle_receipt(receipt_bytes: bytes, receipt: dict[str, Any], stem: str) -> None:
        nonlocal live, retired, scanned
        scanned += 1
        policy_id = str(receipt.get("policyId") or "")
        backup_id = str(receipt.get("backupId") or stem)
        if not policy_id or not backup_id:
            return
        is_retired = backup_retirement._receipt_has_valid_retirement_marker(target, receipt_bytes, receipt)
        state = "retired" if is_retired else "live"
        index_receipt_objects(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
            ref_state=state,
        )
        if is_retired:
            retired += 1
        else:
            live += 1

    if getattr(target, "root", None) is not None:
        receipts_dir = Path(target.root) / "receipts"
        candidates = sorted(receipts_dir.rglob("*.json")) if receipts_dir.is_dir() else []
        for path in candidates:
            receipt_bytes = path.read_bytes()
            receipt = _read_json_bytes(receipt_bytes)
            if receipt is None:
                continue
            _handle_receipt(receipt_bytes, receipt, path.stem)
    elif getattr(target, "store", None) is not None:
        cursor: str | None = None
        while True:
            page = target.store.list_objects("receipts/", cursor=cursor, limit=200)
            for meta in page.objects:
                receipt_bytes = target.store.get_bytes(meta.key)
                receipt = _read_json_bytes(receipt_bytes)
                if receipt is None or receipt_bytes is None:
                    continue
                _handle_receipt(receipt_bytes, receipt, Path(meta.key).stem)
            if page.cursor is None:
                break
            cursor = page.cursor
    return {"scannedReceipts": scanned, "liveRecoveryPoints": live, "retiredRecoveryPoints": retired}


def reconcile_inventory_page(
    target: Any,
    *,
    prefix: str = "",
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Compare one provider listing page against the reference index (bounded)."""
    target_id = str(getattr(target, "target_id", "") or "")
    orphans: list[str] = []
    missing: list[str] = []
    mismatches: list[str] = []
    next_cursor = None
    examined = 0
    if getattr(target, "store", None) is None:
        return {
            "targetId": target_id,
            "examined": 0,
            "orphans": orphans,
            "missing": missing,
            "sizeMismatches": mismatches,
            "cursor": None,
        }
    page = target.store.list_objects(prefix, cursor=cursor, limit=limit)
    next_cursor = page.cursor
    listed_keys: set[str] = set()
    for meta in page.objects:
        examined += 1
        key = str(meta.key)
        listed_keys.add(key)
        indexed = backup_control.get_target_object(target_id, key)
        if indexed is None:
            # Control-plane metadata paths are not payload inventory.
            if key.startswith("receipts/") or key.startswith("commits/") or key.startswith("retirements/"):
                continue
            orphans.append(key)
            continue
        size = int(getattr(meta, "size", 0) or 0)
        if size and int(indexed.get("sizeBytes") or 0) and size != int(indexed["sizeBytes"]):
            mismatches.append(key)
    # Missing detection is deferred to full rebuilds; page-local missing is incomplete.
    _ = read_json  # retained import for future marker-aware inventory
    return {
        "targetId": target_id,
        "examined": examined,
        "orphans": orphans,
        "missing": missing,
        "sizeMismatches": mismatches,
        "cursor": next_cursor,
    }
