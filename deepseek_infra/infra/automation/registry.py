"""Persistent automation definition registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.config import AUTOMATION_DIR
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation import schema
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.workspace.schema import read_json_file, validate_project_id, write_json_atomic

STORE_NAME = "automations.json"
MAX_AUTOMATIONS = 500


def list_automations(*, project_id: str = "", include_disabled: bool = True) -> list[dict[str, Any]]:
    safe_project_id = validate_project_id(project_id) if project_id else ""
    items = _load_automations()
    if safe_project_id:
        items = [item for item in items if item.get("projectId") == safe_project_id]
    if not include_disabled:
        items = [item for item in items if item.get("enabled")]
    return sorted(items, key=lambda item: int(item.get("updatedAtMs") or item.get("createdAtMs") or 0), reverse=True)


def get_automation(automation_id: str) -> dict[str, Any]:
    safe_id = schema.validate_automation_id(automation_id)
    for automation in _load_automations():
        if automation.get("automationId") == safe_id:
            return automation
    raise AppError("Automation not found", code=ErrorCode.NOT_FOUND, status=404)


def create_automation(payload: dict[str, Any]) -> dict[str, Any]:
    automation = schema.normalize_automation(payload)
    if automation["projectId"]:
        legacy_projects.require_project(str(automation["projectId"]))
    items = _load_automations()
    if len(items) >= MAX_AUTOMATIONS:
        raise AppError("Too many automations", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    if any(item.get("automationId") == automation["automationId"] for item in items):
        raise AppError("Automation already exists", code=ErrorCode.INVALID_PAYLOAD, status=409)
    items.append(automation)
    _write_automations(items)
    _touch_project(str(automation.get("projectId") or ""))
    return automation


def update_automation(automation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    safe_id = schema.validate_automation_id(automation_id)
    items = _load_automations()
    for index, current in enumerate(items):
        if current.get("automationId") != safe_id:
            continue
        merged = {**current, **(patch if isinstance(patch, dict) else {}), "automationId": safe_id}
        updated = schema.normalize_automation(merged, existing=current)
        if updated["projectId"]:
            legacy_projects.require_project(str(updated["projectId"]))
        items[index] = updated
        _write_automations(items)
        _touch_project(str(updated.get("projectId") or ""))
        return updated
    raise AppError("Automation not found", code=ErrorCode.NOT_FOUND, status=404)


def set_automation_enabled(automation_id: str, enabled: bool) -> dict[str, Any]:
    return update_automation(automation_id, {"enabled": bool(enabled)})


def delete_automation(automation_id: str) -> int:
    safe_id = schema.validate_automation_id(automation_id)
    items = _load_automations()
    kept = [item for item in items if item.get("automationId") != safe_id]
    if len(kept) == len(items):
        return 0
    removed = next((item for item in items if item.get("automationId") == safe_id), {})
    _write_automations(kept)
    _touch_project(str(removed.get("projectId") or ""))
    return 1


def list_templates() -> list[dict[str, Any]]:
    return [dict(item) for item in BUILTIN_TEMPLATES]


def get_template(template_id: str) -> dict[str, Any]:
    for template in BUILTIN_TEMPLATES:
        if template["templateId"] == template_id:
            return dict(template)
    raise AppError("Automation template not found", code=ErrorCode.NOT_FOUND, status=404)


def create_from_template(template_id: str, *, project_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    template = get_template(template_id)
    automation_payload = dict(template["automation"])
    automation_payload["projectId"] = project_id
    patch = overrides if isinstance(overrides, dict) else {}
    automation_payload.update(patch)
    return create_automation(automation_payload)


def store_path() -> Path:
    return AUTOMATION_DIR / STORE_NAME


def _load_automations() -> list[dict[str, Any]]:
    data = read_json_file(store_path(), default={"automations": []})
    raw_items = data.get("automations")
    if not isinstance(raw_items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            result.append(schema.normalize_automation(item, existing=item, touch=False))
        except AppError:
            continue
    return result[-MAX_AUTOMATIONS:]


def _write_automations(items: list[dict[str, Any]]) -> None:
    write_json_atomic(store_path(), {"automations": items[-MAX_AUTOMATIONS:]})


def _touch_project(project_id: str) -> None:
    if not project_id:
        return
    try:
        from deepseek_infra.infra.workspace.projects import touch_project

        touch_project(project_id)
    except Exception:
        return


BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "templateId": "daily_project_summary",
        "name": "Daily Project Summary",
        "description": "Create a local markdown summary of project changes.",
        "automation": {
            "name": "Daily Project Summary",
            "description": "Summarize project files, saved items, artifacts, and media.",
            "trigger": {"type": "schedule", "cron": "0 22 * * *"},
            "condition": {"type": "project_changed", "sinceLastRun": True},
            "action": {"type": "project_summary"},
            "output": {"saveToProject": True, "createArtifact": True, "artifactType": "markdown"},
            "policy": {"maxRunsPerDay": 3, "allowBrowser": False, "allowNetwork": False},
        },
    },
    {
        "templateId": "weekly_project_export",
        "name": "Weekly Project Export",
        "description": "Export the full project bundle once a week.",
        "automation": {
            "name": "Weekly Project Export",
            "trigger": {"type": "schedule", "cron": "0 18 * * 5"},
            "condition": {"type": "project_changed", "sinceLastRun": True},
            "action": {"type": "export_project", "format": "zip"},
            "policy": {"maxRunsPerDay": 1, "allowBrowser": False, "allowNetwork": False},
        },
    },
    {
        "templateId": "webpage_change_watch",
        "name": "Webpage Change Watch",
        "description": "Read a page, diff against the last snapshot, and save a report when it changes.",
        "automation": {
            "name": "Webpage Change Watch",
            "trigger": {"type": "interval", "intervalSeconds": 3600},
            "condition": {"type": "url_changed", "urlChanged": True},
            "action": {"type": "browser_check", "url": "https://example.com", "selector": "body"},
            "policy": {"maxRunsPerDay": 12, "allowBrowser": True, "browserMode": "read_only", "allowNetwork": True},
        },
    },
    {
        "templateId": "media_digest",
        "name": "Media Digest",
        "description": "Run media_to_report when new media becomes ready.",
        "automation": {
            "name": "Media Digest",
            "trigger": {"type": "event", "event": "media.ready"},
            "condition": {"type": "media_ready", "newMediaReady": True},
            "action": {"type": "media_process", "skillId": "media_to_report", "input": {"task": "Write a cited media digest."}},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        },
    },
    {
        "templateId": "saved_items_digest",
        "name": "Saved Items Digest",
        "description": "Create a digest when new saved items are added.",
        "automation": {
            "name": "Saved Items Digest",
            "trigger": {"type": "event", "event": "saved_item.created"},
            "condition": {"type": "new_saved_items", "newSavedItems": True},
            "action": {"type": "project_summary", "section": "saved_items"},
            "policy": {"maxRunsPerDay": 6, "allowBrowser": False, "allowNetwork": False},
        },
    },
    {
        "templateId": "artifact_backup",
        "name": "Artifact Backup",
        "description": "Export project artifacts when an artifact is created.",
        "automation": {
            "name": "Artifact Backup",
            "trigger": {"type": "event", "event": "artifact.created"},
            "condition": {"type": "artifact_created", "artifactCreated": True},
            "action": {"type": "export_project", "format": "zip"},
            "policy": {"maxRunsPerDay": 10, "allowBrowser": False, "allowNetwork": False},
        },
    },
]
