"""Durable encrypted publish spool (4.4.7).

Holds Age ciphertext that already passed creation verification so a blocked
remote upload can resume without regenerating the package. Spool entries are
bound to a frozen ``runPlanDigest`` so scheduler retries reuse the same
ciphertext instead of re-encrypting. Never stores plaintext ZIP bytes or
Recovery Identities.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_object_set, backup_unattended

SPOOL_DIR = config.ROOT / ".backup-spool"
SPOOL_SCHEMA_VERSION = 3
OBJECT_SET_SPOOL_SCHEMA_VERSION = 4
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_QUOTA_BYTES = 50 * 1024 * 1024 * 1024

_RECEIPT_SNAPSHOT_STRING_FIELDS = (
    "kind",
    "lineageId",
    "parentBackupId",
    "baseBackupId",
    "chunkProtocol",
)


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _receipt_manifest(package: Any) -> dict[str, Any]:
    """Keep only the public lineage fields needed to rebuild a receipt."""
    raw_manifest = getattr(package, "manifest", None)
    manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
    result: dict[str, Any] = {"snapshotKind": str(manifest.get("snapshotKind") or "full")}
    chunk_protocol = str(manifest.get("chunkProtocol") or "")
    if chunk_protocol:
        result["chunkProtocol"] = chunk_protocol
    raw_snapshot = manifest.get("snapshot")
    if isinstance(raw_snapshot, dict):
        snapshot: dict[str, Any] = {
            field: str(raw_snapshot.get(field) or "") or None
            for field in _RECEIPT_SNAPSHOT_STRING_FIELDS
        }
        snapshot["chainDepth"] = int(raw_snapshot.get("chainDepth") or 0)
        result["snapshot"] = snapshot
    return result


def _slot_dir(policy_id: str, slot_digest: str) -> Path:
    return SPOOL_DIR / policy_id / slot_digest


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def spool_usage_bytes() -> int:
    if not SPOOL_DIR.is_dir():
        return 0
    total = 0
    for path in SPOOL_DIR.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def lookup_verified_package(
    *,
    policy_id: str,
    slot_digest: str,
    run_plan_digest: str | None = None,
) -> SpooledPackage | backup_object_set.ObjectSetPackage | None:
    """Return an existing verified spool package when present and plan-compatible."""
    object_set_meta = read_object_set_meta(policy_id, slot_digest)
    if object_set_meta is not None:
        if run_plan_digest is not None and str(object_set_meta.get("runPlanDigest") or "") not in {"", run_plan_digest}:
            raise AppError("spool run plan digest mismatch for schedule slot", code=ErrorCode.INVALID_REQUEST, status=409)
        return _load_spooled_object_set(policy_id, slot_digest, object_set_meta)
    meta = read_package_meta(policy_id, slot_digest)
    path = package_path(policy_id, slot_digest)
    if meta is None or path is None:
        return None
    if int(meta.get("schemaVersion") or 0) != SPOOL_SCHEMA_VERSION or not isinstance(meta.get("receiptManifest"), dict):
        clear_slot(policy_id, slot_digest)
        return None
    if run_plan_digest is not None and str(meta.get("runPlanDigest") or "") not in {"", run_plan_digest}:
        raise AppError("spool run plan digest mismatch for schedule slot", code=ErrorCode.INVALID_REQUEST, status=409)
    if not bool(meta.get("creationVerified")):
        return None
    if backup_unattended.sha256_file(path) != str(meta.get("ciphertextSha256") or ""):
        return None
    return SpooledPackage(meta, path)


def store_verified_package(
    package: Any,
    *,
    policy_id: str,
    schedule_slot: str,
    run_id: str,
    slot_digest: str | None = None,
    run_plan_digest: str | None = None,
) -> dict[str, Any]:
    """Copy a verified Age package into the durable spool for the schedule slot."""
    from deepseek_infra.infra.workspace.backup_target_store import commit_slot_digest

    digest = slot_digest or commit_slot_digest(schedule_slot)
    if isinstance(package, backup_object_set.ObjectSetPackage):
        return _store_verified_object_set(
            package,
            policy_id=policy_id,
            schedule_slot=schedule_slot,
            run_id=run_id,
            slot_digest=digest,
            run_plan_digest=run_plan_digest,
        )
    dest_dir = _slot_dir(policy_id, digest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    package_path = dest_dir / "package.age"
    meta_path = dest_dir / "package.json"
    ciphertext_sha256 = str(package.ciphertext_sha256)
    if package_path.is_file():
        existing_meta = read_package_meta(policy_id, digest)
        if existing_meta and str(existing_meta.get("ciphertextSha256") or "") == ciphertext_sha256:
            if run_plan_digest and str(existing_meta.get("runPlanDigest") or "") not in {"", run_plan_digest}:
                raise AppError("spool run plan digest mismatch for schedule slot", code=ErrorCode.INVALID_REQUEST, status=409)
            if int(existing_meta.get("schemaVersion") or 0) == SPOOL_SCHEMA_VERSION and isinstance(existing_meta.get("receiptManifest"), dict):
                return existing_meta
            return _write_meta(
                meta_path,
                package,
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                run_id=run_id,
                slot_digest=digest,
                run_plan_digest=run_plan_digest,
            )
        if backup_unattended.sha256_file(package_path) == ciphertext_sha256:  # pragma: no cover
            meta = existing_meta or {}
            return meta if meta else _write_meta(
                meta_path,
                package,
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                run_id=run_id,
                slot_digest=digest,
                run_plan_digest=run_plan_digest,
            )
        raise AppError("spool slot already holds a different ciphertext digest", code=ErrorCode.INVALID_REQUEST, status=409)
    if spool_usage_bytes() + int(package.size) > DEFAULT_QUOTA_BYTES:
        cleanup_expired(force_oldest=True)
        if spool_usage_bytes() + int(package.size) > DEFAULT_QUOTA_BYTES:  # pragma: no cover
            raise AppError("backup spool quota exceeded", code=ErrorCode.INVALID_REQUEST, status=507)
    tmp = dest_dir / f".package.{os.getpid()}.part"
    with Path(package.path).open("rb") as source, tmp.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if backup_unattended.sha256_file(tmp) != ciphertext_sha256:  # pragma: no cover
        tmp.unlink(missing_ok=True)
        raise AppError("spool package digest mismatch", code=ErrorCode.INTERNAL, status=500)
    os.replace(tmp, package_path)
    return _write_meta(
        meta_path,
        package,
        policy_id=policy_id,
        schedule_slot=schedule_slot,
        run_id=run_id,
        slot_digest=digest,
        run_plan_digest=run_plan_digest,
    )


def _store_verified_object_set(
    package: backup_object_set.ObjectSetPackage,
    *,
    policy_id: str,
    schedule_slot: str,
    run_id: str,
    slot_digest: str,
    run_plan_digest: str | None,
) -> dict[str, Any]:
    dest_dir = _slot_dir(policy_id, slot_digest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta_path = dest_dir / "object-set.json"
    existing = read_object_set_meta(policy_id, slot_digest)
    if existing is not None:
        if (
            str(existing.get("objectSetDigest") or "") != package.object_set_digest
            or str(existing.get("controlObjectDigest") or "") != package.control.ciphertext_digest
        ):
            raise AppError("spool slot already holds a different object set", code=ErrorCode.INVALID_REQUEST, status=409)
        if run_plan_digest and str(existing.get("runPlanDigest") or "") not in {"", run_plan_digest}:
            raise AppError("spool run plan digest mismatch for schedule slot", code=ErrorCode.INVALID_REQUEST, status=409)
        if _load_spooled_object_set(policy_id, slot_digest, existing) is not None:
            return existing
    if (dest_dir / "package.age").exists() or (dest_dir / "package.json").exists():
        raise AppError("spool slot already holds a whole-age package", code=ErrorCode.INVALID_REQUEST, status=409)
    if spool_usage_bytes() + package.size > DEFAULT_QUOTA_BYTES:
        cleanup_expired(force_oldest=True)
        if spool_usage_bytes() + package.size > DEFAULT_QUOTA_BYTES:  # pragma: no cover
            raise AppError("backup spool quota exceeded", code=ErrorCode.INVALID_REQUEST, status=507)
    objects: list[dict[str, Any]] = []
    payload_ordinal = 0
    for component in package.components:
        relative = "control.age" if component.control else f"payload/{payload_ordinal:04d}.age"
        if not component.control:
            payload_ordinal += 1
        destination = dest_dir.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        with component.path.open("rb") as source, tmp.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if tmp.stat().st_size != component.ciphertext_size or backup_unattended.sha256_file(tmp) != component.ciphertext_digest:
            tmp.unlink(missing_ok=True)
            raise AppError("spool object-set component mismatch", code=ErrorCode.INTERNAL, status=500)
        os.replace(tmp, destination)
        objects.append({"path": relative, "digest": component.ciphertext_digest, "size": component.ciphertext_size})
    meta = {
        "schemaVersion": OBJECT_SET_SPOOL_SCHEMA_VERSION,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "runId": run_id,
        "runPlanDigest": run_plan_digest or "",
        "backupId": package.backup_id,
        "objectSetDigest": package.object_set_digest,
        "controlObjectDigest": package.control.ciphertext_digest,
        "creationVerified": package.creation_verified,
        "receiptManifest": _receipt_manifest(package),
        "storedAt": _utc_iso(),
        "objects": objects,
    }
    _atomic_write_json(meta_path, meta)
    return meta


def read_object_set_meta(policy_id: str, slot_digest: str) -> dict[str, Any] | None:
    path = _slot_dir(policy_id, slot_digest) / "object-set.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_spooled_object_set(
    policy_id: str,
    slot_digest: str,
    meta: dict[str, Any],
) -> backup_object_set.ObjectSetPackage | None:
    if int(meta.get("schemaVersion") or 0) != OBJECT_SET_SPOOL_SCHEMA_VERSION:
        return None
    raw_objects = meta.get("objects")
    if not isinstance(raw_objects, list):
        return None
    components: list[backup_object_set.EncryptedComponent] = []
    payload_ordinal = 0
    control_count = 0
    seen_paths: set[str] = set()
    for raw in raw_objects:
        if not isinstance(raw, dict):
            return None
        relative = str(raw.get("path") or "")
        relative_path = PurePosixPath(relative)
        expected_payload = f"payload/{payload_ordinal:04d}.age"
        is_control = relative == "control.age"
        control_count += int(is_control)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in seen_paths
            or (not is_control and relative != expected_payload)
        ):
            return None
        seen_paths.add(relative)
        path = _slot_dir(policy_id, slot_digest).joinpath(*relative_path.parts)
        digest = str(raw.get("digest") or "")
        raw_size = raw.get("size")
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or isinstance(raw_size, bool)
            or not isinstance(raw_size, int)
            or raw_size < 0
        ):
            return None
        size = raw_size
        if not path.is_file() or path.stat().st_size != size or backup_unattended.sha256_file(path) != digest:
            return None
        components.append(
            backup_object_set.EncryptedComponent(
                component_id="control" if is_control else f"p{payload_ordinal:04d}",
                path=path,
                ciphertext_digest=digest,
                ciphertext_size=size,
                control=is_control,
            )
        )
        if not is_control:
            payload_ordinal += 1
    if control_count != 1:
        return None
    raw_manifest = meta.get("receiptManifest")
    package = backup_object_set.ObjectSetPackage(
        backup_id=str(meta.get("backupId") or ""),
        components=tuple(components),
        manifest_digest="",
        coverage_digest="",
        manifest=dict(raw_manifest) if isinstance(raw_manifest, dict) else {},
        creation_verified=bool(meta.get("creationVerified")),
    )
    if (
        package.object_set_digest != str(meta.get("objectSetDigest") or "")
        or package.control.ciphertext_digest != str(meta.get("controlObjectDigest") or "")
    ):
        return None
    return package


def _write_meta(
    path: Path,
    package: Any,
    *,
    policy_id: str,
    schedule_slot: str,
    run_id: str,
    slot_digest: str,
    run_plan_digest: str | None = None,
) -> dict[str, Any]:
    meta = {
        "schemaVersion": SPOOL_SCHEMA_VERSION,
        "policyId": policy_id,
        "scheduleSlot": schedule_slot,
        "slotDigest": slot_digest,
        "runId": run_id,
        "runPlanDigest": run_plan_digest or "",
        "backupId": package.backup_id,
        "filename": package.filename,
        "size": int(package.size),
        "ciphertextSha256": str(package.ciphertext_sha256),
        "manifestDigest": str(package.manifest_digest),
        "coverageDigest": str(package.coverage_digest),
        "creationVerified": bool(package.creation_verified),
        "receiptManifest": _receipt_manifest(package),
        "storedAt": _utc_iso(),
        "packagePath": "package.age",
    }
    _atomic_write_json(path, meta)
    return meta


def read_package_meta(policy_id: str, slot_digest: str) -> dict[str, Any] | None:
    path = _slot_dir(policy_id, slot_digest) / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def package_path(policy_id: str, slot_digest: str) -> Path | None:
    path = _slot_dir(policy_id, slot_digest) / "package.age"
    return path if path.is_file() else None


def read_multipart_state(policy_id: str, slot_digest: str) -> dict[str, Any] | None:
    path = _slot_dir(policy_id, slot_digest) / "multipart.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_multipart_state(policy_id: str, slot_digest: str, state: dict[str, Any]) -> None:
    _atomic_write_json(_slot_dir(policy_id, slot_digest) / "multipart.json", state)


def read_component_multipart_state(policy_id: str, slot_digest: str, ciphertext_digest: str) -> dict[str, Any] | None:
    path = _slot_dir(policy_id, slot_digest) / "multipart" / f"{ciphertext_digest}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_component_multipart_state(
    policy_id: str,
    slot_digest: str,
    ciphertext_digest: str,
    state: dict[str, Any],
) -> None:
    _atomic_write_json(_slot_dir(policy_id, slot_digest) / "multipart" / f"{ciphertext_digest}.json", state)


def clear_slot(policy_id: str, slot_digest: str) -> None:
    dest = _slot_dir(policy_id, slot_digest)
    if dest.is_dir():
        shutil.rmtree(dest, ignore_errors=True)


def cleanup_expired(*, ttl_seconds: int = DEFAULT_TTL_SECONDS, force_oldest: bool = False) -> dict[str, int]:
    removed = 0
    freed = 0
    if not SPOOL_DIR.is_dir():
        return {"removed": 0, "freedBytes": 0}
    now = time.time()
    entries: list[tuple[float, Path, int]] = []
    for policy_dir in SPOOL_DIR.iterdir():
        if not policy_dir.is_dir():
            continue
        for slot_dir in policy_dir.iterdir():
            if not slot_dir.is_dir():
                continue
            meta_path = slot_dir / "package.json"
            object_set_meta_path = slot_dir / "object-set.json"
            if meta_path.is_file():
                mtime = meta_path.stat().st_mtime
            elif object_set_meta_path.is_file():
                mtime = object_set_meta_path.stat().st_mtime
            else:
                mtime = slot_dir.stat().st_mtime
            size = sum(p.stat().st_size for p in slot_dir.rglob("*") if p.is_file())
            entries.append((mtime, slot_dir, size))
            if now - mtime > ttl_seconds:
                shutil.rmtree(slot_dir, ignore_errors=True)
                removed += 1
                freed += size
    if force_oldest and spool_usage_bytes() > DEFAULT_QUOTA_BYTES:
        remaining = sorted((e for e in entries if e[1].exists()), key=lambda item: item[0])
        for mtime, slot_dir, size in remaining:
            if spool_usage_bytes() <= DEFAULT_QUOTA_BYTES * 0.8:
                break
            if slot_dir.exists():
                shutil.rmtree(slot_dir, ignore_errors=True)
                removed += 1
                freed += size
    return {"removed": removed, "freedBytes": freed}


class SpooledPackage:
    """Lightweight package view backed by spool ciphertext."""

    def __init__(self, meta: dict[str, Any], path: Path) -> None:
        self.backup_id = str(meta["backupId"])
        self.filename = str(meta["filename"])
        self.size = int(meta["size"])
        self.ciphertext_sha256 = str(meta["ciphertextSha256"])
        self.manifest_digest = str(meta.get("manifestDigest") or "")
        self.coverage_digest = str(meta.get("coverageDigest") or "")
        self.creation_verified = bool(meta.get("creationVerified"))
        raw_manifest = meta.get("receiptManifest")
        self.manifest = dict(raw_manifest) if isinstance(raw_manifest, dict) else {}
        self.path = path
