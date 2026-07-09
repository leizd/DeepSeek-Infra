from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.web.routes.status import StatusRouteDeps, create_status_router


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = SimpleNamespace(
        auth=SimpleNamespace(enabled=False, token=""),
        deepseek_api_key="",
        default_model="deepseek-v4-pro",
        ocr=SimpleNamespace(enabled=False, mode="balanced"),
    )
    deps = StatusRouteDeps(
        version="3.0.1",
        settings=settings,
        tavily_api_key="",
        supported_models=["deepseek-v4-pro"],
        model_routes={"expert": "deepseek-v4-pro"},
        max_upload_file_bytes=100,
        max_upload_bytes=200,
        max_multipart_files=10,
        local_ip=lambda: "192.168.1.2",
        url_with_token=lambda url, token: f"{url}?token={token}",
        edge_inference_status=lambda: {"ok": True},
        local_rag_status=lambda: {"ok": True},
        trace_status=lambda: {"ok": True},
        semantic_cache_status=lambda: {"ok": True},
        gateway_status=lambda: {"ok": True},
        providers_status=lambda: {"ok": True},
        model_router_status=lambda: {"ok": True},
        budget_status=lambda scope: {"scope": scope, "ok": True},
        tool_policy_status=lambda: {"ok": True},
        read_recent_audit=lambda limit: [{"limit": limit}],
        scheduler_status=lambda: {"ok": True},
        scheduler_dead_letters=lambda limit: [{"limit": limit}],
        mcp_status=lambda: {"ok": True},
        a2a_status=lambda: {"ok": True},
        taint_status=lambda: {"ok": True},
        rust_status=lambda: {"ok": True},
    )
    app = FastAPI()
    app.include_router(create_status_router(deps))
    with patch("deepseek_infra.web.routes.status.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_api_config(client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "3.0.1"
    assert data["defaultModel"] == "deepseek-v4-pro"
    assert data["computerUrl"].startswith("http://127.0.0.1:")
    assert data["phoneUrl"].startswith("http://192.168.1.2:")


def test_api_config_with_auth_enabled() -> None:
    settings = SimpleNamespace(
        auth=SimpleNamespace(enabled=True, token="secret-token"),
        deepseek_api_key="",
        default_model="deepseek-v4-pro",
        ocr=SimpleNamespace(enabled=False, mode="balanced"),
    )
    deps = StatusRouteDeps(
        version="3.0.1",
        settings=settings,
        tavily_api_key="",
        supported_models=["deepseek-v4-pro"],
        model_routes={},
        max_upload_file_bytes=100,
        max_upload_bytes=200,
        max_multipart_files=10,
        local_ip=lambda: "192.168.1.2",
        url_with_token=lambda url, token: f"{url}?token={token}",
        edge_inference_status=lambda: {},
        local_rag_status=lambda: {},
        trace_status=lambda: {},
        semantic_cache_status=lambda: {},
        gateway_status=lambda: {},
        providers_status=lambda: {},
        model_router_status=lambda: {},
        budget_status=lambda scope: {},
        tool_policy_status=lambda: {},
        read_recent_audit=lambda limit: [],
        scheduler_status=lambda: {},
        scheduler_dead_letters=lambda limit: [],
        mcp_status=lambda: {},
        a2a_status=lambda: {},
        taint_status=lambda: {},
        rust_status=lambda: {},
    )
    app = FastAPI()
    app.include_router(create_status_router(deps))
    with patch("deepseek_infra.web.routes.status.require_api_auth", lambda request: None):
        test_client = TestClient(app)
        response = test_client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "token=secret-token" in data["computerUrl"]
    assert "token=secret-token" in data["phoneUrl"]


def test_api_rag_status(client: TestClient) -> None:
    response = client.get("/api/rag/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_budget_default_scope(client: TestClient) -> None:
    response = client.get("/api/budget")
    assert response.status_code == 200
    assert response.json()["budget"]["scope"] == "global"


def test_api_budget_custom_scope(client: TestClient) -> None:
    response = client.get("/api/budget?scope=project1")
    assert response.status_code == 200
    assert response.json()["budget"]["scope"] == "project1"


def test_api_tool_policy_default_limit(client: TestClient) -> None:
    response = client.get("/api/tool-policy")
    assert response.status_code == 200
    assert response.json()["audit"][0]["limit"] == 50


def test_api_tool_policy_custom_and_invalid_limit(client: TestClient) -> None:
    response = client.get("/api/tool-policy?limit=10")
    assert response.status_code == 200
    assert response.json()["audit"][0]["limit"] == 10

    response = client.get("/api/tool-policy?limit=abc")
    assert response.status_code == 200
    assert response.json()["audit"][0]["limit"] == 50


def test_api_scheduler_default_limit(client: TestClient) -> None:
    response = client.get("/api/scheduler")
    assert response.status_code == 200
    assert response.json()["deadLetters"][0]["limit"] == 50


def test_api_scheduler_custom_and_invalid_limit(client: TestClient) -> None:
    response = client.get("/api/scheduler?limit=10")
    assert response.status_code == 200
    assert response.json()["deadLetters"][0]["limit"] == 10

    response = client.get("/api/scheduler?limit=abc")
    assert response.status_code == 200
    assert response.json()["deadLetters"][0]["limit"] == 50


def test_api_mcp_status(client: TestClient) -> None:
    response = client.get("/api/mcp")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_taint_status(client: TestClient) -> None:
    response = client.get("/api/taint")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_semantic_cache_status(client: TestClient) -> None:
    response = client.get("/api/semantic-cache/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_gateway_status(client: TestClient) -> None:
    response = client.get("/api/gateway/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_edge_status(client: TestClient) -> None:
    response = client.get("/api/edge/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_rust_status(client: TestClient) -> None:
    response = client.get("/api/rust/status")
    assert response.status_code == 200
    assert response.json()["ok"] is True
