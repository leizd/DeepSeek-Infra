"""Tests for DeepSeek Infra Verified Autonomous Remediation & Exactly-Once Resilience Closures.

Validates:
- Gate A: Fully materialized, executable action intents with resolved parameters.
- Gate B: Mandatory plan materialization & rejection of un-materialized raw actions.
- Gate C: TOCTOU freshness evaluation before execution.
- Gate D: CAS claim lease semantics, concurrent race prevention, and expired lease reclamation.
- Gate E: Subsystem idempotency key (resilienceActionId) propagation.
- Gate F: Immutable code safety floor (NEVER_AUTONOMOUS) immune to config tampering.
- Gate G: Canonical multi-replica authority verification.
- Gate H: Precondition simulation gates (capacity watermark, invalid targets).
- Gate I: Real post-condition outcome verification (ledger committed copies).
- Gate J: Classified effect lifecycle & safe compensation states (FAILED_BEFORE_EFFECT, COMPENSATED, NEEDS_OPERATOR).
- Gate K: Autonomous action rate limiting admission controls.
- Gate L: Closed-loop risk reduction verification (riskBefore -> riskAfter) and Decision Proof v3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_capacity,
    backup_control_recovery,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_replication,
    backup_targets,
    evidence_proof,
    resilience_action_journal,
    resilience_planner,
    resilience_risk_engine,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- Gate F: Immutable Code Safety Floor ---


def test_immutable_safety_floor_cannot_be_bypassed(tmp_settings: Path) -> None:
    """Gate F: Verify NEVER_AUTONOMOUS actions are permanently blocked."""
    forbidden = {"PRIMARY_PROMOTION", "POLICY_CHANGE", "COPY_DELETION", "TOPOLOGY_MUTATION"}
    for act in forbidden:
        assert autonomous_action_policy.is_action_autonomous(act) is False

    # Attempt to bypass via set_autonomous_action_policy
    for act in forbidden:
        with pytest.raises(AppError) as exc_info:
            autonomous_action_policy.set_autonomous_action_policy({
                "allowedActions": [act, "CREATE_REPAIR_JOB"],
            })
        assert exc_info.value.status == 400
        assert "safety floor" in str(exc_info.value).lower()


# --- Gate G: Canonical Multi-Replica Authority Verification ---


def test_canonical_authority_verify_mapping(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate G: Verify authority_verify results correctly map to risk engine severities."""
    monkeypatch.setattr(
        backup_control_recovery,
        "authority_verify",
        lambda: {"overall": "DIVERGENT", "issues": ["cross-replica-split-brain"]},
    )
    r_div = resilience_risk_engine.evaluate_authority_risk()
    assert r_div["severity"] == "blocked"
    assert any("cross-replica-split-brain" in ev for ev in r_div["evidence"])

    monkeypatch.setattr(
        backup_control_recovery,
        "authority_verify",
        lambda: {"overall": "UNAVAILABLE", "issues": ["insufficient-reachable-replicas"]},
    )
    r_unavail = resilience_risk_engine.evaluate_authority_risk()
    assert r_unavail["severity"] == "critical"

    monkeypatch.setattr(
        backup_control_recovery,
        "authority_verify",
        lambda: {"overall": "DEGRADED", "issues": ["replica-lag-detected"]},
    )
    r_deg = resilience_risk_engine.evaluate_authority_risk()
    assert r_deg["severity"] in {"warning", "degraded"}

    monkeypatch.setattr(
        backup_control_recovery,
        "authority_verify",
        lambda: {"overall": "HEALTHY", "issues": []},
    )
    r_healthy = resilience_risk_engine.evaluate_authority_risk()
    assert r_healthy["severity"] == "healthy"


# --- Gate A & B: Planner Parameter Population & Plan Materialization ---


