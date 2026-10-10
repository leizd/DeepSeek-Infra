"""Project 2.0 facade for Workspace Core."""

from __future__ import annotations

import time
from typing import Any

from deepseek_infra.infra.data import memory as memory_store
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.workspace.schema import (
    normalize_content,
    normalize_description,
    normalize_source_ref,
    normalize_tags,
    normalize_title,
    timestamp_ms_to_iso,
    validate_project_id,
)

MAX_PROJECT_CONVERSATIONS = 200
MAX_CONVERSATION_MESSAGES = 400


def create_project(name: str, *, description: str = "") -> dict[str, Any]:
    project = legacy_projects.create_project(name)
    if description:
        stored = legacy_projects.require_project(str(project["id"]))
        stored["description"] = normalize_description(description)
        stored["updatedAt"] = int(time.time() * 1000)
        legacy_projects.write_project(stored)
        project = stored
    return public_project(legacy_projects.require_project(str(project["id"])), include_children=True)


def rename_project(project_id: str, name: str, *, description: str | None = None) -> dict[str, Any]:
    project = legacy_projects.require_project(validate_project_id(project_id))
    if str(name or "").strip():
        project["name"] = legacy_projects.normalize_project_name(name)
    if description is not None:
        project["description"] = normalize_description(description)
    project["updatedAt"] = int(time.time() * 1000)
    legacy_projects.write_project(project)
    return public_project(project, include_children=True)


def delete_project(project_id: str) -> int:
    return legacy_projects.delete_project(validate_project_id(project_id))


def list_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for item in legacy_projects.list_projects():
        project_id = str(item.get("id") or "")
        try:
            project = legacy_projects.require_project(project_id)
        except Exception:
            continue
        projects.append(public_project(project, include_children=False))
    return projects


def get_project(project_id: str) -> dict[str, Any]:
    return public_project(legacy_projects.require_project(validate_project_id(project_id)), include_children=True)


def list_project_conversations(project_id: str) -> list[dict[str, Any]]:
    project = legacy_projects.require_project(validate_project_id(project_id))
    return normalize_conversations(project.get("conversations"))


def upsert_project_conversation(project_id: str, conversation: dict[str, Any]) -> dict[str, Any]:
    project = legacy_projects.require_project(validate_project_id(project_id))
    normalized = normalize_conversation(conversation)
    conversation_id = str(normalized["conversationId"])
    conversations = [item for item in normalize_conversations(project.get("conversations")) if item.get("conversationId") != conversation_id]
    conversations.append(normalized)
    project["conversations"] = conversations[-MAX_PROJECT_CONVERSATIONS:]
    project["updatedAt"] = int(time.time() * 1000)
    legacy_projects.write_project(project)
    return normalized


def public_project(project: dict[str, Any], *, include_children: bool = False) -> dict[str, Any]:
    project_id = str(project.get("id") or "")
    documents = legacy_projects.normalize_documents(project.get("documents"))
    conversations = normalize_conversations(project.get("conversations"))
    saved_items = _safe_saved_items(project_id) if include_children else []
    artifacts = _safe_artifacts(project_id) if include_children else []
    memories = _project_memories(project_id) if include_children else []
    payload: dict[str, Any] = {
        "id": project_id,
        "projectId": project_id,
        "name": legacy_projects.normalize_project_name(project.get("name")),
        "description": normalize_description(project.get("description")),
        "createdAt": timestamp_ms_to_iso(project.get("createdAt")),
        "updatedAt": timestamp_ms_to_iso(project.get("updatedAt")),
        "createdAtMs": int(project.get("createdAt") or 0),
        "updatedAtMs": int(project.get("updatedAt") or 0),
        "stats": {
            "files": len(documents),
            "savedItems": len(_safe_saved_items(project_id)),
            "artifacts": len(_safe_artifacts(project_id)),
            "conversations": len(conversations),
            "memories": len(_project_memories(project_id)),
        },
    }
    if include_children:
        payload.update(
            {
                "files": documents,
                "documents": documents,
                "savedItems": saved_items,
                "artifacts": artifacts,
                "conversations": conversations,
                "memories": memories,
            }
        )
    return payload


def normalize_conversations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    conversations: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            conversations.append(normalize_conversation(item))
    conversations.sort(key=lambda item: int(item.get("updatedAtMs") or item.get("createdAtMs") or 0), reverse=True)
    return conversations[:MAX_PROJECT_CONVERSATIONS]


def normalize_conversation(value: dict[str, Any]) -> dict[str, Any]:
    raw_id = str(value.get("conversationId") or value.get("id") or "").strip()
    conversation_id = raw_id[:80] or f"conv-{int(time.time() * 1000)}"
    created_at = _coerce_timestamp_ms(value.get("createdAtMs") or value.get("createdAt"), default=int(time.time() * 1000))
    updated_at = _coerce_timestamp_ms(value.get("updatedAtMs") or value.get("updatedAt"), default=created_at)
    messages = value.get("messages")
    normalized_messages = []
    if isinstance(messages, list):
        for message in messages[:MAX_CONVERSATION_MESSAGES]:
            if not isinstance(message, dict):
                continue
            normalized_messages.append(
                {
                    "id": str(message.get("id") or "")[:80],
                    "role": str(message.get("role") or "")[:40],
                    "content": normalize_content(message.get("content")),
                    "reasoning": normalize_content(message.get("reasoning")),
                    "sourceRef": normalize_source_ref(message.get("sourceRef")),
                    "createdAt": str(message.get("createdAt") or "")[:80],
                }
            )
    return {
        "id": conversation_id,
        "conversationId": conversation_id,
        "title": normalize_title(value.get("title"), default="Conversation"),
        "tags": normalize_tags(value.get("tags")),
        "sourceRef": normalize_source_ref(value.get("sourceRef")),
        "messageCount": len(normalized_messages),
        "messages": normalized_messages,
        "createdAt": timestamp_ms_to_iso(created_at),
        "updatedAt": timestamp_ms_to_iso(updated_at),
        "createdAtMs": created_at,
        "updatedAtMs": updated_at,
    }


def touch_project(project_id: str) -> None:
    project = legacy_projects.require_project(validate_project_id(project_id))
    project["updatedAt"] = int(time.time() * 1000)
    legacy_projects.write_project(project)


def _coerce_timestamp_ms(value: Any, *, default: int) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_saved_items(project_id: str) -> list[dict[str, Any]]:
    try:
        from deepseek_infra.infra.workspace.saved_items import list_saved_items

        return list_saved_items(project_id)
    except Exception:
        return []


def _safe_artifacts(project_id: str) -> list[dict[str, Any]]:
    try:
        from deepseek_infra.infra.workspace.artifacts import list_artifacts

        return list_artifacts(project_id)
    except Exception:
        return []


def _project_memories(project_id: str) -> list[dict[str, Any]]:
    scope = f"project:{project_id}"
    try:
        return [item for item in memory_store.load_memories() if str(item.get("scope") or "") == scope]
    except Exception:
        return []
