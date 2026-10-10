from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.gateway.edge_inference import EdgeRouteDecision


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


def test_edge_route_is_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/edge/reload" in paths
    assert "/api/edge/route-preview" in paths


# --- auth enforcement ---


def test_edge_reload_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/edge/reload",
            body=b'{"action":"unload"}',
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_edge_route_preview_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/edge/route-preview",
            body=b'{"messages":[{"role":"user","content":"hello"}]}',
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- valid payloads ---


def test_edge_reload_unload_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "edge_unload", return_value={"ok": True}) as unload:
        status, data, _ = _request(
            server, "POST", "/api/edge/reload",
            body=b'{"action":"unload"}',
            headers=_auth_headers(),
        )

    assert status == 200
    assert json.loads(data.decode("utf-8")) == {"ok": True}
    unload.assert_called_once()


def test_edge_reload_reload_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "edge_unload", return_value={"ok": True}) as unload:
        status, data, _ = _request(
            server, "POST", "/api/edge/reload",
            body=b'{"action":"reload"}',
            headers=_auth_headers(),
        )

    assert status == 200
    unload.assert_called_once()


def test_edge_route_preview_returns_routing_decision() -> None:
    route = EdgeRouteDecision(True, "simple_task_local", "auto", "fake", {"available": True, "provider": "fake"})
    with _running_server() as server, patch.object(server_module, "edge_route_for_payload", return_value=route) as preview:
        status, data, _ = _request(
            server, "POST", "/api/edge/route-preview",
            body=b'{"messages":[{"role":"user","content":"hello"}]}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 200
    assert payload["useEdge"] is True
    assert payload["reason"] == "simple_task_local"
    assert payload["provider"] == "fake"
    assert payload["status"]["available"] is True
    preview.assert_called_once()


# --- invalid payload ---


def test_edge_reload_rejects_invalid_action() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/edge/reload",
            body=b'{"action":"nope"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


def test_edge_route_preview_surfaces_forced_local_unavailable_409() -> None:
    error = AppError("Edge unavailable", code=ErrorCode.INVALID_PAYLOAD, status=409)
    with _running_server() as server, patch.object(server_module, "edge_route_for_payload", side_effect=error):
        status, data, _ = _request(
            server, "POST", "/api/edge/route-preview",
            body=b'{"edgeMode":"local","messages":[{"role":"user","content":"hello"}]}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 409
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


# --- server_module patch compatibility ---


def test_edge_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._edge_route_deps)
    assert hasattr(server_module, "edge_unload")
