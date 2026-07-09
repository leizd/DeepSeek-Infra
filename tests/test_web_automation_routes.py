from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import projects
from deepseek_infra.web.routes.automation import (
    _automation_id,
    _automation_payload,
    _bool,
    _limit,
    _now_payload,
    _patch_payload,
    _run_id,
    _template_id,
    _trigger_payload,
    create_automation_router,
)


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
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data.decode("utf-8"))
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


def test_automation_routes_auth_enforced() -> None:
    with _running_server() as server:
        status, payload = _request(server, "GET", "/api/automation")

    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_automation_routes_create_run_templates_and_history(tmp_settings: Path) -> None:
    project = projects.create_project("Automation API Project")
    with _running_server() as server:
        status, created = _request(
            server,
            "POST",
            "/api/automation",
            payload={
                "action": "create",
                "automation": {
                    "projectId": project["id"],
                    "name": "API summary",
                    "trigger": {"type": "manual"},
                    "condition": {"type": "always"},
                    "action": {"type": "project_summary"},
                    "policy": {"maxRunsPerDay": 5, "allowBrowser": False, "allowNetwork": False},
                },
            },
            headers=_auth_headers(),
        )
        assert status == 200
        automation_id = created["automation"]["automationId"]

        status, run = _request(server, "POST", f"/api/automation/{automation_id}/run", payload={}, headers=_auth_headers())
        assert status == 200
        assert run["run"]["status"] == "success"

        status, runs = _request(server, "GET", f"/api/automation/{automation_id}/runs", headers=_auth_headers())
        assert status == 200
        assert runs["runs"][0]["runId"] == run["run"]["runId"]

        status, templates = _request(server, "GET", "/api/automation/templates", headers=_auth_headers())
        assert status == 200
        assert len(templates["templates"]) >= 6

        status, disabled = _request(
            server,
            "POST",
            "/api/automation",
            payload={"action": "disable", "automationId": automation_id},
            headers=_auth_headers(),
        )
        assert status == 200
        assert disabled["automation"]["enabled"] is False


@contextlib.contextmanager
def _mocked_automation_client() -> Iterator[tuple[TestClient, MagicMock, MagicMock, MagicMock, MagicMock]]:
    registry = MagicMock()
    runner = MagicMock()
    scheduler = MagicMock()
    history = MagicMock()
    with (
        patch("deepseek_infra.web.routes.automation.registry", registry),
        patch("deepseek_infra.web.routes.automation.runner", runner),
        patch("deepseek_infra.web.routes.automation.scheduler", scheduler),
        patch("deepseek_infra.web.routes.automation.history", history),
        patch("deepseek_infra.web.routes.automation.require_api_auth", lambda request: None),
    ):
        app = FastAPI()

        @app.exception_handler(AppError)
        async def app_error_handler(request: Any, exc: Exception) -> JSONResponse:
            app_exc = exc if isinstance(exc, AppError) else AppError(str(exc))
            return JSONResponse(app_exc.to_response(), status_code=app_exc.status)

        app.include_router(create_automation_router())
        yield TestClient(app), registry, runner, scheduler, history


def test_api_automation_list_query_params() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.list_automations.return_value = [{"automationId": "a1"}]
        response = client.get("/api/automation?projectId=p1&includeDisabled=0")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "automations": [{"automationId": "a1"}]}
        registry.list_automations.assert_called_once_with(project_id="p1", include_disabled=False)


def test_api_automation_action_list() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.list_automations.return_value = [{"automationId": "a2"}]
        response = client.post("/api/automation", json={"action": "list", "projectId": "p2", "includeDisabled": False})
        assert response.status_code == 200
        registry.list_automations.assert_called_once_with(project_id="p2", include_disabled=False)


def test_api_automation_action_create() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.create_automation.return_value = {"automationId": "new"}
        response = client.post("/api/automation", json={"action": "create", "automation": {"name": "x"}})
        assert response.status_code == 200
        assert response.json()["automation"]["automationId"] == "new"
        registry.create_automation.assert_called_once_with({"name": "x"})


def test_api_automation_action_create_uses_config_key() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.create_automation.return_value = {"automationId": "cfg"}
        response = client.post("/api/automation", json={"action": "create", "config": {"name": "y"}})
        assert response.status_code == 200
        registry.create_automation.assert_called_once_with({"name": "y"})


def test_api_automation_action_get() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.get_automation.return_value = {"automationId": "g1"}
        response = client.post("/api/automation", json={"action": "get", "automationId": "g1"})
        assert response.status_code == 200
        registry.get_automation.assert_called_once_with("g1")


