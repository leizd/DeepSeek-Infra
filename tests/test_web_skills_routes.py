from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import ErrorCode
from deepseek_infra.infra.data import projects


def _collect_route_paths(routes: list[Any]) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= _collect_route_paths(getattr(original, "routes", []))
    return paths


@contextlib.contextmanager
def _running_server() -> Iterator[Any]:
    server, _ = server_module.create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: Any,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], http.client.HTTPResponse]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data.decode("utf-8")), response
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


def _custom_skill() -> dict[str, Any]:
    return {
        "skillId": "skill_web_custom",
        "name": "Web Custom Skill",
        "description": "Used by Skill route tests.",
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


def test_skills_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/skills" in paths
    assert "/api/skills/{skill_id}/run" in paths


def test_skills_action_auth_enforced() -> None:
    with _running_server() as server:
        status, payload, _ = _request(server, "POST", "/api/skills", payload={"action": "list"})

    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_skills_action_list_get_create_disable_enable_and_run(tmp_settings: Path) -> None:
    project = projects.create_project("Skill Route Project")

    with _running_server() as server:
        status, listed, _ = _request(server, "POST", "/api/skills", payload={"action": "list"}, headers=_auth_headers())
        assert status == 200
        assert "skill_research_brief" in {item["skillId"] for item in listed["skills"]}

        status, fetched, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "get", "skillId": "skill_research_brief"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert fetched["skill"]["skillId"] == "skill_research_brief"

        status, created, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "create", "skill": _custom_skill()},
            headers=_auth_headers(),
        )
        assert status == 200
        assert created["skill"]["skillId"] == "skill_web_custom"

        status, disabled, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "disable", "skillId": "skill_web_custom"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert disabled["skill"]["disabled"] is True

        status, enabled, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "enable", "skillId": "skill_web_custom"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert enabled["skill"]["disabled"] is False

        status, run, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={
                "action": "run",
                "skillId": "skill_research_brief",
                "input": {"topic": "Skill API", "depth": "quick"},
                "projectId": project["id"],
                "offline": True,
            },
            headers=_auth_headers(),
        )
        assert status == 200
        assert run["ok"] is True
        assert run["skillId"] == "skill_research_brief"
        assert run["projectId"] == project["id"]
        assert run["artifacts"][0]["source"]["skillRunId"] == run["skillRunId"]


def test_skill_run_path_executes_offline(tmp_settings: Path) -> None:
    project = projects.create_project("Skill Run Path")

    with _running_server() as server:
        status, payload, _ = _request(
            server,
            "POST",
            "/api/skills/skill_research_brief/run",
            payload={"input": {"topic": "Path Run", "depth": "quick"}, "projectId": project["id"], "offline": True},
            headers=_auth_headers(),
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["skillId"] == "skill_research_brief"


def test_skills_run_analytics_actions_and_project_endpoint(tmp_settings: Path) -> None:
    project = projects.create_project("Skill Analytics API Project")

    with _running_server() as server:
        status, run, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={
                "action": "run",
                "skillId": "skill_research_brief",
                "input": {"topic": "Skill analytics", "depth": "quick"},
                "projectId": project["id"],
                "offline": True,
            },
            headers=_auth_headers(),
        )
        assert status == 200
        run_id = run["skillRunId"]

        status, listed, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "list_runs", "skillId": "skill_research_brief", "limit": 10},
            headers=_auth_headers(),
        )
        assert status == 200
        assert run_id in {item["skillRunId"] for item in listed["skillRuns"]}
        first = listed["skillRuns"][0]
        assert first["skillVersion"]
        assert first["latencyMs"] >= 0
        assert first["artifactCount"] >= 1
        assert first["links"]["trace"]

        status, fetched, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "get_run", "skillRunId": run_id},
            headers=_auth_headers(),
        )
        assert status == 200
        assert fetched["skillRun"]["skillRunId"] == run_id

        status, summary, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "analytics_summary", "scope": "skill", "skillId": "skill_research_brief"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert summary["summary"]["totalRuns"] >= 1
        assert summary["summary"]["successRate"] > 0

        status, project_summary, _ = _request(
            server,
            "GET",
            f"/api/workspace/projects/{project['id']}/skill-analytics",
            headers=_auth_headers(),
        )
        assert status == 200
        assert project_summary["summary"]["projectId"] == project["id"]
        assert project_summary["summary"]["projectBindingRuns"] >= 1

        status, redacted, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "redact_run", "skillRunId": run_id},
            headers=_auth_headers(),
        )
        assert status == 200
        assert redacted["run"]["redacted"] is True
        assert redacted["run"]["inputSummary"] == "[redacted]"

        status, _, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "run", "skillId": "skill_research_brief", "input": {}, "offline": True},
            headers=_auth_headers(),
        )
        assert status == 400

        status, failed, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "list_runs", "status": "failed", "limit": 10},
            headers=_auth_headers(),
        )
        assert status == 200
        assert any(item["failureCategory"] == "schema_validation_failed" for item in failed["skillRuns"])

        status, cleaned, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "cleanup_runs", "status": "failed"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert cleaned["deleted"] >= 1

        status, deleted, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "delete_run", "skillRunId": run_id},
            headers=_auth_headers(),
        )
        assert status == 200
        assert deleted["deleted"] == 1


