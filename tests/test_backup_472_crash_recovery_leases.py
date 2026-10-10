"""current-release Gate A, B, C, I, L - Immutable Action Identity, CAS Fencing, Crash Recovery & Compensation Test Suite.

Validates:
1. Immutable Create-Once Plan & Action Identity (Gate A).
2. Exactly-Once Crash-Recoverable CAS Fencing & Epochs (Gate B).
3. Expired Lease Reclaim by New Worker (Gate B).
4. Stale Worker Fence Out with 409 on Outdated Epoch (Gate B).
5. Effect Reconciliation across In-Flight Subsystems (Gate C).
6. Transactional Resource Locks (Gate I).
7. Effect-Aware Compensation Lifecycle (Gate L).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_replication,
    resilience_action_journal,
    resilience_effect_reconciler,
    resilience_planner,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_immutable_create_once_plan_and_action_identity(tmp_settings: Path) -> None:
    """Test Gate A: Create-once semantics for Plans and Actions."""
    plan_id = "plan_immutable_1"
    act_id = "act_immutable_1"
    raw_plan = {
        "planId": plan_id,
        "planVersion": 1,
        "inputRiskDigest": "rd_imm_1",
        "overallRisk": "warning",
        "actions": [
            {
                "actionId": act_id,
                "type": "START_DR_DRILL",
                "parameters": {"targetId": "target_dr"},
                "requiresApproval": False,
            }
        ],
    }
    raw_plan["planDigest"] = resilience_planner.compute_plan_digest(raw_plan)

    # 1. Initial materialization
    p1 = resilience_action_journal.materialize_resilience_plan(raw_plan)
    assert p1["planId"] == plan_id
    assert p1["actions"][0]["state"] == "PENDING"

    # Advance action to SUCCEEDED
    resilience_action_journal.update_action_state(act_id, "SUCCEEDED")
    act_succeeded = resilience_action_journal.get_action(act_id)
    assert act_succeeded is not None and act_succeeded["state"] == "SUCCEEDED"

    # 2. Re-materializing identical plan must NOT overwrite or reset action to PENDING
    p2 = resilience_action_journal.materialize_resilience_plan(raw_plan)
    assert p2["planId"] == plan_id
    act_still_succeeded = resilience_action_journal.get_action(act_id)
    assert act_still_succeeded is not None and act_still_succeeded["state"] == "SUCCEEDED"

    # 3. Materializing a modified plan with the same plan_id must raise 409 conflict
    mutated_plan = dict(raw_plan)
    mutated_plan["inputRiskDigest"] = "rd_mutated_digest"
    mutated_plan["planDigest"] = resilience_planner.compute_plan_digest(mutated_plan)
    with pytest.raises(AppError) as exc_plan:
        resilience_action_journal.materialize_resilience_plan(mutated_plan)
    assert exc_plan.value.status == 409
    assert "Plan identity conflict" in str(exc_plan.value)

    # 4. Individual action intent conflict
    with pytest.raises(AppError) as exc_act:
        resilience_action_journal.record_action_intent({
            "actionId": act_id,
            "type": "START_DR_DRILL",
            "parameters": {"targetId": "different_target"},
        })
    assert exc_act.value.status == 409
    assert "Action identity conflict" in str(exc_act.value)


def test_cas_lease_fencing_and_expired_lease_reclaim(tmp_settings: Path) -> None:
    """Test Gate B: CAS claim, epoch fencing, expired lease takeover and stale worker rejection."""
    act_id = "act_cas_test_1"
    resilience_action_journal.record_action_intent({
        "actionId": act_id,
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "target_cas"},
    })

    # Worker 1 claims action
    now = datetime.now(tz=timezone.utc)
    claimed, act_w1, _ = resilience_action_journal.claim_action(act_id, owner_instance_id="worker-1", lease_seconds=60, now=now)
    assert claimed is True
    assert act_w1 is not None
    assert act_w1["executionEpoch"] == 1
    w1_token = act_w1["claimToken"]
    assert w1_token is not None

    # Worker 1 can make valid CAS state transition
    resilience_action_journal.update_action_state(
        act_id,
        "EXECUTING",
        execution_epoch=1,
        claim_token=w1_token,
    )
    act_exec = resilience_action_journal.get_action(act_id)
    assert act_exec is not None and act_exec["state"] == "EXECUTING"

    # Simulate Worker 1 crashing and lease expiring (e.g. 10 minutes later)
    past_now = now + timedelta(seconds=600)

    # Worker 2 reclaims expired action
    claimed_w2, act_w2, _ = resilience_action_journal.claim_action(act_id, owner_instance_id="worker-2", lease_seconds=60, now=past_now)
    assert claimed_w2 is True
    assert act_w2 is not None
    assert act_w2["executionEpoch"] == 2
    w2_token = act_w2["claimToken"]
    assert w2_token != w1_token

    # Stale Worker 1 wakes up and tries to commit with epoch 1 and old token -> FENCED OUT (409)!
    with pytest.raises(AppError) as exc_stale:
        resilience_action_journal.update_action_state(
            act_id,
            "SUCCEEDED",
            execution_epoch=1,
            claim_token=w1_token,
        )
    assert exc_stale.value.status == 409
    assert "lease lost" in str(exc_stale.value)

    # Valid Worker 2 commits with epoch 2 and new token -> SUCCEEDS!
    resilience_action_journal.update_action_state(
        act_id,
        "SUCCEEDED",
        execution_epoch=2,
        claim_token=w2_token,
    )
    act_final = resilience_action_journal.get_action(act_id)
    assert act_final is not None and act_final["state"] == "SUCCEEDED"


def test_effect_reconciliation_engine(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test Gate C: Inspecting effectHandle to reconcile crashed worker states."""
    # 1. Reconcile CLAIMED action -> RESUME_SIMULATING
    claimed_act = {"actionId": "act_rec_1", "state": "CLAIMED", "type": "CREATE_REPAIR_JOB", "parameters": {}}
    status, _ = resilience_effect_reconciler.reconcile_action_effect(claimed_act)
    assert status == "RESUME_SIMULATING"

    # 2. Reconcile Completed Repair Job -> ADVANCE_TO_VERIFYING
    monkeypatch.setattr(backup_replication, "read_repair_job", lambda r_id: {"repairId": r_id, "phase": "complete"})
    executing_repair = {
        "actionId": "act_rec_2",
        "state": "EXECUTING",
        "type": "CREATE_REPAIR_JOB",
        "effectHandle": {"repairId": "rep_completed"},
        "parameters": {},
    }
    status_rep, details_rep = resilience_effect_reconciler.reconcile_action_effect(executing_repair)
    assert status_rep == "ADVANCE_TO_VERIFYING"
    assert details_rep["repairId"] == "rep_completed"

    # 3. Reconcile Active Rebalance Job -> RESUME_EXECUTION
    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda j_id: {"jobId": j_id, "phase": "transferring"})
    executing_reb = {
        "actionId": "act_rec_3",
        "state": "EXECUTING",
        "type": "CREATE_REBALANCE_JOB",
        "effectHandle": {"jobId": "reb_in_flight"},
        "parameters": {},
    }
    status_reb, _ = resilience_effect_reconciler.reconcile_action_effect(executing_reb)
    assert status_reb == "RESUME_EXECUTION"

    # 4. Reconcile Failed Subsystem -> TRIGGER_COMPENSATION
    monkeypatch.setattr(backup_replication, "read_repair_job", lambda r_id: {"repairId": r_id, "phase": "failed", "error": "disk-io-error"})
    failed_repair = {
        "actionId": "act_rec_4",
        "state": "EXECUTING",
        "type": "CREATE_REPAIR_JOB",
        "effectHandle": {"repairId": "rep_failed"},
        "parameters": {},
    }
    status_fail, details_fail = resilience_effect_reconciler.reconcile_action_effect(failed_repair)
    assert status_fail == "TRIGGER_COMPENSATION"
    assert "disk-io-error" in details_fail["error"]

    # 5. Reconcile Repair Job not found -> RECREATE_EFFECT
    monkeypatch.setattr(backup_replication, "read_repair_job", lambda r_id: None)
    monkeypatch.setattr(backup_replication, "list_repair_jobs", lambda: [])
    status_notfound, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_rec_missing", "state": "EXECUTING", "type": "CREATE_REPAIR_JOB", "parameters": {}
    })
    assert status_notfound == "RECREATE_EFFECT"

    # 6. Reconcile Repair Job found via list_repair_jobs
    monkeypatch.setattr(backup_replication, "list_repair_jobs", lambda: [{"resilienceActionId": "act_found_list", "phase": "complete"}])
    status_found, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_found_list", "state": "EXECUTING", "type": "CREATE_REPAIR_JOB", "parameters": {}
    })
    assert status_found == "ADVANCE_TO_VERIFYING"

    # 7. Reconcile Rebalance Job completed / failed / not found
    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda j_id: {"jobId": j_id, "phase": "complete"})
    status_reb_done, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_reb_done", "state": "EXECUTING", "type": "CREATE_REBALANCE_JOB", "effectHandle": {"jobId": "j1"}, "parameters": {}
    })
    assert status_reb_done == "ADVANCE_TO_VERIFYING"

    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda j_id: {"jobId": j_id, "phase": "failed", "error": "net-fail"})
    status_reb_fail, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_reb_fail", "state": "EXECUTING", "type": "CREATE_REBALANCE_JOB", "effectHandle": {"jobId": "j2"}, "parameters": {}
    })
    assert status_reb_fail == "TRIGGER_COMPENSATION"

    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda j_id: None)
    monkeypatch.setattr(backup_replication, "list_rebalance_jobs", lambda: [])
    status_reb_missing, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_reb_missing", "state": "EXECUTING", "type": "CREATE_REBALANCE_JOB", "parameters": {}
    })
    assert status_reb_missing == "RECREATE_EFFECT"

    monkeypatch.setattr(backup_replication, "list_rebalance_jobs", lambda: [{"resilienceActionId": "act_reb_inlist", "phase": "complete"}])
    status_reb_inlist, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_reb_inlist", "state": "EXECUTING", "type": "CREATE_REBALANCE_JOB", "parameters": {}
    })
    assert status_reb_inlist == "ADVANCE_TO_VERIFYING"

    # 8. Reconcile DR Drill records
    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda: [{"resilienceActionId": "act_drill_succ", "result": "success"}])
    status_drill_ok, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_drill_succ", "state": "EXECUTING", "type": "START_DR_DRILL", "parameters": {}
    })
    assert status_drill_ok == "ADVANCE_TO_VERIFYING"

    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda: [{"resilienceActionId": "act_drill_bad", "result": "failed", "error": "dr-corrupt"}])
    status_drill_bad, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_drill_bad", "state": "EXECUTING", "type": "START_DR_DRILL", "parameters": {}
    })
    assert status_drill_bad == "TRIGGER_COMPENSATION"

    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda: [])
    status_drill_none, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_drill_none", "state": "EXECUTING", "type": "START_DR_DRILL", "parameters": {}
    })
    assert status_drill_none == "RECREATE_EFFECT"

    # 9. Reconcile unknown action type and exception handler
    status_unk, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_unk", "state": "EXECUTING", "type": "NON_EXISTENT_TYPE", "parameters": {}
    })
    assert status_unk == "EFFECT_UNKNOWN"

    def broken_read(*a, **k):
        raise RuntimeError("db connection dropped")
    monkeypatch.setattr(backup_replication, "read_repair_job", broken_read)
    status_exc, _ = resilience_effect_reconciler.reconcile_action_effect({
        "actionId": "act_exc", "state": "EXECUTING", "type": "CREATE_REPAIR_JOB", "effectHandle": {"repairId": "r_err"}, "parameters": {}
    })
    assert status_exc == "EFFECT_UNKNOWN"



