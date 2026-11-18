from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    resilience_action_journal,
    resilience_scheduler_service,
    resilience_wave_executor,
)
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_resilience_wave_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    plan_src = (RUST_GATEWAY_SRC / "resilience_plan_routes.rs").read_text(
        encoding="utf-8"
    )
    route_src = (RUST_GATEWAY_SRC / "resilience_wave_routes.rs").read_text(
        encoding="utf-8"
    )
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(resilience_wave_routes::router())" in lib_src
    assert '"/api/workspace/resilience/execute"' in route_src
    assert '"/api/workspace/resilience/waves/admit"' in route_src
    assert '"/api/workspace/resilience/waves/run"' in route_src
    assert "NATIVE_RESILIENCE_STORE_WRITE_NOT_OWNED" in plan_src
    assert "write_not_owned" in route_src
    assert "python_is_de_authorised" in route_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("post", "/api/workspace/resilience/execute") not in python_only
    assert ("post", "/api/workspace/resilience/waves/admit") not in python_only
    assert ("post", "/api/workspace/resilience/waves/run") not in python_only


def test_resilience_wave_python_oracle_contracts(
    tmp_settings: Path,
) -> None:
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = _auth_headers()

    r_missing = client.post("/api/workspace/resilience/execute", headers=headers, json={})
    assert r_missing.status_code == 400

    r_raw = client.post(
        "/api/workspace/resilience/execute",
        headers=headers,
        json={"type": "START_DR_DRILL"},
    )
    assert r_raw.status_code == 400
    assert "Raw action execution not permitted" in r_raw.json()["error"]

    r_plan = client.post(
        "/api/workspace/resilience/plan", headers=headers, json={"probe": False}
    )
    assert r_plan.status_code == 200
    drill_act = next(
        a for a in r_plan.json()["actions"] if a["type"] == "START_DR_DRILL"
    )
    drill_aid = drill_act["actionId"]

    r_exec = client.post(
        "/api/workspace/resilience/execute",
        headers=headers,
        json={"actionId": drill_aid},
    )
    assert r_exec.status_code == 200
    assert r_exec.json()["state"] == "SUCCEEDED"
    assert r_exec.json()["decisionProof"]["executionVerified"] is True
    assert r_exec.json()["decisionProof"]["effectObserved"] is True

    r_not_found = client.post(
        "/api/workspace/resilience/execute",
        headers=headers,
        json={"actionId": "act_does_not_exist"},
    )
    assert r_not_found.status_code == 404

    r_admit_missing = client.post(
        "/api/workspace/resilience/waves/admit",
        headers=headers,
        json={"scheduleId": "sched_missing"},
    )
    assert r_admit_missing.status_code == 500
    assert r_admit_missing.json()["code"] == "internal"

    r_run_missing = client.post(
        "/api/workspace/resilience/waves/run",
        headers=headers,
        json={"scheduleId": "sched_missing"},
    )
    assert r_run_missing.status_code == 500
    assert r_run_missing.json()["code"] == "internal"

    r_plan2 = client.post(
        "/api/workspace/resilience/plan", headers=headers, json={"probe": False}
    )
    assert r_plan2.status_code == 200
    action_after = resilience_action_journal.get_action(drill_aid)
    assert action_after is not None and action_after["state"] == "SUCCEEDED"
    assert callable(resilience_wave_executor.admit_wave)
    assert callable(resilience_scheduler_service.settle_action_from_effect)
