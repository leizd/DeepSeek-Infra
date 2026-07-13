from __future__ import annotations

import json
import logging
import urllib.error
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.infra.mcp.protocol_preparation import (
    MCP_PROTOCOL_PREPARATION_MAX_BYTES,
    McpProtocolDecision,
    log_mcp_protocol_diagnostics,
    prepare_mcp_protocol,
    prepare_mcp_protocol_json,
    prepare_mcp_protocol_with_optional_rust,
    protocol_diagnostic_headers,
    protocol_error_response,
    request_id_type,
)
from deepseek_infra.infra.rust_core import mcp_client


def _payload(method: str = "tools/list", *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        value["params"] = params
    return value


def _rust_result(body: Any, *, ok: bool = True, kind: str = "", latency: int = 3) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, status=200 if ok else 0, body=body, error_kind=kind, latency_ms=latency)


def test_protocol_prepares_supported_requests_notifications_and_responses() -> None:
    request = prepare_mcp_protocol(_payload("resources/read", params={"uri": " runtime://capabilities "}))
    assert request["ok"] is True
    assert request["messageType"] == "request"
    assert request["request"]["params"]["uri"] == "runtime://capabilities"
    assert request["routing"] == {"owner": "python", "category": "resources"}

    notification = prepare_mcp_protocol({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification["messageType"] == "notification"
    assert notification["routing"]["category"] == "lifecycle"

    response = prepare_mcp_protocol({"jsonrpc": "2.0", "id": None, "result": {"ok": True}})
    assert response["messageType"] == "response"
    assert response["routing"]["owner"] == "python"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "invalid_request"),
        ({}, "invalid_jsonrpc_version"),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, "invalid_jsonrpc_version"),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, "invalid_request_id"),
        ({"jsonrpc": "2.0", "id": 1, "method": 7}, "invalid_method"),
        ({"jsonrpc": "2.0", "id": 1, "method": "unknown"}, "method_not_supported"),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, "invalid_params"),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": []}},
            "invalid_capabilities",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "old"}},
            "invalid_protocol_version",
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {}}},
            "invalid_initialize_request",
        ),
    ],
)
def test_protocol_stable_error_categories(payload: Any, code: str) -> None:
    result = prepare_mcp_protocol(payload)
    assert result["ok"] is False
    assert result["code"] == code
    assert isinstance(result["jsonRpcCode"], int)


def test_protocol_raw_parse_size_depth_and_serialization_errors() -> None:
    assert prepare_mcp_protocol_json("{")["code"] == "parse_error"
    assert prepare_mcp_protocol_json(b"\xff")["code"] == "parse_error"
    assert prepare_mcp_protocol_json('{"x":NaN}')["code"] == "parse_error"
    assert prepare_mcp_protocol_json(b"x" * (MCP_PROTOCOL_PREPARATION_MAX_BYTES + 1))["code"] == "request_too_large"

    nested: Any = "leaf"
    for _ in range(40):
        nested = {"next": nested}
    assert prepare_mcp_protocol(_payload("tools/call", params={"name": "x", "arguments": nested}))["code"] == "nesting_limit_exceeded"
    assert prepare_mcp_protocol({"jsonrpc": "2.0", "id": 1, "method": "ping", "value": {1, 2}})["code"] == "invalid_request"


def test_protocol_preserves_tool_arguments_byte_semantics() -> None:
    arguments = {"query": "Rust MCP 中文🚀", "nested": {"items": [1, True, None, "二"]}}
    result = prepare_mcp_protocol(_payload("tools/call", params={"name": " search ", "arguments": arguments}))
    assert result["ok"] is True
    assert result["request"]["params"]["name"] == "search"
    assert result["request"]["params"]["arguments"] == arguments
    assert json.dumps(result["request"]["params"]["arguments"], sort_keys=True, ensure_ascii=False) == json.dumps(
        arguments, sort_keys=True, ensure_ascii=False
    )


def test_rust_mcp_prepare_success() -> None:
    payload = _payload("tools/list")
    local = prepare_mcp_protocol(payload)
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client, "prepare_mcp_with_rust", return_value=_rust_result(local)
    ):
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    assert decision.preparation == local
    assert decision.diagnostics["runtime"] == "rust"
    assert decision.diagnostics["fallback"] is False


def test_rust_mcp_disabled_uses_python() -> None:
    payload = _payload()
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=False), patch.object(
        mcp_client, "prepare_mcp_with_rust"
    ) as rust:
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    rust.assert_not_called()
    assert decision.diagnostics["runtime"] == "python"
    assert decision.diagnostics["fallback"] is False


@pytest.mark.parametrize(
    "reason",
    [
        "rust_backend_unavailable",
        "rust_backend_timeout",
        "rust_empty_response",
        "rust_malformed_json",
        "rust_http_failure",
    ],
)
def test_rust_mcp_backend_failures_fall_back(reason: str) -> None:
    payload = _payload()
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client,
        "prepare_mcp_with_rust",
        return_value=_rust_result(None, ok=False, kind=reason),
    ):
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    assert decision.preparation == prepare_mcp_protocol(payload)
    assert decision.diagnostics["runtime"] == "python"
    assert decision.diagnostics["fallback"] is True
    assert decision.diagnostics["fallbackReason"] == reason


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ([], "rust_response_not_object"),
        ({"ok": False}, "rust_contract_invalid"),
        ({"ok": True, "messageType": "mystery", "routing": {"owner": "python"}}, "rust_message_type_invalid"),
        (
            {"ok": True, "messageType": "request", "request": {}, "routing": {"owner": "rust", "category": "tools"}},
            "rust_routing_owner_invalid",
        ),
    ],
)
def test_rust_mcp_invalid_contract_falls_back(candidate: Any, reason: str) -> None:
    payload = _payload()
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client, "prepare_mcp_with_rust", return_value=_rust_result(candidate)
    ):
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    assert decision.preparation == prepare_mcp_protocol(payload)
    assert decision.diagnostics["fallbackReason"] == reason


