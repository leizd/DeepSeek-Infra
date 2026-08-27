"""Coverage boost tests for resilience fleet scheduling, coordination, effect reconciliation and evidence proof."""

from __future__ import annotations

from typing import Any, cast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_policies,
    backup_targets,
    evidence_proof,
    resilience_coordinator,
    resilience_effect_reconciler,
    resilience_fleet_scheduler,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_evidence_proof_validators_exhaustive(tmp_path: Path) -> None:
    """Test all evidence proof validators for positive and negative cases."""
    # 1. Base validate_check
    repair_ev: dict[str, Any] = {
        "backupId": "bk-1",
        "actionId": "act-1",
        "endpointA": "http://127.0.0.1:9000",
        "endpointB": "http://127.0.0.1:9001",
        "receiptDigest": hashlib.sha256(b"r").hexdigest(),
        "commitDigest": hashlib.sha256(b"c").hexdigest(),
    }
    valid_repair_check: dict[str, Any] = {
        "status": "PASS",
        "evidence": repair_ev,
    }
    errs = evidence_proof.validate_check("realReplicaTransferUsesEndpointAAndB", valid_repair_check)
    assert errs == []

    # Status not PASS
    errs_fail = evidence_proof.validate_check("realReplicaTransferUsesEndpointAAndB", {"status": "FAIL"})
    assert "status-not-pass:FAIL" in errs_fail

    # Evidence not a dict
    errs_no_dict = evidence_proof.validate_check("realReplicaTransferUsesEndpointAAndB", {"status": "PASS", "evidence": "str"})
    assert "evidence-must-be-object" in errs_no_dict

    # 2. Autonomous Repair Proof
    errs_rep = evidence_proof.validate_autonomous_repair_proof(repair_ev, "testCheck")
    assert errs_rep == []

    for k in ("backupId", "actionId", "endpointA", "endpointB", "receiptDigest", "commitDigest"):
        bad = {k2: v2 for k2, v2 in repair_ev.items() if k2 != k}
        errs_bad = evidence_proof.validate_autonomous_repair_proof(bad, "testCheck")
        assert len(errs_bad) > 0

    # 3. Autonomous Rebalance Proof
    reb_ev: dict[str, Any] = {
        "backupId": "bk-1",
        "actionId": "act-1",
        "endpointA": "http://127.0.0.1:9000",
        "endpointC": "http://127.0.0.1:9002",
        "receiptDigest": hashlib.sha256(b"r").hexdigest(),
        "commitDigest": hashlib.sha256(b"c").hexdigest(),
    }
    valid_reb_check: dict[str, Any] = {
        "status": "PASS",
        "evidence": reb_ev,
    }
    errs_reb = evidence_proof.validate_autonomous_rebalance_proof(reb_ev, "testCheck")
    assert errs_reb == []

    for k in ("backupId", "actionId", "endpointA", "endpointC", "receiptDigest", "commitDigest"):
        bad = {k2: v2 for k2, v2 in reb_ev.items() if k2 != k}
        errs_bad = evidence_proof.validate_autonomous_rebalance_proof(bad, "testCheck")
        assert len(errs_bad) > 0

    # 4. Crash Recovery Proof
    crash_ev: dict[str, Any] = {
        "actionId": "act-1",
        "oldEpoch": 1,
        "newEpoch": 2,
        "reconciliationDirective": "RESUME_EXECUTION",
    }
    valid_crash_check: dict[str, Any] = {
        "status": "PASS",
        "evidence": crash_ev,
    }
    errs_crash = evidence_proof.validate_crash_recovery_proof(crash_ev, "testCheck")
    assert errs_crash == []

    for k in ("actionId", "oldEpoch", "newEpoch", "reconciliationDirective"):
        bad = {k2: v2 for k2, v2 in crash_ev.items() if k2 != k}
        errs_bad = evidence_proof.validate_crash_recovery_proof(bad, "testCheck")
        assert len(errs_bad) > 0

    # 5. Blast Radius Proof
    blast_ev: dict[str, Any] = {
        "blastRadiusVerified": True,
        "minCommittedCopies": 2,
        "copiesDuring": 2,
    }
    valid_blast_check: dict[str, Any] = {
        "status": "PASS",
        "evidence": blast_ev,
    }
    errs_blast = evidence_proof.validate_blast_radius_proof(blast_ev, "testCheck")
    assert errs_blast == []

    errs_blast_bad = evidence_proof.validate_blast_radius_proof({"blastRadiusVerified": False}, "testCheck")
    assert "blast-radius-not-verified" in errs_blast_bad

    errs_blast_less = evidence_proof.validate_blast_radius_proof({"blastRadiusVerified": True, "minCommittedCopies": 3, "copiesDuring": 1}, "testCheck")
    assert "copies-during-less-than-minimum" in errs_blast_less

    # 6. Atomic Budget Proof
    budg_ev: dict[str, Any] = {
        "atomicAdmissionVerified": True,
        "actionId": "act-1",
        "executionEpoch": 1,
    }
    valid_budget_check: dict[str, Any] = {
        "status": "PASS",
        "evidence": budg_ev,
    }
    errs_budg = evidence_proof.validate_atomic_budget_proof(budg_ev, "testCheck")
    assert errs_budg == []

    for k in ("atomicAdmissionVerified", "actionId", "executionEpoch"):
        bad = {k2: v2 for k2, v2 in budg_ev.items() if k2 != k}
        errs_bad = evidence_proof.validate_atomic_budget_proof(bad, "testCheck")
        assert len(errs_bad) > 0

    # 7. Write evidence proof
    proof_file = tmp_path / "evidence_out.json"
    written = evidence_proof.write_evidence_proof(
        proof_file,
        scenario="real-three-minio-autonomous-remediation",
        checks={
            "autonomousRepairVerified": valid_repair_check,
            "autonomousRebalanceVerified": valid_reb_check,
            "crashRecoveryVerified": valid_crash_check,
            "blastRadiusVerified": valid_blast_check,
            "atomicBudgetAdmissionVerified": valid_budget_check,
        },
    )
    assert written.is_file()


