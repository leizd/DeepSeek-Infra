"""Cover the few remaining rust_core branches not hit by the existing client tests."""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from deepseek_infra.infra.rust_core import gateway_client, mcp_client, policy_client, rag_client
from deepseek_infra.infra.rust_core.config import DEFAULT_RUST_GATEWAY_URL, rust_gateway_url


@pytest.fixture(autouse=True)
def _clear_rust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("DEEPSEEK_RUST_GATEWAY", "DEEPSEEK_RUST_MCP", "DEEPSEEK_RUST_POLICY", "DEEPSEEK_RUST_RAG"):
        monkeypatch.delenv(prefix, raising=False)
        monkeypatch.delenv(f"{prefix}_FALLBACK", raising=False)
        monkeypatch.delenv(f"{prefix}_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_URL", raising=False)


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    msg = http.client.HTTPMessage()
    return urllib.error.HTTPError(
        "http://127.0.0.1:8787/path", code, "reason", msg, BytesIO(body.encode("utf-8"))
    )


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


class TestConfig:
    def test_rust_gateway_url_whitespace_fallback(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_RUST_GATEWAY_URL": "  "}, clear=False):
            assert rust_gateway_url() == DEFAULT_RUST_GATEWAY_URL


class TestGatewayClient:
    def test_timeout_ms_invalid(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS": "bad"}, clear=False):
            assert gateway_client._timeout_ms() == gateway_client.DEFAULT_TIMEOUT_MS

    def test_proxy_chat_disabled(self) -> None:
        result = gateway_client.proxy_chat_to_rust({"model": "x"})
        assert not result.ok
        assert result.body == {"error": "Rust Gateway is disabled"}

    def test_request_without_authorization(self, mock_urlopen) -> None:
        mock_urlopen.return_value = MockHTTPResponse(200, b"{}")
        gateway_client._request("GET", "/test")
        request = mock_urlopen.call_args[0][0]
        assert "Authorization" not in request.headers

    def test_request_connection_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = gateway_client._request("GET", "/test")
        assert not result.ok
        assert result.status == 0

    def test_getattr_default_url(self) -> None:
        assert gateway_client.__getattr__("DEFAULT_RUST_GATEWAY_URL") == DEFAULT_RUST_GATEWAY_URL
        with pytest.raises(AttributeError):
            gateway_client.__getattr__("MISSING")


class TestMcpClient:
    def test_timeout_ms_invalid(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_RUST_MCP_TIMEOUT_MS": "bad"}, clear=False):
            assert mcp_client._timeout_ms() == mcp_client.DEFAULT_MCP_TIMEOUT_MS

    def test_proxy_mcp_disabled(self) -> None:
        result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
        assert not result.ok
        assert result.body == {"error": "Rust MCP is disabled"}

    def test_request_connection_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = mcp_client._request("GET", "/test")
        assert not result.ok
        assert result.status == 0


class TestPolicyClient:
    def test_timeout_ms_invalid(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_RUST_POLICY_TIMEOUT_MS": "bad"}, clear=False):
            assert policy_client._timeout_ms() == policy_client.DEFAULT_POLICY_TIMEOUT_MS

    def test_check_path_disabled(self) -> None:
        result = policy_client.check_path("/root", "file.txt")
        assert not result.ok
        assert result.reason == "Rust Policy is disabled"

    def test_request_connection_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = policy_client._request("/test", {})
        assert not result.ok
        assert result.status == 0


class TestRagClient:
    def test_timeout_ms_invalid(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_RUST_RAG_TIMEOUT_MS": "bad"}, clear=False):
            assert rag_client._timeout_ms() == rag_client.DEFAULT_RAG_TIMEOUT_MS

    def test_format_citation_disabled(self) -> None:
        result, used_rust = rag_client.format_citation("src", 1, 2)
        assert result is None
        assert not used_rust

    def test_score_chunks_with_rust_ranked(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(
            200,
            json.dumps({"ranked": [{"id": "1", "score": 0.5}, {"id": 2, "score": 0.5}]}).encode(),
        )
        result, used_rust = rag_client.score_chunks("q", [{"id": "1", "text": "x"}])
        assert used_rust is True
        assert result == [("1", 0.5)]

    def test_normalize_query_success(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"normalized": "hello world"}).encode())
        result, used_rust = rag_client.normalize_query("q")
        assert result == "hello world"
        assert used_rust is True

    def test_normalize_query_non_string(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"normalized": 123}).encode())
        result, used_rust = rag_client.normalize_query("q")
        assert result is None
        assert not used_rust

    def test_normalize_query_non_200_status(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
        mock_urlopen.return_value = MockHTTPResponse(500, json.dumps({"error": "x"}).encode())
        result, used_rust = rag_client.normalize_query("q")
        assert result is None
        assert not used_rust

    def test_validate_index_success(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"valid": True, "error": "ok"}).encode())
        result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
        assert used_rust is True
        assert result == {"valid": True, "error": "ok"}

    def test_validate_index_empty_chunks(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        result, used_rust = rag_client.validate_index([])
        assert result is None
        assert not used_rust

    def test_validate_index_non_dict_body(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
        mock_urlopen.return_value = MockHTTPResponse(200, b'"ok"')
        result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
        assert result is None
        assert not used_rust

    def test_score_chunks_empty(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        result, used_rust = rag_client.score_chunks("q", [])
        assert result is None
        assert not used_rust

    def test_score_chunks_non_200_status(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
        mock_urlopen.return_value = MockHTTPResponse(500, json.dumps({"error": "x"}).encode())
        result, used_rust = rag_client.score_chunks("q", [{"id": "a"}])
        assert result is None
        assert not used_rust

    def test_format_citation_success(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": "Source: doc.md"}).encode())
        result, used_rust = rag_client.format_citation("src", 1, 2)
        assert result == "Source: doc.md"
        assert used_rust is True

    def test_format_citation_non_string(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": 123}).encode())
        result, used_rust = rag_client.format_citation("src", 1, 2)
        assert result is None
        assert not used_rust

    def test_format_citation_fallback(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": 123}).encode())
        result, used_rust = rag_client.format_citation("src", 1, 2)
        assert result is None
        assert not used_rust

    def test_validate_index_fallback(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, b'"ok"')
        result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
        assert result is None
        assert not used_rust

    def test_gateway_client_authorization_header(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, b"{}")
        gateway_client.proxy_chat_to_rust({"model": "x"}, headers={"Authorization": "Bearer t"})
        request = mock_urlopen.call_args[0][0]
        assert request.headers.get("Authorization") == "Bearer t"

    def test_policy_client_check_path_enabled(self, mock_urlopen, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
        mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"decision": "Allow"}).encode())
        result = policy_client.check_path("/root", "file.txt")
        assert result.ok is True
        assert result.allowed is True
