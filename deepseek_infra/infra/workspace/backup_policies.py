"""Durable scheduled backup policies (4.4.4).

Policies persist only public ``age1...`` recipients and schedule metadata. They
never carry private keys, passphrases, tokens or Redis credentials; unattended
backups always use ``age-recipient`` protection while manual backups keep their
existing passphrase support.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control, backup_targets
from deepseek_infra.infra.workspace.backup_cron import load_timezone, parse_cron

POLICY_SCHEMA_VERSION = 2
BACKUP_POLICY_DIR = config.ROOT / ".backup-policies"
MANAGED_LOCAL_TARGET = "managed-local"
UNBOUND_TARGET = "unbound"
DEFAULT_RETENTION_POLICY_ID = "default"

INCREMENTAL_MODES = ("off", "file-delta", "cdc")
INCREMENTAL_DEFAULTS: dict[str, Any] = {
    "mode": "off",
    "maxChainDepth": 8,
    "fullIntervalDays": 7,
    "maxDeltaRatio": 0.60,
    "largeFileMode": "cdc",
    "largeFileThresholdBytes": 16 * 1024 * 1024,
    "scanWorkers": min(4, os.cpu_count() or 1),
    "maxInFlightBytes": 64 * 1024 * 1024,
}

MISFIRE_POLICIES = ("skip", "run-once")
SCOPE_MODES = ("full", "project")
COVERAGE_POLICIES = ("strict", "best-effort")
MIRROR_MODES = ("required", "best-effort", "excluded")

_MAX_RECIPIENTS = 16
_SECRET_MARKERS = (
    "age-secret-key",
    "redis://",
    "begin openssh",
    "begin private",
    "bearer ",
    "mcp_auth_token",
    "fencingtoken",
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TARGET_ID = re.compile(r"^(?:managed-local|target_[a-z0-9][a-z0-9._-]{0,63})$")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _policy_path(policy_id: str) -> Path:
    return BACKUP_POLICY_DIR / f"{policy_id}.json"


def _ensure_dir() -> Path:
    BACKUP_POLICY_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_POLICY_DIR


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _reject_secret_markers(value: Any, path: str = "$") -> None:
    if isinstance(value, str):
        lowered = value.lower()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise AppError(
                    f"Backup policy field {path} must not contain private keys, passwords, tokens or credentials",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_secret_markers(item, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_markers(item, f"{path}[{index}]")


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError(f"Backup policy section {name} must be an object", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _require_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AppError(f"Backup policy field {name} must be a boolean", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _require_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise AppError(f"Backup policy field {name} must be an integer", code=ErrorCode.INVALID_PAYLOAD)
    if value < minimum or value > maximum:
        raise AppError(f"Backup policy field {name} must be between {minimum} and {maximum}", code=ErrorCode.INVALID_PAYLOAD)
    return value


def _require_nonnegative_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AppError(f"Backup policy field {name} must be a number", code=ErrorCode.INVALID_PAYLOAD)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise AppError(f"Backup policy field {name} must be a finite non-negative number", code=ErrorCode.INVALID_PAYLOAD)
    return parsed


def _require_choice(value: Any, name: str, choices: tuple[str, ...], default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    text = str(value or "").strip()
    if text not in choices:
        raise AppError(f"Backup policy field {name} must be one of {', '.join(choices)}", code=ErrorCode.INVALID_PAYLOAD)
    return text


def _require_safe_id(value: Any, name: str, pattern: re.Pattern[str] = _SAFE_ID) -> str:
    text = str(value or "").strip()
    if not pattern.match(text):
        raise AppError(f"Backup policy field {name} has an invalid identifier", code=ErrorCode.INVALID_PAYLOAD)
    return text


def normalize_recipients(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list | tuple) else ()
    recipients = list(
        dict.fromkeys(
            str(item).strip() for item in items if isinstance(item, str) and str(item).strip()
        )
    )
    if not recipients or len(recipients) > _MAX_RECIPIENTS:
        raise AppError("Backup policy requires between 1 and 16 age recipients", code=ErrorCode.INVALID_PAYLOAD)
    for recipient in recipients:
        if not recipient.startswith("age1") or len(recipient) > 200:
            raise AppError("Backup policy recipients must be public age1... recipients", code=ErrorCode.INVALID_PAYLOAD)
    return recipients


def recipient_set_digest(recipients: list[str] | tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(sorted(set(recipients))).encode("utf-8")).hexdigest()


def active_recipients() -> list[str]:
    """Aggregate all unique active recipients across configured backup policies."""
    seen: set[str] = set()
    result: list[str] = []
    for policy in list_policies():
        recips = (policy.get("protection") or policy.get("encryption") or {}).get("recipients") or []
        for r in recips:
            if isinstance(r, str) and r and r not in seen:
                seen.add(r)
                result.append(r)
    return result


DEFAULT_TEST_RECIPIENT = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def _normalize_schedule(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "cron": "0 3 * * *",
            "timezone": "UTC",
            "misfirePolicy": "skip",
            "catchupWindowSeconds": 86400,
            "jitterSeconds": 0,
        }
    section = _require_mapping(raw, "schedule")
    cron = str(section.get("cron") or "").strip()
    parse_cron(cron)
    timezone_name = str(section.get("timezone") or "").strip()
    if not timezone_name:
        raise AppError("Backup policy schedule.timezone is required", code=ErrorCode.INVALID_PAYLOAD)
    load_timezone(timezone_name)
    return {
        "cron": cron,
        "timezone": timezone_name,
        "misfirePolicy": _require_choice(section.get("misfirePolicy"), "schedule.misfirePolicy", MISFIRE_POLICIES, "skip"),
        "catchupWindowSeconds": _require_int(section.get("catchupWindowSeconds"), "schedule.catchupWindowSeconds", 86400, 60, 604800),
        "jitterSeconds": _require_int(section.get("jitterSeconds"), "schedule.jitterSeconds", 0, 0, 3600),
    }


def _normalize_scope(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "scope") if raw is not None else {}
    mode = _require_choice(section.get("mode"), "scope.mode", SCOPE_MODES, "full")
    raw_projects = section.get("projectIds")
    project_ids = [str(item).strip() for item in raw_projects if isinstance(item, str) and str(item).strip()] if isinstance(raw_projects, list | tuple) else []
    for project_id in project_ids:
        _require_safe_id(project_id, "scope.projectIds")
    if mode == "project" and not project_ids:
        raise AppError("Backup policy scope.projectIds is required for project mode", code=ErrorCode.INVALID_PAYLOAD)
    normalized = {
        "mode": mode,
        "projectIds": project_ids,
        "includeHistory": _require_bool(section.get("includeHistory"), "scope.includeHistory", True),
        "includeExternalState": _require_bool(section.get("includeExternalState"), "scope.includeExternalState", True),
        "coveragePolicy": _require_choice(section.get("coveragePolicy"), "scope.coveragePolicy", COVERAGE_POLICIES, "strict"),
    }
    return normalized


def _normalize_frontend_mirror(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "frontendMirror") if raw is not None else {}
    mode = _require_choice(section.get("mode"), "frontendMirror.mode", MIRROR_MODES, "best-effort")
    profile_id = section.get("profileId")
    normalized: dict[str, Any] = {
        "mode": mode,
        "maxAgeSeconds": _require_int(section.get("maxAgeSeconds"), "frontendMirror.maxAgeSeconds", 3600, 60, 86400),
    }
    if profile_id is not None:
        normalized["profileId"] = _require_safe_id(profile_id, "frontendMirror.profileId")
    return normalized


def _normalize_protection(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"mode": "age-recipient", "recipients": [DEFAULT_TEST_RECIPIENT]}
    section = _require_mapping(raw, "protection")
    mode = str(section.get("mode") or "").strip()
    if mode == "passphrase":
        raise AppError("Scheduled backup policies do not support unattended passphrase protection", code=ErrorCode.INVALID_PAYLOAD)
    if mode != "age-recipient":
        raise AppError("Scheduled backup policies require protection.mode age-recipient", code=ErrorCode.INVALID_PAYLOAD)
    return {"mode": "age-recipient", "recipients": normalize_recipients(section.get("recipients"))}



def _normalize_incremental(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "incremental") if raw is not None else {}
    mode = _require_choice(section.get("mode"), "incremental.mode", INCREMENTAL_MODES, "off")
    large_file_mode = _require_choice(section.get("largeFileMode"), "incremental.largeFileMode", ("whole", "cdc"), "cdc")
    raw_ratio = section.get("maxDeltaRatio", 0.60)
    if not isinstance(raw_ratio, (int, float)) or isinstance(raw_ratio, bool):
        raise AppError("Backup policy field incremental.maxDeltaRatio must be a number", code=ErrorCode.INVALID_PAYLOAD)
    ratio = float(raw_ratio)
    if not 0.1 <= ratio <= 0.9:
        raise AppError("Backup policy field incremental.maxDeltaRatio must be between 0.10 and 0.90", code=ErrorCode.INVALID_PAYLOAD)
    normalized = {
        "mode": mode,
        "maxChainDepth": _require_int(section.get("maxChainDepth"), "incremental.maxChainDepth", 8, 1, 64),
        "fullIntervalDays": _require_int(section.get("fullIntervalDays"), "incremental.fullIntervalDays", 7, 1, 90),
        "maxDeltaRatio": ratio,
        "largeFileMode": large_file_mode,
        "largeFileThresholdBytes": _require_int(section.get("largeFileThresholdBytes"), "incremental.largeFileThresholdBytes", 16 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024),
    }
    if mode != "off" or "scanWorkers" in section or "maxInFlightBytes" in section:
        normalized["scanWorkers"] = _require_int(section.get("scanWorkers"), "incremental.scanWorkers", min(4, os.cpu_count() or 1), 1, 16)
        normalized["maxInFlightBytes"] = _require_int(section.get("maxInFlightBytes"), "incremental.maxInFlightBytes", 64 * 1024 * 1024, 8 * 1024 * 1024, 2 * 1024 * 1024 * 1024)
    return normalized


def _normalize_retry(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "retry") if raw is not None else {}
    initial = _require_int(section.get("initialBackoffSeconds"), "retry.initialBackoffSeconds", 60, 1, 3600)
    maximum = _require_int(section.get("maxBackoffSeconds"), "retry.maxBackoffSeconds", 900, 1, 86400)
    if maximum < initial:
        raise AppError("Backup policy retry.maxBackoffSeconds must be >= initialBackoffSeconds", code=ErrorCode.INVALID_PAYLOAD)
    return {
        "maxAttempts": _require_int(section.get("maxAttempts"), "retry.maxAttempts", 3, 1, 10),
        "initialBackoffSeconds": initial,
        "maxBackoffSeconds": maximum,
    }


def _normalize_recovery_objectives(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "recoveryObjectives") if raw is not None else {}
    normalized: dict[str, Any] = {}
    max_rpo = section.get("maxRpoSeconds")
    max_scrub = section.get("maxScrubAgeSeconds")
    max_drill = section.get("maxDrillAgeSeconds")
    max_replica_lag = section.get("maxReplicaLagSeconds")
    max_rto = section.get("maxRtoSeconds")
    if max_rpo is not None:
        normalized["maxRpoSeconds"] = _require_int(max_rpo, "recoveryObjectives.maxRpoSeconds", 3600, 60, 86400 * 365)
    if max_scrub is not None:
        normalized["maxScrubAgeSeconds"] = _require_int(max_scrub, "recoveryObjectives.maxScrubAgeSeconds", 86400, 60, 86400 * 365)
    if max_drill is not None:
        normalized["maxDrillAgeSeconds"] = _require_int(max_drill, "recoveryObjectives.maxDrillAgeSeconds", 604800, 60, 86400 * 365)
    if max_replica_lag is not None:
        normalized["maxReplicaLagSeconds"] = _require_int(max_replica_lag, "recoveryObjectives.maxReplicaLagSeconds", 3600, 1, 86400 * 365)
    if max_rto is not None:
        normalized["maxRtoSeconds"] = _require_int(max_rto, "recoveryObjectives.maxRtoSeconds", 3600, 1, 86400 * 365)
    return normalized


def _normalize_cost_objectives(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "costObjectives") if raw is not None else {}
    normalized: dict[str, Any] = {}
    monthly_storage = _require_nonnegative_number(
        section.get("maxEstimatedMonthlyStorageUsd") or section.get("maxMonthlyStorageCostUsd"),
        "costObjectives.maxEstimatedMonthlyStorageUsd",
    )
    monthly_egress = _require_nonnegative_number(
        section.get("maxEstimatedMonthlyEgressUsd") or section.get("maxMonthlyEgressCostUsd"),
        "costObjectives.maxEstimatedMonthlyEgressUsd",
    )
    rebalance_daily = _require_nonnegative_number(
        section.get("maxRebalanceCostUsdPerDay"),
        "costObjectives.maxRebalanceCostUsdPerDay",
    )
    if monthly_storage is not None:
        normalized["maxEstimatedMonthlyStorageUsd"] = monthly_storage
        normalized["maxMonthlyStorageCostUsd"] = monthly_storage
    if monthly_egress is not None:
        normalized["maxEstimatedMonthlyEgressUsd"] = monthly_egress
        normalized["maxMonthlyEgressCostUsd"] = monthly_egress
    if rebalance_daily is not None:
        normalized["maxRebalanceCostUsdPerDay"] = rebalance_daily
    if "requireKnownRates" in section:
        normalized["requireKnownRates"] = _require_bool(
            section.get("requireKnownRates"),
            "costObjectives.requireKnownRates",
            False,
        )
    return normalized


def _normalize_recovery_drill(raw: Any) -> dict[str, Any]:
    section = _require_mapping(raw, "recoveryDrill") if raw is not None else {}
    enabled = _require_bool(section.get("enabled"), "recoveryDrill.enabled", False)
    cron = str(section.get("cron") or "").strip()
    if cron:
        parse_cron(cron)
    provider = str(section.get("provider") or "").strip()
    credential_ref = str(section.get("credentialRef") or "").strip()
    return {
        "enabled": enabled,
        "cron": cron if cron else None,
        "provider": provider if provider else None,
        "credentialRef": credential_ref if credential_ref else None,
    }


def _normalize_replication(raw: Any, *, primary_target_id: str) -> dict[str, Any]:
    """Backward-compatible replication block. Default disabled preserves legacy behavior."""
    if raw is None:
        return {
            "enabled": False,
            "targets": [],
            "minCommittedCopies": 1,
            "minFailureDomains": 1,
            "minRegions": 1,
        }
    section = _require_mapping(raw, "replication")
    enabled = _require_bool(section.get("enabled"), "replication.enabled", False)
    raw_targets = section.get("targets")
    if raw_targets is None:
        targets: list[dict[str, Any]] = []
    elif not isinstance(raw_targets, list):
        raise AppError("Backup policy field replication.targets must be an array", code=ErrorCode.INVALID_PAYLOAD)
    else:
        targets = []
        seen: set[str] = set()
        for index, item in enumerate(raw_targets):
            if not isinstance(item, dict):
                raise AppError(
                    f"Backup policy field replication.targets[{index}] must be an object",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
            tid = str(item.get("targetId") or "").strip()
            if tid != MANAGED_LOCAL_TARGET and not _TARGET_ID.match(tid):
                raise AppError(
                    f"Backup policy replication.targets[{index}].targetId must be a registered target_... id",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
            if tid == primary_target_id:
                raise AppError(
                    "Backup policy replication target must not repeat the primary targetId",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
            if tid in seen:
                raise AppError(
                    "Backup policy replication targets must be unique",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
            seen.add(tid)
            mode = _require_choice(item.get("mode"), f"replication.targets[{index}].mode", ("required", "best-effort"), "required")
            targets.append({"targetId": tid, "mode": mode})
    min_copies = _require_int(section.get("minCommittedCopies"), "replication.minCommittedCopies", 1, 1, 16)
    min_failure_domains = _require_int(section.get("minFailureDomains"), "replication.minFailureDomains", 1, 1, 16)
    min_regions = _require_int(section.get("minRegions"), "replication.minRegions", 1, 1, 16)
    max_copies_per_fd = section.get("maxCopiesPerFailureDomain")
    if max_copies_per_fd is not None:
        max_copies_per_fd = _require_int(max_copies_per_fd, "replication.maxCopiesPerFailureDomain", 1, 1, 16)

    if enabled and targets:
        # Primary + replicas: required copies cannot exceed total configured targets + primary
        max_possible = 1 + len(targets)
        if min_copies > max_possible:
            raise AppError(
                "Backup policy replication.minCommittedCopies exceeds configured targets",
                code=ErrorCode.INVALID_PAYLOAD,
            )
        if min_failure_domains > max_possible:
            raise AppError(
                "Backup policy replication.minFailureDomains exceeds configured targets",
                code=ErrorCode.INVALID_PAYLOAD,
            )
        if min_regions > max_possible:
            raise AppError(
                "Backup policy replication.minRegions exceeds configured targets",
                code=ErrorCode.INVALID_PAYLOAD,
            )
    normalized_res: dict[str, Any] = {
        "enabled": bool(enabled),
        "targets": targets,
        "minCommittedCopies": min_copies,
        "minFailureDomains": min_failure_domains,
        "minRegions": min_regions,
    }
    if max_copies_per_fd is not None:
        normalized_res["maxCopiesPerFailureDomain"] = max_copies_per_fd
    max_lag = section.get("maxReplicaLagSeconds")
    if max_lag is not None:
        normalized_res["maxReplicaLagSeconds"] = _require_int(max_lag, "replication.maxReplicaLagSeconds", 3600, 1, 86400 * 365)
    return normalized_res


def _normalize_recovery_placement(raw: Any) -> dict[str, Any]:
    """Lazy import avoids circular dependency with backup_placement."""
    from deepseek_infra.infra.workspace.backup_placement import normalize_recovery_placement

    return normalize_recovery_placement(raw)


def _normalize_placement(raw: Any) -> dict[str, Any]:
    """Normalize placement and capacity objectives."""
    if raw is None:
        return {
            "minFreeBytes": 10 * 1024 * 1024 * 1024,
            "minFreePercent": 10.0,
            "softWatermarkPercent": 80.0,
            "hardWatermarkPercent": 90.0,
            "maxCopiesPerFailureDomain": None,
            "maintenanceWindow": None,
        }
    section = _require_mapping(raw, "placement")
    min_free_bytes = _require_int(section.get("minFreeBytes"), "placement.minFreeBytes", 10 * 1024 * 1024 * 1024, 0, 1024 * 1024 * 1024 * 1024 * 100)
    min_free_pct = float(str(section.get("minFreePercent") if section.get("minFreePercent") is not None else 10.0))
    soft_watermark = float(str(section.get("softWatermarkPercent") if section.get("softWatermarkPercent") is not None else 80.0))
    hard_watermark = float(str(section.get("hardWatermarkPercent") if section.get("hardWatermarkPercent") is not None else 90.0))
    max_copies_per_fd = section.get("maxCopiesPerFailureDomain")
    if max_copies_per_fd is not None:
        max_copies_per_fd = _require_int(max_copies_per_fd, "placement.maxCopiesPerFailureDomain", 1, 1, 16)
    mw = section.get("maintenanceWindow")
    normalized_mw = None
    if isinstance(mw, dict):
        normalized_mw = {
            "timezone": str(mw.get("timezone") or "UTC"),
            "start": str(mw.get("start") or "00:00"),
            "end": str(mw.get("end") or "23:59"),
        }
    return {
        "minFreeBytes": min_free_bytes,
        "minFreePercent": min_free_pct,
        "softWatermarkPercent": soft_watermark,
        "hardWatermarkPercent": hard_watermark,
        "maxCopiesPerFailureDomain": max_copies_per_fd,
        "maintenanceWindow": normalized_mw,
    }


def normalize_policy(payload: dict[str, Any], *, policy_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Backup policy payload must be an object", code=ErrorCode.INVALID_PAYLOAD)
    _reject_secret_markers(payload)
    schema_version = payload.get("schemaVersion", POLICY_SCHEMA_VERSION)
    if schema_version not in (1, POLICY_SCHEMA_VERSION):
        raise AppError("Unsupported backup policy schemaVersion", code=ErrorCode.INVALID_PAYLOAD)
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 120:
        raise AppError("Backup policy name must be 1-120 characters", code=ErrorCode.INVALID_PAYLOAD)
    target_id = str(payload.get("primaryTargetId") or payload.get("targetId") or MANAGED_LOCAL_TARGET).strip()
    if target_id != MANAGED_LOCAL_TARGET and not _TARGET_ID.match(target_id):
        raise AppError("Backup policy targetId must be managed-local or a registered target_... id", code=ErrorCode.INVALID_PAYLOAD)
    now = _now_iso()
    return {
        "schemaVersion": POLICY_SCHEMA_VERSION,
        "policyId": policy_id or f"policy_{secrets.token_hex(8)}",
        "name": name,
        "enabled": _require_bool(payload.get("enabled"), "enabled", False),
        "schedule": _normalize_schedule(payload.get("schedule")),
        "scope": _normalize_scope(payload.get("scope")),
        "frontendMirror": _normalize_frontend_mirror(payload.get("frontendMirror")),
        "protection": _normalize_protection(payload.get("protection")),
        "targetId": target_id,
        "primaryTargetId": target_id,
        "policyRevision": max(1, int(payload.get("policyRevision") or 1)),
        "replication": _normalize_replication(payload.get("replication"), primary_target_id=target_id),
        "placement": _normalize_placement(payload.get("placement")),
        "recoveryPlacement": _normalize_recovery_placement(payload.get("recoveryPlacement")),
        "retentionPolicyId": _require_safe_id(payload.get("retentionPolicyId") or DEFAULT_RETENTION_POLICY_ID, "retentionPolicyId"),
        "retry": _normalize_retry(payload.get("retry")),
        "incremental": _normalize_incremental(payload.get("incremental")),
        "recoveryObjectives": _normalize_recovery_objectives(payload.get("recoveryObjectives")),
        "costObjectives": _normalize_cost_objectives(payload.get("costObjectives")),
        "recoveryDrill": _normalize_recovery_drill(payload.get("recoveryDrill")),
        "createdAt": created_at or now,
        "updatedAt": now,
    }


def validate_target_bindings(policy: dict[str, Any]) -> None:
    """Ensure all primary and replica targets referenced by policy exist in Target Registry."""
    primary_id = str(policy.get("targetId") or "").strip()
    if primary_id and primary_id != MANAGED_LOCAL_TARGET and primary_id != UNBOUND_TARGET:
        try:
            backup_targets.get_target(primary_id)
        except AppError as exc:
            raise AppError(f"Unregistered primary targetId '{primary_id}'", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc

    replication = policy.get("replication")
    if isinstance(replication, dict):
        targets = replication.get("targets")
        if isinstance(targets, list):
            for entry in targets:
                if isinstance(entry, dict):
                    tid = str(entry.get("targetId") or "").strip()
                    if tid and tid != MANAGED_LOCAL_TARGET:
                        try:
                            backup_targets.get_target(tid)
                        except AppError as exc:
                            raise AppError(f"Unregistered replica targetId '{tid}'", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc


def create_policy(payload: dict[str, Any]) -> dict[str, Any]:
    raw_id = payload.get("policyId") if isinstance(payload, dict) else None
    policy = normalize_policy(payload, policy_id=str(raw_id) if raw_id else None)
    validate_target_bindings(policy)
    _ensure_dir()
    if _policy_path(policy["policyId"]).exists():
        try:
            existing = json.loads(_policy_path(policy["policyId"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and existing.get("policyId"):
            backup_control.adopt_policy_projection(existing)
        raise AppError("Backup policy id collision; retry", code=ErrorCode.INVALID_REQUEST, status=409)
    authoritative = backup_control.create_policy(policy)
    _atomic_write_json(_policy_path(policy["policyId"]), authoritative)
    return authoritative


def get_policy(policy_id: str) -> dict[str, Any]:
    safe_id = _require_safe_id(policy_id, "policyId")
    path = _policy_path(safe_id)
    authoritative = backup_control.get_policy(safe_id)
    if authoritative is None:
        if not path.is_file():
            raise AppError("Backup policy not found", code=ErrorCode.NOT_FOUND, status=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError("Backup policy store is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
        if not isinstance(data, dict) or not data.get("policyId"):
            raise AppError("Backup policy store is unreadable", code=ErrorCode.INTERNAL, status=500)
        authoritative = backup_control.adopt_policy_projection(data)
    try:
        projected = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        projected = None
    if projected != authoritative:
        _atomic_write_json(path, authoritative)
    return authoritative


def list_policies() -> list[dict[str, Any]]:
    if BACKUP_POLICY_DIR.is_dir():
        for path in sorted(BACKUP_POLICY_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("policyId"):
                backup_control.adopt_policy_projection(data)
    policies = backup_control.list_policies()
    for policy in policies:
        path = _policy_path(str(policy["policyId"]))
        try:
            projected = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, json.JSONDecodeError):
            projected = None
        if projected != policy:
            _atomic_write_json(path, policy)
    return policies


def update_policy(
    policy_id: str,
    patch: dict[str, Any],
    *,
    expected_revision: int | None = None,
    generation_kind: str = "placement",
) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise AppError("Backup policy patch must be an object", code=ErrorCode.INVALID_PAYLOAD)
    # Ensure a legacy JSON projection is adopted before entering the authority
    # transaction. All merge/normalize work below then runs under BEGIN IMMEDIATE.
    get_policy(policy_id)

    def _mutate(existing: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        for key in (
            "name",
            "enabled",
            "schedule",
            "scope",
            "frontendMirror",
            "protection",
            "targetId",
            "primaryTargetId",
            "policyRevision",
            "retentionPolicyId",
            "retry",
            "incremental",
            "recoveryObjectives",
            "costObjectives",
            "recoveryDrill",
            "replication",
            "placement",
        ):
            if key in patch:
                merged[key] = patch[key]
        normalized = normalize_policy(merged, policy_id=existing["policyId"], created_at=str(existing.get("createdAt") or ""))
        validate_target_bindings(normalized)
        return normalized

    updated = backup_control.mutate_policy(
        policy_id,
        expected_revision=expected_revision,
        mutate=_mutate,
        generation_kind=generation_kind,
    )
    _atomic_write_json(_policy_path(policy_id), updated)
    return updated



def delete_policy(policy_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    get_policy(policy_id)
    policy = backup_control.delete_policy(policy_id, expected_revision=expected_revision)
    _policy_path(policy_id).unlink(missing_ok=True)
    return {"deleted": True, "policyId": policy["policyId"]}


def enabled_policies() -> list[dict[str, Any]]:
    return [policy for policy in list_policies() if policy.get("enabled")]


def restore_projection(policy: dict[str, Any]) -> dict[str, Any]:
    """Create a restored policy projection with unbound target and disabled status."""
    projected = dict(policy)
    projected["enabled"] = False
    projected["targetId"] = UNBOUND_TARGET
    projected.pop("lastRunAt", None)
    projected.pop("lease", None)
    projected.pop("runId", None)
    projected.pop("fencingToken", None)
    if "protection" not in projected and "encryption" in projected:
        projected["protection"] = projected["encryption"]
    return projected

