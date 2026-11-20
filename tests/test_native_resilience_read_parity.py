from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_resilience_read_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    src = (RUST_GATEWAY_SRC / "resilience_read_routes.rs").read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(resilience_read_routes::router())" in lib_src
    for route_path in (
        "/api/workspace/resilience/journal",
        "/api/workspace/resilience/capacity-forecast",
        "/api/workspace/resilience/waves",
    ):
        assert f'"{route_path}"' in src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/resilience/journal") not in python_only
    assert ("get", "/api/workspace/resilience/capacity-forecast") not in python_only
    assert ("get", "/api/workspace/resilience/waves") not in python_only


def test_resilience_read_python_oracle_empty_shapes(tmp_settings: Path) -> None:
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    journal = client.get("/api/workspace/resilience/journal", headers=headers)
    assert journal.status_code == 200
    assert journal.json() == []

    cf = client.get("/api/workspace/resilience/capacity-forecast", headers=headers)
    assert cf.status_code == 200
    cf_body = cf.json()
    assert cf_body["horizonDays"] == 90
    assert cf_body["targets"] == []
    assert cf_body["source"] == "durable-forecast-registry"

    waves = client.get("/api/workspace/resilience/waves", headers=headers)
    assert waves.status_code == 200
    assert waves.json() == {"schedule": None, "waves": [], "actions": []}