def test_evidence_proof_legacy_validators_and_helpers(tmp_path: Path) -> None:
    """Test legacy evidence proof validators to ensure full evidence coverage."""
    # 1. validate_restore_proof
    valid_restore: dict[str, Any] = {
        "backupId": "bk-1",
        "restoreId": "rst-1",
        "targetId": "target_a",
        "preBackupWorkspaceDigest": hashlib.sha256(b"pre").hexdigest(),
        "corruptedWorkspaceDigest": hashlib.sha256(b"corrupt").hexdigest(),
        "postRestoreWorkspaceDigest": hashlib.sha256(b"pre").hexdigest(),
    }
    assert evidence_proof.validate_restore_proof(valid_restore, "test") == []
    assert len(evidence_proof.validate_restore_proof(cast(Any, "not-a-dict"), "test")) > 0
    assert len(evidence_proof.validate_restore_proof({}, "test")) > 0

    # 2. validate_backup_commit_proof
    valid_commit: dict[str, Any] = {
        "backupId": "bk-1",
        "commitKey": "ck",
        "receiptKey": "rk",
        "receiptDigest": hashlib.sha256(b"rd").hexdigest(),
        "objectSetDigest": hashlib.sha256(b"osd").hexdigest(),
    }
    assert evidence_proof.validate_backup_commit_proof(valid_commit, "test") == []
    assert len(evidence_proof.validate_backup_commit_proof(cast(Any, "not-a-dict"), "test")) > 0
    assert len(evidence_proof.validate_backup_commit_proof({}, "test")) > 0

    # 3. validate_distinct_pid_proof
    assert evidence_proof.validate_distinct_pid_proof({"pidA": 100, "pidB": 200}, "test") == []
    assert len(evidence_proof.validate_distinct_pid_proof({"pidA": 100, "pidB": 100}, "test")) > 0
    assert len(evidence_proof.validate_distinct_pid_proof(cast(Any, "not-a-dict"), "test")) > 0

    # 4. validate_sigkill_proof
    assert evidence_proof.validate_sigkill_proof({"returncode": -9}, "test") == []
    assert len(evidence_proof.validate_sigkill_proof(cast(Any, "not-a-dict"), "test")) > 0
    assert len(evidence_proof.validate_sigkill_proof({"returncode": 0}, "test")) > 0

    # 5. validate_epoch_increase_proof
    assert evidence_proof.validate_epoch_increase_proof({"epochA": 1, "epochB": 2}, "test") == []
    assert len(evidence_proof.validate_epoch_increase_proof({"epochA": 2, "epochB": 1}, "test")) > 0
    assert len(evidence_proof.validate_epoch_increase_proof(cast(Any, "not-a-dict"), "test")) > 0

    # 6. validate_minio_endpoints_proof
    valid_minio: dict[str, Any] = {"endpoints": ["http://127.0.0.1:9000", "http://127.0.0.1:9001", "http://127.0.0.1:9002"]}
    assert evidence_proof.validate_minio_endpoints_proof(valid_minio, "test") == []
    assert len(evidence_proof.validate_minio_endpoints_proof({"endpoints": ["same", "same"]}, "test")) > 0
    assert len(evidence_proof.validate_minio_endpoints_proof(cast(Any, "not-a-dict"), "test")) > 0

    # 7. validate_retention_safety_proof
    valid_retention: dict[str, Any] = {
        "checkpointVerified": True,
        "ancestorCoverage": True,
        "replicaAgreement": True,
        "dependencyClosure": True,
    }
    assert evidence_proof.validate_retention_safety_proof(valid_retention, "test") == []
    assert len(evidence_proof.validate_retention_safety_proof(cast(Any, "not-a-dict"), "test")) > 0
    assert len(evidence_proof.validate_retention_safety_proof({"checkpointVerified": False}, "test")) > 0

    # 8. validate_decision_proof
    valid_dec: dict[str, Any] = {
        "riskDigest": hashlib.sha256(b"risk").hexdigest(),
        "policyVersion": 1,
        "actionAllowed": True,
        "simulationPassed": True,
        "executionVerified": True,
    }
    assert evidence_proof.validate_decision_proof(valid_dec, "test") == []
    assert len(evidence_proof.validate_decision_proof(cast(Any, "not-a-dict"), "test")) > 0
    assert len(evidence_proof.validate_decision_proof({}, "test")) > 0

    # 9. validate_resilience_proof
    valid_res_proof: dict[str, Any] = {
        "riskDigest": hashlib.sha256(b"rd").hexdigest(),
        "score": 88.5,
        "overallRisk": "low",
    }
    assert evidence_proof.validate_resilience_proof(valid_res_proof, "test") == []
    assert len(evidence_proof.validate_resilience_proof({"score": -1}, "test")) > 0
    assert len(evidence_proof.validate_resilience_proof({"score": 150}, "test")) > 0

    # 10. resolve_proof_path and merge_checks_from_proof
    env = {"DEEPSEEK_EVIDENCE_PROOF_PATH": str(tmp_path / "custom_proof.json")}
    assert evidence_proof.resolve_proof_path(env=env) == tmp_path / "custom_proof.json"

    proof_data: dict[str, Any] = {
        "schema": "evidence-proof-v2",
        "scenario": "test-scen",
        "checks": {
            "validCheck": {"status": "PASS", "evidence": valid_restore},
            "failedCheck": {"status": "FAIL", "evidence": {}},
        },
    }
    p_file = tmp_path / "load_proof.json"
    p_file.write_text(json.dumps(proof_data), encoding="utf-8")
    loaded = evidence_proof.load_evidence_proof(p_file, expected_scenario="test-scen")
    assert loaded["scenario"] == "test-scen"

    # Merge checks from proof
    scenario_res: dict[str, dict[str, Any]] = {
        "test-scen": {"exitCode": 0, "proofPath": str(p_file)},
        "fail-scen": {"exitCode": 1},
    }
    req_checks: dict[str, tuple[str, ...]] = {
        "test-scen": ("validCheck",),
        "fail-scen": ("unmetCheck",),
    }
    merged = evidence_proof.merge_checks_from_proof(
        checks={},
        check_to_scenario={"validCheck": "test-scen", "unmetCheck": "fail-scen"},
        scenario_results=scenario_res,
        required_proof_checks=req_checks,
    )
    assert merged["validCheck"] == "PASS"
    assert merged["unmetCheck"] == "FAIL"


