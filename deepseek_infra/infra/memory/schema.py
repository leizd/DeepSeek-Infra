"""Public Memory object schema for v3.0 workspace memory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import memory as legacy_memory
from deepseek_infra.infra.workspace.schema import normalize_source_ref

MEMORY_SCOPES = {"global", "project", "skill", "automation"}
MEMORY_TYPES = {"preference", "fact", "instruction", "summary", "artifact_ref"}
SOURCE_KINDS = {"chat", "saved_item", "project", "automation", "manual"}

LEGACY_TYPE_MAP = {
    "preference": "preference",
    "fact": "fact",
    "project": "fact",
    "todo": "instruction",
    "instruction": "instruction",
    "summary": "summary",
    "artifact_ref": "artifact_ref",
}


@dataclass(frozen=True)
class MemoryRecord:
    memoryId: str
    scope: str
    type: str
    content: str
    source: dict[str, Any]
    confidence: float
    createdAt: str
    updatedAt: str
    expiresAt: str | None = None


def public_memory(item: dict[str, Any]) -> dict[str, Any]:
    """Return a v3.0 Memory object while preserving legacy fields for clients."""
    record = MemoryRecord(
        memoryId=str(item.get("memoryId") or item.get("id") or ""),
        scope=public_scope(str(item.get("scope") or "global")),
        type=public_type(str(item.get("type") or item.get("category") or "fact")),
        content=legacy_memory.normalize_memory_text(item.get("content") or ""),
        source=public_source(item.get("source"), fallback_ref=str(item.get("id") or "")),
        confidence=public_confidence(item.get("confidence")),
        createdAt=str(item.get("createdAt") or utc_now_iso()),
        updatedAt=str(item.get("updatedAt") or item.get("createdAt") or utc_now_iso()),
        expiresAt=str(item.get("expiresAt")) if item.get("expiresAt") else None,
    )
    payload = asdict(record)
    payload.update(
        {
            "id": payload["memoryId"],
            "category": payload["type"],
            "legacyScope": legacy_memory.normalize_memory_scope(item.get("scope") or "global"),
            "pinned": bool(item.get("pinned")),
        }
    )
    return payload


def public_scope(value: str) -> str:
    normalized = legacy_memory.normalize_memory_scope(value)
    if normalized == "global":
        return "global"
    if normalized.startswith("project:"):
        return "project"
    if normalized.startswith("skill:"):
        return "skill"
    if normalized.startswith("automation:"):
        return "automation"
    return "global"


def storage_scope(scope: str, *, project_id: str = "", skill_id: str = "", automation_id: str = "") -> str:
    value = str(scope or "global").strip()
    if value.startswith(("project:", "skill:", "automation:")):
        return legacy_memory.normalize_memory_scope(value)
    if value == "project" and project_id:
        return legacy_memory.normalize_memory_scope(f"project:{project_id}")
    if value == "skill" and skill_id:
        return legacy_memory.normalize_memory_scope(f"skill:{skill_id}")
    if value == "automation" and automation_id:
        return legacy_memory.normalize_memory_scope(f"automation:{automation_id}")
    return legacy_memory.normalize_memory_scope(value)


def public_type(value: str) -> str:
    return LEGACY_TYPE_MAP.get(str(value or "").strip().lower(), "fact")


def legacy_category(value: str) -> str:
    public = public_type(value)
    if public in {"instruction", "summary", "artifact_ref"}:
        return "fact"
    return public


def public_source(value: Any, *, fallback_ref: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        source = normalize_source_ref(value)
    else:
        source = {"kind": str(value or "manual"), "refId": fallback_ref}
    kind = str(source.get("kind") or source.get("type") or "manual").strip().lower()
    if kind not in SOURCE_KINDS:
        kind = "manual"
    ref_id = str(source.get("refId") or source.get("id") or source.get("savedItemId") or source.get("messageId") or fallback_ref)
    result = {"kind": kind, "refId": ref_id}
    for key, item in source.items():
        if key not in result and key not in {"type", "id"}:
            result[key] = item
    return result


def public_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.9
    return max(0.0, min(1.0, confidence))