def test_transactional_resource_locks(tmp_settings: Path) -> None:
    """Test Gate I: Resource locks prevent concurrent conflicting action claims."""
    resilience_action_journal.record_action_intent({
        "actionId": "act_lock_1",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "pol_lock", "backupId": "bkp_lock", "destTargetId": "target_dst_lock"},
    })
    resilience_action_journal.record_action_intent({
        "actionId": "act_lock_2",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"policyId": "pol_lock", "backupId": "bkp_lock", "sourceTargetId": "target_src_lock", "destTargetId": "target_dst_lock"},
    })

    # Worker 1 claims action 1 -> acquires locks on backup:pol_lock:bkp_lock and target:target_dst_lock
    ok1, _, _ = resilience_action_journal.claim_action("act_lock_1")
    assert ok1 is True

    # Worker 2 tries to claim action 2 (competing on same backup/target) -> rejected by lock!
    ok2, _, reason2 = resilience_action_journal.claim_action("act_lock_2")
    assert ok2 is False
    assert "resource-locked" in reason2

    # Action 1 reaches SUCCEEDED terminal state -> releases locks
    resilience_action_journal.update_action_state("act_lock_1", "SUCCEEDED")

    # Now Worker 2 can claim action 2!
    ok2_after, _, _ = resilience_action_journal.claim_action("act_lock_2")
    assert ok2_after is True


