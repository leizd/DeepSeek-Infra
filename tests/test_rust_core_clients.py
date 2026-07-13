"""Focused tests for rust_core client modules to cover remaining edge cases."""

from __future__ import annotations

import http.client
import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from deepseek_infra.infra.rust_core import gateway_client, mcp_client, policy_client, rag_client
from deepseek_infra.infra.rust_core.health import check_rust_gateway_health


@pytest.fixture(autouse=True)
def _clear_rust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("DEEPSEEK_RUST_GATEWAY", "DEEPSEEK_RUST_MCP", "DEEPSEEK_RUST_POLICY", "DEEPSEEK_RUST_RAG"):
        monkeypatch.delenv(prefix, raising=False)
        monkeypatch.delenv(f"{prefix}_FALLBACK", raising=False)
        monkeypatch.delenv(f"{prefix}_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_URL", raising=False)


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


# --- gateway_client.py ---


def test_gateway_client_models_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"data": []}).encode())
    result = gateway_client.proxy_models_to_rust()
    assert result.ok
    assert result.body == {"data": []}


def test_gateway_client_models_disabled() -> None:
    result = gateway_client.proxy_models_to_rust()
    assert not result.ok
    assert result.body == {"error": "Rust Gateway is disabled"}


def test_gateway_client_empty_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"")
    result = gateway_client.proxy_models_to_rust()
    assert result.ok
    assert result.body == {}


def test_gateway_client_http_error_body_read_fails(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    err = http_error(503, "overloaded")

    def bad_read(*_args: object) -> bytes:
        raise OSError("read failed")

    err.read = bad_read  # type: ignore[assignment]
    mock_urlopen.side_effect = err
    result = gateway_client.proxy_chat_to_rust({"model": "x"})
    assert not result.ok
    assert result.status == 503


def test_gateway_client_unknown_attr() -> None:
    with pytest.raises(AttributeError):
        _ = gateway_client.this_does_not_exist


# --- mcp_client.py ---


def test_mcp_client_empty_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"")
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert result.ok
    assert result.body == {}


def test_mcp_client_preserves_authorization(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"{}")
    mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"}, headers={"Authorization": "Bearer token"})
    request = mock_urlopen.call_args[0][0]
    assert request.headers.get("Authorization") == "Bearer token"


def test_mcp_client_http_error_body_read_fails(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "1")
    err = http_error(500, "error")

    def bad_read(*_args: object) -> bytes:
        raise OSError("read failed")

    err.read = bad_read  # type: ignore[assignment]
    mock_urlopen.side_effect = err
    result = mcp_client.proxy_mcp_to_rust({"jsonrpc": "2.0"})
    assert not result.ok
    assert result.status == 500


# --- policy_client.py ---


def test_policy_client_empty_response_is_backend_failure(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"")
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert result.allowed is False
    assert result.code == "policy_backend_unavailable"


def test_policy_client_preserves_authorization(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"decision": "Allow"}).encode())
    policy_client.check_url("https://example.com", headers={"Authorization": "Bearer token"})
    request = mock_urlopen.call_args[0][0]
    assert request.headers.get("Authorization") == "Bearer token"


def test_policy_client_http_error_body_read_fails(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "1")
    err = http_error(403, "blocked")

    def bad_read(*_args: object) -> bytes:
        raise OSError("read failed")

    err.read = bad_read  # type: ignore[assignment]
    mock_urlopen.side_effect = err
    result = policy_client.check_url("https://example.com")
    assert not result.ok
    assert result.status == 403


def test_policy_client_check_path_disabled() -> None:
    result = policy_client.check_path("/workspace", "file.txt")
    assert not result.ok
    assert result.reason == "Rust Policy is disabled"


def test_policy_client_check_capability_disabled() -> None:
    result = policy_client.check_capability("ReadFile", ["ReadFile"], "Low")
    assert not result.ok
    assert result.reason == "Rust Policy is disabled"


# --- rag_client.py ---


def test_rag_client_normalize_empty_response(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, b"")
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert used_rust is False


def test_rag_client_normalize_non_dict_body(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps(["a"]).encode())
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_normalize_fallback_disabled(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
    mock_urlopen.return_value = MockHTTPResponse(500, b"error")
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


def test_rag_client_score_chunks_fallback_disabled(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
    mock_urlopen.return_value = MockHTTPResponse(500, b"error")
    result, used_rust = rag_client.score_chunks("hello", [{"id": "a", "text": "x"}])
    assert result is None
    assert not used_rust


def test_rag_client_rank_vectors_accepts_valid_best_match(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"index": 1, "similarity": 0.75}).encode())
    result, used_rust = rag_client.rank_vectors([1.0, 0.0], [[0.5, 0.0], [0.75, 0.0]])
    assert result == (1, 0.75)
    assert used_rust is True


@pytest.mark.parametrize(
    "body",
    [
        {"index": True, "similarity": 1.0},
        {"index": 9, "similarity": 1.0},
        {"index": 0, "similarity": "1.0"},
        {"index": 0, "similarity": 1.5},
    ],
)
def test_rag_client_rank_vectors_rejects_malformed_response(
    mock_urlopen, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps(body).encode())
    assert rag_client.rank_vectors([1.0], [[1.0]]) == (None, False)


def test_rag_client_format_citation_fallback_disabled(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
    mock_urlopen.return_value = MockHTTPResponse(200, json.dumps({"citation": 123}).encode())
    result, used_rust = rag_client.format_citation("doc.md", 1, 2)
    assert result is None
    assert not used_rust


def test_rag_client_validate_index_fallback_disabled(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_FALLBACK", "0")
    mock_urlopen.return_value = MockHTTPResponse(200, b"not json")
    result, used_rust = rag_client.validate_index([{"id": "a", "text": "x"}])
    assert result is None
    assert not used_rust


def test_rag_client_http_error_body_read_fails(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    err = http_error(500, "error")

    def bad_read(*_args: object) -> bytes:
        raise OSError("read failed")

    err.read = bad_read  # type: ignore[assignment]
    mock_urlopen.side_effect = err
    result, used_rust = rag_client.normalize_query("hello")
    assert result is None
    assert not used_rust


# --- health.py ---


def test_rust_gateway_health_https_connection() -> None:
    class FakeResponse:
        status = 200

    class FakeHTTPSConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host = host
            self.port = port

        def request(self, method: str, path: str) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    with patch("http.client.HTTPSConnection", FakeHTTPSConnection):
        assert check_rust_gateway_health("https://127.0.0.1:8787") is True


@pytest.mark.parametrize("url", ["", "not-a-url", "file:///etc/passwd"])
def test_rust_gateway_health_invalid_urls(url: str) -> None:
    assert check_rust_gateway_health(url) is False
