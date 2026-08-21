"""Filesystem backup target registry with lineage checkpoints (4.4.5).

Targets are recognized by a marker file (``.deepseek-infra-backup-target.json``)
rather than by drive letter or mount path, so a USB disk that moves from
``E:`` to ``F:`` is still the same target. Location safety rules keep backups
out of the runtime root, the source repository, contributor roots, restore
staging, temp dirs and other targets; the marker is re-verified before every
publish and retention pass.

Marker v2 carries an ``incarnationId``, ``ownerInstallationId`` and the
target's commit head (``targetGeneration`` / ``latestCommitHash``). A trusted
checkpoint in ``.backup-targets/<targetId>.checkpoint.json`` records what this
installation last saw, so reconnecting a rolled-back disk raises
``target-rollback-detected``, a same-generation head or incarnation change
raises ``target-fork-detected``, and one target id alive at two locations
raises ``target-clone-detected``. Anomalies must be resolved explicitly —
adopt the branch or register it as new — before writes resume; read-only
listing and scrubbing stay allowed.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control, backups

TARGET_SCHEMA_VERSION = 3
TARGET_MARKER_NAME = ".deepseek-infra-backup-target.json"
TARGET_GENESIS_HASH = "0" * 64
BACKUP_TARGET_DIR = config.ROOT / ".backup-targets"
_SECRET_FIELD_NAMES = (
    "accessKey",
    "accessKeyId",
    "secretAccessKey",
    "secret",
    "sessionToken",
    "password",
    "secret_access_key",
    "access_key_id",
    "session_token",
)

_TARGET_ID = re.compile(r"^target_[a-z0-9][a-z0-9._-]{0,63}$")
_WINDOWS_REPARSE_POINT = 0x400


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _operator_cost(value: float | int | None, field: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise AppError(f"Backup target {field} must be a finite non-negative number", code=ErrorCode.INVALID_PAYLOAD)
    return parsed


def _positive_qos(value: int | None, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AppError(f"Backup target {field} must be a positive integer", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _optional_positive_int(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AppError(f"Backup target {field} must be a positive integer", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _registry_path(target_id: str) -> Path:
    return BACKUP_TARGET_DIR / f"{target_id}.json"


def _checkpoint_path(target_id: str) -> Path:
    return BACKUP_TARGET_DIR / f"{target_id}.checkpoint.json"


def _read_checkpoint(target_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_checkpoint_path(target_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_checkpoint(target_id: str, marker_data: dict[str, Any]) -> None:
    _atomic_write_json(
        _checkpoint_path(target_id),
        {
            "incarnationId": str(marker_data.get("incarnationId") or ""),
            "lastSeenGeneration": int(marker_data.get("targetGeneration") or 0),
            "lastSeenCommitHash": str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH),
            "lastSeenAt": _utc_iso(),
        },
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _project_target(record: dict[str, Any]) -> None:
    _assert_no_secrets(record)
    _atomic_write_json(_registry_path(str(record["targetId"])), record)


def _store_target(record: dict[str, Any], *, expected_generation: int | None = None) -> dict[str, Any]:
    authoritative = backup_control.upsert_target(record, expected_generation=expected_generation)
    _project_target(authoritative)
    return authoritative


def _mutate_target(
    target_id: str,
    mutate: Any,
    *,
    expected_generation: int | None = None,
    bump_generation: bool = True,
) -> dict[str, Any]:
    # Adopt pre-4.5.8 JSON before entering the cross-process mutation.
    current = get_target(target_id)
    if backup_control.get_target(target_id) is None:
        backup_control.adopt_target_projection(current)
    authoritative = backup_control.mutate_target(
        target_id,
        expected_generation=expected_generation,
        mutate=mutate,
        bump_generation=bump_generation,
    )
    _project_target(authoritative)
    return authoritative


def installation_id() -> str:
    path = BACKUP_TARGET_DIR / "installation.id"
    try:
        existing = path.read_text(encoding="ascii").strip()
        if existing:
            return existing
    except OSError:
        pass
    value = f"inst_{secrets.token_hex(8)}"
    BACKUP_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")
    return value


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & _WINDOWS_REPARSE_POINT)
    return False


def _has_reparse_component(path: Path) -> bool:  # pragma: no cover - OS reparse traversal
    current = path
    while True:
        if _is_reparse_point(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _containment_violation(resolved: Path, *, exclude_target_id: str | None = None) -> str | None:
    runtime_root = config.ROOT.resolve()
    forbidden: list[tuple[str, Path]] = [
        ("runtime root", runtime_root),
        ("source repository", _repo_root()),
        ("restore staging", backups.RESTORE_DIR.resolve()),
        ("managed backup directory", backups.BACKUP_DIR.resolve()),
        ("system temporary directory", Path(tempfile.gettempdir()).resolve()),
    ]
    for name, base in forbidden:
        if resolved == base or resolved.is_relative_to(base):
            return f"target is inside the {name}"
    for contributor in backups._registered_contributors():
        path_getter = getattr(contributor, "path_getter", None)
        if path_getter is None:
            continue
        root = Path(path_getter()).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return f"target is inside contributor root {contributor.contributor_id}"
    for other in list_targets():
        if exclude_target_id is not None and other.get("targetId") == exclude_target_id:
            continue
        if str(other.get("kind") or "filesystem") != "filesystem":
            continue
        other_raw = str(other.get("path") or "")
        if not other_raw:
            continue
        other_path = Path(other_raw).resolve()
        if resolved == other_path or resolved.is_relative_to(other_path) or other_path.is_relative_to(resolved):
            return f"target overlaps registered target {other.get('targetId')}"
    return None


def _resolve_basic(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise AppError("Backup target path must be absolute", code=ErrorCode.INVALID_PAYLOAD)
    if not candidate.is_dir():
        raise AppError("Backup target directory does not exist", code=ErrorCode.INVALID_PAYLOAD)
    if _has_reparse_component(candidate):
        raise AppError("Backup target must not sit behind a symlink or reparse point", code=ErrorCode.INVALID_PAYLOAD)
    return candidate.resolve()


def validate_target_location(path: Path, *, exclude_target_id: str | None = None) -> Path:
    """Resolve and validate a target directory; raises AppError on violations."""
    resolved = _resolve_basic(Path(path))
    violation = _containment_violation(resolved, exclude_target_id=exclude_target_id)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    return resolved


def init_target(
    path: Path | str,
    *,
    label: str = "",
    region: str | None = None,
    failure_domain: str | None = None,
    provider: str | None = None,
    jurisdiction: str | None = None,
    priority: int = 0,
    cost_class: str | None = None,
    storage_cost_per_gib_month: float | int | None = None,
    egress_cost_per_gib: float | int | None = None,
    max_read_bytes_per_second: int | None = None,
    max_write_bytes_per_second: int | None = None,
    max_concurrent_transfers: int | None = None,
) -> dict[str, Any]:
    """Initialize a directory as a backup target and register it."""
    resolved = _resolve_basic(Path(path))
    marker = resolved / TARGET_MARKER_NAME
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError("Backup target marker is unreadable", code=ErrorCode.INVALID_PAYLOAD) from exc
        if isinstance(existing, dict) and existing.get("targetId"):
            target_id = str(existing["targetId"])
            violation = _containment_violation(resolved, exclude_target_id=target_id)
            if violation:
                raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
            try:
                registration = get_target(target_id)
            except AppError:
                registration = None
            if registration is not None:
                registered_path = Path(str(registration.get("path") or ""))
                if registered_path != resolved and registered_path.exists():
                    raise AppError(
                        "target-clone-detected: target id is registered at another location that still exists; adopt one branch or register it as new",
                        code=ErrorCode.INVALID_REQUEST,
                        status=409,
                    )
            return _register(
                resolved,
                target_id,
                str(existing.get("targetNonce") or ""),
                label=label,
                created_at=str(existing.get("createdAt") or ""),
                region=region,
                failure_domain=failure_domain,
                provider=provider,
                jurisdiction=jurisdiction,
                priority=priority,
                cost_class=cost_class,
                storage_cost_per_gib_month=storage_cost_per_gib_month,
                egress_cost_per_gib=egress_cost_per_gib,
                max_read_bytes_per_second=max_read_bytes_per_second,
                max_write_bytes_per_second=max_write_bytes_per_second,
                max_concurrent_transfers=max_concurrent_transfers,
            )
        raise AppError("Backup target marker is invalid", code=ErrorCode.INVALID_PAYLOAD)
    violation = _containment_violation(resolved)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    target_id = f"target_{secrets.token_hex(6)}"
    nonce = secrets.token_hex(16)
    marker_payload = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "targetNonce": nonce,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "createdAt": _utc_iso(),
    }
    _atomic_write_json(marker, marker_payload)
    record = _register(
        resolved,
        target_id,
        nonce,
        label=label,
        created_at=marker_payload["createdAt"],
        region=region,
        failure_domain=failure_domain,
        provider=provider,
        jurisdiction=jurisdiction,
        priority=priority,
        cost_class=cost_class,
        storage_cost_per_gib_month=storage_cost_per_gib_month,
        egress_cost_per_gib=egress_cost_per_gib,
        max_read_bytes_per_second=max_read_bytes_per_second,
        max_write_bytes_per_second=max_write_bytes_per_second,
        max_concurrent_transfers=max_concurrent_transfers,
    )
    _write_checkpoint(target_id, marker_payload)
    return record


def register_filesystem_target(
    target_id: str,
    *,
    path: Path | str,
    label: str = "",
    region: str | None = None,
    failure_domain: str | None = None,
    provider: str | None = None,
    jurisdiction: str | None = None,
    priority: int = 0,
    cost_class: str | None = None,
    storage_cost_per_gib_month: float | int | None = None,
    egress_cost_per_gib: float | int | None = None,
    max_read_bytes_per_second: int | None = None,
    max_write_bytes_per_second: int | None = None,
    max_concurrent_transfers: int | None = None,
) -> dict[str, Any]:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    resolved = _resolve_basic(p)
    marker = resolved / TARGET_MARKER_NAME
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("targetId") == target_id:
                return _register(
                    resolved,
                    target_id,
                    str(existing.get("targetNonce") or ""),
                    label=label,
                    created_at=str(existing.get("createdAt") or ""),
                    region=region,
                    failure_domain=failure_domain,
                    provider=provider,
                    jurisdiction=jurisdiction,
                    priority=priority,
                    cost_class=cost_class,
                    storage_cost_per_gib_month=storage_cost_per_gib_month,
                    egress_cost_per_gib=egress_cost_per_gib,
                    max_read_bytes_per_second=max_read_bytes_per_second,
                    max_write_bytes_per_second=max_write_bytes_per_second,
                    max_concurrent_transfers=max_concurrent_transfers,
                )
        except Exception:
            pass
    nonce = secrets.token_hex(16)
    marker_payload = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "targetNonce": nonce,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "createdAt": _utc_iso(),
    }
    _atomic_write_json(marker, marker_payload)
    record = _register(
        resolved,
        target_id,
        nonce,
        label=label,
        created_at=marker_payload["createdAt"],
        region=region,
        failure_domain=failure_domain,
        provider=provider,
        jurisdiction=jurisdiction,
        priority=priority,
        cost_class=cost_class,
        storage_cost_per_gib_month=storage_cost_per_gib_month,
        egress_cost_per_gib=egress_cost_per_gib,
        max_read_bytes_per_second=max_read_bytes_per_second,
        max_write_bytes_per_second=max_write_bytes_per_second,
        max_concurrent_transfers=max_concurrent_transfers,
    )
    _write_checkpoint(target_id, marker_payload)
    return record


def _register(
    resolved: Path,
    target_id: str,
    nonce: str,
    *,
    label: str,
    created_at: object = "",
    region: str | None = None,
    failure_domain: str | None = None,
    provider: str | None = None,
    jurisdiction: str | None = None,
    priority: int = 0,
    cost_class: str | None = None,
    storage_cost_per_gib_month: float | int | None = None,
    egress_cost_per_gib: float | int | None = None,
    max_read_bytes_per_second: int | None = None,
    max_write_bytes_per_second: int | None = None,
    max_concurrent_transfers: int | None = None,
) -> dict[str, Any]:
    if not _TARGET_ID.match(target_id):
        raise AppError("Backup target marker carries an invalid target id", code=ErrorCode.INVALID_PAYLOAD)
    record = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "kind": "filesystem",
        "path": str(resolved),
        "targetNonce": nonce,
        "label": label,
        "region": str(region or "local").strip() or "local",
        "failureDomain": failure_domain or "local",
        "provider": str(provider or "operator-filesystem").strip() or "operator-filesystem",
        "jurisdiction": str(jurisdiction or "local").strip() or "local",
        "priority": int(priority),
        "costClass": cost_class or "standard",
        "storageCostPerGiBMonth": _operator_cost(storage_cost_per_gib_month, "storageCostPerGiBMonth"),
        "egressCostPerGiB": _operator_cost(egress_cost_per_gib, "egressCostPerGiB"),
        "maxReadBytesPerSecond": _positive_qos(max_read_bytes_per_second, "maxReadBytesPerSecond", 100 * 1024 * 1024),
        "maxWriteBytesPerSecond": _positive_qos(max_write_bytes_per_second, "maxWriteBytesPerSecond", 100 * 1024 * 1024),
        "maxConcurrentTransfers": _positive_qos(max_concurrent_transfers, "maxConcurrentTransfers", 4),
        "drainState": "active",
        "createdAt": str(created_at or "") or _utc_iso(),
        "registeredAt": _utc_iso(),
    }
    return _store_target(record)


def _assert_no_secrets(payload: dict[str, Any]) -> None:
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _SECRET_FIELD_NAMES and value not in (None, ""):
                    raise AppError("cloud credentials must not be stored in target registry", code=ErrorCode.INVALID_PAYLOAD)
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def init_s3_target(
    *,
    bucket: str,
    prefix: str = "",
    region: str | None = None,
    endpoint_url: str | None = None,
    expected_bucket_owner: str | None = None,
    label: str = "",
    failure_domain: str | None = None,
    provider: str | None = None,
    jurisdiction: str | None = None,
    priority: int = 0,
    cost_class: str | None = None,
    storage_cost_per_gib_month: float | int | None = None,
    egress_cost_per_gib: float | int | None = None,
    max_read_bytes_per_second: int | None = None,
    max_write_bytes_per_second: int | None = None,
    max_concurrent_transfers: int | None = None,
    quota_bytes: int | None = None,
    credential_provider: dict[str, Any] | None = None,
    client: Any | None = None,
    probe: bool = True,
) -> dict[str, Any]:
    """Register a secret-free S3-compatible target (schema v3)."""
    from deepseek_infra.infra.workspace import backup_target_s3
    from deepseek_infra.infra.workspace.backup_target_store import (
        head_key,
        identity_key,
        put_json_if_absent,
        read_json,
    )

    if not backup_target_s3.s3_sdk_available() and client is None:
        raise AppError("s3TargetAvailable=false: install boto3 to use S3 targets", code=ErrorCode.INVALID_REQUEST, status=503)
    credential_config = dict(credential_provider or {"type": "aws-default-chain"})
    if credential_config.get("type") == "aws-default-chain" and credential_config.get("profile"):
        credential_config = {"type": "aws-profile", "profile": str(credential_config.get("profile"))}
    record_preview = {
        "bucket": str(bucket or "").strip(),
        "prefix": str(prefix or "").strip().strip("/"),
        "region": str(region or "").strip() or None,
        "endpointUrl": str(endpoint_url or "").strip() or None,
        "expectedBucketOwner": str(expected_bucket_owner or "").strip() or None,
        "credentialProvider": credential_config,
    }
    _assert_no_secrets(record_preview)
    if not record_preview["bucket"]:
        raise AppError("S3 bucket is required", code=ErrorCode.INVALID_PAYLOAD)
    store = backup_target_s3.open_s3_store(record_preview, client=client)
    target_id = f"target_{secrets.token_hex(6)}"
    identity = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "kind": "s3",
        "bucket": record_preview["bucket"],
        "prefix": record_preview["prefix"],
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "createdAt": _utc_iso(),
    }
    existing_identity = read_json(store, identity_key())
    if existing_identity is not None and existing_identity.get("targetId"):
        target_id = str(existing_identity["targetId"])
        identity = existing_identity
    else:
        put_json_if_absent(store, identity_key(), identity)
    head = read_json(store, head_key())
    if head is None:
        put_json_if_absent(
            store,
            head_key(),
            {
                "schemaVersion": 1,
                "targetGeneration": 0,
                "latestCommitHash": TARGET_GENESIS_HASH,
                "incarnationId": str(identity.get("incarnationId") or ""),
            },
        )
    probe_result: dict[str, Any] | None = None
    if probe:
        from deepseek_infra.infra.workspace.backup_target_store import probe_store_capabilities

        probe_result = probe_store_capabilities(store)
        if store.capabilities().kind == "s3":
            detect = getattr(store, "detect_versioning", None)
            try:
                versioning = detect() if callable(detect) else None
            except Exception:
                versioning = None
            if probe_result is not None:
                probe_result.setdefault("capabilities", {})["versioning"] = versioning
    record = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "kind": "s3",
        "label": label,
        "bucket": record_preview["bucket"],
        "prefix": record_preview["prefix"],
        "region": record_preview["region"],
        "endpointUrl": record_preview["endpointUrl"],
        "expectedBucketOwner": record_preview["expectedBucketOwner"],
        "failureDomain": failure_domain or record_preview["region"] or "remote-s3",
        "provider": str(provider or "s3-compatible").strip() or "s3-compatible",
        "jurisdiction": str(jurisdiction or record_preview["region"] or "unknown").strip() or "unknown",
        "priority": int(priority),
        "costClass": cost_class or "standard",
        "storageCostPerGiBMonth": _operator_cost(storage_cost_per_gib_month, "storageCostPerGiBMonth"),
        "egressCostPerGiB": _operator_cost(egress_cost_per_gib, "egressCostPerGiB"),
        "maxReadBytesPerSecond": _positive_qos(max_read_bytes_per_second, "maxReadBytesPerSecond", 100 * 1024 * 1024),
        "maxWriteBytesPerSecond": _positive_qos(max_write_bytes_per_second, "maxWriteBytesPerSecond", 100 * 1024 * 1024),
        "maxConcurrentTransfers": _positive_qos(max_concurrent_transfers, "maxConcurrentTransfers", 4),
        "quotaBytes": _optional_positive_int(quota_bytes, "quotaBytes"),
        "drainState": "active",
        "credentialProvider": credential_config,
        "createdAt": str(identity.get("createdAt") or _utc_iso()),
        "registeredAt": _utc_iso(),
        "lastProbe": probe_result,
    }
    _assert_no_secrets(record)
    record = _store_target(record)
    _write_checkpoint(
        target_id,
        {
            "incarnationId": str(identity.get("incarnationId") or ""),
            "targetGeneration": int((head or {}).get("targetGeneration") or 0),
            "latestCommitHash": str((head or {}).get("latestCommitHash") or TARGET_GENESIS_HASH),
        },
    )
    return record


def open_target_store(target_id: str, *, write_intent: bool = True, client: Any | None = None) -> Any:
    """Open the backend store for a registered target."""
    from deepseek_infra.infra.workspace import backup_target_s3
    from deepseek_infra.infra.workspace.backup_target_store import open_filesystem_store, probe_store_capabilities

    if target_id == "managed-local":
        from deepseek_infra.infra.workspace import backups

        return open_filesystem_store(backups.BACKUP_DIR)
    record = get_target(target_id)
    kind = str(record.get("kind") or "filesystem")
    if kind == "filesystem":
        root = verify_target_ready(target_id, write_intent=write_intent)
        return open_filesystem_store(root)
    if kind == "s3":
        store = backup_target_s3.open_s3_store(record, client=client)
        last = record.get("lastProbe") if isinstance(record.get("lastProbe"), dict) else None
        last_results: dict[str, Any] = dict(last["results"]) if isinstance(last, dict) and isinstance(last.get("results"), dict) else {}
        last_capabilities: dict[str, Any] = (
            dict(last["capabilities"])
            if isinstance(last, dict) and isinstance(last.get("capabilities"), dict)
            else {}
        )
        provider_checksum_proven = (
            last_results.get("single-provider-sha256") == "PASS"
            and last_results.get("multipart-provider-sha256") == "PASS"
            and last_capabilities.get("integrityMode") == "strong-provider-checksum"
        )
        if provider_checksum_proven:
            store.set_integrity_mode("strong-provider-checksum")
        if write_intent:
            if not last or not last.get("scheduledBackupReady"):
                probe = probe_store_capabilities(store)
                record = _mutate_target(
                    target_id,
                    lambda current: {**current, "lastProbe": probe},
                    bump_generation=False,
                )
                if not probe.get("scheduledBackupReady"):
                    raise AppError("unsupported-conditional-target: conditional writes unavailable", code=ErrorCode.INVALID_REQUEST, status=503)
        return store
    if kind == "webdav":
        raise AppError("WebDAV targets are reserved but not GA in 4.4.6", code=ErrorCode.INVALID_REQUEST, status=501)
    raise AppError(f"unsupported target kind: {kind}", code=ErrorCode.INVALID_PAYLOAD)


def record_remote_target_head(store: Any, *, target_id: str, generation: int, commit_hash: str) -> None:
    """CAS-update control/head.json after a remote slot commit."""
    from deepseek_infra.infra.workspace.backup_target_store import head_key, put_json_if_match, read_json

    current = read_json(store, head_key()) or {
        "schemaVersion": 1,
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "incarnationId": "",
    }
    meta = store.stat(head_key())
    payload = {
        **current,
        "schemaVersion": 1,
        "targetGeneration": int(generation),
        "latestCommitHash": commit_hash,
    }
    if meta is None:
        from deepseek_infra.infra.workspace.backup_target_store import put_json_if_absent

        put_json_if_absent(store, head_key(), payload)
    else:
        try:
            put_json_if_match(store, head_key(), payload, expected_etag=meta.etag)
        except AppError:
            # Concurrent head advance is reconciled later; commit marker already won.
            pass
    _write_checkpoint(
        target_id,
        {
            "incarnationId": str(current.get("incarnationId") or ""),
            "targetGeneration": int(generation),
            "latestCommitHash": commit_hash,
        },
    )


def get_target(target_id: str) -> dict[str, Any]:
    safe_id = str(target_id or "")
    path = _registry_path(safe_id)
    authoritative = backup_control.get_target(safe_id)
    if authoritative is None:
        if not path.is_file():
            raise AppError("Backup target not found", code=ErrorCode.NOT_FOUND, status=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError("Backup target registry is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
        if not isinstance(data, dict) or not data.get("targetId"):
            raise AppError("Backup target registry is unreadable", code=ErrorCode.INTERNAL, status=500)
        authoritative = backup_control.adopt_target_projection(data)
    try:
        projected = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        projected = None
    if projected != authoritative:
        _project_target(authoritative)
    return authoritative


def list_targets() -> list[dict[str, Any]]:
    if BACKUP_TARGET_DIR.is_dir():
        for path in sorted(BACKUP_TARGET_DIR.glob("*.json")):
            if path.name.endswith(".checkpoint.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("targetId"):
                backup_control.adopt_target_projection(data)
    targets = backup_control.list_targets()
    for target in targets:
        path = _registry_path(str(target["targetId"]))
        try:
            projected = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, json.JSONDecodeError):
            projected = None
        if projected != target:
            _project_target(target)
    return targets


def delete_target(target_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
    get_target(target_id)
    record = backup_control.delete_target(target_id, expected_generation=expected_generation)
    _registry_path(target_id).unlink(missing_ok=True)
    return {"deleted": True, "targetId": record["targetId"]}


def probe_target(target_id: str) -> dict[str, Any]:
    """Re-verify marker, location and lineage; never raises for offline targets."""
    try:
        record = get_target(target_id)
    except AppError as exc:
        return {"targetId": target_id, "ready": False, "status": "blocked-target-unavailable", "detail": str(exc)}
    kind = str(record.get("kind") or "filesystem")
    if kind == "s3":
        try:
            from deepseek_infra.infra.workspace.backup_target_store import probe_store_capabilities

            store = open_target_store(target_id, write_intent=False)
            result = probe_store_capabilities(store)
            try:
                detect = getattr(store, "detect_versioning", None)
                if callable(detect):
                    result.setdefault("capabilities", {})["versioning"] = detect()
            except Exception:
                pass
            _mutate_target(
                target_id,
                lambda current: {**current, "lastProbe": result},
                bump_generation=False,
            )
            try:
                from deepseek_infra.infra.workspace import backup_dr_ledger
                backup_dr_ledger.record_target_evidence(
                    target_id=target_id,
                    observed_at=str(result.get("probedAt") or _now_iso()),
                    scheduled_ready=bool(result.get("scheduledBackupReady")),
                    integrity_mode=str((result.get("capabilities") or {}).get("integrityMode") or ""),
                    status="ok" if result.get("scheduledBackupReady") else "error",
                    reason=None if result.get("scheduledBackupReady") else str(result.get("status") or "target-not-ready"),
                    details=result,
                )
            except Exception:
                pass
            return {
                "targetId": target_id,
                "ready": bool(result.get("scheduledBackupReady")),
                "status": str(result.get("status") or "ok"),
                "kind": "s3",
                "scheduledBackupReady": bool(result.get("scheduledBackupReady")),
                "probe": result,
            }
        except AppError as exc:
            try:
                from deepseek_infra.infra.workspace import backup_dr_ledger
                backup_dr_ledger.record_target_evidence(
                    target_id=target_id,
                    observed_at=_now_iso(),
                    scheduled_ready=False,
                    status="error",
                    reason=str(exc)[:100],
                )
            except Exception:
                pass
            return {
                "targetId": target_id,
                "ready": False,
                "status": "blocked-target-unavailable",
                "kind": "s3",
                "scheduledBackupReady": False,
                "detail": str(exc),
            }
    try:
        path = verify_target_ready(target_id)
    except AppError as exc:
        detail = str(exc)
        status = "blocked-target-unavailable"
        for code in ("target-rollback-detected", "target-fork-detected", "target-clone-detected", "unsupported-conditional-target"):
            if code in detail:
                status = code
                break
        try:
            from deepseek_infra.infra.workspace import backup_dr_ledger
            backup_dr_ledger.record_target_evidence(
                target_id=target_id,
                observed_at=_now_iso(),
                scheduled_ready=False,
                status="error",
                reason=status,
                details={"detail": detail},
            )
        except Exception:
            pass
        return {"targetId": target_id, "ready": False, "status": status, "detail": detail, "kind": "filesystem", "scheduledBackupReady": False}
    try:
        from deepseek_infra.infra.workspace import backup_dr_ledger
        backup_dr_ledger.record_target_evidence(
            target_id=target_id,
            observed_at=_now_iso(),
            scheduled_ready=True,
            status="ok",
            details={"path": str(path)},
        )
    except Exception:
        pass
    return {"targetId": target_id, "ready": True, "status": "ok", "path": str(path), "kind": "filesystem", "scheduledBackupReady": True}


def verify_target_ready(target_id: str, *, write_intent: bool = True) -> Path:
    """Return the validated target directory or raise blocked-target-unavailable.

    Called before every publish and retention pass so a swapped mount or marker
    stops writes immediately. With ``write_intent`` the target lineage is also
    checked against the trusted checkpoint — rollbacks, forks and incarnation
    changes raise their explicit anomalies — while read-only callers get the
    marker-validated path without checkpoint side effects.
    """
    record = get_target(target_id)
    registered_path = Path(str(record.get("path") or ""))
    try:
        resolved = validate_target_location(registered_path, exclude_target_id=str(record.get("targetId") or ""))
    except AppError as exc:
        raise AppError(f"blocked-target-unavailable: {exc}", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    marker = resolved / TARGET_MARKER_NAME
    if not marker.is_file():
        raise AppError("blocked-target-unavailable: target marker is missing", code=ErrorCode.INVALID_REQUEST, status=409)
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("blocked-target-unavailable: target marker is unreadable", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    if not isinstance(marker_data, dict) or marker_data.get("targetId") != record.get("targetId") or marker_data.get("targetNonce") != record.get("targetNonce"):
        raise AppError("blocked-target-unavailable: target marker was replaced", code=ErrorCode.INVALID_REQUEST, status=409)
    if not write_intent:
        return resolved
    return _check_lineage(str(record["targetId"]), resolved, marker_data)


def _check_lineage(target_id: str, resolved: Path, marker_data: dict[str, Any]) -> Path:
    marker = resolved / TARGET_MARKER_NAME
    if not marker_data.get("incarnationId"):
        from deepseek_infra.infra.workspace import backup_publish

        latest = backup_publish.latest_commit(resolved)
        upgraded = {
            **marker_data,
            "schemaVersion": TARGET_SCHEMA_VERSION,
            "incarnationId": f"inc_{secrets.token_hex(8)}",
            "ownerInstallationId": str(marker_data.get("ownerInstallationId") or installation_id()),
            "targetGeneration": int(latest["targetGeneration"]) if latest is not None else 0,
            "latestCommitHash": str(latest["commitHash"]) if latest is not None else TARGET_GENESIS_HASH,
        }
        _atomic_write_json(marker, upgraded)
        _write_checkpoint(target_id, upgraded)
        return resolved
    checkpoint = _read_checkpoint(target_id)
    if checkpoint is None:
        _write_checkpoint(target_id, marker_data)
        return resolved
    generation = int(marker_data.get("targetGeneration") or 0)
    seen_generation = int(checkpoint.get("lastSeenGeneration") or 0)
    if generation < seen_generation:
        raise AppError(
            f"target-rollback-detected: target generation {generation} is behind the trusted generation {seen_generation}; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    head = str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH)
    seen_head = str(checkpoint.get("lastSeenCommitHash") or TARGET_GENESIS_HASH)
    if str(marker_data.get("incarnationId") or "") != str(checkpoint.get("incarnationId") or ""):
        raise AppError(
            "target-fork-detected: target incarnation changed; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    if generation == seen_generation and head != seen_head:
        raise AppError(
            "target-fork-detected: same target generation but a different commit head; adopt this branch or register it as new",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    _write_checkpoint(target_id, marker_data)
    return resolved


def record_target_head(root: Path, *, target_id: str, generation: int, commit_hash: str) -> None:
    """Advance the marker's commit head and the trusted checkpoint after a slot commit."""
    marker = root / TARGET_MARKER_NAME
    if not marker.is_file():
        return
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(marker_data, dict):
        return
    marker_data = {
        **marker_data,
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "incarnationId": str(marker_data.get("incarnationId") or f"inc_{secrets.token_hex(8)}"),
        "ownerInstallationId": str(marker_data.get("ownerInstallationId") or installation_id()),
        "targetGeneration": int(generation),
        "latestCommitHash": commit_hash,
    }
    _atomic_write_json(marker, marker_data)
    _write_checkpoint(target_id, marker_data)


