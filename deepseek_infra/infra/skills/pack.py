"""Skill Pack schema and helpers for Skill Template Library.

A Skill Pack bundles several complete Skill units (prompt + tools + schema +
policy + project binding) into a portable ``.skillpack.json`` so they can be
imported, exported, installed, and bound to projects as a set.

Pack manifest shape::

    {
      "packId": "pack_study",
      "name": "Study Pack",
      "description": "Skills for study, writing and reading.",
      "version": "1.0.0",
      "author": "builtin",
      "skills": [
        {"skillId": "skill_study_tutor"},
        {"skillId": "skill_paper_writer", "name": "...", ...full Skill config}
      ]
    }

A pack ``skills`` entry is either a *reference* (only ``skillId``) that resolves
against existing built-in / custom Skills, or an *embedded* full Skill config.
Importing a pack with embedded configs creates those Skills locally; references
are resolved at install / export time. This keeps built-in template packs small
while letting exported packs stay self-contained.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from deepseek_infra.infra.skills.schema import SkillSchemaError, validate_skill_config
from deepseek_infra.infra.tool_runtime.tool_policy import RISK_ORDER, tool_metadata

PACK_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{3,80}$")
PACK_SKILL_REQUIRED_FIELDS = (
    "skillId",
    "name",
    "description",
    "version",
    "systemPrompt",
    "inputSchema",
    "outputSchema",
    "allowedTools",
    "memoryPolicy",
    "artifactPolicy",
    "projectBinding",
)


class PackSchemaError(ValueError):
    """Raised when a Skill Pack manifest is invalid."""


def normalize_pack_id(value: Any) -> str:
    pack_id = str(value or "").strip()
    if not PACK_ID_RE.fullmatch(pack_id):
        raise PackSchemaError("packId must be 3-80 chars and contain only letters, numbers, _, :, or -")
    return pack_id


def is_reference_entry(entry: Any) -> bool:
    """True when a pack skill entry only carries a skillId (no full config)."""
    if not isinstance(entry, dict):
        return False
    return not any(field in entry for field in PACK_SKILL_REQUIRED_FIELDS if field != "skillId")


def validate_pack_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized Skill Pack manifest or raise PackSchemaError.

    Embedded Skill configs are validated through ``validate_skill_config`` so
    import-time schema / tool checks happen before any file is written.
    """
    if not isinstance(config, dict):
        raise PackSchemaError("Skill Pack config must be an object")
    data = copy.deepcopy(config)
    required = ("packId", "name", "description", "version", "skills")
    missing = [key for key in required if key not in data]
    if missing:
        raise PackSchemaError(f"Skill Pack config missing required fields: {', '.join(missing)}")

    data["packId"] = normalize_pack_id(data.get("packId"))
    for key, limit in (("name", 160), ("description", 1200), ("version", 40)):
        value = str(data.get(key) or "").strip()
        if not value:
            raise PackSchemaError(f"{key} is required")
        data[key] = value[:limit]
    data["author"] = str(data.get("author") or "local").strip()[:120]

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise PackSchemaError("skills must be a non-empty list")

    seen: set[str] = set()
    normalized_skills: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_skills):
        normalized = _normalize_pack_skill_entry(entry, index)
        skill_id = normalized["skillId"]
        if skill_id in seen:
            raise PackSchemaError(f"duplicate skillId in pack: {skill_id}")
        seen.add(skill_id)
        normalized_skills.append(normalized)
    data["skills"] = normalized_skills
    return data


def _normalize_pack_skill_entry(entry: Any, index: int) -> dict[str, Any]:
    if isinstance(entry, str):
        skill_id = str(entry or "").strip()
        if not skill_id:
            raise PackSchemaError(f"skills[{index}] skillId is required")
        try:
            from deepseek_infra.infra.skills.schema import normalize_skill_id

            skill_id = normalize_skill_id(skill_id)
        except SkillSchemaError as exc:
            raise PackSchemaError(f"skills[{index}] {exc}") from exc
        return {"skillId": skill_id}
    if not isinstance(entry, dict):
        raise PackSchemaError(f"skills[{index}] must be a skillId string or a Skill config object")
    skill_id = str(entry.get("skillId") or "").strip()
    if not skill_id:
        raise PackSchemaError(f"skills[{index}] skillId is required")
    if is_reference_entry(entry):
        try:
            from deepseek_infra.infra.skills.schema import normalize_skill_id

            skill_id = normalize_skill_id(skill_id)
        except SkillSchemaError as exc:
            raise PackSchemaError(f"skills[{index}] {exc}") from exc
        return {"skillId": skill_id}
    try:
        validated = validate_skill_config(entry)
    except SkillSchemaError as exc:
        raise PackSchemaError(f"skills[{index}] {exc}") from exc
    return validated


def pack_skill_ids(pack: dict[str, Any]) -> list[str]:
    return [str(entry.get("skillId") or "") for entry in (pack.get("skills") or []) if isinstance(entry, dict)]


def embedded_skill_configs(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the full embedded Skill configs (excluding bare references)."""
    out: list[dict[str, Any]] = []
    for entry in pack.get("skills") or []:
        if isinstance(entry, dict) and not is_reference_entry(entry):
            out.append(entry)
    return out


def pack_allowed_tools(pack: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    for skill in embedded_skill_configs(pack):
        for tool in skill.get("allowedTools") or []:
            tool = str(tool or "").strip()
            if tool and tool not in tools:
                tools.append(tool)
    return tools


def tool_risk_label(tool_name: str) -> str:
    meta = tool_metadata(tool_name)
    if meta is None:
        return "unknown" if not tool_name.startswith("mcp__") else "mcp"
    if meta.requires_confirm:
        return "requires approval"
    if meta.network:
        return "network"
    if meta.filesystem:
        return "filesystem"
    if meta.sensitive_sink:
        return "sensitive"
    risk = str(meta.risk or "low")
    return risk if risk in RISK_ORDER else risk


def tool_permission_summary(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-skill allowedTools with risk labels for the import safety diff."""
    summary: list[dict[str, Any]] = []
    for entry in pack.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        skill_id = str(entry.get("skillId") or "")
        tools = entry.get("allowedTools") if not is_reference_entry(entry) else []
        tools = [str(tool or "").strip() for tool in (tools or []) if str(tool or "").strip()]
        summary.append(
            {
                "skillId": skill_id,
                "embedded": not is_reference_entry(entry),
                "allowedTools": [
                    {"tool": tool, "risk": tool_risk_label(tool), "requiresApproval": tool_risk_label(tool) == "requires approval"}
                    for tool in tools
                ],
            }
        )
    return summary


def high_risk_tools(pack: dict[str, Any]) -> list[str]:
    """Tools in the pack that require explicit approval or are high/critical risk."""
    flagged: list[str] = []
    for skill in embedded_skill_configs(pack):
        for tool in skill.get("allowedTools") or []:
            tool = str(tool or "").strip()
            if not tool or tool in flagged:
                continue
            meta = tool_metadata(tool)
            if meta is None:
                continue
            if meta.requires_confirm or RISK_ORDER.get(str(meta.risk), 0) >= RISK_ORDER["high"]:
                flagged.append(tool)
    return flagged
