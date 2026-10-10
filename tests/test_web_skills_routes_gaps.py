"""Gap tests for skills routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.skills import SkillsRouteDeps, create_skills_router


def _skill_config() -> dict[str, Any]:
    return {
        "skillId": "skill_gap_test",
        "name": "Gap Skill",
        "description": "d",
        "version": "1.0.0",
        "systemPrompt": "Return markdown.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
        "allowedTools": ["search_files"],
        "memoryPolicy": {"scope": "project", "read": True, "write": False},
        "artifactPolicy": {"autoSave": True, "types": ["md"]},
        "projectBinding": {"enabled": True},
        "exampleInputs": [{"topic": "web"}],
    }


@pytest.fixture
def skills_client() -> Iterator[tuple[TestClient, Any]]:
    deps = SkillsRouteDeps(
        list_skills=MagicMock(return_value=[]),
        list_builtin_skills=MagicMock(return_value=[]),
        get_skill=MagicMock(return_value={}),
        create_custom_skill=MagicMock(return_value={}),
        update_skill=MagicMock(return_value={}),
        set_skill_disabled=MagicMock(return_value={}),
        delete_skill=MagicMock(return_value={"ok": True}),
        import_skill_config=MagicMock(return_value={}),
        export_skill_config=MagicMock(return_value={}),
        run_skill=MagicMock(return_value={"ok": True, "skillId": "s", "projectId": "", "skillRunId": "r", "artifacts": [], "savedItems": [], "traceId": ""}),
        list_packs=MagicMock(return_value=[]),
        get_pack=MagicMock(return_value={}),
        export_pack=MagicMock(return_value={}),
        import_pack=MagicMock(return_value={"ok": True}),
        validate_pack=MagicMock(return_value={}),
        delete_pack=MagicMock(return_value={"ok": True}),
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_skills_router(deps))
    with patch("deepseek_infra.web.routes.skills.require_api_auth", lambda request: None):
        yield TestClient(app), deps


def test_skills_unsupported_action(skills_client: tuple[TestClient, Any]) -> None:
    client, _ = skills_client
    resp = client.post("/api/skills", json={"action": "not_supported"})
    assert resp.status_code == 400


def test_skills_pack_config_fallback(skills_client: tuple[TestClient, Any]) -> None:
    """Pack config can be supplied as top-level keys excluding action/onConflict/overwrite."""
    client, deps = skills_client
    resp = client.post("/api/skills", json={"action": "validate_pack", "packId": "p1", "name": "P"})
    assert resp.status_code == 200
    assert deps.validate_pack.call_count == 2
    called_config = deps.validate_pack.call_args_list[0][0][0]
    assert called_config["packId"] == "p1"
    assert called_config["name"] == "P"


def test_skills_pack_config_missing(skills_client: tuple[TestClient, Any]) -> None:
    resp = skills_client[0].post("/api/skills", json={"action": "validate_pack"})
    assert resp.status_code == 400


def test_skills_skill_config_fallback(skills_client: tuple[TestClient, Any]) -> None:
    """Skill config can be supplied as top-level keys excluding action/overwrite."""
    client, deps = skills_client
    resp = client.post("/api/skills", json={"action": "create", **{k: v for k, v in _skill_config().items()}})
    assert resp.status_code == 200
    deps.create_custom_skill.assert_called_once()
    called_config = deps.create_custom_skill.call_args[0][0]
    assert called_config["skillId"] == "skill_gap_test"


def test_skills_skill_config_missing(skills_client: tuple[TestClient, Any]) -> None:
    resp = skills_client[0].post("/api/skills", json={"action": "create"})
    assert resp.status_code == 400


def test_skills_eval_case_fallback(skills_client: tuple[TestClient, Any]) -> None:
    """Eval case can be supplied as top-level keys excluding action."""
    client, _ = skills_client
    with patch("deepseek_infra.web.routes.skills.skill_eval.save_eval_case", side_effect=lambda case: case) as save:
        resp = client.post("/api/skills", json={"action": "create_eval_case", "caseId": "c1", "skillId": "s1", "input": {"topic": "x"}})
    assert resp.status_code == 200
    save.assert_called_once()
    called_case = save.call_args[0][0]
    assert called_case["caseId"] == "c1"


def test_skills_eval_case_missing(skills_client: tuple[TestClient, Any]) -> None:
    resp = skills_client[0].post("/api/skills", json={"action": "create_eval_case"})
    assert resp.status_code == 400


def test_skills_limit_invalid_uses_default(skills_client: tuple[TestClient, Any]) -> None:
    client, _ = skills_client
    with patch("deepseek_infra.web.routes.skills.skill_analytics.list_runs", return_value=[]) as list_runs:
        resp = client.post("/api/skills", json={"action": "list_runs", "limit": "bad"})
    assert resp.status_code == 200
    list_runs.assert_called_once()
    assert list_runs.call_args.kwargs["limit"] == 50


def test_skills_skill_patch_fallback(skills_client: tuple[TestClient, Any]) -> None:
    """Skill patch can be supplied as top-level keys excluding action/skillId/id."""
    client, deps = skills_client
    resp = client.post("/api/skills", json={"action": "update", "skillId": "s1", "name": "New", "version": "2.0.0"})
    assert resp.status_code == 200
    deps.update_skill.assert_called_once()
    called_patch = deps.update_skill.call_args[0][1]
    assert called_patch["name"] == "New"
    assert called_patch["version"] == "2.0.0"


def test_skills_run_input_fallback(skills_client: tuple[TestClient, Any]) -> None:
    """Skill run uses empty input when no recognized input key is present."""
    client, deps = skills_client
    resp = client.post("/api/skills/s1/run", json={"offline": True})
    assert resp.status_code == 200
    deps.run_skill.assert_called_once()
    called_input = deps.run_skill.call_args[0][1]
    assert called_input == {}


def test_skills_dry_run_input_violation(skills_client: tuple[TestClient, Any]) -> None:
    client, _ = skills_client
    skill = _skill_config()
    resp = client.post("/api/skills", json={"action": "dry_run", "skill": skill, "input": {}})
    assert resp.status_code == 400
    assert "input" in resp.json()["error"].lower()


def test_skills_dry_run_output_violation(skills_client: tuple[TestClient, Any]) -> None:
    client, _ = skills_client
    skill = _skill_config()
    skill["outputSchema"] = {"type": "object", "properties": {"content": {"type": "number"}, "mode": {"type": "string", "enum": ["online"]}}, "required": ["content", "mode"]}
    resp = client.post("/api/skills", json={"action": "dry_run", "skill": skill, "input": {"topic": "x"}})
    assert resp.status_code == 400
    assert "output" in resp.json()["error"].lower()


def test_skills_bool_string_truthy(skills_client: tuple[TestClient, Any]) -> None:
    """_bool treats the string 'true' as truthy."""
    client, deps = skills_client
    resp = client.post("/api/skills/s1/run", json={"input": {"topic": "x"}, "offline": "true"})
    assert resp.status_code == 200
    deps.run_skill.assert_called_once()
    assert deps.run_skill.call_args.kwargs["offline"] is True


def test_skills_bool_string_falsy(skills_client: tuple[TestClient, Any]) -> None:
    """_bool treats the string 'false' as falsy."""
    client, deps = skills_client
    resp = client.post("/api/skills/s1/run", json={"input": {"topic": "x"}, "offline": "false"})
    assert resp.status_code == 200
    deps.run_skill.assert_called_once()
    assert deps.run_skill.call_args.kwargs["offline"] is False
