from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.gateway import request_preparation as preparation
from deepseek_infra.infra.gateway.deepseek_client import call_deepseek
from deepseek_infra.infra.rust_core import gateway_client


def minimal_request(**updates: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": " hello "}],
        "stream": False,
    }
    request.update(updates)
    return request


def rust_success(request: dict[str, Any]) -> gateway_client.GatewayProxyResult:
    return gateway_client.GatewayProxyResult(
        True,
        200,
        {"ok": True, "request": preparation.prepare_gateway_request(request), "diagnostics": {"runtime": "rust"}},
    )


def test_python_reference_normalizes_messages_models_tools_and_numbers() -> None:
    request = minimal_request(
        model="fast",
        messages=[
            {"role": "system", "content": " rules "},
            {"role": "assistant", "content": "", "tool_calls": [{"id": " c1 ", "function": {"name": " echo ", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": " c1 ", "content": " result "},
            {"role": "user", "content": [{"type": "text", "text": " hi "}, {"type": "image_url", "image_url": {"url": " data:image/png;base64,AA== ", "detail": "low"}}]},
        ],
        tools=[
            {
                "type": "function",
                "ignored": True,
                "function": {
                    "name": " echo ",
                    "description": " echo input ",
                    "parameters": {"type": "object"},
                    "strict": True,
                    "ignored": True,
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "echo"}},
        temperature=1,
        top_p=0.5,
        max_tokens=1024,
        reasoning_effort="minimal",
        thinking={"type": "enabled"},
        ignored="removed",
    )

    normalized = preparation.prepare_gateway_request(request)

    assert normalized["model"] == "deepseek-v4-flash"
    assert normalized["messages"][0]["content"] == "rules"
    assert normalized["messages"][1]["tool_calls"][0]["id"] == "c1"
    assert normalized["messages"][2] == {"role": "tool", "content": "result", "tool_call_id": "c1"}
    assert normalized["messages"][3]["content"][1]["image_url"]["detail"] == "low"
    assert normalized["tools"][0]["function"] == {
        "name": "echo",
        "parameters": {"type": "object"},
        "description": "echo input",
        "strict": True,
    }
    assert normalized["temperature"] == 1.0
    assert "ignored" not in normalized


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], ErrorCode.INVALID_REQUEST),
        (minimal_request(model=""), ErrorCode.UNSUPPORTED_MODEL),
        (minimal_request(model="unknown"), ErrorCode.UNSUPPORTED_MODEL),
        (minimal_request(messages=None), ErrorCode.INVALID_MESSAGES),
        (minimal_request(messages=[]), ErrorCode.INVALID_MESSAGES),
        (minimal_request(messages=["bad"]), ErrorCode.INVALID_MESSAGES),
        (minimal_request(messages=[{"role": "owner", "content": "x"}]), ErrorCode.INVALID_MESSAGE_ROLE),
        (minimal_request(messages=[{"role": "user", "content": 1}]), ErrorCode.INVALID_MESSAGE_CONTENT),
        (minimal_request(messages=[{"role": "tool", "content": "x"}]), ErrorCode.INVALID_MESSAGE_CONTENT),
        (minimal_request(messages=[{"role": "assistant", "content": "", "tool_calls": [1]}]), ErrorCode.INVALID_MESSAGE_CONTENT),
        (minimal_request(messages=[{"role": "assistant", "content": "", "tool_calls": [{"function": "bad"}]}]), ErrorCode.INVALID_MESSAGE_CONTENT),
        (minimal_request(tools={}), ErrorCode.INVALID_TOOLS),
        (minimal_request(tools=[{"type": "function", "function": []}]), ErrorCode.INVALID_TOOLS),
        (minimal_request(tools=[{"type": "function", "function": {}}]), ErrorCode.INVALID_TOOLS),
        (minimal_request(tools=[{"type": "function", "function": {"name": "x", "parameters": []}}]), ErrorCode.INVALID_TOOLS),
        (minimal_request(tools=[], tool_choice="sometimes"), ErrorCode.INVALID_TOOL_CHOICE),
        (minimal_request(temperature=True), ErrorCode.INVALID_TEMPERATURE),
        (minimal_request(temperature=float("nan")), ErrorCode.INVALID_REQUEST),
        (minimal_request(temperature=2.1), ErrorCode.INVALID_TEMPERATURE),
        (minimal_request(top_p=1.1), ErrorCode.INVALID_REQUEST),
        (minimal_request(max_tokens=True), ErrorCode.INVALID_MAX_TOKENS),
        (minimal_request(max_tokens=-1), ErrorCode.INVALID_MAX_TOKENS),
        (minimal_request(stream=True), ErrorCode.INVALID_REQUEST),
        (minimal_request(reasoning_effort="extreme"), ErrorCode.INVALID_REQUEST),
        (minimal_request(thinking={"type": "disabled"}), ErrorCode.INVALID_REQUEST),
        (minimal_request(api_key="secret"), ErrorCode.INVALID_REQUEST),
    ],
)
def test_python_reference_rejects_invalid_requests(payload: Any, code: ErrorCode) -> None:
    with pytest.raises(AppError) as exc_info:
        preparation.prepare_gateway_request(payload)
    assert exc_info.value.code == code


def test_python_reference_enforces_size_and_depth_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preparation, "MAX_REQUEST_BYTES", 20)
    with pytest.raises(AppError) as size_error:
        preparation.prepare_gateway_request(minimal_request())
    assert size_error.value.code == ErrorCode.REQUEST_TOO_LARGE

    monkeypatch.setattr(preparation, "MAX_REQUEST_BYTES", 16_000_000)
    monkeypatch.setattr(preparation, "MAX_REQUEST_DEPTH", 2)
    with pytest.raises(AppError) as depth_error:
        preparation.prepare_gateway_request(minimal_request(extra={"a": {"b": {"c": 1}}}))
    assert depth_error.value.code == ErrorCode.REQUEST_TOO_LARGE