def adopt_target_incarnation(target_id: str) -> dict[str, Any]:
    """Accept the currently attached disk as the authoritative branch of a fork."""
    record = get_target(target_id)
    resolved = validate_target_location(Path(str(record.get("path") or "")), exclude_target_id=target_id)
    marker = resolved / TARGET_MARKER_NAME
    if not marker.is_file():
        raise AppError("blocked-target-unavailable: target marker is missing", code=ErrorCode.INVALID_REQUEST, status=409)
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("blocked-target-unavailable: target marker is unreadable", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    if not isinstance(marker_data, dict) or marker_data.get("targetId") != record.get("targetId") or marker_data.get("targetNonce") != record.get("targetNonce"):
        raise AppError("blocked-target-unavailable: target marker was replaced", code=ErrorCode.INVALID_REQUEST, status=409)
    adopted = {
        **marker_data,
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": int(marker_data.get("targetGeneration") or 0),
        "latestCommitHash": str(marker_data.get("latestCommitHash") or TARGET_GENESIS_HASH),
    }
    _atomic_write_json(marker, adopted)
    _write_checkpoint(target_id, adopted)
    return {"targetId": target_id, "adopted": True, "incarnationId": adopted["incarnationId"]}


def reinitialize_target(path: Path | str, *, label: str = "") -> dict[str, Any]:
    """Register a directory as a brand-new target with a fresh lineage."""
    resolved = _resolve_basic(Path(path))
    marker = resolved / TARGET_MARKER_NAME
    previous_id: str | None = None
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict) and existing.get("targetId"):
            previous_id = str(existing["targetId"])
    violation = _containment_violation(resolved, exclude_target_id=previous_id)
    if violation:
        raise AppError(f"Unsafe backup target: {violation}", code=ErrorCode.INVALID_PAYLOAD)
    target_id = f"target_{secrets.token_hex(6)}"
    nonce = secrets.token_hex(16)
    marker_payload = {
        "schemaVersion": TARGET_SCHEMA_VERSION,
        "targetId": target_id,
        "targetNonce": nonce,
        "incarnationId": f"inc_{secrets.token_hex(8)}",
        "ownerInstallationId": installation_id(),
        "targetGeneration": 0,
        "latestCommitHash": TARGET_GENESIS_HASH,
        "createdAt": _utc_iso(),
    }
    _atomic_write_json(marker, marker_payload)
    record = _register(resolved, target_id, nonce, label=label, created_at=marker_payload["createdAt"])
    _write_checkpoint(target_id, marker_payload)
    return record


