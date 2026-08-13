"""Read-only capacity, dependency, and health checks for Recovery Jobs."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_component_cache, backup_recovery_state, backups
from deepseek_infra.infra.workspace.backup_target_store import object_key, receipt_key

DEFAULT_DISK_RESERVE_BYTES = 1024 * 1024 * 1024
SAFETY_ARCHIVE_FIXED_OVERHEAD_BYTES = 1024 * 1024
SAFETY_ARCHIVE_OVERHEAD_PERCENT = 1
CRYPTO_QUEUE_COMPONENTS = 2


def estimate_safety_backup_peak(live_logical_bytes: int) -> dict[str, int]:
    """Conservative peak for encrypted create + decrypt/extract verification."""
    if live_logical_bytes < 0:
        raise ValueError("live_logical_bytes must be non-negative")
    proportional = (live_logical_bytes * SAFETY_ARCHIVE_OVERHEAD_PERCENT + 99) // 100
    archive_bytes = live_logical_bytes + max(SAFETY_ARCHIVE_FIXED_OVERHEAD_BYTES, proportional)
    peak_bytes = (2 * live_logical_bytes) + (2 * archive_bytes)
    return {
        "liveLogicalBytes": live_logical_bytes,
        "archiveBytes": archive_bytes,
        "estimatedPeakBytes": peak_bytes,
    }


def capacity_report(
    *,
    materialized_tree_bytes: int,
    safety_backup_peak_bytes: int,
    uncached_ciphertext_bytes: int,
    plaintext_component_bytes: list[int],
    free_disk_bytes: int | None,
    reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
) -> dict[str, Any]:
    values = [materialized_tree_bytes, safety_backup_peak_bytes, uncached_ciphertext_bytes, reserve_bytes, *plaintext_component_bytes]
    if any(value < 0 for value in values):
        raise ValueError("capacity inputs must be non-negative")
    crypto_plaintext = sum(sorted(plaintext_component_bytes, reverse=True)[:CRYPTO_QUEUE_COMPONENTS])
    scratch_bytes = materialized_tree_bytes + uncached_ciphertext_bytes + crypto_plaintext
    required = scratch_bytes + safety_backup_peak_bytes + reserve_bytes
    sufficient = free_disk_bytes is not None and free_disk_bytes >= required
    return {
        "scratch": {
            "materializedTreeBytes": materialized_tree_bytes,
            "uncachedCiphertextBytes": uncached_ciphertext_bytes,
            "boundedCryptoPlaintextBytes": crypto_plaintext,
            "estimatedPeakBytes": scratch_bytes,
        },
        "disk": {
            "freeBytes": free_disk_bytes,
            "reserveBytes": reserve_bytes,
            "requiredBytes": required,
            "sufficient": sufficient,
        },
    }


def estimate_local_safety_backup() -> dict[str, Any]:
    """Inventory the exact local scope used by the mandatory safety backup."""
    context = backups.BackupContext(mode="full", include_history=True, coverage_policy="best-effort")
    logical_bytes = sum(int(contributor.inventory(context).get("bytes") or 0) for contributor in backups._registered_contributors())
    result: dict[str, Any] = estimate_safety_backup_peak(logical_bytes)
    external_configured = bool(os.environ.get("STATELESS_MCP_BACKUP_URL", "").strip())
    result["externalBytesKnown"] = not external_configured
    return result


def _sha256_file(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        return 0, ""
    return size, digest.hexdigest()


def _local_component_valid(component: dict[str, Any]) -> bool:
    expected = int(component.get("expectedBytes") or 0)
    digest = str(component.get("objectDigest") or "")
    size, observed = _sha256_file(Path(str(component.get("ciphertextPath") or "")))
    return size == expected and observed == digest


def _disk_free_bytes(paths: tuple[Path, ...]) -> int:
    observed: list[int] = []
    devices: set[int] = set()
    for original in paths:
        path = original
        while not path.exists() and path != path.parent:
            path = path.parent
        stat = path.stat()
        identity = int(stat.st_dev)
        if identity in devices:
            continue
        devices.add(identity)
        observed.append(int(shutil.disk_usage(path).free))
    if not observed:
        raise OSError("no recovery disk could be inspected")
    return min(observed)


def _whole_snapshot_health(session: dict[str, Any], catalog: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    raw_chain = session.get("chain")
    chain: list[Any] = raw_chain if isinstance(raw_chain, list) else []
    full = next(
        (item for item in reversed(chain) if isinstance(item, dict) and str(item.get("snapshotKind") or "full") == "full"),
        None,
    )
    if not isinstance(full, dict) or catalog is None:
        return {"status": "unavailable", "source": "catalog"}
    record = catalog.get(str(full.get("backupId") or ""))
    if not isinstance(record, dict):
        return {"status": "unavailable", "source": "catalog"}
    scrub_ok = record.get("scrubOk")
    unlock_at = record.get("userUnlockVerifiedAt")
    status = "ok" if scrub_ok is True and unlock_at else "error" if scrub_ok is False else "warning"
    return {
        "status": status,
        "source": "catalog-scrub",
        "ciphertextScrubbedAt": record.get("ciphertextScrubbedAt"),
        "userUnlockVerifiedAt": unlock_at,
    }


def evaluate_preflight(
    session: dict[str, Any],
    projection_report: dict[str, Any],
    *,
    store: Any | None,
    target_kind: str,
    catalog: dict[str, dict[str, Any]] | None = None,
    cache: backup_component_cache.ComponentCache | None = None,
    safety_backup: dict[str, Any] | None = None,
    free_disk_bytes: int | None = None,
    reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
) -> dict[str, Any]:
    """Evaluate a frozen plan without deleting, pinning, downloading, or writing."""
    components = backup_recovery_state.required_components(session)
    cache_layer = cache or backup_component_cache.ComponentCache()
    blockers: list[dict[str, str]] = []
    cache_hits = 0
    cache_hit_bytes = 0
    local_components = 0
    remote_bytes = 0
    available_remote = 0
    missing_remote = 0
    plaintext_sizes: list[int] = []
    target_reachable = store is not None
    receipt_present = False

    if store is not None:
        try:
            raw_chain = session.get("chain")
            chain: list[Any] = raw_chain if isinstance(raw_chain, list) else []
            backup_ids = [str(item.get("backupId") or "") for item in chain if isinstance(item, dict)]
            if not backup_ids:
                backup_ids = [str(session.get("backupId") or "")]
            receipt_present = all(backup_id and store.stat(receipt_key(backup_id)) is not None for backup_id in backup_ids)
        except (AppError, OSError):
            target_reachable = False
    if not target_reachable or not receipt_present:
        blockers.append({"code": "target-unavailable", "message": "Backup target receipt is unavailable."})

    for component in components:
        digest = str(component.get("objectDigest") or "")
        expected = int(component.get("expectedBytes") or 0)
        plaintext_sizes.append(max(0, int(component.get("plaintextSize") or 0)))
        if cache_layer.inspect(digest, expected):
            cache_hits += 1
            cache_hit_bytes += expected
            local_components += 1
            continue
        if _local_component_valid(component):
            local_components += 1
            continue
        remote_bytes += expected
        meta = None
        if store is not None and target_reachable:
            try:
                meta = store.stat(object_key(digest))
            except (AppError, OSError):
                target_reachable = False
        expected_etag = str(component.get("remoteETag") or "")
        expected_version = component.get("remoteVersionId")
        valid = (
            meta is not None
            and int(meta.size) == expected
            and not (expected_etag and meta.etag and meta.etag != expected_etag)
            and not (expected_version and meta.version_id and meta.version_id != expected_version)
        )
        if valid:
            available_remote += 1
        else:
            missing_remote += 1
    if missing_remote:
        blockers.append({"code": "required-component-unavailable", "message": "One or more required Components are unavailable."})

    safety = safety_backup
    if safety is None:
        try:
            safety = estimate_local_safety_backup()
        except OSError:
            safety = {**estimate_safety_backup_peak(0), "externalBytesKnown": False}
            blockers.append({"code": "safety-backup-estimate-unavailable", "message": "Safety backup size could not be inspected."})
    if not bool(safety.get("externalBytesKnown", True)):
        blockers.append({"code": "external-safety-backup-size-unavailable", "message": "External safety backup size is unavailable."})

    disk_probe_failed = False
    if free_disk_bytes is None:
        try:
            free_disk_bytes = _disk_free_bytes((backups.RESTORE_DIR.parent, backups.BACKUP_DIR.parent))
        except OSError:
            disk_probe_failed = True
    capacity = capacity_report(
        materialized_tree_bytes=max(0, int((projection_report.get("bytes") or {}).get("estimatedMaterializedBytes") or 0)),
        safety_backup_peak_bytes=max(0, int(safety.get("estimatedPeakBytes") or 0)),
        uncached_ciphertext_bytes=remote_bytes,
        plaintext_component_bytes=plaintext_sizes,
        free_disk_bytes=free_disk_bytes,
        reserve_bytes=reserve_bytes,
    )
    if disk_probe_failed:
        blockers.append({"code": "disk-probe-failed", "message": "Recovery disk capacity could not be inspected."})
    elif not bool(capacity["disk"]["sufficient"]):
        blockers.append({"code": "insufficient-disk", "message": "Recovery disk capacity is insufficient."})

    report_counts = projection_report
    raw_bytes_report = report_counts.get("bytes")
    bytes_report: dict[str, Any] = raw_bytes_report if isinstance(raw_bytes_report, dict) else {}
    required_count = len(components)
    available = local_components + available_remote
    target_status = "ok" if target_reachable and receipt_present else "blocked"
    projection_status = "recoverable" if missing_remote == 0 else "blocked"
    return {
        "schemaVersion": 1,
        "restoreId": str(session.get("restoreId") or ""),
        "phase": "preflighted",
        "ready": not blockers,
        "closure": {
            "chainLength": len(session.get("chain") or []),
            "selectedLogicalBytes": max(0, int(bytes_report.get("selectedLogicalBytes") or 0)),
            "requiredComponents": required_count,
            "totalComponents": max(required_count, int(projection_report.get("totalComponents") or required_count)),
            "localComponents": local_components,
        },
        "cache": {
            "hitComponents": cache_hits,
            "missComponents": required_count - cache_hits,
            "hitBytes": cache_hit_bytes,
        },
        "network": {"remoteBytes": remote_bytes},
        **capacity,
        "safetyBackup": safety,
        "targetHealth": {"status": target_status, "kind": target_kind, "receiptPresent": receipt_present},
        "projectionRecoverability": {
            "status": projection_status,
            "requiredComponents": required_count,
            "availableComponents": available,
            "missingComponents": missing_remote,
        },
        "lastWholeSnapshotHealth": _whole_snapshot_health(session, catalog),
        "blockingReasons": blockers,
    }
