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
import queue
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_component_cache,
    backup_component_transport,
    backup_crypto,
    backup_incremental,
    backup_incremental_restore,
    backup_object_set,
    backup_pack,
    backup_projection,
    backup_publish,
    backup_recovery_lease,
    backup_recovery_job,
    backup_recovery_preflight,
    backup_recovery_state,
    backup_recovery_telemetry,
    backup_targets,
    backup_transfer_budget,
    backup_unattended,
    backup_verified_plan,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    object_key,
    put_json_if_absent,
    read_json,
    receipt_key,
    restore_hold_key,
)

HOLD_TTL_SECONDS = 6 * 3600
SESSION_SCHEMA_VERSION = 4
CRYPTO_QUEUE_MAX_COMPONENTS = 2


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _session_dir(restore_id: str) -> Path:
    return backups.RESTORE_DIR / restore_id


def _session_path(restore_id: str) -> Path:
    return _session_dir(restore_id) / "remote-fetch.json"


def _assert_live_restore_allowed(root: Path, session: dict[str, Any]) -> None:
    if bool(session.get("drillOnly")) or any((root / name).is_file() for name in ("drill-running.json", "drill-result.json")):
        raise AppError("Recovery Drill job cannot enter live restore", code=ErrorCode.INVALID_REQUEST, status=409)


def _atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    clear_pause: bool = False,
    clear_abort: bool = False,
    allow_control_transition: bool = False,
) -> None:
    """Atomically persist a session without losing concurrent job intents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with backup_recovery_job.session_lock(path):
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                decoded = json.loads(path.read_text(encoding="utf-8"))
                existing = decoded if isinstance(decoded, dict) else {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        if existing.get("pauseRequested") and not clear_pause:
            payload["pauseRequested"] = True
        if existing.get("abortRequested") and not clear_abort:
            payload["abortRequested"] = True
        existing_generation = int(existing.get("controlGeneration") or 0)
        payload_generation = int(payload.get("controlGeneration") or 0)
        if existing_generation > payload_generation:
            payload["controlGeneration"] = existing_generation
            payload["phase"] = str(existing.get("phase") or payload.get("phase") or "")
            for key in ("pauseRequested", "abortRequested", "pausedFromPhase"):
                if key in existing:
                    payload[key] = existing[key]
                else:
                    payload.pop(key, None)
        existing_phase = str(existing.get("phase") or "")
        if existing_phase in backup_recovery_job.CONTROLLED_STOP_PHASES and not (
            allow_control_transition and payload_generation >= existing_generation
        ):
            payload["phase"] = existing_phase
            if existing.get("pausedFromPhase"):
                payload["pausedFromPhase"] = existing["pausedFromPhase"]
        _write_json_unlocked(path, payload)


def _write_json_unlocked(path: Path, payload: dict[str, Any]) -> None:
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
            existing = decoded if isinstance(decoded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    backup_recovery_telemetry.update_for_persist(existing, payload)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _set_phase(session: dict[str, Any], phase: str) -> None:
    session["phase"] = phase
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(_session_path(str(session["restoreId"])), session)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


def _manifest_work(manifest: dict[str, Any]) -> tuple[int, int]:
    files = manifest.get("files")
    if not isinstance(files, list):
        return 0, 0
    valid = [item for item in files if isinstance(item, dict)]
    return sum(max(0, int(item.get("size") or 0)) for item in valid), len(valid)


def _renew_session_holds(store: Any, session: dict[str, Any]) -> None:
    try:
        renewed = backup_recovery_lease.renew_session(store, session)
    except BaseException:
        backup_recovery_telemetry.increment_counter(session, "holdRenewalFailure")
        session["updatedAt"] = _utc_iso()
        _atomic_write_json(_session_path(str(session["restoreId"])), session)
        raise
    if renewed:
        backup_recovery_telemetry.increment_counter(session, "holdRenewalSuccess")
        session["updatedAt"] = _utc_iso()
        _atomic_write_json(_session_path(str(session["restoreId"])), session)


def _converge_job_control(session: dict[str, Any]) -> str:
    restore_id = str(session["restoreId"])
    session["scratchRoot"] = str(_session_dir(restore_id))
    session["transactionPath"] = str(_session_dir(restore_id) / "transaction.json")

    def _abort_prepared() -> None:
        backups.abort_restore(restore_id)

    phase = backup_recovery_job.converge(
        session,
        abort_prepared=_abort_prepared,
        release=lambda: _release_session_holds(session),
    )
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(
        _session_path(restore_id),
        session,
        clear_pause=phase in {"aborted", "rolled-back"},
        clear_abort=phase in {"aborted", "rolled-back"},
        allow_control_transition=True,
    )
    return str(session.get("phase") or phase)


def _checkpoint_job_control(session: dict[str, Any], store: Any) -> None:
    """Observe durable control intent at a chunk/component boundary."""
    restore_id = str(session["restoreId"])
    latest = read_restore_session(restore_id)
    if latest is not None:
        session["controlGeneration"] = int(latest.get("controlGeneration") or 0)
        for key in ("pauseRequested", "abortRequested"):
            if latest.get(key):
                session[key] = True
        latest_phase = str(latest.get("phase") or "")
        if latest_phase in backup_recovery_job.CONTROLLED_STOP_PHASES:
            session["phase"] = latest_phase
            if latest.get("pausedFromPhase"):
                session["pausedFromPhase"] = latest["pausedFromPhase"]
    phase = _converge_job_control(session)
    if phase in {"paused", "recovery-required"}:
        _renew_session_holds(store, session)
    if phase in backup_recovery_job.CONTROLLED_STOP_PHASES:
        raise backup_recovery_job.RecoveryJobStopped(phase)
    _renew_session_holds(store, session)


def request_restore_pause(restore_id: str) -> dict[str, Any]:
    path = _session_path(restore_id)
    with backup_recovery_job.session_lock(path):
        session = read_restore_session(restore_id)
        if session is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        current_phase = str(session.get("phase") or "")
        if session.get("abortRequested") or current_phase == "aborting":
            return {"restoreId": restore_id, "phase": "aborting", "pauseRequested": bool(session.get("pauseRequested"))}
        if current_phase in backup_recovery_job.TERMINAL_PHASES | {"paused", "recovery-required"}:
            return {"restoreId": restore_id, "phase": current_phase, "pauseRequested": bool(session.get("pauseRequested"))}
        backup_recovery_job.request_pause(session)
        session["controlGeneration"] = int(session.get("controlGeneration") or 0) + 1
        session["scratchRoot"] = str(_session_dir(restore_id))
        session["transactionPath"] = str(_session_dir(restore_id) / "transaction.json")
        phase = backup_recovery_job.converge(session)
        session["updatedAt"] = _utc_iso()
        _write_json_unlocked(path, session)
        return {"restoreId": restore_id, "phase": phase, "pauseRequested": bool(session.get("pauseRequested"))}


def resume_restore_session(restore_id: str) -> dict[str, Any]:
    path = _session_path(restore_id)
    with backup_recovery_job.session_lock(path):
        session = read_restore_session(restore_id)
        if session is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        current_phase = str(session.get("phase") or "")
        if current_phase != "paused":
            return {"restoreId": restore_id, "phase": current_phase}
        phase = backup_recovery_job.resume(session)
        session["controlGeneration"] = int(session.get("controlGeneration") or 0) + 1
        session["updatedAt"] = _utc_iso()
        _write_json_unlocked(path, session)
        return {"restoreId": restore_id, "phase": phase}


def request_restore_abort(restore_id: str) -> dict[str, Any]:
    path = _session_path(restore_id)
    with backup_recovery_job.session_lock(path):
        session = read_restore_session(restore_id)
        if session is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        current_phase = str(session.get("phase") or "")
        if current_phase in backup_recovery_job.TERMINAL_PHASES:
            return {"restoreId": restore_id, "phase": current_phase, "abortRequested": bool(session.get("abortRequested"))}
        if not session.get("abortRequested"):
            backup_recovery_job.request_abort(session)
            session["controlGeneration"] = int(session.get("controlGeneration") or 0) + 1
        abort_from_phase = str(session.get("pausedFromPhase") or current_phase) if current_phase == "paused" else current_phase
        session["abortFromPhase"] = str(session.get("abortFromPhase") or abort_from_phase)
        session["phase"] = "aborting"
        session["scratchRoot"] = str(_session_dir(restore_id))
        session["transactionPath"] = str(_session_dir(restore_id) / "transaction.json")
        session["updatedAt"] = _utc_iso()
        _write_json_unlocked(path, session)

    abort_result: dict[str, Any] = {}

    def _abort_prepared() -> None:
        abort_result.update(backups.abort_restore(restore_id))

    phase = backup_recovery_job.converge(
        session,
        abort_prepared=_abort_prepared,
        release=lambda: _release_session_holds(session),
    )
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(
        path,
        session,
        clear_pause=phase in {"aborted", "rolled-back"},
        clear_abort=phase in {"aborted", "rolled-back"},
        allow_control_transition=True,
    )
    return {**abort_result, "restoreId": restore_id, "phase": phase, "abortRequested": bool(session.get("abortRequested"))}


def read_restore_session(restore_id: str) -> dict[str, Any] | None:
    path = _session_path(restore_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover
        return None
    return data if isinstance(data, dict) else None


def _catalog_receipts(target: backup_publish.ResolvedTarget, store: Any) -> dict[str, dict[str, Any]]:
    """Receipt map by backup id, filesystem or S3."""
    if target.root is not None:
        return {str(key): value for key, value in backup_catalog.catalog_state(target.root).items()}
    catalog: dict[str, dict[str, Any]] = {}
    cursor = None
    while True:
        page = store.list_objects("receipts/", cursor=cursor)
        for item in page.objects:
            if item.key.endswith(".json"):
                data = read_json(store, item.key)
                if isinstance(data, dict) and data.get("backupId"):
                    catalog[str(data["backupId"])] = data
        if not page.cursor:
            break
        cursor = page.cursor
    return catalog


def _chain_member(store: Any, receipt: dict[str, Any], staging_root: Path) -> dict[str, Any]:
    backup_id = str(receipt.get("backupId") or "")
    digest = str(receipt.get("objectDigest") or receipt.get("ciphertextSha256") or "")
    if len(digest) != 64:
        raise AppError(f"Backup receipt {backup_id} is missing object digest", code=ErrorCode.INVALID_REQUEST, status=409)
    meta = store.stat(object_key(digest))
    if meta is None:
        raise AppError(f"Backup ciphertext object is missing on target: {backup_id}", code=ErrorCode.NOT_FOUND, status=404)
    filename = str(receipt.get("filename") or f"{backup_id}.age")
    return {
        "backupId": backup_id,
        "objectDigest": digest,
        "filename": filename,
        "expectedBytes": int(meta.size),
        "downloadedBytes": 0,
        "remoteETag": meta.etag,
        "remoteVersionId": meta.version_id,
        "ciphertextPath": str(staging_root / f"{backup_id}.age"),
        "fetched": False,
        "snapshotKind": str(receipt.get("snapshotKind") or "full"),
        "parentBackupId": receipt.get("parentBackupId"),
        "baseBackupId": receipt.get("baseBackupId"),
    }


def _target_markers(target: backup_publish.ResolvedTarget, store: Any) -> list[dict[str, Any]]:
    if target.root is not None:
        return backup_publish.read_commit_markers(target.root)
    markers: list[dict[str, Any]] = []
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
    return markers


def _object_set_member(store: Any, receipt: dict[str, Any], staging_root: Path, index: int) -> dict[str, Any]:
    backup_id = str(receipt.get("backupId") or "")
    control_digest = str(receipt.get("controlObjectDigest") or "")
    raw_objects = receipt.get("objects")
    if len(control_digest) != 64 or not isinstance(raw_objects, list) or any(not isinstance(item, dict) for item in raw_objects):
        raise AppError(f"Backup receipt {backup_id} has an invalid object set", code=ErrorCode.INVALID_REQUEST, status=409)
    objects = [dict(item) for item in raw_objects]
    expected_set_digest = str(receipt.get("objectSetDigest") or "")
    if backup_object_set.object_inventory_digest(objects) != expected_set_digest:
        raise AppError(f"Backup receipt {backup_id} object-set digest mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
    by_digest = {str(item["digest"]): item for item in objects}
    if control_digest not in by_digest:
        raise AppError(f"Backup receipt {backup_id} control object is foreign", code=ErrorCode.INVALID_REQUEST, status=409)
    control_receipt = by_digest[control_digest]
    control_meta = store.stat(object_key(control_digest))
    if control_meta is None or control_meta.size != int(control_receipt["size"]):
        raise AppError(f"Committed object-set control is missing: {backup_id}", code=ErrorCode.NOT_FOUND, status=404)
    # Receipt/Commit validation authenticates the full inventory. Payload
    # existence is intentionally probed only after Projection closure selects
    # it, avoiding one remote HEAD per unselected Component.
    remote_objects = [{"digest": str(item["digest"]), "size": int(item["size"])} for item in objects]
    control = {
        "backupId": backup_id,
        "objectDigest": control_digest,
        "expectedBytes": int(control_meta.size),
        "downloadedBytes": 0,
        "remoteETag": control_meta.etag,
        "remoteVersionId": control_meta.version_id,
        "ciphertextPath": str(staging_root / f"control-{index:04d}.age"),
        "fetched": False,
    }
    return {
        "backupId": backup_id,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "objectSetDigest": expected_set_digest,
        "controlObjectDigest": control_digest,
        "objects": remote_objects,
        "control": control,
        "snapshotKind": str(receipt.get("snapshotKind") or "full"),
        "parentBackupId": receipt.get("parentBackupId"),
        "baseBackupId": receipt.get("baseBackupId"),
    }


def restore_members(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered ciphertext members for either restore shape.

    Full sessions historically stored their only member as top-level fields,
    while Incremental sessions stored ``chain``.  Projection planning must not
    inherit that storage distinction, so callers consume this canonical view.
    """
    if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        raw_chain = session.get("chain")
        if not isinstance(raw_chain, list) or not raw_chain or any(not isinstance(item, dict) for item in raw_chain):
            raise AppError("Object-set restore session has no valid chain", code=ErrorCode.INVALID_REQUEST, status=409)
        return raw_chain
    if str(session.get("snapshotKind") or "full") == "incremental":
        raw_chain = session.get("chain")
        if not isinstance(raw_chain, list) or not raw_chain or any(not isinstance(item, dict) for item in raw_chain):
            raise AppError("Incremental restore session has no valid chain", code=ErrorCode.INVALID_REQUEST, status=409)
        return raw_chain
    return [
        {
            "backupId": str(session.get("backupId") or ""),
            "objectDigest": str(session.get("objectDigest") or ""),
            "filename": str(session.get("filename") or ""),
            "expectedBytes": int(session.get("expectedBytes") or 0),
            "downloadedBytes": int(session.get("downloadedBytes") or 0),
            "ciphertextPath": str(session.get("ciphertextPath") or ""),
            "snapshotKind": "full",
        }
    ]


