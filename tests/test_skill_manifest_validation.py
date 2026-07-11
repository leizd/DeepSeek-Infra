from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.infra.skills import pack, schema


def valid_skill(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "skillId": "skill_valid",
        "name": "Valid",
        "description": "Valid skill",
        "version": "1.0.0",
        "systemPrompt": "Help",
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        "allowedTools": [],
        "memoryPolicy": {"scope": "none", "read": False, "write": False},
        "artifactPolicy": {"autoSave": False, "types": []},
        "projectBinding": {"enabled": False},
    }
    value.update(overrides)
    return value


def test_skill_manifest_missing_fields_empty_values_and_id_validation() -> None:
    with pytest.raises(schema.SkillSchemaError, match="must be an object"):
        schema.validate_skill_config("bad")  # type: ignore[arg-type]
    with pytest.raises(schema.SkillSchemaError, match="missing required fields"):
        schema.validate_skill_config({"skillId": "skill"})
    for key in ("name", "description", "version", "systemPrompt"):
        with pytest.raises(schema.SkillSchemaError, match=f"{key} is required"):
            schema.validate_skill_config(valid_skill(**{key: ""}))
    with pytest.raises(schema.SkillSchemaError, match="skillId"):
        schema.normalize_skill_id("../bad")
    normalized = schema.validate_skill_config(valid_skill(exampleInputs="bad", disabled=1, browserPolicy=None))
    assert normalized["exampleInputs"] == []
    assert normalized["disabled"] is True
    assert normalized["browserPolicy"] == {}


def test_allowed_tools_memory_artifact_project_and_browser_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "all_tool_names", lambda: ["known"])
    assert schema.validate_allowed_tools(["known", "known", "", "mcp__server__tool"]) == ["known", "mcp__server__tool"]
    with pytest.raises(schema.SkillSchemaError, match="must be a list"):
        schema.validate_allowed_tools("known")
    with pytest.raises(schema.SkillSchemaError, match="unknown tool"):
        schema.validate_allowed_tools(["unknown"])
    with pytest.raises(schema.SkillSchemaError, match="memoryPolicy must be"):
        schema.validate_memory_policy([])
    with pytest.raises(schema.SkillSchemaError, match="scope"):
        schema.validate_memory_policy({"scope": "workspace"})
    assert schema.validate_memory_policy({"scope": "PROJECT", "read": 1}) == {"scope": "project", "read": True, "write": False}
    with pytest.raises(schema.SkillSchemaError, match="artifactPolicy must be"):
        schema.validate_artifact_policy([])
    with pytest.raises(schema.SkillSchemaError, match="types must be a list"):
        schema.validate_artifact_policy({})
    with pytest.raises(schema.SkillSchemaError, match="unsupported type"):
        schema.validate_artifact_policy({"types": ["exe"]})
    assert schema.validate_artifact_policy({"autoSave": True, "types": [".PDF", "", "pdf"]}) == {"autoSave": True, "types": ["pdf"]}
    with pytest.raises(schema.SkillSchemaError, match="projectBinding"):
        schema.validate_project_binding([])
    with pytest.raises(schema.SkillSchemaError, match="browserPolicy"):
        schema.validate_browser_policy([])
    assert schema.validate_browser_policy({"allowClick": 1})["requireConfirmation"] is True


@pytest.mark.parametrize(
    "value",
    [
        {"type": "unsupported"},
        {"type": ["object", "bad"]},
        {"properties": []},
        {"properties": {1: {}}},
        {"properties": {"x": []}},
        {"required": "x"},
        {"required": [1]},
        {"items": []},
    ],
)
def test_json_schema_rejects_invalid_nodes(value: dict[str, Any]) -> None:
    with pytest.raises(schema.SkillSchemaError):
        schema.validate_json_schema(value, label="schema")


