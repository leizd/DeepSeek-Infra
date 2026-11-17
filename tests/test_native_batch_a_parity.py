from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.gateway import deepseek_client, edge_inference
from deepseek_infra.infra.mcp import bridge as mcp_bridge
from deepseek_infra.infra.mcp import client as mcp_client
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_batch_a_routes_registered_and_removed_from_python_only_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    expected_modules = [
        ("edge_action_routes", ["/api/edge/reload", "/api/edge/route-preview"]),
        ("external_mcp_routes", ["/api/mcp/external/tools"]),
        ("semantic_cache_routes", ["/api/semantic-cache/status"]),
        ("backup_capacity_routes", ["/api/workspace/backup-capacity/summary"]),
    ]
    for module_name, route_paths in expected_modules:
        assert f".merge({module_name}::router())" in lib_src
        src = (RUST_GATEWAY_SRC / f"{module_name}.rs").read_text(encoding="utf-8")
        for route_path in route_paths:
            assert f'"{route_path}"' in src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    for method, route_path in (
        ("post", "/api/edge/reload"),
        ("post", "/api/edge/route-preview"),
        ("get", "/api/mcp/external/tools"),
        ("get", "/api/semantic-cache/status"),
        ("get", "/api/workspace/backup-capacity/summary"),
    ):
        assert (method, route_path) not in python_only


def test_edge_current_query_pattern_matches_rust_constant() -> None:
    edge_action_src = (RUST_GATEWAY_SRC / "edge_action_routes.rs").read_text(encoding="utf-8")
    match = re.search(
        r'pub const CURRENT_QUERY_PATTERN:\s*&str\s*=\s*r"([^"]+)";',
        edge_action_src,
    )
    assert match is not None
    assert match.group(1) == edge_inference.CURRENT_QUERY_RE.pattern


def test_batch_a_python_oracle_envelopes_and_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    patched = replace(
        settings,
        edge=replace(settings.edge, enabled=True, provider="fake", mode="auto"),
        deepseek_api_key="sk-test-batch-a",
        mcp=replace(settings.mcp, client_enabled=False, client_servers=()),
    )
    for mod in (edge_inference, deepseek_client, mcp_bridge, mcp_client):
        monkeypatch.setattr(mod, "settings", patched)

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    cache_resp = client.get("/api/semantic-cache/status", headers=headers)
    assert cache_resp.status_code == 200
    cache_body = cache_resp.json()
    assert cache_body["ok"] is True
    assert set(cache_body["semanticCache"]) == {
        "enabled",
        "databasePath",
        "similarityThreshold",
        "ttlSeconds",
        "maxItems",
        "items",
        "hits",
        "embeddingProvider",
        "embeddingDimensions",
        "cacheVersion",
        "minQualityScore",
        "cacheAttachments",
        "lastError",
    }

    reload_ok = client.post("/api/edge/reload", headers=headers, json={})
    assert reload_ok.status_code == 200
    assert reload_ok.json()["edgeInference"]["loaded"] is False

    reload_bad = client.post("/api/edge/reload", headers=headers, json={"action": "bad"})
    assert reload_bad.status_code == 400
    assert reload_bad.json()["code"] == "invalid_payload"

    preview_simple = client.post(
        "/api/edge/route-preview",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert preview_simple.status_code == 200
    assert preview_simple.json()["useEdge"] is True
    assert preview_simple.json()["reason"] == "simple_task_local"

    preview_409 = client.post(
        "/api/edge/route-preview",
        headers=headers,
        json={
            "edgeMode": "local",
            "edgeProvider": "unknown_backend",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert preview_409.status_code == 409
    assert preview_409.json()["code"] == "invalid_payload"
    assert "Unsupported edge inference provider" in preview_409.json()["error"]

    mcp_ext = client.get("/api/mcp/external/tools", headers=headers)
    assert mcp_ext.status_code == 200
    assert mcp_ext.json() == {"ok": True, "servers": [], "tools": []}

    cap = client.get("/api/workspace/backup-capacity/summary", headers=headers)
    assert cap.status_code == 200
    assert cap.json() == {"overallStatus": "healthy", "targets": []}
