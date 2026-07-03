from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.observability import observability
from deepseek_infra.infra.skills import eval as skill_eval
from deepseek_infra.infra.skills import evidence, permissions, registry
from deepseek_infra.infra.skills import versioning as skill_versioning
from deepseek_infra.infra.skills.runner import run_skill


def _custom_skill() -> dict[str, object]:
    return {
        "skillId": "skill_unit_custom",
        "name": "Unit Custom Skill",
        "description": "Used by registry unit tests.",
        "version": "1.0.0",
        "systemPrompt": "Return markdown.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": ["search_files"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "unit"}],
    }


def test_builtin_skill_pack_loads() -> None:
    skills = registry.list_builtin_skills()
    ids = {item["skillId"] for item in skills}

    assert {
        "skill_document_reader",
        "skill_research_brief",
        "skill_paper_writer",
        "skill_ppt_generator",
        "skill_code_review",
        "skill_study_tutor",
    } <= ids


def test_custom_skill_crud_disable_and_export(tmp_settings: Path) -> None:
    created = registry.create_custom_skill(_custom_skill())
    exported = registry.export_skill_config(created["skillId"])
    disabled = registry.set_skill_disabled(created["skillId"], True)

    assert created["builtin"] is False
    assert exported["skillId"] == "skill_unit_custom"
    assert disabled["disabled"] is True
    assert created["skillId"] not in {item["skillId"] for item in registry.list_skills()}

    enabled = registry.set_skill_disabled(created["skillId"], False)
    updated = registry.update_skill(created["skillId"], {"description": "Updated description"})
    deleted = registry.delete_skill(created["skillId"])

    assert enabled["disabled"] is False
    assert updated["description"] == "Updated description"
    assert deleted["deleted"] == "skill_unit_custom"


def test_skill_schema_rejects_unknown_tool(tmp_settings: Path) -> None:
    config = _custom_skill()
    config["allowedTools"] = ["rm_rf"]

    with pytest.raises(AppError):
        registry.create_custom_skill(config)


