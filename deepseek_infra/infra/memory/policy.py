"""Memory read/write policy helpers."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import memory as legacy_memory
from deepseek_infra.infra.memory.schema import storage_scope


def assert_memory_safe(content: str) -> None:
    if legacy_memory.is_sensitive_memory(content):
        raise AppError("Memory content contains sensitive data and was not saved", code=ErrorCode.SENSITIVE_CONTENT)


def is_sensitive_memory(content: str) -> bool:
    return legacy_memory.is_sensitive_memory(content)


def readable_scopes(*, project_id: str = "", skill_id: str = "", automation_id: str = "") -> list[str]:
    scopes = ["global"]
    if project_id:
        scopes.append(storage_scope("project", project_id=project_id))
    if skill_id:
        scopes.append(storage_scope("skill", skill_id=skill_id))
    if automation_id:
        scopes.append(storage_scope("automation", automation_id=automation_id))
    return scopes


def skill_can_read_memory(skill: dict[str, Any], *, project_id: str = "") -> bool:
    raw_policy = skill.get("memoryPolicy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    if policy.get("read") is False:
        return False
    scope = str(policy.get("scope") or "global")
    return scope != "project" or bool(project_id)
