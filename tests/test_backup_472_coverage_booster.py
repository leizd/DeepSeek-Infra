"""Comprehensive coverage booster for current-release Coordinated Resilience modules."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_replication,
    backup_targets,
    resilience_action_journal,
    resilience_coordinator,
    resilience_outcome_verifier,
    resilience_planner,
    resilience_resource_locks,
)


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
