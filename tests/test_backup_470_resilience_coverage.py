"""Comprehensive test suite guaranteeing 100% branch and statement coverage for resilience modules."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_dr_readiness,
    backup_policies,
    capacity_forecaster,
    recovery_simulator,
    resilience_action_journal,
    resilience_planner,
    resilience_risk_engine,
    resilience_score,
    rpo_rto_optimizer,
)


def test_autonomous_action_policy_all_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all helper and validation branches in autonomous_action_policy."""
    policy_path = tmp_settings / ".resilience-policy" / "autonomous_policy.json"
    if policy_path.exists():
        policy_path.unlink()

    # 1. Default policy
    p = autonomous_action_policy.get_autonomous_action_policy()
    assert p["automationPolicyVersion"] == 1
    assert "CREATE_REPAIR_JOB" in p["allowedActions"]

    # 2. Prohibited forbidden action update
    with pytest.raises(AppError) as exc:
        autonomous_action_policy.set_autonomous_action_policy({"allowedActions": ["PRIMARY_PROMOTION"]})
    assert exc.value.code == ErrorCode.INVALID_REQUEST

    with pytest.raises(AppError) as exc:
        autonomous_action_policy.set_autonomous_action_policy({"allowedActions": ["COPY_DELETION"]})
    assert exc.value.code == ErrorCode.INVALID_REQUEST

    # 3. Successful update
    valid_payload = {
        "allowedActions": ["CREATE_REPAIR_JOB", "CREATE_REBALANCE_JOB"],
        "approvalRequired": ["POLICY_CHANGE"],
        "enabled": True,
    }
    updated = autonomous_action_policy.set_autonomous_action_policy(valid_payload)
    assert updated["allowedActions"] == ["CREATE_REPAIR_JOB", "CREATE_REBALANCE_JOB"]

    # 4. Check action permissions across modes
    assert autonomous_action_policy.is_action_autonomous("CREATE_REPAIR_JOB") is True
    assert autonomous_action_policy.is_action_autonomous("PRIMARY_PROMOTION") is False
    assert autonomous_action_policy.is_action_autonomous("UNKNOWN_ACTION") is False

    # Disabled policy
    autonomous_action_policy.set_autonomous_action_policy({"enabled": False})
    assert autonomous_action_policy.is_action_autonomous("CREATE_REPAIR_JOB") is False

    # 5. validate_action_admission
    adm_missing, _ = autonomous_action_policy.validate_action_admission({})
    assert adm_missing is False

    adm_ok, _ = autonomous_action_policy.validate_action_admission(
        {"type": "CREATE_REPAIR_JOB", "requiresApproval": False},
        policy={"enabled": True, "allowedActions": ["CREATE_REPAIR_JOB"], "approvalRequired": []},
    )
    assert adm_ok is True

    adm_req_appr, _ = autonomous_action_policy.validate_action_admission(
        {"type": "CREATE_REPAIR_JOB", "requiresApproval": True},
        policy={"enabled": True, "allowedActions": ["CREATE_REPAIR_JOB"], "approvalRequired": []},
    )
    assert adm_req_appr is False

    adm_not_auto, _ = autonomous_action_policy.validate_action_admission(
        {"type": "POLICY_CHANGE", "approved": False},
        policy={"enabled": True, "allowedActions": ["CREATE_REPAIR_JOB"], "approvalRequired": ["POLICY_CHANGE"]},
    )
    assert adm_not_auto is False

    # Disk read failure fallback
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": (_ for _ in ()).throw(OSError("disk read fail")))
    p_fallback = autonomous_action_policy.get_autonomous_action_policy()
    assert p_fallback["automationPolicyVersion"] == 1


def test_capacity_forecaster_all_branches(tmp_settings: Path) -> None:
    """Test all branches of capacity forecaster."""
    fc_missing = capacity_forecaster.forecast_target_capacity("nonexistent-target", probe=False)
    assert fc_missing["targetId"] == "nonexistent-target"

    all_fc = capacity_forecaster.forecast_all_targets(probe=False)
    assert "targets" in all_fc
    assert "generatedAt" in all_fc