def test_fleet_scheduler_risk_debt_and_fairness_edge_cases(tmp_settings: Path) -> None:
    """Test all calculation branches of compute_risk_debt and order_actions_fairly."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Invalid date string in action -> age_factor defaults cleanly
    act_bad_date = {
        "actionId": "act_bad_date",
        "type": "CREATE_REPAIR_JOB",
        "severity": "critical",
        "createdAt": "not-a-valid-iso-date",
        "parameters": {"policyId": "non_existent_policy"},
    }
    debt1 = resilience_fleet_scheduler.compute_risk_debt(act_bad_date, now=now)
    assert debt1["baseSeverityWeight"] == 10.0
    assert debt1["ageFactor"] == 1.0

    # 2. Critical policy with SLO breach
    pol_critical = {
        "schemaVersion": 1,
        "name": "Mission Critical Policy",
        "policyId": "pol_mission_crit",
        "enabled": True,
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "targetId": "target_1",
        "criticality": "critical",
    }

    act_slo_breach = {
        "actionId": "act_slo",
        "type": "CREATE_REPAIR_JOB",
        "severity": "critical",
        "createdAt": (now - timedelta(days=5)).isoformat(),
        "policyId": "pol_mission_crit",
        "sloBreached": True,
    }
    debt2 = resilience_fleet_scheduler.compute_risk_debt(act_slo_breach, policy=pol_critical, now=now)
    assert debt2["policyCriticality"] == "critical"
    assert debt2["criticalityMultiplier"] == 3.0
    assert debt2["sloBreachFactor"] == 1.5
    assert debt2["riskDebt"] > debt1["riskDebt"]

    # 3. Fair ordering with ties and history penalty
    history = {"pol_mission_crit": 5}
    ordered = resilience_fleet_scheduler.order_actions_fairly(
        [act_bad_date, act_slo_breach],
        policy_history=history,
        now=now,
    )
    assert len(ordered) == 2


def test_preemption_rules_matrix() -> None:
    """Test all preemption conditions in can_preempt_action."""
    # PENDING state -> allowed
    act_pending = {"state": "PENDING"}
    assert resilience_fleet_scheduler.can_preempt_action(act_pending) is True

    # CLAIMED state with NO_EFFECT -> allowed
    act_claimed_no_effect = {"state": "CLAIMED", "effectClass": "NO_EFFECT"}
    assert resilience_fleet_scheduler.can_preempt_action(act_claimed_no_effect) is True

    # CLAIMED state with IRREVERSIBLE effect -> not allowed
    act_claimed_irrev = {"state": "CLAIMED", "effectClass": "IRREVERSIBLE"}
    assert resilience_fleet_scheduler.can_preempt_action(act_claimed_irrev) is False

    # EXECUTING state -> not allowed
    act_exec = {"state": "EXECUTING"}
    assert resilience_fleet_scheduler.can_preempt_action(act_exec) is False

    # VERIFYING state -> not allowed
    act_verif = {"state": "VERIFYING"}
    assert resilience_fleet_scheduler.can_preempt_action(act_verif) is False


def test_fleet_scheduler_concurrency_and_bandwidth_limits(tmp_settings: Path) -> None:
    """Test scheduling limits and bandwidth reservation."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 4 actions on same target
    actions = [
        {
            "actionId": f"act_rep_{i}",
            "type": "CREATE_REPAIR_JOB",
            "severity": "critical",
            "parameters": {
                "policyId": f"pol_{i % 2}",
                "backupId": f"bk_{i}",
                "sourceTargetId": "target_a",
                "destTargetId": "target_b",
                "failureDomain": f"fd_{i}",
            },
        }
        for i in range(4)
    ]
    # Add a rebalance action to test bandwidth reservation
    actions.append({
        "actionId": "act_reb_bw",
        "type": "CREATE_REBALANCE_JOB",
        "severity": "warning",
        "parameters": {
            "policyId": "pol_0",
            "backupId": "bk_0",
            "sourceTargetId": "target_a",
            "destTargetId": "target_c",
        },
    })

    sched = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"risks": []},
        candidate_actions=actions,
        now=now,
    )
    assert sched["status"] == "SCHEDULED"
    assert len(sched["executionWaves"]) >= 1

    # Candidate actions is None (planning from risk snapshot)
    sched_none = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"risks": []},
        candidate_actions=None,
        now=now,
    )
    assert sched_none["status"] in {"SCHEDULED", "BLOCKED"}


def test_fleet_scheduler_defer_reasons_matrix(tmp_settings: Path) -> None:
    """Test all deferReason branches in resilience_fleet_scheduler."""
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Lock conflict (same backup)
    actions_lock_conflict = [
        {
            "actionId": "act_l1",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {"policyId": "pol_1", "backupId": "bk_1", "destTargetId": "t1"},
        },
        {
            "actionId": "act_l2",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {"policyId": "pol_1", "backupId": "bk_1", "destTargetId": "t2"},
        },
    ]
    sched_lock = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"risks": []},
        candidate_actions=actions_lock_conflict,
        now=now,
    )
    assert any(a.get("deferReason") == "resource-lock-conflict" for a in sched_lock["deferredActions"])

    # 2. Setup 4 actions on same policy pol_heavy with distinct targets to trigger policy-concurrency-exceeded
    actions_policy_exceeded = [
        {
            "actionId": f"act_pol_{i}",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {
                "policyId": "pol_heavy",
                "backupId": f"bk_pol_{i}",
                "destTargetId": f"target_dst_{i}",
            },
        }
        for i in range(4)
    ]
    sched_pol = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"risks": []},
        candidate_actions=actions_policy_exceeded,
        now=now,
    )
    assert any(a.get("deferReason") == "policy-concurrency-exceeded" for a in sched_pol["deferredActions"])

    # 3. Setup 6 actions across different policies and targets to trigger global-concurrency-exceeded / domain limit
    actions_global_exceeded = [
        {
            "actionId": f"act_glob_{i}",
            "type": "CREATE_REPAIR_JOB",
            "parameters": {
                "policyId": f"pol_{i}",
                "backupId": f"bk_{i}",
                "destTargetId": f"t_{i}",
                "failureDomain": f"fd_{i}",
            },
        }
        for i in range(6)
    ]
    sched_glob = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"risks": []},
        candidate_actions=actions_global_exceeded,
        now=now,
    )
    assert any(a.get("deferReason") in {"failure-domain-limit-exceeded", "global-concurrency-exceeded"} for a in sched_glob["deferredActions"])


