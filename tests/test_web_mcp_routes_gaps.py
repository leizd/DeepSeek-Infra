"""Gap tests for MCP routes."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.mcp import McpRouteDeps, create_mcp_router


@pytest.fixture
def mcp_client() -> Iterator[TestClient]:
    deps = McpRouteDeps(
        mcp_enabled=lambda: True,
        handle_mcp_message=lambda body: {"jsonrpc": "2.0", "id": 1, "result": []},
        list_external_mcp_tools=lambda: {"ok": True, "tools": []},
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_mcp_router(deps))
    with patch("deepseek_infra.web.routes.mcp.require_api_auth", lambda request: None):
        yield TestClient(app)


@pytest.fixture
def mcp_disabled_client() -> Iterator[TestClient]:
    deps = McpRouteDeps(
        mcp_enabled=lambda: False,
        handle_mcp_message=lambda body: None,
        list_external_mcp_tools=lambda: {"ok": True, "tools": []},
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_mcp_router(deps))
    with patch("deepseek_infra.web.routes.mcp.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_mcp_endpoint_disabled(mcp_disabled_client: TestClient) -> None:
    resp = mcp_disabled_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 403


def test_mcp_rust_proxy_passes_authorization_header(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with patch("deepseek_infra.web.routes.mcp.rust_mcp_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.mcp.proxy_mcp_to_rust", return_value=SimpleNamespace(ok=True, body={"tools": []})) as proxy:
        resp = mcp_client.post("/mcp", json=body, headers={"Authorization": "Bearer token"})
    assert resp.status_code == 200
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}


def test_mcp_rust_proxy_ok_with_empty_auth_headers(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with patch("deepseek_infra.web.routes.mcp.rust_mcp_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.mcp.proxy_mcp_to_rust", return_value=SimpleNamespace(ok=True, body={"tools": []})) as proxy, \
         patch("deepseek_infra.web.routes.mcp.fallback_to_python_enabled", return_value=False):
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"tools": []}
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {}


def test_mcp_rust_proxy_returns_202_on_empty_body(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}
    with patch("deepseek_infra.web.routes.mcp.rust_mcp_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.mcp.proxy_mcp_to_rust", return_value=SimpleNamespace(ok=True, body=None)):
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 202
    assert resp.content == b""


def test_mcp_rust_proxy_unavailable_without_fallback_raises(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with patch("deepseek_infra.web.routes.mcp.rust_mcp_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.mcp.proxy_mcp_to_rust", return_value=SimpleNamespace(ok=False, body="rust down")), \
         patch("deepseek_infra.web.routes.mcp.fallback_to_python_enabled", return_value=False):
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 502
