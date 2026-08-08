"""Backup catalog as a projection of committed target events (4.4.5).

The catalog no longer decides which backups exist — slot commit markers and
receipts do. Every mutation (receipt, pin, scrub, unlock verification, trash,
restore, delete) is written first as an immutable event file under
``events/<prefix>/<entryHash>.json`` carrying ``previousEntryHash``, the
target generation and the writer's fencing token, and only then appended to
the JSONL projection. Appends accept a :class:`CatalogPrecondition`
(expected head hash + expected target generation) so callers binding to a
snapshot fail fast instead of writing against a stale head, and the whole
projection can be rebuilt from the event files plus on-disk receipts without
losing pin/scrub/unlock/trash history.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_writer_lease

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


@dataclass(frozen=True, slots=True)
class CatalogPrecondition:
    expected_head_hash: str | None = None
    expected_target_generation: int | None = None


def _target_generation(root: Path) -> int:
    from deepseek_infra.infra.workspace import backup_publish

    latest = backup_publish.latest_commit(root)
    return int(latest.get("targetGeneration") or 0) if latest is not None else 0


def catalog_precondition(root: Path) -> CatalogPrecondition:
    """Snapshot the current catalog head and target generation for CAS appends."""
    entries = _read_entries(root)
    head = str(entries[-1]["entryHash"]) if entries else GENESIS_HASH
    return CatalogPrecondition(expected_head_hash=head, expected_target_generation=_target_generation(root))


def _write_event_file(root: Path, entry: dict[str, Any]) -> None:
    from deepseek_infra.infra.workspace import backup_publish

    digest = str(entry["entryHash"])
    content = (json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    backup_publish._create_exclusive(root / "events" / digest[:2] / f"{digest}.json", content)


def _append_entry(root: Path, entry_type: str, payload: dict[str, Any], *, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    _assert_writer_ownership(writer)
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _read_entries(root)
    head = str(entries[-1]["entryHash"]) if entries else GENESIS_HASH
    if precondition is not None and precondition.expected_head_hash is not None and precondition.expected_head_hash != head:
        raise AppError("catalog-head-cas-failed: catalog head moved since the precondition snapshot", code=ErrorCode.INVALID_REQUEST, status=409)
    generation = _target_generation(root)
    if precondition is not None and precondition.expected_target_generation is not None and precondition.expected_target_generation != generation:
        raise AppError(f"catalog-generation-cas-failed: target generation is now {generation}", code=ErrorCode.INVALID_REQUEST, status=409)
    entry = {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "type": entry_type,
        "payload": payload,
        "previousEntryHash": head,
        "entryHash": _entry_hash(entry_type, payload, head),
        "recordedAt": _utc_iso(),
        "targetGeneration": generation,
        "writerFencingToken": int(writer.fencing_token) if writer is not None else 0,
    }
    _write_event_file(root, entry)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def _assert_writer_ownership(writer: backup_writer_lease.TargetWriterLease | None) -> None:
    if writer is not None:
        writer.assert_owned()


def append_receipt(root: Path, receipt: dict[str, Any], *, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    if not receipt.get("backupId") or not receipt.get("filename"):
        raise AppError("Backup receipt is incomplete", code=ErrorCode.INVALID_PAYLOAD)
    return _append_entry(root, "receipt", receipt, writer=writer, precondition=precondition)


def pin_backup(root: Path, backup_id: str, pinned: bool, *, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "pin", {"backupId": backup_id, "pinned": bool(pinned)}, writer=writer, precondition=precondition)


def record_scrub(root: Path, backup_id: str, *, ok: bool, detail: str = "", writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "scrub", {"backupId": backup_id, "ok": bool(ok), "detail": detail[:200], "scrubbedAt": _utc_iso()}, writer=writer, precondition=precondition)


def record_unlock_verification(root: Path, backup_id: str, *, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "unlock-verified", {"backupId": backup_id, "userUnlockVerifiedAt": _utc_iso()}, writer=writer, precondition=precondition)


def record_trash(root: Path, backup_id: str, *, retention_run_id: str, at: str | None = None, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "trash", {"backupId": backup_id, "retentionRunId": retention_run_id, "trashedAt": at or _utc_iso()}, writer=writer, precondition=precondition)


def record_restore_from_trash(root: Path, backup_id: str, *, at: str | None = None, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "restore-trash", {"backupId": backup_id, "restoredAt": at or _utc_iso()}, writer=writer, precondition=precondition)


def record_delete(root: Path, backup_id: str, *, retention_run_id: str, at: str | None = None, writer: backup_writer_lease.TargetWriterLease | None = None, precondition: CatalogPrecondition | None = None) -> dict[str, Any]:
    return _append_entry(root, "delete", {"backupId": backup_id, "retentionRunId": retention_run_id, "deletedAt": at or _utc_iso()}, writer=writer, precondition=precondition)


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


def rebuild_catalog_from_receipts(root: Path, *, writer: backup_writer_lease.TargetWriterLease | None = None) -> dict[str, Any]:
    """Rebuild the projection from immutable events, legacy JSONL and receipts.

    Governance history (pin/scrub/unlock/trash/delete) survives because every
    event has its own immutable file; receipts without any receipt event are
    appended last in ``createdAt`` order. Forked entries that cannot extend
    the chain from genesis are reported as skipped.
    """
    _assert_writer_ownership(writer)
    candidates: dict[str, dict[str, Any]] = {}
    try:
        for entry in _read_entries(root):
            if entry.get("entryHash") and entry.get("type") and isinstance(entry.get("payload"), dict):
                candidates.setdefault(str(entry["entryHash"]), entry)
    except AppError:
        pass
    events_dir = root / "events"
    if events_dir.is_dir():
        for path in sorted(events_dir.glob("*/*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("entryHash") and entry.get("type") and isinstance(entry.get("payload"), dict):
                candidates[str(entry["entryHash"])] = entry
    covered = {str(entry["payload"].get("backupId") or "") for entry in candidates.values() if entry.get("type") == "receipt"}
    receipts_dir = root / "receipts"
    synthesized: list[dict[str, Any]] = []
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("backupId") and str(data["backupId"]) not in covered:
                synthesized.append(data)
    synthesized.sort(key=lambda item: str(item.get("createdAt") or ""))
    by_previous: dict[str, list[dict[str, Any]]] = {}
    for entry in candidates.values():
        by_previous.setdefault(str(entry.get("previousEntryHash") or ""), []).append(entry)
    for group in by_previous.values():
        group.sort(key=lambda item: str(item.get("entryHash") or ""))
    chain: list[dict[str, Any]] = []
    used: set[str] = set()
    head = GENESIS_HASH
    while True:
        nxt = next((entry for entry in by_previous.get(head, []) if str(entry.get("entryHash")) not in used), None)
        if nxt is None:
            break
        used.add(str(nxt["entryHash"]))
        chain.append(nxt)
        head = str(nxt["entryHash"])
    skipped = len(candidates) - len(chain)
    generation = _target_generation(root)
    for receipt in synthesized:
        entry = {
            "schemaVersion": CATALOG_SCHEMA_VERSION,
            "type": "receipt",
            "payload": receipt,
            "previousEntryHash": head,
            "entryHash": _entry_hash("receipt", receipt, head),
            "recordedAt": _utc_iso(),
            "targetGeneration": generation,
            "writerFencingToken": int(writer.fencing_token) if writer is not None else 0,
        }
        _write_event_file(root, entry)
        chain.append(entry)
        head = str(entry["entryHash"])
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".rebuild.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in chain:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return {"rebuilt": len(chain), "chainValid": verify_chain(root), "skippedForkEntries": skipped}


def find_orphans_and_missing(root: Path) -> dict[str, list[str]]:
    from deepseek_infra.infra.workspace import backup_publish

    records = [record for record in catalog_state(root).values() if not record.get("deleted")]
    referenced: set[Path] = set()
    for record in records:
        referenced.update(backup_publish.backup_file_candidates(root, record))
    receipts_dir = root / "receipts"
    if receipts_dir.is_dir():
        for path in receipts_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                referenced.update(backup_publish.backup_file_candidates(root, data))
    on_disk: list[Path] = []
    objects_dir = root / "objects" / "sha256"
    if objects_dir.is_dir():
        on_disk.extend(objects_dir.glob("*/*.age"))
    backups_dir = root / "backups"
    if backups_dir.is_dir():
        on_disk.extend(backups_dir.glob("*.dsibackup*"))
    orphans = sorted(path.name for path in on_disk if path not in referenced)
    missing = sorted(
        str(record.get("filename") or record.get("backupId"))
        for record in records
        if not record.get("trashed")
        and backup_publish.backup_file_candidates(root, record)
        and not any(candidate.is_file() for candidate in backup_publish.backup_file_candidates(root, record))
    )
    return {"orphans": orphans, "missing": missing}