def drain_target(target_id: str, *, reason: str = "", expected_generation: int | None = None) -> dict[str, Any]:
    """Set target drain state to draining."""
    now = _utc_iso()
    return _mutate_target(
        target_id,
        lambda target: {**target, "drainState": "draining", "drainReason": reason, "drainingAt": now},
        expected_generation=expected_generation,
    )


def activate_target(target_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
    """Set target drain state back to active."""
    def _activate(target: dict[str, Any]) -> dict[str, Any]:
        updated = {**target, "drainState": "active"}
        updated.pop("drainReason", None)
        updated.pop("drainingAt", None)
        updated.pop("drainedAt", None)
        return updated

    return _mutate_target(target_id, _activate, expected_generation=expected_generation)


def mark_target_drained(target_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
    """Mark a draining target as drained."""
    now = _utc_iso()
    return _mutate_target(
        target_id,
        lambda target: {**target, "drainState": "drained", "drainedAt": now},
        expected_generation=expected_generation,
    )


def get_target_drain_state(target_id: str) -> str:
    try:
        target = get_target(target_id)
        return str(target.get("drainState") or "active")
    except Exception:
        return "unknown"


def probe_target_capacity(target_id: str) -> dict[str, Any]:
    """Probe physical or configured capacity for a target without forging fake disk sizes."""
    import shutil

    try:
        target = get_target(target_id)
    except Exception:
        return {
            "targetId": target_id,
            "totalBytes": None,
            "usedBytes": None,
            "freeBytes": None,
            "freePercent": None,
            "observedAt": _utc_iso(),
            "source": "unknown",
        }

    kind = str(target.get("kind") or "filesystem")
    quota_bytes = target.get("quotaBytes")

    if kind == "filesystem" or target.get("path"):
        target_path = Path(str(target.get("path") or ""))
        if target_path.is_dir():
            try:
                usage = shutil.disk_usage(target_path)
                total = usage.total
                free = usage.free
                used = usage.used
                free_pct = round((free / total) * 100.0, 2) if total > 0 else 0.0
                return {
                    "targetId": target_id,
                    "totalBytes": total,
                    "usedBytes": used,
                    "freeBytes": free,
                    "freePercent": free_pct,
                    "observedAt": _utc_iso(),
                    "source": "filesystem",
                }
            except OSError:
                pass

    if quota_bytes is not None and int(quota_bytes) > 0:
        quota = int(quota_bytes)
        # Approximate used bytes from known copy sizes or zero
        from deepseek_infra.infra.workspace import backup_dr_ledger
        copies = backup_dr_ledger.list_logical_recovery_copies(target_id=target_id)
        used = sum(int(c.get("logicalBytes") or c.get("physicalBytes") or 0) for c in copies if c.get("recoverable"))
        free = max(0, quota - used)
        free_pct = round((free / quota) * 100.0, 2) if quota > 0 else 0.0
        return {
            "targetId": target_id,
            "totalBytes": quota,
            "usedBytes": used,
            "freeBytes": free,
            "freePercent": free_pct,
            "observedAt": _utc_iso(),
            "source": "configured-quota",
        }

    return {
        "targetId": target_id,
        "totalBytes": None,
        "usedBytes": None,
        "freeBytes": None,
        "freePercent": None,
        "observedAt": _utc_iso(),
        "source": "unknown",
    }