def _create_object_set_restore(
    *,
    target_id: str,
    backup_id: str,
    target: backup_publish.ResolvedTarget,
    store: Any,
    catalog: dict[str, dict[str, Any]],
    receipt: dict[str, Any],
    selection_value: backup_projection.RestoreSelection | None,
    selection_digest_value: str | None,
) -> dict[str, Any]:
    markers = _target_markers(target, store)
    snapshot_kind = str(receipt.get("snapshotKind") or "full")
    if snapshot_kind == "incremental":
        try:
            chain_receipts = backup_incremental.resolve_lineage_from_receipts(catalog, backup_id)
        except AppError as exc:
            raise AppError(f"Cannot resolve incremental chain: {exc}", code=ErrorCode.INVALID_REQUEST, status=409)
    else:
        chain_receipts = [receipt]
    verified_receipts: list[dict[str, Any]] = []
    for member_receipt in chain_receipts:
        member_backup_id = str(member_receipt.get("backupId") or "")
        committed_receipt = read_json(store, receipt_key(member_backup_id))
        if committed_receipt is None:
            raise AppError(f"Backup {member_backup_id} has no immutable receipt", code=ErrorCode.INVALID_REQUEST, status=409)
        set_digest = str(committed_receipt.get("objectSetDigest") or "")
        if str(committed_receipt.get("storageProtocol") or "") != backup_object_set.OBJECT_SET_V1:
            raise AppError("Object-set lineage cannot reference Whole-Age ancestors", code=ErrorCode.INVALID_REQUEST, status=409)
        backup_object_set.committed_object_inventory(committed_receipt)
        receipt_digest = hashlib.sha256(store.get_bytes(receipt_key(member_backup_id))).hexdigest()
        marker = next(
            (
                candidate
                for candidate in markers
                if int(candidate.get("schemaVersion") or 0) == 4
                and str(candidate.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1
                and str(candidate.get("backupId") or "") == member_backup_id
                and str(candidate.get("objectSetDigest") or "") == set_digest
                and str(candidate.get("controlObjectDigest") or "") == str(committed_receipt.get("controlObjectDigest") or "")
                and str(candidate.get("receiptDigest") or "") == receipt_digest
                and backup_publish.commit_marker_valid(candidate)
            ),
            None,
        )
        if marker is None:
            raise AppError(f"Backup {member_backup_id} has no formal object-set commit", code=ErrorCode.INVALID_REQUEST, status=409)
        verified_receipts.append(committed_receipt)
    chain_receipts = verified_receipts

    restore_id = f"restore_{uuid.uuid4().hex[:12]}"
    staging_root = _session_dir(restore_id)
    staging_root.mkdir(parents=True, exist_ok=True)
    members = [
        _object_set_member(store, member_receipt, staging_root, index)
        for index, member_receipt in enumerate(chain_receipts)
    ]
    hold_keys: list[str] = []
    for index, member in enumerate(members):
        hold = {
            "schemaVersion": 3,
            "storageProtocol": backup_object_set.OBJECT_SET_V1,
            "restoreId": restore_id,
            "backupId": member["backupId"],
            "objectSetDigest": member["objectSetDigest"],
            "objects": [{"digest": item["digest"], "size": item["size"]} for item in member["objects"]],
            "targetId": target_id,
            "createdAt": _utc_iso(),
            "generation": 0,
            "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
        }
        key = restore_hold_key(f"{restore_id}-{index}")
        put_json_if_absent(store, key, hold)
        hold_keys.append(key)
    session = {
        "schemaVersion": SESSION_SCHEMA_VERSION,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "restoreId": restore_id,
        "source": "remote-target",
        "targetId": target_id,
        "activeSourceTargetId": target_id,
        "attemptedSourceTargets": [target_id],
        "failoverCount": 0,
        "targetSwitchEvents": [],
        "backupId": backup_id,
        "snapshotKind": snapshot_kind,
        "chain": members,
        "controlIndex": 0,
        "holdKeys": hold_keys,
        "selection": selection_value.canonical() if selection_value is not None else None,
        "selectionDigest": selection_digest_value,
        "phase": "fetching-controls",
        "createdAt": _utc_iso(),
        "updatedAt": _utc_iso(),
    }
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "snapshotKind": snapshot_kind,
        "chain": [str(item["backupId"]) for item in members],
        "phase": "fetching-controls",
        "targetId": target_id,
        "activeSourceTargetId": target_id,
        "backupId": backup_id,
        "selection": session["selection"],
        "selectionDigest": selection_digest_value,
        "holds": hold_keys,
    }