def test_api_automation_action_update() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.update_automation.return_value = {"automationId": "u1"}
        response = client.post("/api/automation", json={"action": "update", "automationId": "u1", "patch": {"name": "new"}})
        assert response.status_code == 200
        registry.update_automation.assert_called_once_with("u1", {"name": "new"})


def test_api_automation_action_enable_and_disable() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.set_automation_enabled.return_value = {"enabled": True}
        response = client.post("/api/automation", json={"action": "enable", "automationId": "e1"})
        assert response.status_code == 200
        registry.set_automation_enabled.assert_called_with("e1", True)

        registry.set_automation_enabled.reset_mock()
        registry.set_automation_enabled.return_value = {"enabled": False}
        response = client.post("/api/automation", json={"action": "disable", "automationId": "e1"})
        assert response.status_code == 200
        registry.set_automation_enabled.assert_called_with("e1", False)


def test_api_automation_action_delete() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.delete_automation.return_value = True
        response = client.post("/api/automation", json={"action": "delete", "automationId": "d1"})
        assert response.status_code == 200
        assert response.json()["deleted"] is True
        registry.delete_automation.assert_called_once_with("d1")


def test_api_automation_action_run() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        runner.run_once.return_value = {"runId": "r1"}
        response = client.post(
            "/api/automation",
            json={"action": "run", "automationId": "a1", "confirmed": True, "force": True, "event": {"x": 1}},
        )
        assert response.status_code == 200
        runner.run_once.assert_called_once_with(
            "a1",
            trigger={"type": "manual"},
            event={"x": 1},
            now=None,
            confirmed=True,
            force=True,
        )


def test_api_automation_action_rerun() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        runner.rerun.return_value = {"runId": "rr1"}
        response = client.post("/api/automation", json={"action": "rerun", "runId": "r1", "confirmed": True})
        assert response.status_code == 200
        runner.rerun.assert_called_once_with("r1", confirmed=True)


def test_api_automation_action_list_runs() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        history.list_runs.return_value = [{"runId": "lr1"}]
        response = client.post(
            "/api/automation",
            json={"action": "list_runs", "automationId": "a1", "status": "success", "limit": 10},
        )
        assert response.status_code == 200
        history.list_runs.assert_called_once_with(automation_id="a1", project_id="", status="success", limit=10)


def test_api_automation_action_templates() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.list_templates.return_value = [{"templateId": "t1"}]
        response = client.post("/api/automation", json={"action": "templates"})
        assert response.status_code == 200
        registry.list_templates.assert_called_once()


def test_api_automation_action_create_from_template() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.create_from_template.return_value = {"automationId": "c1"}
        response = client.post(
            "/api/automation",
            json={"action": "create_from_template", "templateId": "t1", "projectId": "p1", "overrides": {"name": "o"}},
        )
        assert response.status_code == 200
        registry.create_from_template.assert_called_once_with("t1", project_id="p1", overrides={"name": "o"})


def test_api_automation_action_simulate() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        scheduler.simulate_trigger.return_value = {"ok": True}
        response = client.post(
            "/api/automation",
            json={"action": "simulate", "automationId": "a1", "now": "2024-01-01T00:00:00Z"},
        )
        assert response.status_code == 200
        scheduler.simulate_trigger.assert_called_once()
        args = scheduler.simulate_trigger.call_args
        assert args[0][0] == "a1"


def test_api_automation_action_run_due() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        scheduler.run_due.return_value = {"ok": True}
        response = client.post("/api/automation", json={"action": "run_due", "confirmed": True})
        assert response.status_code == 200
        scheduler.run_due.assert_called_once()
        args = scheduler.run_due.call_args
        assert args.kwargs["confirmed"] is True


def test_api_automation_action_unsupported() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        response = client.post("/api/automation", json={"action": "unknown"})
        assert response.status_code == 400
        assert response.json()["code"] == ErrorCode.INVALID_PAYLOAD.value


def test_api_automation_get_templates() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.list_templates.return_value = [{"templateId": "t1"}]
        response = client.get("/api/automation/templates")
        assert response.status_code == 200
        registry.list_templates.assert_called_once()


def test_api_automation_create_from_template_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.create_from_template.return_value = {"automationId": "c2"}
        response = client.post("/api/automation/templates/t1", json={"projectId": "p1", "overrides": {"name": "x"}})
        assert response.status_code == 200
        registry.create_from_template.assert_called_once_with("t1", project_id="p1", overrides={"name": "x"})


def test_api_automation_get_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.get_automation.return_value = {"automationId": "g1"}
        response = client.get("/api/automation/g1")
        assert response.status_code == 200
        registry.get_automation.assert_called_once_with("g1")


def test_api_automation_patch_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.update_automation.return_value = {"automationId": "u1"}
        response = client.patch("/api/automation/u1", json={"name": "new"})
        assert response.status_code == 200
        registry.update_automation.assert_called_once_with("u1", {"name": "new"})


