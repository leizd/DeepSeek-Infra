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
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_crypto,
    backup_incremental,
    backup_incremental_restore,
    backup_pack,
    backup_projection,
    backup_publish,
    backup_targets,
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
SESSION_SCHEMA_VERSION = 3


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


def _set_phase(session: dict[str, Any], phase: str) -> None:
    session["phase"] = phase
    session["updatedAt"] = _utc_iso()
    _atomic_write_json(_session_path(str(session["restoreId"])), session)


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


def restore_members(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered ciphertext members for either restore shape.

    Full sessions historically stored their only member as top-level fields,
    while Incremental sessions stored ``chain``.  Projection planning must not
    inherit that storage distinction, so callers consume this canonical view.
    """
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
                "schemaVersion": 1,
                "restoreId": restore_id,
                "backupId": str(member["backupId"]),
                "objectDigest": str(member["objectDigest"]),
                "targetId": target_id,
                "createdAt": _utc_iso(),
                "expiresAt": _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)),
            }
            try:
                put_json_if_absent(store, restore_hold_key(f"{restore_id}:{index}"), hold)
            except AppError:  # pragma: no cover
                pass
            hold_keys.append(restore_hold_key(f"{restore_id}:{index}"))
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


def _download_member(store: Any, member: dict[str, Any], max_bytes: int | None) -> bool:
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
    with ciphertext_path.open(mode) as handle:
        position = offset
        consumed = 0
        while consumed < budget:
            chunk_len = min(1024 * 1024, budget - consumed)
            piece = store.get_bytes(object_key(digest), offset=position, length=chunk_len)
            if not piece:
                break
            handle.write(piece)
            hasher.update(piece)
            position += len(piece)
            consumed += len(piece)
        handle.flush()
        os.fsync(handle.fileno())
    downloaded = ciphertext_path.stat().st_size if ciphertext_path.is_file() else 0
    member["downloadedBytes"] = downloaded
    if downloaded >= expected:
        if hasher.hexdigest() != digest:
            raise AppError(f"Downloaded backup digest mismatch: {member['backupId']}", code=ErrorCode.INTERNAL, status=500)
        member["fetched"] = True
        return True
    return False


def fetch_restore_session(restore_id: str, *, client: Any | None = None, max_bytes: int | None = None) -> dict[str, Any]:
    """Idempotently continue a durable remote download until complete."""
    session = read_restore_session(restore_id)
    if session is None:
        raise AppError("Remote restore session not found", code=ErrorCode.NOT_FOUND, status=404)
    if str(session.get("phase") or "") in {"fetched", "chain-fetched"}:
        return {
            "restoreId": restore_id,
            "phase": str(session.get("phase") or "fetched"),
            "downloadedBytes": int(session.get("downloadedBytes") or 0),
            "expectedBytes": int(session.get("expectedBytes") or 0),
            "path": str(session.get("ciphertextPath") or ""),
            "next": "inspect-or-unlock",
        }
    target_id = str(session.get("targetId") or "")
    target = backup_publish.resolve_target(target_id, write_intent=False)
    store = target.require_store() if target.store is not None else backup_targets.open_target_store(target_id, write_intent=False, client=client)

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
            done = _download_member(store, member, max_bytes)
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
    done = _download_member(store, member, max_bytes)
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

    Because 4.4.13 keeps the whole-age-object model, the preview downloads the
    full chain before it can report accurate byte counts; ``networkSelective``
    is always ``False``. The client provides a secret first so the metadata
    plane can be decrypted; the preview re-puts it so the later materialize
    step can still consume it.
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
    if phase not in {"fetched", "chain-fetched"}:
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
        base = _session_dir(restore_id)
        members = session.get("chain") or []
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


def restore_from_target(*, target_id: str, backup_id: str, client: Any | None = None) -> dict[str, Any]:  # pragma: no cover - thin wrapper
    """Compatibility helper: create session and fetch to completion in one call."""
    created = create_restore_from_target(target_id=target_id, backup_id=backup_id, client=client)
    restore_id = str(created["restoreId"])
    while True:
        result = fetch_restore_session(restore_id, client=client)
        if str(result.get("phase") or "") in {"fetched", "chain-fetched"}:
            paths = result.get("ciphertextPaths")
            first_path = paths[0] if isinstance(paths, list) and paths else result.get("path")
            return {
                **result,
                "targetId": target_id,
                "backupId": backup_id,
                "filename": Path(str(first_path or "package.age")).name,
                "size": int(result.get("downloadedBytes") or 0),
                "hold": {"restoreId": restore_id},
            }


def release_restore_hold(store: Any, restore_id: str) -> None:
    try:
        store.delete_if_match(restore_hold_key(restore_id))
    except AppError:  # pragma: no cover
        pass


def _release_session_holds(session: dict[str, Any]) -> None:
    """Release every remote hold this restore session created."""
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


def materialize_restore_session(
    restore_id: str,
    *,
    kind: str = "passphrase",
    secret: bytearray,
    client: Any | None = None,
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
    phase = str(session.get("phase") or "")
    if phase not in {"fetched", "chain-fetched", "decrypting-chain", "materializing", "verified"}:
        raise AppError("Restore session has not finished fetching", code=ErrorCode.INVALID_REQUEST, status=409)
    del client
    base = _session_dir(restore_id)
    secret_kind: Literal["passphrase", "age-identity"] = "passphrase" if str(kind) != "age-identity" else "age-identity"
    _set_phase(session, "decrypting-chain")
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

    _set_phase(session, "materializing")
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
    phase = str(session.get("phase") or "")
    if phase in {"preparing", "prepared", "committing", "complete"}:
        restored = backups.get_restore(restore_id)
        return {**restored, "phase": phase, "remoteRestorePhase": phase, "materializedTreeVerified": True}
    if phase not in {"fetched", "chain-fetched", "decrypting-chain", "materializing", "verified"}:
        raise AppError("Restore session has not finished fetching", code=ErrorCode.INVALID_REQUEST, status=409)
    kind, secret = backup_crypto.consume_secret(restore_id)
    try:
        materialized = materialize_restore_session(restore_id, kind=kind, secret=secret, client=client)
        session = read_restore_session(restore_id) or session
        tree = Path(str(materialized["tree"]))
        members = session.get("chain") if isinstance(session.get("chain"), list) else []
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
    except Exception:
        session = read_restore_session(restore_id) or session
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
