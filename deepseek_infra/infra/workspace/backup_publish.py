"""Immutable object publication with fenced commit markers (4.4.6).

A backup becomes visible only when its schedule slot's commit marker is
created. The ciphertext is first stored as a content-addressed object under
``objects/sha256/<prefix>/<digest>.age`` and re-hashed on the target; the
intent is journaled under ``transactions/<runId>.json``; the immutable receipt
lives at ``receipts/<backupId>.json``; and finally the slot marker under
``commits/<policyId>/<fullSlotSha256>.json`` is created with exclusive create
(``O_EXCL`` locally, ``If-None-Match: *`` remotely). A second publisher for the
same slot converges when the object digest matches and is rejected with
``slot-commit-conflict`` otherwise. Callers pass a lease ``checkpoint`` that
runs at every chunk boundary and right before the marker — the first visible
step. Remote targets publish through :class:`BackupTargetStore` and may reuse a
verified encrypted spool across retries. Legacy 4.4.4/4.4.5 layouts stay
readable through :func:`backup_file_candidates` and truncated commit keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_component_transport,
    backup_object_set,
    backup_scheduler,
    backup_spool,
    backup_targets,
    backup_transfer_budget,
    backup_unattended,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    BackupTargetStore,
    STRONG_PROVIDER_CHECKSUM,
    commit_marker_key,
    commit_marker_keys,
    commit_slot_digest,
    object_key,
    open_filesystem_store,
    put_json_if_absent,
    read_json,
    receipt_key,
    transaction_key,
)

LEGACY_RECEIPT_SCHEMA_VERSION = 3
RECEIPT_SCHEMA_VERSION = 4
LEGACY_COMMIT_SCHEMA_VERSION = 3
COMMIT_SCHEMA_VERSION = 4
GENESIS_COMMIT_HASH = "0" * 64

LAYOUT_DIRS = ("objects", "transactions", "receipts", "commits", "catalog", "control", "events", "holds", ".partial", ".trash", ".orphaned")


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    target_id: str
    root: Path | None
    managed: bool
    kind: str = "filesystem"
    store: BackupTargetStore | None = None

    def require_root(self) -> Path:
        if self.root is None:
            raise AppError("remote target has no local filesystem root", code=ErrorCode.INVALID_REQUEST, status=400)
        return self.root

    def require_store(self) -> BackupTargetStore:
        if self.store is not None:
            return self.store
        if self.root is not None:
            return open_filesystem_store(self.root)
        raise AppError("target store is unavailable", code=ErrorCode.INTERNAL, status=500)


@dataclass(frozen=True, slots=True)
class PublishResult:
    receipt: dict[str, Any]
    path: Path | None
    receipt_path: Path | None
    commit: dict[str, Any]
    converged: bool
    object_key: str = ""
    receipt_key: str = ""


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def resolve_target(target_id: str, *, write_intent: bool = True) -> ResolvedTarget:
    """Resolve and re-verify a publish target; raises blocked-target-unavailable."""
    if target_id == "managed-local":
        root = backups.BACKUP_DIR
        root.mkdir(parents=True, exist_ok=True)
        store = open_filesystem_store(root)
        return ResolvedTarget(target_id=target_id, root=root, managed=True, kind="filesystem", store=store)
    record = backup_targets.get_target(target_id)
    kind = str(record.get("kind") or "filesystem")
    if kind == "s3":
        store = backup_targets.open_target_store(target_id, write_intent=write_intent)
        caps = store.capabilities()
        if write_intent and not caps.scheduled_backup_ready:
            probe = backup_targets.probe_target(target_id)
            if not probe.get("scheduledBackupReady"):
                raise AppError(
                    f"unsupported-conditional-target: {probe.get('status') or 'conditional writes unavailable'}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=503,
                )
        return ResolvedTarget(target_id=target_id, root=None, managed=False, kind="s3", store=store)
    root = backup_targets.verify_target_ready(target_id, write_intent=write_intent)
    return ResolvedTarget(target_id=target_id, root=root, managed=False, kind="filesystem", store=open_filesystem_store(root))


def _ensure_layout(root: Path) -> None:
    for name in LAYOUT_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def object_path(root: Path, digest: str) -> Path:
    return root / "objects" / "sha256" / digest[:2] / f"{digest}.age"


def backup_file_candidates(root: Path, record: dict[str, Any]) -> list[Path]:
    """Existing-layout then legacy locations for a catalog/receipt record."""
    candidates: list[Path] = []
    if str(record.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        try:
            inventory = backup_object_set.committed_object_inventory(record)
        except AppError:
            return []
        for item in inventory:
            candidates.append(object_path(root, str(item["digest"])))
        return candidates
    digest = str(record.get("objectDigest") or record.get("ciphertextSha256") or "")
    if len(digest) == 64:
        candidates.append(object_path(root, digest))
    filename = str(record.get("filename") or "")
    if filename and "/" not in filename and "\\" not in filename:
        candidates.append(root / "backups" / filename)
    return candidates


def commit_marker_path(root: Path, policy_id: str, schedule_slot: str) -> Path:
    """Return the on-disk path for a slot commit marker.

    New writes use the full SHA-256 of the schedule slot. Readers should prefer
    :func:`find_commit_marker_path` which also accepts the 4.4.5 16-hex prefix.
    """
    return root.joinpath(*commit_marker_key(policy_id, schedule_slot, full_digest=True).split("/"))


def find_commit_marker_path(root: Path, policy_id: str, schedule_slot: str) -> Path | None:
    for key in commit_marker_keys(policy_id, schedule_slot):
        path = root.joinpath(*key.split("/"))
        if path.is_file():
            return path
    return None


def read_commit_markers(root: Path) -> list[dict[str, Any]]:
    commits_dir = root / "commits"
    markers: list[dict[str, Any]] = []
    if not commits_dir.is_dir():
        return markers
    for path in sorted(commits_dir.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("commitHash"):
            markers.append(data)
    return markers


def latest_commit(root: Path) -> dict[str, Any] | None:
    markers = read_commit_markers(root)
    if not markers:
        return None
    return max(markers, key=lambda marker: int(marker.get("targetGeneration") or 0))


def _commit_hash(marker: dict[str, Any]) -> str:
    body = {key: value for key, value in marker.items() if key != "commitHash"}
    return hashlib.sha256(_stable_json(body)).hexdigest()


def commit_marker_valid(marker: dict[str, Any]) -> bool:
    commit_hash = str(marker.get("commitHash") or "")
    if not commit_hash:
        return False
    return commit_hash == _commit_hash(marker)


def _object_set_commit_fields_valid(marker: dict[str, Any], *, object_set_digest: str, control_digest: str) -> bool:
    return (
        int(marker.get("schemaVersion") or 0) == COMMIT_SCHEMA_VERSION
        and str(marker.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1
        and str(marker.get("objectSetDigest") or "") == object_set_digest
        and str(marker.get("controlObjectDigest") or "") == control_digest
        and commit_marker_valid(marker)
    )


def _object_set_receipt_fields_valid(receipt: dict[str, Any], *, object_set_digest: str, control_digest: str) -> bool:
    try:
        backup_object_set.committed_object_inventory(receipt)
    except AppError:
        return False
    return (
        int(receipt.get("schemaVersion") or 0) == RECEIPT_SCHEMA_VERSION
        and str(receipt.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1
        and str(receipt.get("objectSetDigest") or "") == object_set_digest
        and str(receipt.get("controlObjectDigest") or "") == control_digest
    )


def _journal_path(root: Path, run_id: str) -> Path:
    return root / "transactions" / f"{run_id}.json"


def _write_journal(root: Path, journal: dict[str, Any]) -> None:
    path = _journal_path(root, str(journal["runId"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_journal(root: Path, run_id: str) -> dict[str, Any] | None:
    path = _journal_path(root, run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if path.read_bytes() != content:
            raise AppError("Immutable target file already exists with different content", code=ErrorCode.INVALID_REQUEST, status=409)
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _create_exclusive(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def receipt_for(
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    target_id: str,
    schedule_slot: str,
) -> dict[str, Any]:
    manifest = getattr(package, "manifest", None) or {}
    is_object_set = isinstance(package, backup_object_set.ObjectSetPackage)
    receipt_schema = RECEIPT_SCHEMA_VERSION if is_object_set else LEGACY_RECEIPT_SCHEMA_VERSION
    snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
    snapshot_kind = str(manifest.get("snapshotKind") or "full")
    if isinstance(snapshot, dict):
        lineage = {
            "schemaVersion": receipt_schema,
            "snapshotKind": str(snapshot.get("kind") or snapshot_kind),
            "lineageId": str(snapshot.get("lineageId") or "") or None,
            "parentBackupId": str(snapshot.get("parentBackupId") or "") or None,
            "baseBackupId": str(snapshot.get("baseBackupId") or "") or None,
            "chainDepth": int(snapshot.get("chainDepth") or 0),
            "chunkProtocol": str(snapshot.get("chunkProtocol") or manifest.get("chunkProtocol") or "") or None,
        }
    else:
        lineage = {
            "schemaVersion": receipt_schema,
            "snapshotKind": snapshot_kind,
            "lineageId": None,
            "parentBackupId": None,
            "baseBackupId": None,
            "chainDepth": 0,
            "chunkProtocol": str(manifest.get("chunkProtocol") or "") or None,
        }
    common_result = {
        **lineage,
        "backupId": package.backup_id,
        "runId": run_id,
        "policyId": policy_id,
        "targetId": target_id,
        "scheduleSlot": schedule_slot,
        "size": package.size,
        "creationVerified": package.creation_verified,
        "createdAt": _utc_iso(),
        "pinned": False,
    }
    if is_object_set:
        return {
            **common_result,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "controlObjectDigest": package.control.ciphertext_digest,
            "objectSetDigest": package.object_set_digest,
            "objects": backup_object_set.remote_object_inventory(package.components),
        }
    return {
        **common_result,
        "filename": package.filename,
        "manifestDigest": package.manifest_digest,
        "coverageDigest": package.coverage_digest,
        "ciphertextSha256": package.ciphertext_sha256,
        "objectDigest": package.ciphertext_sha256,
    }


def _read_commit_marker(store: BackupTargetStore, policy_id: str, schedule_slot: str) -> dict[str, Any] | None:
    for key in commit_marker_keys(policy_id, schedule_slot):
        data = read_json(store, key)
        if data is not None:
            return data
    return None


def _record_publish_to_dr_ledger(
    target_id: str,
    policy_id: str,
    result: PublishResult,
    package: Any,
) -> None:
    try:
        from deepseek_infra.infra.workspace import backup_capacity, backup_dr_ledger
        receipt = result.receipt or {}
        commit = result.commit or {}
        backup_id = str(receipt.get("backupId") or getattr(package, "backup_id", ""))
        if not backup_id:
            return
        size_val = getattr(package, "size", 0) or 0
        logical_bytes = int(receipt.get("logicalBytes") or receipt.get("size") or size_val)
        ciphertext_bytes = int(receipt.get("size") or size_val)
        committed_at = str(commit.get("committedAt") or receipt.get("createdAt") or _utc_iso())
        object_set_digest = str(receipt.get("objectSetDigest") or commit.get("objectSetDigest") or "")
        backup_dr_ledger.record_recovery_point(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            committed_at=committed_at,
            snapshot_kind=str(receipt.get("snapshotKind") or "full"),
            parent_backup_id=receipt.get("parentBackupId"),
            chain_digest=str(commit.get("commitHash") or ""),
            chain_length=int(receipt.get("chainLength") or 1),
            ciphertext_bytes=ciphertext_bytes,
            logical_bytes=logical_bytes,
            recoverable=True,
            verified_at=committed_at,
            storage_protocol=str(receipt.get("storageProtocol") or ""),
            metadata=receipt,
        )
        try:
            backup_dr_ledger.record_logical_recovery_copy(
                target_id=target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                committed_at=committed_at,
                object_set_digest=object_set_digest or None,
                recoverable=True,
                role="primary",
                mode="required",
                snapshot_kind=str(receipt.get("snapshotKind") or "full"),
                verified_at=committed_at,
                metadata={
                    "objectSetDigest": object_set_digest or None,
                    "logicalBytes": logical_bytes,
                    "physicalBytes": ciphertext_bytes,
                    "ciphertextBytes": ciphertext_bytes,
                    "snapshotKind": str(receipt.get("snapshotKind") or "full"),
                },
            )
        except Exception:
            pass
        try:
            backup_capacity.record_physical_size_evidence(
                policy_id=policy_id,
                backup_id=backup_id,
                snapshot_kind=str(receipt.get("snapshotKind") or "full"),
                physical_bytes=ciphertext_bytes,
                observed_at=committed_at,
            )
        except Exception:
            pass
    except Exception:
        pass


def publish_backup(
    target: ResolvedTarget,
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    fencing_token: int,
    receipt: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
) -> PublishResult:
    """Publish a verified package as an immutable object plus slot commit."""
    result: PublishResult
    if isinstance(package, backup_object_set.ObjectSetPackage):
        if target.kind != "filesystem" or target.root is None:
            result = _publish_object_set_via_store(
                target,
                package,
                run_id=run_id,
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                fencing_token=fencing_token,
                receipt=receipt,
                checkpoint=checkpoint,
                traffic_class=traffic_class,
            )
        else:
            result = _publish_object_set_filesystem(
                target,
                package,
                run_id=run_id,
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                fencing_token=fencing_token,
                receipt=receipt,
                checkpoint=checkpoint,
                traffic_class=traffic_class,
            )
    elif target.kind != "filesystem" or target.root is None:
        result = _publish_via_store(
            target,
            package,
            run_id=run_id,
            policy_id=policy_id,
            schedule_slot=schedule_slot,
            fencing_token=fencing_token,
            receipt=receipt,
            checkpoint=checkpoint,
            traffic_class=traffic_class,
        )
    else:
        result = _publish_filesystem(
            target,
            package,
            run_id=run_id,
            policy_id=policy_id,
            schedule_slot=schedule_slot,
            fencing_token=fencing_token,
            receipt=receipt,
            checkpoint=checkpoint,
            traffic_class=traffic_class,
        )
    _record_publish_to_dr_ledger(target.target_id, policy_id, result, package)
    return result


def _publish_filesystem(
    target: ResolvedTarget,
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    fencing_token: int,
    receipt: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
) -> PublishResult:
    root = target.require_root()
    _ensure_layout(root)
    digest = str(package.ciphertext_sha256)
    obj = object_path(root, digest)
    partial = root / ".partial" / f"{run_id}.part"

    def _checkpoint() -> None:
        if checkpoint is not None:
            checkpoint()

    journal: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "fencingToken": int(fencing_token),
        "backupId": package.backup_id,
        "objectDigest": digest,
        "filename": package.filename,
        "size": package.size,
        "phase": "started",
        "updatedAt": _utc_iso(),
    }
    try:
        _write_journal(root, journal)
        if not obj.is_file():
            manager = backup_transfer_budget.get_global_transfer_budget_manager()
            with manager.track_transfer(
                traffic_class=traffic_class,
                source_target_id="managed-local",
                dest_target_id=target.target_id,
                estimated_bytes=int(package.size),
            ) as transfer_id, package.path.open("rb") as source, partial.open("wb") as output:
                while True:
                    _checkpoint()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    manager.wait_for_bandwidth(transfer_id, len(chunk))
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if backup_unattended.sha256_file(partial, checkpoint=checkpoint) != digest:
                raise AppError("Target-side backup digest mismatch after copy", code=ErrorCode.INTERNAL, status=500)
            obj.parent.mkdir(parents=True, exist_ok=True)
            _checkpoint()
            os.replace(partial, obj)
            _fsync_dir(obj.parent)
        elif backup_unattended.sha256_file(obj, checkpoint=checkpoint) != digest:
            raise AppError("Content-addressed object on target fails its digest", code=ErrorCode.INTERNAL, status=500)
        journal.update(phase="object-published", updatedAt=_utc_iso())
        _write_journal(root, journal)

        receipt_data = dict(receipt) if receipt is not None else receipt_for(package, run_id=run_id, policy_id=policy_id, target_id=target.target_id, schedule_slot=schedule_slot)
        receipt_data["objectDigest"] = digest
        receipt_bytes = (json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        receipt_path = root / "receipts" / f"{package.backup_id}.json"
        # Prefer existing legacy truncated marker; new writes use full digest.
        existing_marker_path = find_commit_marker_path(root, policy_id, schedule_slot)
        marker_path = existing_marker_path or commit_marker_path(root, policy_id, schedule_slot)

        def _converge_or_conflict(existing: dict[str, Any]) -> PublishResult:
            if str(existing.get("objectDigest") or "") == digest:
                existing_receipt_path = root / "receipts" / f"{existing.get('backupId')}.json"
                existing_receipt = json.loads(existing_receipt_path.read_text(encoding="utf-8"))
                journal.update(phase="converged", convergedToRunId=str(existing.get("runId") or ""), updatedAt=_utc_iso())
                _write_journal(root, journal)
                backup_scheduler.record_target_health(target.target_id, "ok", None)
                return PublishResult(
                    receipt=existing_receipt,
                    path=obj,
                    receipt_path=existing_receipt_path,
                    commit=existing,
                    converged=True,
                    object_key=object_key(digest),
                    receipt_key=receipt_key(str(existing.get("backupId") or "")),
                )
            detail = "stale-fencing-token" if int(existing.get("fencingToken") or 0) > int(fencing_token) else "different-object"
            journal.update(phase="slot-commit-conflict", conflict=detail, updatedAt=_utc_iso())
            _write_journal(root, journal)
            raise AppError(f"slot-commit-conflict ({detail}): schedule slot is already committed", code=ErrorCode.INVALID_REQUEST, status=409)

        _checkpoint()
        if marker_path.is_file():
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))

        _write_immutable(receipt_path, receipt_bytes)
        journal.update(phase="receipt-published", receiptDigest=receipt_digest, receipt=receipt_data, updatedAt=_utc_iso())
        _write_journal(root, journal)

        _checkpoint()
        latest = latest_commit(root)
        generation = int(latest["targetGeneration"]) + 1 if latest is not None else 1
        marker: dict[str, Any] = {
            "schemaVersion": LEGACY_COMMIT_SCHEMA_VERSION,
            "policyId": policy_id,
            "scheduleSlot": schedule_slot,
            "slotDigest": commit_slot_digest(schedule_slot),
            "runId": run_id,
            "fencingToken": int(fencing_token),
            "backupId": package.backup_id,
            "objectDigest": digest,
            "receiptDigest": receipt_digest,
            "targetGeneration": generation,
            "previousCommitHash": str(latest["commitHash"]) if latest is not None else GENESIS_COMMIT_HASH,
        }
        marker["commitHash"] = _commit_hash(marker)
        marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if not _create_exclusive(marker_path, marker_bytes):
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))
        _fsync_dir(marker_path.parent)
        journal.update(phase="committed", commitHash=marker["commitHash"], updatedAt=_utc_iso())
        _write_journal(root, journal)
        if not target.managed:
            backup_targets.record_target_head(root, target_id=target.target_id, generation=int(marker["targetGeneration"]), commit_hash=str(marker["commitHash"]))
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        return PublishResult(
            receipt=receipt_data,
            path=obj,
            receipt_path=receipt_path,
            commit=marker,
            converged=False,
            object_key=object_key(digest),
            receipt_key=receipt_key(str(package.backup_id)),
        )
    except AppError:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "blocked", str(exc)[:200])
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


def _publish_object_set_filesystem(
    target: ResolvedTarget,
    package: backup_object_set.ObjectSetPackage,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    fencing_token: int,
    receipt: dict[str, Any] | None,
    checkpoint: Callable[[], None] | None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
) -> PublishResult:
    root = target.require_root()
    _ensure_layout(root)
    set_digest = package.object_set_digest
    control_digest = package.control.ciphertext_digest
    partials: list[Path] = []

    def _checkpoint() -> None:
        if checkpoint is not None:
            checkpoint()

    journal: dict[str, Any] = {
        "schemaVersion": 2,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "runId": run_id,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "fencingToken": int(fencing_token),
        "backupId": package.backup_id,
        "objectSetDigest": set_digest,
        "controlObjectDigest": control_digest,
        "objects": backup_object_set.remote_object_inventory(package.components),
        "phase": "started",
        "updatedAt": _utc_iso(),
    }
    try:
        _write_journal(root, journal)

        def _publish_component(component: backup_object_set.EncryptedComponent) -> None:
            if (
                not component.path.is_file()
                or component.path.stat().st_size != component.ciphertext_size
                or backup_unattended.sha256_file(component.path, checkpoint=checkpoint) != component.ciphertext_digest
            ):
                raise AppError("Object-set component fails its ciphertext commitment", code=ErrorCode.INVALID_PAYLOAD)
            destination = object_path(root, component.ciphertext_digest)
            partial = root / ".partial" / f"{run_id}-{component.ciphertext_digest}.part"
            partials.append(partial)
            if not destination.is_file():
                manager = backup_transfer_budget.get_global_transfer_budget_manager()
                with manager.track_transfer(
                    traffic_class=traffic_class,
                    source_target_id="managed-local",
                    dest_target_id=target.target_id,
                    estimated_bytes=component.ciphertext_size,
                ) as transfer_id, component.path.open("rb") as source, partial.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        _checkpoint()
                        manager.wait_for_bandwidth(transfer_id, len(chunk))
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if backup_unattended.sha256_file(partial, checkpoint=checkpoint) != component.ciphertext_digest:
                    raise AppError("Target-side object-set digest mismatch after copy", code=ErrorCode.INTERNAL, status=500)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _checkpoint()
                os.replace(partial, destination)
                _fsync_dir(destination.parent)
            elif backup_unattended.sha256_file(destination, checkpoint=checkpoint) != component.ciphertext_digest:
                raise AppError("Content-addressed component on target fails its digest", code=ErrorCode.INTERNAL, status=500)

        def _make_upload_task(component: backup_object_set.EncryptedComponent) -> backup_component_transport.TransferTask:
            def _execute() -> None:
                _publish_component(component)
            return backup_component_transport.TransferTask(
                component_id=component.component_id,
                ciphertext_digest=component.ciphertext_digest,
                ciphertext_size=component.ciphertext_size,
                execute=_execute,
            )

        upload_tasks = [_make_upload_task(component) for component in package.components]
        backup_component_transport.default_scheduler().run(upload_tasks)
        # Barrier: the journal advances only after every component is verified
        # and in place. A crash mid-transfer leaves the journal at "started";
        # restart re-verifies completed components via the destination branch.
        journal.update(phase="objects-published", updatedAt=_utc_iso())
        _write_journal(root, journal)

        receipt_data = receipt_for(
            package,
            run_id=run_id,
            policy_id=policy_id,
            target_id=target.target_id,
            schedule_slot=schedule_slot,
        )
        receipt_data.update(
            schemaVersion=RECEIPT_SCHEMA_VERSION,
            storageProtocol=backup_object_set.OBJECT_SET_V1,
            controlObjectDigest=control_digest,
            objectSetDigest=set_digest,
            objects=backup_object_set.remote_object_inventory(package.components),
        )
        receipt_data.pop("objectDigest", None)
        receipt_data.pop("ciphertextSha256", None)
        receipt_bytes = (json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        receipt_path = root / "receipts" / f"{package.backup_id}.json"
        marker_path = find_commit_marker_path(root, policy_id, schedule_slot) or commit_marker_path(root, policy_id, schedule_slot)

        def _converge_or_conflict(existing: dict[str, Any]) -> PublishResult:
            if _object_set_commit_fields_valid(existing, object_set_digest=set_digest, control_digest=control_digest):
                existing_receipt_path = root / "receipts" / f"{existing.get('backupId')}.json"
                if existing_receipt_path.is_file():
                    existing_receipt_bytes = existing_receipt_path.read_bytes()
                    existing_receipt = json.loads(existing_receipt_bytes)
                    if (
                        hashlib.sha256(existing_receipt_bytes).hexdigest() == str(existing.get("receiptDigest") or "")
                        and _object_set_receipt_fields_valid(existing_receipt, object_set_digest=set_digest, control_digest=control_digest)
                    ):
                        journal.update(phase="converged", convergedToRunId=str(existing.get("runId") or ""), updatedAt=_utc_iso())
                        _write_journal(root, journal)
                        backup_scheduler.record_target_health(target.target_id, "ok", None)
                        return PublishResult(
                            receipt=existing_receipt,
                            path=object_path(root, control_digest),
                            receipt_path=existing_receipt_path,
                            commit=existing,
                            converged=True,
                            object_key=object_key(control_digest),
                            receipt_key=receipt_key(str(existing.get("backupId") or "")),
                        )
            detail = "stale-fencing-token" if int(existing.get("fencingToken") or 0) > int(fencing_token) else "different-object-set"
            journal.update(phase="slot-commit-conflict", conflict=detail, updatedAt=_utc_iso())
            _write_journal(root, journal)
            raise AppError(f"slot-commit-conflict ({detail}): schedule slot is already committed", code=ErrorCode.INVALID_REQUEST, status=409)

        _checkpoint()
        if marker_path.is_file():
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))
        _write_immutable(receipt_path, receipt_bytes)
        journal.update(phase="receipt-published", receiptDigest=receipt_digest, receipt=receipt_data, updatedAt=_utc_iso())
        _write_journal(root, journal)

        _checkpoint()
        latest = latest_commit(root)
        generation = int(latest["targetGeneration"]) + 1 if latest is not None else 1
        marker: dict[str, Any] = {
            "schemaVersion": COMMIT_SCHEMA_VERSION,
            "policyId": policy_id,
            "scheduleSlot": schedule_slot,
            "slotDigest": commit_slot_digest(schedule_slot),
            "runId": run_id,
            "fencingToken": int(fencing_token),
            "backupId": package.backup_id,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "objectSetDigest": set_digest,
            "controlObjectDigest": control_digest,
            "receiptDigest": receipt_digest,
            "targetGeneration": generation,
            "previousCommitHash": str(latest["commitHash"]) if latest is not None else GENESIS_COMMIT_HASH,
        }
        marker["commitHash"] = _commit_hash(marker)
        marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if not _create_exclusive(marker_path, marker_bytes):
            return _converge_or_conflict(json.loads(marker_path.read_text(encoding="utf-8")))
        _fsync_dir(marker_path.parent)
        journal.update(phase="committed", commitHash=marker["commitHash"], updatedAt=_utc_iso())
        _write_journal(root, journal)
        if not target.managed:
            backup_targets.record_target_head(
                root,
                target_id=target.target_id,
                generation=int(marker["targetGeneration"]),
                commit_hash=str(marker["commitHash"]),
            )
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        return PublishResult(
            receipt=receipt_data,
            path=object_path(root, control_digest),
            receipt_path=receipt_path,
            commit=marker,
            converged=False,
            object_key=object_key(control_digest),
            receipt_key=receipt_key(package.backup_id),
        )
    except AppError:
        for partial in partials:
            partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise
    except OSError as exc:
        for partial in partials:
            partial.unlink(missing_ok=True)
        backup_scheduler.record_target_health(target.target_id, "blocked", str(exc)[:200])
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


def _publish_object_set_via_store(
    target: ResolvedTarget,
    package: backup_object_set.ObjectSetPackage,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    fencing_token: int,
    receipt: dict[str, Any] | None,
    checkpoint: Callable[[], None] | None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
) -> PublishResult:
    store = target.require_store()
    slot_digest = commit_slot_digest(schedule_slot)
    marker_key = commit_marker_key(policy_id, schedule_slot, full_digest=True)
    def _checkpoint() -> None:
        if checkpoint is not None:
            checkpoint()

    spool_meta = backup_spool.store_verified_package(
        package,
        policy_id=policy_id,
        schedule_slot=schedule_slot,
        run_id=run_id,
        slot_digest=slot_digest,
    )
    spooled = backup_spool.lookup_verified_package(policy_id=policy_id, slot_digest=slot_digest)
    if not isinstance(spooled, backup_object_set.ObjectSetPackage):  # pragma: no cover
        raise AppError("verified object-set spool is unavailable", code=ErrorCode.INTERNAL, status=500)
    if spooled.object_set_digest != str(spool_meta.get("objectSetDigest") or ""):  # pragma: no cover
        raise AppError("verified object-set spool commitment mismatch", code=ErrorCode.INTERNAL, status=500)
    set_digest = spooled.object_set_digest
    control_digest = spooled.control.ciphertext_digest

    journal: dict[str, Any] = {
        "schemaVersion": 2,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "runId": run_id,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "fencingToken": int(fencing_token),
        "backupId": spooled.backup_id,
        "objectSetDigest": set_digest,
        "controlObjectDigest": control_digest,
        "objects": backup_object_set.remote_object_inventory(spooled.components),
        "phase": "started",
        "updatedAt": _utc_iso(),
    }
    try:
        put_json_if_absent(store, transaction_key(run_id), journal)

        def _converge_or_conflict(existing: dict[str, Any]) -> PublishResult:
            if _object_set_commit_fields_valid(existing, object_set_digest=set_digest, control_digest=control_digest):
                existing_receipt_key = receipt_key(str(existing.get("backupId") or ""))
                existing_receipt = read_json(store, existing_receipt_key) or {}
                existing_receipt_bytes = store.get_bytes(existing_receipt_key) if existing_receipt else b""
                if (
                    existing_receipt_bytes
                    and hashlib.sha256(existing_receipt_bytes).hexdigest() == str(existing.get("receiptDigest") or "")
                    and _object_set_receipt_fields_valid(existing_receipt, object_set_digest=set_digest, control_digest=control_digest)
                ):
                    journal.update(phase="converged", convergedToRunId=str(existing.get("runId") or ""), updatedAt=_utc_iso())
                    _replace_journal(store, journal)
                    backup_scheduler.record_target_health(target.target_id, "ok", None)
                    backup_spool.clear_slot(policy_id, slot_digest)
                    return PublishResult(
                        receipt=existing_receipt,
                        path=None,
                        receipt_path=None,
                        commit=existing,
                        converged=True,
                        object_key=object_key(control_digest),
                        receipt_key=existing_receipt_key,
                    )
            detail = "stale-fencing-token" if int(existing.get("fencingToken") or 0) > int(fencing_token) else "different-object-set"
            journal.update(phase="slot-commit-conflict", conflict=detail, updatedAt=_utc_iso())
            try:
                _replace_journal(store, journal)
            except AppError:  # pragma: no cover
                pass
            raise AppError(f"slot-commit-conflict ({detail}): schedule slot is already committed", code=ErrorCode.INVALID_REQUEST, status=409)

        existing_marker = _read_commit_marker(store, policy_id, schedule_slot)
        if existing_marker is not None:
            return _converge_or_conflict(existing_marker)

        checkpoint_lock = threading.Lock()

        def _component_checkpoint() -> None:
            # Writer-lease implementations are not required to be re-entrant;
            # serialize checkpoints while component I/O remains parallel.
            with checkpoint_lock:
                _checkpoint()

        def _publish_component(component: backup_object_set.EncryptedComponent) -> None:
            _component_checkpoint()
            key = object_key(component.ciphertext_digest)
            metadata = store.stat(key)
            if metadata is None:
                _upload_object_resumable(
                    store,
                    component,
                    obj_key=key,
                    policy_id=policy_id,
                    slot_digest=slot_digest,
                    checkpoint=_component_checkpoint,
                    multipart_digest=component.ciphertext_digest,
                    workers=1,
                    traffic_class=traffic_class,
                    source_target_id="managed-local",
                    dest_target_id=target.target_id,
                )
                metadata = store.stat(key)
            if metadata is None or metadata.size != component.ciphertext_size:
                raise AppError("Committed object-set component is missing or truncated", code=ErrorCode.INTERNAL, status=500)
            if not component.control and store.capabilities().integrity_mode == STRONG_PROVIDER_CHECKSUM:
                if (
                    metadata.provider_checksum_type != "FULL_OBJECT"
                    or metadata.provider_sha256 != component.ciphertext_digest
                ):
                    raise AppError("Committed object-set provider checksum is unproven", code=ErrorCode.INTERNAL, status=500)
                return
            digest = hashlib.sha256()
            observed_size = 0
            manager = backup_transfer_budget.get_global_transfer_budget_manager()
            verified_stream = manager.throttled_generator(
                store.get_stream(key),
                traffic_class=traffic_class,
                source_target_id=target.target_id,
            )
            for chunk in verified_stream:
                _component_checkpoint()
                digest.update(chunk)
                observed_size += len(chunk)
            if observed_size != component.ciphertext_size or digest.hexdigest() != component.ciphertext_digest:
                raise AppError("Content-addressed component on target fails its digest", code=ErrorCode.INTERNAL, status=500)

        def _make_upload_task(component: backup_object_set.EncryptedComponent) -> backup_component_transport.TransferTask:
            def _execute() -> None:
                _publish_component(component)

            return backup_component_transport.TransferTask(
                component_id=component.component_id,
                ciphertext_digest=component.ciphertext_digest,
                ciphertext_size=component.ciphertext_size,
                execute=_execute,
                working_set_bytes=16 * 1024 * 1024,
                open_files=1,
                priority=0 if component.control else 2,
            )

        upload_tasks = [_make_upload_task(component) for component in spooled.components]
        backup_component_transport.default_scheduler().run(upload_tasks)
        journal.update(phase="objects-published", updatedAt=_utc_iso())
        _replace_journal(store, journal)

        receipt_data = receipt_for(
            spooled,
            run_id=run_id,
            policy_id=policy_id,
            target_id=target.target_id,
            schedule_slot=schedule_slot,
        )
        receipt_data.update(
            schemaVersion=RECEIPT_SCHEMA_VERSION,
            storageProtocol=backup_object_set.OBJECT_SET_V1,
            controlObjectDigest=control_digest,
            objectSetDigest=set_digest,
            objects=backup_object_set.remote_object_inventory(spooled.components),
        )
        receipt_data.pop("objectDigest", None)
        receipt_data.pop("ciphertextSha256", None)
        receipt_bytes = (json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        r_key = receipt_key(spooled.backup_id)
        _checkpoint()
        try:
            store.put_if_absent(r_key, receipt_bytes, checksum_sha256=receipt_digest, content_type="application/json")
        except AppError as exc:
            if exc.status not in {409, 412}:
                raise
            existing_receipt_bytes = store.get_bytes(r_key)
            if existing_receipt_bytes is None or hashlib.sha256(existing_receipt_bytes).hexdigest() != receipt_digest:
                raise
        journal.update(phase="receipt-published", receiptDigest=receipt_digest, receipt=receipt_data, updatedAt=_utc_iso())
        _replace_journal(store, journal)

        _checkpoint()
        latest = latest_commit_store(store)
        generation = int(latest["targetGeneration"]) + 1 if latest is not None else 1
        marker: dict[str, Any] = {
            "schemaVersion": COMMIT_SCHEMA_VERSION,
            "policyId": policy_id,
            "scheduleSlot": schedule_slot,
            "slotDigest": slot_digest,
            "runId": run_id,
            "fencingToken": int(fencing_token),
            "backupId": spooled.backup_id,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "objectSetDigest": set_digest,
            "controlObjectDigest": control_digest,
            "receiptDigest": receipt_digest,
            "targetGeneration": generation,
            "previousCommitHash": str(latest["commitHash"]) if latest is not None else GENESIS_COMMIT_HASH,
        }
        marker["commitHash"] = _commit_hash(marker)
        marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            store.put_if_absent(marker_key, marker_bytes, checksum_sha256=hashlib.sha256(marker_bytes).hexdigest(), content_type="application/json")
        except AppError as exc:
            if exc.status in {409, 412}:
                existing = _read_commit_marker(store, policy_id, schedule_slot)
                if existing is not None:
                    return _converge_or_conflict(existing)
            raise
        journal.update(phase="committed", commitHash=marker["commitHash"], updatedAt=_utc_iso())
        _replace_journal(store, journal)
        backup_targets.record_remote_target_head(
            store,
            target_id=target.target_id,
            generation=int(marker["targetGeneration"]),
            commit_hash=str(marker["commitHash"]),
        )
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        backup_spool.clear_slot(policy_id, slot_digest)
        return PublishResult(
            receipt=receipt_data,
            path=None,
            receipt_path=None,
            commit=marker,
            converged=False,
            object_key=object_key(control_digest),
            receipt_key=r_key,
        )
    except AppError:
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise


def _publish_via_store(
    target: ResolvedTarget,
    package: Any,
    *,
    run_id: str,
    policy_id: str,
    schedule_slot: str,
    fencing_token: int,
    receipt: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
) -> PublishResult:
    store = target.require_store()
    digest = str(package.ciphertext_sha256)
    obj_key = object_key(digest)
    slot_digest = commit_slot_digest(schedule_slot)
    r_key = receipt_key(str(package.backup_id))
    marker_key = commit_marker_key(policy_id, schedule_slot, full_digest=True)

    def _checkpoint() -> None:
        if checkpoint is not None:
            checkpoint()

    journal: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "fencingToken": int(fencing_token),
        "backupId": package.backup_id,
        "objectDigest": digest,
        "filename": package.filename,
        "size": package.size,
        "phase": "started",
        "updatedAt": _utc_iso(),
    }
    try:
        # Durable spool so retries reuse identical ciphertext.
        spool_meta = backup_spool.store_verified_package(
            package,
            policy_id=policy_id,
            schedule_slot=schedule_slot,
            run_id=run_id,
            slot_digest=slot_digest,
        )
        spool_path = backup_spool.package_path(policy_id, slot_digest)
        if spool_path is None:  # pragma: no cover - store_verified_package always writes package
            raise AppError("verified spool package missing after store", code=ErrorCode.INTERNAL, status=500)
        package_view = backup_spool.SpooledPackage(spool_meta, spool_path)

        put_json_if_absent(store, transaction_key(run_id), journal)

        existing_marker = _read_commit_marker(store, policy_id, schedule_slot)

        def _converge_or_conflict(existing: dict[str, Any]) -> PublishResult:
            if str(existing.get("objectDigest") or "") == digest:
                existing_receipt = read_json(store, receipt_key(str(existing.get("backupId") or ""))) or {}
                journal.update(phase="converged", convergedToRunId=str(existing.get("runId") or ""), updatedAt=_utc_iso())
                _replace_journal(store, journal)
                backup_scheduler.record_target_health(target.target_id, "ok", None)
                backup_spool.clear_slot(policy_id, slot_digest)
                return PublishResult(
                    receipt=existing_receipt,
                    path=None,
                    receipt_path=None,
                    commit=existing,
                    converged=True,
                    object_key=obj_key,
                    receipt_key=receipt_key(str(existing.get("backupId") or "")),
                )
            detail = "stale-fencing-token" if int(existing.get("fencingToken") or 0) > int(fencing_token) else "different-object"
            journal.update(phase="slot-commit-conflict", conflict=detail, updatedAt=_utc_iso())
            try:
                _replace_journal(store, journal)
            except AppError:  # pragma: no cover - best-effort conflict journal
                pass
            raise AppError(f"slot-commit-conflict ({detail}): schedule slot is already committed", code=ErrorCode.INVALID_REQUEST, status=409)

        if existing_marker is not None:
            return _converge_or_conflict(existing_marker)

        _checkpoint()
        if store.stat(obj_key) is None:
            _upload_object_resumable(
                store,
                package_view,
                obj_key=obj_key,
                policy_id=policy_id,
                slot_digest=slot_digest,
                checkpoint=checkpoint,
                traffic_class=traffic_class,
                source_target_id="managed-local",
                dest_target_id=target.target_id,
            )
        else:
            # Verify remote object when present.
            remote = store.get_bytes(obj_key)
            if remote is None or hashlib.sha256(remote).hexdigest() != digest:  # pragma: no cover - corrupt remote object
                raise AppError("Content-addressed object on target fails its digest", code=ErrorCode.INTERNAL, status=500)
        journal.update(phase="object-published", updatedAt=_utc_iso())
        _replace_journal(store, journal)

        receipt_data = dict(receipt) if receipt is not None else receipt_for(package_view, run_id=run_id, policy_id=policy_id, target_id=target.target_id, schedule_slot=schedule_slot)
        receipt_data["objectDigest"] = digest
        receipt_bytes = (json.dumps(receipt_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        _checkpoint()
        try:
            store.put_if_absent(r_key, receipt_bytes, checksum_sha256=receipt_digest, content_type="application/json")
        except AppError as exc:  # pragma: no cover - receipt race
            if exc.status not in {409, 412}:
                raise
            existing_receipt = read_json(store, r_key) or {}
            if existing_receipt and hashlib.sha256(
                (json.dumps(existing_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            ).hexdigest() != receipt_digest and str(existing_receipt.get("objectDigest") or "") != digest:
                raise
        journal.update(phase="receipt-published", receiptDigest=receipt_digest, receipt=receipt_data, updatedAt=_utc_iso())
        _replace_journal(store, journal)

        _checkpoint()
        latest = latest_commit_store(store)
        generation = int(latest["targetGeneration"]) + 1 if latest is not None else 1
        marker = {
            "schemaVersion": LEGACY_COMMIT_SCHEMA_VERSION,
            "policyId": policy_id,
            "scheduleSlot": schedule_slot,
            "slotDigest": slot_digest,
            "runId": run_id,
            "fencingToken": int(fencing_token),
            "backupId": package_view.backup_id,
            "objectDigest": digest,
            "receiptDigest": receipt_digest,
            "targetGeneration": generation,
            "previousCommitHash": str(latest["commitHash"]) if latest is not None else GENESIS_COMMIT_HASH,
        }
        marker["commitHash"] = _commit_hash(marker)
        marker_bytes = (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            store.put_if_absent(marker_key, marker_bytes, checksum_sha256=hashlib.sha256(marker_bytes).hexdigest(), content_type="application/json")
        except AppError as exc:  # pragma: no cover - marker race converges above
            if exc.status in {409, 412}:
                existing = _read_commit_marker(store, policy_id, schedule_slot)
                if existing is not None:
                    return _converge_or_conflict(existing)
            raise
        journal.update(phase="committed", commitHash=marker["commitHash"], updatedAt=_utc_iso())
        _replace_journal(store, journal)
        backup_targets.record_remote_target_head(
            store,
            target_id=target.target_id,
            generation=int(str(marker["targetGeneration"])),
            commit_hash=str(marker["commitHash"]),
        )
        backup_scheduler.record_target_health(target.target_id, "ok", None)
        backup_spool.clear_slot(policy_id, slot_digest)
        return PublishResult(
            receipt=receipt_data,
            path=None,
            receipt_path=None,
            commit=marker,
            converged=False,
            object_key=obj_key,
            receipt_key=r_key,
        )
    except AppError:
        backup_scheduler.record_target_health(target.target_id, "error", "publish-failed")
        raise
    except OSError as exc:  # pragma: no cover - remote adapters raise AppError
        backup_scheduler.record_target_health(target.target_id, "blocked", str(exc)[:200])
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


def _replace_journal(store: BackupTargetStore, journal: dict[str, Any]) -> None:
    key = transaction_key(str(journal["runId"]))
    data = (json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    current = store.stat(key)
    if current is None:
        store.put_if_absent(key, data, content_type="application/json")
        return
    try:
        store.put_if_match(key, data, expected_etag=current.etag, content_type="application/json")
    except AppError:  # pragma: no cover - best-effort journal
        # Best-effort journal; publish still driven by commit marker.
        pass


def _upload_object_resumable(
    store: BackupTargetStore,
    package: Any,
    *,
    obj_key: str,
    policy_id: str,
    slot_digest: str,
    checkpoint: Callable[[], None] | None = None,
    multipart_digest: str | None = None,
    workers: int = 4,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
    source_target_id: str | None = "managed-local",
    dest_target_id: str | None = None,
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    default_part_size = 16 * 1024 * 1024
    workers = max(1, workers)
    digest = str(getattr(package, "ciphertext_sha256", None) or getattr(package, "ciphertext_digest", ""))
    raw_size = getattr(package, "size", None)
    if raw_size is None:
        raw_size = getattr(package, "ciphertext_size", 0)
    size = int(str(raw_size if raw_size is not None else 0))
    state = (
        backup_spool.read_component_multipart_state(policy_id, slot_digest, multipart_digest)
        if multipart_digest is not None
        else backup_spool.read_multipart_state(policy_id, slot_digest)
    ) or {}

    def _write_state(value: dict[str, Any]) -> None:
        if multipart_digest is not None:
            backup_spool.write_component_multipart_state(policy_id, slot_digest, multipart_digest, value)
        else:
            backup_spool.write_multipart_state(policy_id, slot_digest, value)
    upload = None
    if state.get("uploadId") and str(state.get("key") or "") == obj_key:
        part_size = int(state.get("partSize") or (8 * 1024 * 1024))
        from deepseek_infra.infra.workspace.backup_target_store import MultipartUpload

        upload = MultipartUpload(
            key=obj_key,
            upload_id=str(state["uploadId"]),
            checksum_sha256=digest,
            parts=list(state.get("parts") or []),
            expected_size=size,
        )
        state["partSize"] = part_size
        state["workers"] = workers
        state["maxInFlightBytes"] = workers * part_size
        state["checksumSha256"] = digest
    else:
        part_size = default_part_size
        upload = store.begin_multipart(obj_key, checksum_sha256=digest)
        upload.expected_size = size
        state = {
            "key": obj_key,
            "uploadId": upload.upload_id,
            "parts": [],
            "checksumSha256": digest,
            "partSize": part_size,
            "workers": workers,
            "maxInFlightBytes": workers * part_size,
            "phase": "uploading",
        }
        _write_state(state)

    completed_parts = {int(item["partNumber"]): item for item in upload.parts}
    list_parts = getattr(store, "list_multipart_parts", None)
    if callable(list_parts):
        try:
            completed_parts.update({int(item["partNumber"]): item for item in list_parts(upload)})
        except AppError:
            if completed_parts:
                raise
    upload.parts = sorted(completed_parts.values(), key=lambda item: int(item["partNumber"]))
    state["parts"] = upload.parts
    _write_state(state)

    def upload_one(part_number: int, offset: int, length: int) -> dict[str, Any]:
        with Path(package.path).open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(length)
        if len(chunk) != length:
            raise AppError("spooled multipart package was truncated", code=ErrorCode.INTERNAL, status=500)
        manager = backup_transfer_budget.get_global_transfer_budget_manager()
        with manager.track_transfer(
            traffic_class=traffic_class,
            source_target_id=source_target_id,
            dest_target_id=dest_target_id,
            estimated_bytes=length,
        ) as transfer_id:
            manager.wait_for_bandwidth(transfer_id, length)
            return store.upload_part(upload, part_number, chunk, checksum_sha256=hashlib.sha256(chunk).hexdigest())

    pending = [
        (part_number, offset, min(part_size, size - offset))
        for part_number, offset in enumerate(range(0, size, part_size), start=1)
        if part_number not in completed_parts
    ]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backup-multipart") as executor:
        futures = {}
        for part_number, offset, length in pending:
            if checkpoint is not None:
                checkpoint()
            futures[executor.submit(upload_one, part_number, offset, length)] = part_number
        for future in as_completed(futures):
            if checkpoint is not None:
                checkpoint()
            part_number = futures[future]
            completed_parts[part_number] = future.result()
            upload.parts = sorted(completed_parts.values(), key=lambda item: int(item["partNumber"]))
            state["parts"] = upload.parts
            state["completedParts"] = len(upload.parts)
            _write_state(state)
    if checkpoint is not None:
        checkpoint()
    state["phase"] = "completing"
    _write_state(state)
    store.complete_multipart_if_absent(upload)


def latest_commit_store(store: BackupTargetStore) -> dict[str, Any] | None:
    markers: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = store.list_objects("commits/", cursor=cursor)
        for item in page.objects:
            if not item.key.endswith(".json"):
                continue
            data = read_json(store, item.key)
            if isinstance(data, dict) and data.get("commitHash"):
                markers.append(data)
        if not page.cursor:
            break
        cursor = page.cursor
    if not markers:
        return None
    return max(markers, key=lambda marker: int(marker.get("targetGeneration") or 0))


INCOMPLETE_JOURNAL_PHASES = ("started", "object-published", "objects-published", "receipt-published")


def slot_has_incomplete_journal(root: Path, *, policy_id: str, schedule_slot: str, exclude_run_id: str | None = None) -> bool:
    """True when another run left an uncommitted transaction for this slot."""
    if find_commit_marker_path(root, policy_id, schedule_slot) is not None:
        return False
    transactions = root / "transactions"
    if not transactions.is_dir():
        return False
    for path in sorted(transactions.glob("*.json")):
        journal = read_journal(root, path.name[: -len(".json")])
        if not journal:
            continue
        if exclude_run_id is not None and str(journal.get("runId") or "") == exclude_run_id:
            continue
        if str(journal.get("policyId") or "") != policy_id or str(journal.get("scheduleSlot") or "") != schedule_slot:
            continue
        if str(journal.get("phase") or "") in INCOMPLETE_JOURNAL_PHASES:
            return True
    return False


def slot_has_incomplete_journal_store(
    store: BackupTargetStore,
    *,
    policy_id: str,
    schedule_slot: str,
    exclude_run_id: str | None = None,
) -> bool:
    if _read_commit_marker(store, policy_id, schedule_slot) is not None:
        return False
    cursor = None
    while True:
        page = store.list_objects("transactions/", cursor=cursor)
        for item in page.objects:
            if not item.key.endswith(".json"):
                continue
            journal = read_json(store, item.key)
            if not journal:
                continue
            if exclude_run_id is not None and str(journal.get("runId") or "") == exclude_run_id:
                continue
            if str(journal.get("policyId") or "") != policy_id or str(journal.get("scheduleSlot") or "") != schedule_slot:
                continue
            if str(journal.get("phase") or "") in INCOMPLETE_JOURNAL_PHASES:
                return True
        if not page.cursor:
            break
        cursor = page.cursor
    return False


def cleanup_partial(root: Path, run_id: str) -> None:
    (root / ".partial" / f"{run_id}.part").unlink(missing_ok=True)
