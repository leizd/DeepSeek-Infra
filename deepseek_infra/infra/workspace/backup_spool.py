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
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_unattended

SPOOL_DIR = config.ROOT / ".backup-spool"
SPOOL_SCHEMA_VERSION = 2
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_QUOTA_BYTES = 50 * 1024 * 1024 * 1024


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
) -> SpooledPackage | None:
    """Return an existing verified spool package when present and plan-compatible."""
    meta = read_package_meta(policy_id, slot_digest)
    path = package_path(policy_id, slot_digest)
    if meta is None or path is None:
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
            return existing_meta
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
            mtime = meta_path.stat().st_mtime if meta_path.is_file() else slot_dir.stat().st_mtime
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
        self.path = path
