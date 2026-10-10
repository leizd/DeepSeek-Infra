"""Tests for Rust Policy opt-in integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from deepseek_infra.infra.rust_core.policy_client import (
    PolicyProxyResult,
    check_url,
    fallback_to_python_enabled,
    rust_policy_enabled,
)
from deepseek_infra.infra.tool_runtime.tool_policy import ToolPolicy
from deepseek_infra.infra.tool_runtime.tools import execute_tool_call


@pytest.fixture(autouse=True)
def _clear_rust_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_TIMEOUT_MS", raising=False)


# --- config ---


def test_fallback_enabled_by_default() -> None:
    assert fallback_to_python_enabled() is True


def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "0")
    assert fallback_to_python_enabled() is False


def test_rust_policy_disabled_by_default() -> None:
    assert rust_policy_enabled() is False


# --- client ---


def test_rust_policy_client_disabled_returns_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "0")
    result = check_url("https://example.com")
    assert not result.ok
    assert result.reason == "Rust Policy is disabled"


# --- tool execution integration ---


def test_rust_policy_disabled_uses_python_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "0")
    with patch("deepseek_infra.infra.tool_runtime.tools.rust_check_url") as mock:
        result = execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "http://localhost"}}},
            policy=ToolPolicy.permissive(),
        )
        mock.assert_not_called()
    assert result["ok"] is False
    assert "blocked" in result["reason"].lower() or "localhost" in result["reason"].lower()


def test_rust_policy_enabled_allows_safe_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_url",
        return_value=PolicyProxyResult(
            ok=True, status=200, allowed=True, reason="", body={"decision": "Allow"}
        ),
    ) as mock:
        execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}},
            policy=ToolPolicy.permissive(),
        )
        mock.assert_called_once()


def test_rust_policy_enabled_denies_private_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_url",
        return_value=PolicyProxyResult(
            ok=True,
            status=200,
            allowed=False,
            reason="private or local IP address",
            body={"decision": "Deny", "reason": "private or local IP address"},
        ),
    ):
        result = execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1"}}},
            policy=ToolPolicy.permissive(),
        )
    assert result["ok"] is False
    assert "Rust Policy" in result["reason"]


def test_rust_policy_enabled_denies_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_path",
        return_value=PolicyProxyResult(
            ok=True,
            status=200,
            allowed=False,
            reason="parent directory traversal",
            body={"decision": "Deny", "reason": "parent directory traversal"},
        ),
    ):
        result = execute_tool_call(
            {"function": {"name": "search_files", "arguments": {"query": "x", "fileId": "../secret"}}},
            policy=ToolPolicy.permissive(),
        )
    assert result["ok"] is False
    assert "Rust Policy" in result["reason"]


def test_rust_policy_unreachable_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_url",
        return_value=PolicyProxyResult(
            ok=False, status=0, allowed=False, reason="connection refused", body=""
        ),
    ):
        result = execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "http://localhost"}}},
            policy=ToolPolicy.permissive(),
        )
    assert result["ok"] is False


def test_rust_policy_no_fallback_blocks_on_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "0")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_url",
        return_value=PolicyProxyResult(
            ok=False, status=0, allowed=False, reason="connection refused", body=""
        ),
    ):
        result = execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}},
            policy=ToolPolicy.permissive(),
        )
    assert result["ok"] is False
    assert "Rust Policy backend unavailable" in result["reason"]


def test_rust_policy_deny_blocks_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    with patch(
        "deepseek_infra.infra.tool_runtime.tools.rust_check_capability",
        return_value=PolicyProxyResult(
            ok=True,
            status=200,
            allowed=False,
            reason="capability not granted",
            body={"decision": "Deny", "reason": "capability not granted"},
        ),
    ):
        result = execute_tool_call(
            {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}},
            policy=ToolPolicy.permissive(),
        )
    assert result["ok"] is False
    assert "Capability blocked" in result["reason"]
