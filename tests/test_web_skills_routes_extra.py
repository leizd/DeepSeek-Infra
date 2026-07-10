"""Extra tests for skills routes to cover edge cases."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.skills import SkillsRouteDeps, create_skills_router


def _skill_config() -> dict[str, Any]:
    return {
        "skillId": "skill_test_extra",
        "name": "Extra Skill",
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
def client() -> Iterator[TestClient]:
    deps = SkillsRouteDeps(
        list_skills=lambda *_, **__: [],
        list_builtin_skills=lambda *_, **__: [],
        get_skill=lambda *_args, **_kwargs: {},
        create_custom_skill=lambda *_, **__: {},
        update_skill=lambda _id, _patch: {},
        set_skill_disabled=lambda _id, _disabled: {},
        delete_skill=lambda _id: {"ok": True},
        import_skill_config=lambda *_, **__: {},
        export_skill_config=lambda _id: {},
        run_skill=lambda *_, **__: {"ok": True, "skillId": "s", "projectId": "", "skillRunId": "r", "artifacts": [], "savedItems": [], "traceId": ""},
        list_packs=lambda *_, **__: [],
        get_pack=lambda _id: {},
        export_pack=lambda _id: {},
        import_pack=lambda *_, **__: {"ok": True},
        validate_pack=lambda _config: {},
        delete_pack=lambda _id: {"ok": True},
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_skills_router(deps))
    with patch("deepseek_infra.web.routes.skills.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_skills_builtin_action(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "builtin"})
    assert resp.status_code == 200


def test_skills_get_update_disable_enable_delete(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "get", "skillId": "s1"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "update", "skillId": "s1", "patch": {"name": "X"}})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "disable", "skillId": "s1"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "enable", "skillId": "s1"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "delete", "skillId": "s1"})
    assert resp.status_code == 200


def test_skills_import_export(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "import", "skill": _skill_config()})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "export", "skillId": "s1"})
    assert resp.status_code == 200


def test_skills_run_path(client: TestClient) -> None:
    resp = client.post("/api/skills/s1/run", json={"input": {"topic": "x"}, "offline": True})
    assert resp.status_code == 200


def test_skills_dry_run(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "dry_run", "skill": _skill_config(), "input": {"topic": "x"}})
    assert resp.status_code == 200


def test_skills_pack_actions(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "list_packs"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "get_pack", "packId": "p1"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "export_pack", "packId": "p1"})
    assert resp.status_code == 200

    pack = {"packId": "p1", "name": "P", "description": "d", "version": "1.0.0", "skills": [_skill_config()]}
    resp = client.post("/api/skills", json={"action": "validate_pack", "pack": pack})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "import_pack", "pack": pack, "overwrite": True, "onConflict": "replace"})
    assert resp.status_code == 200

    resp = client.post("/api/skills", json={"action": "delete_pack", "packId": "p1"})
    assert resp.status_code == 200


def test_skills_eval_case_actions(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "list_eval_cases"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_eval.save_eval_case", return_value={"caseId": "c1"}):
        resp = client.post("/api/skills", json={"action": "create_eval_case", "case": {"caseId": "c1", "skillId": "s1", "input": {"topic": "x"}}})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_eval.delete_eval_case", return_value={"deleted": "c1"}):
        resp = client.post("/api/skills", json={"action": "delete_eval_case", "caseId": "c1"})
    assert resp.status_code == 200


def test_skills_security_review_with_config(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_security.review_skill", return_value={"ok": True}):
        resp = client.post("/api/skills", json={"action": "security_review", "config": _skill_config()})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_security.review_pack", return_value={"ok": True}):
        resp = client.post("/api/skills", json={"action": "security_review_pack", "pack": {"packId": "p1"}})
    assert resp.status_code == 200


def test_skills_trust_block_untrust(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_security.trust_skill", return_value={"trustLevel": "trusted"}):
        resp = client.post("/api/skills", json={"action": "trust_skill", "skillId": "s1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_security.untrust_skill", return_value={"trustLevel": "needs-review"}):
        resp = client.post("/api/skills", json={"action": "untrust_skill", "skillId": "s1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_security.block_skill", return_value={"trustLevel": "blocked"}):
        resp = client.post("/api/skills", json={"action": "block_skill", "skillId": "s1", "reason": "test"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_security.security_summary", return_value={"highRisk": 0}):
        resp = client.post("/api/skills", json={"action": "security_summary"})
    assert resp.status_code == 200


def test_skills_versioning_actions(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_versioning.list_skill_versions", return_value=[]):
        resp = client.post("/api/skills", json={"action": "list_versions", "skillId": "s1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.diff_skill_versions", return_value={"fields": []}):
        resp = client.post("/api/skills", json={"action": "diff_versions", "skillId": "s1", "from": "1", "to": "2"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.migration_plan", return_value={"safe": True}):
        resp = client.post("/api/skills", json={"action": "migration_plan", "skillId": "s1", "from": "1", "to": "2"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.rollback_skill", return_value={"skillId": "s1"}):
        resp = client.post("/api/skills", json={"action": "rollback_skill", "skillId": "s1", "version": "1"})
    assert resp.status_code == 200


def test_skills_pack_versioning_actions(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_versioning.list_pack_versions", return_value=[]):
        resp = client.post("/api/skills", json={"action": "list_pack_versions", "packId": "p1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.diff_pack_versions", return_value={"packId": "p1"}):
        resp = client.post("/api/skills", json={"action": "diff_pack_versions", "packId": "p1", "from": "1", "to": "current"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.upgrade_pack", return_value={"evalAwareUpgradeGate": {"status": "PASS"}, "projectBinding": {"enabledPackVersions": [{"packId": "p1"}]}}):
        resp = client.post("/api/skills", json={"action": "upgrade_pack", "packId": "p1", "version": "1.0.0", "projectId": "p1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_versioning.rollback_pack", return_value={"ok": True}):
        resp = client.post("/api/skills", json={"action": "rollback_pack", "packId": "p1", "version": "1.0.0", "projectId": "p1"})
    assert resp.status_code == 200


def test_skills_eval_upgrade_gate(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_versioning.eval_aware_upgrade_gate", return_value={"status": "PASS"}):
        resp = client.post("/api/skills", json={"action": "eval_upgrade_gate", "kind": "skill", "itemId": "s1"})
    assert resp.status_code == 200


def test_skills_analytics_and_runs(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_analytics.list_runs", return_value=[]):
        resp = client.post("/api/skills", json={"action": "list_runs", "skillId": "s1", "limit": 10})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.get_run", return_value={"skillRunId": "r1"}):
        resp = client.post("/api/skills", json={"action": "get_run", "skillRunId": "r1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.delete_run", return_value={"deleted": 1}):
        resp = client.post("/api/skills", json={"action": "delete_run", "skillRunId": "r1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.cleanup_runs", return_value={"deleted": 1}):
        resp = client.post("/api/skills", json={"action": "cleanup_runs", "status": "failed", "keepRecent": 5})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.redact_run", return_value={"run": {"redacted": True}}):
        resp = client.post("/api/skills", json={"action": "redact_run", "skillRunId": "r1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.analytics_summary", return_value={"totalRuns": 0}):
        resp = client.post("/api/skills", json={"action": "analytics_summary", "scope": "all", "days": 7})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_analytics.list_runs", return_value=[]):
        resp = client.post("/api/skills", json={"action": "export_runs", "skillId": "s1"})
    assert resp.status_code == 200


def test_skills_catalog_actions(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_manifest", return_value={"items": []}):
        resp = client.post("/api/skills", json={"action": "catalog_list"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_get", return_value={"itemId": "i1"}):
        resp = client.post("/api/skills", json={"action": "catalog_get", "itemId": "i1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_search", return_value={"items": []}):
        resp = client.post("/api/skills", json={"action": "catalog_search", "query": "q", "filters": {"trusted": True}})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_install", return_value={"ok": True}):
        resp = client.post("/api/skills", json={"action": "catalog_install", "itemId": "i1", "projectId": "p1", "securityApproved": True, "dryRun": True})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_uninstall", return_value={"ok": True}):
        resp = client.post("/api/skills", json={"action": "catalog_uninstall", "itemId": "i1", "projectId": "p1"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_refresh", return_value={"schemaVersion": "v1"}):
        resp = client.post("/api/skills", json={"action": "catalog_refresh"})
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.skills.skill_catalog.catalog_export", return_value={"summary": {"itemCount": 1}}):
        resp = client.post("/api/skills", json={"action": "catalog_export"})
    assert resp.status_code == 200


def test_skills_helper_validation_errors(client: TestClient) -> None:
    resp = client.post("/api/skills", json={"action": "get", "skillId": "   "})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "get_pack", "packId": ""})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "catalog_get", "itemId": ""})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "validate", "config": {}})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "create_eval_case", "case": {}})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "delete_eval_case", "caseId": ""})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "get_run", "skillRunId": ""})
    assert resp.status_code == 400

    resp = client.post("/api/skills", json={"action": "rollback_skill", "skillId": "s1", "version": ""})
    assert resp.status_code == 400