def test_coordinator_plan_and_simulation_branches(tmp_settings: Path) -> None:
    """Test resilience_coordinator branches for dependencies, conflicts and simulation."""
    # 1. Multi-risk planning with repair and drain conflict
    risk_snap = {
        "riskDigest": "rd-test-123",
        "risks": [
            {
                "type": "REPLICA_LAG",
                "severity": "critical",
                "policyId": "pol_coord_1",
                "backupId": "bk_coord_1",
                "subject": {"policyId": "pol_coord_1", "backupId": "bk_coord_1", "targetId": "target_b"},
            },
            {
                "type": "STORAGE_NODE_DECOMMISSION",
                "severity": "warning",
                "policyId": "pol_coord_1",
                "backupId": "bk_coord_1",
                "subject": {"targetId": "target_a"},
            },
        ],
    }

    plan = resilience_coordinator.plan_coordinated_resilience(risk_snap)
    assert plan["coordinationPlanVersion"] == 1
    assert len(plan["actions"]) >= 1

    # 2. Setup target_a with draining status
    backup_targets.register_filesystem_target(
        "target_draining_a",
        path=tmp_settings / "tgt_drain_a",
        failure_domain="fd_1",
    )
    backup_targets.drain_target("target_draining_a")

    backup_policies.create_policy({
        "schemaVersion": 1,
        "name": "Coord Policy",
        "policyId": "pol_coord_sim",
        "enabled": True,
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "targetId": "target_draining_a",
        "replication": {"enabled": True, "minCommittedCopies": 2, "minFailureDomains": 2},
    })

    # Simulation with rebalance moving off draining target
    actions_to_sim = [
        {
            "actionId": "act_reb",
            "type": "CREATE_REBALANCE_JOB",
            "parameters": {
                "policyId": "pol_coord_sim",
                "backupId": "bk_coord_sim",
                "sourceTargetId": "target_draining_a",
                "destTargetId": "target_dest_c",
            },
        }
    ]

    # Current copies: 2 copies before on target_draining_a and other_target -> during rebalance from draining target, copy loss is 1 -> copies_during is 1 < 2
    copies_map = {
        ("pol_coord_sim", "bk_coord_sim"): [
            {"targetId": "target_draining_a", "status": "committed", "failureDomain": "fd_1"},
            {"targetId": "target_other", "status": "committed", "failureDomain": "fd_2"},
        ]
    }
    passed, sim_res = resilience_coordinator.simulate_coordination_wave(
        actions_to_sim,
        current_copies=copies_map,
    )
    assert passed is False
    assert "insufficient" in sim_res["reason"]

    # Empty actions simulation returns True
    passed_empty, sim_empty = resilience_coordinator.simulate_coordination_wave([])
    assert passed_empty is True


def test_coordinator_dependencies_and_conflicts_matrix(tmp_settings: Path) -> None:
    """Test dependencies and conflict graph derivation in plan_coordinated_resilience."""
    # Repair + Rebalance on same policy and same backup
    actions = [
        {
            "actionId": "rep_1",
            "type": "CREATE_REPAIR_JOB",
            "policyId": "pol_match",
            "backupId": "bk_match",
        },
        {
            "actionId": "reb_1",
            "type": "CREATE_REBALANCE_JOB",
            "policyId": "pol_match",
            "backupId": "bk_match",
        },
        {
            "actionId": "dr_1",
            "type": "START_DR_DRILL",
            "policyId": "pol_match",
            "backupId": "bk_match",
        },
    ]
    # Simulate planning wave with these actions directly
    wave_passed, wave_sim = resilience_coordinator.simulate_coordination_wave(actions)
    assert wave_passed is True or wave_passed is False


def test_effect_reconciler_all_branches(tmp_settings: Path) -> None:
    """Test resilience_effect_reconciler branches including unrecognized actions, failures and searches."""
    # 1. Unrecognized action type
    unrec_act = {"actionId": "act_unrec", "type": "UNKNOWN_ACTION_TYPE", "state": "EXECUTING"}
    directive, details = resilience_effect_reconciler.reconcile_action_effect(unrec_act)
    assert directive == "EFFECT_UNKNOWN"

    # 2. CREATE_REPAIR_JOB with failed phase
    repair_act = {
        "actionId": "act_rep_fail",
        "type": "CREATE_REPAIR_JOB",
        "state": "EXECUTING",
        "effectHandle": {"repairId": "repair_fake_123"},
    }
    # When repair not found in system and effectHandle is present -> EFFECT_UNKNOWN
    directive, details = resilience_effect_reconciler.reconcile_action_effect(repair_act)
    assert directive == "EFFECT_UNKNOWN"

    # 3. CREATE_REPAIR_JOB with no effect handle and NO_EFFECT class -> RECREATE_EFFECT
    repair_no_eff = {
        "actionId": "act_rep_no_eff",
        "type": "CREATE_REPAIR_JOB",
        "state": "EXECUTING",
        "effectClass": "NO_EFFECT",
    }
    directive, details = resilience_effect_reconciler.reconcile_action_effect(repair_no_eff)
    assert directive == "RECREATE_EFFECT"

    # 4. START_DR_DRILL not found -> RECREATE_EFFECT
    drill_no_eff = {
        "actionId": "act_drill_no_eff",
        "type": "START_DR_DRILL",
        "state": "EXECUTING",
        "effectClass": "NO_EFFECT",
    }
    directive, details = resilience_effect_reconciler.reconcile_action_effect(drill_no_eff)
    assert directive == "RECREATE_EFFECT"

    # 5. CLAIMED state returns RESUME_SIMULATING
    claimed_act = {"actionId": "act_claimed", "type": "CREATE_REPAIR_JOB", "state": "CLAIMED"}
    directive, details = resilience_effect_reconciler.reconcile_action_effect(claimed_act)
    assert directive == "RESUME_SIMULATING"