def test_rust_mcp_semantic_divergence_falls_back() -> None:
    payload = _payload("tools/list")
    candidate = prepare_mcp_protocol(payload)
    candidate["routing"]["category"] = "prompts"
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client, "prepare_mcp_with_rust", return_value=_rust_result(candidate)
    ):
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    assert decision.diagnostics["fallbackReason"] == "rust_semantic_divergence"
    assert decision.preparation["routing"]["category"] == "tools"


def test_rust_mcp_tool_arguments_are_preserved_or_fall_back() -> None:
    arguments = {"query": "unchanged", "secret-shaped-user-data": "preserve"}
    payload = _payload("tools/call", params={"name": "search", "arguments": arguments})
    candidate = prepare_mcp_protocol(payload)
    candidate["request"]["params"]["arguments"] = {"query": "changed"}
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client, "prepare_mcp_with_rust", return_value=_rust_result(candidate)
    ):
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    assert decision.diagnostics["fallbackReason"] == "rust_tool_arguments_changed"
    assert decision.preparation["request"]["params"]["arguments"] == arguments


def test_rust_mcp_user_error_remains_user_error() -> None:
    payload = _payload("tools/call", params={})
    with patch.object(mcp_client, "rust_mcp_enabled", return_value=True), patch.object(
        mcp_client, "prepare_mcp_with_rust"
    ) as rust:
        decision = prepare_mcp_protocol_with_optional_rust(payload)
    rust.assert_not_called()
    assert decision.preparation["code"] == "invalid_params"
    assert decision.diagnostics["fallback"] is False


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("side_effect", "body", "kind"),
    [
        (urllib.error.URLError("connection refused"), None, "rust_backend_unavailable"),
        (TimeoutError("timed out"), None, "rust_backend_timeout"),
        (None, b"", "rust_empty_response"),
        (None, b"{", "rust_malformed_json"),
        (None, b"[]", "rust_response_not_object"),
    ],
)
def test_rust_mcp_transport_failure_classification(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: BaseException | None,
    body: bytes | None,
    kind: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mocked = patch("urllib.request.urlopen", side_effect=side_effect) if side_effect else patch(
        "urllib.request.urlopen", return_value=_Response(body or b"")
    )
    with mocked:
        result = mcp_client.prepare_mcp_with_rust(_payload())
    assert result.ok is False
    assert result.error_kind == kind


def test_rust_mcp_never_receives_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        local = prepare_mcp_protocol(captured["body"])
        return _Response(json.dumps(local).encode("utf-8"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = mcp_client.proxy_mcp_to_rust(
            _payload(), headers={"Authorization": "Bearer should-never-reach-rust", "X-Token": "secret"}
        )
    assert result.ok is True
    assert "Authorization" not in captured["headers"]
    assert "X-token" not in captured["headers"]
    assert "should-never-reach-rust" not in json.dumps(captured["body"])


def test_rust_mcp_diagnostics_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    diagnostics = {
        "method": "tools/call",
        "messageType": "request",
        "requestIdType": "string",
        "payloadSize": 123,
        "runtime": "rust",
        "fallback": False,
        "fallbackReason": "",
        "latencyMs": 4,
    }
    decision = McpProtocolDecision({"ok": True}, diagnostics)
    with caplog.at_level(logging.INFO, logger="deepseek_infra.mcp.protocol_preparation"):
        log_mcp_protocol_diagnostics(decision.diagnostics)
    record = caplog.records[-1]
    assert record.method == "tools/call"  # type: ignore[attr-defined]
    assert not any("arguments" in str(value).lower() or "secret" in str(value).lower() for value in record.__dict__.values())

    headers = protocol_diagnostic_headers(diagnostics)
    assert headers["X-DeepSeek-MCP-Preparation-Runtime"] == "rust"
    assert set(headers) == {
        "X-DeepSeek-MCP-Preparation-Runtime",
        "X-DeepSeek-MCP-Preparation-Fallback",
        "X-DeepSeek-MCP-Preparation-Fallback-Reason",
        "X-DeepSeek-MCP-Preparation-Latency-Ms",
    }


def test_protocol_error_response_preserves_notification_and_stable_code() -> None:
    invalid = prepare_mcp_protocol(_payload("tools/call", params={}))
    response = protocol_error_response(invalid, _payload("tools/call", params={}))
    assert response is not None
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["code"] == "invalid_params"

    notification = prepare_mcp_protocol({"jsonrpc": "2.0", "method": "notifications/unknown"})
    assert protocol_error_response(notification, {"jsonrpc": "2.0", "method": "notifications/unknown"}) is None


@pytest.mark.parametrize(
    ("value", "present", "kind"),
    [(None, False, "absent"), (None, True, "null"), (1, True, "integer"), ("x", True, "string"), (True, True, "invalid")],
)
def test_request_id_type(value: Any, present: bool, kind: str) -> None:
    assert request_id_type(value, present=present) == kind