def test_rust_gateway_prepare_success(monkeypatch: pytest.MonkeyPatch) -> None:
    request = minimal_request()
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "prepare_request_with_rust", rust_success)

    prepared = preparation.prepare_request_with_optional_rust(request)

    assert prepared.request["messages"][0]["content"] == "hello"
    assert prepared.diagnostics["runtime"] == "rust"
    assert prepared.diagnostics["fallback"] is False


def test_rust_gateway_disabled_uses_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: False)
    monkeypatch.setattr(gateway_client, "prepare_request_with_rust", lambda _request: pytest.fail("sidecar called"))

    prepared = preparation.prepare_request_with_optional_rust(minimal_request())

    assert prepared.diagnostics == {"runtime": "python", "fallback": False}


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (gateway_client.GatewayProxyResult(False, 0, "timeout", "rust_backend_timeout"), "rust_backend_timeout"),
        (gateway_client.GatewayProxyResult(False, 0, "refused", "rust_backend_unavailable"), "rust_backend_unavailable"),
        (gateway_client.GatewayProxyResult(False, 200, {}, "rust_empty_response"), "rust_empty_response"),
        (gateway_client.GatewayProxyResult(True, 200, {}, "rust_empty_response"), "rust_empty_response"),
        (gateway_client.GatewayProxyResult(False, 200, "<html>", "rust_malformed_json"), "rust_malformed_json"),
        (gateway_client.GatewayProxyResult(True, 200, []), "rust_invalid_shape"),
        (gateway_client.GatewayProxyResult(True, 200, {"ok": True}), "rust_invalid_shape"),
    ],
)
def test_rust_gateway_backend_failures_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    result: gateway_client.GatewayProxyResult,
    reason: str,
) -> None:
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "fallback_to_python_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "prepare_request_with_rust", lambda _request: result)

    prepared = preparation.prepare_request_with_optional_rust(minimal_request())

    assert prepared.diagnostics["runtime"] == "python"
    assert prepared.diagnostics["fallback"] is True
    assert prepared.diagnostics["fallbackReason"] == reason


def test_rust_gateway_result_receives_python_defensive_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    request = minimal_request()
    injected = preparation.prepare_gateway_request(request)
    injected["Authorization"] = "Bearer secret"
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "fallback_to_python_enabled", lambda: True)
    monkeypatch.setattr(
        gateway_client,
        "prepare_request_with_rust",
        lambda _request: gateway_client.GatewayProxyResult(True, 200, {"ok": True, "request": injected}),
    )

    prepared = preparation.prepare_request_with_optional_rust(request)

    assert "Authorization" not in prepared.request
    assert prepared.diagnostics["fallbackReason"] == "rust_defensive_validation_failed"


