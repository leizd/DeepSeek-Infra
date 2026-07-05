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
