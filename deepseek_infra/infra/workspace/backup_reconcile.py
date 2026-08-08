"""Crash reconciliation for interrupted target publications (4.4.5).

On worker startup every target is scanned deterministically — scheduler DB,
transaction journals, commit markers, receipts, objects and the catalog — and
converged without ever re-running a backup (a re-run would mint a second
ciphertext for the same schedule slot):

- object present, commit missing → stays invisible; after the grace period
  the object and its unpublished receipt move to ``.orphaned/``;
- commit present, scheduler run still active → the marker is validated and
  the run converges to ``complete``;
- commit present, receipt missing → the receipt is rebuilt from the
  transaction journal;
- receipt present, catalog missing → the catalog projection is rebuilt
  (rebuilt from committed receipts only);
- catalog records without a slot commit → reported as ``catalog-corrupt``
  and retention refuses to run until the target is reconciled.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_publish,
    backup_scheduler,
    backup_targets,
    backup_writer_lease,
    backups,
)

ORPHAN_GRACE_SECONDS = 24 * 3600


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _receipt_files(root: Path) -> dict[str, Path]:
    receipts_dir = root / "receipts"
    result: dict[str, Path] = {}
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            result[path.name[: -len(".json")]] = path
    return result


def catalog_corrupt_backup_ids(root: Path) -> list[str]:
    """Catalog receipt records (schema ≥ 2) that have no slot commit marker."""
    committed = {str(marker.get("backupId") or "") for marker in backup_publish.read_commit_markers(root)}
    corrupt: list[str] = []
    for record in backup_catalog.catalog_state(root).values():
        if int(record.get("schemaVersion") or 0) < 2:
            continue
        backup_id = str(record.get("backupId") or "")
        if backup_id and backup_id not in committed:
            corrupt.append(backup_id)
    return sorted(corrupt)


def assert_catalog_committed(root: Path) -> None:
    corrupt = catalog_corrupt_backup_ids(root)
    if corrupt:
        raise AppError(f"catalog-corrupt: {len(corrupt)} catalog records have no slot commit ({', '.join(corrupt[:3])}); reconcile the target before retention", code=ErrorCode.INVALID_REQUEST, status=409)


def _marker_valid(marker: dict[str, Any]) -> bool:
    return backup_publish.commit_marker_valid(marker)


def _rebuild_receipt_from_journal(root: Path, marker: dict[str, Any], *, writer: backup_writer_lease.TargetWriterLease) -> dict[str, Any] | None:
    journal = backup_publish.read_journal(root, str(marker.get("runId") or ""))
    receipt = (journal or {}).get("receipt")
    if not isinstance(receipt, dict) or not receipt.get("backupId"):
        return None
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    backup_publish._write_immutable(root / "receipts" / f"{receipt['backupId']}.json", receipt_bytes)
    return receipt


def _converge_run(run_id: str, *, backup_id: str, filename: str) -> bool:
    try:
        return backup_scheduler.converge_completed_run(run_id, backup_id=backup_id, filename=filename)
    except AppError:
        return False


def reconcile_target(
    root: Path,
    *,
    target_id: str,
    writer: backup_writer_lease.TargetWriterLease,
    now: datetime | None = None,
    orphan_grace_seconds: int = ORPHAN_GRACE_SECONDS,
) -> dict[str, Any]:
    """Converge one target's scheduler/journal/marker/receipt/object/catalog state."""
    current = now or datetime.now(tz=timezone.utc)
    report: dict[str, Any] = {
        "targetId": target_id,
        "convergedRuns": [],
        "rebuiltReceipts": [],
        "catalogBackfilled": [],
        "invalidMarkers": [],
        "orphanedObjects": [],
        "orphanedReceipts": [],
        "catalogCorrupt": [],
        "catalogRebuilt": False,
    }
    markers = backup_publish.read_commit_markers(root)
    receipt_files = _receipt_files(root)
    try:
        state = backup_catalog.catalog_state(root)
    except AppError:
        committed_receipts = []
        markers_by_backup = {str(marker.get("backupId") or ""): marker for marker in markers}
        for backup_id, path in receipt_files.items():
            if backup_id in markers_by_backup:
                try:
                    committed_receipts.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        _rewrite_catalog(root, committed_receipts, writer=writer)
        report["catalogRebuilt"] = True
        state = backup_catalog.catalog_state(root)
    for marker in markers:
        backup_id = str(marker.get("backupId") or "")
        digest = str(marker.get("objectDigest") or "")
        if not _marker_valid(marker) or not backup_id or not digest or not backup_publish.object_path(root, digest).is_file():
            report["invalidMarkers"].append(str(marker.get("commitHash") or "")[:16])
            continue
        receipt_path = receipt_files.get(backup_id)
        if receipt_path is None:
            receipt = _rebuild_receipt_from_journal(root, marker, writer=writer)
            if receipt is None:
                report["invalidMarkers"].append(str(marker.get("commitHash") or "")[:16])
                continue
            report["rebuiltReceipts"].append(backup_id)
        else:
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report["invalidMarkers"].append(str(marker.get("commitHash") or "")[:16])
                continue
        if backup_id not in state:
            backup_catalog.append_receipt(root, receipt, writer=writer)
            report["catalogBackfilled"].append(backup_id)
            state = backup_catalog.catalog_state(root)
        run_id = str(marker.get("runId") or "")
        if run_id and _converge_run(run_id, backup_id=backup_id, filename=str(receipt.get("filename") or "")):
            report["convergedRuns"].append(run_id)
    committed_digests = {str(marker.get("objectDigest") or "") for marker in markers}
    committed_backups = {str(marker.get("backupId") or "") for marker in markers}
    objects_dir = root / "objects" / "sha256"
    if objects_dir.is_dir():
        for path in sorted(objects_dir.glob("*/*.age")):
            digest = path.name[: -len(".age")]
            if digest in committed_digests:
                continue
            age = current - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if age < timedelta(seconds=orphan_grace_seconds):
                continue
            destination = root / ".orphaned" / "objects" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            report["orphanedObjects"].append(path.name)
    for backup_id, path in receipt_files.items():
        if backup_id in committed_backups:
            continue
        if not path.is_file():
            continue
        age = current - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if age < timedelta(seconds=orphan_grace_seconds):
            continue
        destination = root / ".orphaned" / "receipts" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
        report["orphanedReceipts"].append(path.name)
    report["catalogCorrupt"] = catalog_corrupt_backup_ids(root)
    return report


