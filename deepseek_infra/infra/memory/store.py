"""First-class Memory store operations backed by the legacy local store."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import memory as legacy_memory
from deepseek_infra.infra.memory.policy import assert_memory_safe
from deepseek_infra.infra.memory.schema import legacy_category, public_memory, public_source, public_type, storage_scope


def list_memories(*, scope: str = "", project_id: str = "") -> list[dict[str, Any]]:
    storage = storage_scope(scope or "global", project_id=project_id) if scope or project_id else ""
    items = legacy_memory.load_memories()
    if storage:
        items = [item for item in items if legacy_memory.normalize_memory_scope(item.get("scope") or "global") == storage]
    return [public_memory(item) for item in items]


def add_memory(
    content: str,
    *,
    scope: str = "global",
    memory_type: str = "fact",
    project_id: str = "",
    skill_id: str = "",
    automation_id: str = "",
    source: dict[str, Any] | None = None,
    confidence: float = 0.9,
    expires_at: str = "",
    pinned: bool = False,
) -> dict[str, Any]:
    assert_memory_safe(content)
    source_data = public_source(source or {"kind": "manual", "refId": ""})
    storage = storage_scope(scope, project_id=project_id, skill_id=skill_id, automation_id=automation_id)
    item = legacy_memory.upsert_memory(
        content,
        category=legacy_category(memory_type),
        scope=storage,
        source=str(source_data.get("kind") or "manual"),
        pinned=pinned,
    )
    item["type"] = public_type(memory_type)
    item["source"] = source_data
    item["confidence"] = confidence
    if expires_at:
        item["expiresAt"] = expires_at
    legacy_memory.save_memories(_merge_item(item))
    return public_memory(item)


def edit_memory(memory_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    safe_id = str(memory_id or "").strip()
    if not safe_id:
        raise AppError("Memory id is required", code=ErrorCode.INVALID_PAYLOAD)
    with legacy_memory._memory_lock, legacy_memory.memory_file_lock():  # noqa: SLF001
        items = legacy_memory._load_memories_unlocked()  # noqa: SLF001
        for index, item in enumerate(items):
            if str(item.get("id") or item.get("memoryId") or "") != safe_id:
                continue
            updated = dict(item)
            if "content" in updates:
                content = legacy_memory.normalize_memory_text(updates.get("content") or "")
                assert_memory_safe(content)
                updated["content"] = content
            if "type" in updates or "category" in updates:
                memory_type = public_type(str(updates.get("type") or updates.get("category") or "fact"))
                updated["type"] = memory_type
                updated["category"] = legacy_category(memory_type)
            if "scope" in updates:
                updated["scope"] = storage_scope(
                    str(updates.get("scope") or "global"),
                    project_id=str(updates.get("projectId") or ""),
                    skill_id=str(updates.get("skillId") or ""),
                    automation_id=str(updates.get("automationId") or ""),
                )
            if "source" in updates:
                updated["source"] = public_source(updates.get("source"))
            if "confidence" in updates:
                updated["confidence"] = updates.get("confidence")
            if "expiresAt" in updates:
                updated["expiresAt"] = str(updates.get("expiresAt") or "")
            if "pinned" in updates:
                updated["pinned"] = bool(updates.get("pinned"))
            updated["updatedAt"] = utc_now_iso()
            items[index] = updated
            legacy_memory._save_memories_unlocked(items)  # noqa: SLF001
            return public_memory(updated)
    raise AppError("Memory not found", code=ErrorCode.NOT_FOUND, status=404)


def delete_memory(memory_id: str) -> int:
    return legacy_memory.delete_memory_by_id(str(memory_id or ""))


def _merge_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    items = legacy_memory.load_memories()
    merged = []
    replaced = False
    memory_id = str(item.get("id") or "")
    for existing in items:
        if str(existing.get("id") or "") == memory_id:
            merged.append(item)
            replaced = True
        else:
            merged.append(existing)
    if not replaced:
        merged.insert(0, item)
    return merged
