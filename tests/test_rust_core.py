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
from deepseek_infra.infra.rust_core import (
    RustComponentFlags,
    check_rust_gateway_health,
    load_rust_flags,
    rust_gateway_url,
    rust_status,
)
from deepseek_infra.infra.rust_core.registry import RustRegistry


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
def _clear_rust_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "DEEPSEEK_RUST_GATEWAY",
        "DEEPSEEK_RUST_MCP",
        "DEEPSEEK_RUST_POLICY",
        "DEEPSEEK_RUST_RAG",
        "DEEPSEEK_RUST_GATEWAY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# --- config ---


def test_load_rust_flags_defaults_disabled() -> None:
    flags = load_rust_flags()
    assert flags == RustComponentFlags(
        gateway=False, mcp=False, policy=False, rag=False
    )


def test_load_rust_flags_reads_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_MCP", "true")
    monkeypatch.setenv("DEEPSEEK_RUST_POLICY", "yes")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "on")
    flags = load_rust_flags()
    assert flags == RustComponentFlags(
        gateway=True, mcp=True, policy=True, rag=True
    )


def test_rust_gateway_url_defaults_to_localhost_8787() -> None:
    assert rust_gateway_url() == "http://127.0.0.1:8787"


def test_rust_gateway_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_URL", "http://localhost:9999")
    assert rust_gateway_url() == "http://localhost:9999"


# --- health ---


def test_rust_gateway_health_unreachable_returns_false() -> None:
    assert check_rust_gateway_health("http://127.0.0.1:1", timeout=0.1) is False


def test_rust_gateway_health_rejects_invalid_scheme() -> None:
    assert check_rust_gateway_health("ftp://127.0.0.1:8787") is False


def test_rust_gateway_health_success_returns_true_with_mock() -> None:
    class _FakeResponse:
        status = HTTPStatus.OK

    class _FakeConnection:
        def request(self, method: str, path: str) -> None:
            pass

        def getresponse(self) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    with patch("http.client.HTTPConnection", return_value=_FakeConnection()):
        assert check_rust_gateway_health("http://127.0.0.1:8787") is True


# --- registry / status ---


def test_rust_status_defaults_disabled() -> None:
    status = rust_status()
    assert status["enabled"] == {
        "gateway": False,
        "mcp": False,
        "policy": False,
        "rag": False,
    }
    gateway = status["components"]["gateway"]
    assert gateway["enabled"] is False
    assert gateway["url"] == ""
    assert gateway["healthy"] is False


def test_rust_status_reads_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_URL", "http://localhost:9999")
    status = rust_status()
    assert status["enabled"]["gateway"] is True
    assert status["components"]["gateway"]["url"] == "http://localhost:9999"


def test_rust_status_gateway_healthy_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    with patch(
        "deepseek_infra.infra.rust_core.registry.check_rust_gateway_health",
        return_value=True,
    ):
        status = rust_status()
    assert status["components"]["gateway"]["healthy"] is True


def test_rust_status_gateway_unhealthy_when_disabled() -> None:
    registry = RustRegistry()
    with patch(
        "deepseek_infra.infra.rust_core.registry.check_rust_gateway_health",
        return_value=True,
    ):
        status = registry.status()
    assert status["components"]["gateway"]["healthy"] is False


# --- web route ---


def test_rust_status_route_is_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)
    assert "/api/rust/status" in paths


def test_rust_status_route_requires_auth_if_existing_api_needs_auth() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/rust/status")
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.UNAUTHORIZED
    assert payload["code"] == server_module.ErrorCode.UNAUTHORIZED.value

    with _running_server() as server:
        status, data, _ = _request(
            server, "GET", "/api/rust/status", headers=_auth_headers()
        )
    payload = json.loads(data.decode("utf-8"))
    assert status == HTTPStatus.OK
    assert payload["ok"] is True
    assert "rust" in payload
