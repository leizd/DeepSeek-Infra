"""Schema helpers for v2.6 Skill definitions and Skill run I/O."""

from __future__ import annotations

import copy
import re
from typing import Any

from deepseek_infra.infra.tool_runtime.tool_policy import all_tool_names

SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{3,80}$")
SUPPORTED_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
SUPPORTED_ARTIFACT_TYPES = {"docx", "pdf", "pptx", "md", "svg"}
MEMORY_SCOPES = {"none", "global", "project"}


class SkillSchemaError(ValueError):
    """Raised when a Skill definition or Skill input/output object is invalid."""


def normalize_skill_id(value: Any) -> str:
    skill_id = str(value or "").strip()
    if not SKILL_ID_RE.fullmatch(skill_id):
        raise SkillSchemaError("skillId must be 3-80 chars and contain only letters, numbers, _, :, or -")
    return skill_id


def validate_skill_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized Skill config or raise SkillSchemaError."""
    if not isinstance(config, dict):
        raise SkillSchemaError("Skill config must be an object")
    data = copy.deepcopy(config)
    required = (
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
    missing = [key for key in required if key not in data]
    if missing:
        raise SkillSchemaError(f"Skill config missing required fields: {', '.join(missing)}")

    data["skillId"] = normalize_skill_id(data.get("skillId"))
    for key, limit in (("name", 120), ("description", 600), ("version", 40), ("systemPrompt", 20_000)):
        value = str(data.get(key) or "").strip()
        if not value:
            raise SkillSchemaError(f"{key} is required")
        data[key] = value[:limit]

    data["inputSchema"] = validate_json_schema(data.get("inputSchema"), label="inputSchema")
    data["outputSchema"] = validate_json_schema(data.get("outputSchema"), label="outputSchema")
    data["allowedTools"] = validate_allowed_tools(data.get("allowedTools"))
    data["memoryPolicy"] = validate_memory_policy(data.get("memoryPolicy"))
    data["artifactPolicy"] = validate_artifact_policy(data.get("artifactPolicy"))
    data["projectBinding"] = validate_project_binding(data.get("projectBinding"))
    data["browserPolicy"] = validate_browser_policy(data.get("browserPolicy"))

    examples = data.get("exampleInputs")
    data["exampleInputs"] = examples if isinstance(examples, list) else []
    data["disabled"] = bool(data.get("disabled"))
    return data


def validate_allowed_tools(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise SkillSchemaError("allowedTools must be a list")
    known_tools = set(all_tool_names())
    out: list[str] = []
    for item in value:
        tool = str(item or "").strip()
        if not tool:
            continue
        if tool not in known_tools and not tool.startswith("mcp__"):
            raise SkillSchemaError(f"allowedTools contains unknown tool: {tool}")
        if tool not in out:
            out.append(tool)
    return out


def validate_memory_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillSchemaError("memoryPolicy must be an object")
    scope = str(value.get("scope") or "none").strip().lower()
    if scope not in MEMORY_SCOPES:
        raise SkillSchemaError("memoryPolicy.scope must be one of none, global, project")
    return {"scope": scope, "read": bool(value.get("read")), "write": bool(value.get("write"))}


def validate_artifact_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillSchemaError("artifactPolicy must be an object")
    types_raw = value.get("types")
    if not isinstance(types_raw, list):
        raise SkillSchemaError("artifactPolicy.types must be a list")
    types: list[str] = []
    for item in types_raw:
        artifact_type = str(item or "").strip().lower().lstrip(".")
        if not artifact_type:
            continue
        if artifact_type not in SUPPORTED_ARTIFACT_TYPES:
            raise SkillSchemaError(f"artifactPolicy.types contains unsupported type: {artifact_type}")
        if artifact_type not in types:
            types.append(artifact_type)
    return {"autoSave": bool(value.get("autoSave")), "types": types}


def validate_project_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillSchemaError("projectBinding must be an object")
    return {"enabled": bool(value.get("enabled"))}


def validate_browser_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillSchemaError("browserPolicy must be an object")
    return {
        "allowClick": bool(value.get("allowClick")),
        "allowType": bool(value.get("allowType")),
        "allowDownload": bool(value.get("allowDownload")),
        "requireConfirmation": bool(value.get("requireConfirmation", True)),
    }


def validate_json_schema(schema: Any, *, label: str) -> dict[str, Any]:
    if schema in (None, {}):
        return {}
    if not isinstance(schema, dict):
        raise SkillSchemaError(f"{label} must be an object")
    _validate_json_schema_node(schema, label)
    return copy.deepcopy(schema)


def _validate_json_schema_node(schema: dict[str, Any], label: str) -> None:
    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, list):
            unknown = [str(item) for item in schema_type if item not in SUPPORTED_SCHEMA_TYPES]
        else:
            unknown = [] if schema_type in SUPPORTED_SCHEMA_TYPES else [str(schema_type)]
        if unknown:
            raise SkillSchemaError(f"{label}.type contains unsupported values: {', '.join(unknown)}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise SkillSchemaError(f"{label}.properties must be an object")
        for key, child in properties.items():
            if not isinstance(key, str) or not isinstance(child, dict):
                raise SkillSchemaError(f"{label}.properties must map strings to schema objects")
            _validate_json_schema_node(child, f"{label}.properties.{key}")
    required = schema.get("required")
    if required is not None and (not isinstance(required, list) or not all(isinstance(item, str) for item in required)):
        raise SkillSchemaError(f"{label}.required must be a list of strings")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise SkillSchemaError(f"{label}.items must be an object")
        _validate_json_schema_node(items, f"{label}.items")


def validate_instance(value: Any, schema: dict[str, Any], *, label: str = "value") -> list[str]:
    """Lightweight JSON-schema validation used for Skill run input/output."""
    if not schema:
        return []
    return _validate_instance_node(value, schema, label)


def _validate_instance_node(value: Any, schema: dict[str, Any], label: str) -> list[str]:
    violations: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, str(item)) for item in allowed):
            violations.append(f"{label} must be {expected}")
            return violations
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        violations.append(f"{label} must be one of {enum}")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str):
        try:
            if re.search(pattern, value) is None:
                violations.append(f"{label} does not match pattern")
        except re.error:
            pass
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for key in schema.get("required") or []:
            if key not in value:
                violations.append(f"{label}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    violations.append(f"{label}.{key} is not allowed")
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                violations.extend(_validate_instance_node(value[key], child_schema, f"{label}.{key}"))
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                violations.extend(_validate_instance_node(item, item_schema, f"{label}[{index}]"))
    return violations


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True