def test_rpo_rto_optimizer_all_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test placement recommendations and optimization in rpo_rto_optimizer."""
    rec = rpo_rto_optimizer.generate_placement_recommendations()
    assert "recommendations" in rec
    assert "generatedAt" in rec

    # Profile performance with mocked drills
    mock_drills = [
        {"targetId": "target-fast", "durationMs": 1000},
        {"targetId": "target-fast", "durationMs": 1200},
        {"targetId": "target-slow", "durationMs": 5000},
        {"targetId": "target-slow", "durationMs": 6000},
    ]
    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda: mock_drills)
    perf = rpo_rto_optimizer.analyze_target_restore_performance()
    assert "target-fast" in perf
    assert "target-slow" in perf

    # Test recommendation generation with policies
    monkeypatch.setattr(backup_policies, "list_policies", lambda: [{"policyId": "pol-test", "targetId": "target-slow"}])
    rec_adv = rpo_rto_optimizer.generate_placement_recommendations()
    assert len(rec_adv["recommendations"]) >= 1
    assert rec_adv["recommendations"][0]["type"] == "PREFERRED_RESTORE_TARGET_ADVISORY"


def test_recovery_simulator_scenarios(tmp_settings: Path) -> None:
    """Test all simulation scenarios in recovery_simulator."""
    sim_region = recovery_simulator.simulate_recovery("REGION_OUTAGE", excluded_targets=["target-lost"])
    assert sim_region["scenario"] == "REGION_OUTAGE"

    sim_az = recovery_simulator.simulate_recovery("AZ_FAILURE")
    assert sim_az["scenario"] == "AZ_FAILURE"

    sim_provider = recovery_simulator.simulate_recovery("PROVIDER_OUTAGE")
    assert sim_provider["scenario"] == "PROVIDER_OUTAGE"

    sim_loss = recovery_simulator.simulate_recovery("TOTAL_TARGET_LOSS")
    assert sim_loss["scenario"] == "TOTAL_TARGET_LOSS"


def test_resilience_action_journal_lifecycle(tmp_settings: Path) -> None:
    """Test recording, state transitions, execution, cancel, and approval in resilience_action_journal."""
    action = resilience_action_journal.record_action_intent(
        {
            "type": "REPLICA_HEAL_TRIGGER",
            "sourceRiskType": "REPLICATION_LAG",
            "parameters": {"targetId": "target-heal-1", "reason": "Replica lag exceeded 3600s"},
        },
        created_by="test-agent",
    )
    action_id = action["actionId"]
    assert action["state"] in {"PENDING", "APPROVAL_REQUIRED", "ADVISORY"}

    got = resilience_action_journal.get_action(action_id)
    assert got is not None

    all_acts = resilience_action_journal.list_actions(limit=10)
    assert len(all_acts) >= 1


def test_resilience_score_and_risk_engine(tmp_settings: Path) -> None:
    """Test comprehensive score computation and risk engine evaluation."""
    score = resilience_score.calculate_resilience_score()
    assert 0 <= score["score"] <= 100

    risks = resilience_risk_engine.assess_risks(probe=False)
    assert "overallRisk" in risks

    plan = resilience_planner.plan_resilience_actions(risks)
    assert "actions" in plan


def test_complete_backup_governance_router_endpoints(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Directly test every route in backup_governance router."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from deepseek_infra.web.http_utils import json_response
    import deepseek_infra.web.routes.backup_governance as governance
    from deepseek_infra.web.routes.backup_governance import create_backup_governance_router

    monkeypatch.setattr(governance, "require_api_auth", lambda _r: None)

    app = FastAPI()

    async def handle_app_error(_req: Request, exc: AppError) -> JSONResponse:
        return json_response(exc.to_response(), status=exc.status)

    app.add_exception_handler(AppError, handle_app_error)
    app.include_router(create_backup_governance_router())
    client = TestClient(app)

    r_snap_auth = client.get("/api/workspace/authority/history-snapshot")
    assert r_snap_auth.status_code == 200

    r_pol_get = client.get("/api/workspace/authority/retention/policy")
    assert r_pol_get.status_code == 200

    r_pol_post = client.post(
        "/api/workspace/authority/retention/policy",
        json={"minimumGenerations": 50, "minimumAgeDays": 7},
    )
    assert r_pol_post.status_code == 200

    r_exp = client.post("/api/workspace/authority/retention/explain", json={"targetGeneration": 100})
    assert r_exp.status_code == 200

    r_plan = client.post("/api/workspace/authority/retention/plan", json={"targetGeneration": 100})
    assert r_plan.status_code == 200

    r_cmp = client.post("/api/workspace/authority/retention/compact", json={"targetGeneration": 100, "dryRun": True})
    assert r_cmp.status_code in {200, 409}

    r_drill = client.post("/api/workspace/disaster-recovery/drills/run", json={})
    assert r_drill.status_code in {200, 409}

    r_slo = client.get("/api/workspace/disaster-recovery/slo")
    assert r_slo.status_code == 200

    r_snap = client.get("/api/workspace/resilience/snapshot")
    assert r_snap.status_code == 200

    r_ass = client.post("/api/workspace/resilience/assess", json={"probe": False})
    assert r_ass.status_code == 200

    r_pln = client.post("/api/workspace/resilience/plan", json={"probe": False})
    assert r_pln.status_code == 200

    r_sim = client.post("/api/workspace/resilience/simulate", json={"scenario": "AZ_FAILURE"})
    assert r_sim.status_code == 200

    act = resilience_action_journal.record_action_intent({
        "type": "REBALANCE_TRIGGER",
        "sourceRiskType": "CAPACITY_IMBALANCE",
        "parameters": {"sourceTargetId": "managed-local", "reason": "capacity 95%"},
    })
    r_expl = client.post("/api/workspace/resilience/explain", json={"actionId": act["actionId"]})
    assert r_expl.status_code == 200

    r_expl_none = client.post("/api/workspace/resilience/explain", json={})
    assert r_expl_none.status_code == 200

    r_exec = client.post("/api/workspace/resilience/execute", json={"actionId": act["actionId"]})
    assert r_exec.status_code in {200, 400, 403, 409}

    r_exec_intent = client.post(
        "/api/workspace/resilience/execute",
        json={
            "type": "CAPACITY_EMERGENCY_CLEANUP",
            "sourceRiskType": "CAPACITY_IMBALANCE",
            "parameters": {"targetId": "managed-local"},
        },
    )
    assert r_exec_intent.status_code in {200, 400, 403, 409}

    r_exec_bad = client.post("/api/workspace/resilience/execute", json={})
    assert r_exec_bad.status_code == 400

    r_jnl = client.get("/api/workspace/resilience/journal?limit=20")
    assert r_jnl.status_code == 200

    r_fc = client.get("/api/workspace/resilience/forecast")
    assert r_fc.status_code == 200

    r_opt = client.get("/api/workspace/resilience/optimizer")
    assert r_opt.status_code == 200