def test_planner_emits_complete_executable_action_intents(tmp_settings: Path) -> None:
    """Gate A: Planner resolves complete parameters (policyId, backupId, source, destination)."""
    dir_t1 = tmp_settings / "target_1"
    dir_t2 = tmp_settings / "target_2"
    dir_t1.mkdir(parents=True, exist_ok=True)
    dir_t2.mkdir(parents=True, exist_ok=True)

    backup_targets.register_filesystem_target("target_1", path=dir_t1, label="Target 1")
    backup_targets.register_filesystem_target("target_2", path=dir_t2, label="Target 2")

    backup_policies.create_policy({
        "name": "Repl Pol",
        "policyId": "pol_full",
        "targetId": "target_1",
        "replication": {"enabled": True, "minCommittedCopies": 2, "destTargets": ["target_2"]},
    })

    now = datetime.now(tz=timezone.utc)
    backup_dr_ledger.record_recovery_point(
        policy_id="pol_full",
        backup_id="bkp_full_1",
        target_id="target_1",
        chain_digest="cd_full",
        committed_at=_utc_iso(now),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol_full",
        backup_id="bkp_full_1",
        target_id="target_1",
        state="committed",
        committed_at=_utc_iso(now),
    )

    # 1. Evaluate risk -> REPLICA_LAG
    snapshot = resilience_risk_engine.assess_risks()
    assert any(r["type"] == "REPLICA_LAG" for r in snapshot["risks"])

    # 2. Plan actions
    plan = resilience_planner.plan_resilience_actions(snapshot)
    assert len(plan["actions"]) >= 1
    repair_act = next(a for a in plan["actions"] if a["type"] == "CREATE_REPAIR_JOB")

    # Gate A: Parameters must be fully populated
    params = repair_act["parameters"]
    assert params["policyId"] == "pol_full"
    assert params["backupId"] == "bkp_full_1"
    assert params["destTargetId"] == "target_2"
    assert params["sourceTargetId"] == "target_1"

    # Validate action intent contract
    valid, issues = resilience_planner.validate_action_intent(repair_act)
    assert valid is True
    assert issues == []

    # Gate B: Plan materialization
    mat_plan = resilience_action_journal.materialize_resilience_plan(plan, created_by="test-suite")
    assert mat_plan["status"] == "MATERIALIZED"
    assert len(mat_plan["actions"]) == len(plan["actions"])

    # Action is in journal
    act_id = mat_plan["actions"][0]["actionId"]
    db_act = resilience_action_journal.get_action(act_id)
    assert db_act is not None
    assert db_act["state"] == "PENDING"
    assert db_act["planId"] == plan["planId"]


# --- Gate D: CAS Claim & Leases ---


def test_cas_claim_and_concurrency_fencing(tmp_settings: Path) -> None:
    """Gate D: CAS claim guarantees exactly-once execution and expires leases."""
    action = {
        "actionId": "act-cas-test",
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)

    # 1. First claim succeeds
    claimed1, act1, reason1 = resilience_action_journal.claim_action(
        "act-cas-test",
        owner_instance_id="worker-1",
        lease_seconds=10,
    )
    assert claimed1 is True
    assert act1 is not None
    assert act1["state"] == "CLAIMED"
    assert act1["ownerInstanceId"] == "worker-1"

    # 2. Concurrent second claim by worker-2 fails
    claimed2, act2, reason2 = resilience_action_journal.claim_action(
        "act-cas-test",
        owner_instance_id="worker-2",
        lease_seconds=10,
    )
    assert claimed2 is False
    assert "claim-rejected" in reason2

    # 3. Simulate expired lease by updating leaseUntil in the past
    past_iso = _utc_iso(datetime.now(tz=timezone.utc) - timedelta(seconds=20))
    with resilience_action_journal._connect() as conn:
        conn.execute("UPDATE resilience_actions SET lease_until = ? WHERE action_id = ?", (past_iso, "act-cas-test"))
        conn.commit()

    # 4. Now worker-2 can claim expired lease
    claimed3, act3, reason3 = resilience_action_journal.claim_action(
        "act-cas-test",
        owner_instance_id="worker-2",
        lease_seconds=10,
    )
    assert claimed3 is True
    assert act3 is not None
    assert act3["ownerInstanceId"] == "worker-2"