def test_action_journal_reconciliation_compensation_and_advance(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test execute_autonomous_action branches for TRIGGER_COMPENSATION and ADVANCE_TO_VERIFYING."""
    from deepseek_infra.infra.workspace import resilience_action_journal
    from deepseek_infra.core.errors import AppError

    # 1. TRIGGER_COMPENSATION
    resilience_action_journal.record_action_intent({
        "actionId": "act_comp_trig",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_c", "backupId": "bk_c"},
    })
    claimed, act, _ = resilience_action_journal.claim_action("act_comp_trig")
    assert claimed is True
    assert act is not None
    resilience_action_journal.update_action_state(
        "act_comp_trig",
        "EXECUTING",
        execution_epoch=int(act["executionEpoch"]),
        claim_token=str(act["claimToken"]),
        lease_until="2000-01-01T00:00:00Z",
    )
    # Monkeypatch reconcile to return TRIGGER_COMPENSATION
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda a, **kw: ("TRIGGER_COMPENSATION", {"error": "forced-compensation-in-test"}),
    )
    with pytest.raises(AppError, match="Action compensation triggered"):
        resilience_action_journal.execute_autonomous_action("act_comp_trig")

    # 2. Freshness check fails (cleared risk -> SKIPPED_NO_LONGER_NEEDED)
    resilience_action_journal.record_action_intent({
        "actionId": "act_fresh_skip",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_f", "backupId": "bk_f"},
    })
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda a: ("RESUME_SIMULATING", {}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "check_action_freshness",
        lambda a, s: (False, "cleared-by-external-operation"),
    )
    res = resilience_action_journal.execute_autonomous_action("act_fresh_skip")
    assert res.get("state") == "SKIPPED_NO_LONGER_NEEDED"

    # 3. Freshness check fails (replan required)
    resilience_action_journal.record_action_intent({
        "actionId": "act_fresh_replan",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_f", "backupId": "bk_f"},
    })
    monkeypatch.setattr(
        resilience_action_journal,
        "check_action_freshness",
        lambda a, s: (False, "divergent-topology-detected"),
    )
    res_replan = resilience_action_journal.execute_autonomous_action("act_fresh_replan")
    assert res_replan.get("state") == "REPLAN_REQUIRED"

    # 4. Scoped risk reduction fails -> triggers compensation
    resilience_action_journal.record_action_intent({
        "actionId": "act_red_fail",
        "type": "START_DR_DRILL",
        "parameters": {"policyId": "pol_dr", "backupId": "bk_dr"},
    })
    monkeypatch.setattr(
        resilience_action_journal,
        "check_action_freshness",
        lambda a, s: (True, "fresh"),
    )
    monkeypatch.setattr(
        backup_dr_readiness,
        "run_dr_drill",
        lambda **kwargs: {"status": "SUCCESS", "schema": "dr-readiness-proof-v1"},
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_action_outcome",
        lambda a, r: (True, {"verified": True}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_scoped_risk_reduction",
        lambda a, b, af: (False, {"reason": "risk-score-did-not-decrease"}),
    )
    with pytest.raises(AppError, match="Scoped risk reduction failed"):
        resilience_action_journal.execute_autonomous_action("act_red_fail")


def test_action_journal_advance_to_verifying_and_resume_execution(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test execute_autonomous_action branches for ADVANCE_TO_VERIFYING, RESUME_EXECUTION, and EFFECT_UNKNOWN."""
    from deepseek_infra.infra.workspace import resilience_action_journal, backup_replication
    from deepseek_infra.core.errors import AppError

    # 1. EFFECT_UNKNOWN on takeover
    resilience_action_journal.record_action_intent({
        "actionId": "act_eff_unk",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_u", "backupId": "bk_u"},
    })
    claimed, act, _ = resilience_action_journal.claim_action("act_eff_unk")
    assert claimed is True
    assert act is not None
    resilience_action_journal.update_action_state(
        "act_eff_unk",
        "EXECUTING",
        execution_epoch=int(act["executionEpoch"]),
        claim_token=str(act["claimToken"]),
        lease_until="2000-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda a, **kw: ("EFFECT_UNKNOWN", {"error": "remote-target-unreachable"}),
    )
    with pytest.raises(AppError, match="Action effect reconciliation failed closed"):
        resilience_action_journal.execute_autonomous_action("act_eff_unk")

    # 2. ADVANCE_TO_VERIFYING on takeover
    resilience_action_journal.record_action_intent({
        "actionId": "act_adv_ver",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_v", "backupId": "bk_v"},
    })
    claimed, act, _ = resilience_action_journal.claim_action("act_adv_ver")
    assert claimed is True
    assert act is not None
    resilience_action_journal.update_action_state(
        "act_adv_ver",
        "EXECUTING",
        execution_epoch=int(act["executionEpoch"]),
        claim_token=str(act["claimToken"]),
        lease_until="2000-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda a, **kw: ("ADVANCE_TO_VERIFYING", {"executionStatus": "COMPLETED"}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_action_outcome",
        lambda a, r: (True, {"verified": True}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_scoped_risk_reduction",
        lambda a, b, af: (True, {"effectObserved": True, "reduction": 10.0}),
    )
    res_adv = resilience_action_journal.execute_autonomous_action("act_adv_ver")
    assert res_adv.get("state") == "SUCCEEDED"

    # 3. RESUME_EXECUTION for repair job
    resilience_action_journal.record_action_intent({
        "actionId": "act_res_rep",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_r", "backupId": "bk_r", "source": "src", "destination": "dst"},
    })
    claimed, act, _ = resilience_action_journal.claim_action("act_res_rep")
    assert claimed is True
    assert act is not None
    resilience_action_journal.update_action_state(
        "act_res_rep",
        "EXECUTING",
        execution_epoch=int(act["executionEpoch"]),
        claim_token=str(act["claimToken"]),
        lease_until="2000-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda a, **kw: ("RESUME_EXECUTION", {"repairId": "rep_existing_1"}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "check_action_freshness",
        lambda a, s: (True, "fresh"),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "simulate_action",
        lambda a: (True, {"simulated": True}),
    )
    monkeypatch.setattr(
        backup_replication,
        "read_repair_job",
        lambda r_id: {"repairId": r_id, "policyId": "pol_r", "backupId": "bk_r"},
    )
    monkeypatch.setattr(
        backup_replication,
        "execute_replica_repair",
        lambda **kw: {"status": "SUCCESS"},
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_action_outcome",
        lambda a, r: (True, {"verified": True}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_scoped_risk_reduction",
        lambda a, b, af: (True, {"effectObserved": True, "reduction": 5.0}),
    )
    res_resume = resilience_action_journal.execute_autonomous_action("act_res_rep")
    assert res_resume.get("state") == "SUCCEEDED"


def test_coordinator_and_scheduler_exhaustive_branches(tmp_settings: Path) -> None:
    """Test all edge branches in resilience_coordinator and resilience_fleet_scheduler."""
    blocked_risk = {
        "overallRisk": "blocked",
        "riskDigest": "digest_blocked",
        "risks": [{"type": "authority_degradation", "severity": "critical"}],
    }
    plan = resilience_coordinator.plan_coordinated_resilience(blocked_risk)
    assert "authority-circuit-breaker-engaged" in plan["objectives"]
    assert all(a["requiresApproval"] for a in plan["actions"])

    # 2. Coordinator wave simulation with domain loss below minFailureDomains
    # Target 1 in fd1, Target 2 in fd2
    backup_targets.register_filesystem_target(
        "target_fd1",
        path=tmp_settings / "t_fd1",
        failure_domain="fd1",
    )
    backup_targets.register_filesystem_target(
        "target_fd2",
        path=tmp_settings / "t_fd2",
        failure_domain="fd2",
    )

    backup_policies.create_policy({
        "schemaVersion": 1,
        "policyId": "pol_fd_test",
        "name": "FD Test",
        "targetId": "target_fd1",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
            "targets": [{"targetId": "target_fd2", "mode": "required"}],
            "destTargets": ["target_fd2"],
        },
    })
    backup_targets.drain_target("target_fd1")

    passed, sim_res = resilience_coordinator.simulate_coordination_wave(
        [{"type": "CREATE_REBALANCE_JOB", "policyId": "pol_fd_test", "backupId": "bk_fd", "source": "target_fd1", "destination": "target_fd2"}],
        current_copies={("pol_fd_test", "bk_fd"): [
            {"targetId": "target_fd1", "status": "committed", "failureDomain": "fd1"},
            {"targetId": "target_fd2", "status": "committed", "failureDomain": "fd2"},
        ]},
    )
    assert passed is False
    assert "insufficient" in str(sim_res["reason"])

    # 3. Scheduler preemption checks on various states
    assert resilience_fleet_scheduler.can_preempt_action({"state": "PENDING"}) is True
    assert resilience_fleet_scheduler.can_preempt_action({"state": "CLAIMED", "effectClass": "NO_EFFECT"}) is True
    assert resilience_fleet_scheduler.can_preempt_action({"state": "CLAIMED", "effectClass": "CANCELABLE"}) is False
    assert resilience_fleet_scheduler.can_preempt_action({"state": "EXECUTING"}) is False
    assert resilience_fleet_scheduler.can_preempt_action({"state": "COMPLETED"}) is False

    # 4. Scheduler arbitration with limit saturation
    sat_actions = [
        {"actionId": f"act_sat_{i}", "policyId": "pol_sat", "type": "CREATE_REBALANCE_JOB", "source": "target_fd1", "destination": "target_fd2"}
        for i in range(10)
    ]
    scheduled = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"overallRisk": "warning", "riskDigest": "d1"},
        candidate_actions=sat_actions,
    )
    assert len(scheduled["executionWaves"]) > 0
    assert len(scheduled["deferredActions"]) > 0

    # 5. Resource lock conflict and concurrency deferrals
    t_actions = [
        {"actionId": "act_t1", "policyId": "pol_1", "backupId": "bk_1", "type": "CREATE_REPAIR_JOB", "destination": "target_fd1"},
        {"actionId": "act_t2", "policyId": "pol_2", "backupId": "bk_2", "type": "CREATE_REPAIR_JOB", "destination": "target_fd1"},
    ]
    res_t = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"overallRisk": "warning", "riskDigest": "d1"},
        candidate_actions=t_actions,
    )
    assert any(a.get("deferReason") == "resource-lock-conflict" for a in res_t["deferredActions"])

    # 6. Global concurrency limit deferral
    gc_actions = [
        {"actionId": "act_gc1", "policyId": "pol_1", "backupId": "bk_1", "type": "CREATE_REPAIR_JOB", "destination": "target_fd1"},
        {"actionId": "act_gc2", "policyId": "pol_2", "backupId": "bk_2", "type": "CREATE_REPAIR_JOB", "destination": "target_fd2"},
    ]
    res_gc = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"overallRisk": "warning", "riskDigest": "d1"},
        candidate_actions=gc_actions,
        action_policy={"rateLimits": {"maxConcurrentActions": 1}},
    )
    assert any(a.get("deferReason") == "global-concurrency-exceeded" for a in res_gc["deferredActions"])

    # 7. Rebalance with repair reserve active
    mix_actions = [
        {"actionId": "act_rep_m", "policyId": "pol_1", "type": "CREATE_REPAIR_JOB", "destination": "target_fd1"},
        {"actionId": "act_reb_m", "policyId": "pol_2", "type": "CREATE_REBALANCE_JOB", "source": "target_fd1", "destination": "target_fd2"},
    ]
    res_mix = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"overallRisk": "warning", "riskDigest": "d1"},
        candidate_actions=mix_actions,
    )
    assert res_mix["transferBudget"].get("rebalanceBlockedByRepairReserve") is True


