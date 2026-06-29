from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

import deepseek_infra.web.http_utils as http_utils
import deepseek_infra.web.server as server_module


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


def test_create_app_still_returns_fastapi_app() -> None:
    app = server_module.create_app()

    assert isinstance(app, FastAPI)


def test_phase1_status_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    for expected in {
        "/api/config",
        "/api/rag/status",
        "/api/budget",
        "/api/tool-policy",
        "/api/scheduler",
        "/api/mcp",
        "/api/taint",
        "/api/semantic-cache/status",
        "/api/gateway/status",
        "/api/edge/status",
    }:
        assert expected in paths


def test_legacy_server_entrypoints_and_http_helpers_remain_available() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert hasattr(server_module, "FastAPIServer")
    assert server_module.json_response is http_utils.json_response
    assert server_module.read_json_body is http_utils.read_json_body
    assert server_module.require_api_auth is http_utils.require_api_auth


def test_phase1_status_routes_are_not_declared_inline_in_server() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_status_router(_status_route_deps())" in server_source
    for decorator in [
        '@api.get("/api/config")',
        '@api.get("/api/rag/status")',
        '@api.get("/api/budget")',
        '@api.get("/api/tool-policy")',
        '@api.get("/api/scheduler")',
        '@api.get("/api/mcp")',
        '@api.get("/api/taint")',
        '@api.get("/api/semantic-cache/status")',
        '@api.get("/api/gateway/status")',
        '@api.get("/api/edge/status")',
    ]:
        assert decorator not in server_source


def test_phase2_file_and_download_routes_are_not_declared_inline_in_server() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_files_router(_files_route_deps())" in server_source
    assert "create_downloads_router(_downloads_route_deps())" in server_source
    for decorator in [
        '@api.get("/api/download")',
        '@api.get("/api/file-source")',
        '@api.get("/api/file-page-image")',
        '@api.get("/api/file-page-layout")',
        '@api.get("/api/file-page-search")',
    ]:
        assert decorator not in server_source


def test_phase3_rag_and_memory_routes_are_not_declared_inline_in_server() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_rag_router(_rag_route_deps())" in server_source
    assert "create_memory_router(_memory_route_deps())" in server_source
    for decorator in [
        '@api.post("/api/rag/reindex")',
        '@api.post("/api/rag/verify-citation")',
        '@api.post("/api/rag/eval")',
        '@api.get("/api/memory")',
        '@api.post("/api/memory")',
    ]:
        assert decorator not in server_source


def test_phase4_mcp_a2a_edge_routes_are_not_declared_inline_in_server() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_mcp_router(_mcp_route_deps())" in server_source
    assert "create_a2a_router(_a2a_route_deps())" in server_source
    assert "create_edge_router(_edge_route_deps())" in server_source
    for decorator in [
        '@api.post("/mcp")',
        '@api.get("/api/mcp/external/tools")',
        '@api.get("/.well-known/agent-card.json")',
        '@api.get("/a2a/agents")',
        '@api.post("/a2a")',
        '@api.post("/a2a/agents/{agent_id}")',
        '@api.post("/api/edge/reload")',
    ]:
        assert decorator not in server_source