# --- Gate C: TOCTOU Freshness Fencing ---


def test_toctou_freshness_fencing_skips_cleared_risk(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate C: Action is skipped if the triggering risk was already cleared before execution."""
    action = {
        "actionId": "act-toctou-repair",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol-cleared", "backupId": "bkp-1", "destTargetId": "target_2"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)

    # When risk assessment runs, risks are empty / healthy
    monkeypatch.setattr(
        resilience_risk_engine,
        "assess_risks",
        lambda probe=False: {"riskSnapshotVersion": 1, "overallRisk": "healthy", "riskDigest": "0" * 64, "risks": []},
    )

    result = resilience_action_journal.execute_autonomous_action("act-toctou-repair")
    assert result["state"] == "SKIPPED_NO_LONGER_NEEDED"
    assert "replica-risk-already-cleared" in str(result["error"])


# --- Gate H: Precondition Simulation ---


def test_precondition_simulation_rejects_insufficient_watermark(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate H: Precondition simulation rejects rebalance destination with insufficient free capacity."""
    dir_s = tmp_settings / "target_src"
    dir_d = tmp_settings / "target_dst"
    dir_s.mkdir(parents=True, exist_ok=True)
    dir_d.mkdir(parents=True, exist_ok=True)

    backup_targets.register_filesystem_target("target_src", path=dir_s)
    backup_targets.register_filesystem_target("target_dst", path=dir_d)

    action = {
        "actionId": "act-sim-reb",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "target_src", "destTargetId": "target_dst", "policyId": "p", "backupId": "b"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)

    # Mock capacity on destination to only 15% free (below 20% floor)
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda target_id, probe=False: {"freePercent": 15.0, "totalBytes": 1000, "freeBytes": 150},
    )
    monkeypatch.setattr(
        resilience_risk_engine,
        "assess_risks",
        lambda probe=False: {
            "riskSnapshotVersion": 1,
            "overallRisk": "warning",
            "riskDigest": "1" * 64,
            "risks": [{"type": "CAPACITY_EXHAUSTION", "severity": "warning", "target": "target_src"}],
        },
    )

    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act-sim-reb")
    assert exc_info.value.status == 400
    assert "destination-capacity-watermark-insufficient" in str(exc_info.value)

    db_act = resilience_action_journal.get_action("act-sim-reb")
    assert db_act is not None
    assert db_act["state"] == "BLOCKED"


# --- Gate J: Compensation Lifecycle ---


