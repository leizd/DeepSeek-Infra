from __future__ import annotations

import io
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.mcp import client, registry


class _Response332:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self) -> "_Response332":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_mcp_sse_parser_skips_comments_invalid_and_non_objects() -> None:
    assert client._parse_sse_jsonrpc(": ping\n\ndata: bad\n\ndata: []\n\ndata: {\"result\":\ndata: {\"ok\":true}}") == {"result": {"ok": True}}
    assert client._parse_sse_jsonrpc("data: bad") is None


def test_mcp_client_empty_invalid_and_sse_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = client.MCPClient("https://mcp.test", max_retries=0)
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response332(b""))
    assert mcp._post({"jsonrpc": "2.0"})[0] is None
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response332(b"[]"))
    assert mcp._post({"jsonrpc": "2.0"})[0] is None
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response332(b"not-json"))
    with pytest.raises(AppError, match="invalid JSON"):
        mcp._post({"jsonrpc": "2.0"})
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response332(b'data: {"result":{"ok":true}}\n\n', {"Content-Type": "text/event-stream"}),
    )
    assert mcp._post({"jsonrpc": "2.0"})[0] == {"result": {"ok": True}}


def test_mcp_client_http_and_unreachable_retry_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = client.MCPClient("https://mcp.test", name="demo", max_retries=1, retry_backoff_seconds=0)
    attempts = 0

    def retry_http(*_args: object, **_kwargs: object) -> _Response332:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError("https://mcp.test", 503, "down", Message(), io.BytesIO())
        return _Response332(b'{"result":{}}')

    monkeypatch.setattr(client.urllib.request, "urlopen", retry_http)
    assert mcp._post({})[0] == {"result": {}}
    assert mcp.last_stats.retry_count == 1

    error = urllib.error.HTTPError("https://mcp.test", 400, "bad", Message(), io.BytesIO())
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(AppError, match="HTTP 400"):
        mcp._post({})
    assert mcp.last_stats.error_type == "http_error"

    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(AppError, match="unreachable"):
        mcp._post({})
    assert mcp.last_stats.timeout is True


def test_mcp_client_rpc_headers_notifications_and_tool_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = client.MCPClient("https://mcp.test", extra_headers={"Authorization": "secret"})
    mcp.protocol_version = "v1"
    mcp.session_id = "session"
    assert mcp._headers()["MCP-Protocol-Version"] == "v1"
    assert mcp._headers()["Mcp-Session-Id"] == "session"
    assert mcp._headers()["Authorization"] == "secret"
    monkeypatch.setattr(mcp, "_post", lambda _message: (None, {}))
    with pytest.raises(AppError, match="no response"):
        mcp._rpc("test")
    monkeypatch.setattr(mcp, "_post", lambda _message: ({"error": {"code": -1, "message": "bad"}}, {}))
    with pytest.raises(AppError, match="error -1"):
        mcp._rpc("test")
    monkeypatch.setattr(mcp, "_post", lambda _message: ({"result": []}, {"mcp-session-id": "new"}))
    assert mcp._rpc("test") == {}
    assert mcp.session_id == "new"
    monkeypatch.setattr(mcp, "_post", lambda _message: (_ for _ in ()).throw(AppError("ignored")))
    mcp._notify("event")
    monkeypatch.setattr(mcp, "_rpc", lambda *_args, **_kwargs: {"tools": [None, {"name": "ok"}]})
    assert mcp.list_tools() == [{"name": "ok"}]


def test_mcp_client_initialize_call_and_config_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = client.MCPClient("https://mcp.test")
    calls: list[tuple[str, object]] = []

    def rpc(method: str, params: object = None) -> dict[str, object]:
        calls.append((method, params))
        return {"protocolVersion": "2025-test"} if method == "initialize" else {"ok": True}

    monkeypatch.setattr(mcp, "_rpc", rpc)
    monkeypatch.setattr(mcp, "_notify", lambda method: calls.append((method, None)))
    assert mcp.initialize()["protocolVersion"] == "2025-test"
    assert mcp.protocol_version == "2025-test"
    assert mcp.call_tool("echo", None) == {"ok": True}
    assert client.configured_clients() == []
    assert client._looks_like_timeout(urllib.error.URLError("socket timeout"))
    assert not client._looks_like_timeout(OSError("refused"))


def test_mcp_registry_tools_skip_malformed_and_external_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "allowed_tool_names", lambda: ["ok", "external"])
    monkeypatch.setattr(
        registry,
        "available_tool_definitions",
        lambda: [{"function": "bad"}, {"function": {}}, {"function": {"name": "blocked"}}, {"function": {"name": "ok", "parameters": []}}],
    )
    monkeypatch.setattr(registry, "hub_capability", lambda: "full")
    from deepseek_infra.infra.mcp import bridge

    monkeypatch.setattr(bridge.external_mcp_registry, "list_profiles", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert registry.mcp_tools() == [{"name": "ok", "description": "", "inputSchema": {"type": "object"}}]


def test_mcp_registry_external_schema_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "allowed_tool_names", lambda: [])
    monkeypatch.setattr(registry, "available_tool_definitions", lambda: [])
    monkeypatch.setattr(registry, "hub_capability", lambda: "full")
    from deepseek_infra.infra.mcp import bridge

    profile = SimpleNamespace(
        server="remote",
        tool="echo",
        bridged_name="external",
        input_schema={"message": {"type": "string"}},
        requires_approval=True,
        network=True,
    )
    monkeypatch.setattr(bridge.external_mcp_registry, "list_profiles", lambda: [profile])
    tool = registry.mcp_tools()[0]
    assert tool["inputSchema"]["type"] == "object"
    assert tool["annotations"]["destructiveHint"] is True


def test_mcp_registry_disabled_resources_prompts_and_bad_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "MCP_EXPOSE_RESOURCES", False)
    assert registry.mcp_resources() == []
    with pytest.raises(AppError) as caught:
        registry.read_mcp_resource("runtime://capabilities")
    assert caught.value.code == ErrorCode.FORBIDDEN
    monkeypatch.setattr(registry, "MCP_EXPOSE_RESOURCES", True)
    monkeypatch.setattr(registry, "resolve_generated_file", lambda _file_id: None)
    with pytest.raises(AppError):
        registry.read_mcp_resource("generated://missing")
    with pytest.raises(AppError):
        registry.read_mcp_resource("unknown://x")
    monkeypatch.setattr(registry, "MCP_EXPOSE_PROMPTS", False)
    assert registry.mcp_prompts() == []
    with pytest.raises(AppError):
        registry.get_mcp_prompt("slides-outline")


def test_mcp_registry_resource_text_blob_and_prompt_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svg = tmp_path / "map.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    binary = tmp_path / "deck.pptx"
    binary.write_bytes(b"pptx")
    monkeypatch.setattr(registry, "MCP_EXPOSE_RESOURCES", True)
    monkeypatch.setattr(registry, "resolve_generated_file", lambda file_id: svg if file_id == "svg" else binary)
    assert registry.read_mcp_resource("generated://svg")[0]["text"] == "<svg/>"
    assert registry.read_mcp_resource("generated://pptx")[0]["blob"]
    monkeypatch.setattr(registry, "MCP_EXPOSE_PROMPTS", True)
    slides = registry.get_mcp_prompt("slides-outline", {"topic": "T", "audience": "A"})
    research = registry.get_mcp_prompt("research-brief", {})
    assert "T" in slides["messages"][0]["content"]["text"]
    assert research["messages"]
    with pytest.raises(AppError):
        registry.get_mcp_prompt("missing")
