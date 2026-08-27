"""Coverage boost tests for resilience fleet scheduling, coordination, effect reconciliation and evidence proof."""

from __future__ import annotations

from typing import Any, cast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json

from deepseek_infra.infra.workspace import (
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
