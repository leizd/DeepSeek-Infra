"""Tests for 3.1.5 hybrid runtime hardening: fallbacks, config parsing, and coverage uplift."""

from __future__ import annotations

import http.client
import json
import urllib.error
from io import BytesIO

import pytest

from deepseek_infra.infra.rust_core import config as rust_config
from deepseek_infra.infra.rust_core import gateway_client
from deepseek_infra.infra.rust_core import mcp_client
from deepseek_infra.infra.rust_core import policy_client
from deepseek_infra.infra.rust_core import rag_client
from deepseek_infra.infra.rust_core.health import check_rust_gateway_health
from deepseek_infra.infra.rust_core.registry import rust_status


@pytest.fixture(autouse=True)
def _clear_rust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("DEEPSEEK_RUST_GATEWAY", "DEEPSEEK_RUST_MCP", "DEEPSEEK_RUST_POLICY", "DEEPSEEK_RUST_RAG"):
        monkeypatch.delenv(prefix, raising=False)
        monkeypatch.delenv(f"{prefix}_FALLBACK", raising=False)
        monkeypatch.delenv(f"{prefix}_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_POLICY_FAILURE_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_URL", raising=False)


# --- helpers ---


class MockHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "MockHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    msg = http.client.HTTPMessage()
    return urllib.error.HTTPError(
        "http://127.0.0.1:8787/path", code, "reason", msg, BytesIO(body.encode("utf-8"))
    )


# --- rust_core/config.py ---


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " Yes "])
def test_rust_flags_enabled_variants(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", value)
    flags = rust_config.load_rust_flags()
    assert flags.gateway is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " No "])
def test_rust_flags_disabled_variants(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", value)
    flags = rust_config.load_rust_flags()
    assert flags.mcp is False


def test_rust_flags_all_disabled_by_default() -> None:
    flags = rust_config.load_rust_flags()
    assert flags == rust_config.RustComponentFlags(False, False, False, False)


def test_rust_flags_all_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    flags = rust_config.load_rust_flags()
    assert flags == rust_config.RustComponentFlags(True, True, True, True)


def test_rust_gateway_url_default() -> None:
    assert rust_config.rust_gateway_url() == rust_config.DEFAULT_RUST_GATEWAY_URL


def test_rust_gateway_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_URL", "http://rust:9999")
    assert rust_config.rust_gateway_url() == "http://rust:9999"


def test_rust_gateway_url_empty_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_URL", "  ")
    assert rust_config.rust_gateway_url() == rust_config.DEFAULT_RUST_GATEWAY_URL


# --- gateway_client.py failure paths ---


def test_gateway_client_disabled_returns_disabled_reason() -> None:
    result = gateway_client.proxy_chat_to_rust({"messages": []})
    assert not result.ok
    assert result.body == {"error": "Rust Gateway is disabled"}


def test_gateway_client_models_disabled_returns_disabled_reason() -> None:
    result = gateway_client.proxy_models_to_rust()
    assert not result.ok
    assert result.body == {"error": "Rust Gateway is disabled"}


def test_gateway_client_timeout(monkeypatch: pytest.MonkeyPatch, mock_urlopen) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.side_effect = urllib.error.URLError("timed out")
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert not result.ok
    assert result.status == 0
    assert "timed out" in str(result.body)


def test_gateway_client_http_error(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.side_effect = http_error(503, "overloaded")
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert not result.ok
    assert result.status == 503


def test_gateway_client_invalid_json(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"not json")
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert not result.ok


def test_gateway_client_empty_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"")
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert result.ok
    assert result.body == {}


def test_gateway_client_successful_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"id": "x"}).encode())
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert result.ok
    assert result.body == {"id": "x"}


def test_gateway_client_preserves_authorization(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"{}")
    gateway_client.proxy_chat_to_rust({"model": "x"}, headers={"Authorization": "Bearer token"})
    request = mock_urlopen.call_args[0][0]
    assert request.headers.get("Authorization") == "Bearer token"


