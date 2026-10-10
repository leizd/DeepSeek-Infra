"""current-release Gate E, F, D - Real Outcome Contracts, Scoped Risk Reduction & Drill Idempotency Test Suite.

Validates:
1. Real Outcome Verification for Repairs (Receipt/Commit, Failure Domains, Min Copies) (Gate E).
2. Real Outcome Verification for Rebalances (Execution to Completion, Destination Authenticated) (Gate E).
3. Scoped Risk Reduction on exact riskSubject (Gate F).
4. Subsystem Idempotency on DR drills with resilienceActionId (Gate D).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_replication,
    backup_targets,
    resilience_outcome_verifier,
)
from deepseek_infra.infra.workspace.resilience_risk_engine import RiskSeverity, RiskType


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_repair_outcome_verification_contracts(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test Gate E: Real repair outcome contracts (copies count, failure domain separation, auth)."""
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)
    t1 = tmp_settings / "target_r1"
    t2 = tmp_settings / "target_r2"
    t1.mkdir(parents=True, exist_ok=True)
    t2.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_r1", path=t1, failure_domain="zone-a")
    backup_targets.register_filesystem_target("target_r2", path=t2, failure_domain="zone-b")

    policy_id = "pol_rep_outcome"
    backup_policies.create_policy({
        "name": "Repair Policy",
        "policyId": policy_id,
        "targetId": "target_r1",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "destTargets": ["target_r2"],
            "minFailureDomains": 2,
        },
    })

    action = {
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": policy_id, "backupId": "b1", "destTargetId": "target_r2"},
    }

    # 1. No committed copies in ledger -> Fails verification
    ok1, details1 = resilience_outcome_verifier.verify_action_outcome(action, {"repairId": "rep_1"})
    assert ok1 is False
    assert "committed-copies-insufficient" in details1["error"]

    # Record only 1 committed copy (requires 2)
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id=policy_id,
        backup_id="b1",
        target_id="target_r1",
        state="committed",
        committed_at=_utc_iso(),
    )
    ok2, details2 = resilience_outcome_verifier.verify_action_outcome(action, {"repairId": "rep_1"})
    assert ok2 is False
    assert "committed-copies-insufficient:1<2" in details2["error"]

    # Record second copy but in same failure domain (simulate same target or same zone)
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id=policy_id,
        backup_id="b1",
        target_id="target_r1",  # duplicate target
        state="committed",
        committed_at=_utc_iso(),
    )

    # Mock authenticate_committed_copy to return unauthenticated
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("corrupt", None, None))
    # Record copy on target_r2
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id=policy_id,
        backup_id="b1",
        target_id="target_r2",
        state="committed",
        committed_at=_utc_iso(),
    )
    ok3, details3 = resilience_outcome_verifier.verify_action_outcome(action, {"repairId": "rep_1"})
    assert ok3 is False
    assert "repair-destination-authentication-failed:corrupt" in details3["error"]

    # Mock authenticate_committed_copy to succeed
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("authenticated", {"receipt": 1}, {"commit": 1}))
    ok4, details4 = resilience_outcome_verifier.verify_action_outcome(action, {"repairId": "rep_1"})
    assert ok4 is True
    assert details4["executionVerified"] is True
    assert details4["committedCopies"] >= 2
    assert len(details4["failureDomains"]) >= 2