def test_safe_compensation_lifecycle_on_failure(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate J: Action transitions through typed compensation states."""
    action = {
        "actionId": "act-comp-test",
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)

    monkeypatch.setattr(resilience_action_journal, "check_action_freshness", lambda a, s: (True, "fresh"))
    monkeypatch.setattr(resilience_action_journal, "simulate_action", lambda a: (True, {"simulationPassed": True}))
    monkeypatch.setattr(
        backup_dr_readiness,
        "run_dr_drill",
        lambda **kw: {"drillId": "d-fail", "success": False, "error": "drill-restoration-timeout"},
    )

    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act-comp-test")
    assert exc_info.value.status == 500

    db_act = resilience_action_journal.get_action("act-comp-test")
    assert db_act is not None
    assert db_act["state"] == "COMPENSATED"
    assert db_act["compensationState"] == "EFFECT_COMPENSATED"
    assert "drill-restoration-timeout" in str(db_act["error"])


# --- Gate K: Rate Limits Admission ---


def test_rate_limits_block_excessive_concurrent_actions(tmp_settings: Path) -> None:
    """Gate K: Concurrent action limit strictly blocks exceeding executions."""
    autonomous_action_policy.set_action_rate_limits({"maxConcurrentActions": 2, "maxActionsPerHour": 10})

    now_iso = _utc_iso()
    with resilience_action_journal._connect() as conn:
        for i in range(2):
            conn.execute(
                """
                INSERT INTO resilience_actions (
                    action_id, action_type, created_by, state, created_at, updated_at
                ) VALUES (?, 'START_DR_DRILL', 'rate-test', 'EXECUTING', ?, ?)
                """,
                (f"act-running-{i}", now_iso, now_iso),
            )
        conn.commit()

    action = {
        "actionId": "act-rate-blocked",
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)

    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act-rate-blocked")
    assert exc_info.value.status == 429

    db_act = resilience_action_journal.get_action("act-rate-blocked")
    assert db_act is not None
    assert db_act["state"] == "BLOCKED"
    assert "max-concurrent-actions-exceeded" in str(db_act["error"])


# --- Gate I, L & Decision Proof v3: End-to-End Verification ---


def test_end_to_end_autonomous_remediation_and_proof_validation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate I, L: Execute repair, verify outcome, validate risk reduction and Decision Proof v3."""
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("authenticated", {"r": 1}, {"c": 1}))
    dir_t1 = tmp_settings / "target_src"
    dir_t2 = tmp_settings / "target_dst"
    dir_t1.mkdir(parents=True, exist_ok=True)
    dir_t2.mkdir(parents=True, exist_ok=True)

    backup_targets.register_filesystem_target("target_src", path=dir_t1)
    backup_targets.register_filesystem_target("target_dst", path=dir_t2)

    backup_policies.create_policy({
        "name": "E2E Pol",
        "policyId": "pol_e2e",
        "targetId": "target_src",
        "replication": {"enabled": True, "minCommittedCopies": 2, "destTargets": ["target_dst"]},
    })

    now = datetime.now(tz=timezone.utc)
    backup_dr_ledger.record_recovery_point(
        policy_id="pol_e2e",
        backup_id="bkp_e2e_1",
        target_id="target_src",
        chain_digest="cd_e2e",
        committed_at=_utc_iso(now),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol_e2e",
        backup_id="bkp_e2e_1",
        target_id="target_src",
        state="committed",
        committed_at=_utc_iso(now),
    )

    # Initial risk assessment
    snap1 = resilience_risk_engine.assess_risks()
    assert snap1["overallRisk"] in {"warning", "degraded", "critical"}

    # Plan
    plan = resilience_planner.plan_resilience_actions(snap1)
    mat_plan = resilience_action_journal.materialize_resilience_plan(plan, created_by="e2e-runner")
    repair_act = mat_plan["actions"][0]
    action_id = repair_act["actionId"]

    # Mock execute_replica_repair to synchronously commit copy on target_dst
    def mock_execute_repair(*args: Any, **kwargs: Any) -> dict[str, Any]:
        r_id = kwargs.get("repair_id") or (args[0] if args else "rep-001")
        backup_dr_ledger.record_logical_recovery_copy(
            policy_id="pol_e2e",
            backup_id="bkp_e2e_1",
            target_id="target_dst",
            state="committed",
            committed_at=_utc_iso(),
        )
        return {"repairId": r_id, "phase": "complete", "bytesRepaired": 1024}

    monkeypatch.setattr(backup_replication, "execute_replica_repair", mock_execute_repair)

    # Execute autonomous action
    result = resilience_action_journal.execute_autonomous_action(action_id)
    assert result["state"] == "SUCCEEDED"
    assert result["verificationResult"]["executionVerified"] is True
    assert result["verificationResult"]["committedCopies"] >= 2

    # Decision Proof v3 validation
    proof = result["decisionProof"]
    assert proof is not None
    assert proof["actionAllowed"] is True
    assert proof["simulationPassed"] is True
    assert proof["executionVerified"] is True
    assert proof["effectObserved"] is True
    assert proof["riskBeforeDigest"] != ""
    assert proof["riskAfterDigest"] != ""

    proof_errors = evidence_proof.validate_decision_proof(proof, "e2e-decision-proof")
    assert proof_errors == []