def test_gateway_client_timeout_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS", "abc")
    assert gateway_client._timeout_ms() == gateway_client.DEFAULT_TIMEOUT_MS


def test_gateway_client_default_constant_attribute() -> None:
    assert gateway_client.DEFAULT_RUST_GATEWAY_URL == "http://127.0.0.1:8787"


def test_gateway_client_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        _ = gateway_client.nonexistent_attribute


# --- mcp_client.py failure paths ---


def test_mcp_client_disabled_returns_disabled_reason() -> None:
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert not result.ok
    assert result.body == {"error": "Rust MCP is disabled"}


def test_mcp_client_timeout(monkeypatch: pytest.MonkeyPatch, mock_urlopen) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert not result.ok
    assert result.status == 0
    assert "connection refused" in str(result.body)


def test_mcp_client_http_error(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.side_effect = http_error(500, "internal error")
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert not result.ok
    assert result.status == 500


def test_mcp_client_invalid_json(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"{invalid")
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert not result.ok


def test_mcp_client_successful_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"result": "ok"}).encode())
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert result.ok
    assert result.body == {"result": "ok"}


def test_mcp_client_timeout_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_TIMEOUT_MS", "not-a-number")
    assert mcp_client._timeout_ms() == mcp_client.DEFAULT_MCP_TIMEOUT_MS


# --- policy_client.py failure paths ---


def test_policy_client_disabled_returns_disabled_reason() -> None:
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert result.reason == "Rust Policy is disabled"


def test_policy_client_timeout(monkeypatch: pytest.MonkeyPatch, mock_urlopen) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.side_effect = urllib.error.URLError("timeout")
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert result.status == 0
    assert "timeout" in result.reason


def test_policy_client_http_error(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.side_effect = http_error(400, "bad request")
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert result.status == 400


def test_policy_client_malformed_decision(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"foo": "bar"}).encode())
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert not result.allowed
    assert result.code == "policy_backend_unavailable"


def test_policy_client_check_path_and_capability(monkeypatch: pytest.MonkeyPatch, mock_urlopen) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"decision": "Allow"}).encode())
    path_result = policy_client.check_path("/workspace", "file.txt")
    cap_result = policy_client.check_capability("ReadFile", ["ReadFile"], "Low")
    assert path_result.allowed
    assert cap_result.allowed


def test_policy_client_timeout_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_TIMEOUT_MS", "bad")
    assert policy_client._timeout_ms() == policy_client.DEFAULT_POLICY_TIMEOUT_MS


# --- rag_client.py failure paths ---


def test_rag_client_normalize_disabled_returns_none() -> None:
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_score_chunks_disabled_returns_none() -> None:
    result, used_rust = rag_client.score_chunks("hello", [{"id": "x", "text": "y"}])
    assert result is None
    assert not used_rust


def test_rag_client_format_citation_disabled_returns_none() -> None:
    result, used_rust = rag_client.format_citation("doc.md", 1, 2)
    assert result is None
    assert not used_rust


def test_rag_client_validate_index_disabled_returns_none() -> None:
    result, used_rust = rag_client.validate_index([{"id": "x", "text": "y"}])
    assert result is None
    assert not used_rust


def test_rag_client_timeout(monkeypatch: pytest.MonkeyPatch, mock_urlopen) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.side_effect = urllib.error.URLError("timeout")
    result, used_rust = rag_client.score_chunks("hello", [{"id": "x", "text": "y"}])
    assert result is None
    assert not used_rust


def test_rag_client_http_error(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.side_effect = http_error(500, "error")
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_invalid_json(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"not json")
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_unexpected_status(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(204, json.dumps({"normalized": "hello"}).encode())
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_normalize_query_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"normalized": "hello world"}).encode())
    result, used_rust = rag_client.normalize_query("Hello   WORLD")
    assert result == "hello world"
    assert used_rust is True


