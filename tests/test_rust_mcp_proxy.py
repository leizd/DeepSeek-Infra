"""Tests for Rust MCP opt-in proxy integration."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest

import deepseek_infra.web.server as server_module
from deepseek_infra.infra.rust_core.mcp_client import (
    McpProxyResult,
    fallback_to_python_enabled,
)


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


@pytest.fixture(autouse=True)
def _clear_rust_mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DEEPSEEK_RUST_MCP", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_MCP_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_MCP_TIMEOUT_MS", raising=False)
    yield


# --- config ---


def test_fallback_enabled_by_default() -> None:
    assert fallback_to_python_enabled() is True


def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_FALLBACK", "0")
    assert fallback_to_python_enabled() is False


# --- route behavior ---


def _initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
        "id": "init-1",
    }


def _tools_list_payload() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "tools/list", "id": "list-1"}


def _tools_call_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "hi"}},
        "id": "call-1",
    }


def test_rust_mcp_proxy_disabled_uses_python_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "0")
    with patch("deepseek_infra.web.routes.mcp.proxy_mcp_to_rust") as proxy, patch(
        "deepseek_infra.web.routes.mcp.rust_mcp_enabled", return_value=False
    ):
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_initialize_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_not_called()
    assert status == HTTPStatus.OK
    payload = json.loads(data.decode("utf-8"))
    assert payload.get("jsonrpc") == "2.0"


def test_rust_mcp_proxy_enabled_forwards_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    fake_response = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "deepseek-mcp-rs", "version": "0.1.0"},
            "capabilities": {},
        },
    }
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_initialize_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["result"]["serverInfo"]["name"] == "deepseek-mcp-rs"


def test_rust_mcp_proxy_enabled_forwards_tools_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    fake_response = {
        "jsonrpc": "2.0",
        "id": "list-1",
        "result": {"tools": [{"name": "calculator"}]},
    }
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_tools_list_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["result"]["tools"][0]["name"] == "calculator"


def test_rust_mcp_proxy_enabled_forwards_tools_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    fake_response = {
        "jsonrpc": "2.0",
        "id": "call-1",
        "result": {
            "content": [{"type": "text", "text": "echo: hi"}],
            "isError": False,
        },
    }
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_tools_call_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["result"]["content"][0]["text"] == "echo: hi"


def test_rust_mcp_unreachable_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=False, status=0, body="connection refused"),
    ):
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_initialize_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload.get("jsonrpc") == "2.0"


def test_rust_mcp_no_fallback_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_FALLBACK", "0")
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=False, status=0, body="connection refused"),
    ):
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_initialize_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.BAD_GATEWAY
    assert payload["code"] == "upstream_failure"


def test_rust_mcp_invalid_payload_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    with _running_server() as server:
        status, data, _ = _request(
            server,
            "POST",
            "/mcp",
            body=b"not json",
            headers=_auth_headers(),
        )
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.BAD_REQUEST
    assert payload["code"] == "invalid_payload"


def test_rust_mcp_preserves_authorization_header_if_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    fake_response = {"jsonrpc": "2.0", "id": "init-1", "result": {}}
    with patch(
        "deepseek_infra.web.routes.mcp.proxy_mcp_to_rust",
        return_value=McpProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            _request(
                server,
                "POST",
                "/mcp",
                body=json.dumps(_initialize_payload()).encode("utf-8"),
                headers=_auth_headers(),
            )
        call_kwargs = proxy.call_args.kwargs
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"].startswith("Bearer ")


def test_rust_mcp_route_is_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)
    assert "/mcp" in paths