def test_api_automation_delete_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        registry.delete_automation.return_value = True
        response = client.delete("/api/automation/d1")
        assert response.status_code == 200
        registry.delete_automation.assert_called_once_with("d1")


def test_api_automation_run_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        runner.run_once.return_value = {"runId": "r1"}
        response = client.post("/api/automation/a1/run", json={"confirmed": True, "force": True, "event": {"x": 1}})
        assert response.status_code == 200
        runner.run_once.assert_called_once_with(
            "a1",
            trigger={"type": "manual"},
            event={"x": 1},
            now=None,
            confirmed=True,
            force=True,
        )


def test_api_automation_run_endpoint_empty_body() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        runner.run_once.return_value = {"runId": "r1"}
        response = client.post("/api/automation/a1/run", content=b"")
        assert response.status_code == 200
        runner.run_once.assert_called_once_with(
            "a1",
            trigger={"type": "manual"},
            event=None,
            now=None,
            confirmed=False,
            force=False,
        )


def test_api_automation_runs_endpoint() -> None:
    with _mocked_automation_client() as (client, registry, runner, scheduler, history):
        history.list_runs.return_value = [{"runId": "lr1"}]
        response = client.get("/api/automation/a1/runs?limit=5")
        assert response.status_code == 200
        history.list_runs.assert_called_once_with(automation_id="a1", limit=5)


def test_automation_id_requires_value() -> None:
    with pytest.raises(AppError) as exc:
        _automation_id({})
    assert exc.value.code == ErrorCode.INVALID_PAYLOAD
    assert _automation_id({"automationId": "a1"}) == "a1"
    assert _automation_id({"id": "a2"}) == "a2"


def test_run_id_requires_value() -> None:
    with pytest.raises(AppError) as exc:
        _run_id({})
    assert exc.value.code == ErrorCode.INVALID_PAYLOAD
    assert _run_id({"runId": "r1"}) == "r1"
    assert _run_id({"id": "r2"}) == "r2"


def test_template_id_requires_value() -> None:
    with pytest.raises(AppError) as exc:
        _template_id({})
    assert exc.value.code == ErrorCode.INVALID_PAYLOAD
    assert _template_id({"templateId": "t1"}) == "t1"
    assert _template_id({"id": "t2"}) == "t2"


def test_automation_payload_extracts_nested_keys() -> None:
    assert _automation_payload({"action": "create", "automation": {"name": "x"}}) == {"name": "x"}
    assert _automation_payload({"action": "create", "config": {"name": "y"}}) == {"name": "y"}
    assert _automation_payload({"action": "create", "name": "z"}) == {"name": "z"}


def test_patch_payload_extracts_nested_keys() -> None:
    assert _patch_payload({"action": "update", "patch": {"name": "x"}}) == {"name": "x"}
    assert _patch_payload({"action": "update", "automation": {"name": "y"}}) == {"name": "y"}
    assert _patch_payload({"action": "update", "config": {"name": "z"}}) == {"name": "z"}
    assert _patch_payload({"action": "update", "automationId": "a1", "name": "w"}) == {"name": "w"}


def test_trigger_payload_defaults_to_manual() -> None:
    assert _trigger_payload({}) == {"type": "manual"}
    assert _trigger_payload({"trigger": {"type": "cron"}}) == {"type": "cron"}
    assert _trigger_payload({"trigger": "not-dict"}) == {"type": "manual"}


def test_now_payload_parses_formats() -> None:
    assert _now_payload({}) is None
    assert _now_payload({"now": ""}) is None
    assert _now_payload({"now": None}) is None
    assert _now_payload({"now": "   "}) is None
    assert _now_payload({"now": 1704067200000}) == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert _now_payload({"now": "2024-01-01T00:00:00Z"}) == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert _now_payload({"now": "2024-01-01T00:00:00"}) == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(AppError) as exc:
        _now_payload({"now": "bad"})
    assert exc.value.code == ErrorCode.INVALID_PAYLOAD


def test_limit_clamps_and_defaults() -> None:
    assert _limit({}) == 100
    assert _limit({"limit": 10}) == 10
    assert _limit({"limit": 5000}) == 2000
    assert _limit({"limit": "abc"}) == 100
    assert _limit({"limit": -5}) == 0


def test_bool_coerces_values() -> None:
    assert _bool({"k": True}, "k") is True
    assert _bool({"k": False}, "k") is False
    assert _bool({"k": "true"}, "k") is True
    assert _bool({"k": "0"}, "k") is False
    assert _bool({"k": "yes"}, "k") is True
    assert _bool({}, "k") is False
    assert _bool({}, "k", default=True) is True