def test_skills_validate_and_dry_run_authoring_actions(tmp_settings: Path) -> None:
    skill = _custom_skill()

    with _running_server() as server:
        status, validated, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "validate", "skill": skill},
            headers=_auth_headers(),
        )
        assert status == 200
        assert validated["ok"] is True
        assert validated["skill"]["skillId"] == skill["skillId"]

        status, dry_run, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "dry_run", "skill": skill, "input": {"topic": "builder"}},
            headers=_auth_headers(),
        )

    assert status == 200
    assert dry_run["ok"] is True
    assert dry_run["dryRun"] is True
    assert dry_run["skillRunId"] == "dry-run"
    assert dry_run["policy"]["allowedTools"] == ["search_files"]
    assert "builder" in dry_run["output"]["content"]


def test_skills_validate_rejects_unknown_builder_tool(tmp_settings: Path) -> None:
    skill = _custom_skill()
    skill["allowedTools"] = ["unknown_builder_tool"]

    with _running_server() as server:
        status, payload, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "validate", "skill": skill},
            headers=_auth_headers(),
        )

    assert status == 400
    assert "unknown_builder_tool" in payload["error"]


def _pack_payload() -> dict[str, Any]:
    return {
        "packId": "pack_web_test",
        "name": "Web Pack",
        "description": "Used by Skill Pack route tests.",
        "version": "1.0.0",
        "author": "local",
        "skills": [
            {
                "skillId": "skill_pack_web_a",
                "name": "Pack Web A",
                "description": "d",
                "version": "1.0.0",
                "systemPrompt": "Return markdown.",
                "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"], "additionalProperties": False},
                "outputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": True},
                "allowedTools": ["search_files"],
                "memoryPolicy": {"scope": "project", "read": True, "write": False},
                "artifactPolicy": {"autoSave": True, "types": ["md"]},
                "projectBinding": {"enabled": True},
                "exampleInputs": [{"topic": "pack"}],
            }
        ],
    }


def test_skills_pack_list_get_export_and_import(tmp_settings: Path) -> None:
    with _running_server() as server:
        status, listed, _ = _request(server, "POST", "/api/skills", payload={"action": "list_packs"}, headers=_auth_headers())
        assert status == 200
        assert "pack_study" in {item["packId"] for item in listed["packs"]}

        status, fetched, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "get_pack", "packId": "pack_study"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert fetched["pack"]["packId"] == "pack_study"

        status, exported, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "export_pack", "packId": "pack_study"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert all("systemPrompt" in skill for skill in exported["pack"]["skills"])

        status, imported, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "import_pack", "pack": _pack_payload()},
            headers=_auth_headers(),
        )
        assert status == 200
        assert imported["ok"] is True
        assert imported["installedSkills"] == ["skill_pack_web_a"]
        assert imported["toolPermissions"]


def test_skills_pack_import_conflict_and_delete(tmp_settings: Path) -> None:
    with _running_server() as server:
        _request(server, "POST", "/api/skills", payload={"action": "import_pack", "pack": _pack_payload()}, headers=_auth_headers())
        status, payload, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "import_pack", "pack": _pack_payload()},
            headers=_auth_headers(),
        )
        assert status == 409
        assert "skill_pack_web_a" in payload["error"]

        status, deleted, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "delete_pack", "packId": "pack_web_test"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert deleted["ok"] is True


def test_skills_pack_install_to_project(tmp_settings: Path) -> None:
    project = projects.create_project("Pack Install Project")
    with _running_server() as server:
        status, payload, _ = _request(
            server,
            "POST",
            f"/api/workspace/projects/{project['id']}/skill-packs/pack_study/install",
            headers=_auth_headers(),
        )
    assert status == 200
    assert "skill_study_tutor" in payload["skills"]["enabledSkills"]
    assert "pack_study" in payload["skills"]["enabledPacks"]


