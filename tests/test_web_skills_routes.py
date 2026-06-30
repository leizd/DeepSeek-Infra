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
