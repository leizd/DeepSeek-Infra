"""Memory search helpers for workspace, Skill, and Automation consumers."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.data import memory as legacy_memory
from deepseek_infra.infra.memory.policy import readable_scopes
from deepseek_infra.infra.memory.schema import public_memory


def search_memories(
    query: str,
    *,
    project_id: str = "",
    skill_id: str = "",
    automation_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    scopes = readable_scopes(project_id=project_id, skill_id=skill_id, automation_id=automation_id)
    memories = legacy_memory.retrieve_memories(query, scopes=scopes)
    result = [public_memory(item) for item in memories]
    return result[: max(0, int(limit))] if limit is not None else result


def memory_context_for_skill(skill: dict[str, Any], query: str, *, project_id: str = "") -> str:
    raw_policy = skill.get("memoryPolicy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    if not policy.get("read"):
        return ""
    scopes = ["global"]
    if str(policy.get("scope") or "") == "project" and project_id:
        scopes.append(f"project:{project_id}")
    return legacy_memory.format_memory_context(legacy_memory.retrieve_memories(query, scopes=scopes))