def test_rebalance_outcome_verification_contracts(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test Gate E: Real rebalance outcome contracts."""
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *a, **k: None)
    ts = tmp_settings / "target_s"
    td = tmp_settings / "target_d"
    ts.mkdir(parents=True, exist_ok=True)
    td.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_s", path=ts)
    backup_targets.register_filesystem_target("target_d", path=td)

    action = {
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "target_s", "destTargetId": "target_d", "policyId": "p1", "backupId": "b1"},
    }

    # 1. Rebalance job failed -> Fails verification
    ok1, details1 = resilience_outcome_verifier.verify_action_outcome(action, {"job": {"jobId": "reb_1", "phase": "failed", "error": "target-full"}})
    assert ok1 is False
    assert "target-full" in details1["error"]

    # 2. Rebalance job complete, but destination copy not authenticated -> Fails verification
    monkeypatch.setattr(backup_replication, "read_rebalance_job", lambda j_id: {"jobId": j_id, "phase": "complete"})
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("missing_receipt", None, None))
    ok2, details2 = resilience_outcome_verifier.verify_action_outcome(action, {"jobId": "reb_2"})
    assert ok2 is False
    assert "rebalance-destination-not-authenticated" in details2["error"]

    # 3. Rebalance complete and authenticated -> Succeeds
    monkeypatch.setattr(backup_replication, "authenticate_committed_copy", lambda *a, **k: ("authenticated", {"r": 1}, {"c": 1}))
    ok3, details3 = resilience_outcome_verifier.verify_action_outcome(action, {"jobId": "reb_3"})
    assert ok3 is True
    assert details3["executionVerified"] is True
    assert details3["rebalanceJobId"] == "reb_3"


def test_scoped_risk_reduction_verification(tmp_settings: Path) -> None:
    """Test Gate F: Scoped risk reduction verification on exact subject."""
    action = {
        "type": "CREATE_REPAIR_JOB",
        "riskSubject": {
            "type": RiskType.REPLICA_LAG.value,
            "policyId": "pol_scope_1",
            "backupId": "bkp_scope_1",
        },
        "severityBefore": "critical",
    }

    risk_before_snapshot = {
        "risks": [
            {
                "type": RiskType.REPLICA_LAG.value,
                "policyId": "pol_scope_1",
                "backupId": "bkp_scope_1",
                "severity": RiskSeverity.CRITICAL.value,
            },
            {
                "type": RiskType.CAPACITY_EXHAUSTION.value,
                "target": "target_other",
                "severity": RiskSeverity.WARNING.value,
            },
        ]
    }

    # Case 1: Target risk cleared completely -> Effect Observed
    risk_after_cleared = {
        "risks": [
            {
                "type": RiskType.CAPACITY_EXHAUSTION.value,
                "target": "target_other",
                "severity": RiskSeverity.WARNING.value,
            },
        ]
    }
    ok1, details1 = resilience_outcome_verifier.verify_scoped_risk_reduction(action, risk_before_snapshot, risk_after_cleared)
    assert ok1 is True
    assert details1["effectObserved"] is True
    assert details1["severityAfter"] == "healthy"

    # Case 2: Target risk reduced from critical to warning -> Effect Observed
    risk_after_reduced = {
        "risks": [
            {
                "type": RiskType.REPLICA_LAG.value,
                "policyId": "pol_scope_1",
                "backupId": "bkp_scope_1",
                "severity": RiskSeverity.WARNING.value,
            },
        ]
    }
    ok2, details2 = resilience_outcome_verifier.verify_scoped_risk_reduction(action, risk_before_snapshot, risk_after_reduced)
    assert ok2 is True
    assert details2["effectObserved"] is True
    assert details2["severityBefore"] == "critical"
    assert details2["severityAfter"] == "warning"

    # Case 3: Target risk unchanged (still critical) -> Fails closed (effectObserved = False)
    risk_after_unchanged = {
        "risks": [
            {
                "type": RiskType.REPLICA_LAG.value,
                "policyId": "pol_scope_1",
                "backupId": "bkp_scope_1",
                "severity": RiskSeverity.CRITICAL.value,
            },
        ]
    }
    ok3, details3 = resilience_outcome_verifier.verify_scoped_risk_reduction(action, risk_before_snapshot, risk_after_unchanged)
    assert ok3 is False
    assert details3["effectObserved"] is False
    assert "target-risk-not-improved" in details3["reason"]


def test_dr_drill_resilience_action_idempotency(tmp_settings: Path) -> None:
    """Test Gate D: DR drill exact resilienceActionId deduplication."""
    action_id = "act_dr_drill_exact_idemp_1"

    # 1. Run drill with resilienceActionId
    res1 = backup_dr_readiness.run_dr_drill(target_id="managed-local", resilience_action_id=action_id)
    assert res1["status"] == "success"
    drill_id_1 = res1["drillId"]
    assert res1["resilienceActionId"] == action_id
    assert res1["proof"]["resilienceActionId"] == action_id

    # 2. Re-run drill with identical resilienceActionId -> must return existing drill record without running a new drill
    res2 = backup_dr_readiness.run_dr_drill(target_id="managed-local", resilience_action_id=action_id)
    assert res2["status"] == "success"
    assert res2["drillId"] == drill_id_1
    assert res2["resilienceActionId"] == action_id
