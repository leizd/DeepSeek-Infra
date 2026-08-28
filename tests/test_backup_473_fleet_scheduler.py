"""Fleet Execution Scheduler, Risk Debt, Weighted Fairness, Preemption & Bandwidth Tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


from deepseek_infra.infra.workspace import (
    backup_policies,
    backup_targets,
    resilience_fleet_scheduler,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_risk_debt_calculation_age_and_criticality(tmp_settings: Path) -> None:
    """Test Gate K: Risk debt calculation scales with severity, age, criticality, and SLO."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    
    # 1. Fresh warning on standard policy
    fresh_warning = {
        "actionId": "act_fresh_warn",
        "type": "START_DR_DRILL",
        "severity": "warning",
        "createdAt": _utc_iso(now - timedelta(minutes=5)),
        "parameters": {"policyId": "pol_std"},
    }
    pol_std = {"policyId": "pol_std", "criticality": "standard"}
    debt_fresh = resilience_fleet_scheduler.compute_risk_debt(fresh_warning, policy=pol_std, now=now)
    assert debt_fresh["baseSeverityWeight"] == 2.0
    assert debt_fresh["criticalityMultiplier"] == 1.0
    assert debt_fresh["riskDebt"] >= 2.0 and debt_fresh["riskDebt"] < 3.0

    # 2. 14-day-old degraded on standard policy
    old_degraded = {
        "actionId": "act_old_deg",
        "type": "CREATE_REBALANCE_JOB",
        "severity": "degraded",
        "createdAt": _utc_iso(now - timedelta(days=14)),
        "parameters": {"policyId": "pol_std"},
    }
    debt_old = resilience_fleet_scheduler.compute_risk_debt(old_degraded, policy=pol_std, now=now)
    assert debt_old["baseSeverityWeight"] == 5.0
    assert debt_old["ageDays"] >= 14.0
    assert debt_old["ageFactor"] > 1.0
    # Old degraded debt (5.0 * ~8.0) > fresh critical debt (10.0 * 1.0)
    assert debt_old["riskDebt"] > 20.0

    # 3. Critical policy multiplier & SLO breach
    crit_breached = {
        "actionId": "act_crit_breached",
        "type": "CREATE_REPAIR_JOB",
        "severity": "critical",
        "sloBreached": True,
        "createdAt": _utc_iso(now - timedelta(hours=1)),
        "parameters": {"policyId": "pol_crit"},
    }
    pol_crit = {"policyId": "pol_crit", "criticality": "critical"}
    debt_crit = resilience_fleet_scheduler.compute_risk_debt(crit_breached, policy=pol_crit, now=now)
    assert debt_crit["baseSeverityWeight"] == 10.0
    assert debt_crit["criticalityMultiplier"] == 3.0
    assert debt_crit["sloBreachFactor"] == 1.5
    assert debt_crit["riskDebt"] >= 45.0


