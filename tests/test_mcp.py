from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import deepseek_infra.infra.mcp.permissions as mcp_permissions
import deepseek_infra.infra.mcp.registry as mcp_registry
import deepseek_infra.infra.tool_runtime.generated_files as generated_files
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.mcp.client import MCPClient
from deepseek_infra.infra.mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    handle_mcp_message,
    mcp_status,
)


def rpc(method: str, params: dict[str, Any] | None = None, message_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def result_of(response: dict[str, Any] | None) -> dict[str, Any]:
    assert response is not None
    assert "error" not in response, response
    result = response["result"]
    assert isinstance(result, dict)
    return result


def test_initialize_handshake_reports_protocol_and_capabilities() -> None:
    result = result_of(handle_mcp_message(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})))
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "deepseek-infra"
    assert "tools" in result["capabilities"]
    assert "resources" in result["capabilities"]
    assert "prompts" in result["capabilities"]
    # The follow-up notification produces no response body.
    assert handle_mcp_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_runtime_tools_with_schemas_and_annotations() -> None:
    tools = result_of(handle_mcp_message(rpc("tools/list")))["tools"]
    names = {tool["name"] for tool in tools}
    assert len(tools) == 17
    assert {"web_search", "create_pptx", "python_eval", "forget_memory"} <= names
    by_name = {tool["name"]: tool for tool in tools}
    # OpenAI parameter schemas pass through as MCP inputSchema.
    assert by_name["web_search"]["inputSchema"]["required"] == ["query", "intent"]
    # Risk-card annotations: network tools are open-world, generators are not read-only.
    assert by_name["fetch_url"]["annotations"]["openWorldHint"] is True
    assert by_name["create_pptx"]["annotations"]["readOnlyHint"] is False
    assert by_name["forget_memory"]["annotations"]["destructiveHint"] is True


def test_capability_scoping_narrows_catalog_and_blocks_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_permissions, "MCP_CAPABILITY", "researcher")
    tools = result_of(handle_mcp_message(rpc("tools/list")))["tools"]
    assert {tool["name"] for tool in tools} == {"web_search", "compare_search_results", "fetch_url"}
    # Out-of-slice execution is denied by the policy engine (tool error, not protocol error).
    call = result_of(handle_mcp_message(rpc("tools/call", {"name": "python_eval", "arguments": {"expression": "1+1"}})))
    assert call["isError"] is True
    assert "capability_denied" in json.dumps(call["structuredContent"])


def test_tools_call_executes_local_tool() -> None:
    call = result_of(
        handle_mcp_message(
            rpc("tools/call", {"name": "data_transform", "arguments": {"operation": "number_summary", "input": "1 2 3 4"}})
        )
    )
    assert call["isError"] is False
    structured = call["structuredContent"]
    assert structured["ok"] is True
    assert structured["result"]["count"] == 4
    # The text content part carries the same stable JSON for non-structured clients.
    text_payload = json.loads(call["content"][0]["text"])
    assert text_payload["tool"] == "data_transform"


def test_tools_call_keeps_policy_security_guards() -> None:
    call = result_of(handle_mcp_message(rpc("tools/call", {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}})))
    assert call["isError"] is True
    assert "ssrf_blocked" in json.dumps(call["structuredContent"])
    unknown = result_of(handle_mcp_message(rpc("tools/call", {"name": "rm_rf", "arguments": {}})))
    assert unknown["isError"] is True


def test_jsonrpc_error_codes() -> None:
    no_method = handle_mcp_message({"jsonrpc": "2.0", "id": 5})
    assert no_method is not None and no_method["error"]["code"] == INVALID_REQUEST
    bad_version = handle_mcp_message({"jsonrpc": "1.0", "id": 6, "method": "ping"})
    assert bad_version is not None and bad_version["error"]["code"] == INVALID_REQUEST
    unknown = handle_mcp_message(rpc("does/not-exist"))
    assert unknown is not None and unknown["error"]["code"] == METHOD_NOT_FOUND
    missing_name = handle_mcp_message(rpc("tools/call", {}))
    assert missing_name is not None and missing_name["error"]["code"] == INVALID_PARAMS
    assert result_of(handle_mcp_message(rpc("ping"))) == {}