def test_skills_pack_actions_require_auth() -> None:
    with _running_server() as server:
        status, payload, _ = _request(server, "POST", "/api/skills", payload={"action": "list_packs"})
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_skills_eval_report_and_case_builder_actions(tmp_settings: Path) -> None:
    with _running_server() as server:
        status, created, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={
                "action": "create_eval_case",
                "case": {
                    "caseId": "case_web_eval",
                    "skillId": "skill_study_tutor",
                    "input": {"question": "Explain FCFS and RR scheduling."},
                    "expectedKeywords": ["FCFS", "RR"],
                    "requiredOutputPaths": ["content"],
                    "expectedArtifactTypes": ["md"],
                    "projectBindingRequired": True,
                },
            },
            headers=_auth_headers(),
        )
        assert status == 200
        assert created["case"]["caseId"] == "case_web_eval"

        status, cases, _ = _request(server, "POST", "/api/skills", payload={"action": "list_eval_cases"}, headers=_auth_headers())
        assert status == 200
        assert "case_web_eval" in {item["caseId"] for item in cases["cases"]}

        status, report, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "eval_report", "scope": "skill", "skillId": "skill_study_tutor", "version": "test"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert report["report"]["status"] == "PASS"
        assert report["report"]["summary"]["caseCount"] >= 1
        assert report["report"]["checks"]["regressionCompare"] == "PASS"

        status, deleted, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "delete_eval_case", "caseId": "case_web_eval"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert deleted["deleted"] == "case_web_eval"


def test_skills_versioning_actions(tmp_settings: Path) -> None:
    skill = _custom_skill()
    with _running_server() as server:
        status, created, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "create", "skill": skill},
            headers=_auth_headers(),
        )
        assert status == 200
        assert created["skill"]["skillId"] == "skill_web_custom"

        updated_skill = dict(skill)
        updated_skill["version"] = "1.1.0"
        updated_skill["inputSchema"] = {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "default": "web"},
                "level": {"type": "string", "default": "beginner"},
            },
            "required": ["subject", "level"],
            "additionalProperties": False,
        }
        status, updated, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={
                "action": "update",
                "skillId": "skill_web_custom",
                "patch": {
                    "version": "1.1.0",
                    "inputSchema": updated_skill["inputSchema"],
                    "changeSummary": "Web versioning update",
                },
            },
            headers=_auth_headers(),
        )
        assert status == 200
        assert updated["skill"]["version"] == "1.1.0"

        status, versions, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "list_versions", "skillId": "skill_web_custom"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert {"1.0.0", "1.1.0"} <= {item["version"] for item in versions["versions"]}

        status, diff, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "diff_versions", "skillId": "skill_web_custom", "from": "1.0.0", "to": "1.1.0"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert diff["diff"]["toolGrantDiff"]["added"] == []
        assert any(item["field"] == "inputSchema" and item["changed"] for item in diff["diff"]["fields"])

        status, plan, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "migration_plan", "skillId": "skill_web_custom", "from": "1.0.0", "to": "1.1.0"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert plan["migrationPlan"]["safe"] is True

        status, rollback, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "rollback_skill", "skillId": "skill_web_custom", "version": "1.0.0"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert rollback["skill"]["version"] == "1.0.0"


def test_skills_pack_versioning_actions(tmp_settings: Path) -> None:
    project = projects.create_project("Pack Version API Project")
    with _running_server() as server:
        status, imported, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "import_pack", "pack": _pack_payload()},
            headers=_auth_headers(),
        )
        assert status == 200
        assert imported["pack"]["packId"] == "pack_web_test"

        status, versions, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "list_pack_versions", "packId": "pack_web_test"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert any(item["version"] == "1.0.0" for item in versions["versions"])

        status, diff, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "diff_pack_versions", "packId": "pack_web_test", "from": "1.0.0", "to": "current"},
            headers=_auth_headers(),
        )
        assert status == 200
        assert diff["diff"]["packId"] == "pack_web_test"

        status, upgraded, _ = _request(
            server,
            "POST",
            "/api/skills",
            payload={"action": "upgrade_pack", "packId": "pack_web_test", "version": "1.0.0", "projectId": project["id"]},
            headers=_auth_headers(),
        )
        assert status == 200
        assert upgraded["evalAwareUpgradeGate"]["status"] in {"PASS", "REVIEW"}
        assert upgraded["projectBinding"]["enabledPackVersions"][0]["packId"] == "pack_web_test"
