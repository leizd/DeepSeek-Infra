"""Tests for Rust Gateway opt-in proxy integration."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest

import deepseek_infra.web.server as server_module
from deepseek_infra.infra.rust_core.gateway_client import (
    GatewayProxyResult,
    fallback_to_python_enabled,
)


def _collect_route_paths(routes: list[Any]) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= _collect_route_paths(getattr(original, "routes", []))
    return paths


@contextlib.contextmanager
def _running_server() -> Iterator[Any]:
    server, _ = server_module.create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: Any,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, response
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


@pytest.fixture(autouse=True)
def _clear_rust_gateway_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS", raising=False)
    yield


# --- config / client behavior ---


def test_fallback_enabled_by_default() -> None:
    assert fallback_to_python_enabled() is True


def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", "0")
    assert fallback_to_python_enabled() is False


# --- route behavior ---


def test_rust_gateway_proxy_disabled_uses_python_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "0")
    with patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust") as proxy, \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion", return_value={"choices": []}) as python:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_not_called()
        python.assert_called_once()
    assert status == HTTPStatus.OK


def test_rust_gateway_proxy_enabled_forwards_chat_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    fake_response = {"id": "rust-chat", "choices": [{"message": {"content": "from rust"}}]}
    with patch(
        "deepseek_infra.web.routes.chat.proxy_chat_to_rust",
        return_value=GatewayProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload == fake_response


def test_rust_gateway_proxy_enabled_forwards_models_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    fake_response = {"data": [{"id": "rust-model"}]}
    with patch(
        "deepseek_infra.web.routes.chat.proxy_models_to_rust",
        return_value=GatewayProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "GET",
                "/v1/models",
                headers=_auth_headers(),
            )
        proxy.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload == fake_response


def test_rust_gateway_unreachable_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    with patch(
        "deepseek_infra.web.routes.chat.proxy_chat_to_rust",
        return_value=GatewayProxyResult(ok=False, status=0, body="connection refused"),
    ), patch(
        "deepseek_infra.web.routes.chat.openai_chat_completion",
        return_value={"choices": [{"message": {"content": "fallback"}}]},
    ) as python:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
        python.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["choices"][0]["message"]["content"] == "fallback"


def test_rust_gateway_timeout_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS", "1")
    with patch(
        "deepseek_infra.web.routes.chat.proxy_chat_to_rust",
        return_value=GatewayProxyResult(ok=False, status=0, body="timeout"),
    ), patch(
        "deepseek_infra.web.routes.chat.openai_chat_completion",
        return_value={"choices": [{"message": {"content": "fallback"}}]},
    ) as python:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
        python.assert_called_once()
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["choices"][0]["message"]["content"] == "fallback"


def test_rust_gateway_no_fallback_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", "0")
    with patch(
        "deepseek_infra.web.routes.chat.proxy_chat_to_rust",
        return_value=GatewayProxyResult(ok=False, status=0, body="connection refused"),
    ):
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.BAD_GATEWAY
    assert payload["code"] == "upstream_failure"


def test_rust_gateway_preserves_authorization_header_if_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    fake_response = {"choices": [{"message": {"content": "ok"}}]}
    with patch(
        "deepseek_infra.web.routes.chat.proxy_chat_to_rust",
        return_value=GatewayProxyResult(ok=True, status=200, body=fake_response),
    ) as proxy:
        with _running_server() as server:
            _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                headers=_auth_headers(),
            )
        call_kwargs = proxy.call_args.kwargs
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"].startswith("Bearer ")


def test_streaming_request_bypasses_rust_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    with patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust") as proxy:
        with _running_server() as server:
            status, data, _ = _request(
                server,
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode("utf-8"),
                headers=_auth_headers(),
            )
        proxy.assert_not_called()
    assert status == HTTPStatus.OK