def _rewrite_catalog(root: Path, receipts: list[dict[str, Any]], *, writer: backup_writer_lease.TargetWriterLease) -> None:
    path = backup_catalog.catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".reconcile.tmp")
    receipts = sorted(receipts, key=lambda item: str(item.get("createdAt") or ""))
    previous = backup_catalog.GENESIS_HASH
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for receipt in receipts:
            entry = {
                "schemaVersion": backup_catalog.CATALOG_SCHEMA_VERSION,
                "type": "receipt",
                "payload": receipt,
                "previousEntryHash": previous,
                "entryHash": backup_catalog._entry_hash("receipt", receipt, previous),
                "recordedAt": _utc_iso(),
            }
            previous = str(entry["entryHash"])
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def reconcile_all_targets(
    *,
    instance_id: str,
    now: datetime | None = None,
    orphan_grace_seconds: int = ORPHAN_GRACE_SECONDS,
) -> list[dict[str, Any]]:
    """Reconcile the managed target and every registered filesystem target."""
    roots: list[tuple[str, Path]] = [("managed-local", backups.BACKUP_DIR)]
    for target in backup_targets.list_targets():
        try:
            roots.append((str(target["targetId"]), Path(str(target["path"]))))
        except KeyError:
            continue
    reports: list[dict[str, Any]] = []
    for target_id, root in roots:
        clock = (lambda: now) if now is not None else None
        writer = backup_writer_lease.TargetWriterLease(
            root,
            target_id=target_id,
            owner_run_id=f"reconcile_{instance_id}",
            owner_instance_id=instance_id,
            fencing_token=backup_scheduler.allocate_fencing_token(),
            clock=clock,
        )
        try:
            writer.acquire()
        except AppError as exc:
            reports.append({"targetId": target_id, "skipped": str(exc)[:200]})
            continue
        try:
            reports.append(reconcile_target(root, target_id=target_id, writer=writer, now=now, orphan_grace_seconds=orphan_grace_seconds))
        finally:
            writer.release()
    return reports
