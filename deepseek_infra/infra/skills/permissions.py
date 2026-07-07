"""Permission helpers that bind Skill allowedTools to the Tool Policy Engine."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.skills.schema import validate_allowed_tools
from deepseek_infra.infra.tool_runtime.tool_policy import PolicyDecision, ToolPolicy


def skill_allowed_tools(skill: dict[str, Any]) -> list[str]:
    tools = validate_allowed_tools(skill.get("allowedTools") if isinstance(skill, dict) else [])
    policy = skill.get("browserPolicy") if isinstance(skill.get("browserPolicy"), dict) else {}
    if not policy:
        return tools
    filtered = []
    for tool in tools:
        if tool == "browser_click" and not bool(policy.get("allowClick")):
            continue
        if tool == "browser_type_text" and not bool(policy.get("allowType")):
            continue
        if tool == "browser_select" and not bool(policy.get("allowType")):
            continue
        if tool == "browser_download" and not bool(policy.get("allowDownload")):
            continue
        filtered.append(tool)
    return filtered


def build_skill_tool_policy(
    skill: dict[str, Any],
    *,
    project_id: str = "",
    approvals: set[str] | None = None,
    enforce_schema: bool | None = None,
) -> ToolPolicy:
    scope = f"project:{project_id}" if project_id else f"skill:{skill.get('skillId') or 'unknown'}"
    return ToolPolicy(
        capability="full",
        allowed_tools=skill_allowed_tools(skill),
        approvals=approvals,
        enforce_schema=enforce_schema,
        scope=scope,
    )


def evaluate_skill_tool(skill: dict[str, Any], tool_name: str, arguments: dict[str, Any] | None = None) -> PolicyDecision:
    policy = build_skill_tool_policy(skill, enforce_schema=False)
    return policy.evaluate(tool_name, arguments or {})
