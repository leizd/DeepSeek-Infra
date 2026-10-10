"""Rust Policy failure-mode enforcement tests for v3.2.3."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from deepseek_infra.infra.rust_core import config as rust_config
from deepseek_infra.infra.rust_core.policy_client import PolicyProxyResult
from deepseek_infra.infra.tool_runtime import tools


@pytest.fixture(autouse=True)
def _rust_policy_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", raising=False)


def _unavailable(reason: str = "connection refused") -> PolicyProxyResult:
    return PolicyProxyResult(
        ok=False,
        status=0,
        allowed=False,
        reason=reason,
        body={},
        code="policy_backend_unavailable",
    )


def _deny(capability: str) -> PolicyProxyResult:
    return PolicyProxyResult(
        ok=True,
        status=200,
        allowed=False,
        reason="required capability was not granted",
        body={},
        code="missing_capability",
        decision_id="pd_rust_deny",
        trace_id="trace-deny",
        capability=capability,
        risk_level="High",
    )


def test_rust_policy_deny_prevents_network_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tools,
        "rust_check_url",
        lambda *_args, **_kwargs: PolicyProxyResult(
            ok=True,
            status=200,
            allowed=False,
            reason="private network addresses are not allowed",
            body={},
            code="private_network_blocked",
            decision_id="pd_network_deny",
            trace_id="trace-network",
            capability="NetworkFetch",
            risk_level="High",
        ),
    )

    with patch.object(tools, "fetch_url") as executor:
        output = tools.execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}}},
            trace_id="trace-network",
        )

    executor.assert_not_called()
    assert output["code"] == "private_network_blocked"
    assert output["decision_id"] == "pd_network_deny"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "executor_name", "capability"),
    [
        ("create_document", {"title": "blocked", "sections": []}, "create_document", "WriteFile"),
        ("python_eval", {"expression": "2 + 2"}, "python_eval", "ShellExec"),
    ],
)
def test_rust_policy_deny_prevents_file_and_shell_execution(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, object],
    executor_name: str,
    capability: str,
) -> None:
    monkeypatch.setattr(
        tools,
        "rust_check_capability",
        lambda *_args, **_kwargs: _deny(capability),
    )

    with patch.object(tools, executor_name) as executor:
        output = tools.execute_tool_call(
            {"function": {"name": tool_name, "arguments": arguments}},
            trace_id="trace-deny",
        )

    executor.assert_not_called()
    assert output["ok"] is False
    assert output["code"] == "missing_capability"


def test_rust_policy_fallback_uses_python_policy_without_explicit_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "fallback")
    monkeypatch.setattr(tools, "rust_check_url", lambda *_args, **_kwargs: _unavailable())
    monkeypatch.setattr(tools, "rust_check_capability", lambda *_args, **_kwargs: _unavailable())

    with patch.object(tools, "fetch_url") as executor:
        output = tools.execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}}}
        )

    executor.assert_not_called()
    assert output["ok"] is False
    assert "ssrf_blocked" in str(output["policy"]["reasons"])


def test_rust_policy_failure_mode_deny_blocks_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "deny")
    monkeypatch.setattr(tools, "rust_check_url", lambda *_args, **_kwargs: _unavailable())

    with patch.object(tools, "fetch_url") as executor:
        output = tools.execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}
        )

    executor.assert_not_called()
    assert output["status"] == 403
    assert output["code"] == "policy_backend_unavailable"
    assert output["policy_backend"] == "rust"


def test_rust_policy_failure_mode_error_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "error")
    monkeypatch.setattr(tools, "rust_check_url", lambda *_args, **_kwargs: _unavailable())

    with patch.object(tools, "fetch_url") as executor:
        output = tools.execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}
        )

    executor.assert_not_called()
    assert output["status"] == 503
    assert output["code"] == "policy_backend_unavailable"
    assert output["failure_mode"] == "error"


def test_rust_policy_invalid_failure_mode_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "surprise")
    assert rust_config.rust_policy_failure_mode() == "fallback"


def test_legacy_no_fallback_switch_maps_to_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "0")
    assert rust_config.rust_policy_failure_mode() == "deny"


def test_rust_policy_fallback_logs_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="deepseek_infra.rust_policy")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "fallback")
    monkeypatch.setattr(tools, "rust_check_url", lambda *_args, **_kwargs: _unavailable())
    monkeypatch.setattr(tools, "rust_check_capability", lambda *_args, **_kwargs: _unavailable())

    tools.execute_tool_call(
        {"function": {"name": "fetch_url", "arguments": {"url": "http://localhost/admin"}}},
        trace_id="trace-fallback",
    )

    records = [record for record in caplog.records if getattr(record, "event", "") == "tool_policy_decision"]
    assert records
    assert all(getattr(record, "failure_mode", "") == "fallback" for record in records)
    assert all(getattr(record, "policy_code", "") == "policy_backend_unavailable" for record in records)
    assert all(getattr(record, "trace_id", "") == "trace-fallback" for record in records)