def test_typed_compensation_transitions(tmp_settings: Path) -> None:
    """Test Gate L: Typed compensation states and transitions."""
    act_id = "act_comp_1"
    resilience_action_journal.record_action_intent({
        "actionId": act_id,
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "target_src", "destTargetId": "target_dst"},
    })

    # NO_EFFECT -> FAILED_BEFORE_EFFECT
    res = resilience_action_journal.compensate_action(act_id, "Simulation failed", effect_class="NO_EFFECT")
    assert res["state"] == "FAILED_BEFORE_EFFECT"
    assert res["compensationState"] == "NONE"

    # CANCELABLE without a durable job handle -> EFFECT_UNKNOWN
    res2 = resilience_action_journal.compensate_action(act_id, "Rebalance cancelled", effect_class="CANCELABLE")
    assert res2["state"] == "EFFECT_UNKNOWN"
    assert res2["compensationState"] == "REMOTE_EFFECT_UNCERTAIN"

    # EFFECT_UNKNOWN -> EFFECT_UNKNOWN
    res3 = resilience_action_journal.compensate_action(act_id, "Connection timed out", effect_class="EFFECT_UNKNOWN")
    assert res3["state"] == "EFFECT_UNKNOWN"
    assert res3["compensationState"] == "REMOTE_EFFECT_UNCERTAIN"

    # IRREVERSIBLE -> NEEDS_OPERATOR
    res4 = resilience_action_journal.compensate_action(act_id, "Irreversible storage error", effect_class="IRREVERSIBLE")
    assert res4["state"] == "NEEDS_OPERATOR"
    assert res4["compensationState"] == "MANUAL_INTERVENTION_REQUIRED"