def test_rag_client_score_chunks_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    payload = {"ranked": [{"id": "a", "score": 5.5}, {"id": "b", "score": 1.0}]}
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps(payload).encode())
    result, used_rust = rag_client.score_chunks("hello", [{"id": "a", "text": "x"}])
    assert result == [("a", 5.5), ("b", 1.0)]
    assert used_rust is True


def test_rag_client_score_chunks_missing_id_and_score(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    payload = {"ranked": [{"id": "a"}, {"score": 1.0}, {"id": "b", "score": 2.0}]}
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps(payload).encode())
    result, used_rust = rag_client.score_chunks("hello", [{"id": "a", "text": "x"}])
    assert result == [("b", 2.0)]
    assert used_rust is True


def test_rag_client_format_citation_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": "doc.md:L1-L2"}).encode())
    result, used_rust = rag_client.format_citation("doc.md", 1, 2)
    assert result == "doc.md:L1-L2"
    assert used_rust is True


def test_rag_client_format_citation_failure_falls_back(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": 123}).encode())
    result, used_rust = rag_client.format_citation("doc.md", 1, 2)
    assert result is None
    assert not used_rust


def test_rag_client_validate_index_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"valid": True}).encode())
    result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
    assert result == {"valid": True, "error": None}
    assert used_rust is True


def test_rag_client_validate_index_failure(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"valid": False, "error": "bad"}).encode())
    result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
    assert result == {"valid": False, "error": "bad"}
    assert used_rust is True


def test_rag_client_validate_index_empty_chunks() -> None:
    result, used_rust = rag_client.validate_index([])
    assert result is None
    assert not used_rust


def test_rag_client_timeout_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_TIMEOUT_MS", "bad")
    assert rag_client._timeout_ms() == rag_client.DEFAULT_RAG_TIMEOUT_MS


# --- fallback helpers ---


def test_fallback_variants_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", "true")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_FALLBACK", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "yes")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "on")
    assert gateway_client.fallback_to_python_enabled() is True
    assert mcp_client.fallback_to_python_enabled() is True
    assert policy_client.fallback_to_python_enabled() is True
    assert rag_client.fallback_to_python_enabled() is True


def test_fallback_variants_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", "0")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP_FALLBACK", "false")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY_FALLBACK", "no")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "off")
    assert gateway_client.fallback_to_python_enabled() is False
    assert mcp_client.fallback_to_python_enabled() is False
    assert policy_client.fallback_to_python_enabled() is False
    assert rag_client.fallback_to_python_enabled() is False


# --- health + registry ---


def test_check_rust_gateway_health_http_scheme_unhealthy() -> None:
    assert check_rust_gateway_health("http://127.0.0.1:8787", timeout=0.01) is False


def test_check_rust_gateway_health_unsupported_scheme() -> None:
    assert check_rust_gateway_health("ftp://127.0.0.1:8787") is False


def test_rust_status_reflects_all_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    status = rust_status()
    assert status["enabled"]["gateway"] is True
    assert status["enabled"]["mcp"] is True
    assert status["enabled"]["policy"] is True
    assert status["enabled"]["rag"] is True


def test_rust_status_all_disabled() -> None:
    status = rust_status()
    for component in status["enabled"].values():
        assert component is False


# --- end-to-end fallback combinations ---


def test_all_rust_flags_enabled_but_gateway_unreachable(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.side_effect = urllib.error.URLError("unreachable")
    assert gateway_client.proxy_chat_to_rust({"model": "x"}).ok is False
    assert mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"}).ok is False
    assert policy_client.check_url("https://x.com").ok is False
    assert rag_client.normalize_query("hello")[0] is None


def test_all_rust_flags_disabled() -> None:
    assert gateway_client.rust_gateway_enabled() is False
    assert mcp_client.rust_mcp_enabled() is False
    assert policy_client.rust_policy_enabled() is False
    assert rag_client.rust_rag_enabled() is False
