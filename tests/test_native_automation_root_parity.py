from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.data import projects
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_automation_root_registered_and_removed_from_python_only_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    root_src = (RUST_GATEWAY_SRC / "automation_root_routes.rs").read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(automation_root_routes::router())" in lib_src
    assert '"/api/automation"' in root_src
    for action in (
        '"list"',
        '"create"',
        '"get"',
        '"update"',
        '"enable"',
        '"disable"',
        '"delete"',
        '"run"',
        '"rerun"',
        '"list_runs"',
        '"templates"',
        '"create_from_template"',
        '"simulate"',
        '"run_due"',
    ):
        assert action in root_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/automation") not in python_only
    assert ("post", "/api/automation") not in python_only


def test_automation_root_python_oracle_action_parity(tmp_settings: Path) -> None:
    project = projects.create_project("Native Automation Parity Project")
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    listed = client.get("/api/automation", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == {"ok": True, "automations": []}

    created = client.post(
        "/api/automation",
        headers=headers,
        json={
            "action": "create",
            "automation": {
                "projectId": project["id"],
                "name": "Parity Digest",
                "trigger": {"type": "event", "event": "media.ready"},
                "condition": {"type": "always"},
                "action": {"type": "save_item", "title": "parity"},
            },
        },
    )
    assert created.status_code == 200
    aid = created.json()["automation"]["automationId"]

    sim = client.post(
        "/api/automation",
        headers=headers,
        json={"action": "simulate", "automationId": aid, "event": {"type": "media.ready"}},
    )
    assert sim.status_code == 200
    assert sim.json()["wouldRun"] is True

    due = client.post(
        "/api/automation",
        headers=headers,
        json={"action": "run_due", "event": {"type": "media.ready"}},
    )
    assert due.status_code == 200
    assert due.json()["dueCount"] == 1
    run_id = due.json()["runs"][0]["runId"]

    rerun = client.post(
        "/api/automation",
        headers=headers,
        json={"action": "rerun", "runId": run_id},
    )
    assert rerun.status_code == 200
    assert rerun.json()["run"]["runId"] != run_id

    bad = client.post(
        "/api/automation",
        headers=headers,
        json={"action": "not_real"},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_payload"
