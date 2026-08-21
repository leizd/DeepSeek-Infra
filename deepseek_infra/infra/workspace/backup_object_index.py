"""Rebuildable ciphertext reference index for scale-safe retirement/GC (4.6.0).

Formal Receipt v4 / Commit v4 remain the source of truth. Physical capacity
counts each ciphertext once via canonical object identity; compatibility
aliases never inflate usage. GC correctness uses SQL-native live-ref checks
and never materializes a truncated live-key set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control
from deepseek_infra.infra.workspace.backup_target_store import object_key


def _read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def canonical_object_key(digest: str) -> str:
    """Single physical identity for object-set-v1 ciphertext."""
    return object_key(digest)


def is_canonical_object_key(key: str) -> bool:
    return str(key).startswith("objects/sha256/") and str(key).endswith(".age")


def receipt_payload_entries(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract payload objects with one canonical physical key per digest.

    Compatibility aliases (legacy ``ciphertext/sha256/...`` paths, explicit
    Receipt paths) are recorded with ``physical=False`` and zero size so they
    never inflate physicalStoredBytes.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(
        key: str | None,
        *,
        digest: str | None = None,
        size: int | None = None,
        physical: bool = True,
    ) -> None:
        if not key or key in seen:
            return
        seen.add(key)
        size_bytes = int(size) if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
        entries.append(
            {
                "objectKey": key,
                "ciphertextDigest": digest,
                "sizeBytes": size_bytes if physical else 0,
                "physical": physical,
                "canonicalObjectKey": canonical_object_key(digest) if digest else (key if is_canonical_object_key(key) else None),
            }
        )

    filename = receipt.get("filename")
    if filename:
        fname = str(filename)
        digest = str(receipt.get("objectDigest") or "") or None
        if digest:
            _add(canonical_object_key(digest), digest=digest, size=None, physical=True)
            if fname != canonical_object_key(digest):
                _add(fname, digest=digest, size=0, physical=False)
        else:
            _add(fname, digest=None, size=None, physical=is_canonical_object_key(fname))
    object_digest = str(receipt.get("objectDigest") or "")
    if object_digest:
        _add(canonical_object_key(object_digest), digest=object_digest, physical=True)
    for item in list(receipt.get("objects") or []) + list(receipt.get("components") or []):
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("key")
        digest = str(item.get("digest") or item.get("ciphertextDigest") or "") or None
        size = item.get("size") or item.get("sizeBytes") or item.get("ciphertextBytes")
        size_int = size if isinstance(size, int) and not isinstance(size, bool) else None
        if digest:
            canon = canonical_object_key(digest)
            _add(canon, digest=digest, size=size_int, physical=True)
            if path and str(path) != canon:
                _add(str(path), digest=digest, size=0, physical=False)
            alias = f"ciphertext/sha256/{digest}"
            if alias != canon:
                _add(alias, digest=digest, size=0, physical=False)
        elif path:
            _add(str(path), digest=None, size=size_int, physical=is_canonical_object_key(str(path)))
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
            physical=bool(entry.get("physical", True)),
            canonical_object_key=entry.get("canonicalObjectKey"),
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
    # Keyset scan — never rely on a hard 20k correctness ceiling.
    refs = backup_control.list_recovery_object_refs_complete(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
    )
    if not refs and receipt is not None:
        index_receipt_objects(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
            ref_state="live",
        )
        refs = backup_control.list_recovery_object_refs_complete(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
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
            physical=bool(ref.get("physical", True)),
            canonical_object_key=ref.get("canonicalObjectKey"),
        )
        changed += 1
    return changed


def retained_payload_keys_from_index(target_id: str, *, retiring_backup_id: str) -> set[str] | None:
    """Signal how GC should consult the object index.

    Returns:
    - ``set()`` when coverage is **complete** and the index is non-empty:
      callers must use :func:`object_is_live_referenced` per key (never a
      truncated key-set materialization).
    - ``None`` when the index is empty or coverage is incomplete: callers must
      fall back to a conservative full Receipt scan. Incomplete indexes must
      never solely authorize deletes.
    """
    del retiring_backup_id
    if not backup_control.target_object_index_nonempty(target_id):
        return None
    allowed, _reason = gc_allowed(target_id)
    if not allowed:
        return None
    return set()


def object_is_live_referenced(target_id: str, object_key: str, *, excluding_backup_id: str | None = None) -> bool:
    """SQL-native live-ref check — never page-limited materialization."""
    return backup_control.object_has_live_ref(
        target_id,
        object_key,
        excluding_backup_id=excluding_backup_id,
    )


def gc_allowed(target_id: str) -> tuple[bool, str]:
    """GC via index only when coverage generation is complete."""
    return backup_control.index_coverage_allows_gc(target_id)


def gc_candidate_keys(target_id: str, *, limit: int = 500) -> list[str]:
    allowed, _reason = gc_allowed(target_id)
    if not allowed:
        return []
    return [str(item["objectKey"]) for item in backup_control.list_target_objects(target_id, gc_candidates_only=True, limit=limit)]

def rebuild_index_from_target(target: Any) -> dict[str, Any]:
    """Rebuild the object index for one target from formal Receipt truth.

    When a valid retirement marker is present the ref is stored as retired;
    otherwise it is live. Missing markers keep payload protected.
    """
    from deepseek_infra.infra.workspace import backup_retirement

    target_id = str(getattr(target, "target_id", "") or "")
    if not target_id:
        raise AppError("target_id required for index rebuild", code=ErrorCode.INVALID_REQUEST, status=400)
    backup_control.clear_target_object_index(target_id)
    backup_control.set_target_index_coverage(target_id, state="building", formal_receipt_count=0)
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

    backup_control.set_target_index_coverage(target_id, state="scanning", formal_receipt_count=0)
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
    head_gen = None
    try:
        from deepseek_infra.infra.workspace import backup_targets

        rec = backup_targets.get_target(target_id)
        if isinstance(rec, dict) and rec.get("topologyGeneration") is not None:
            head_gen = int(rec["topologyGeneration"])
    except Exception:
        head_gen = None
    coverage = backup_control.set_target_index_coverage(
        target_id,
        state="complete",
        formal_receipt_count=scanned,
        source_head_generation=head_gen,
    )
    return {
        "scannedReceipts": scanned,
        "liveRecoveryPoints": live,
        "retiredRecoveryPoints": retired,
        "indexGeneration": int(coverage.get("indexGeneration") or 0),
        "coverageState": "complete",
    }

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
    return {
        "targetId": target_id,
        "examined": examined,
        "orphans": orphans,
        "missing": missing,
        "sizeMismatches": mismatches,
        "cursor": next_cursor,
    }
