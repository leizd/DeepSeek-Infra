"""Schema normalization helpers for Automation Runtime."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.schema import (
    new_id,
    normalize_description,
    normalize_source_ref,
    normalize_title,
    now_ms,
    timestamp_ms_to_iso,
    validate_project_id,
    validate_workspace_id,
)

TRIGGER_TYPES = {"manual", "schedule", "interval", "event"}
EVENT_TYPES = {"project.updated", "media.ready", "artifact.created", "saved_item.created"}
CONDITION_TYPES = {"always", "project_changed", "media_ready", "new_saved_items", "url_changed", "artifact_created"}
ACTION_TYPES = {
    "run_skill",
    "browser_snapshot",
    "browser_check",
    "project_summary",
    "media_process",
    "create_artifact",
    "save_item",
    "export_conversation",
    "export_project",
}
RUN_STATUSES = {"success", "failed", "skipped", "canceled", "requires_confirmation"}
DEFAULT_ARTIFACT_TYPE = "markdown"


def new_automation_id() -> str:
    return new_id("auto")


def new_run_id() -> str:
    return new_id("auto_run")


def validate_automation_id(value: str) -> str:
    return validate_workspace_id(value, label="automation id")


def validate_run_id(value: str) -> str:
    return validate_workspace_id(value, label="automation run id")


def normalize_automation(payload: dict[str, Any], *, existing: dict[str, Any] | None = None, touch: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Automation payload must be an object", code=ErrorCode.INVALID_PAYLOAD)
    existing_data = existing if isinstance(existing, dict) else {}
    now = now_ms()
    automation_id = str(payload.get("automationId") or payload.get("id") or existing_data.get("automationId") or "").strip()
    if automation_id:
        automation_id = validate_automation_id(automation_id)
    else:
        automation_id = new_automation_id()
    project_id = str(payload.get("projectId") if "projectId" in payload else existing_data.get("projectId") or "").strip()
    safe_project_id = validate_project_id(project_id) if project_id else ""
    created_at_ms = _safe_int(existing_data.get("createdAtMs"), default=now)
    updated_at_ms = now if touch else _safe_int(existing_data.get("updatedAtMs") or payload.get("updatedAtMs"), default=now)
    return {
        "automationId": automation_id,
        "id": automation_id,
        "projectId": safe_project_id,
        "name": normalize_title(payload.get("name", existing_data.get("name")), default="Automation"),
        "description": normalize_description(payload.get("description", existing_data.get("description"))),
        "enabled": _bool(payload.get("enabled", existing_data.get("enabled", True)), default=True),
        "trigger": normalize_trigger(payload.get("trigger", existing_data.get("trigger") or {"type": "manual"})),
        "condition": normalize_condition(payload.get("condition", existing_data.get("condition") or {"type": "always"})),
        "action": normalize_action(payload.get("action", existing_data.get("action") or {})),
        "output": normalize_output(payload.get("output", existing_data.get("output") or {})),
        "policy": normalize_policy(payload.get("policy", existing_data.get("policy") or {})),
        "metadata": normalize_source_ref(payload.get("metadata", existing_data.get("metadata") or {})),
        "createdAt": str(existing_data.get("createdAt") or timestamp_ms_to_iso(created_at_ms)),
        "updatedAt": timestamp_ms_to_iso(updated_at_ms),
        "createdAtMs": created_at_ms,
        "updatedAtMs": updated_at_ms,
    }


def normalize_trigger(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    trigger_type = str(data.get("type") or "manual").strip().lower()
    if trigger_type not in TRIGGER_TYPES:
        raise AppError("Unsupported automation trigger type", code=ErrorCode.INVALID_PAYLOAD)
    trigger: dict[str, Any] = {"type": trigger_type}
    cron = str(data.get("cron") or "").strip()[:120]
    if cron:
        trigger["cron"] = cron
    interval = _safe_int(data.get("intervalSeconds"), default=0)
    if interval > 0:
        trigger["intervalSeconds"] = interval
    event = str(data.get("event") or "").strip().lower()
    if event:
        if event not in EVENT_TYPES:
            raise AppError("Unsupported automation event trigger", code=ErrorCode.INVALID_PAYLOAD)
        trigger["event"] = event
    return trigger


def normalize_condition(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    condition_type = str(data.get("type") or "always").strip().lower()
    if condition_type not in CONDITION_TYPES:
        raise AppError("Unsupported automation condition type", code=ErrorCode.INVALID_PAYLOAD)
    return {
        "type": condition_type,
        "sinceLastRun": _bool(data.get("sinceLastRun"), default=False),
        "projectChanged": _bool(data.get("projectChanged"), default=False),
        "newMediaReady": _bool(data.get("newMediaReady"), default=False),
        "newSavedItems": _bool(data.get("newSavedItems"), default=False),
        "urlChanged": _bool(data.get("urlChanged"), default=False),
        "artifactCreated": _bool(data.get("artifactCreated"), default=False),
    }


def normalize_action(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    action_type = str(data.get("type") or "").strip().lower()
    if action_type not in ACTION_TYPES:
        raise AppError("Unsupported automation action type", code=ErrorCode.INVALID_PAYLOAD)
    action = dict(data)
    action["type"] = action_type
    if "input" in action and not isinstance(action.get("input"), dict):
        raise AppError("Automation action input must be an object", code=ErrorCode.INVALID_PAYLOAD)
    return action


def normalize_output(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    artifact_type = str(data.get("artifactType") or DEFAULT_ARTIFACT_TYPE).strip().lower().lstrip(".")
    if artifact_type == "md":
        artifact_type = "markdown"
    return {
        "saveToProject": _bool(data.get("saveToProject"), default=True),
        "createArtifact": _bool(data.get("createArtifact"), default=True),
        "artifactType": artifact_type or DEFAULT_ARTIFACT_TYPE,
    }


def normalize_policy(value: Any) -> dict[str, Any]:
    data: dict[str, Any] = value if isinstance(value, dict) else {}
    raw_retry = data.get("retry")
    retry: dict[str, Any] = raw_retry if isinstance(raw_retry, dict) else {}
    browser_mode = str(data.get("browserMode") or "read_only").strip().lower()
    if browser_mode not in {"read_only", "write_requires_confirmation", "disabled"}:
        browser_mode = "read_only"
    return {
        "requiresConfirmation": _bool(data.get("requiresConfirmation"), default=False),
        "maxRunsPerDay": _safe_int(data.get("maxRunsPerDay"), default=config.AUTOMATION_MAX_RUNS_PER_DAY),
        "timeoutSeconds": _safe_int(data.get("timeoutSeconds"), default=config.AUTOMATION_RUN_TIMEOUT_SECONDS),
        "allowBrowser": _bool(data.get("allowBrowser"), default=config.AUTOMATION_ALLOW_BROWSER),
        "browserMode": browser_mode,
        "allowNetwork": _bool(data.get("allowNetwork"), default=False),
        "allowPrivateHosts": _bool(data.get("allowPrivateHosts"), default=False),
        "retry": {
            "maxAttempts": min(5, max(1, _safe_int(retry.get("maxAttempts"), default=1))),
            "backoffSeconds": min(3_600, max(0, _safe_int(retry.get("backoffSeconds"), default=0))),
        },
    }


def public_run_outputs(value: Any) -> dict[str, list[str]]:
    data = value if isinstance(value, dict) else {}
    return {
        "artifactIds": _string_list(data.get("artifactIds")),
        "savedItemIds": _string_list(data.get("savedItemIds")),
        "mediaIds": _string_list(data.get("mediaIds")),
        "exportIds": _string_list(data.get("exportIds")),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:120])
    return result


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
