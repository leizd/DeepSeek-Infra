from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.mcp import executor


class _Policy:
    def evaluate(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, policy_verdict="allow")

    def sanitize_result(self, _name: str, output: dict[str, Any]) -> dict[str, Any]:
        return output


def _policy() -> Any:
    return _Policy()


def test_external_mcp_rejects_invalid_bridged_name() -> None:
    result = executor.call_external_mcp_tool("not-bridged", {}, _policy())

    assert result["code"] == "invalid_payload"


def test_external_mcp_reports_unavailable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor.external_mcp_registry, "get_profile", lambda _name: None)
    monkeypatch.setattr(executor.external_mcp_registry, "resolve", lambda _name: None)

    result = executor.call_external_mcp_tool("mcp__offline__echo", {}, _policy())

    assert result["code"] == "upstream_failure"
    assert "offline" in result["error"]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AppError("connection timeout", code=ErrorCode.UPSTREAM_TIMEOUT), "upstream_failure"),
        (RuntimeError("unexpected transport failure"), "internal"),
    ],
)
def test_external_mcp_shapes_client_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_code: str,
) -> None:
    class Client:
        last_stats = SimpleNamespace(latency_ms=4, attempts=2, retry_count=1, timeout=isinstance(error, AppError))

        def call_tool(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            raise error

    client = Client()
    span = SimpleNamespace(finish=lambda **_kwargs: None)
    profile = SimpleNamespace(input_schema={"type": "object"}, risk="medium")
    failures: list[Exception] = []
    monkeypatch.setattr(executor.external_mcp_registry, "get_profile", lambda _name: profile)
    monkeypatch.setattr(executor.external_mcp_registry, "resolve", lambda _name: (client, "echo"))
    monkeypatch.setattr(executor.external_mcp_registry, "record_call_failure", lambda _server, _client, exc: failures.append(exc))
    monkeypatch.setattr(executor, "start_span", lambda *_args, **_kwargs: span)
    monkeypatch.setattr(executor, "_write_audit_for_call", lambda **_kwargs: None)

    result = executor.call_external_mcp_tool("mcp__remote__echo", {"message": "hello"}, _policy())

    assert result["code"] == expected_code
    assert failures == [error]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("request timeout", "timeout"),
        ("connection refused", "unreachable"),
        ("invalid JSON schema", "schema_error"),
        ("HTTP 503", "http_error"),
        ("protocol mismatch", "protocol_error"),
        ("other upstream failure", "upstream_failure"),
    ],
)
def test_classify_external_mcp_errors(message: str, expected: str) -> None:
    error = AppError(message, code=ErrorCode.UPSTREAM_FAILURE)

    assert executor._classify_error(error) == expected
