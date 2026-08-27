"""Comprehensive coverage booster for current-release Coordinated Resilience modules."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_targets,
    resilience_action_journal,
    resilience_coordinator,
    resilience_outcome_verifier,
    resilience_planner,
    resilience_resource_locks,
)
from deepseek_infra.web.server import create_server


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_coordinator_blast_radius_and_conflicts(tmp_settings: Path) -> None:
    """Test coordinator DAG dependencies, blast radius limits and conflict edges."""
    # Create actions with conflicting lock targets and blast radius constraints
    snapshot = {
        "riskSnapshotVersion": 1,
        "overallRisk": "degraded",
        "riskDigest": "d" * 64,
        "risks": [
            {
                "type": "REPLICA_LAG",
                "policyId": "pol_1",
                "severity": "degraded",
                "confidence": "verified",
                "evidence": ["replica-lag"],
            },
            {
                "type": "CAPACITY_EXHAUSTION",
                "target": "target_a",
                "severity": "warning",
                "confidence": "verified",
                "evidence": ["free-space:15%"],
            },
        ],
    }

    coord = resilience_coordinator.plan_coordinated_resilience(snapshot)
    assert coord["coordinationPlanVersion"] == 1
    assert "actions" in coord
    assert "dependencies" in coord

    # Test degraded authority stops coordination
    coord_deg = resilience_coordinator.plan_coordinated_resilience(
        {"overallRisk": "blocked", "risks": [{"type": "AUTHORITY_DEGRADATION", "severity": "blocked"}]},
    )
    assert "authority-circuit-breaker-engaged" in coord_deg["objectives"]
    assert all(a.get("requiresApproval") for a in coord_deg.get("actions", []))


def test_outcome_verifier_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all error and boundary branches in resilience_outcome_verifier."""
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)

    # 1. _utc_iso with explicit dt
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert "2026-01-01" in resilience_outcome_verifier._utc_iso(dt)

    # 2. _resolve_target_safely with empty string / unknown target
    assert resilience_outcome_verifier._resolve_target_safely("") is None
    resolved = resilience_outcome_verifier._resolve_target_safely("target_nonexistent_xyz")
    assert resolved is not None
    assert resolved.target_id == "target_nonexistent_xyz"

    # 3. verify_action_outcome with unsupported type
    ok_unk, res_unk = resilience_outcome_verifier.verify_action_outcome(
        {"type": "UNKNOWN_ACTION_TYPE"},
        {},
    )
    assert ok_unk is False
    assert "unsupported-verification-type" in res_unk["error"]

    # 4. verify_action_outcome repair execution exception
    def failing_exec_repair(*a, **k):
        raise RuntimeError("network failure during repair execution")

    monkeypatch.setattr(backup_replication, "read_repair_job", lambda rid: {"repairId": rid, "phase": "pending"})
    monkeypatch.setattr(backup_replication, "execute_replica_repair", failing_exec_repair)

    ok_rep_exc, res_rep_exc = resilience_outcome_verifier.verify_action_outcome(
        {"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "t"}},
        {"job": {"repairId": "r1", "phase": "pending"}},
    )
    assert ok_rep_exc is False
    assert "repair-execution-failed" in res_rep_exc["error"]

    # 5. verify_action_outcome repair destination auth exception
    monkeypatch.setattr(backup_replication, "read_repair_job", lambda rid: {"repairId": rid, "phase": "complete"})
    monkeypatch.setattr(backup_dr_ledger, "list_logical_recovery_copies", lambda **kw: [{"status": "committed", "targetId": "t1"}])
    monkeypatch.setattr(backup_policies, "get_policy", lambda pid: {"replication": {"minCommittedCopies": 1, "minFailureDomains": 1}})

    def failing_auth(*a, **k):
        raise ValueError("corrupt header")
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", failing_auth)

    ok_auth_exc, res_auth_exc = resilience_outcome_verifier.verify_action_outcome(
        {"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "t1"}},
        {"job": {"repairId": "r1", "phase": "complete"}},
    )
    assert ok_auth_exc is False
    assert "destination-target-auth-error" in res_auth_exc["error"]

    # 6. verify_action_outcome repair failure domain unsatisfied
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("authenticated", {"r": 1}, {"c": 1}))
    monkeypatch.setattr(backup_policies, "get_policy", lambda pid: {"replication": {"minCommittedCopies": 1, "minFailureDomains": 3}})
    monkeypatch.setattr(backup_targets, "list_targets", lambda: [{"targetId": "t1", "failureDomain": "fd-1"}])

    ok_fd_bad, res_fd_bad = resilience_outcome_verifier.verify_action_outcome(
        {"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "t1"}},
        {"job": {"repairId": "r1", "phase": "complete"}},
    )
    assert ok_fd_bad is False
    assert "failure-domain-objective-unsatisfied" in res_fd_bad["error"]

    # 7. verify_action_outcome rebalance execution exception & destination auth exception
    def failing_exec_reb(jid):
        raise RuntimeError("rebalance socket closed")
    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda jid: {"jobId": jid, "phase": "transferring"})
    monkeypatch.setattr(backup_replication, "execute_rebalance_job", failing_exec_reb)

    ok_reb_exc, res_reb_exc = resilience_outcome_verifier.verify_action_outcome(
        {"type": "CREATE_REBALANCE_JOB", "parameters": {"sourceTargetId": "t1", "destTargetId": "t2", "policyId": "p", "backupId": "b"}},
        {"job": {"jobId": "j1", "phase": "transferring"}},
    )
    assert ok_reb_exc is False
    assert "rebalance-execution-exception" in res_reb_exc["error"]

    # 8. verify_action_outcome DR drill proof commit unverified
    ok_drill_bad, res_drill_bad = resilience_outcome_verifier.verify_action_outcome(
        {"type": "START_DR_DRILL", "parameters": {}},
        {"success": True, "proof": {"commitVerified": False}},
    )
    assert ok_drill_bad is False
    assert "dr-drill-proof-commit-unverified" in res_drill_bad["error"]

    # 9. verify_scoped_risk_reduction legacy action synthesize riskSubject & worsening risk
    act_legacy_repair = {"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "pol_x", "backupId": "bkp_x", "destTargetId": "tgt_x"}}
    snap_b = {"risks": [{"type": "REPLICA_LAG", "policyId": "pol_x", "severity": "warning"}]}
    snap_worse = {"risks": [{"type": "REPLICA_LAG", "policyId": "pol_x", "severity": "critical"}]}
    ok_worse, res_worse = resilience_outcome_verifier.verify_scoped_risk_reduction(act_legacy_repair, snap_b, snap_worse)
    assert ok_worse is False
    assert "target-risk-not-improved" in res_worse["reason"]

    act_legacy_drill = {"type": "START_DR_DRILL", "parameters": {"policyId": "pol_y"}}
    snap_dr_b = {"risks": [{"type": "DR_STALENESS", "policyId": "pol_y", "severity": "warning"}]}
    snap_dr_a = {"risks": [{"type": "DR_STALENESS", "policyId": "pol_y", "severity": "healthy"}]}
    ok_dr, res_dr = resilience_outcome_verifier.verify_scoped_risk_reduction(act_legacy_drill, snap_dr_b, snap_dr_a)
    assert ok_dr is True


def test_resource_locks_and_journal_limits(tmp_settings: Path) -> None:
    """Test resilience_resource_locks list/clear and journal rate limits."""
    with resilience_action_journal._connect() as conn:
        ok, _ = resilience_resource_locks.acquire_action_locks(
            conn,
            action_id="act_test_locks",
            lock_keys=["backup:p:b", "target:t1"],
            lease_until="2099-01-01T00:00:00Z",
        )
        assert ok is True
        active = resilience_resource_locks.list_active_locks(conn)
        assert len(active) >= 2

        resilience_resource_locks.release_action_locks(conn, "act_test_locks")
        active_after = resilience_resource_locks.list_active_locks(conn)
        assert len(active_after) == 0

    # 2. Journal list filters
    p = {
        "planId": "plan_journal_list",
        "planVersion": 1,
        "inputRiskDigest": "d" * 64,
        "actions": [{"actionId": "act_jl_1", "type": "START_DR_DRILL", "parameters": {}}],
    }
    p["planDigest"] = resilience_planner.compute_plan_digest(p)
    resilience_action_journal.materialize_resilience_plan(p)

    actions = resilience_action_journal.list_actions(limit=5)
    assert any(a["actionId"] == "act_jl_1" for a in actions)

    # 3. Simulate action with unknown type & missing policy
    sim_bad_type, _ = resilience_action_journal.simulate_action({"type": "UNSUPPORTED_TYPE", "parameters": {}})
    assert sim_bad_type is False

    sim_bad_rep, _ = resilience_action_journal.simulate_action({
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_nonexistent_123", "backupId": "b1", "destTargetId": "t1"},
    })
    assert sim_bad_rep is False


def test_backup_governance_resilience_api_endpoints(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test HTTP endpoints in backup_governance for resilience coordination and execution."""
    srv, _ = create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    # 1. GET /api/workspace/resilience/snapshot and POST /api/workspace/resilience/assess
    r1 = client.get("/api/workspace/resilience/snapshot", headers=headers)
    assert r1.status_code == 200
    assert "grade" in r1.json()

    r2 = client.post("/api/workspace/resilience/assess", json={"probe": False}, headers=headers)
    assert r2.status_code == 200
    assert "riskSnapshot" in r2.json()

    # 2. POST /api/workspace/resilience/plan (materialize=True and materialize=False)
    p1 = client.post("/api/workspace/resilience/plan", json={"materialize": False}, headers=headers)
    assert p1.status_code == 200

    p2 = client.post("/api/workspace/resilience/plan", json={"materialize": True}, headers=headers)
    assert p2.status_code == 200

    # 3. GET & POST /api/workspace/resilience/coordination-plan
    cp_get = client.get("/api/workspace/resilience/coordination-plan", headers=headers)
    assert cp_get.status_code == 200
    assert "coordinationPlanVersion" in cp_get.json()

    cp = client.post("/api/workspace/resilience/coordination-plan", json={"probe": False}, headers=headers)
    assert cp.status_code == 200
    assert "coordinationPlanVersion" in cp.json()

    # 4. POST /api/workspace/resilience/execute (error branches & success execution)
    e_no_id = client.post("/api/workspace/resilience/execute", json={}, headers=headers)
    assert e_no_id.status_code == 400

    e_anon = client.post("/api/workspace/resilience/execute", json={"type": "START_DR_DRILL"}, headers=headers)
    assert e_anon.status_code == 400

    e_not_found = client.post("/api/workspace/resilience/execute", json={"actionId": "act_nonexistent_999"}, headers=headers)
    assert e_not_found.status_code == 404

    # Execute a real materialized action intent
    act_real = resilience_action_journal.record_action_intent({
        "actionId": "act_http_exec",
        "type": "START_DR_DRILL",
        "parameters": {"policyId": "pol_http"},
    })
    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", lambda aid: {"success": True, "actionId": aid})
    e_ok = client.post("/api/workspace/resilience/execute", json={"actionId": act_real["actionId"]}, headers=headers)
    assert e_ok.status_code == 200

    # 5. POST /api/workspace/resilience/simulate and /api/workspace/resilience/explain
    sim = client.post("/api/workspace/resilience/simulate", json={"scenario": "AZ_FAILURE"}, headers=headers)
    assert sim.status_code == 200

    from deepseek_infra.infra.workspace import backup_capacity
    monkeypatch.setattr(backup_capacity, "get_target_capacity", lambda tid, **kw: {"freePercent": 15.0, "freeBytes": 1000})
    monkeypatch.setattr(backup_capacity, "estimate_target_exhaustion_horizon", lambda tid, **kw: {"estimatedDaysToFull": 10})
    monkeypatch.setattr(resilience_planner, "select_rebalance_destination", lambda tid: "target_b")

    exp = client.post("/api/workspace/resilience/explain", json={"targetId": "target_a", "actionId": act_real["actionId"]}, headers=headers)
    assert exp.status_code == 200
    assert len(exp.json()["reasons"]) >= 2

    # 6. GET /api/workspace/resilience/journal, forecast, optimizer
    jnl = client.get("/api/workspace/resilience/journal?state=PENDING&limit=10", headers=headers)
    assert jnl.status_code == 200

    fc = client.get("/api/workspace/resilience/forecast", headers=headers)
    assert fc.status_code == 200

    opt = client.get("/api/workspace/resilience/optimizer", headers=headers)
    assert opt.status_code == 200


def test_coordinator_and_planner_exhaustive_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test exhaustive branches in coordinator and planner."""
    # 1. Coordinate plan with DR drill and rebalance on the same backup
    snap = {
        "riskSnapshotVersion": 1,
        "overallRisk": "degraded",
        "riskDigest": "e" * 64,
        "risks": [
            {"type": "DR_STALENESS", "policyId": "pol_shared", "severity": "warning"},
            {"type": "CAPACITY_EXHAUSTION", "target": "target_src", "severity": "warning"},
        ],
    }

    # Mock planner actions to simulate drill & rebalance on same backup
    mock_plan = {
        "planId": "p_mock",
        "actions": [
            {"actionId": "act_rep", "type": "CREATE_REPAIR_JOB", "policyId": "pol_shared", "backupId": "bkp_1", "source": "target_src", "destination": "target_dst"},
            {"actionId": "act_reb", "type": "CREATE_REBALANCE_JOB", "policyId": "pol_shared", "backupId": "bkp_1", "source": "target_src", "destination": "target_dst2"},
            {"actionId": "act_dr", "type": "START_DR_DRILL", "policyId": "pol_shared", "backupId": "bkp_1"},
        ],
    }
    monkeypatch.setattr(resilience_planner, "plan_resilience_actions", lambda *a, **k: mock_plan)

    coord = resilience_coordinator.plan_coordinated_resilience(snap)
    assert len(coord["dependencies"]) >= 2
    assert len(coord["conflicts"]) >= 2
    assert coord["planDigest"] is not None

    # 2. Planner find_rebalance_candidate_copy non-committed vs committed
    monkeypatch.setattr(backup_policies, "list_policies", lambda: [{"policyId": "pol_x"}])
    monkeypatch.setattr(backup_dr_ledger, "latest_recovery_point", lambda policy_id: {"backupId": "b_comm"})
    monkeypatch.setattr(backup_dr_ledger, "list_logical_recovery_copies", lambda **kw: [{"targetId": "target_src", "status": "staged"}])
    assert resilience_planner.find_rebalance_candidate_copy("target_src") == (None, None)

    monkeypatch.setattr(backup_dr_ledger, "list_logical_recovery_copies", lambda **kw: [{"targetId": "target_src", "status": "committed"}])
    cand = resilience_planner.find_rebalance_candidate_copy("target_src")
    assert cand == ("pol_x", "b_comm")


def test_journal_rate_limits_preemption_and_hourly(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test rate limit checks with preemption and hourly ceiling."""
    with resilience_action_journal._connect() as conn:
        # Preemption: Insert a warning rebalance job in CLAIMED state
        act_reb = resilience_action_journal.record_action_intent({
            "actionId": "act_reb_preempt_target",
            "type": "CREATE_REBALANCE_JOB",
            "severityBefore": "warning",
            "parameters": {"sourceTargetId": "target_a", "destTargetId": "target_b"},
        })
        conn.execute(
            "UPDATE resilience_actions SET state = 'CLAIMED', lease_until = '2099-01-01T00:00:00Z' WHERE action_id = ?",
            (act_reb["actionId"],),
        )
        conn.commit()

        # Check rate limits for critical repair when maxConcurrentActions is simulated as 1
        monkeypatch.setattr(
            "deepseek_infra.infra.workspace.autonomous_action_policy.get_action_rate_limits",
            lambda: {"maxConcurrentActions": 1, "maxActionsPerHour": 50},
        )
        act_crit = {"type": "CREATE_REPAIR_JOB", "severityBefore": "critical", "parameters": {}}
        admitted, reason = resilience_action_journal.check_rate_limits(conn, act_crit)
        assert admitted is True
        assert reason == "admitted-with-preemption"

        # Exceeded hourly limit
        monkeypatch.setattr(
            "deepseek_infra.infra.workspace.autonomous_action_policy.get_action_rate_limits",
            lambda: {"maxConcurrentActions": 10, "maxActionsPerHour": 0},
        )
        adm_hr, r_hr = resilience_action_journal.check_rate_limits(conn, act_crit)
        assert adm_hr is False
        assert "max-actions-per-hour-exceeded" in r_hr


def test_journal_freshness_and_simulation_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test freshness evaluation and simulation error branches."""
    # 1. Freshness: destination target draining
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"targetId": tid, "status": "draining"})
    act_reb = {"type": "CREATE_REBALANCE_JOB", "parameters": {"sourceTargetId": "target_src", "destTargetId": "target_dst"}}
    snap_fresh = {"risks": [{"type": "CAPACITY_EXHAUSTION", "target": "target_src", "severity": "warning"}]}
    fresh_ok, fresh_reason = resilience_action_journal.check_action_freshness(act_reb, snap_fresh)
    assert fresh_ok is False
    assert fresh_reason == "destination-target-draining-or-unavailable"

    # 2. Simulation: recovery point not found for repair
    monkeypatch.setattr(backup_policies, "get_policy", lambda pid: {"policyId": pid})
    monkeypatch.setattr(backup_dr_ledger, "get_recovery_point", lambda pid, bid: None)
    sim_ok, sim_res = resilience_action_journal.simulate_action({
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_1", "backupId": "bkp_1", "destTargetId": "target_b"},
    })
    assert sim_ok is False
    assert "recovery-point-not-found" in sim_res["error"]

    # 3. Simulation: draining destination target for repair
    monkeypatch.setattr(backup_dr_ledger, "get_recovery_point", lambda pid, bid: {"backupId": bid})
    sim_drain_ok, sim_drain_res = resilience_action_journal.simulate_action({
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_1", "backupId": "bkp_1", "destTargetId": "target_dst"},
    })
    assert sim_drain_ok is False
    assert "repair-destination-target-invalid" in sim_drain_res["error"]


def test_compensation_typed_states(tmp_settings: Path) -> None:
    """Test compensate_action transitions across all typed effect classes."""
    act = resilience_action_journal.record_action_intent({
        "actionId": "act_comp_states",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {},
    })

    # CANCELABLE
    c_canc = resilience_action_journal.compensate_action(act["actionId"], "canceled", effect_class="CANCELABLE")
    assert c_canc["state"] == "COMPENSATED"
    assert c_canc["compensationState"] == "JOB_CANCELLED"

    # COMPENSATABLE
    c_comp = resilience_action_journal.compensate_action(act["actionId"], "compensated", effect_class="COMPENSATABLE")
    assert c_comp["state"] == "COMPENSATED"
    assert c_comp["compensationState"] == "EFFECT_COMPENSATED"

    # IRREVERSIBLE
    c_irr = resilience_action_journal.compensate_action(act["actionId"], "irreversible", effect_class="IRREVERSIBLE")
    assert c_irr["state"] == "NEEDS_OPERATOR"
    assert c_irr["compensationState"] == "MANUAL_INTERVENTION_REQUIRED"

    # EFFECT_UNKNOWN
    c_unk = resilience_action_journal.compensate_action(act["actionId"], "unknown", effect_class="EFFECT_UNKNOWN")
    assert c_unk["state"] == "EFFECT_UNKNOWN"
    assert c_unk["compensationState"] == "REMOTE_EFFECT_UNCERTAIN"


def test_reconciler_and_outcome_verifier_deep_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test reconciler and outcome verifier branches for 100% booster coverage."""
    from deepseek_infra.infra.workspace import resilience_effect_reconciler

    # 1. Reconciler: CREATE_REPAIR_JOB in running phase
    act_rep = {"actionId": "act_rep_run", "type": "CREATE_REPAIR_JOB", "state": "EXECUTING", "effectHandle": {"repairId": "rep_1"}}
    monkeypatch.setattr(backup_replication, "read_repair_job", lambda rid: {"phase": "running", "repairId": rid})
    dec, ctx = resilience_effect_reconciler.reconcile_action_effect(act_rep)
    assert dec == "RESUME_EXECUTION"

    # 2. Reconciler: CREATE_REBALANCE_JOB in running phase
    act_reb = {"actionId": "act_reb_run", "type": "CREATE_REBALANCE_JOB", "state": "EXECUTING", "effectHandle": {"jobId": "reb_1"}}
    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda jid: {"phase": "running", "jobId": jid})
    dec_rb, ctx_rb = resilience_effect_reconciler.reconcile_action_effect(act_reb)
    assert dec_rb == "RESUME_EXECUTION"

    # 3. Verifier: _resolve_target_safely fallback to target_dict with path
    monkeypatch.setattr(backup_publish, "resolve_target", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"targetId": tid, "path": "/tmp/custom_target", "kind": "filesystem"})
    res_t = resilience_outcome_verifier._resolve_target_safely("target_custom")
    assert res_t.target_id == "target_custom"
    assert res_t.root == Path("/tmp/custom_target")

    # 4. Verifier: Rebalance missing jobId and destination auth exception
    v_no_job, v_no_job_res = resilience_outcome_verifier.verify_action_outcome(
        {"actionId": "act_rb_noj", "type": "CREATE_REBALANCE_JOB", "parameters": {}},
        {},
    )
    assert v_no_job is False
    assert v_no_job_res["error"] == "rebalance-job-id-missing"

    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda jid: {"phase": "complete"})
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("auth-fail")))
    v_exc, v_exc_res = resilience_outcome_verifier.verify_action_outcome(
        {"actionId": "act_rb_exc", "type": "CREATE_REBALANCE_JOB", "parameters": {"policyId": "p", "backupId": "b", "destTargetId": "target_custom"}},
        {"jobId": "reb_1"},
    )
    assert v_exc is False
    assert "rebalance-destination-auth-error" in v_exc_res["error"]



