"""Backup catalog with hash-chained receipts (4.4.4).

The catalog is an append-only JSONL hash chain living next to the backups on
each target. It is a convenience index, not a trust root: restore decisions
always rely on age, the manifest and per-file digests, and a damaged catalog
can be rebuilt from the receipts on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "catalog-v1.jsonl"
GENESIS_HASH = "0" * 64


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _entry_hash(entry_type: str, payload: dict[str, Any], previous_hash: str) -> str:
    body = {"type": entry_type, "payload": payload, "previousEntryHash": previous_hash}
    return hashlib.sha256(_stable_json(body)).hexdigest()


def catalog_path(root: Path) -> Path:
    return root / "catalog" / CATALOG_FILENAME


def _read_entries(root: Path) -> list[dict[str, Any]]:
    path = catalog_path(root)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AppError("Backup catalog is corrupt; rebuild it from receipts", code=ErrorCode.INVALID_REQUEST, status=409) from exc
            if isinstance(value, dict):
                entries.append(value)
    return entries


def verify_chain(root: Path) -> bool:
    previous = GENESIS_HASH
    for entry in _read_entries(root):
        if entry.get("previousEntryHash") != previous:
            return False
        expected = _entry_hash(str(entry.get("type") or ""), entry.get("payload") or {}, previous)
        if entry.get("entryHash") != expected:
            return False
        previous = str(entry["entryHash"])
    return True


def _append_entry(root: Path, entry_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _read_entries(root)
    previous = str(entries[-1]["entryHash"]) if entries else GENESIS_HASH
    entry = {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "type": entry_type,
        "payload": payload,
        "previousEntryHash": previous,
        "entryHash": _entry_hash(entry_type, payload, previous),
        "recordedAt": _utc_iso(),
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def append_receipt(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    if not receipt.get("backupId") or not receipt.get("filename"):
        raise AppError("Backup receipt is incomplete", code=ErrorCode.INVALID_PAYLOAD)
    return _append_entry(root, "receipt", receipt)


def pin_backup(root: Path, backup_id: str, pinned: bool) -> dict[str, Any]:
    return _append_entry(root, "pin", {"backupId": backup_id, "pinned": bool(pinned)})


def record_scrub(root: Path, backup_id: str, *, ok: bool, detail: str = "") -> dict[str, Any]:
    return _append_entry(root, "scrub", {"backupId": backup_id, "ok": bool(ok), "detail": detail[:200], "scrubbedAt": _utc_iso()})


def record_unlock_verification(root: Path, backup_id: str) -> dict[str, Any]:
    return _append_entry(root, "unlock-verified", {"backupId": backup_id, "userUnlockVerifiedAt": _utc_iso()})


def record_trash(root: Path, backup_id: str, *, retention_run_id: str) -> dict[str, Any]:
    return _append_entry(root, "trash", {"backupId": backup_id, "retentionRunId": retention_run_id, "trashedAt": _utc_iso()})


def record_restore_from_trash(root: Path, backup_id: str) -> dict[str, Any]:
    return _append_entry(root, "restore-trash", {"backupId": backup_id, "restoredAt": _utc_iso()})


def record_delete(root: Path, backup_id: str, *, retention_run_id: str) -> dict[str, Any]:
    return _append_entry(root, "delete", {"backupId": backup_id, "retentionRunId": retention_run_id, "deletedAt": _utc_iso()})


def catalog_state(root: Path) -> dict[str, dict[str, Any]]:
    """Fold the chain into per-backup records."""
    state: dict[str, dict[str, Any]] = {}
    for entry in _read_entries(root):
        payload = entry.get("payload") or {}
        backup_id = str(payload.get("backupId") or "")
        entry_type = str(entry.get("type") or "")
        if entry_type == "receipt":
            state[backup_id] = {
                **payload,
                "pinned": False,
                "ciphertextScrubbedAt": None,
                "scrubOk": None,
                "userUnlockVerifiedAt": None,
                "trashed": False,
                "deleted": False,
            }
            continue
        if backup_id not in state:
            continue
        record = state[backup_id]
        if entry_type == "pin":
            record["pinned"] = bool(payload.get("pinned"))
        elif entry_type == "scrub":
            record["ciphertextScrubbedAt"] = payload.get("scrubbedAt")
            record["scrubOk"] = bool(payload.get("ok"))
        elif entry_type == "unlock-verified":
            record["userUnlockVerifiedAt"] = payload.get("userUnlockVerifiedAt")
        elif entry_type == "trash":
            record["trashed"] = True
            record["trashedAt"] = payload.get("trashedAt")
        elif entry_type == "restore-trash":
            record["trashed"] = False
            record.pop("trashedAt", None)
        elif entry_type == "delete":
            record["deleted"] = True
            record["deletedAt"] = payload.get("deletedAt")
    return state


def list_backups(
    root: Path,
    *,
    policy_id: str | None = None,
    target_id: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    records = catalog_state(root)
    result: list[dict[str, Any]] = []
    for record in state_sorted(records.values()):
        if not include_deleted and record.get("deleted"):
            continue
        if policy_id and record.get("policyId") != policy_id:
            continue
        if target_id and record.get("targetId") != target_id:
            continue
        result.append(record)
    return result


def state_sorted(records: Any) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: str(item.get("createdAt") or ""), reverse=True)


def rebuild_catalog_from_receipts(root: Path) -> dict[str, Any]:
    """Rebuild the chain from on-disk receipts after catalog damage."""
    receipts_dir = root / "receipts"
    receipts: list[dict[str, Any]] = []
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.receipt.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("backupId"):
                receipts.append(data)
    receipts.sort(key=lambda item: str(item.get("createdAt") or ""))
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".rebuild.tmp")
    previous = GENESIS_HASH
    count = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for receipt in receipts:
            entry = {
                "schemaVersion": CATALOG_SCHEMA_VERSION,
                "type": "receipt",
                "payload": receipt,
                "previousEntryHash": previous,
                "entryHash": _entry_hash("receipt", receipt, previous),
                "recordedAt": _utc_iso(),
            }
            previous = str(entry["entryHash"])
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return {"rebuilt": count, "chainValid": verify_chain(root)}


def find_orphans_and_missing(root: Path) -> dict[str, list[str]]:
    records = catalog_state(root)
    known_files = {str(record.get("filename") or ""): record for record in records.values() if not record.get("deleted")}
    backups_dir = root / "backups"
    on_disk = {path.name for path in backups_dir.glob("*.dsibackup*")} if backups_dir.is_dir() else set()
    receipts_dir = root / "receipts"
    receipt_files = {path.name[: -len(".receipt.json")] for path in receipts_dir.glob("*.receipt.json")} if receipts_dir.is_dir() else set()
    orphans = sorted(name for name in on_disk if name not in known_files and name not in receipt_files)
    missing = sorted(name for name, record in known_files.items() if name not in on_disk and not record.get("trashed"))
    return {"orphans": orphans, "missing": missing}