def test_json_schema_and_instance_validation_all_types_patterns_and_arrays() -> None:
    current = {
        "type": "object",
        "required": ["name"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "pattern": "^[A-Z]", "enum": ["Alice", "Bob"]},
            "count": {"type": "integer"},
            "score": {"type": "number"},
            "enabled": {"type": "boolean"},
            "none": {"type": "null"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert schema.validate_json_schema(None, label="schema") == {}
    with pytest.raises(schema.SkillSchemaError, match="must be an object"):
        schema.validate_json_schema([], label="schema")
    violations = schema.validate_instance(
        {"name": "carol", "count": True, "score": False, "enabled": 1, "none": "x", "tags": [1], "extra": 1},
        current,
        label="input",
    )
    assert any("does not match pattern" in item for item in violations)
    assert any("must be integer" in item for item in violations)
    assert any("extra is not allowed" in item for item in violations)
    assert schema.validate_instance({}, {}) == []
    assert schema.validate_instance({}, current) == ["value.name is required"]
    assert schema.validate_instance("x", {"type": ["string", "null"]}) == []
    assert schema.validate_instance("x", {"type": "string", "pattern": "["}) == []
    assert schema._matches_type({}, "object") is True
    assert schema._matches_type([], "array") is True
    assert schema._matches_type(1, "number") is True
    assert schema._matches_type(False, "boolean") is True
    assert schema._matches_type(None, "null") is True
    assert schema._matches_type("x", "unknown") is True


def valid_pack(skills: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "packId": "pack_valid",
        "name": "Pack",
        "description": "Description",
        "version": "1.0.0",
        "skills": skills if skills is not None else ["skill_valid"],
    }
    value.update(overrides)
    return value


def test_pack_manifest_missing_empty_duplicate_and_invalid_entries() -> None:
    with pytest.raises(pack.PackSchemaError, match="must be an object"):
        pack.validate_pack_config("bad")  # type: ignore[arg-type]
    with pytest.raises(pack.PackSchemaError, match="missing required"):
        pack.validate_pack_config({"packId": "pack"})
    for key in ("name", "description", "version"):
        current = valid_pack()
        current[key] = ""
        with pytest.raises(pack.PackSchemaError, match=f"{key} is required"):
            pack.validate_pack_config(current)
    with pytest.raises(pack.PackSchemaError, match="non-empty"):
        pack.validate_pack_config(valid_pack(skills=[]))
    with pytest.raises(pack.PackSchemaError, match="duplicate skillId"):
        pack.validate_pack_config(valid_pack(["skill_same", {"skillId": "skill_same"}]))
    for entry in ("", "x", 3, {}, {"skillId": ""}):
        with pytest.raises(pack.PackSchemaError):
            pack._normalize_pack_skill_entry(entry, 0)
    with pytest.raises(pack.PackSchemaError, match="name is required"):
        pack._normalize_pack_skill_entry(valid_skill(name=""), 0)
    with pytest.raises(pack.PackSchemaError, match="packId"):
        pack.normalize_pack_id("bad/path")


def test_pack_helpers_permissions_and_risk_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "all_tool_names", lambda: ["network", "confirm"])
    embedded = valid_skill(skillId="skill_embedded", allowedTools=["network", "confirm", "mcp__x"])
    current = pack.validate_pack_config(valid_pack(["skill_ref", embedded]))
    assert pack.pack_skill_ids(current) == ["skill_ref", "skill_embedded"]
    assert pack.embedded_skill_configs(current)[0]["skillId"] == "skill_embedded"
    assert pack.pack_allowed_tools(current) == ["network", "confirm", "mcp__x"]
    assert pack.is_reference_entry("bad") is False

    metadata = {
        "network": SimpleNamespace(requires_confirm=False, network=True, filesystem=False, sensitive_sink=False, risk="medium"),
        "confirm": SimpleNamespace(requires_confirm=True, network=False, filesystem=False, sensitive_sink=False, risk="high"),
    }
    monkeypatch.setattr(pack, "tool_metadata", lambda name: metadata.get(name))
    assert pack.tool_risk_label("missing") == "unknown"
    assert pack.tool_risk_label("mcp__tool") == "mcp"
    assert pack.tool_risk_label("network") == "network"
    assert pack.tool_risk_label("confirm") == "requires approval"
    summary = pack.tool_permission_summary(current)
    assert summary[0]["embedded"] is False
    assert summary[1]["allowedTools"][1]["requiresApproval"] is True
    assert pack.high_risk_tools(current) == ["confirm"]
