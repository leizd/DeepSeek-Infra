"""current-release Gate H, I, J, K - Coordinated Multi-Risk Resilience Test Suite.

Validates:
1. Multi-Risk Coordination Graph (ResilienceCoordinationPlan v1).
2. DAG Dependencies (e.g. Repair before Rebalance).
3. Conflict Serialization & Resource Locks.
4. Authority Circuit-Breaker Integration.
5. Atomic Safety Budgets & Preemption of Warning Actions by Critical Actions.
6. Blast-Radius Safety Invariants (minCommittedCopies & minFailureDomains).
7. Governance route /api/workspace/resilience/coordination-plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_targets,
    resilience_action_journal,
    resilience_coordinator,
)
from deepseek_infra.infra.workspace.resilience_risk_engine import RiskSeverity, RiskType


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_coordination_plan_generation_and_dag_dependencies(tmp_settings: Path) -> None:
    """Test generating a ResilienceCoordinationPlan v1 with repair -> rebalance DAG dependency."""
    t1 = tmp_settings / "target_1"
    t2 = tmp_settings / "target_2"
    t3 = tmp_settings / "target_3"
    for t in (t1, t2, t3):
        t.mkdir(parents=True, exist_ok=True)

    backup_targets.register_filesystem_target("target_1", path=t1, failure_domain="zone-a")
    backup_targets.register_filesystem_target("target_2", path=t2, failure_domain="zone-b")
    backup_targets.register_filesystem_target("target_3", path=t3, failure_domain="zone-c")

    policy_id = "pol_coord_1"
    backup_policies.create_policy({
        "name": "Coord Policy",
        "policyId": policy_id,
        "targetId": "target_1",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "destTargets": ["target_2", "target_3"],
            "minFailureDomains": 2,
        },
    })

    backup_dr_ledger.record_recovery_point(
        policy_id=policy_id,
        backup_id="bkp_coord_1",
        target_id="target_1",
        chain_digest="chain_1",
        committed_at=_utc_iso(),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id=policy_id,
        backup_id="bkp_coord_1",
        target_id="target_1",
        state="committed",
        committed_at=_utc_iso(),
    )

    # 1. Synthesize multi-risk state (Replica lag on pol_coord_1 + capacity risk on target_1)
    risk_snapshot = {
        "riskDigest": "rd_synth_multi",
        "overallRisk": "warning",
        "risks": [
            {
                "type": RiskType.REPLICA_LAG.value,
                "policyId": policy_id,
                "backupId": "bkp_coord_1",
                "severity": RiskSeverity.WARNING.value,
                "evidence": ["only-1-replica"],
            },
            {
                "type": RiskType.CAPACITY_EXHAUSTION.value,
                "target": "target_1",
                "policyId": policy_id,
                "severity": RiskSeverity.WARNING.value,
                "evidence": ["target_1-capacity-high"],
            },
        ],
    }

    # 2. Generate Coordinated Resilience Plan
    coord_plan = resilience_coordinator.plan_coordinated_resilience(risk_snapshot)

    assert coord_plan["coordinationPlanVersion"] == 1
    assert coord_plan["coordinationPlanId"].startswith("coord_")
    assert coord_plan["planDigest"] != ""
    assert "restore-replica-durability" in coord_plan["objectives"]
    assert "relieve-capacity-pressure" in coord_plan["objectives"]

    actions = coord_plan["actions"]
    assert len(actions) == 2
    repair_act = next(a for a in actions if a["type"] == "CREATE_REPAIR_JOB")
    reb_act = next(a for a in actions if a["type"] == "CREATE_REBALANCE_JOB")

    # Dependency assertion: repair must precede rebalance
    assert [repair_act["actionId"], reb_act["actionId"]] in coord_plan["dependencies"]
    # Conflict assertion
    assert [repair_act["actionId"], reb_act["actionId"]] in coord_plan["conflicts"]

    # Blast-radius verification flag on actions
    assert repair_act["blastRadiusVerified"] is True
    assert reb_act["blastRadiusVerified"] is True


def test_authority_circuit_breaker_blocks_coordination(tmp_settings: Path) -> None:
    """Test that critical/blocked Authority Degradation engages circuit breaker on coordination plan."""
    risk_snapshot = {
        "riskDigest": "rd_auth_block",
        "overallRisk": "critical",
        "risks": [
            {
                "type": RiskType.AUTHORITY_DEGRADATION.value,
                "severity": RiskSeverity.CRITICAL.value,
                "evidence": ["control-authority-corrupted"],
            },
            {
                "type": RiskType.REPLICA_LAG.value,
                "policyId": "pol_auth_1",
                "backupId": "b1",
                "severity": RiskSeverity.CRITICAL.value,
                "evidence": ["missing-replicas"],
            },
        ],
    }

    coord_plan = resilience_coordinator.plan_coordinated_resilience(risk_snapshot)
    assert "authority-circuit-breaker-engaged" in coord_plan["objectives"]
    for act in coord_plan["actions"]:
        assert act["requiresApproval"] is True
        assert "blocked-by-authority-risk" in act["reason"]


def test_atomic_safety_budget_and_priority_preemption(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test atomic rate limits & safety budget preemption of warning rebalance by critical repair."""
    t1 = tmp_settings / "target_1"
    t2 = tmp_settings / "target_2"
    t1.mkdir(parents=True, exist_ok=True)
    t2.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_1", path=t1)
    backup_targets.register_filesystem_target("target_2", path=t2)

    # 1. Fill active concurrency with a pending/claimed warning rebalance
    resilience_action_journal.record_action_intent({
        "actionId": "act_reb_active",
        "type": "CREATE_REBALANCE_JOB",
        "severity": "warning",
        "parameters": {"sourceTargetId": "target_1", "destTargetId": "target_2", "policyId": "p1", "backupId": "b1"},
    })

    from deepseek_infra.infra.workspace import autonomous_action_policy
    monkeypatch.setattr(autonomous_action_policy, "get_action_rate_limits", lambda: {"maxConcurrentActions": 1, "maxActionsPerHour": 10})

    # Claim rebalance
    ok, _, _ = resilience_action_journal.claim_action("act_reb_active")
    assert ok is True

    # 2. Check rate limit for a WARNING action -> blocked
    with resilience_action_journal._connect() as conn:
        warning_act = {"type": "START_DR_DRILL", "severity": "warning", "parameters": {}}
        allowed, reason = resilience_action_journal.check_rate_limits(conn, warning_act)
        assert allowed is False
        assert "max-concurrent-actions-exceeded" in reason

    # 3. Check rate limit for a CRITICAL repair action -> preempts active warning rebalance!
    with resilience_action_journal._connect() as conn:
        critical_repair = {"type": "CREATE_REPAIR_JOB", "severity": "critical", "parameters": {"policyId": "p1", "backupId": "b1", "destTargetId": "target_2"}}
        allowed, reason = resilience_action_journal.check_rate_limits(conn, critical_repair)
        assert allowed is True
        assert "admitted-with-preemption" in reason

    # Verify the warning rebalance was transitioned to PREEMPTED
    reb_after = resilience_action_journal.get_action("act_reb_active")
    assert reb_after is not None
    assert reb_after["state"] == "PREEMPTED"