def test_resources_list_and_read_generated_artifact(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    generated_dir = tmp_path / ".generated"
    generated_dir.mkdir()
    file_id = "a" * 32
    (generated_dir / f"{file_id}.svg").write_text("<svg>diagram</svg>", encoding="utf-8")
    monkeypatch.setattr(mcp_registry, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(generated_files, "GENERATED_DIR", generated_dir)

    resources = result_of(handle_mcp_message(rpc("resources/list")))["resources"]
    uris = {resource["uri"] for resource in resources}
    assert "runtime://capabilities" in uris
    assert f"generated://{file_id}" in uris

    svg = result_of(handle_mcp_message(rpc("resources/read", {"uri": f"generated://{file_id}"})))["contents"][0]
    assert svg["mimeType"] == "image/svg+xml"
    assert "diagram" in svg["text"]

    capabilities = result_of(handle_mcp_message(rpc("resources/read", {"uri": "runtime://capabilities"})))["contents"][0]
    assert json.loads(capabilities["text"])["capability"] == "full"

    missing = handle_mcp_message(rpc("resources/read", {"uri": "generated://" + "f" * 32}))
    assert missing is not None and missing["error"]["data"]["code"] == "not_found"


def test_prompts_list_and_get() -> None:
    prompts = result_of(handle_mcp_message(rpc("prompts/list")))["prompts"]
    assert {prompt["name"] for prompt in prompts} == {"slides-outline", "research-brief"}
    rendered = result_of(handle_mcp_message(rpc("prompts/get", {"name": "slides-outline", "arguments": {"topic": "CLOCK 算法"}})))
    text = rendered["messages"][0]["content"]["text"]
    assert "CLOCK 算法" in text
    assert "create_pptx" in text
    unknown = handle_mcp_message(rpc("prompts/get", {"name": "nope"}))
    assert unknown is not None and "error" in unknown


def test_mcp_status_shape() -> None:
    status = mcp_status()
    assert status["enabled"] is True
    assert status["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert status["endpoint"] == "/mcp"
    assert status["toolCount"] == 17
    assert status["client"]["enabled"] is False


class _FakeHeaders:
    def __init__(self, items: dict[str, str]) -> None:
        self._items = items

    def items(self) -> list[tuple[str, str]]:
        return list(self._items.items())


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = _FakeHeaders(headers or {})

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _loopback_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
    """Route the client's HTTP POST straight into our own MCP server."""
    message = json.loads(request.data.decode("utf-8"))
    response = handle_mcp_message(message)
    if response is None:
        return _FakeResponse(b"", {"Mcp-Session-Id": "session-1"})
    return _FakeResponse(json.dumps(response).encode("utf-8"), {"Mcp-Session-Id": "session-1"})


def test_client_initialize_list_and_call_roundtrip() -> None:
    client = MCPClient("http://127.0.0.1:9/mcp", name="loopback")
    with patch("urllib.request.urlopen", side_effect=_loopback_urlopen):
        info = client.initialize()
        assert info["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert client.session_id == "session-1"
        tools = client.list_tools()
        assert len(tools) == 17
        result = client.call_tool("data_transform", {"operation": "number_summary", "input": "5 5"})
        assert result["isError"] is False


def test_client_raises_apperror_on_rpc_error_and_unreachable() -> None:
    client = MCPClient("http://127.0.0.1:9/mcp", name="loopback")
    with patch("urllib.request.urlopen", side_effect=_loopback_urlopen):
        with pytest.raises(AppError):
            client.call_tool("", {})  # name required -> JSON-RPC error -> AppError
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        with pytest.raises(AppError):
            client.list_tools()
