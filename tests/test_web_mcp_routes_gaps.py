"""Gap tests for MCP routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.mcp.protocol_preparation import McpProtocolDecision, prepare_mcp_protocol
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


def test_mcp_protocol_preparation_never_receives_authorization_header(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    decision = McpProtocolDecision(
        prepare_mcp_protocol(body),
        {"runtime": "rust", "fallback": False, "fallbackReason": "", "latencyMs": 1},
    )
    with patch("deepseek_infra.web.routes.mcp.prepare_mcp_protocol_with_optional_rust", return_value=decision) as prepare:
        resp = mcp_client.post("/mcp", json=body, headers={"Authorization": "Bearer token"})
    assert resp.status_code == 200
    prepare.assert_called_once_with(body)
    assert resp.headers["X-DeepSeek-MCP-Preparation-Runtime"] == "rust"


def test_mcp_protocol_preparation_success_uses_python_handler(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    decision = McpProtocolDecision(
        prepare_mcp_protocol(body),
        {"runtime": "python", "fallback": True, "fallbackReason": "rust_backend_unavailable", "latencyMs": 2},
    )
    with patch("deepseek_infra.web.routes.mcp.prepare_mcp_protocol_with_optional_rust", return_value=decision):
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 1, "result": []}
    assert resp.headers["X-DeepSeek-MCP-Preparation-Fallback"] == "1"
    assert resp.headers["X-DeepSeek-MCP-Preparation-Fallback-Reason"] == "rust_backend_unavailable"


def test_mcp_protocol_notification_error_returns_202(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "method": "notifications/unknown"}
    decision = McpProtocolDecision(
        prepare_mcp_protocol(body),
        {"runtime": "python", "fallback": False, "fallbackReason": "", "latencyMs": 0},
    )
    with patch("deepseek_infra.web.routes.mcp.prepare_mcp_protocol_with_optional_rust", return_value=decision):
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 202
    assert resp.content == b""


def test_mcp_protocol_invalid_request_returns_stable_jsonrpc_error(mcp_client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
    with patch("deepseek_infra.web.routes.mcp.prepare_mcp_protocol_with_optional_rust") as prepare:
        preparation = prepare_mcp_protocol(body)
        prepare.return_value = McpProtocolDecision(
            preparation,
            {"runtime": "python", "fallback": False, "fallbackReason": "", "latencyMs": 0},
        )
        resp = mcp_client.post("/mcp", json=body)
    assert resp.status_code == 200
    assert resp.json()["error"]["data"]["code"] == "invalid_params"