def test_rust_gateway_user_error_does_not_become_backend_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(
        gateway_client,
        "prepare_request_with_rust",
        lambda _request: gateway_client.GatewayProxyResult(
            True,
            200,
            {"ok": False, "code": "invalid_tool_choice", "message": "invalid tool_choice"},
        ),
    )

    with pytest.raises(AppError) as exc_info:
        preparation.prepare_request_with_optional_rust(minimal_request())
    assert exc_info.value.code == ErrorCode.INVALID_TOOL_CHOICE


def test_rust_gateway_unknown_error_shape_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "fallback_to_python_enabled", lambda: True)
    monkeypatch.setattr(
        gateway_client,
        "prepare_request_with_rust",
        lambda _request: gateway_client.GatewayProxyResult(
            True,
            200,
            {"ok": False, "code": "unexpected", "message": "unknown"},
        ),
    )

    prepared = preparation.prepare_request_with_optional_rust(minimal_request())
    assert prepared.diagnostics["fallbackReason"] == "rust_invalid_shape"


def test_rust_gateway_safe_but_divergent_result_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = preparation.prepare_gateway_request(minimal_request())
    candidate["model"] = "deepseek-v4-flash"
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "fallback_to_python_enabled", lambda: True)
    monkeypatch.setattr(
        gateway_client,
        "prepare_request_with_rust",
        lambda _request: gateway_client.GatewayProxyResult(True, 200, {"ok": True, "request": candidate}),
    )

    prepared = preparation.prepare_request_with_optional_rust(minimal_request())
    assert prepared.request["model"] == "deepseek-v4-pro"
    assert prepared.diagnostics["fallbackReason"] == "rust_defensive_validation_failed"


def test_rust_gateway_never_receives_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    request = minimal_request()
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)

    def prepare(payload: dict[str, Any]) -> gateway_client.GatewayProxyResult:
        captured.append(json.loads(json.dumps(payload)))
        return rust_success(payload)

    monkeypatch.setattr(gateway_client, "prepare_request_with_rust", prepare)
    preparation.prepare_request_with_optional_rust(request)

    assert captured == [request]
    assert not ({"authorization", "api_key", "apikey", "deepseek_api_key"} & {key.lower() for key in captured[0]})


def test_rust_gateway_no_fallback_returns_structured_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_client, "rust_gateway_enabled", lambda: True)
    monkeypatch.setattr(gateway_client, "fallback_to_python_enabled", lambda: False)
    monkeypatch.setattr(
        gateway_client,
        "prepare_request_with_rust",
        lambda _request: gateway_client.GatewayProxyResult(False, 0, "offline", "rust_backend_unavailable"),
    )

    with pytest.raises(AppError) as exc_info:
        preparation.prepare_request_with_optional_rust(minimal_request())
    assert exc_info.value.code == ErrorCode.UPSTREAM_FAILURE
    assert exc_info.value.status == 502


class _Response:
    def __init__(self, body: dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_real_python_gateway_path_prepares_in_rust_then_executes_upstream(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_settings
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    captured: dict[str, dict[str, Any]] = {}

    def urlopen(request: urllib.request.Request, timeout: float = 0) -> _Response:
        del timeout
        data = request.data or b"{}"
        assert isinstance(data, bytes)
        body = json.loads(data.decode("utf-8"))
        if request.full_url.endswith("/gateway/request/prepare"):
            captured["rust"] = body
            assert request.get_header("Authorization") is None
            return _Response({"ok": True, "request": preparation.prepare_gateway_request(body)})
        captured["upstream"] = body
        assert request.get_header("Authorization") == "Bearer server-secret"
        return _Response(
            {
                "id": "chatcmpl-upstream",
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "executed by Python", "reasoning_content": ""}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }
        )

    with patch("urllib.request.urlopen", side_effect=urlopen):
        result = call_deepseek(
            {
                "apiKey": "server-secret",
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hello"}],
                "toolsEnabled": False,
                "semanticCacheEnabled": False,
                "memoryEnabled": False,
                "searchEnabled": False,
            }
        )

    assert result["content"] == "executed by Python"
    assert result["diagnostics"]["gatewayRequestPreparation"]["runtime"] == "rust"
    assert "apiKey" not in captured["rust"]
    assert captured["upstream"] == captured["rust"]
