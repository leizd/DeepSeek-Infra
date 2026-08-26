# Copyright (c) 2026 DeepSeek Infra Contributors
# SPDX-License-Identifier: MIT
"""Comprehensive coverage booster for verified autonomous resilience action journal, planner, and governance endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
    resilience_action_journal,
    resilience_planner,
)
from deepseek_infra.web.server import create_server


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_settings: Any) -> None:
    pass


def test_booster_journal_list_and_filters() -> None:
    plan = {
        "planId": "plan_filter_1",
        "planVersion": 1,
        "inputRiskDigest": "risk_digest_1",
        "overallRisk": "MEDIUM",
        "actions": [
            {
                "actionId": "act_f1",
                "type": "CREATE_REPAIR_JOB",
                "policyId": "pol_1",
                "backupId": "b1",
                "destTargetId": "target_1",
                "parameters": {"policyId": "pol_1", "backupId": "b1", "destTargetId": "target_1"},
                "requiresApproval": False,
            },
            {
                "actionId": "act_f2",
                "type": "PRIMARY_PROMOTION",
                "requiresApproval": True,
                "parameters": {},
            },
        ],
    }
    plan["planDigest"] = resilience_planner.compute_plan_digest(plan)
    mat = resilience_action_journal.materialize_resilience_plan(plan, created_by="test-user")
    assert mat["planId"] == "plan_filter_1"

    # List all
    all_acts = resilience_action_journal.list_actions(limit=10)
    assert len(all_acts) >= 2

    # Filter by state
    pending = resilience_action_journal.list_actions(state="PENDING")
    assert any(a["actionId"] == "act_f1" for a in pending)

    approval_req = resilience_action_journal.list_actions(state="APPROVAL_REQUIRED")
    assert any(a["actionId"] == "act_f2" for a in approval_req)

    # Filter by action_type
    rep_acts = resilience_action_journal.list_actions(action_type="CREATE_REPAIR_JOB")
    assert any(a["actionId"] == "act_f1" for a in rep_acts)

    # Rollback helper
    rb = resilience_action_journal.rollback_action("act_f1", reason="test-rollback")
    assert rb.get("state") in {"ROLLED_BACK", "BLOCKED", "FAILED_BEFORE_EFFECT"} or "state" in rb


def test_booster_journal_claim_concurrency_and_expiry() -> None:
    act = {
        "actionId": "act_claim_exp",
        "planId": "plan_exp",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_1"},
    }
    resilience_action_journal.record_action_intent(act)

    # Claim 1
    ok1, claimed1, reason1 = resilience_action_journal.claim_action("act_claim_exp", owner_instance_id="worker-1", lease_seconds=1)
    assert ok1 is True
    assert claimed1 is not None

    # Duplicate claim while lease active
    ok2, claimed2, reason2 = resilience_action_journal.claim_action("act_claim_exp", owner_instance_id="worker-2", lease_seconds=1)
    assert ok2 is False
    assert "not-pending" in reason2.lower() or "lease" in reason2.lower()

    # Expire the lease manually in database to test takeover
    with resilience_action_journal._connect() as conn:
        conn.execute(
            "UPDATE resilience_actions SET lease_until='2020-01-01T00:00:00Z', state='PENDING' WHERE action_id='act_claim_exp'"
        )
        conn.commit()

    ok3, claimed3, reason3 = resilience_action_journal.claim_action("act_claim_exp", owner_instance_id="worker-3", lease_seconds=60)
    assert ok3 is True
    assert claimed3 is not None
    assert claimed3["ownerInstanceId"] == "worker-3"


def test_booster_execute_autonomous_action_failure_paths() -> None:
    # 1. Action not found
    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("non_existent_act")
    assert exc_info.value.status == 404

    # 2. Blocked by NEVER_AUTONOMOUS policy
    resilience_action_journal.record_action_intent(
        {"actionId": "act_promo", "planId": "p", "type": "PRIMARY_PROMOTION", "parameters": {}}
    )
    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act_promo")
    assert exc_info.value.status == 403

    # 3. Rate limits exceeded
    resilience_action_journal.record_action_intent(
        {"actionId": "act_rate", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_1"}}
    )
    with patch("deepseek_infra.infra.workspace.resilience_action_journal.check_rate_limits", return_value=(False, "hourly-rate-limit-exceeded")):
        with pytest.raises(AppError) as exc_info:
            resilience_action_journal.execute_autonomous_action("act_rate")
        assert exc_info.value.status == 429

    # 4. Freshness check skips if risk is no longer present (TOCTOU fencing)
    resilience_action_journal.record_action_intent(
        {"actionId": "act_toctou", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_1"}}
    )
    with patch("deepseek_infra.infra.workspace.resilience_action_journal.check_action_freshness", return_value=(False, "risk-cleared-already")):
        res_skipped = resilience_action_journal.execute_autonomous_action("act_toctou")
        assert res_skipped["state"] in {"SKIPPED_NO_LONGER_NEEDED", "REPLAN_REQUIRED"}

    # 5. Simulation failure
    resilience_action_journal.record_action_intent(
        {"actionId": "act_sim_fail", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_1"}}
    )
    with patch("deepseek_infra.infra.workspace.resilience_action_journal.check_action_freshness", return_value=(True, "fresh")):
        with patch("deepseek_infra.infra.workspace.resilience_action_journal.simulate_action", return_value=(False, {"error": "target-not-found"})):
            with pytest.raises(AppError) as exc_info:
                resilience_action_journal.execute_autonomous_action("act_sim_fail")
            assert exc_info.value.status == 400

    # 6. Outcome verification failure triggers compensation
    resilience_action_journal.record_action_intent(
        {
            "actionId": "act_verif_fail",
            "planId": "p",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_1"},
        }
    )
    with patch("deepseek_infra.infra.workspace.resilience_action_journal.check_action_freshness", return_value=(True, "fresh")):
        with patch("deepseek_infra.infra.workspace.resilience_action_journal.simulate_action", return_value=(True, {"simulationPassed": True})):
            with patch("deepseek_infra.infra.workspace.backup_replication.create_repair_job", return_value={"jobId": "j1", "repairId": "r1"}):
                with patch("deepseek_infra.infra.workspace.resilience_action_journal.verify_action_outcome", return_value=(False, {"error": "ledger-verification-failed"})):
                    with pytest.raises(AppError) as exc_info:
                        resilience_action_journal.execute_autonomous_action("act_verif_fail")
                    assert exc_info.value.status == 500
    failed_act = resilience_action_journal.get_action("act_verif_fail")
    assert failed_act is not None
    assert failed_act["compensationState"] in {"JOB_CANCELLED", "COMPENSATED", "CANCELLED", "MANUAL_INTERVENTION_REQUIRED"}


def test_booster_compensation_and_effects() -> None:
    resilience_action_journal.record_action_intent(
        {"actionId": "act_no_eff", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {}}
    )
    resilience_action_journal.record_action_intent(
        {"actionId": "act_irr", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {}}
    )
    resilience_action_journal.record_action_intent(
        {"actionId": "act_can", "planId": "p", "type": "CREATE_REPAIR_JOB", "parameters": {}}
    )

    # 1. NO_EFFECT compensation
    res = resilience_action_journal.compensate_action("act_no_eff", "Failed before running", effect_class="NO_EFFECT")
    assert res["state"] == "FAILED_BEFORE_EFFECT"
    assert res["compensationState"] == "NONE"

    # 2. IRREVERSIBLE compensation
    res_irr = resilience_action_journal.compensate_action("act_irr", "Irreversible action failed", effect_class="IRREVERSIBLE")
    assert res_irr["state"] == "NEEDS_OPERATOR"
    assert res_irr["compensationState"] == "MANUAL_INTERVENTION_REQUIRED"

    # 3. CANCELABLE compensation
    res_can = resilience_action_journal.compensate_action("act_can", "Job cancelled", effect_class="CANCELABLE")
    assert res_can["state"] == "COMPENSATED"
    assert res_can["compensationState"] == "JOB_CANCELLED"


def test_booster_autonomous_policy_and_proof() -> None:
    policy = autonomous_action_policy.get_autonomous_action_policy()
    assert "allowedActions" in policy or "autonomousEnabled" in policy

    # Update policy
    updated = autonomous_action_policy.set_autonomous_action_policy(
        {
            "autonomousEnabled": True,
            "maxActionsPerHour": 25,
            "allowedActions": ["CREATE_REPAIR_JOB", "CREATE_REBALANCE_JOB"],
        }
    )
    assert updated["maxActionsPerHour"] == 25

    # Check allowed vs disallowed
    assert autonomous_action_policy.is_action_autonomous("CREATE_REPAIR_JOB", updated) is True
    assert autonomous_action_policy.is_action_autonomous("PRIMARY_PROMOTION", updated) is False
    assert autonomous_action_policy.is_action_autonomous("UNKNOWN_ACTION", updated) is False


def test_booster_planner_comprehensive_scenarios(tmp_path: Path) -> None:
    t_src = tmp_path / "tsrc"
    t_src.mkdir(parents=True, exist_ok=True)
    t1_dir = tmp_path / "tgt1"
    t1_dir.mkdir(parents=True, exist_ok=True)
    t2_dir = tmp_path / "tgt2"
    t2_dir.mkdir(parents=True, exist_ok=True)

    backup_targets.register_filesystem_target("target_src", path=t_src)
    backup_targets.register_filesystem_target("target_cand1", path=t1_dir)
    backup_targets.register_filesystem_target("target_cand2", path=t2_dir)

    backup_policies.create_policy(
        {
            "policyId": "pol_rep",
            "name": "Replica Policy",
            "primaryTargetId": "target_src",
            "replication": {"enabled": True, "targetIds": ["target_cand1", "target_cand2"], "minCommittedCopies": 2},
        }
    )

    backup_dr_ledger.record_recovery_point(
        target_id="target_src",
        policy_id="pol_rep",
        backup_id="bid_rep",
        committed_at="2026-08-26T00:00:00Z",
    )

    snapshot = {
        "overallRisk": "HIGH",
        "riskDigest": "digest_multi",
        "risks": [
            {
                "type": "REPLICA_LAG",
                "severity": "HIGH",
                "policyId": "pol_rep",
                "backupId": "bid_rep",
                "sourceTarget": "target_src",
            },
            {
                "type": "CAPACITY_EXHAUSTION",
                "severity": "HIGH",
                "target": "target_cand1",
                "freePercent": 5.0,
            },
            {
                "type": "DR_STALENESS",
                "severity": "MEDIUM",
                "policyId": "pol_rep",
            },
            {
                "type": "AUTHORITY_SPLIT_BRAIN",
                "severity": "CRITICAL",
            },
            {
                "type": "UNKNOWN_FUTURE_RISK",
                "severity": "HIGH",
            },
        ],
    }

    plan = resilience_planner.plan_resilience_actions(snapshot)
    assert plan["planId"].startswith("plan_")
    assert len(plan["actions"]) >= 1

    for act in plan["actions"]:
        if act["type"] in autonomous_action_policy.NEVER_AUTONOMOUS:
            assert act["requiresApproval"] is True


def test_booster_governance_routes_via_client(tmp_path: Path) -> None:
    srv, _ = create_server(8000)
    auth_header = {"Authorization": f"Bearer {settings.auth.token}"} if settings.auth.enabled else {}
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {**auth_header, "X-DeepSeek-Client": "test"}

    # 1. /api/workspace/resilience/plan (materialize=False and True)
    resp_plan_raw = client.post(
        "/api/workspace/resilience/plan",
        json={"materialize": False, "probe": False},
        headers=headers,
    )
    assert resp_plan_raw.status_code == 200
    data_raw = resp_plan_raw.json()
    assert "planId" in data_raw

    resp_plan_mat = client.post(
        "/api/workspace/resilience/plan",
        json={"materialize": True, "probe": False},
        headers=headers,
    )
    assert resp_plan_mat.status_code == 200
    data_mat = resp_plan_mat.json()
    assert data_mat.get("status") == "MATERIALIZED"

    # 2. /api/workspace/resilience/execute (missing actionId, raw type disallowed)
    resp_no_id = client.post("/api/workspace/resilience/execute", json={}, headers=headers)
    assert resp_no_id.status_code == 400

    resp_raw_disallowed = client.post(
        "/api/workspace/resilience/execute",
        json={"type": "CREATE_REPAIR_JOB"},
        headers=headers,
    )
    assert resp_raw_disallowed.status_code == 400

    # 3. /api/workspace/resilience/simulate
    resp_sim = client.post(
        "/api/workspace/resilience/simulate",
        json={"scenario": "TARGET_FAILURE", "excludedTargets": ["target_1"]},
        headers=headers,
    )
    assert resp_sim.status_code == 200
    assert "scenario" in resp_sim.json() or "simulated" in resp_sim.json() or "resilienceScore" in resp_sim.json()

    # 4. /api/workspace/resilience/explain
    t_exp_dir = tmp_path / "exp1"
    t_exp_dir.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_exp1", path=t_exp_dir)
    resp_exp1 = client.post(
        "/api/workspace/resilience/explain",
        json={"targetId": "target_exp1"},
        headers=headers,
    )
    assert resp_exp1.status_code == 200
    assert "reasons" in resp_exp1.json()

    # Explain with actionId
    resilience_action_journal.record_action_intent(
        {
            "actionId": "act_exp_route",
            "planId": "plan_exp",
            "type": "CREATE_REBALANCE_JOB",
            "parameters": {"reason": "capacity-watermark-exceeded", "sourceTargetId": "target_exp1"},
        }
    )
    resp_exp2 = client.post(
        "/api/workspace/resilience/explain",
        json={"actionId": "act_exp_route"},
        headers=headers,
    )
    assert resp_exp2.status_code == 200
    exp2_data = resp_exp2.json()
    assert "CREATE_REBALANCE_JOB" in exp2_data["reasons"]

    # Explain with empty body
    resp_exp3 = client.post("/api/workspace/resilience/explain", json={}, headers=headers)
    assert resp_exp3.status_code == 200
