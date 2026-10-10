"""Targeted test coverage boosters for server route dependency factories and lambdas."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.web import server


def test_server_route_dependency_lambdas(tmp_settings: Path) -> None:
    # 1. Status route deps lambdas
    status_deps = server._status_route_deps()
    assert status_deps.version is not None
    assert status_deps.local_ip() is not None
    assert status_deps.edge_inference_status() is not None
    assert status_deps.local_rag_status() is not None
    assert status_deps.trace_status() is not None
    assert status_deps.semantic_cache_status() is not None
    assert status_deps.gateway_status() is not None
    assert status_deps.providers_status() is not None
    assert status_deps.model_router_status() is not None
    assert status_deps.budget_status("global") is not None
    assert status_deps.tool_policy_status() is not None
    assert status_deps.read_recent_audit(10) is not None
    assert status_deps.scheduler_status() is not None
    assert status_deps.scheduler_dead_letters(10) is not None
    assert status_deps.mcp_status() is not None
    assert status_deps.a2a_status() is not None
    assert status_deps.taint_status() is not None
    assert status_deps.rust_status() is not None

    # 2. Files route deps lambdas
    files_deps = server._files_route_deps()
    with pytest.raises(Exception):
        files_deps.cached_file_source("0123456789abcdef0123456789abcdef", "")

    # 3. Downloads route deps lambdas
    downloads_deps = server._downloads_route_deps()
    assert downloads_deps.resolve_generated_file("nonexistent") is None

    # 4. RAG route deps lambdas
    rag_deps = server._rag_route_deps()
    assert rag_deps.rebuild_local_rag_index() is not None

    # 5. Memory route deps lambdas
    mem_deps = server._memory_route_deps()
    assert mem_deps.load_memories() is not None
    assert mem_deps.normalize_memory_category("preference", "content") == "preference"
    assert mem_deps.normalize_memory_scope("global") == "global"

    # 6. MCP route deps lambdas
    mcp_deps = server._mcp_route_deps()
    assert isinstance(mcp_deps.mcp_enabled(), bool)
    assert mcp_deps.list_external_mcp_tools() is not None

    # 7. A2A route deps lambdas
    a2a_deps = server._a2a_route_deps()
    assert isinstance(a2a_deps.a2a_enabled(), bool)
    assert a2a_deps.agent_cards() is not None

    # 8. Edge route deps lambdas
    edge_deps = server._edge_route_deps()
    assert edge_deps.edge_unload() is not None

    # 9. Skills route deps lambdas
    skills_deps = server._skills_route_deps()
    assert skills_deps.list_skills(include_disabled=True) is not None
    assert skills_deps.list_builtin_skills(include_disabled=True) is not None
    assert skills_deps.list_packs(include_builtin=True) is not None
