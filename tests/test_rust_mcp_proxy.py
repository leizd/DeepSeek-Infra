"""Integration tests for optional Rust MCP protocol preparation."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

import deepseek_infra.web.server as server_module
from deepseek_infra.infra.mcp.protocol_preparation import prepare_mcp_protocol
from deepseek_infra.infra.rust_core import mcp_client
from deepseek_infra.infra.rust_core.mcp_client import McpProxyResult, fallback_to_python_enabled


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
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(
            "POST",
            "/mcp",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {server_module.settings.auth.token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, response
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _clear_rust_mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DEEPSEEK_RUST_MCP", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_MCP_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_MCP_TIMEOUT_MS", raising=False)
    yield


def _rust_success(payload: Any) -> McpProxyResult:
    return McpProxyResult(ok=True, status=200, body=prepare_mcp_protocol(payload), latency_ms=2)


def test_fallback_compatibility_setting_is_still_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fallback_to_python_enabled() is True
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_FALLBACK", "0")
    assert fallback_to_python_enabled() is False


def test_rust_mcp_disabled_uses_python_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "0")
    payload = {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}}
    with patch.object(mcp_client, "prepare_mcp_with_rust") as rust, _running_server() as server:
        status, body, response = _request(server, payload)
    rust.assert_not_called()
    assert status == 200
    assert body is not None and body["result"]["serverInfo"]["name"] == "deepseek-infra"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Runtime") == "python"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback") == "0"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        ("tools/list", {}),
        ("resources/list", {}),
        ("prompts/list", {}),
    ],
)
def test_rust_mcp_prepare_success_routes_to_python(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict[str, Any],
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    with patch.object(mcp_client, "prepare_mcp_with_rust", side_effect=_rust_success) as rust, _running_server() as server:
        status, body, response = _request(server, payload)
    rust.assert_called_once_with(payload)
    assert status == 200
    assert body is not None and body["id"] == method and "result" in body
    assert response.getheader("X-DeepSeek-MCP-Preparation-Runtime") == "rust"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback") == "0"


def test_rust_mcp_never_executes_tool_and_python_executes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {
        "jsonrpc": "2.0",
        "id": "call",
        "method": "tools/call",
        "params": {"name": "proof-tool", "arguments": {"value": "中文🚀"}},
    }
    python_result = {
        "content": [{"type": "text", "text": "python-only"}],
        "structuredContent": {"ok": True, "owner": "python"},
        "isError": False,
    }
    with patch.object(mcp_client, "prepare_mcp_with_rust", side_effect=_rust_success), patch(
        "deepseek_infra.infra.mcp.server.call_hub_tool", return_value=python_result
    ) as execute, _running_server() as server:
        status, body, response = _request(server, payload)
    execute.assert_called_once_with("proof-tool", {"value": "中文🚀"}, meta=None)
    assert status == 200
    assert body is not None and body["result"]["structuredContent"]["owner"] == "python"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Runtime") == "rust"


@pytest.mark.parametrize(
    "reason",
    ["rust_backend_unavailable", "rust_backend_timeout", "rust_empty_response", "rust_malformed_json"],
)
def test_rust_backend_failure_falls_back_to_python(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {"jsonrpc": "2.0", "id": "fallback", "method": "ping"}
    failure = McpProxyResult(ok=False, status=0, body=None, error_kind=reason, latency_ms=1)
    with patch.object(mcp_client, "prepare_mcp_with_rust", return_value=failure), _running_server() as server:
        status, body, response = _request(server, payload)
    assert status == 200
    assert body is not None and body["result"] == {}
    assert response.getheader("X-DeepSeek-MCP-Preparation-Runtime") == "python"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback") == "1"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback-Reason") == reason


def test_rust_invalid_contract_cannot_enter_python_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {"jsonrpc": "2.0", "id": "safe", "method": "ping"}
    malicious = {
        "ok": True,
        "messageType": "request",
        "request": {**payload, "Authorization": "Bearer injected"},
        "routing": {"owner": "rust", "category": "tools"},
    }
    with patch.object(
        mcp_client,
        "prepare_mcp_with_rust",
        return_value=McpProxyResult(ok=True, status=200, body=malicious),
    ), _running_server() as server:
        status, body, response = _request(server, payload)
    assert status == 200
    assert body is not None and body["result"] == {}
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback-Reason") == "rust_routing_owner_invalid"


def test_protocol_user_error_is_not_backend_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {"jsonrpc": "2.0", "id": "bad", "method": "tools/call", "params": {}}
    with patch.object(mcp_client, "prepare_mcp_with_rust") as rust, _running_server() as server:
        status, body, response = _request(server, payload)
    rust.assert_not_called()
    assert status == 200
    assert body is not None and body["error"]["data"]["code"] == "invalid_params"
    assert response.getheader("X-DeepSeek-MCP-Preparation-Fallback") == "0"


def test_initialized_notification_returns_202_after_rust_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    with patch.object(mcp_client, "prepare_mcp_with_rust", side_effect=_rust_success), _running_server() as server:
        status, body, response = _request(server, payload)
    assert status == 202
    assert body is None
    assert response.getheader("X-DeepSeek-MCP-Preparation-Runtime") == "rust"


def test_rust_mcp_route_is_registered() -> None:
    server, _ = server_module.create_server(0, host="127.0.0.1")
    try:
        paths = _collect_route_paths(server.app.routes)
    finally:
        server.server_close()
    assert "/mcp" in paths