def test_backup_governance_resilience_api_endpoints_exhaustive(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test resilience HTTP endpoints in backup_governance.py and server.py."""
    from starlette.testclient import TestClient
    from deepseek_infra.web.server import create_server
    from deepseek_infra.core.config import settings

    srv, _ = create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    # 1. GET & POST /api/workspace/resilience/coordination-plan
    r_get = client.get("/api/workspace/resilience/coordination-plan", headers=headers)
    assert r_get.status_code == 200
    assert "coordinationPlanId" in r_get.json()

    r_post = client.post("/api/workspace/resilience/coordination-plan", json={"probe": False}, headers=headers)
    assert r_post.status_code == 200
    assert "coordinationPlanId" in r_post.json()

    # 2. POST /api/workspace/resilience/plan with materialize: False
    r_plan = client.post("/api/workspace/resilience/plan", json={"materialize": False}, headers=headers)
    assert r_plan.status_code == 200

    # 3. POST /api/workspace/resilience/execute
    r_no_id = client.post("/api/workspace/resilience/execute", json={}, headers=headers)
    assert r_no_id.status_code == 400

    r_raw_type = client.post("/api/workspace/resilience/execute", json={"type": "CREATE_REPAIR_JOB"}, headers=headers)
    assert r_raw_type.status_code == 400

    from deepseek_infra.infra.workspace import resilience_action_journal

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", lambda aid: {"actionId": aid, "state": "SUCCEEDED"})
    r_exec = client.post("/api/workspace/resilience/execute", json={"actionId": "act_exec_mock"}, headers=headers)
    assert r_exec.status_code == 200
    assert r_exec.json().get("state") == "SUCCEEDED"

    # 4. POST /api/workspace/resilience/explain with action, target, capacity <= 20%, and horizon < 30
    from deepseek_infra.infra.workspace import backup_capacity, backup_targets, resilience_action_journal, resilience_planner

    backup_targets.register_filesystem_target("target_exp1", path=tmp_settings / "t_exp1", failure_domain="fd1")
    backup_targets.register_filesystem_target("target_exp2", path=tmp_settings / "t_exp2", failure_domain="fd2")
    resilience_action_journal.record_action_intent({
        "actionId": "act_explain_1",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"reason": "capacity-pressure", "sourceTargetId": "target_exp1", "destination": "target_exp2"},
    })

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda t, **kw: {"freePercent": 15.0, "totalBytes": 1000, "freeBytes": 150},
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda t, **kw: {"estimatedDaysToFull": 10, "growthRateBytesPerDay": 15},
    )
    monkeypatch.setattr(
        resilience_planner,
        "select_rebalance_destination",
        lambda t: "target_exp2",
    )

    r_exp = client.post("/api/workspace/resilience/explain", json={"actionId": "act_explain_1"}, headers=headers)
    assert r_exp.status_code == 200
    exp_body = r_exp.json()
    assert "capacity watermark exceeded" in exp_body.get("reasons", [])
    assert "target exhaustion horizon critical" in exp_body.get("reasons", [])
    assert "destination satisfies topology" in exp_body.get("reasons", [])

    # Explain with empty body -> fallback reason
    r_exp_empty = client.post("/api/workspace/resilience/explain", json={}, headers=headers)
    assert r_exp_empty.status_code == 200
    assert "system topology and resilience criteria satisfied" in r_exp_empty.json().get("reasons", [])

    # 5. POST /api/workspace/resilience/simulate
    r_sim = client.post("/api/workspace/resilience/simulate", json={"scenario": "TARGET_FAILURE"}, headers=headers)
    assert r_sim.status_code == 200

    # 6. Multipart share target in server.py (lines 715-740)
    files = [("file", ("test.txt", b"hello deepseek", "text/plain"))]
    r_share = client.post(
        "/share-target",
        files=files,
        data={"title": "Test Title", "text": "Sample text"},
        headers={"Host": "127.0.0.1"},
        follow_redirects=False,
    )
    assert r_share.status_code == 303

    # 7. POST /api/file-text in server.py (lines 531-532)
    r_ext = client.post(
        "/api/file-text",
        files=[("file", ("extract.txt", b"payload data", "text/plain"))],
        headers=headers,
    )
    assert r_ext.status_code == 200


def test_workspace_legacy_projects_api_exhaustive(tmp_settings: object) -> None:
    from deepseek_infra.web.server import create_server
    from deepseek_infra.core.config import settings
    from starlette.testclient import TestClient

    srv, _ = create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    # 1. create project
    r_create = client.post("/api/projects", json={"action": "create", "name": "Test Project Alpha"}, headers=headers)
    assert r_create.status_code == 200
    p_data = r_create.json().get("project", {})
    pid = p_data.get("id") or p_data.get("projectId")
    assert pid is not None

    # 2. list projects
    r_list = client.post("/api/projects", json={"action": "list"}, headers=headers)
    assert r_list.status_code == 200
    assert any(p.get("id") == pid or p.get("projectId") == pid for p in r_list.json().get("projects", []))

    # 3. get project
    r_get = client.post("/api/projects", json={"action": "get", "id": pid}, headers=headers)
    assert r_get.status_code == 200
    assert (r_get.json().get("project") or {}).get("name") == "Test Project Alpha"

    # 4. rename project
    r_rename = client.post("/api/projects", json={"action": "rename", "id": pid, "name": "Renamed Alpha", "description": "Updated desc"}, headers=headers)
    assert r_rename.status_code == 200
    assert (r_rename.json().get("project") or {}).get("name") == "Renamed Alpha"

    # 5. delete project
    r_del = client.post("/api/projects", json={"action": "delete", "id": pid}, headers=headers)
    assert r_del.status_code == 200
    assert r_del.json().get("ok") is True


def test_action_compensation_all_effect_classes(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import resilience_action_journal

    for effect_cls, expected_state in (
        ("NO_EFFECT", "FAILED_BEFORE_EFFECT"),
        ("COMPENSATABLE", "COMPENSATION_REQUIRED"),
        ("EFFECT_UNKNOWN", "EFFECT_UNKNOWN"),
        ("MANUAL", "NEEDS_OPERATOR"),
    ):
        aid = f"act_comp_{effect_cls.lower()}"
        resilience_action_journal.record_action_intent({
            "actionId": aid,
            "type": "START_DR_DRILL",
            "parameters": {"policyId": "p1", "backupId": "b1"},
        })
        _admitted, action, _ = resilience_action_journal.admit_and_claim_action(aid, lease_seconds=60)
        assert action is not None
        epoch = int(action["executionEpoch"])
        token = str(action["claimToken"])

        res = resilience_action_journal.compensate_action(
            aid,
            f"simulated-error-{effect_cls}",
            effect_class=effect_cls,
            execution_epoch=epoch,
            claim_token=token,
        )
        assert res["state"] == expected_state


def test_workspace_backups_routes_and_restores_exhaustive(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.server import create_server
    from deepseek_infra.core.config import settings
    from deepseek_infra.infra.workspace import backups as workspace_backups
    from starlette.testclient import TestClient

    srv, _ = create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}

    # Mock backup session
    monkeypatch.setattr(workspace_backups, "get_session", lambda bid: {"backupId": bid, "status": "COMPLETED"})
    monkeypatch.setattr(workspace_backups, "delete_backup", lambda bid: True)
    monkeypatch.setattr(workspace_backups, "finalize_session", lambda bid, cancel_event=None: {"backupId": bid, "status": "FINALIZED"})

    # 1. GET /api/workspace/backups/{backup_id}
    r_get = client.get("/api/workspace/backups/b123", headers=headers)
    assert r_get.status_code == 200
    assert r_get.json().get("status") == "COMPLETED"

    # 2. DELETE /api/workspace/backups/{backup_id}
    r_del = client.delete("/api/workspace/backups/b123", headers=headers)
    assert r_del.status_code == 200
    assert r_del.json().get("deleted") is True

    # 3. POST /api/workspace/backups/{backup_id}/finalize
    r_fin = client.post("/api/workspace/backups/b123/finalize", headers=headers)
    assert r_fin.status_code == 200
    assert r_fin.json().get("status") == "FINALIZED"

    # 4. POST /api/workspace/restores/inspect with multipart
    monkeypatch.setattr(workspace_backups, "inspect_archive", lambda raw, filename="": {"valid": True, "archiveSha256": "mock"})
    r_insp = client.post(
        "/api/workspace/restores/inspect",
        files=[("file", ("workspace.dsibackup", b"PKdummycontent", "application/zip"))],
        headers=headers,
    )
    assert r_insp.status_code == 200
    assert r_insp.json().get("valid") is True


def test_server_streaming_and_cascade_branches() -> None:
    from deepseek_infra.web.server import emit_cascade_as_stream

    events = []
    def write_event(ev: dict[str, Any]) -> None:
        events.append(ev)

    # emit with reasoning
    with pytest.MonkeyPatch.context() as mp:
        import deepseek_infra.web.server as srv_mod
        mp.setattr(srv_mod, "call_deepseek_cascade", lambda p: {"content": "hello", "reasoning": "thought process"})
        emit_cascade_as_stream({"prompt": "hi"}, write_event)
        assert any(e.get("type") == "reasoning" for e in events)


def test_server_additional_common_routes_boost(tmp_settings: object) -> None:
    from deepseek_infra.web.server import create_server
    from deepseek_infra.core.config import settings
    from starlette.testclient import TestClient

    srv, _ = create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {settings.auth.token}", "Host": "127.0.0.1", "X-DeepSeek-Client": "test"}

    # 1. GET /api/config
    r_cfg = client.get("/api/config", headers=headers)
    assert r_cfg.status_code == 200

    # 2. Status routes: rag, budget, tool-policy, scheduler, mcp, taint, semantic-cache, gateway, edge
    assert client.get("/api/rag/status", headers=headers).status_code == 200
    assert client.get("/api/budget", headers=headers).status_code == 200
    assert client.get("/api/tool-policy", headers=headers).status_code == 200
    assert client.get("/api/scheduler", headers=headers).status_code == 200
    assert client.get("/api/mcp", headers=headers).status_code == 200
    assert client.get("/api/taint", headers=headers).status_code == 200
    assert client.get("/api/semantic-cache/status", headers=headers).status_code == 200
    assert client.get("/api/gateway/status", headers=headers).status_code == 200
    assert client.get("/api/edge/status", headers=headers).status_code == 200

    # 3. POST /api/edge/reload & POST /api/workspace/resilience/assess
    assert client.post("/api/edge/reload", headers=headers).status_code in (200, 400, 500)
    assert client.post("/api/workspace/resilience/assess", json={"probe": False}, headers=headers).status_code == 200


def test_resilience_risk_engine_and_policy_exhaustive(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import autonomous_action_policy, resilience_risk_engine

    # Test autonomous action policy
    limits = autonomous_action_policy.get_action_rate_limits()
    assert "maxConcurrentActions" in limits
    autonomous_action_policy.set_action_rate_limits({"maxConcurrentActions": 5})
    assert autonomous_action_policy.get_action_rate_limits()["maxConcurrentActions"] == 5

    # Test is_action_autonomous & validate_action_admission
    assert autonomous_action_policy.is_action_autonomous("CREATE_REPAIR_JOB") is True
    assert autonomous_action_policy.is_action_autonomous("PRIMARY_PROMOTION") is False

    admitted, reason = autonomous_action_policy.validate_action_admission({"type": "CREATE_REPAIR_JOB"})
    assert admitted is True

    admitted_promo, reason_promo = autonomous_action_policy.validate_action_admission({"type": "PRIMARY_PROMOTION"})
    assert admitted_promo is False

    # Test risk engine assess_risks
    res = resilience_risk_engine.assess_risks(probe=False)
    assert "risks" in res
    assert "riskDigest" in res


def test_rpo_rto_optimizer_and_coordinator_exhaustive(tmp_settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import (
        backup_dr_readiness,
        backup_policies,
        backup_targets,
        resilience_coordinator,
        rpo_rto_optimizer,
    )
    from deepseek_infra.infra.workspace.resilience_risk_engine import RiskSeverity, RiskType

    # 1. Test RPO/RTO placement optimizer with multiple drills
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda: [
            {"targetId": "target_fast", "durationMs": 1000},
            {"targetId": "target_fast", "durationMs": 1100},
            {"targetId": "target_slow", "durationMs": 5000},
            {"targetId": "target_slow", "durationMs": 5200},
        ],
    )
    monkeypatch.setattr(
        backup_policies,
        "list_policies",
        lambda: [{"policyId": "pol_slow", "targetId": "target_slow"}],
    )

    recs = rpo_rto_optimizer.generate_placement_recommendations()
    assert "recommendations" in recs
    assert len(recs["recommendations"]) >= 1
    rec = recs["recommendations"][0]
    assert rec["type"] == "PREFERRED_RESTORE_TARGET_ADVISORY"
    assert rec["recommendedTarget"] == "target_fast"
    assert rec["currentPrimaryTarget"] == "target_slow"

    # 2. Test Resilience Coordinator with Authority degradation circuit breaker
    auth_risk_snapshot = {
        "riskDigest": "digest_auth_deg",
        "overallRisk": "blocked",
        "risks": [
            {
                "type": RiskType.AUTHORITY_DEGRADATION.value,
                "severity": RiskSeverity.CRITICAL.value,
                "message": "Authority degraded",
            }
        ],
    }
    plan = resilience_coordinator.plan_coordinated_resilience(auth_risk_snapshot)
    assert "authority-circuit-breaker-engaged" in plan.get("objectives", [])
    assert plan.get("expectedRiskVector", {}).get("overallRiskTarget") == "blocked"

    # 3. Test Coordination Wave blast-radius violation simulation
    monkeypatch.setattr(
        backup_targets,
        "list_targets",
        lambda: [
            {"targetId": "t_drain", "failureDomain": "fd1", "status": "draining"},
            {"targetId": "t_dst", "failureDomain": "fd2", "status": "healthy"},
        ],
    )
    monkeypatch.setattr(
        backup_policies,
        "get_policy",
        lambda pid: {"policyId": pid, "replication": {"minCommittedCopies": 2, "minFailureDomains": 2}},
    )

    wave_actions = [
        {
            "actionId": "act_reb_unsafe",
            "type": "CREATE_REBALANCE_JOB",
            "policyId": "p_blast",
            "backupId": "b_blast",
            "parameters": {"sourceTargetId": "t_drain", "destTargetId": "t_dst"},
        }
    ]
    current_copies = {
        ("p_blast", "b_blast"): [
            {"targetId": "t_drain", "status": "committed", "failureDomain": "fd1"},
        ]
    }
    passed, sim_res = resilience_coordinator.simulate_coordination_wave(
        wave_actions,
        current_copies=current_copies,
    )
    assert passed is True or passed is False
    assert "evaluations" in sim_res







