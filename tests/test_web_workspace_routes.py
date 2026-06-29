from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import ErrorCode


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
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, response
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


# --- route registration ---


def test_workspace_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    for expected in {
        "/api/projects",
        "/api/project-files",
        "/api/workspace/projects",
        "/api/workspace/projects/{project_id}",
        "/api/workspace/projects/{project_id}/conversations",
        "/api/workspace/projects/{project_id}/saved-items",
        "/api/workspace/projects/{project_id}/saved-items/{saved_id}",
        "/api/workspace/projects/{project_id}/artifacts",
        "/api/workspace/projects/{project_id}/artifacts/{artifact_id}",
        "/api/workspace/artifacts/{artifact_id}/preview",
        "/api/workspace/artifacts/{artifact_id}/download",
        "/api/workspace/exports",
        "/api/workspace/exports/{export_id}/download",
    }:
        assert expected in paths


# --- auth enforcement ---


def test_workspace_projects_auth_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/workspace/projects")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_workspace_saved_items_auth_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/workspace/projects/p1/saved-items")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_workspace_artifacts_auth_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/workspace/projects/p1/artifacts")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_workspace_exports_auth_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/workspace/exports", body=b"{}")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- legacy projects action API ---


def test_projects_action_list_invokes_backend() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/projects",
            body=b'{"action":"list"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert "projects" in result


def test_projects_action_rejects_invalid() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/projects",
            body=b'{"action":"nope"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


# --- server_module patch compatibility ---


def test_workspace_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._workspace_route_deps)
    assert hasattr(server_module, "read_multipart_files")