def create_restore_from_target(
    *,
    target_id: str,
    backup_id: str,
    client: Any | None = None,
    selection: Any | None = None,
    restore_id: str | None = None,
) -> dict[str, Any]:
    """Create or resume a durable remote restore session (phase=fetching / fetching-chain).

    For incremental backups the session resolves the whole chain from the target
    receipts and creates a remote hold for every required ancestor, so no member
    can be garbage-collected before the chain is fetched.

    An optional ``selection`` freezes the restore into a Contributor/Project
    projection: its canonical ``selectionDigest`` is persisted with the session
    and, once frozen, resuming the same session with a different selection is
    rejected with ``409 restore-selection-mismatch``.
    """
    selection_value = backup_projection.normalize_selection(selection)
    selection_digest_value = backup_projection.selection_digest(selection_value) if selection_value is not None else None
    if restore_id:
        existing = read_restore_session(restore_id)
        if existing is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        if str(existing.get("targetId") or "") != target_id or str(existing.get("backupId") or "") != backup_id:
            raise AppError(
                "Restore session source does not match the requested target and backup",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        frozen = existing.get("selectionDigest")
        if selection_digest_value is not None and frozen and frozen != selection_digest_value:
            raise AppError(
                "Restore selection does not match the frozen session selection",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        if selection_digest_value is not None and not frozen:
            assert selection_value is not None
            existing["selection"] = selection_value.canonical()
            existing["selectionDigest"] = selection_digest_value
            existing["schemaVersion"] = SESSION_SCHEMA_VERSION
            _atomic_write_json(_session_path(restore_id), existing)
            frozen = selection_digest_value
        return {
            "restoreId": restore_id,
            "phase": str(existing.get("phase") or "fetching"),
            "targetId": str(existing.get("targetId") or target_id),
            "backupId": str(existing.get("backupId") or backup_id),
            "selection": selection_value.canonical() if selection_value is not None else existing.get("selection"),
            "selectionDigest": selection_digest_value or existing.get("selectionDigest"),
            "holds": list(existing.get("holdKeys") or ([existing["holdKey"]] if existing.get("holdKey") else [])),
        }
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
    catalog = _catalog_receipts(target, store)
    receipt = catalog.get(backup_id) or read_json(store, receipt_key(backup_id))
    if receipt is None:
        raise AppError("Backup receipt not found on target", code=ErrorCode.NOT_FOUND, status=404)
    if str(receipt.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        return _create_object_set_restore(
            target_id=target_id,
            backup_id=backup_id,
            target=target,
            store=store,
            catalog=catalog,
            receipt=receipt,
            selection_value=selection_value,
            selection_digest_value=selection_digest_value,
        )
    digest = str(receipt.get("objectDigest") or receipt.get("ciphertextSha256") or "")
    if len(digest) != 64:
        raise AppError("Backup receipt is missing object digest", code=ErrorCode.INVALID_REQUEST, status=409)
    markers = _target_markers(target, store)
    if not any(str(marker.get("backupId") or "") == backup_id and str(marker.get("objectDigest") or "") == digest for marker in markers):
        raise AppError("Backup has no formal slot commit on target", code=ErrorCode.INVALID_REQUEST, status=409)

    restore_id = f"restore_{uuid.uuid4().hex[:12]}"
    staging_root = _session_dir(restore_id)
    staging_root.mkdir(parents=True, exist_ok=True)
    snapshot_kind = str(receipt.get("snapshotKind") or "full")
    hold_keys: list[str] = []
    if snapshot_kind == "incremental":
        try:
            chain = backup_incremental.resolve_lineage_from_receipts(catalog, backup_id)
        except AppError as exc:
            raise AppError(f"Cannot resolve incremental chain: {exc}", code=ErrorCode.INVALID_REQUEST, status=409)
        chain_ids = [str(item.get("backupId") or "") for item in chain]
        if backup_id not in chain_ids or not chain_ids:
            raise AppError("Incremental chain cannot be resolved on target", code=ErrorCode.INVALID_REQUEST, status=409)
        members = [_chain_member(store, item, staging_root) for item in chain]
        for index, member in enumerate(members):
            hold = {
                "schemaVersion": 3,
                "restoreId": restore_id,
                "backupId": str(member["backupId"]),
                "objectDigest": str(member["objectDigest"]),
                "targetId": target_id,
                "createdAt": _utc_iso(),
                "generation": 0,
                "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
            }
            try:
                put_json_if_absent(store, restore_hold_key(f"{restore_id}-{index}"), hold)
            except AppError:  # pragma: no cover
                pass
            hold_keys.append(restore_hold_key(f"{restore_id}-{index}"))
        session = {
            "schemaVersion": SESSION_SCHEMA_VERSION,
            "restoreId": restore_id,
            "source": "remote-target",
            "targetId": target_id,
            "backupId": backup_id,
            "snapshotKind": "incremental",
            "chain": members,
            "chainIndex": 0,
            "holdKeys": hold_keys,
            "selection": selection_value.canonical() if selection_value is not None else None,
            "selectionDigest": selection_digest_value,
            "phase": "fetching-chain",
            "createdAt": _utc_iso(),
            "updatedAt": _utc_iso(),
        }
        _atomic_write_json(_session_path(restore_id), session)
        return {
            "restoreId": restore_id,
            "snapshotKind": "incremental",
            "chain": [str(item["backupId"]) for item in chain],
            "phase": "fetching-chain",
            "targetId": target_id,
            "backupId": backup_id,
            "selection": session["selection"],
            "selectionDigest": selection_digest_value,
            "holds": hold_keys,
        }

    meta = store.stat(object_key(digest))
    if meta is None:
        raise AppError("Backup ciphertext object is missing on target", code=ErrorCode.NOT_FOUND, status=404)
    hold = {
        "schemaVersion": 3,
        "restoreId": restore_id,
        "backupId": backup_id,
        "objectDigest": digest,
        "targetId": target_id,
        "createdAt": _utc_iso(),
        "generation": 0,
        "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
    }
    try:
        put_json_if_absent(store, restore_hold_key(restore_id), hold)
    except AppError:  # pragma: no cover
        pass

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
        "selection": selection_value.canonical() if selection_value is not None else None,
        "selectionDigest": selection_digest_value,
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
        "selection": session["selection"],
        "selectionDigest": selection_digest_value,
        "hold": hold,
    }


def _download_member(
    store: Any,
    member: dict[str, Any],
    max_bytes: int | None,
    *,
    progress: Callable[[int], None] | None = None,
    checkpoint: Callable[[], None] | None = None,
    transferred: Callable[[int], None] | None = None,
    integrity_failure: Callable[[], None] | None = None,
    source_target_id: str | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY,
) -> bool:
    """Resume-download one chain member; returns True when fully fetched."""
    digest = str(member["objectDigest"])
    ciphertext_path = Path(str(member["ciphertextPath"]))
    ciphertext_path.parent.mkdir(parents=True, exist_ok=True)
    expected = int(member["expectedBytes"])
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
    remaining = max(0, expected - offset)
    budget = remaining if max_bytes is None else min(remaining, max(0, int(max_bytes)))
    manager = backup_transfer_budget.get_global_transfer_budget_manager()
    with manager.track_transfer(
        traffic_class=traffic_class,
        source_target_id=source_target_id,
        dest_target_id="restore-staging",
        estimated_bytes=budget,
    ) as transfer_id, ciphertext_path.open(mode) as handle:
        position = offset
        consumed = 0
        if max_bytes is None:
            pieces = manager.throttled_generator(
                store.get_stream(object_key(digest), offset=position),
                transfer_id=transfer_id,
                traffic_class=traffic_class,
                source_target_id=source_target_id,
                dest_target_id="restore-staging",
            )
        else:
            if budget > 0:
                manager.wait_for_bandwidth(transfer_id, budget)
            fetched = store.get_bytes(object_key(digest), offset=position, length=budget)
            pieces = iter((fetched,)) if fetched else iter(())
        for raw_piece in pieces:
            if consumed >= budget:
                break
            piece = raw_piece[: budget - consumed]
            if not piece:
                break
            handle.write(piece)
            hasher.update(piece)
            position += len(piece)
            consumed += len(piece)
            if transferred is not None:
                transferred(len(piece))
            if progress is not None:
                handle.flush()
                os.fsync(handle.fileno())
                progress(position)
            if checkpoint is not None:
                checkpoint()
        handle.flush()
        os.fsync(handle.fileno())
    downloaded = ciphertext_path.stat().st_size if ciphertext_path.is_file() else 0
    member["downloadedBytes"] = downloaded
    if downloaded >= expected:
        if hasher.hexdigest() != digest:
            if integrity_failure is not None:
                integrity_failure()
            raise AppError(f"Downloaded backup digest mismatch: {member['backupId']}", code=ErrorCode.INTERNAL, status=500)
        member["fetched"] = True
        return True
    return False


def _download_member_with_telemetry(
    session: dict[str, Any],
    store: Any,
    member: dict[str, Any],
    max_bytes: int | None,
) -> bool:
    started = time.monotonic()
    transferred_bytes = 0
    initial_bytes = int(member.get("downloadedBytes") or 0)

    def _transferred(amount: int) -> None:
        nonlocal transferred_bytes
        transferred_bytes += amount
        backup_recovery_telemetry.increment_counter(session, "transferBytes", amount)

    try:
        done = _download_member(
            store,
            member,
            max_bytes,
            checkpoint=lambda: _checkpoint_job_control(session, store),
            transferred=_transferred,
            integrity_failure=lambda: backup_recovery_telemetry.increment_counter(session, "integrityFailure"),
            source_target_id=str(session.get("targetId") or "") or None,
            traffic_class=(
                backup_transfer_budget.TrafficClass.P4_SCRUB_DRILL
                if session.get("drillOnly")
                else backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY
            ),
        )
    except backup_recovery_job.RecoveryJobStopped:
        result = "paused"
        raise
    except BaseException:
        result = "failed"
        raise
    else:
        result = "success"
        return done
    finally:
        if transferred_bytes:
            backup_recovery_telemetry.increment_counter(session, "componentsTransferred")
            if initial_bytes:
                backup_recovery_telemetry.increment_counter(session, "transferRetry")
        backup_recovery_telemetry.record_stage(
            session,
            stage="transfer",
            result=result,
            duration_ms=_elapsed_ms(started),
            byte_count=transferred_bytes,
            components=int(transferred_bytes > 0),
        )
        session["updatedAt"] = _utc_iso()
        _atomic_write_json(_session_path(str(session["restoreId"])), session)


def _fetch_object_set_controls(
    session: dict[str, Any],
    store: Any,
    *,
    max_bytes: int | None,
) -> dict[str, Any]:
    restore_id = str(session["restoreId"])
    chain = restore_members(session)
    index = int(session.get("controlIndex") or 0)
    while index < len(chain):
        member = chain[index]
        control = member.get("control")
        if not isinstance(control, dict):
            raise AppError("Object-set restore control descriptor is invalid", code=ErrorCode.INVALID_REQUEST, status=409)
        meta = store.stat(object_key(str(control.get("objectDigest") or "")))
        if meta is None:
            raise AppError(f"Backup control object is missing on target: {member['backupId']}", code=ErrorCode.NOT_FOUND, status=404)
        expected_etag = str(control.get("remoteETag") or "")
        if expected_etag and meta.etag and meta.etag != expected_etag:
            raise AppError("remote control object changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
        expected_version = control.get("remoteVersionId")
        if expected_version and meta.version_id and meta.version_id != expected_version:
            raise AppError("remote control object version changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
        try:
            done = _download_member_with_telemetry(session, store, control, max_bytes)
        except backup_recovery_job.RecoveryJobStopped as stopped:
            return {"restoreId": restore_id, "phase": stopped.phase}
        session["updatedAt"] = _utc_iso()
        if not done:
            session["controlIndex"] = index
            session["phase"] = "fetching-controls"
            _atomic_write_json(_session_path(restore_id), session)
            return {
                "restoreId": restore_id,
                "phase": "fetching-controls",
                "backupId": member["backupId"],
                "downloadedBytes": int(control.get("downloadedBytes") or 0),
                "expectedBytes": int(control.get("expectedBytes") or 0),
            }
        index += 1
        session["controlIndex"] = index
        session["chain"] = chain
        session["updatedAt"] = _utc_iso()
        _atomic_write_json(_session_path(restore_id), session)
    session["controlIndex"] = index
    session["phase"] = "controls-fetched"
    session["downloadedBytes"] = sum(int(item["control"].get("downloadedBytes") or 0) for item in chain)
    session["expectedBytes"] = sum(int(item["control"].get("expectedBytes") or 0) for item in chain)
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "phase": "controls-fetched",
        "downloadedBytes": session["downloadedBytes"],
        "expectedBytes": session["expectedBytes"],
        "controlObjects": len(chain),
        "next": "unlock-and-plan-projection",
    }


def _fetch_object_set_components(
    session: dict[str, Any],
    store: Any,
    *,
    max_bytes: int | None,
    on_verified: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    restore_id = str(session["restoreId"])
    chain = restore_members(session)
    pending = backup_recovery_state.required_components(session)
    states = backup_recovery_state.ensure_component_states(session)
    component_cache = backup_component_cache.ComponentCache()
    session["chain"] = chain
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(_session_path(restore_id), session)

    def _validate_source(component: dict[str, Any]) -> Any:
        digest = str(component.get("objectDigest") or "")
        meta = store.stat(object_key(digest))
        if meta is None or meta.size != int(component.get("expectedBytes") or -1):
            raise AppError("Required object-set component is missing", code=ErrorCode.NOT_FOUND, status=404)
        expected_etag = str(component.get("remoteETag") or "")
        if expected_etag and meta.etag and meta.etag != expected_etag:
            raise AppError("remote payload component changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
        expected_version = component.get("remoteVersionId")
        if expected_version and meta.version_id and meta.version_id != expected_version:
            raise AppError("remote payload component version changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
        return meta

    for component in pending:
        digest = str(component.get("objectDigest") or "")
        state = states[digest]
        component["downloadedBytes"] = int(state.get("downloadedBytes") or 0)
        component["fetched"] = str(state.get("state") or "") == "verified"
        if component["fetched"] and on_verified is not None:
            on_verified(component)

    if max_bytes is not None:
        for component in pending:
            if component["fetched"]:
                continue
            meta = _validate_source(component)
            expected_etag = str(component.get("remoteETag") or "")
            expected_version = component.get("remoteVersionId")
            if not expected_etag:
                component["remoteETag"] = meta.etag
            if not expected_version:
                component["remoteVersionId"] = meta.version_id
            backup_recovery_state.update_component_state(
                session,
                component,
                state="downloading",
                downloaded_bytes=int(component.get("downloadedBytes") or 0),
            )
            session["updatedAt"] = _utc_iso()
            _atomic_write_json(_session_path(restore_id), session)
            try:
                done = _download_member_with_telemetry(session, store, component, max_bytes)
            except backup_recovery_job.RecoveryJobStopped as stopped:
                downloaded = Path(str(component.get("ciphertextPath") or "")).stat().st_size
                component["downloadedBytes"] = downloaded
                backup_recovery_state.update_component_state(session, component, state="partial", downloaded_bytes=downloaded)
                _atomic_write_json(_session_path(restore_id), session)
                return {"restoreId": restore_id, "phase": stopped.phase}
            downloaded = int(component.get("downloadedBytes") or 0)
            backup_recovery_state.update_component_state(
                session,
                component,
                state="verified" if done else "partial",
                downloaded_bytes=downloaded,
            )
            session["chain"] = chain
            session["updatedAt"] = _utc_iso()
            _atomic_write_json(_session_path(restore_id), session)
            if not done:
                session["phase"] = "fetching-selected-components"
                _atomic_write_json(_session_path(restore_id), session)
                return {
                    "restoreId": restore_id,
                    "phase": "fetching-selected-components",
                    "downloadedBytes": int(component.get("downloadedBytes") or 0),
                    "expectedBytes": int(component.get("expectedBytes") or 0),
                    "requiredComponents": len(pending),
                }
            if on_verified is not None:
                on_verified(component)
    else:
        state_lock = threading.Lock()
        network_bytes = 0
        network_components = 0

        def _parallel_checkpoint() -> None:
            with state_lock:
                _checkpoint_job_control(session, store)

        def _make_download_task(component: dict[str, Any]) -> backup_component_transport.TransferTask:
            digest = str(component.get("objectDigest") or "")
            state = states[digest]

            def _execute() -> None:
                nonlocal network_bytes, network_components
                _parallel_checkpoint()
                expected_bytes = int(component.get("expectedBytes") or 0)
                cache_entry_existed = component_cache.path_for(digest).is_file()
                cached = component_cache.get(digest, expected_bytes)
                if cached is not None:
                    with state_lock:
                        backup_recovery_telemetry.increment_counter(session, "cacheHit")
                        component["ciphertextPath"] = str(cached)
                        component["downloadedBytes"] = expected_bytes
                        component["fetched"] = True
                        backup_recovery_state.update_component_state(
                            session,
                            component,
                            state="verified",
                            downloaded_bytes=expected_bytes,
                        )
                        session["chain"] = chain
                        session["updatedAt"] = _utc_iso()
                        _atomic_write_json(_session_path(restore_id), session)
                    if on_verified is not None:
                        on_verified(component)
                    return
                with state_lock:
                    backup_recovery_telemetry.increment_counter(
                        session,
                        "cacheCorruption" if cache_entry_existed else "cacheMiss",
                    )
                prior_path = Path(str(component.get("ciphertextPath") or ""))
                cache_partial = component_cache.partial_path(digest)
                if (
                    int(component.get("downloadedBytes") or 0) > 0
                    and prior_path.is_file()
                    and prior_path not in {cache_partial, component_cache.path_for(digest)}
                    and not cache_partial.exists()
                ):
                    cache_partial.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(prior_path, cache_partial)
                meta = _validate_source(component)
                working = dict(component)
                if not str(working.get("remoteETag") or ""):
                    working["remoteETag"] = meta.etag
                if not working.get("remoteVersionId"):
                    working["remoteVersionId"] = meta.version_id
                with state_lock:
                    component["ciphertextPath"] = str(component_cache.partial_path(digest))
                    component["remoteETag"] = working.get("remoteETag")
                    component["remoteVersionId"] = working.get("remoteVersionId")
                    backup_recovery_state.update_component_state(
                        session,
                        component,
                        state="downloading",
                        downloaded_bytes=int(component.get("downloadedBytes") or 0),
                    )
                    session["updatedAt"] = _utc_iso()
                    _atomic_write_json(_session_path(restore_id), session)
                progress_position = component_cache.partial_path(digest).stat().st_size if component_cache.partial_path(digest).is_file() else 0
                resumed_transfer = progress_position > 0
                component_used_network = False

                def _checkpoint(downloaded: int) -> None:
                    nonlocal component_used_network, network_bytes, network_components, progress_position
                    with state_lock:
                        transferred = max(0, downloaded - progress_position)
                        progress_position = max(progress_position, downloaded)
                        if transferred:
                            if not component_used_network:
                                component_used_network = True
                                network_components += 1
                                backup_recovery_telemetry.increment_counter(session, "componentsTransferred")
                                if resumed_transfer:
                                    backup_recovery_telemetry.increment_counter(session, "transferRetry")
                            network_bytes += transferred
                            backup_recovery_telemetry.increment_counter(session, "transferBytes", transferred)
                        component["downloadedBytes"] = downloaded
                        backup_recovery_state.update_component_state(
                            session,
                            component,
                            state="partial" if downloaded < int(component.get("expectedBytes") or 0) else "downloading",
                            downloaded_bytes=downloaded,
                        )
                        session["updatedAt"] = _utc_iso()
                        _atomic_write_json(_session_path(restore_id), session)
                    _parallel_checkpoint()

                try:
                    transfer_class = (
                        backup_transfer_budget.TrafficClass.P4_SCRUB_DRILL
                        if session.get("drillOnly")
                        else backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY
                    )
                    manager = backup_transfer_budget.get_global_transfer_budget_manager()

                    def _managed_component_stream(offset: int) -> Any:
                        return manager.throttled_generator(
                            store.get_stream(object_key(digest), offset=offset),
                            traffic_class=transfer_class,
                            source_target_id=str(session.get("targetId") or "") or None,
                            dest_target_id="restore-staging",
                        )

                    cached = component_cache.fetch(
                        digest,
                        expected_bytes,
                        _managed_component_stream,
                        progress=_checkpoint,
                    )
                except backup_recovery_job.RecoveryJobStopped:
                    partial_path = component_cache.partial_path(digest)
                    downloaded = partial_path.stat().st_size if partial_path.is_file() else 0
                    with state_lock:
                        component["downloadedBytes"] = downloaded
                        backup_recovery_state.update_component_state(
                            session,
                            component,
                            state="partial",
                            downloaded_bytes=downloaded,
                        )
                        session["updatedAt"] = _utc_iso()
                        _atomic_write_json(_session_path(restore_id), session)
                    raise
                except BaseException as exc:
                    partial_path = component_cache.partial_path(digest)
                    downloaded = partial_path.stat().st_size if partial_path.is_file() else 0
                    with state_lock:
                        if isinstance(exc, AppError) and str(exc) == "Component cache digest mismatch":
                            backup_recovery_telemetry.increment_counter(session, "integrityFailure")
                        component["downloadedBytes"] = downloaded
                        backup_recovery_state.update_component_state(
                            session,
                            component,
                            state="failed",
                            downloaded_bytes=downloaded,
                        )
                        session["updatedAt"] = _utc_iso()
                        _atomic_write_json(_session_path(restore_id), session)
                    raise
                with state_lock:
                    component["ciphertextPath"] = str(cached)
                    component["downloadedBytes"] = expected_bytes
                    component["fetched"] = True
                    backup_recovery_state.update_component_state(
                        session,
                        component,
                        state="verified",
                        downloaded_bytes=int(component["downloadedBytes"]),
                    )
                    session["chain"] = chain
                    session["updatedAt"] = _utc_iso()
                    _atomic_write_json(_session_path(restore_id), session)
                if on_verified is not None:
                    on_verified(component)

            priority = state.get("priority")
            return backup_component_transport.TransferTask(
                component_id=str(component.get("componentId") or digest),
                ciphertext_digest=digest,
                ciphertext_size=int(component.get("expectedBytes") or 0),
                execute=_execute,
                open_files=1,
                priority=int(priority) if isinstance(priority, int) else 2,
            )

        tasks = [_make_download_task(component) for component in pending if not component["fetched"]]
        transfer_started = time.monotonic()
        try:
            telemetry = backup_component_transport.default_scheduler().run(tasks)
        except backup_recovery_job.RecoveryJobStopped as stopped:
            latest = read_restore_session(restore_id) or session
            backup_recovery_telemetry.record_stage(
                latest,
                stage="transfer",
                result="paused",
                duration_ms=_elapsed_ms(transfer_started),
                byte_count=network_bytes,
                components=network_components,
            )
            latest["updatedAt"] = _utc_iso()
            _atomic_write_json(_session_path(restore_id), latest)
            return {"restoreId": restore_id, "phase": stopped.phase}
        except BaseException:
            backup_recovery_telemetry.record_stage(
                session,
                stage="transfer",
                result="failed",
                duration_ms=_elapsed_ms(transfer_started),
                byte_count=network_bytes,
                components=network_components,
            )
            session["phase"] = "fetching-selected-components"
            session["updatedAt"] = _utc_iso()
            _atomic_write_json(_session_path(restore_id), session)
            raise
        if tasks:
            backup_recovery_telemetry.record_stage(
                session,
                stage="transfer",
                result="success",
                duration_ms=_elapsed_ms(transfer_started),
                byte_count=network_bytes,
                components=network_components,
            )
        session["componentTransfer"] = telemetry.as_dict()
    session["phase"] = "components-fetched"
    control_bytes = sum(int(member["control"].get("downloadedBytes") or 0) for member in chain)
    component_bytes = sum(int(item.get("downloadedBytes") or 0) for item in pending)
    session["downloadedBytes"] = control_bytes + component_bytes
    session["expectedBytes"] = control_bytes + sum(int(item.get("expectedBytes") or 0) for item in pending)
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "phase": "components-fetched",
        "downloadedBytes": session["downloadedBytes"],
        "expectedBytes": session["expectedBytes"],
        "requiredComponents": len(pending),
        "next": "decrypt-and-materialize",
    }


def fetch_restore_session(
    restore_id: str,
    *,
    client: Any | None = None,
    max_bytes: int | None = None,
    _on_component_verified: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Idempotently continue a durable remote download until complete."""
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    phase = str(session.get("phase") or "")
    controlled_phase = _converge_job_control(session)
    if controlled_phase in {"paused", "aborted", "rolled-back", "recovery-required"}:
        return {"restoreId": restore_id, "phase": controlled_phase}
    phase = controlled_phase
    target_id = str(session.get("targetId") or "")
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
    _renew_session_holds(store, session)
    if phase in {"fetched", "chain-fetched", "controls-fetched", "components-fetched"} and not (
        phase == "components-fetched" and _on_component_verified is not None
    ):
        return {
            "restoreId": restore_id,
            "phase": str(session.get("phase") or "fetched"),
            "downloadedBytes": int(session.get("downloadedBytes") or 0),
            "expectedBytes": int(session.get("expectedBytes") or 0),
            "path": str(session.get("ciphertextPath") or ""),
            "next": "inspect-or-unlock",
        }
    if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        if str(session.get("phase") or "") in {"fetching-selected-components", "components-fetched", "decrypting-components"}:
            return _fetch_object_set_components(session, store, max_bytes=max_bytes, on_verified=_on_component_verified)
        return _fetch_object_set_controls(session, store, max_bytes=max_bytes)

    if str(session.get("snapshotKind") or "full") == "incremental":
        chain = session.get("chain") or []
        index = int(session.get("chainIndex") or 0)
        while index < len(chain):
            member = chain[index]
            expected_etag = str(member.get("remoteETag") or "")
            meta = store.stat(object_key(str(member["objectDigest"])))
            if meta is None:
                raise AppError(f"Backup ciphertext object is missing on target: {member['backupId']}", code=ErrorCode.NOT_FOUND, status=404)
            if expected_etag and meta.etag and meta.etag != expected_etag:
                raise AppError("remote object changed since restore session started", code=ErrorCode.INVALID_REQUEST, status=409)
            try:
                done = _download_member_with_telemetry(session, store, member, max_bytes)
            except backup_recovery_job.RecoveryJobStopped as stopped:
                return {"restoreId": restore_id, "phase": stopped.phase}
            session["updatedAt"] = _utc_iso()
            if not done:
                session["chainIndex"] = index
                session["chain"] = chain
                session["phase"] = "fetching-chain"
                _atomic_write_json(_session_path(restore_id), session)
                return {
                    "restoreId": restore_id,
                    "phase": "fetching-chain",
                    "backupId": str(member["backupId"]),
                    "downloadedBytes": int(member["downloadedBytes"] or 0),
                    "expectedBytes": int(member["expectedBytes"] or 0),
                    "path": str(member["ciphertextPath"] or ""),
                    "chain": [str(item["backupId"]) for item in chain],
                }
            index += 1
        session["chainIndex"] = index
        session["chain"] = chain
        session["phase"] = "chain-fetched"
        session["downloadedBytes"] = sum(int(item.get("downloadedBytes") or 0) for item in chain)
        session["expectedBytes"] = sum(int(item.get("expectedBytes") or 0) for item in chain)
        _atomic_write_json(_session_path(restore_id), session)
        return {
            "restoreId": restore_id,
            "phase": "chain-fetched",
            "downloadedBytes": int(session["downloadedBytes"]),
            "expectedBytes": int(session["expectedBytes"]),
            "chain": [str(item["backupId"]) for item in chain],
            "ciphertextPaths": [str(item["ciphertextPath"]) for item in chain],
            "next": "inspect-or-unlock",
        }

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

    member = {
        "backupId": str(session.get("backupId") or ""),
        "objectDigest": digest,
        "expectedBytes": int(session.get("expectedBytes") or meta.size),
        "ciphertextPath": str(session.get("ciphertextPath") or (_session_dir(restore_id) / str(session.get("filename") or "package.age"))),
        "downloadedBytes": int(session.get("downloadedBytes") or 0),
    }
    try:
        done = _download_member_with_telemetry(session, store, member, max_bytes)
    except backup_recovery_job.RecoveryJobStopped as stopped:
        return {"restoreId": restore_id, "phase": stopped.phase}
    session["downloadedBytes"] = int(member["downloadedBytes"] or 0)
    session["updatedAt"] = _utc_iso()
    if done:
        session["phase"] = "fetched"
        _atomic_write_json(_session_path(restore_id), session)
        return {
            "restoreId": restore_id,
            "phase": "fetched",
            "downloadedBytes": int(session["downloadedBytes"]),
            "expectedBytes": int(session["expectedBytes"]),
            "path": str(member["ciphertextPath"]),
            "objectDigest": digest,
            "next": "inspect-or-unlock",
        }
    session["phase"] = "fetching"
    _atomic_write_json(_session_path(restore_id), session)
    return {
        "restoreId": restore_id,
        "phase": "fetching",
        "downloadedBytes": int(session["downloadedBytes"]),
        "expectedBytes": int(session["expectedBytes"]),
        "path": str(member["ciphertextPath"]),
        "objectDigest": digest,
    }


def preview_restore_from_target(
    *,
    target_id: str,
    backup_id: str,
    selection: Any,
    restore_id: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Fetch the whole chain, metadata-extract it and report the projected plan.

    Whole-Age sessions download every member before reporting accurate byte
    counts, so both ``selectiveFetchSupported`` and ``networkSelective`` are
    ``False`` for this legacy storage protocol. Object-set-v1 sessions report
    ``selectiveFetchSupported: True`` and set ``networkSelective`` to whether
    the required-component closure is a strict subset of the chain. The client provides a secret first so the metadata plane can be
    decrypted; the preview re-puts it so the later materialize step can still
    consume it.
    """
    selection_value = backup_projection.normalize_selection(selection)
    assert selection_value is not None
    created = create_restore_from_target(
        target_id=target_id,
        backup_id=backup_id,
        client=client,
        selection=selection_value.canonical(),
        restore_id=restore_id,
    )
    restore_id = str(created["restoreId"])
    result = fetch_restore_session(restore_id, client=client)
    phase = str(result.get("phase") or "")
    if phase not in {"fetched", "chain-fetched", "controls-fetched"}:
        return {
            "restoreId": restore_id,
            "phase": phase,
            "selectionDigest": created.get("selectionDigest"),
            "downloadedBytes": int(result.get("downloadedBytes") or 0),
            "expectedBytes": int(result.get("expectedBytes") or 0),
            "requiresSecret": False,
        }
    if not backup_crypto.has_secret(restore_id):
        return {
            "restoreId": restore_id,
            "phase": phase,
            "selectionDigest": created.get("selectionDigest"),
            "requiresSecret": True,
        }
    kind, secret = backup_crypto.consume_secret(restore_id)
    try:
        session = read_restore_session(restore_id)
        if session is None:
            raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
        if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
            _projection, report = _plan_object_set_projection(session, kind=kind, secret=secret)
            return {
                "restoreId": restore_id,
                "phase": "preview-planned",
                "selection": selection_value.canonical(),
                "selectionDigest": report.get("selectionDigest") or created.get("selectionDigest"),
                "requiresSecret": False,
                "projection": report,
            }
        base = _session_dir(restore_id)
        members = restore_members(session)
        secret_kind: Literal["passphrase", "age-identity"] = "passphrase" if kind != "age-identity" else "age-identity"
        decrypted_paths: list[Path] = []
        for index, member in enumerate(members):
            ciphertext = Path(str(member["ciphertextPath"]))
            decrypted = base / f"preview-decrypted-{index}.dsibackup"
            backup_crypto.decrypt_file(ciphertext, decrypted, kind=secret_kind, secret=secret)
            decrypted_paths.append(decrypted)
        packages = _metadata_chain_packages(decrypted_paths, base)
        ciphertext_download = sum(int(item.get("expectedBytes") or 0) for item in members)
        plan = backup_projection.plan_projection(
            selection_value,
            packages,
            ciphertext_download_bytes=ciphertext_download,
        )
        session["projectionPlan"] = plan.report
        session["phase"] = "preview-planned"
        _atomic_write_json(_session_path(restore_id), session)
        return {
            "restoreId": restore_id,
            "phase": "preview-planned",
            "selection": selection_value.canonical(),
            "selectionDigest": plan.report["selectionDigest"],
            "requiresSecret": False,
            "projection": plan.report,
        }
    finally:
        backup_crypto.put_secret_bytes(restore_id, kind, bytearray(secret))
        secret[:] = b"\x00" * len(secret)


def preflight_restore_session(
    restore_id: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Build/reuse a verified plan and perform read-only recovery checks."""
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    if str(session.get("storageProtocol") or "") != backup_object_set.OBJECT_SET_V1:
        raise AppError("Recovery preflight requires object-set-v1", code=ErrorCode.INVALID_REQUEST, status=409)

    base = _session_dir(restore_id)
    loaded_plan = backup_verified_plan.load_verified_plan(base, session)
    if loaded_plan is None:
        if not backup_crypto.has_secret(restore_id):
            raise AppError(
                "Recovery preflight requires a verified plan or available unlock secret",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        kind, secret = backup_crypto.consume_secret(restore_id)
        try:
            _projection, projection_report = _plan_object_set_projection(session, kind=kind, secret=secret)
        finally:
            backup_crypto.put_secret_bytes(restore_id, kind, bytearray(secret))
            secret[:] = b"\x00" * len(secret)
        session = read_restore_session(restore_id) or session
    else:
        _projection, projection_report = loaded_plan

    target_id = str(session.get("targetId") or "")
    target_kind = "unavailable"
    store: Any | None = None
    catalog: dict[str, dict[str, Any]] | None = None
    try:
        target = backup_publish.resolve_target(target_id, write_intent=False)
        target_kind = target.kind
        store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
        catalog = backup_catalog.catalog_state(target.root) if target.root is not None else backup_catalog.catalog_state_store(store)
    except AppError:
        store = None

    report = backup_recovery_preflight.evaluate_preflight(
        session,
        projection_report,
        store=store,
        target_kind=target_kind,
        catalog=catalog,
    )
    session["preflight"] = report
    session["preflightedAt"] = _utc_iso()
    _atomic_write_json(_session_path(restore_id), session)
    if not bool(report.get("ready")):
        raw_reasons = report.get("blockingReasons")
        reasons = [item for item in raw_reasons if isinstance(item, dict)] if isinstance(raw_reasons, list) else []
        reason_codes = {str(item.get("code") or "") for item in reasons}
        capacity_blocked = "insufficient-disk" in reason_codes or "disk-probe-failed" in reason_codes
        raise AppError(
            "Recovery preflight is blocked",
            code=ErrorCode.RECOVERY_PREFLIGHT_CAPACITY if capacity_blocked else ErrorCode.INVALID_REQUEST,
            status=409,
            details={"preflight": report},
        )
    return report


def restore_from_target(*, target_id: str, backup_id: str, client: Any | None = None) -> dict[str, Any]:  # pragma: no cover - thin wrapper
    """Compatibility helper: create session and fetch to completion in one call."""
    created = create_restore_from_target(target_id=target_id, backup_id=backup_id, client=client)
    restore_id = str(created["restoreId"])
    while True:
        result = fetch_restore_session(restore_id, client=client)
        if str(result.get("phase") or "") in {"fetched", "chain-fetched", "controls-fetched"}:
            paths = result.get("ciphertextPaths")
            first_path = paths[0] if isinstance(paths, list) and paths else result.get("path")
            return {
                **result,
                "targetId": target_id,
                "backupId": backup_id,
                "filename": Path(str(first_path or "package.age")).name,
                "size": int(result.get("downloadedBytes") or 0),
                "requiresSecret": str(result.get("phase") or "") == "controls-fetched",
                "hold": {"restoreId": restore_id},
            }


def release_restore_hold(store: Any, restore_id: str) -> None:
    backup_component_cache.ComponentCache().unpin(restore_id)
    try:
        store.delete_if_match(restore_hold_key(restore_id))
    except AppError:  # pragma: no cover
        pass


def _release_session_holds(session: dict[str, Any]) -> None:
    """Release every remote hold this restore session created."""
    backup_component_cache.ComponentCache().unpin(str(session.get("restoreId") or ""))
    target_id = str(session.get("targetId") or "")
    if not target_id:
        return
    try:
        target = backup_publish.resolve_target(target_id, write_intent=False)
        store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False)
    except AppError:  # pragma: no cover
        return
    keys = list(session.get("holdKeys") or [])
    if not keys and session.get("holdKey"):
        keys = [str(session["holdKey"])]
    for key in keys:
        try:
            store.delete_if_match(str(key))
        except AppError:  # pragma: no cover
            pass


def _write_checksums(tree_root: Path, manifest: dict[str, Any]) -> None:
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (tree_root / "manifest.json").write_bytes(manifest_bytes)
    lines = [f"{str(item.get('sha256') or '')}  {str(item.get('path') or '')}" for item in manifest.get("files") or []]
    lines.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
    (tree_root / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _normalized_full_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Present a fully materialized chain as a complete, verifiable tree."""
    normalized = dict(manifest)
    normalized["snapshotKind"] = "full"
    normalized.pop("deltaFiles", None)
    return normalized


def _metadata_chain_packages(
    decrypted_paths: list[Path],
    base: Path,
) -> list[backup_projection.ChainPackage]:
    """Extract only metadata from each decrypted member and build the planner input."""
    packages: list[backup_projection.ChainPackage] = []
    for index, decrypted in enumerate(decrypted_paths):
        meta_dir = base / f"metadata-{index}"
        manifest = backups.extract_archive_metadata(decrypted, meta_dir)
        snapshot_kind = str(manifest.get("snapshotKind") or "full")
        operations: dict[str, Any] | None = None
        pack_index: dict[str, Any] | None = None
        if snapshot_kind == "incremental":
            ops_path = meta_dir / "delta" / "operations.json"
            if ops_path.is_file():
                try:
                    operations = json.loads(ops_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):  # pragma: no cover - validated at plan time
                    operations = None
            index_path = meta_dir / backup_pack.PACK_INDEX_PATH
            if index_path.is_file():
                try:
                    pack_index = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):  # pragma: no cover - validated at plan time
                    pack_index = None
        contributor_ids = frozenset(str(item.get("id") or "") for item in manifest.get("contributors") or [])
        frontend = bool(manifest.get("frontend"))
        external_mcp = bool((manifest.get("coverage") or {}).get("externalContributors"))
        packages.append(
            backup_projection.ChainPackage(
                snapshot_kind=snapshot_kind,
                files=tuple(backup_incremental_restore._manifest_files(manifest)),
                root_digest=str(((manifest.get("snapshot") or {}).get("rootDigest")) or ""),
                operations=operations,
                pack_index=pack_index,
                frontend=frontend,
                contributor_ids=contributor_ids,
                external_mcp=external_mcp,
                manifest=manifest,
            )
        )
    return packages


def _validated_component_control(
    member: dict[str, Any],
    component_map: Any,
    payload_index: Any,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if (
        not isinstance(component_map, dict)
        or component_map.get("schemaVersion") != 1
        or not isinstance(payload_index, dict)
        or payload_index.get("schemaVersion") != 1
        or payload_index.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
    ):
        raise AppError("Object-set control metadata is invalid", code=ErrorCode.INVALID_PAYLOAD)
    raw_paths = component_map.get("paths")
    raw_components = component_map.get("components")
    raw_descriptors = payload_index.get("payloadComponents")
    if not isinstance(raw_paths, dict) or not isinstance(raw_components, dict) or not isinstance(raw_descriptors, dict):
        raise AppError("Object-set component map is invalid", code=ErrorCode.INVALID_PAYLOAD)
    if set(raw_components) != set(raw_descriptors):
        raise AppError("Object-set component inventory is inconsistent", code=ErrorCode.INVALID_PAYLOAD)

    normalized_paths: dict[str, str] = {}
    for component_id, entries in raw_components.items():
        if not isinstance(component_id, str) or not isinstance(entries, list) or not entries or any(not isinstance(path, str) for path in entries):
            raise AppError("Object-set component path map is invalid", code=ErrorCode.INVALID_PAYLOAD)
        for raw_path in entries:
            path = backup_object_set._safe_relative_path(raw_path)
            if path in normalized_paths:
                raise AppError("Object-set component paths overlap", code=ErrorCode.INVALID_PAYLOAD)
            normalized_paths[path] = component_id
    if len(raw_paths) != len(normalized_paths):
        raise AppError("Object-set component path map is inconsistent", code=ErrorCode.INVALID_PAYLOAD)
    for raw_path, component_id in raw_paths.items():
        if not isinstance(raw_path, str) or not isinstance(component_id, str):
            raise AppError("Object-set component path map is invalid", code=ErrorCode.INVALID_PAYLOAD)
        path = backup_object_set._safe_relative_path(raw_path)
        if normalized_paths.get(path) != component_id:
            raise AppError("Object-set component path map is inconsistent", code=ErrorCode.INVALID_PAYLOAD)
    if set(normalized_paths.values()) != set(raw_components):
        raise AppError("Object-set component inventory contains an unreachable payload", code=ErrorCode.INVALID_PAYLOAD)

    remote_by_digest = {str(item.get("digest") or ""): item for item in member.get("objects") or [] if isinstance(item, dict)}
    control_digest = str(member.get("controlObjectDigest") or "")
    expected_payload_digests = set(remote_by_digest) - {control_digest}
    descriptors: dict[str, dict[str, Any]] = {}
    descriptor_digests: set[str] = set()
    for component_id, descriptor in raw_descriptors.items():
        if not isinstance(component_id, str) or not isinstance(descriptor, dict):
            raise AppError("Object-set payload descriptor is invalid", code=ErrorCode.INVALID_PAYLOAD)
        digest = str(descriptor.get("ciphertextDigest") or "")
        size = descriptor.get("ciphertextSize")
        plaintext_size = descriptor.get("plaintextSize")
        plaintext_digest = str(descriptor.get("plaintextSha256") or "")
        remote = remote_by_digest.get(digest)
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or digest in descriptor_digests
            or remote is None
            or digest == control_digest
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or int(remote.get("size") or -1) != size
            or isinstance(plaintext_size, bool)
            or not isinstance(plaintext_size, int)
            or plaintext_size < 0
            or len(plaintext_digest) != 64
            or any(char not in "0123456789abcdef" for char in plaintext_digest)
        ):
            raise AppError("Object-set control references a foreign component or invalid commitment", code=ErrorCode.INVALID_PAYLOAD)
        descriptor_digests.add(digest)
        descriptors[component_id] = descriptor
    if descriptor_digests != expected_payload_digests:
        raise AppError("Object-set receipt contains a foreign component", code=ErrorCode.INVALID_PAYLOAD)
    return normalized_paths, descriptors


def _plan_object_set_projection(
    session: dict[str, Any],
    *,
    kind: str,
    secret: bytearray,
) -> tuple[backup_projection.ProjectionPlan | None, dict[str, Any]]:
    restore_id = str(session["restoreId"])
    base = _session_dir(restore_id)
    members = restore_members(session)
    secret_kind: Literal["passphrase", "age-identity"] = "passphrase" if kind != "age-identity" else "age-identity"
    _set_phase(session, "decrypting-controls")
    decrypted_controls: list[Path] = []
    for index, member in enumerate(members):
        control = member.get("control")
        if not isinstance(control, dict):
            raise AppError("Object-set restore control descriptor is invalid", code=ErrorCode.INVALID_REQUEST, status=409)
        ciphertext = Path(str(control.get("ciphertextPath") or ""))
        if not ciphertext.is_file():
            raise AppError("Object-set control ciphertext is unavailable", code=ErrorCode.NOT_FOUND, status=404)
        decrypted = base / f"control-decrypted-{index}.zip"
        backup_crypto.decrypt_file(ciphertext, decrypted, kind=secret_kind, secret=secret)
        decrypted_controls.append(decrypted)
    _set_phase(session, "planning-projection")
    packages = _metadata_chain_packages(decrypted_controls, base)
    for index, (member, package) in enumerate(zip(members, packages, strict=True)):
        manifest = package.manifest
        backup_object_set.verify_control_metadata(base / f"metadata-{index}")
        if (
            not isinstance(manifest, dict)
            or str(manifest.get("backupId") or "") != str(member.get("backupId") or "")
            or str(manifest.get("snapshotKind") or "full") != str(member.get("snapshotKind") or "full")
            or str(manifest.get("storageProtocol") or "") != backup_object_set.OBJECT_SET_V1
        ):
            raise AppError("Object-set control manifest does not match its receipt", code=ErrorCode.INVALID_PAYLOAD)
    selection = backup_projection.normalize_selection(session.get("selection"))
    projection: backup_projection.ProjectionPlan | None = None
    if selection is not None:
        projection = backup_projection.plan_projection(
            selection,
            packages,
            ciphertext_download_bytes=sum(int(member["control"].get("expectedBytes") or 0) for member in members),
            selection_digest_value=str(session.get("selectionDigest") or ""),
        )

    whole_chain_bytes = sum(int(item.get("size") or 0) for member in members for item in member.get("objects") or [])
    control_bytes = sum(int(member["control"].get("expectedBytes") or 0) for member in members)
    required_payload_bytes = 0
    required_count = 0
    total_payload_count = 0
    every_required_component_fetched = True
    for index, member in enumerate(members):
        existing_required = {
            str(item.get("objectDigest") or ""): item
            for item in member.get("requiredComponents") or []
            if isinstance(item, dict)
        }
        meta_dir = base / f"metadata-{index}"
        try:
            component_map = json.loads((meta_dir / "component-map.json").read_text(encoding="utf-8"))
            payload_index = json.loads((meta_dir / "payload-index.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError("Object-set control metadata is invalid", code=ErrorCode.INVALID_PAYLOAD) from exc
        raw_paths, raw_descriptors = _validated_component_control(member, component_map, payload_index)
        total_payload_count += len(raw_descriptors)
        if projection is None:
            needed_paths = set(str(path) for path in raw_paths)
        elif index == 0:
            needed_paths = set(projection.needed_full_entries)
        else:
            packs, _blobs, standalone = backup_projection.layer_needed_payloads(
                packages,
                projection.produced_by_layer,
                index,
            )
            needed_paths = packs | standalone
        required_ids: set[str] = set()
        for path in needed_paths:
            component_id = raw_paths.get(path)
            if not isinstance(component_id, str) or component_id not in raw_descriptors:
                raise AppError(f"Object-set payload path has no component: {path}", code=ErrorCode.INVALID_PAYLOAD)
            required_ids.add(component_id)
        remote_by_digest = {str(item.get("digest") or ""): item for item in member.get("objects") or []}
        required: list[dict[str, Any]] = []
        for component_id in sorted(required_ids):
            descriptor = raw_descriptors.get(component_id)
            if not isinstance(descriptor, dict):
                raise AppError("Object-set payload descriptor is invalid", code=ErrorCode.INVALID_PAYLOAD)
            digest = str(descriptor.get("ciphertextDigest") or "")
            size = int(descriptor.get("ciphertextSize") or -1)
            remote = remote_by_digest.get(digest)
            if remote is None or int(remote.get("size") or -1) != size or digest == str(member.get("controlObjectDigest") or ""):
                raise AppError("Object-set control references a foreign component", code=ErrorCode.INVALID_PAYLOAD)
            existing = existing_required.get(digest) or {}
            required_item = {
                    "backupId": str(member["backupId"]),
                    "componentId": component_id,
                    "objectDigest": digest,
                    "expectedBytes": size,
                    "downloadedBytes": int(existing.get("downloadedBytes") or 0),
                    "remoteETag": existing.get("remoteETag") or remote.get("remoteETag"),
                    "remoteVersionId": existing.get("remoteVersionId") or remote.get("remoteVersionId"),
                    "ciphertextPath": str(existing.get("ciphertextPath") or base / f"payload-{index:04d}-{component_id}.age"),
                    "fetched": bool(existing.get("fetched")),
                    "plaintextSize": int(descriptor.get("plaintextSize") or -1),
                    "plaintextSha256": str(descriptor.get("plaintextSha256") or ""),
                }
            required.append(required_item)
            every_required_component_fetched = every_required_component_fetched and bool(required_item["fetched"])
            required_payload_bytes += size
        member["requiredComponents"] = required
        required_count += len(required)

    required_bytes = control_bytes + required_payload_bytes
    saved = max(0, whole_chain_bytes - required_bytes)
    network_selective = required_count < total_payload_count
    network_report = {
        "selectiveFetchSupported": True,
        "networkSelective": network_selective,
        "wholeChainCiphertextBytes": whole_chain_bytes,
        "requiredCiphertextBytes": required_bytes,
        "networkBytesSaved": saved,
        "networkSavingsRatio": round(saved / whole_chain_bytes, 4) if whole_chain_bytes else 0.0,
        "requiredComponents": required_count,
        "totalComponents": total_payload_count,
    }
    if projection is not None:
        report = dict(projection.report)
        report["networkSelectivityReason"] = "object-set-component-closure"
        report.update(network_report)
        bytes_report = dict(report.get("bytes") or {})
        bytes_report["ciphertextDownloadBytes"] = required_bytes
        report["bytes"] = bytes_report
    else:
        final_files = backup_projection._chain_roots(packages)[-1]
        materialized_bytes = sum(int(item.size) for item in final_files)
        report = {
            **network_report,
            "bytes": {
                "selectedLogicalBytes": materialized_bytes,
                "dependencyBytes": 0,
                "ciphertextDownloadBytes": required_bytes,
                "estimatedMaterializedBytes": materialized_bytes,
            },
        }
    session["chain"] = members
    session["projectionPlan"] = report
    backup_recovery_state.ensure_component_states(session)
    required_digests = [
        str(component.get("objectDigest") or "")
        for member in members
        for component in member.get("requiredComponents") or []
        if isinstance(component, dict)
    ]
    backup_component_cache.ComponentCache().pin(restore_id, required_digests)
    backup_verified_plan.write_verified_plan(base, session, projection, report)
    session["phase"] = "components-fetched" if every_required_component_fetched else "fetching-selected-components"
    _atomic_write_json(_session_path(restore_id), session)
    return projection, report


def _selective_extract_members(
    decrypted_paths: list[Path],
    packages: list[backup_projection.ChainPackage],
    base: Path,
    projection: backup_projection.ProjectionPlan,
) -> list[Path]:
    extracted_dirs: list[Path] = []
    for index, decrypted in enumerate(decrypted_paths):
        extracted = base / f"projected-{index}"
        raw_manifest = packages[index].manifest
        manifest: dict[str, Any] = {}
        if isinstance(raw_manifest, dict):
            manifest = raw_manifest
        if index == 0:
            backups.extract_selected_archive(
                decrypted,
                extracted,
                needed_full=set(projection.needed_full_entries),
                needed_packs=set(),
                needed_standalone=set(),
                manifest=manifest,
            )
        else:
            layer_packs, _layer_blobs, layer_standalone = backup_projection.layer_needed_payloads(
                packages,
                projection.produced_by_layer,
                index,
            )
            backups.extract_selected_archive(
                decrypted,
                extracted,
                needed_full=set(),
                needed_packs=layer_packs,
                needed_standalone=layer_standalone,
                manifest=manifest,
            )
        extracted_dirs.append(extracted)
    return extracted_dirs


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_component_pipeline(
    session: dict[str, Any],
    *,
    secret_kind: Literal["passphrase", "age-identity"],
    secret: bytearray,
    client: Any | None,
    components_ready: bool,
    queue_observer: Callable[[int], None] | None = None,
) -> list[Path]:
    """Overlap verified ciphertext delivery with bounded decrypt/extract work."""
    restore_id = str(session["restoreId"])
    base = _session_dir(restore_id)
    members = restore_members(session)
    component_queue: queue.Queue[tuple[dict[str, Any], Path, list[str], threading.Lock] | None] = queue.Queue(
        maxsize=CRYPTO_QUEUE_MAX_COMPONENTS
    )
    worker_count = 2
    errors: list[BaseException] = []
    error_lock = threading.Lock()
    queued: set[str] = set()
    enqueue_lock = threading.Lock()
    max_queued = 0
    layer_locks = [threading.Lock() for _ in members]
    layers: list[Path] = []
    component_paths: dict[str, tuple[Path, list[str], threading.Lock]] = {}

    for index, member in enumerate(members):
        layer = base / f"object-layer-{index}"
        shutil.rmtree(layer, ignore_errors=True)
        metadata = base / f"metadata-{index}"
        if not metadata.is_dir():
            raise AppError("Verified object-set Control metadata is unavailable", code=ErrorCode.INVALID_PAYLOAD)
        shutil.copytree(metadata, layer)
        layers.append(layer)
        try:
            component_map = json.loads((layer / "component-map.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError("Object-set component map is invalid", code=ErrorCode.INVALID_PAYLOAD) from exc
        components = component_map.get("components") if isinstance(component_map, dict) else None
        if not isinstance(components, dict):
            raise AppError("Object-set component map is invalid", code=ErrorCode.INVALID_PAYLOAD)
        for required in member.get("requiredComponents") or []:
            if not isinstance(required, dict):
                raise AppError("Object-set required component is invalid", code=ErrorCode.INVALID_PAYLOAD)
            component_id = str(required.get("componentId") or "")
            expected_paths = components.get(component_id)
            if not isinstance(expected_paths, list) or any(not isinstance(path, str) for path in expected_paths):
                raise AppError("Object-set component path map is invalid", code=ErrorCode.INVALID_PAYLOAD)
            binding = (layer, expected_paths, layer_locks[index])
            component_paths[str(required.get("objectDigest") or component_id)] = binding
            component_paths[component_id] = binding

    def _worker() -> None:
        while True:
            item = component_queue.get()
            if item is None:
                component_queue.task_done()
                return
            required, layer, expected_paths, layer_lock = item
            component_id = str(required.get("componentId") or "")
            digest = str(required.get("objectDigest") or "")
            safe_token = digest if len(digest) == 64 else hashlib.sha256(component_id.encode("utf-8")).hexdigest()
            decrypted = base / f"payload-decrypted-{safe_token}.zip"
            try:
                with error_lock:
                    if errors:
                        continue
                ciphertext = Path(str(required.get("ciphertextPath") or ""))
                backup_crypto.decrypt_file(ciphertext, decrypted, kind=secret_kind, secret=secret)
                if (
                    decrypted.stat().st_size != int(required.get("plaintextSize") or -1)
                    or _sha256_path(decrypted) != str(required.get("plaintextSha256") or "")
                ):
                    raise AppError("Object-set component plaintext commitment mismatch", code=ErrorCode.INVALID_PAYLOAD)
                with layer_lock:
                    backup_object_set.extract_component_archive(decrypted, layer, expected_paths)
            except BaseException as exc:
                with error_lock:
                    if not errors:
                        errors.append(exc)
            finally:
                backup_unattended.scrub_plaintext_file(decrypted)
                component_queue.task_done()

    workers = [threading.Thread(target=_worker, name=f"restore-crypto-{index}", daemon=True) for index in range(worker_count)]
    for worker in workers:
        worker.start()

    def _enqueue(required: dict[str, Any]) -> None:
        nonlocal max_queued
        digest = str(required.get("objectDigest") or "")
        with enqueue_lock:
            if digest in queued:
                return
            queued.add(digest)
        binding = component_paths.get(digest) or component_paths.get(str(required.get("componentId") or ""))
        if binding is None:
            raise AppError("Object-set required component is foreign", code=ErrorCode.INVALID_PAYLOAD)
        layer, expected_paths, layer_lock = binding
        component_queue.put((required, layer, expected_paths, layer_lock))
        with enqueue_lock:
            max_queued = max(max_queued, component_queue.qsize())
        if queue_observer is not None:
            queue_observer(component_queue.qsize())

    try:
        if components_ready:
            for member in members:
                for required in member.get("requiredComponents") or []:
                    if isinstance(required, dict):
                        _enqueue(required)
        else:
            fetch_restore_session(restore_id, client=client, _on_component_verified=_enqueue)
        component_queue.join()
    finally:
        for _worker_thread in workers:
            component_queue.put(None)
        component_queue.join()
        for worker in workers:
            worker.join()
        for path in base.glob("payload-decrypted-*.zip"):
            backup_unattended.scrub_plaintext_file(path)
    if errors:
        raise errors[0]
    latest_session = read_restore_session(restore_id) or session
    latest_session["cryptoPipeline"] = {
        "maxQueuedComponents": max_queued,
        "queueCapacity": CRYPTO_QUEUE_MAX_COMPONENTS,
        "workers": worker_count,
    }
    latest_session["updatedAt"] = _utc_iso()
    _atomic_write_json(_session_path(restore_id), latest_session)
    return layers


def _materialize_object_set_session(
    session: dict[str, Any],
    *,
    kind: str,
    secret: bytearray,
    client: Any | None,
) -> dict[str, Any]:
    restore_id = str(session["restoreId"])
    base = _session_dir(restore_id)
    target_id = str(session.get("targetId") or "")
    if target_id:
        target = backup_publish.resolve_target(target_id, write_intent=False)
        store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)
        _renew_session_holds(store, session)
    crypto_started = time.monotonic()
    loaded_plan = backup_verified_plan.load_verified_plan(base, session)
    if loaded_plan is None:
        projection, projection_report = _plan_object_set_projection(session, kind=kind, secret=secret)
    else:
        projection, projection_report = loaded_plan
    session = read_restore_session(restore_id) or session
    secret_kind: Literal["passphrase", "age-identity"] = "passphrase" if kind != "age-identity" else "age-identity"
    members = restore_members(session)
    for index, member in enumerate(members):
        metadata = base / f"metadata-{index}"
        if metadata.is_dir():
            continue
        control = member["control"]
        control_decrypted = base / f"control-decrypted-{index}.zip"
        backup_crypto.decrypt_file(Path(str(control["ciphertextPath"])), control_decrypted, kind=secret_kind, secret=secret)
        backups.extract_archive_metadata(control_decrypted, metadata)
    components_ready = str(session.get("phase") or "") == "components-fetched"
    _set_phase(session, "decrypting-components")
    extracted_dirs = _materialize_component_pipeline(
        session,
        secret_kind=secret_kind,
        secret=secret,
        client=client,
        components_ready=components_ready,
    )
    session = read_restore_session(restore_id) or session
    members = restore_members(session)
    crypto_bytes = sum(
        int(item.get("expectedBytes") or 0)
        for member in members
        for item in [member.get("control"), *(member.get("requiredComponents") or [])]
        if isinstance(item, dict)
    )
    crypto_components = sum(1 + len(member.get("requiredComponents") or []) for member in members)
    backup_recovery_telemetry.record_stage(
        session,
        stage="crypto",
        result="success",
        duration_ms=_elapsed_ms(crypto_started),
        byte_count=crypto_bytes,
        components=crypto_components,
    )
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(_session_path(restore_id), session)
    _set_phase(session, "materializing")
    materialization_started = time.monotonic()
    extracted = base / "extracted"
    shutil.rmtree(extracted, ignore_errors=True)
    final_manifest = backup_incremental_restore.materialize_chain(extracted_dirs, extracted, projection=projection)
    snapshot_kind = str(session.get("snapshotKind") or "full")
    if projection is None:
        final_manifest = _normalized_full_manifest(final_manifest)
        _write_checksums(extracted, final_manifest)
        backups._verify_manifest_tree(extracted)
    materialized_bytes, materialized_files = _manifest_work(final_manifest)
    backup_recovery_telemetry.record_stage(
        session,
        stage="materialization",
        result="success",
        duration_ms=_elapsed_ms(materialization_started),
        byte_count=materialized_bytes,
        components=materialized_files,
    )
    _set_phase(session, "verified")
    result: dict[str, Any] = {
        "restoreId": restore_id,
        "phase": "materialized",
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "snapshotKind": snapshot_kind,
        "tree": str(extracted),
        "manifest": final_manifest,
    }
    if snapshot_kind == "incremental":
        result["chain"] = [str(item["backupId"]) for item in members]
    if projection is not None:
        result["projection"] = projection_report
    return result


def materialize_restore_session(
    restore_id: str,
    *,
    kind: str = "passphrase",
    secret: bytearray,
    client: Any | None = None,
    _drill: bool = False,
) -> dict[str, Any]:
    """Decrypt the fetched chain, materialize the workspace and verify every root.

    Every ancestor is decrypted and extracted, then :func:`materialize_chain`
    applies the delta operations layer by layer, verifying each Merkle
    transition. The resulting complete tree carries a normalized manifest so the
    federated restore transaction can consume it without re-extracting.
    """
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    if not _drill:
        _assert_live_restore_allowed(_session_dir(restore_id), session)
    if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        return _materialize_object_set_session(session, kind=kind, secret=secret, client=client)
    phase = str(session.get("phase") or "")
    if phase not in {
        "fetched",
        "chain-fetched",
        "controls-fetched",
        "fetching-selected-components",
        "components-fetched",
        "decrypting-controls",
        "planning-projection",
        "decrypting-components",
        "decrypting-chain",
        "materializing",
        "verified",
    }:
        raise AppError("Restore session has not finished fetching", code=ErrorCode.INVALID_REQUEST, status=409)
    del client
    base = _session_dir(restore_id)
    secret_kind: Literal["passphrase", "age-identity"] = "passphrase" if str(kind) != "age-identity" else "age-identity"
    _set_phase(session, "decrypting-chain")
    crypto_started = time.monotonic()
    members = restore_members(session)
    decrypted_paths: list[Path] = []
    for index, member in enumerate(members):
        ciphertext = Path(str(member["ciphertextPath"]))
        if not ciphertext.is_file():
            raise AppError("Restore session ciphertext is unavailable", code=ErrorCode.NOT_FOUND, status=404)
        decrypted = base / f"decrypted-{index}.dsibackup"
        backup_crypto.decrypt_file(ciphertext, decrypted, kind=secret_kind, secret=secret)
        decrypted_paths.append(decrypted)

    frozen_selection = session.get("selection")
    projection: backup_projection.ProjectionPlan | None = None
    packages: list[backup_projection.ChainPackage] = []
    if frozen_selection is not None:
        packages = _metadata_chain_packages(decrypted_paths, base)
        selection_value = backup_projection.normalize_selection(frozen_selection)
        assert selection_value is not None
        frozen_digest = session.get("selectionDigest")
        computed_digest = backup_projection.selection_digest(selection_value)
        if frozen_digest and computed_digest != frozen_digest:
            raise AppError(
                "Restore selection does not match the frozen session selection",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        projection = backup_projection.plan_projection(
            selection_value,
            packages,
            ciphertext_download_bytes=sum(int(item.get("expectedBytes") or 0) for item in members),
            selection_digest_value=computed_digest,
        )

    backup_recovery_telemetry.record_stage(
        session,
        stage="crypto",
        result="success",
        duration_ms=_elapsed_ms(crypto_started),
        byte_count=sum(int(item.get("expectedBytes") or 0) for item in members),
        components=len(members),
    )
    _set_phase(session, "materializing")
    materialization_started = time.monotonic()
    extracted = base / "extracted"
    shutil.rmtree(extracted, ignore_errors=True)
    if projection is not None:
        extracted_dirs = _selective_extract_members(decrypted_paths, packages, base, projection)
    else:
        extracted_dirs = []
        for index, decrypted in enumerate(decrypted_paths):
            member_extracted = base / f"extracted-{index}"
            backups._safe_extract_and_verify(decrypted, member_extracted)
            extracted_dirs.append(member_extracted)
    final_manifest = backup_incremental_restore.materialize_chain(extracted_dirs, extracted, projection=projection)
    snapshot_kind = str(session.get("snapshotKind") or "full")
    if projection is None:
        final_manifest = _normalized_full_manifest(final_manifest)
        _write_checksums(extracted, final_manifest)
        backups._verify_manifest_tree(extracted)
    materialized_bytes, materialized_files = _manifest_work(final_manifest)
    backup_recovery_telemetry.record_stage(
        session,
        stage="materialization",
        result="success",
        duration_ms=_elapsed_ms(materialization_started),
        byte_count=materialized_bytes,
        components=materialized_files,
    )
    _set_phase(session, "verified")
    result: dict[str, Any] = {
        "restoreId": restore_id,
        "phase": "materialized",
        "snapshotKind": snapshot_kind,
        "tree": str(extracted),
        "manifest": final_manifest,
    }
    if snapshot_kind == "incremental":
        result["chain"] = [str(item["backupId"]) for item in members]
    if projection is not None:
        result["projection"] = projection.report
    return result


def materialize_federated_restore(
    restore_id: str,
    *,
    mode: str = "merge",
    previous_epoch: str = "legacy",
    target_epoch: str | None = None,
    owner_document_id: str = "server",
    client: Any | None = None,
) -> dict[str, Any]:
    """Close the public remote chain into the crash-safe federated restore."""
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    _assert_live_restore_allowed(_session_dir(restore_id), session)
    controlled_phase = _converge_job_control(session)
    if controlled_phase in {"paused", "aborted", "rolled-back", "recovery-required"}:
        return {"restoreId": restore_id, "phase": controlled_phase, "remoteRestorePhase": controlled_phase}
    phase = str(session.get("phase") or "")
    if phase in {"preparing", "prepared", "committing", "complete"}:
        restored = backups.get_restore(restore_id)
        return {**restored, "phase": phase, "remoteRestorePhase": phase, "materializedTreeVerified": True}
    if phase not in {
        "fetched",
        "chain-fetched",
        "controls-fetched",
        "fetching-selected-components",
        "components-fetched",
        "decrypting-controls",
        "planning-projection",
        "decrypting-components",
        "decrypting-chain",
        "materializing",
        "verified",
    }:
        raise AppError("Restore session has not finished fetching", code=ErrorCode.INVALID_REQUEST, status=409)
    if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        preflight_restore_session(restore_id, client=client)
        session = read_restore_session(restore_id) or session
    kind, secret = backup_crypto.consume_secret(restore_id)
    try:
        materialized = materialize_restore_session(restore_id, kind=kind, secret=secret, client=client)
        session = read_restore_session(restore_id) or session
        tree = Path(str(materialized["tree"]))
        members = session.get("chain") if isinstance(session.get("chain"), list) else []
        if str(session.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
            ciphertext_digest = str(
                (members[-1].get("objectSetDigest") if members and isinstance(members[-1], dict) else session.get("objectSetDigest")) or ""
            )
        else:
            ciphertext_digest = str(
                (members[-1].get("objectDigest") if members and isinstance(members[-1], dict) else session.get("objectDigest")) or ""
            )
        protection = "passphrase" if kind == "passphrase" else "age-recipient"
        projection_report = materialized.get("projection")
        if isinstance(projection_report, dict):
            raw_manifest = materialized.get("manifest")
            if not isinstance(raw_manifest, dict):
                raise AppError("Projected restore manifest is unavailable", code=ErrorCode.INVALID_REQUEST, status=409)
            backups.inspect_projected_restore_tree(
                restore_id,
                tree,
                protection=protection,
                ciphertext_sha256=ciphertext_digest or None,
                projection=projection_report,
                manifest=raw_manifest,
            )
        else:
            backups.inspect_verified_restore_tree(
                restore_id,
                tree,
                protection=protection,
                ciphertext_sha256=ciphertext_digest or None,
            )
        backup_crypto.put_secret_bytes(restore_id, kind, bytearray(secret))
        _set_phase(session, "preparing")
        prepared = backups.prepare_restore(
            restore_id,
            mode=mode,
            previous_epoch=previous_epoch,
            target_epoch=target_epoch,
            owner_document_id=owner_document_id,
        )
        session = read_restore_session(restore_id) or session
        session["federatedPhase"] = str(prepared.get("phase") or "backend-staged")
        _set_phase(session, "prepared")
        return {**prepared, "phase": "prepared", "remoteRestorePhase": "prepared", "materializedTreeVerified": True}
    except Exception as exc:
        session = read_restore_session(restore_id) or session
        if isinstance(exc, AppError) and exc.code == ErrorCode.INVALID_PAYLOAD:
            backup_recovery_telemetry.increment_counter(session, "integrityFailure")
        transaction_exists = (_session_dir(restore_id) / "transaction.json").is_file()
        _set_phase(session, "recovery-required" if transaction_exists else "failed")
        if not transaction_exists:
            # Failed before the federated transaction: nothing durable to
            # recover, so the ancestor holds can be released.
            _release_session_holds(session)
        backup_crypto.record_unlock_failure(restore_id)
        raise
    finally:
        secret[:] = b"\x00" * len(secret)


def advance_federated_phase(restore_id: str, phase: str) -> None:
    """Mirror transaction progress into a remote restore session when present.

    Terminal phases (complete / aborted / failed) release every remote ancestor
    hold the session created; ``recovery-required`` intentionally keeps them so
    the chain cannot be garbage-collected before the operator recovers.
    """
    session = read_restore_session(restore_id)
    if session is not None:
        _set_phase(session, phase)
        if phase in {"complete", "aborted", "rolled-back", "failed"}:
            _release_session_holds(session)
            _feed_telemetry_to_ledger(session)
        elif phase in {"paused", "recovery-required"}:
            target_id = str(session.get("activeSourceTargetId") or session.get("targetId") or "")
            if target_id:
                target = backup_publish.resolve_target(target_id, write_intent=False)
                store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False)
                _renew_session_holds(store, session)


def _feed_telemetry_to_ledger(session: dict[str, Any]) -> None:
    """Persist successful production recovery stage samples into the DR ledger."""
    try:
        from deepseek_infra.infra.workspace import backup_dr_ledger, backup_recovery_class

        raw_telemetry = session.get("recoveryTelemetry")
        telemetry: dict[str, Any] = raw_telemetry if isinstance(raw_telemetry, dict) else {}
        samples = list(telemetry.get("samples") or [])
        if not samples:
            return
        phase = str(session.get("phase") or "")
        if phase not in {"complete"} and not session.get("automatedDrill"):
            # Only feed durable success samples from completed recoveries/drills
            successful = [s for s in samples if str(s.get("result") or "") == "success"]
            if not successful or phase not in {"complete", "verified", "components-fetched"}:
                if not session.get("drillResult") == "success":
                    return
        target_id = str(session.get("activeSourceTargetId") or session.get("targetId") or "managed-local")
        kind = "managed-local" if target_id == "managed-local" else "filesystem"
        try:
            from deepseek_infra.infra.workspace import backup_targets as _bt

            t = _bt.get_target(target_id)
            if isinstance(t, dict) and t.get("kind"):
                kind = str(t["kind"])
        except Exception:
            pass
        logical_bytes = int(session.get("expectedBytes") or session.get("logicalBytes") or 0)
        chain_length = len(session.get("chain") or []) or 1
        storage_proto = str(session.get("storageProtocol") or "object-set-v1")
        rclass = backup_recovery_class.classify_recovery(
            target_kind=kind,
            storage_protocol=storage_proto,
            logical_bytes=logical_bytes,
            chain_length=chain_length,
        )
        for sample in samples:
            if str(sample.get("result") or "") != "success":
                continue
            stage = str(sample.get("stage") or "")
            if stage not in {"transfer", "crypto", "materialization", "materialize"}:
                continue
            if stage == "materialize":
                stage = "materialization"
            backup_dr_ledger.record_stage_sample(
                stage=stage,
                bytes_transferred=int(sample.get("bytes") or 0),
                duration_ms=float(sample.get("durationMs") or 0),
                observed_at=str(sample.get("observedAt") or ""),
                result="success",
                recovery_class=rclass,
            )
    except Exception:  # pragma: no cover - telemetry must never break recovery
        pass


def attach_recovery_plan(session: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Freeze planner output onto a durable recovery session."""
    session = dict(session)
    session["recoveryPlan"] = {
        "selectedTargetId": plan.get("selectedTargetId"),
        "orderedCandidates": plan.get("orderedCandidates") or [],
        "logicalRecoveryPoint": plan.get("logicalRecoveryPoint") or {},
        "maxFailovers": int(plan.get("maxFailovers") or 3),
        "selectionReasons": plan.get("selectionReasons") or [],
    }
    session["activeSourceTargetId"] = str(plan.get("selectedTargetId") or session.get("targetId") or "")
    session["attemptedSourceTargets"] = list(session.get("attemptedSourceTargets") or [])
    if session["activeSourceTargetId"] and session["activeSourceTargetId"] not in session["attemptedSourceTargets"]:
        session["attemptedSourceTargets"].append(session["activeSourceTargetId"])
    session["failoverCount"] = int(session.get("failoverCount") or 0)
    session["targetSwitchEvents"] = list(session.get("targetSwitchEvents") or [])
    _atomic_write_json(_session_path(str(session["restoreId"])), session)
    return session


def attempt_target_failover(
    restore_id: str,
    *,
    failure_reason: str,
    client: Any | None = None,
) -> dict[str, Any]:
    """Failover active source target before live prepare; holds ordering is critical.

    1. Select next frozen candidate
    2. Establish holds on the new target
    3. Durable write target switch event
    4. Switch activeSourceTarget
    5. Only then release old target holds
    """
    from deepseek_infra.infra.workspace import backup_recovery_planner

    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    phase = str(session.get("phase") or "")
    if not backup_recovery_planner.failover_allowed(phase):
        raise AppError(
            f"Automatic target failover forbidden in phase {phase}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    if failure_reason not in backup_recovery_planner.RETRYABLE_FAILOVER_REASONS:
        raise AppError(
            f"Failure reason is not retryable via target failover: {failure_reason}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    plan = session.get("recoveryPlan") if isinstance(session.get("recoveryPlan"), dict) else None
    if plan is None:
        raise AppError("Recovery plan is required for automatic failover", code=ErrorCode.INVALID_REQUEST, status=409)

    current_target = str(session.get("activeSourceTargetId") or session.get("targetId") or "")
    attempted = list(session.get("attemptedSourceTargets") or [])
    next_candidate = backup_recovery_planner.select_failover_target(
        plan,
        current_target_id=current_target,
        attempted_targets=attempted,
        failure_reason=failure_reason,
    )
    if next_candidate is None:
        raise AppError("No remaining failover candidates", code=ErrorCode.INVALID_REQUEST, status=409)

    new_target_id = str(next_candidate["targetId"])
    backup_id = str(session.get("backupId") or "")
    old_hold_keys = list(session.get("holdKeys") or [])
    if not old_hold_keys and session.get("holdKey"):
        old_hold_keys = [str(session["holdKey"])]
    # Establish holds on new target BEFORE releasing old holds.
    new_target = backup_publish.resolve_target(new_target_id, write_intent=False)
    new_store = new_target.require_store() if new_target.store is not None else backup_targets.open_target_store(new_target_id, write_intent=False, client=client)
    new_hold_keys: list[str] = []
    new_holds: list[dict[str, Any]] = []
    chain = list(session.get("chain") or [])
    if chain:
        for index, member in enumerate(chain):
            hold = {
                "schemaVersion": 3,
                "storageProtocol": session.get("storageProtocol"),
                "restoreId": restore_id,
                "backupId": str(member.get("backupId") or backup_id),
                "objectSetDigest": member.get("objectSetDigest"),
                "objectDigest": member.get("objectDigest"),
                "objects": member.get("objects") if isinstance(member.get("objects"), list) else None,
                "targetId": new_target_id,
                "createdAt": _utc_iso(),
                "generation": 0,
                "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
            }
            key = restore_hold_key(f"{restore_id}-fo{int(session.get('failoverCount') or 0) + 1}-{index}")
            put_json_if_absent(new_store, key, hold)
            new_hold_keys.append(key)
            new_holds.append({**hold, "holdKey": key})
    else:
        hold = {
            "schemaVersion": 3,
            "restoreId": restore_id,
            "backupId": backup_id,
            "objectDigest": session.get("objectDigest"),
            "targetId": new_target_id,
            "createdAt": _utc_iso(),
            "generation": 0,
            "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
        }
        key = restore_hold_key(f"{restore_id}-fo{int(session.get('failoverCount') or 0) + 1}")
        put_json_if_absent(new_store, key, hold)
        new_hold_keys.append(key)
        new_holds.append({**hold, "holdKey": key})

    switch_event = {
        "fromTargetId": current_target,
        "toTargetId": new_target_id,
        "reason": failure_reason,
        "phase": phase,
        "at": _utc_iso(),
        "failoverIndex": int(session.get("failoverCount") or 0) + 1,
        "oldHoldKeys": old_hold_keys,
        "newHoldKeys": new_hold_keys,
    }
    # Durable switch: write new holds + active source BEFORE releasing old holds.
    session["previousSourceTargetId"] = current_target
    session["activeSourceTargetId"] = new_target_id
    session["targetId"] = new_target_id  # single active source
    session["holdKeys"] = new_hold_keys
    session["holds"] = new_holds
    session["holdKey"] = new_hold_keys[0] if new_hold_keys else None
    session["attemptedSourceTargets"] = list(dict.fromkeys([*attempted, current_target, new_target_id]))
    session["failoverCount"] = int(session.get("failoverCount") or 0) + 1
    events = list(session.get("targetSwitchEvents") or [])
    events.append(switch_event)
    session["targetSwitchEvents"] = events
    session["updatedAt"] = _utc_iso()
    # Reset fetch progress for target-specific state; verified ciphertext cache survives by digest.
    if session.get("downloadedBytes") is not None:
        session["downloadedBytes"] = 0
    session["phase"] = "fetching-controls" if session.get("storageProtocol") == backup_object_set.OBJECT_SET_V1 else "fetching"
    _atomic_write_json(_session_path(restore_id), session)

    # Release old holds only after durable switch (never reverse the order).
    if current_target and old_hold_keys:
        try:
            old_target = backup_publish.resolve_target(current_target, write_intent=False)
            old_store = old_target.require_store() if old_target.store is not None else backup_targets.open_target_store(current_target, write_intent=False, client=client)
            for key in old_hold_keys:
                try:
                    old_store.delete_if_match(str(key))
                except Exception:  # pragma: no cover - best-effort hold release
                    pass
        except Exception:  # pragma: no cover - old target may be unreachable
            pass

    return {
        "restoreId": restore_id,
        "phase": session["phase"],
        "activeSourceTargetId": new_target_id,
        "previousSourceTargetId": current_target,
        "failoverCount": session["failoverCount"],
        "switchEvent": switch_event,
        "holdKeys": new_hold_keys,
        "newHoldBeforeOldRelease": True,
    }
