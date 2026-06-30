from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.observability import observability
from deepseek_infra.infra.skills import eval as skill_eval
from deepseek_infra.infra.skills import evidence, permissions, registry
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
