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


def test_phase5_workspace_routes_are_not_declared_inline_in_server() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_workspace_router(_workspace_route_deps())" in server_source
    for decorator in [
        '@api.post("/api/projects")',
        '@api.get("/api/workspace/projects")',
        '@api.post("/api/workspace/projects")',
        '@api.post("/api/workspace/exports")',
        '@api.post("/api/project-files")',
    ]:
        assert decorator not in server_source


def test_phase6_skill_routes_are_not_declared_inline_in_server() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "/api/skills" in paths
    assert "/api/skills/{skill_id}/run" in paths
    assert "create_skills_router(_skills_route_deps())" in server_source
    for decorator in [
        '@api.post("/api/skills")',
        '@api.post("/api/skills/{skill_id}/run")',
    ]:
        assert decorator not in server_source


def test_phase7_media_routes_are_not_declared_inline_in_server() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "/api/media" in paths
    assert "/api/media/{media_id}" in paths
    assert "/api/media/{media_id}/process" in paths
    assert "/api/media/{media_id}/segments" in paths
    assert "create_media_router(_media_route_deps())" in server_source
    for decorator in [
        '@api.post("/api/media")',
        '@api.get("/api/media")',
        '@api.get("/api/media/{media_id}")',
        '@api.post("/api/media/{media_id}/process")',
    ]:
        assert decorator not in server_source


def test_phase8_automation_routes_are_not_declared_inline_in_server() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "/api/automation" in paths
    assert "/api/automation/{automation_id}/run" in paths
    assert "/api/automation/{automation_id}/runs" in paths
    assert "create_automation_router()" in server_source
    for decorator in [
        '@api.get("/api/automation")',
        '@api.post("/api/automation")',
        '@api.post("/api/automation/{automation_id}/run")',
    ]:
        assert decorator not in server_source


def test_phase_final_chat_routes_split_and_api_surface_intact() -> None:
    server_source = Path(server_module.__file__).read_text(encoding="utf-8")

    assert "create_chat_router(_chat_route_deps())" in server_source
    for decorator in [
        '@api.post("/api/chat")',
        '@api.post("/api/title")',
        '@api.post("/api/conversations/search")',
        '@api.post("/v1/chat/completions")',
        '@api.get("/v1/models")',
    ]:
        assert decorator not in server_source

    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert hasattr(server_module, "FastAPIServer")
