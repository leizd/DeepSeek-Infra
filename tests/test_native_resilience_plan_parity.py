from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
    resilience_action_journal,
    resilience_coordinator,
    resilience_planner,
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


def test_resilience_plan_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    route_src = (RUST_GATEWAY_SRC / "resilience_plan_routes.rs").read_text(
        encoding="utf-8"
    )
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(resilience_plan_routes::router())" in lib_src
    assert '"/api/workspace/resilience/snapshot"' in route_src
    assert '"/api/workspace/resilience/assess"' in route_src
    assert '"/api/workspace/resilience/plan"' in route_src
    assert '"/api/workspace/resilience/coordination-plan"' in route_src
    assert "NATIVE_RESILIENCE_STORE_WRITE_NOT_OWNED" in route_src
    assert "python_is_de_authorised" in route_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/resilience/snapshot") not in python_only
    assert ("post", "/api/workspace/resilience/assess") not in python_only
    assert ("post", "/api/workspace/resilience/plan") not in python_only
    assert ("get", "/api/workspace/resilience/coordination-plan") not in python_only
    assert ("post", "/api/workspace/resilience/coordination-plan") not in python_only


def test_resilience_plan_python_oracle_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    monkeypatch.setattr(
        backup_targets, "_containment_violation", lambda *args, **kwargs: None
    )
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = _auth_headers()

    # 1. Empty workspace oracle baseline for GET /snapshot and GET /coordination-plan
    r_snap0 = client.get("/api/workspace/resilience/snapshot", headers=headers)
    assert r_snap0.status_code == 200
    snap0 = r_snap0.json()
    assert snap0["score"] == 75
    assert snap0["grade"] == "C"
    assert snap0["overallRisk"] == "critical"
    assert snap0["criticalRisks"] == 1
    assert snap0["plannedActions"] == 0
    assert snap0["runningActions"] == 0
    assert snap0["factorBreakdown"] == {
        "drDrill": {"score": 0, "max": 25, "status": "critical"},
        "replicaHealth": {"score": 25, "max": 25, "status": "healthy"},
        "capacity": {"score": 20, "max": 20, "status": "healthy"},
        "restorePerformance": {"score": 15, "max": 15, "status": "healthy"},
        "authorityIntegrity": {"score": 15, "max": 15, "status": "healthy"},
    }

    r_coord0 = client.get("/api/workspace/resilience/coordination-plan", headers=headers)
    assert r_coord0.status_code == 200
    coord0 = r_coord0.json()
    assert coord0["coordinationPlanVersion"] == 1
    assert coord0["planDigest"] == resilience_coordinator.compute_coordination_plan_digest(
        coord0
    )

    # 2. Seed replication policy with deficit and check assess -> plan -> materialize -> snapshot -> coordination
    t1_dir = tmp_settings / "ext-t1"
    t2_dir = tmp_settings / "ext-t2"
    t1_dir.mkdir(parents=True, exist_ok=True)
    t2_dir.mkdir(parents=True, exist_ok=True)
    t1 = backup_targets.init_target(t1_dir, label="Target 1")
    t2 = backup_targets.init_target(t2_dir, label="Target 2")
    t1_id = str(t1["targetId"])
    t2_id = str(t2["targetId"])
    backup_control.upsert_target({**t1, "failureDomain": "fd-a"})
    backup_control.upsert_target({**t2, "failureDomain": "fd-b"})

    backup_policies.create_policy(
        {
            "policyId": "pol_rep",
            "name": "Replicated Policy",
            "targetId": t1_id,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "targetIds": [t1_id, t2_id],
            },
        }
    )
    backup_dr_ledger.record_recovery_point(
        target_id=t1_id,
        policy_id="pol_rep",
        backup_id="bk_1",
        committed_at="2026-11-18T01:00:00Z",
        snapshot_kind="full",
        chain_length=1,
        ciphertext_bytes=128,
        logical_bytes=128,
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1_id,
        policy_id="pol_rep",
        backup_id="bk_1",
        committed_at="2026-11-18T01:00:00Z",
        recoverable=True,
        state="healthy",
        role="primary",
    )

    r_assess = client.post(
        "/api/workspace/resilience/assess", headers=headers, json={"probe": False}
    )
    assert r_assess.status_code == 200
    assess = r_assess.json()
    assert assess["riskSnapshot"]["overallRisk"] == "critical"
    assert any(
        r.get("type") == "REPLICA_LAG" and r.get("severity") == "degraded"
        for r in assess["riskSnapshot"]["risks"]
    )

    r_prop = client.post(
        "/api/workspace/resilience/plan",
        headers=headers,
        json={"probe": False, "materialize": False},
    )
    assert r_prop.status_code == 200
    prop = r_prop.json()
    assert prop["status"] == "PROPOSED"
    assert prop["planDigest"] == resilience_planner.compute_plan_digest(prop)
    repair = next(a for a in prop["actions"] if a["type"] == "CREATE_REPAIR_JOB")
    assert repair["destination"] == t2_id
    assert repair["requiresApproval"] is False

    r_mat = client.post(
        "/api/workspace/resilience/plan", headers=headers, json={"probe": False}
    )
    assert r_mat.status_code == 200
    mat = r_mat.json()
    assert mat["status"] == "MATERIALIZED"
    assert any(
        a["type"] == "CREATE_REPAIR_JOB" and a["state"] == "PENDING"
        for a in mat["actions"]
    )
    assert len(resilience_action_journal.list_actions(limit=50)) == len(mat["actions"])

    r_snap1 = client.get("/api/workspace/resilience/snapshot", headers=headers)
    assert r_snap1.status_code == 200
    snap1 = r_snap1.json()
    assert snap1["criticalRisks"] >= 1
    assert snap1["plannedActions"] == len(mat["actions"])

    r_coord1 = client.post(
        "/api/workspace/resilience/coordination-plan",
        headers=headers,
        json={"probe": False},
    )
    assert r_coord1.status_code == 200
    coord1 = r_coord1.json()
    assert "restore-replica-durability" in coord1["objectives"]
    assert coord1["blastRadiusSimulation"]["passed"] is True
    assert coord1["planDigest"] == resilience_coordinator.compute_coordination_plan_digest(
        coord1
    )