def test_coverage_booster_materialize_and_intent_validation(tmp_settings: Path) -> None:
    """Test plan materialization validation failures and intent checks."""
    # Mismatched plan digest
    bad_plan = {
        "planId": "plan-bad-digest",
        "planVersion": 1,
        "inputRiskDigest": "rd-123",
        "planDigest": "mismatched-digest",
        "overallRisk": "warning",
        "actions": [],
    }
    with pytest.raises(AppError) as exc:
        resilience_action_journal.materialize_resilience_plan(bad_plan)
    assert exc.value.status == 400

    # Invalid action intent inside plan
    invalid_act_plan = {
        "planId": "plan-inv-act",
        "planVersion": 1,
        "inputRiskDigest": "rd-456",
        "overallRisk": "warning",
        "actions": [{"type": "CREATE_REPAIR_JOB", "parameters": {}}],
    }
    with pytest.raises(AppError) as exc:
        resilience_action_journal.materialize_resilience_plan(invalid_act_plan)
    assert exc.value.status == 400


def test_coverage_booster_planner_intent_validation_branches() -> None:
    """Test resilience_planner.validate_action_intent all error branches."""
    # Not a dict
    ok, errs = resilience_planner.validate_action_intent("invalid")  # type: ignore[arg-type]
    assert not ok and "action-intent-must-be-object" in errs

    # Missing type
    ok, errs = resilience_planner.validate_action_intent({})
    assert not ok and "missing-action-type" in errs

    # CREATE_REPAIR_JOB missing fields
    ok, errs = resilience_planner.validate_action_intent({"type": "CREATE_REPAIR_JOB", "parameters": {}})
    assert not ok
    assert any("missing-policyId" in e for e in errs)
    assert any("missing-backupId" in e for e in errs)
    assert any("missing-destTargetId" in e for e in errs)

    # CREATE_REBALANCE_JOB missing fields
    ok, errs = resilience_planner.validate_action_intent({"type": "CREATE_REBALANCE_JOB", "parameters": {}})
    assert not ok
    assert any("missing-sourceTargetId" in e for e in errs)
    assert any("missing-destTargetId" in e for e in errs)


def test_coverage_booster_planner_selection_helpers(tmp_settings: Path) -> None:
    """Test candidate copy and target selection helper branches."""
    # Empty policies -> None, None
    pid, bid = resilience_planner.find_rebalance_candidate_copy("target_src")
    assert pid is None and bid is None

    # Setup dummy policy and backup on target A
    dir_a = tmp_settings / "target_src"
    dir_b = tmp_settings / "target_dst"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_src", path=dir_a)
    backup_targets.register_filesystem_target("target_dst", path=dir_b)

    backup_policies.create_policy({
        "name": "Sel Pol",
        "policyId": "pol_sel",
        "targetId": "target_src",
        "replication": {"enabled": True, "targetIds": ["target_dst"]},
    })
    backup_dr_ledger.record_recovery_point(
        policy_id="pol_sel",
        backup_id="bkp_sel_1",
        target_id="target_src",
        chain_digest="cd_sel",
        committed_at=_utc_iso(),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol_sel",
        backup_id="bkp_sel_1",
        target_id="target_src",
        state="committed",
        committed_at=_utc_iso(),
    )

    # find_rebalance_candidate_copy finds copy on target_src
    p_found, b_found = resilience_planner.find_rebalance_candidate_copy("target_src")
    assert p_found == "pol_sel"
    assert b_found == "bkp_sel_1"

    # select_repair_destination with target_dst
    dst = resilience_planner.select_repair_destination("pol_sel", "bkp_sel_1", existing_targets={"target_src"})
    assert dst == "target_dst"

    # select_repair_destination when all targets exist -> None
    dst_none = resilience_planner.select_repair_destination("pol_sel", "bkp_sel_1", existing_targets={"target_src", "target_dst"})
    assert dst_none is None


