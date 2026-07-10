"""Gap tests for A2A routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.a2a import A2ARouteDeps, create_a2a_router


@pytest.fixture
def a2a_disabled_client() -> Iterator[TestClient]:
    deps = A2ARouteDeps(
        a2a_enabled=lambda: False,
        agent_card=lambda *args, **kwargs: {"name": "agent"},
        agent_cards=lambda *args, **kwargs: [],
        handle_a2a_message=lambda *args, **kwargs: {"ok": True},
        is_stream_request=lambda body: False,
        stream_message_events=lambda *args, **kwargs: iter([]),
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_a2a_router(deps))
    with patch("deepseek_infra.web.routes.a2a.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_well_known_agent_card_disabled(a2a_disabled_client: TestClient) -> None:
    resp = a2a_disabled_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 403


def test_a2a_agents_disabled(a2a_disabled_client: TestClient) -> None:
    resp = a2a_disabled_client.get("/a2a/agents")
    assert resp.status_code == 403


def test_a2a_endpoint_disabled(a2a_disabled_client: TestClient) -> None:
    resp = a2a_disabled_client.post("/a2a", json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 403


def test_a2a_agent_endpoint_disabled(a2a_disabled_client: TestClient) -> None:
    resp = a2a_disabled_client.post("/a2a/agents/researcher", json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 403


@pytest.fixture
def a2a_enabled_client() -> Iterator[TestClient]:
    deps = A2ARouteDeps(
        a2a_enabled=lambda: True,
        agent_card=lambda *args, **kwargs: {"name": "agent"},
        agent_cards=lambda *args, **kwargs: [{"name": "researcher"}],
        handle_a2a_message=lambda *args, **kwargs: {"ok": True},
        is_stream_request=lambda body: False,
        stream_message_events=lambda *args, **kwargs: iter([]),
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_a2a_router(deps))
    with patch("deepseek_infra.web.routes.a2a.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_a2a_agents_enabled(a2a_enabled_client: TestClient) -> None:
    resp = a2a_enabled_client.get("/a2a/agents")
    assert resp.status_code == 200
    assert resp.json()["agents"] == [{"name": "researcher"}]