def test_skill_runner_offline_persists_project_artifacts(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    project = projects.create_project("Skill Project")

    result = run_skill("skill_research_brief", {"topic": "Skill System", "depth": "quick"}, project_id=project["id"], offline=True)
    exported = projects.export_project(project["id"])
    artifact_index = evidence.artifacts_for_skill_run(result["skillRunId"])

    assert result["ok"] is True
    assert result["artifacts"][0]["source"]["skillRunId"] == result["skillRunId"]
    assert result["savedItems"][0]["source"]["skillId"] == "skill_research_brief"
    assert exported["skillRuns"][0]["skillRunId"] == result["skillRunId"]
    assert exported["artifacts"][0]["source"]["type"] == "skill_run"
    assert artifact_index[0]["source"]["projectId"] == project["id"]


def test_skill_permission_gate_denies_tools_outside_allowed_list() -> None:
    skill = registry.get_skill("skill_code_review")
    decision = permissions.evaluate_skill_tool(skill, "fetch_url", {"url": "https://example.com"})

    assert decision.allowed is False


def test_skill_eval_report_scores_skills_and_packs(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    report = skill_eval.build_skill_eval_report(
        version="test",
        scope="pack",
        pack_id="pack_code",
        cases=[
            {
                "caseId": "case_code_quality",
                "skillId": "skill_code_review",
                "input": {"scope": "def add(a, b): return a - b", "focus": "bug"},
                "expectedKeywords": ["Code Review Skill", "Offline Skill run completed"],
                "requiredOutputPaths": ["content"],
                "expectedArtifactTypes": ["md"],
                "deniedTools": ["fetch_url"],
                "projectBindingRequired": True,
            }
        ],
    )

    assert report["status"] == "PASS"
    assert report["summary"]["caseCount"] >= 1
    assert report["checks"]["packLevelEval"] == "PASS"
    assert any(item["packId"] == "pack_code" for item in report["packResults"])
    assert report["caseResults"][0]["metrics"]["toolPolicyPass"] is True


def test_skill_eval_synthetic_media_skill_gets_temp_media_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    report = skill_eval.build_skill_eval_report(version="test", scope="skill", skill_id="image_explainer", cases=[])

    assert report["status"] == "PASS"
    assert report["summary"]["caseCount"] == 1
    case = report["caseResults"][0]
    assert case["metrics"]["schemaPass"] is True
    assert case["metrics"]["projectBindingPass"] is True
    assert case["metrics"]["contentPass"] is True
    assert case["input"]["mediaIds"][0].startswith("media_")


def test_skill_eval_case_crud_uses_runtime_skills_dir(tmp_settings: Path) -> None:
    saved = skill_eval.save_eval_case(
        {
            "caseId": "case_user_eval",
            "skillId": "skill_study_tutor",
            "input": {"question": "Explain RR scheduling"},
            "expectedKeywords": ["RR"],
            "requiredOutputPaths": ["content"],
        }
    )
    cases = skill_eval.load_eval_cases(include_user=True)
    deleted = skill_eval.delete_eval_case("case_user_eval")

    assert saved["caseId"] == "case_user_eval"
    assert any(case["caseId"] == "case_user_eval" for case in cases)
    assert deleted["deleted"] == "case_user_eval"


def test_skill_version_history_diff_migration_and_rollback(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    created = registry.create_custom_skill(_custom_skill())
    project = projects.create_project("Skill Version Project")
    projects.set_project_skill_binding(project["id"], [created["skillId"]], default_skill=created["skillId"])

    updated_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "default": "general"},
            "level": {"type": "string", "default": "beginner"},
        },
        "required": ["subject", "level"],
        "additionalProperties": False,
    }
    updated = registry.update_skill(
        created["skillId"],
        {
            "version": "1.1.0",
            "inputSchema": updated_schema,
            "allowedTools": ["search_files", "fetch_url"],
            "changeSummary": "Rename topic to subject and add level",
        },
    )

    versions = skill_versioning.list_skill_versions(created["skillId"])
    diff = skill_versioning.diff_skill_versions(created["skillId"], "1.0.0", "1.1.0")
    plan = skill_versioning.migration_plan(created["skillId"], "1.0.0", "1.1.0")
    rolled_back = skill_versioning.rollback_skill(created["skillId"], "1.0.0")

    assert updated["version"] == "1.1.0"
    assert {"1.0.0", "1.1.0"} <= {item["version"] for item in versions}
    assert diff["toolGrantDiff"]["added"] == ["fetch_url"]
    assert any(item["field"] == "inputSchema" and item["changed"] for item in diff["fields"])
    assert any(item["type"] == "inputFieldRenamed" for item in plan["changes"])
    assert plan["safe"] is True
    assert plan["migrationTargets"]["projectBindings"] == 1
    assert rolled_back["skill"]["version"] == "1.0.0"
    assert registry.get_skill(created["skillId"], include_disabled=True)["version"] == "1.0.0"


def test_pack_versioning_upgrade_gate_and_project_binding(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "TRACE_ENABLED", False)
    imported = registry.import_pack(
        {
            "packId": "pack_unit_versioned",
            "name": "Versioned Pack",
            "description": "Pack lifecycle test.",
            "version": "1.0.0",
            "skills": [_custom_skill()],
        },
        overwrite=True,
    )
    project = projects.create_project("Pack Version Project")

    versions = skill_versioning.list_pack_versions(imported["packId"])
    gate = skill_versioning.eval_aware_upgrade_gate(kind="pack", item_id=imported["packId"])
    upgraded = skill_versioning.upgrade_pack(imported["packId"], "1.0.0", project_id=project["id"])

    assert any(item["version"] == "1.0.0" for item in versions)
    assert gate["status"] in {"PASS", "REVIEW"}
    assert upgraded["ok"] is True
    assert upgraded["projectBinding"]["enabledPackVersions"][0]["packId"] == "pack_unit_versioned"
    assert upgraded["projectBinding"]["enabledPackVersions"][0]["version"] == "1.0.0"