def test_weighted_fair_queue_prevents_long_term_starvation(tmp_settings: Path) -> None:
    """Test Gate L: Weighted fair queueing prevents long-term starvation of degraded items."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Policy A has 5 recent critical events; Policy B has 1 degraded event from 10 days ago
    actions = [
        {
            "actionId": f"act_a_{i}",
            "type": "CREATE_REPAIR_JOB",
            "severity": "critical",
            "createdAt": _utc_iso(now - timedelta(minutes=10 * i)),
            "parameters": {"policyId": "pol_a"},
        }
        for i in range(5)
    ]
    old_b = {
        "actionId": "act_b_old",
        "type": "CREATE_REPAIR_JOB",
        "severity": "degraded",
        "createdAt": _utc_iso(now - timedelta(days=10)),
        "parameters": {"policyId": "pol_b"},
    }
    actions.append(old_b)

    # History shows Policy A already had 4 actions recently executed
    ordered = resilience_fleet_scheduler.order_actions_fairly(
        actions,
        now=now,
        policy_history={"pol_a": 4, "pol_b": 0},
    )

    # The aged degraded action on pol_b should be scheduled ahead of pol_a due to accumulated debt + fair share penalty
    top_action = ordered[0]
    assert top_action["actionId"] == "act_b_old"
    assert top_action["parameters"]["policyId"] == "pol_b"


def test_safe_point_action_preemption_rules(tmp_settings: Path) -> None:
    """Test Gate M: Only PENDING and CLAIMED-before-effect actions can be preempted."""
    # 1. PENDING is safe to preempt
    act_pending = {"state": "PENDING", "effectClass": "NO_EFFECT"}
    assert resilience_fleet_scheduler.can_preempt_action(act_pending) is True

    # 2. CLAIMED with NO_EFFECT is safe to preempt
    act_claimed = {"state": "CLAIMED", "effectClass": "NO_EFFECT"}
    assert resilience_fleet_scheduler.can_preempt_action(act_claimed) is True

    # 3. EXECUTING (active remote transfer) CANNOT be preempted directly
    act_executing = {"state": "EXECUTING", "effectClass": "CANCELABLE", "effectHandle": {"kind": "repair"}}
    assert resilience_fleet_scheduler.can_preempt_action(act_executing) is False

    # 4. VERIFYING CANNOT be preempted
    act_verifying = {"state": "VERIFYING", "effectClass": "CANCELABLE"}
    assert resilience_fleet_scheduler.can_preempt_action(act_verifying) is False

    # 5. RECONCILING CANNOT be preempted
    act_reconciling = {"state": "RECONCILING", "effectClass": "CANCELABLE"}
    assert resilience_fleet_scheduler.can_preempt_action(act_reconciling) is False


def test_fleet_scheduler_wave_assembly_and_lock_arbitration(tmp_settings: Path) -> None:
    """Test Gate J: Fleet scheduler partitions candidate actions into conflict-free waves."""
    t1 = tmp_settings / "target_1"
    t2 = tmp_settings / "target_2"
    t3 = tmp_settings / "target_3"
    for p in (t1, t2, t3):
        p.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_1", path=t1, failure_domain="zone-a")
    backup_targets.register_filesystem_target("target_2", path=t2, failure_domain="zone-b")
    backup_targets.register_filesystem_target("target_3", path=t3, failure_domain="zone-c")

    backup_policies.create_policy({
        "name": "Policy Wave 1",
        "policyId": "pol_wave_1",
        "targetId": "target_1",
        "replication": {"enabled": True, "minCommittedCopies": 2, "minFailureDomains": 2},
    })
    backup_policies.create_policy({
        "name": "Policy Wave 2",
        "policyId": "pol_wave_2",
        "targetId": "target_2",
        "replication": {"enabled": True, "minCommittedCopies": 2, "minFailureDomains": 2},
    })

    candidate_actions = [
        {
            "actionId": "act_w1",
            "type": "CREATE_REPAIR_JOB",
            "severity": "critical",
            "parameters": {"policyId": "pol_wave_1", "backupId": "b1", "destTargetId": "target_2"},
        },
        {
            "actionId": "act_w2",
            "type": "CREATE_REBALANCE_JOB",
            "severity": "warning",
            # Conflicts on backup b1
            "parameters": {"policyId": "pol_wave_1", "backupId": "b1", "sourceTargetId": "target_1", "destTargetId": "target_3"},
        },
        {
            "actionId": "act_w3",
            "type": "START_DR_DRILL",
            "severity": "warning",
            # Independent policy pol_wave_2
            "parameters": {"policyId": "pol_wave_2", "backupId": "b2", "targetId": "target_2"},
        },
    ]

    snapshot = {"riskDigest": "rd_test_wave", "overallRisk": "warning", "risks": []}
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(snapshot, candidate_actions=candidate_actions)

    assert schedule["status"] == "SCHEDULED"
    assert len(schedule["executionWaves"]) == 1
    wave0 = schedule["executionWaves"][0]
    admitted_ids = [a["actionId"] for a in wave0["actions"]]

    # act_w1 and act_w3 are admitted; act_w2 is deferred due to resource lock conflict on backup:pol_wave_1:b1
    assert "act_w1" in admitted_ids
    assert "act_w3" in admitted_ids
    assert "act_w2" not in admitted_ids

    deferred = schedule["deferredActions"]
    assert any(d["actionId"] == "act_w2" and d["deferReason"] == "resource-lock-conflict" for d in deferred)


def test_transfer_budget_reservation_protects_repair_reserve(tmp_settings: Path) -> None:
    """Test Gate N: Rebalance cannot consume repair reserve bandwidth."""
    actions = [
        {
            "actionId": "act_rep_1",
            "type": "CREATE_REPAIR_JOB",
            "severity": "critical",
            "parameters": {"policyId": "p1", "backupId": "b1", "destTargetId": "t2"},
        },
        {
            "actionId": "act_reb_1",
            "type": "CREATE_REBALANCE_JOB",
            "severity": "warning",
            "parameters": {"policyId": "p2", "backupId": "b2", "sourceTargetId": "t3", "destTargetId": "t4"},
        },
    ]
    snapshot = {"riskDigest": "rd_bw_test", "overallRisk": "critical", "risks": []}
    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(snapshot, candidate_actions=actions)

    assert schedule["transferBudget"]["repairReservePercent"] == 50
    assert schedule["transferBudget"]["rebalanceBlockedByRepairReserve"] is True
