"""Durable scheduled backup policies (4.4.4).

Policies persist only public ``age1...`` recipients and schedule metadata. They
never carry private keys, passphrases, tokens or Redis credentials; unattended
backups always use ``age-recipient`` protection while manual backups keep their
existing passphrase support.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
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
_TARGET_ID = re.compile(r"^target_[a-z0-9][a-z0-9._-]{0,63}$")


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


def _normalize_schedule(raw: Any) -> dict[str, Any]:
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
    target_id = str(payload.get("targetId") or "").strip()
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
        "retentionPolicyId": _require_safe_id(payload.get("retentionPolicyId") or DEFAULT_RETENTION_POLICY_ID, "retentionPolicyId"),
        "retry": _normalize_retry(payload.get("retry")),
        "incremental": _normalize_incremental(payload.get("incremental")),
        "createdAt": created_at or now,
        "updatedAt": now,
    }


def create_policy(payload: dict[str, Any]) -> dict[str, Any]:
    policy = normalize_policy(payload)
    _ensure_dir()
    if _policy_path(policy["policyId"]).exists():
        raise AppError("Backup policy id collision; retry", code=ErrorCode.INVALID_REQUEST, status=409)
    _atomic_write_json(_policy_path(policy["policyId"]), policy)
    return policy


def get_policy(policy_id: str) -> dict[str, Any]:
    path = _policy_path(_require_safe_id(policy_id, "policyId"))
    if not path.is_file():
        raise AppError("Backup policy not found", code=ErrorCode.NOT_FOUND, status=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("Backup policy store is unreadable", code=ErrorCode.INTERNAL, status=500) from exc
    return data


def list_policies() -> list[dict[str, Any]]:
    if not BACKUP_POLICY_DIR.is_dir():
        return []
    policies: list[dict[str, Any]] = []
    for path in sorted(BACKUP_POLICY_DIR.glob("policy_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("policyId"):
            policies.append(data)
    return policies


def update_policy(policy_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    existing = get_policy(policy_id)
    if not isinstance(patch, dict):
        raise AppError("Backup policy patch must be an object", code=ErrorCode.INVALID_PAYLOAD)
    merged = dict(existing)
    for key in ("name", "enabled", "schedule", "scope", "frontendMirror", "protection", "targetId", "retentionPolicyId", "retry", "incremental"):
        if key in patch:
            merged[key] = patch[key]
    normalized = normalize_policy(merged, policy_id=existing["policyId"], created_at=str(existing.get("createdAt") or ""))
    _atomic_write_json(_policy_path(policy_id), normalized)
    return normalized


def delete_policy(policy_id: str) -> dict[str, Any]:
    policy = get_policy(policy_id)
    _policy_path(policy_id).unlink(missing_ok=True)
    return {"deleted": True, "policyId": policy["policyId"]}


def enabled_policies() -> list[dict[str, Any]]:
    return [policy for policy in list_policies() if policy.get("enabled")]


def active_recipients() -> tuple[str, ...]:
    recipients: set[str] = set()
    for policy in enabled_policies():
        protection = policy.get("protection")
        if isinstance(protection, dict) and protection.get("mode") == "age-recipient":
            for recipient in protection.get("recipients") or []:
                if isinstance(recipient, str):
                    recipients.add(recipient)
    return tuple(sorted(recipients))


def restore_projection(policy: dict[str, Any]) -> dict[str, Any]:
    """Project a policy into its post-restore safe state.

    Restored policies are always disabled and unbound so a restored workspace can
    never immediately run a schedule or apply old retention rules to a target.
    """
    projected = dict(policy)
    projected["enabled"] = False
    projected["targetId"] = UNBOUND_TARGET
    for runtime_key in ("lastRunAt", "lastRunId", "lastScheduleSlot", "lease", "scheduleSlot"):
        projected.pop(runtime_key, None)
    return projected