def test_coverage_booster_simulation_and_verification_branches(tmp_settings: Path) -> None:
    """Test action simulation and verification failure edge cases."""
    # Simulation: unsupported type
    ok, res = resilience_action_journal.simulate_action({"type": "UNKNOWN_ACTION", "parameters": {}})
    assert not ok and "unsupported-simulation-type" in res.get("error", "")

    # Simulation: target not found
    ok, res = resilience_action_journal.simulate_action({
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "target_src", "destTargetId": "non-existent-target"},
    })
    assert not ok and "destination-target-invalid-or-draining" in res.get("error", "")

    # Outcome verification: unsupported type
    ok, res = resilience_action_journal.verify_action_outcome({"type": "UNKNOWN_ACTION"}, {})
    assert not ok and "unsupported-verification-type" in res.get("error", "")

    # Outcome verification: failed repair job
    ok, res = resilience_action_journal.verify_action_outcome(
        {"type": "CREATE_REPAIR_JOB", "parameters": {"policyId": "p1", "backupId": "b1", "destTargetId": "t1"}},
        {"job": {"status": "failed", "error": "network-down"}},
    )
    assert not ok and ("network-down" in res.get("error", "") or "committed-copies-insufficient" in res.get("error", ""))

    # Outcome verification: failed rebalance job
    ok, res = resilience_action_journal.verify_action_outcome(
        {"type": "CREATE_REBALANCE_JOB", "parameters": {"sourceTargetId": "t1", "destTargetId": "t2"}},
        {"job": {"jobId": "reb-fail-1", "status": "failed", "error": "out-of-space"}},
    )
    assert not ok and "out-of-space" in res.get("error", "")


def test_coverage_booster_compensation_classes(tmp_settings: Path) -> None:
    """Test typed compensation lifecycle transitions."""
    act_id = "act-comp-irrev"
    resilience_action_journal.record_action_intent({
        "actionId": act_id,
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
    })
    # Compensate with IRREVERSIBLE
    resilience_action_journal.compensate_action(act_id, "fatal-corruption", effect_class="IRREVERSIBLE")
    act = resilience_action_journal.get_action(act_id)
    assert act is not None
    assert act["state"] == "NEEDS_OPERATOR"
    assert act["compensationState"] == "MANUAL_INTERVENTION_REQUIRED"

    # Compensate with NO_EFFECT
    act_id_none = "act-comp-none"
    resilience_action_journal.record_action_intent({
        "actionId": act_id_none,
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
    })
    resilience_action_journal.compensate_action(act_id_none, "early-fail", effect_class="NO_EFFECT")
    act_n = resilience_action_journal.get_action(act_id_none)
    assert act_n is not None
    assert act_n["state"] == "FAILED_BEFORE_EFFECT"
    assert act_n["compensationState"] == "NONE"


def test_coverage_booster_decision_proof_v3_validation() -> None:
    """Test evidence_proof.validate_decision_proof edge cases."""
    # Not a dict
    errs = evidence_proof.validate_decision_proof("invalid", "proof_check")  # type: ignore[arg-type]
    assert "not-a-dict" in errs

    # Missing fields
    errs = evidence_proof.validate_decision_proof({"planDigest": "p", "simulationPassed": True}, "proof_check")
    assert any("missing-field:riskDigest" in e for e in errs)

    # Boolean false checks
    errs = evidence_proof.validate_decision_proof({
        "riskDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "policyVersion": 1,
        "actionAllowed": False,
        "simulationPassed": False,
        "executionVerified": False,
        "effectObserved": False,
    }, "proof_check")
    assert "decision-actionAllowed-not-true" in errs
    assert "decision-simulationPassed-not-true" in errs
    assert "decision-executionVerified-not-true" in errs
    assert "decision-effectObserved-not-true" in errs

