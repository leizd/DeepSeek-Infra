from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

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


def test_mcp_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/mcp/external/tools" in paths
    assert "/mcp" in paths


# --- auth enforcement ---


def test_mcp_endpoint_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/mcp", body=b'{"jsonrpc":"2.0","id":1}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_mcp_external_tools_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/mcp/external/tools")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- MCP notification ---


def test_mcp_notification_returns_202() -> None:
    with _running_server() as server, patch.object(server_module, "handle_mcp_message", return_value=None):
        status, data, _ = _request(
            server, "POST", "/mcp",
            body=b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            headers=_auth_headers(),
        )

    assert status == 202
    assert data == b""


# --- MCP JSON-RPC request ---


def test_mcp_jsonrpc_returns_response() -> None:
    response = {"jsonrpc": "2.0", "result": {"tools": []}, "id": 1}
    with _running_server() as server, patch.object(server_module, "handle_mcp_message", return_value=response):
        status, data, _ = _request(
            server, "POST", "/mcp",
            body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            headers=_auth_headers(),
        )

    assert status == 200
    assert json.loads(data.decode("utf-8")) == response


# --- MCP external tools ---


def test_mcp_external_tools_returns_ok() -> None:
    with _running_server() as server, patch.object(server_module, "_list_external_mcp_tools", return_value={"ok": True, "servers": [], "tools": []}):
        status, data, _ = _request(
            server, "GET", "/api/mcp/external/tools",
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["servers"] == []
    assert result["tools"] == []


# --- server_module patch compatibility ---


def test_mcp_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._mcp_route_deps)
    assert hasattr(server_module, "handle_mcp_message")
