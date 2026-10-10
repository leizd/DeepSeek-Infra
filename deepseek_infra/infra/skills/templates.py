"""Prompt composition helpers for Skill runs."""

from __future__ import annotations

import json
from typing import Any


def format_project_context(project: dict[str, Any] | None) -> str:
    if not project:
        return ""
    documents = project.get("documents") if isinstance(project.get("documents"), list) else []
    saved_items = project.get("savedItems") if isinstance(project.get("savedItems"), list) else []
    skill_runs = project.get("skillRuns") if isinstance(project.get("skillRuns"), list) else []
    lines = [
        "[Project context]",
        f"projectId: {project.get('id')}",
        f"name: {project.get('name')}",
    ]
    if documents:
        lines.append("documents:")
        for item in documents[:12]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('name')} ({item.get('kind')}, fileId={item.get('fileId')})")
    if saved_items:
        lines.append("saved items:")
        for item in saved_items[:8]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('title')} ({item.get('kind')})")
    if skill_runs:
        lines.append("recent Skill runs:")
        for item in skill_runs[:8]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('skillId')} -> {item.get('status')} at {item.get('completedAt') or item.get('startedAt')}")
    return "\n".join(lines)


def skill_system_prompt(skill: dict[str, Any], *, project_context: str = "") -> str:
    parts = [
        str(skill.get("systemPrompt") or "").strip(),
        "[Skill contract]",
        "You are running inside DeepSeek Infra Skill System.",
        "Use only the tools granted to this Skill. Tool calls still pass through Tool Policy.",
        "Honor the input schema, output schema, memory policy, artifact policy, and project binding.",
    ]
    if project_context:
        parts.append(project_context)
    return "\n\n".join(part for part in parts if part)


def skill_user_message(input_data: dict[str, Any]) -> str:
    return "Run this Skill with the following JSON input:\n\n" + json.dumps(input_data, ensure_ascii=False, indent=2)


def offline_skill_content(skill: dict[str, Any], input_data: dict[str, Any], *, project_context: str = "") -> str:
    title = str(input_data.get("title") or input_data.get("topic") or input_data.get("question") or skill.get("name") or "Skill output")
    purpose = str(input_data.get("purpose") or input_data.get("task") or input_data.get("prompt") or "")
    lines = [
        f"# {title}",
        "",
        f"Skill: {skill.get('name')} ({skill.get('skillId')})",
    ]
    if purpose:
        lines.extend(["", "## Request", purpose])
    lines.extend(["", "## Result", "Offline Skill run completed. The registry, schema, permissions, project binding, and artifact policy were exercised."])
    if project_context:
        lines.extend(["", "## Project Context", project_context[:2000]])
    return "\n".join(lines).strip()
