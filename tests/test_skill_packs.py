from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.skills import pack, registry


def _skill(skill_id: str, *, tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "skillId": skill_id,
        "name": f"Pack test {skill_id}",
        "description": "Embedded Skill used by pack tests.",
        "version": "1.0.0",
        "systemPrompt": "Return markdown.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": tools or ["search_files"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "pack"}],
    }


def _pack_config(pack_id: str = "pack_test", *, skills: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "packId": pack_id,
        "name": "Pack Test",
        "description": "A test Skill Pack.",
        "version": "1.0.0",
        "author": "local",
        "skills": skills or [_skill("skill_pack_test_a"), _skill("skill_pack_test_b")],
    }


def test_validate_pack_config_rejects_missing_fields() -> None:
    with pytest.raises(pack.PackSchemaError):
        pack.validate_pack_config({"packId": "pack_test"})


def test_validate_pack_config_rejects_duplicate_skill_ids() -> None:
    config = _pack_config(skills=[_skill("skill_dup"), _skill("skill_dup")])
    with pytest.raises(pack.PackSchemaError):
        pack.validate_pack_config(config)


def test_validate_pack_config_accepts_references_and_embedded() -> None:
    config = {
        "packId": "pack_mixed",
        "name": "Mixed",
        "description": "d",
        "version": "1.0.0",
        "skills": [{"skillId": "skill_study_tutor"}, _skill("skill_pack_mixed_a")],
    }
    validated = pack.validate_pack_config(config)
    assert validated["skills"][0] == {"skillId": "skill_study_tutor"}
    assert validated["skills"][1]["skillId"] == "skill_pack_mixed_a"
    assert "systemPrompt" in validated["skills"][1]


def test_pack_allowed_tools_and_risk_labels() -> None:
    config = _pack_config(skills=[_skill("skill_pack_risk", tools=["python_eval", "fetch_url"])])
    validated = pack.validate_pack_config(config)
    assert pack.pack_allowed_tools(validated) == ["python_eval", "fetch_url"]
    summary = pack.tool_permission_summary(validated)
    labels = {tool["tool"]: tool["risk"] for tool in summary[0]["allowedTools"]}
    assert labels["fetch_url"] == "network"
    assert "fetch_url" in pack.high_risk_tools(validated)


def test_builtin_packs_load_and_reference_existing_skills() -> None:
    packs = registry.list_builtin_packs()
    ids = {item["packId"] for item in packs}
    assert {"pack_study", "pack_research", "pack_code", "pack_office"}.issubset(ids)
    for item in packs:
        assert item["builtin"] is True
        assert item["skills"]
    study = registry.get_pack("pack_study")
    assert {entry["skillId"] for entry in study["skills"]} == {"skill_study_tutor", "skill_paper_writer", "skill_document_reader"}


def test_list_packs_includes_builtin_and_custom(tmp_settings: Path) -> None:
    assert any(item["packId"] == "pack_study" for item in registry.list_packs())
    registry.import_pack(_pack_config("pack_custom_list"))
    ids = {item["packId"] for item in registry.list_packs()}
    assert "pack_custom_list" in ids


def test_export_pack_embeds_full_skill_configs(tmp_settings: Path) -> None:
    exported = registry.export_pack("pack_study")
    assert exported["packId"] == "pack_study"
    assert all("systemPrompt" in skill for skill in exported["skills"])
    assert {skill["skillId"] for skill in exported["skills"]} == {
        "skill_study_tutor",
        "skill_paper_writer",
        "skill_document_reader",
    }


def test_import_pack_creates_skills_and_manifest(tmp_settings: Path) -> None:
    summary = registry.import_pack(_pack_config("pack_import_create"))
    assert summary["ok"] is True
    assert summary["installedSkills"] == ["skill_pack_test_a", "skill_pack_test_b"]
    assert summary["conflicts"] == []
    assert registry.get_skill("skill_pack_test_a")["skillId"] == "skill_pack_test_a"
    custom = [item for item in registry.list_packs() if item["packId"] == "pack_import_create"]
    assert custom and custom[0]["builtin"] is False


def test_import_pack_conflict_error_then_overwrite_and_skip(tmp_settings: Path) -> None:
    registry.import_pack(_pack_config("pack_conflict"))
    with pytest.raises(AppError):
        registry.import_pack(_pack_config("pack_conflict"), on_conflict="error")
    skipped = registry.import_pack(_pack_config("pack_conflict"), on_conflict="skip")
    assert skipped["skippedSkills"] == ["skill_pack_test_a", "skill_pack_test_b"]
    overwritten = registry.import_pack(_pack_config("pack_conflict"), overwrite=True)
    assert overwritten["installedSkills"] == ["skill_pack_test_a", "skill_pack_test_b"]


def test_import_pack_rejects_unknown_on_conflict(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        registry.import_pack(_pack_config("pack_bad_conflict"), on_conflict="rename")


def test_import_pack_unresolved_references_reported(tmp_settings: Path) -> None:
    config = {
        "packId": "pack_unresolved",
        "name": "Unresolved",
        "description": "d",
        "version": "1.0.0",
        "skills": [{"skillId": "skill_does_not_exist_anywhere"}],
    }
    summary = registry.import_pack(config)
    assert summary["unresolvedReferences"] == ["skill_does_not_exist_anywhere"]


def test_delete_pack_custom_and_builtin_guard(tmp_settings: Path) -> None:
    registry.import_pack(_pack_config("pack_delete_me"))
    assert registry.delete_pack("pack_delete_me")["ok"] is True
    with pytest.raises(AppError):
        registry.delete_pack("pack_study")


def test_delete_pack_not_found(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        registry.delete_pack("pack_no_such_pack")


def test_enable_pack_for_project_enables_skills(tmp_settings: Path) -> None:
    registry.import_pack(_pack_config("pack_project_bind"))
    project = projects.create_project("Pack Bind Project")
    binding = projects.enable_pack_for_project(project["id"], "pack_project_bind")
    assert "skill_pack_test_a" in binding["enabledSkills"]
    assert "skill_pack_test_b" in binding["enabledSkills"]
    assert "pack_project_bind" in binding["enabledPacks"]


def test_project_skill_binding_preserves_enabled_packs(tmp_settings: Path) -> None:
    project = projects.create_project("Pack Preserve Project")
    projects.enable_pack_for_project(project["id"], "pack_study")
    binding = projects.set_project_skill_binding(
        project["id"], ["skill_study_tutor"], default_skill="skill_study_tutor", enabled_packs=["pack_study", "pack_code"]
    )
    assert binding["enabledPacks"] == ["pack_study", "pack_code"]
    assert binding["enabledSkills"] == ["skill_study_tutor"]
