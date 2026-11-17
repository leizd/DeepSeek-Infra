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


def test_dr_slo_route_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    slo_src = (RUST_GATEWAY_SRC / "backup_dr_slo_routes.rs").read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(backup_dr_slo_routes::router())" in lib_src
    assert '"/api/workspace/disaster-recovery/slo"' in slo_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/disaster-recovery/slo") not in python_only


def test_dr_slo_python_oracle_empty_workspace_shape(tmp_settings: Path) -> None:
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    resp = client.get("/api/workspace/disaster-recovery/slo", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "restoreSuccessRate",
        "targetSuccessRate",
        "successRateHealthy",
        "rtoSeconds",
        "rpoSeconds",
        "evidenceFreshnessDays",
        "targetFreshnessDays",
        "freshnessHealthy",
        "totalDrillsTested",
        "overallSloCompliant",
        "evaluatedAt",
    }
    assert body["totalDrillsTested"] == 0
    assert body["restoreSuccessRate"] == 1.0
    assert body["overallSloCompliant"] is True
