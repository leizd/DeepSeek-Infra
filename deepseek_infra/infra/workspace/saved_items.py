"""Saved Items store for Workspace Core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.workspace.schema import (
    new_id,
    normalize_content,
    normalize_saved_purpose,
    normalize_saved_type,
    normalize_source_ref,
    normalize_tags,
    normalize_title,
    now_ms,
    timestamp_ms_to_iso,
    validate_project_id,
    validate_workspace_id,
    write_json_atomic,
    read_json_file,
)

MAX_SAVED_ITEMS = 1_000
STORE_NAME = "saved-items.json"


def list_saved_items(project_id: str, *, item_type: str = "", tags: list[str] | None = None) -> list[dict[str, Any]]:
    safe_project_id = validate_project_id(project_id)
    items = _load_items(safe_project_id)
    if item_type:
        normalized_type = normalize_saved_type(item_type)
        items = [item for item in items if item.get("type") == normalized_type]
    tag_filter = {tag.lower() for tag in normalize_tags(tags or [])}
    if tag_filter:
        items = [item for item in items if tag_filter.issubset({str(tag).lower() for tag in item.get("tags", [])})]
    return sorted(items, key=lambda item: int(item.get("createdAtMs") or 0), reverse=True)


def create_saved_item(
    project_id: str,
    *,
    item_type: str,
    title: str,
    content: str,
    source_ref: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    purpose: str = "reference",
) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    legacy_projects.require_project(safe_project_id)
    items = _load_items(safe_project_id)
    if len(items) >= MAX_SAVED_ITEMS:
        raise AppError("Too many saved items", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    created_at = now_ms()
    item = {
        "savedId": new_id("save"),
        "projectId": safe_project_id,
        "type": normalize_saved_type(item_type),
        "title": normalize_title(title, default="Saved item"),
        "content": normalize_content(content),
        "sourceRef": normalize_source_ref(source_ref or {}),
        "tags": normalize_tags(tags or []),
        "purpose": normalize_saved_purpose(purpose),
        "createdAt": timestamp_ms_to_iso(created_at),
        "createdAtMs": created_at,
    }
    items.append(item)
    _write_items(safe_project_id, items)
    _touch_project(safe_project_id)
    return item


def update_saved_item(project_id: str, saved_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    safe_saved_id = validate_workspace_id(saved_id, label="saved item id")
    items = _load_items(safe_project_id)
    for index, item in enumerate(items):
        if item.get("savedId") != safe_saved_id:
            continue
        updated = dict(item)
        if "title" in updates:
            updated["title"] = normalize_title(updates.get("title"), default=str(item.get("title") or "Saved item"))
        if "content" in updates:
            updated["content"] = normalize_content(updates.get("content"))
        if "tags" in updates:
            updated["tags"] = normalize_tags(updates.get("tags"))
        if "purpose" in updates:
            updated["purpose"] = normalize_saved_purpose(updates.get("purpose"))
        if "sourceRef" in updates:
            updated["sourceRef"] = normalize_source_ref(updates.get("sourceRef"))
        items[index] = updated
        _write_items(safe_project_id, items)
        _touch_project(safe_project_id)
        return updated
    raise AppError("Saved item not found", code=ErrorCode.NOT_FOUND, status=404)


def delete_saved_item(project_id: str, saved_id: str) -> int:
    safe_project_id = validate_project_id(project_id)
    safe_saved_id = validate_workspace_id(saved_id, label="saved item id")
    items = _load_items(safe_project_id)
    kept = [item for item in items if item.get("savedId") != safe_saved_id]
    if len(kept) == len(items):
        return 0
    _write_items(safe_project_id, kept)
    _touch_project(safe_project_id)
    return 1


def require_saved_item(project_id: str, saved_id: str) -> dict[str, Any]:
    safe_project_id = validate_project_id(project_id)
    safe_saved_id = validate_workspace_id(saved_id, label="saved item id")
    for item in _load_items(safe_project_id):
        if item.get("savedId") == safe_saved_id:
            return item
    raise AppError("Saved item not found", code=ErrorCode.NOT_FOUND, status=404)


def _store_path(project_id: str) -> Path:
    safe_project_id = validate_project_id(project_id)
    return legacy_projects.PROJECTS_DIR / safe_project_id / STORE_NAME


def _load_items(project_id: str) -> list[dict[str, Any]]:
    data = read_json_file(_store_path(project_id), default={"items": []})
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            created_at = int(raw.get("createdAtMs") or 0)
        except (TypeError, ValueError):
            created_at = 0
        saved_id = str(raw.get("savedId") or raw.get("id") or "")
        if not saved_id:
            continue
        items.append(
            {
                "savedId": saved_id,
                "projectId": validate_project_id(str(raw.get("projectId") or project_id)),
                "type": normalize_saved_type(raw.get("type")),
                "title": normalize_title(raw.get("title"), default="Saved item"),
                "content": normalize_content(raw.get("content")),
                "sourceRef": normalize_source_ref(raw.get("sourceRef")),
                "tags": normalize_tags(raw.get("tags")),
                "purpose": normalize_saved_purpose(raw.get("purpose")),
                "createdAt": str(raw.get("createdAt") or timestamp_ms_to_iso(created_at)),
                "createdAtMs": created_at,
            }
        )
    return items[-MAX_SAVED_ITEMS:]


def _write_items(project_id: str, items: list[dict[str, Any]]) -> None:
    write_json_atomic(_store_path(project_id), {"items": items[-MAX_SAVED_ITEMS:]})


def _touch_project(project_id: str) -> None:
    from deepseek_infra.infra.workspace.projects import touch_project

    touch_project(project_id)
