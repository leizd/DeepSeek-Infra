"""Structured Rust Policy decision and audit contract tests for v3.2.3."""

from __future__ import annotations

import json
import logging
import urllib.error
from unittest.mock import patch

import pytest

from deepseek_infra.infra.rust_core import policy_client
from deepseek_infra.infra.rust_core.policy_client import PolicyProxyResult
from deepseek_infra.infra.tool_runtime import tools
from deepseek_infra.infra.tool_runtime.tool_policy import ToolPolicy


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self.status = 200
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _rust_policy_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", raising=False)


def _decision(*, allowed: bool, code: str, reason: str = "") -> PolicyProxyResult:
    return PolicyProxyResult(
        ok=True,
        status=200,
        allowed=allowed,
        reason=reason,
        body={},
        code=code,
        decision_id="pd_0123456789",
        trace_id="trace-policy-123",
        capability="NetworkFetch",
        risk_level="High",
    )


def test_policy_client_preserves_structured_decision_and_trace(mock_urlopen) -> None:
    mock_urlopen.return_value = _Response(
        {
            "allowed": False,
            "code": "private_network_blocked",
            "reason": "private network addresses are not allowed",
            "decision_id": "pd_rust_123",
            "trace_id": "trace-client-123",
            "capability": "NetworkFetch",
            "risk_level": "High",
        }
    )

    result = policy_client.check_url(
        "http://10.0.0.1/admin",
        trace_id="trace-client-123",
        capability="NetworkFetch",
        risk_level="High",
    )

    assert result.allowed is False
    assert result.code == "private_network_blocked"
    assert result.decision_id == "pd_rust_123"
    assert result.trace_id == "trace-client-123"
    request = mock_urlopen.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["trace_id"] == "trace-client-123"
    assert payload["capability"] == "NetworkFetch"


def test_policy_client_rejects_incomplete_structured_decision(mock_urlopen) -> None:
    mock_urlopen.return_value = _Response(
        {
            "allowed": False,
            "code": "private_network_blocked",
            "reason": "private network addresses are not allowed",
        }
    )

    result = policy_client.check_url("http://10.0.0.1/admin")

    assert result.ok is False
    assert result.code == "policy_backend_unavailable"
    assert "structured fields" in result.reason


def test_rust_policy_deny_audit_and_output_share_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="deepseek_infra.rust_policy")
    monkeypatch.setattr(
        tools,
        "rust_check_url",
        lambda *_args, **_kwargs: _decision(
            allowed=False,
            code="private_network_blocked",
            reason="private network addresses are not allowed",
        ),
    )
    executor = patch.object(tools, "fetch_url", return_value={"unexpected": True})

    with executor as fetch_spy:
        output = tools.execute_tool_call(
            {
                "function": {
                    "name": "fetch_url",
                    "arguments": {
                        "url": "https://admin:secret@example.com/admin?authorization=Bearer-topsecret"
                    },
                }
            },
            policy=ToolPolicy.permissive(),
            trace_id="trace-policy-123",
        )

    fetch_spy.assert_not_called()
    assert output["code"] == "private_network_blocked"
    assert output["decision_id"] == "pd_0123456789"
    assert output["trace_id"] == "trace-policy-123"
    record = next(record for record in caplog.records if getattr(record, "event", "") == "tool_policy_decision")
    assert getattr(record, "decision_id", "") == output["decision_id"]
    assert getattr(record, "policy_code", "") == output["code"]
    assert getattr(record, "trace_id", "") == output["trace_id"]
    assert getattr(record, "policy_target", "") == "https://example.com/admin"


def test_rust_policy_allow_is_audited(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="deepseek_infra.rust_policy")
    monkeypatch.setattr(tools, "rust_check_url", lambda *_args, **_kwargs: _decision(allowed=True, code="allowed"))
    monkeypatch.setattr(
        tools,
        "rust_check_capability",
        lambda *_args, **_kwargs: _decision(allowed=True, code="allowed"),
    )
    monkeypatch.setattr(tools, "fetch_url", lambda _url: {"status": 200, "text": "offline"})

    output = tools.execute_tool_call(
        {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com/docs"}}},
        policy=ToolPolicy.permissive(),
        trace_id="trace-policy-123",
    )

    assert output["ok"] is True
    records = [record for record in caplog.records if getattr(record, "event", "") == "tool_policy_decision"]
    assert len(records) == 2
    assert all(
        getattr(record, "allowed", False) is True and getattr(record, "policy_code", "") == "allowed"
        for record in records
    )


def test_policy_audit_redacts_authorization_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="deepseek_infra.rust_policy")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", "deny")
    monkeypatch.setattr(
        tools,
        "rust_check_url",
        lambda *_args, **_kwargs: PolicyProxyResult(
            ok=False,
            status=0,
            allowed=False,
            reason="Authorization: Bearer topsecret-token",
            body={},
            code="policy_backend_unavailable",
        ),
    )

    tools.execute_tool_call(
        {
            "function": {
                "name": "fetch_url",
                "arguments": {"url": "https://user:password@example.com/path?token=secret-query"},
            }
        },
        trace_id="trace-redaction",
    )

    record = next(record for record in caplog.records if getattr(record, "event", "") == "tool_policy_decision")
    serialized = json.dumps(record.__dict__, default=str)
    for secret in ("topsecret-token", "password", "secret-query"):
        assert secret not in serialized
    assert "[REDACTED]" in str(getattr(record, "policy_reason", ""))


def test_policy_client_redacts_authorization_from_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_urlopen,
) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("Authorization: Bearer transport-secret")
    result = policy_client.check_url("https://example.com")
    assert result.ok is False
    assert "transport-secret" not in result.reason
    assert "[REDACTED]" in result.reason
