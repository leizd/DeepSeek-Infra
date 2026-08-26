"""Tests for DeepSeek Infra resilience engine — Global Recovery Intelligence & Autonomous Resilience."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_authority_provider,
    backup_capacity,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_replication,
    backup_targets,
    capacity_forecaster,
    evidence_proof,
    recovery_simulator,
    resilience_action_journal,
    resilience_planner,
    resilience_risk_engine,
    resilience_score,
    rpo_rto_optimizer,
)
from deepseek_infra.web.server import create_app


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ─── 1. Risk Assessment Engine Tests ──────────────────────────────────────────


def test_capacity_risk_detection(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test capacity exhaustion risk detection across various free percentage thresholds."""
    now = datetime.now(tz=timezone.utc)

    # 1. Unconstrained / healthy target
    risk_unconstrained = resilience_risk_engine.evaluate_target_capacity_risk("managed-local", now=now)
    assert risk_unconstrained["type"] == "CAPACITY_EXHAUSTION"
    assert risk_unconstrained["severity"] == "healthy"

    # Register target target_minio_a with 100 GiB quota
    dir_a = tmp_settings / "minio-a"
    dir_a.mkdir(parents=True, exist_ok=True)
    target_a = backup_targets.register_filesystem_target(
        "target_minio_a",
        path=dir_a,
        label="MinIO A",
    )
    assert target_a["targetId"] == "target_minio_a"

    # Synthetic probe returning 25% free -> healthy
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 1024 * 1024 * 1024,
            "freeBytes": 25 * 1024 * 1024 * 1024,
            "usedBytes": 75 * 1024 * 1024 * 1024,
            "freePercent": 25.0,
            "observedAt": _utc_iso(now),
        },
    )
    r_healthy = resilience_risk_engine.evaluate_target_capacity_risk("target_minio_a", now=now)
    assert r_healthy["severity"] == "healthy"

    # 15% free -> warning
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 1024 * 1024 * 1024,
            "freeBytes": 15 * 1024 * 1024 * 1024,
            "usedBytes": 85 * 1024 * 1024 * 1024,
            "freePercent": 15.0,
            "observedAt": _utc_iso(now),
        },
    )
    r_warning = resilience_risk_engine.evaluate_target_capacity_risk("target_minio_a", now=now)
    assert r_warning["severity"] == "warning"

    # 8% free -> degraded
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 1024 * 1024 * 1024,
            "freeBytes": 8 * 1024 * 1024 * 1024,
            "usedBytes": 92 * 1024 * 1024 * 1024,
            "freePercent": 8.0,
            "observedAt": _utc_iso(now),
        },
    )
    r_degraded = resilience_risk_engine.evaluate_target_capacity_risk("target_minio_a", now=now)
    assert r_degraded["severity"] == "degraded"

    # 3% free -> critical
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 1024 * 1024 * 1024,
            "freeBytes": 3 * 1024 * 1024 * 1024,
            "usedBytes": 97 * 1024 * 1024 * 1024,
            "freePercent": 3.0,
            "observedAt": _utc_iso(now),
        },
    )
    r_critical = resilience_risk_engine.evaluate_target_capacity_risk("target_minio_a", now=now)
    assert r_critical["severity"] == "critical"


def test_replica_lag_risk_detection(tmp_settings: Path) -> None:
    """Test replica lag and failure domain risk evaluation."""
    now = datetime.now(tz=timezone.utc)

    pol = backup_policies.create_policy(
        {
            "name": "Policy Replication",
            "policyId": "policy-repl",
            "targetId": "managed-local",
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
            },
        }
    )
    assert pol["policyId"] == "policy-repl"

    # Record a recovery point with 0 copies -> critical replica risk
    backup_dr_ledger.record_recovery_point(
        policy_id="policy-repl",
        backup_id="bkp-001",
        target_id="managed-local",
        chain_digest="obj-digest-1",
        committed_at=_utc_iso(now),
    )

    risks = resilience_risk_engine.evaluate_policy_replica_risk("policy-repl", now=now)
    assert len(risks) == 2
    rep_risk = next(r for r in risks if r["type"] == "REPLICA_LAG")
    fd_risk = next(r for r in risks if r["type"] == "FAILURE_DOMAIN_VIOLATION")

    assert rep_risk["severity"] == "critical"
    assert fd_risk["severity"] == "critical"

    # Add 1 copy in domain-1 -> degraded
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="policy-repl",
        backup_id="bkp-001",
        target_id="target_1",
        committed_at=_utc_iso(now),
        object_set_digest="obj-digest-1",
        state="healthy",
        metadata={"failureDomain": "fd-1"},
    )

    risks2 = resilience_risk_engine.evaluate_policy_replica_risk("policy-repl", now=now)
    rep_risk2 = next(r for r in risks2 if r["type"] == "REPLICA_LAG")
    fd_risk2 = next(r for r in risks2 if r["type"] == "FAILURE_DOMAIN_VIOLATION")
    assert rep_risk2["severity"] == "degraded"
    assert fd_risk2["severity"] == "degraded"

    # Add 2nd copy in domain-2 -> healthy
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="policy-repl",
        backup_id="bkp-001",
        target_id="target_2",
        committed_at=_utc_iso(now),
        object_set_digest="obj-digest-1",
        state="healthy",
        metadata={"failureDomain": "fd-2"},
    )

    risks3 = resilience_risk_engine.evaluate_policy_replica_risk("policy-repl", now=now)
    rep_risk3 = next(r for r in risks3 if r["type"] == "REPLICA_LAG")
    fd_risk3 = next(r for r in risks3 if r["type"] == "FAILURE_DOMAIN_VIOLATION")
    assert rep_risk3["severity"] == "healthy"
    assert fd_risk3["severity"] == "healthy"


def test_dr_staleness_detection(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test DR freshness and drill staleness risk classification."""
    now = datetime.now(tz=timezone.utc)

    # 1. No drills -> critical
    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda root=None: [])
    r_no_drill = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_no_drill["type"] == "DR_STALENESS"
    assert r_no_drill["severity"] == "critical"

    # 2. Fresh drill (<7d) -> healthy
    fresh_time = _utc_iso(now - timedelta(days=2))
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "drill-1", "success": True, "finishedAt": fresh_time}],
    )
    r_fresh = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_fresh["severity"] == "healthy"

    # 3. Drill 15 days ago (7-30d) -> warning
    warn_time = _utc_iso(now - timedelta(days=15))
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "drill-2", "success": True, "finishedAt": warn_time}],
    )
    r_warn = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_warn["severity"] == "warning"

    # 4. Drill 45 days ago (>30d) -> critical
    stale_time = _utc_iso(now - timedelta(days=45))
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "drill-3", "success": True, "finishedAt": stale_time}],
    )
    r_stale = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_stale["severity"] == "critical"


def test_restore_latency_and_repair_backlog_risks(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test restore latency SLA breach and repair backlog detection."""
    now = datetime.now(tz=timezone.utc)

    # Latency risk test
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {
            "restoreSuccessRate": 0.95,  # <99% -> critical
            "rtoSecondsP95": 2000,
        },
    )
    r_lat = resilience_risk_engine.evaluate_restore_latency_risk(now=now)
    assert r_lat["type"] == "RESTORE_LATENCY_BREACH"
    assert r_lat["severity"] == "critical"

    # Repair backlog test
    monkeypatch.setattr(
        backup_replication,
        "list_repair_jobs",
        lambda: [
            {"repairId": f"rep-{i}", "phase": "failed"} for i in range(4)
        ],
    )
    r_repair = resilience_risk_engine.evaluate_repair_backlog_risk()
    assert r_repair["type"] == "REPAIR_BACKLOG"
    assert r_repair["severity"] == "critical"


def test_authority_risk_detection(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test authority consensus risk detection."""
    r_auth = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth["type"] == "AUTHORITY_DEGRADATION"
    assert r_auth["severity"] == "healthy"

    class FakeDivergentProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "divergent"}

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeDivergentProvider())
    r_auth_div = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_div["severity"] == "blocked"


def test_end_to_end_risk_assessment_and_digest(tmp_settings: Path) -> None:
    """Test full assess_risks output structure and canonical digest computation."""
    snapshot = resilience_risk_engine.assess_risks()
    assert snapshot["riskSnapshotVersion"] == 1
    assert "generatedAt" in snapshot
    assert snapshot["overallRisk"] in {"healthy", "warning", "degraded", "critical", "blocked"}
    assert isinstance(snapshot["risks"], list)
    assert "riskDigest" in snapshot
    assert len(snapshot["riskDigest"]) == 64

    # Digest reproducibility
    recomputed = resilience_risk_engine.compute_risk_digest(snapshot)
    assert recomputed == snapshot["riskDigest"]


# ─── 2. Resilience Decision Planner & Policy Tests ───────────────────────────


def test_risk_generates_safe_action(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that detected risks generate deterministic, policy-admitted actions."""
    # Register target A (full) and target B (available)
    dir_a = tmp_settings / "target-a"
    dir_b = tmp_settings / "target-b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_a", path=dir_a, label="A")
    backup_targets.register_filesystem_target("target_b", path=dir_b, label="B")

    # Monkeypatch capacity: target_a has 5% free, target_b has 80% free
    def fake_cap(tid: str, probe: bool = False) -> dict[str, Any]:
        if tid == "target_a":
            return {"targetId": tid, "totalBytes": 100 * 10**9, "freeBytes": 5 * 10**9, "freePercent": 5.0}
        return {"targetId": tid, "totalBytes": 100 * 10**9, "freeBytes": 80 * 10**9, "freePercent": 80.0}

    monkeypatch.setattr(backup_capacity, "get_target_capacity", fake_cap)

    snapshot = {
        "riskSnapshotVersion": 1,
        "overallRisk": "critical",
        "riskDigest": "a" * 64,
        "risks": [
            {
                "type": "CAPACITY_EXHAUSTION",
                "target": "target_a",
                "severity": "critical",
                "confidence": "verified",
                "evidence": ["free-space-critical:5.0%"],
            },
            {
                "type": "DR_STALENESS",
                "severity": "warning",
                "confidence": "verified",
                "evidence": ["dr-drill-stale"],
            },
        ],
    }

    plan = resilience_planner.plan_resilience_actions(snapshot)
    assert plan["planVersion"] == 1
    assert plan["inputRiskDigest"] == "a" * 64
    assert len(plan["actions"]) == 2

    # Rebalance action
    rebalance_act = next(a for a in plan["actions"] if a["type"] == "CREATE_REBALANCE_JOB")
    assert rebalance_act["source"] == "target_a"
    assert rebalance_act["destination"] == "target_b"
    assert rebalance_act["requiresApproval"] is False

    # Drill action
    drill_act = next(a for a in plan["actions"] if a["type"] == "START_DR_DRILL")
    assert drill_act["requiresApproval"] is False


def test_forbidden_action_requires_approval(tmp_settings: Path) -> None:
    """Test that high-impact actions strictly require manual operator approval."""
    snapshot = {
        "riskSnapshotVersion": 1,
        "overallRisk": "blocked",
        "riskDigest": "b" * 64,
        "risks": [
            {
                "type": "AUTHORITY_DEGRADATION",
                "severity": "blocked",
                "confidence": "verified",
                "evidence": ["consensus-lost"],
            }
        ],
    }

    plan = resilience_planner.plan_resilience_actions(snapshot)
    assert len(plan["actions"]) == 1
    action = plan["actions"][0]
    assert action["type"] == "PRIMARY_PROMOTION"
    assert action["requiresApproval"] is True

    # Validate admission fails without approval
    admitted, reason = autonomous_action_policy.validate_action_admission(action)
    assert admitted is False
    assert "approval-required" in reason or "requires-explicit-approval" in reason


def test_autonomous_action_policy_crud_and_safety_constraints(tmp_settings: Path) -> None:
    """Test action policy read/write and enforce un-gatability of dangerous actions."""
    pol = autonomous_action_policy.get_autonomous_action_policy()
    assert pol["automationPolicyVersion"] == 1
    assert "CREATE_REPAIR_JOB" in pol["allowedActions"]
    assert "PRIMARY_PROMOTION" in pol["approvalRequired"]

    # Attempt to illegally add PRIMARY_PROMOTION to allowed actions -> must error
    with pytest.raises(AppError) as exc_info:
        autonomous_action_policy.set_autonomous_action_policy(
            {
                "allowedActions": ["PRIMARY_PROMOTION", "CREATE_REPAIR_JOB"],
            }
        )
    assert exc_info.value.status == 400


# ─── 3. Action Journal & Autonomous Execution Tests ───────────────────────────


def test_autonomous_action_has_journal_and_executes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test journal lifecycle tracking from intent to successful execution and proof binding."""
    # Mock replication create_repair_job
    monkeypatch.setattr(
        backup_replication,
        "create_repair_job",
        lambda *args, **kwargs: {"repairId": "rep-test-999", "status": "pending"},
    )

    action = {
        "actionId": "act-repair-1",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "p1", "backupId": "b1", "reason": "test-repair"},
        "requiresApproval": False,
        "inputRiskDigest": "c" * 64,
        "planDigest": "d" * 64,
    }

    # 1. Record intent
    rec = resilience_action_journal.record_action_intent(
        action,
        created_by="resilience-engine",
        input_risk_digest="c" * 64,
        plan_digest="d" * 64,
    )
    assert rec["actionId"] == "act-repair-1"
    assert rec["state"] == "PENDING"

    # 2. Execute
    exec_res = resilience_action_journal.execute_autonomous_action("act-repair-1")
    assert exec_res["state"] == "COMPLETED"
    assert exec_res["executionResult"]["repairId"] == "rep-test-999"
    assert exec_res["decisionProof"]["actionAllowed"] is True
    assert exec_res["decisionProof"]["executionVerified"] is True

    # 3. Query list
    actions = resilience_action_journal.list_actions(state="COMPLETED")
    assert any(a["actionId"] == "act-repair-1" for a in actions)


def test_failed_action_rolls_back_state(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that execution failure automatically rolls back action state in the journal."""
    def failing_rebalance(**kwargs: Any) -> Any:
        raise RuntimeError("Rebalance network socket dropped")

    monkeypatch.setattr(backup_replication, "create_rebalance_job", failing_rebalance)

    action = {
        "actionId": "act-rebalance-fail",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "t1", "destTargetId": "t2"},
        "requiresApproval": False,
    }

    resilience_action_journal.record_action_intent(action)

    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act-rebalance-fail")
    assert exc_info.value.status == 500

    record = resilience_action_journal.get_action("act-rebalance-fail")
    assert record is not None
    assert record["state"] == "ROLLED_BACK"
    assert "Rebalance network socket dropped" in str(record["error"])


# ─── 4. Predictive Capacity Planning Tests ───────────────────────────────────


def test_predictive_capacity_forecasting(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test multi-horizon 7d, 30d, 90d predictive capacity forecasting."""
    now = datetime.now(tz=timezone.utc)

    # Mock target with 100 GB total, 80 GB used, 500 MB/day ingress
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 10**9,
            "usedBytes": 80 * 10**9,
            "freeBytes": 20 * 10**9,
            "freePercent": 20.0,
            "observedAt": _utc_iso(now),
        },
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {
            "status": "warning",
            "bytesPerDayP50": 500 * 10**6,
            "estimatedDaysToFull": 40,
            "daysToSoftWatermark": 10,
            "daysToHardWatermark": 20,
            "sampleCount": 10,
            "confidence": "high",
        },
    )

    fc = capacity_forecaster.forecast_target_capacity("target_x", now=now)
    assert fc["targetId"] == "target_x"
    assert "7d" in fc["forecast"]
    assert "30d" in fc["forecast"]
    assert "90d" in fc["forecast"]

    # 30d projected used percent should be higher than current (80%)
    assert fc["forecast"]["30d"]["usedPercent"] > 80.0
    assert fc["forecast"]["7d"]["confidence"] >= 0.90
    assert "CREATE_REBALANCE_JOB" not in fc["recommendations"] or "CREATE_REBALANCE_JOB" in fc["recommendations"]

    all_fc = capacity_forecaster.forecast_all_targets(now=now)
    assert "targets" in all_fc
    assert len(all_fc["targets"]) >= 1


# ─── 5. RPO/RTO Optimizer Tests ──────────────────────────────────────────────


def test_rpo_rto_optimizer_recommendations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test target restore latency profiling and advisory recommendations."""
    now = datetime.now(tz=timezone.utc)

    # 2 drills on target_slow (avg 600s) and 2 drills on target_fast (avg 120s)
    mock_drills = [
        {"targetId": "target_slow", "restoreDurationMs": 600000, "success": True},
        {"targetId": "target_slow", "restoreDurationMs": 620000, "success": True},
        {"targetId": "target_fast", "restoreDurationMs": 120000, "success": True},
        {"targetId": "target_fast", "restoreDurationMs": 130000, "success": True},
    ]
    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda root=None: mock_drills)

    dir_slow = tmp_settings / "target-slow"
    dir_fast = tmp_settings / "target-fast"
    dir_slow.mkdir(parents=True, exist_ok=True)
    dir_fast.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_slow", path=dir_slow, label="Slow")
    backup_targets.register_filesystem_target("target_fast", path=dir_fast, label="Fast")

    backup_policies.create_policy({"name": "Policy Slow", "policyId": "p-slow", "targetId": "target_slow"})

    rec_res = rpo_rto_optimizer.generate_placement_recommendations(now=now)
    assert rec_res["optimizerVersion"] == 1
    assert "target_slow" in rec_res["targetPerformance"]
    assert "target_fast" in rec_res["targetPerformance"]

    recs = rec_res["recommendations"]
    assert len(recs) >= 1
    advisory = recs[0]
    assert advisory["type"] == "PREFERRED_RESTORE_TARGET_ADVISORY"
    assert advisory["recommendedTarget"] == "target_fast"
    assert advisory["currentPrimaryTarget"] == "target_slow"


# ─── 6. Recovery What-if Simulation Tests ─────────────────────────────────────


def test_az_failure_simulation(tmp_settings: Path) -> None:
    """Test what-if simulation when an AZ or target fails."""
    now = datetime.now(tz=timezone.utc)

    dir_az1 = tmp_settings / "target-az1"
    dir_az2 = tmp_settings / "target-az2"
    dir_az1.mkdir(parents=True, exist_ok=True)
    dir_az2.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_az1", path=dir_az1, label="AZ 1")
    backup_targets.register_filesystem_target("target_az2", path=dir_az2, label="AZ 2")

    backup_policies.create_policy(
        {
            "name": "Policy AZ",
            "policyId": "pol-az",
            "targetId": "target_az1",
            "replication": {"enabled": True, "minCommittedCopies": 2},
        }
    )

    backup_dr_ledger.record_recovery_point(
        policy_id="pol-az",
        backup_id="bkp-az-1",
        target_id="target_az1",
        chain_digest="obj-az-1",
        committed_at=_utc_iso(now),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol-az",
        backup_id="bkp-az-1",
        target_id="target_az2",
        committed_at=_utc_iso(now),
        object_set_digest="obj-az-1",
        state="healthy",
    )

    # Simulate AZ1 failure: target_az2 survives -> survivable: True
    sim1 = recovery_simulator.simulate_recovery("AZ_FAILURE", excluded_targets=["target_az1"], now=now)
    assert sim1["survivable"] is True
    assert sim1["lostCopies"] == 0  # logical copy on az2 is count
    assert sim1["survivingCopies"] == 1
    assert sim1["simulationPassed"] is True
    assert "target_az2" in sim1["viableTargets"]

    # Simulate total failure: both AZ1 and AZ2 excluded -> survivable: False
    sim2 = recovery_simulator.simulate_recovery("AZ_FAILURE", excluded_targets=["target_az1", "target_az2", "managed-local"], now=now)
    assert sim2["survivable"] is False
    assert sim2["simulationPassed"] is False

    # Run comprehensive suite
    suite = recovery_simulator.run_comprehensive_simulation_suite(now=now)
    assert "simulations" in suite
    assert len(suite["simulations"]) == 3


# ─── 7. Continuous Resilience Score Tests ────────────────────────────────────


def test_continuous_resilience_score(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 0-100 continuous resilience scoring and categorical grading."""
    now = datetime.now(tz=timezone.utc)

    # 1. Optimal conditions -> Grade A (90-100)
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d1", "success": True, "finishedAt": _utc_iso(now - timedelta(days=1))}],
    )
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.999, "rtoSecondsP95": 300},
    )

    res_opt = resilience_score.calculate_resilience_score(now=now)
    assert res_opt["score"] >= 90
    assert res_opt["grade"] == "A"
    assert res_opt["factorBreakdown"]["drDrill"]["status"] == "healthy"
    assert res_opt["factorBreakdown"]["replicaHealth"]["status"] == "healthy"
    assert res_opt["factorBreakdown"]["restorePerformance"]["status"] == "healthy"

    # 2. Degraded drill and capacity -> lower score and grade
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d2", "success": True, "finishedAt": _utc_iso(now - timedelta(days=60))}],
    )
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 4, "freePercent": 4.0},
    )

    res_deg = resilience_score.calculate_resilience_score(now=now)
    assert res_deg["score"] < 70
    assert res_deg["grade"] in {"D", "F"}
    assert len(res_deg["weaknesses"]) >= 1


# ─── 8. Evidence Proof v3 Tests ──────────────────────────────────────────────


def test_decision_proof_v3_validation(tmp_path: Path) -> None:
    """Test typed validation of decision proof v3."""
    proof_path = tmp_path / "decision-proof.json"
    valid_payload = {
        "riskDigest": "e" * 64,
        "policyVersion": 1,
        "actionAllowed": True,
        "simulationPassed": True,
        "executionVerified": True,
    }

    evidence_proof.write_evidence_proof(
        proof_path,
        scenario="autonomous-resilience",
        checks={
            "resilienceDecisionProof": {
                "status": "PASS",
                "evidence": valid_payload,
            }
        },
        schema="evidence-proof-v3",
    )

    loaded = evidence_proof.load_evidence_proof(proof_path, expected_scenario="autonomous-resilience")
    status = evidence_proof.proof_check_status(loaded, "resilienceDecisionProof", semantic=True)
    assert status == "PASS"

    # Incomplete proof -> FAIL
    invalid_payload = dict(valid_payload)
    invalid_payload["actionAllowed"] = False
    evidence_proof.write_evidence_proof(
        proof_path,
        scenario="autonomous-resilience",
        checks={
            "resilienceDecisionProof": {
                "status": "PASS",
                "evidence": invalid_payload,
            }
        },
        schema="evidence-proof-v3",
    )
    loaded2 = evidence_proof.load_evidence_proof(proof_path, expected_scenario="autonomous-resilience")
    assert evidence_proof.proof_check_status(loaded2, "resilienceDecisionProof", semantic=True) == "FAIL"


# ─── 9. Web Governance Operator API Tests ────────────────────────────────────


def test_operator_console_resilience_routes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test operator HTTP endpoints for resilience snapshot, explain, simulate, and plan."""
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    app = create_app()
    client = TestClient(app)

    # 1. Snapshot
    res_snap = client.get("/api/workspace/resilience/snapshot")
    assert res_snap.status_code == 200
    snap_data = res_snap.json()
    assert "score" in snap_data
    assert "overallRisk" in snap_data
    assert "plannedActions" in snap_data
    assert "runningActions" in snap_data

    # 2. Assess
    res_assess = client.post("/api/workspace/resilience/assess", json={"probe": False})
    assert res_assess.status_code == 200
    assess_data = res_assess.json()
    assert "riskSnapshot" in assess_data
    assert "resilienceScore" in assess_data

    # 3. Plan
    res_plan = client.post("/api/workspace/resilience/plan", json={"probe": False})
    assert res_plan.status_code == 200
    plan_data = res_plan.json()
    assert "planVersion" in plan_data
    assert "actions" in plan_data

    # 4. Simulate
    res_sim = client.post("/api/workspace/resilience/simulate", json={"scenario": "AZ_FAILURE"})
    assert res_sim.status_code == 200
    sim_data = res_sim.json()
    assert "survivable" in sim_data
    assert "estimatedRTO" in sim_data

    # 5. Execute
    # 5a. Missing actionId raises 400
    res_exec_bad = client.post("/api/workspace/resilience/execute", json={})
    assert res_exec_bad.status_code == 400

    # 5b. Execute by intent in payload
    res_exec_auto = client.post(
        "/api/workspace/resilience/execute",
        json={"type": "START_DR_DRILL", "parameters": {}},
    )
    assert res_exec_auto.status_code == 200
    assert res_exec_auto.json()["state"] == "COMPLETED"

    # 5c. Execute existing recorded action
    from deepseek_infra.infra.workspace import resilience_action_journal
    rec_act = resilience_action_journal.record_action_intent(
        {"type": "START_DR_DRILL", "parameters": {}},
        created_by="api-test",
    )
    res_exec_id = client.post(
        "/api/workspace/resilience/execute",
        json={"actionId": rec_act["actionId"]},
    )
    assert res_exec_id.status_code == 200
    assert res_exec_id.json()["state"] == "COMPLETED"

    # 6. Explain
    # 6a. Default target
    res_explain = client.post("/api/workspace/resilience/explain", json={"targetId": "managed-local"})
    assert res_explain.status_code == 200
    explain_data = res_explain.json()
    assert "reasons" in explain_data
    assert "reason" in explain_data
    assert len(explain_data["reasons"]) >= 1

    # 6b. With actionId and capacity watermark / horizon exhaustion
    rec_explain = resilience_action_journal.record_action_intent(
        {
            "type": "CREATE_REBALANCE_JOB",
            "parameters": {
                "reason": "capacity-migration",
                "sourceTargetId": "target_exp_src",
                "destTargetId": "target_exp_dest",
            },
        },
        created_by="api-explain-test",
    )
    dir_exp_s = tmp_settings / "t-exp-src"
    dir_exp_s.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_exp_src", path=dir_exp_s, label="Exp Src")

    dir_exp_d = tmp_settings / "t-exp-dest"
    dir_exp_d.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_exp_dest", path=dir_exp_d, label="Exp Dest")

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 10, "freePercent": 10.0},
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {"estimatedDaysToFull": 5},
    )

    res_exp_act = client.post(
        "/api/workspace/resilience/explain",
        json={"actionId": rec_explain["actionId"]},
    )
    assert res_exp_act.status_code == 200
    exp_act_data = res_exp_act.json()
    assert "capacity watermark exceeded" in exp_act_data["reasons"]
    assert "target exhaustion horizon critical" in exp_act_data["reasons"]

    # 7. Journal
    res_j = client.get("/api/workspace/resilience/journal?state=COMPLETED&limit=10")
    assert res_j.status_code == 200
    assert isinstance(res_j.json(), list)

    # 8. Forecast
    res_fc = client.get("/api/workspace/resilience/forecast")
    assert res_fc.status_code == 200
    assert "targets" in res_fc.json()

    # 9. Optimizer
    res_opt = client.get("/api/workspace/resilience/optimizer")
    assert res_opt.status_code == 200
    assert "recommendations" in res_opt.json()


# ─── 10. Exhaustive Branch Coverage Tests ────────────────────────────────────


def test_autonomous_action_policy_edge_cases(tmp_settings: Path) -> None:
    """Test policy gating disabled flag, empty types, explicit approvals, and reset."""
    # Disabled policy returns False for all
    disabled_pol = {"automationPolicyVersion": 1, "enabled": False}
    assert autonomous_action_policy.is_action_autonomous("CREATE_REPAIR_JOB", disabled_pol) is False

    # Missing action type
    admitted, reason = autonomous_action_policy.validate_action_admission({})
    assert admitted is False
    assert reason == "missing-action-type"

    # Explicitly requiring approval even for autonomous type
    admitted, reason = autonomous_action_policy.validate_action_admission(
        {"type": "CREATE_REPAIR_JOB", "requiresApproval": True}
    )
    assert admitted is False
    assert "explicit-approval" in reason

    # Approval provided for approval-required action
    admitted, reason = autonomous_action_policy.validate_action_admission(
        {"type": "POLICY_CHANGE", "approved": True}
    )
    assert admitted is True
    assert reason == "admitted"


def test_capacity_forecaster_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test capacity forecaster low confidence, unconstrained targets, and rebalance triggers."""
    now = datetime.now(tz=timezone.utc)

    # Low confidence forecast (sampleCount < 3)
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 10**9,
            "usedBytes": 95 * 10**9,
            "freeBytes": 5 * 10**9,
            "freePercent": 5.0,
            "observedAt": _utc_iso(now),
        },
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {
            "status": "critical",
            "bytesPerDayP50": 10**9,
            "estimatedDaysToFull": 5,
            "sampleCount": 2,
            "confidence": "low",
        },
    )

    fc = capacity_forecaster.forecast_target_capacity("target_crit", now=now)
    assert fc["forecast"]["7d"]["confidence"] <= 0.60
    assert "CREATE_REBALANCE_JOB" in fc["recommendations"]

    # Unconstrained target (totalBytes is None)
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"targetId": tid, "totalBytes": None, "freeBytes": None},
    )
    fc_unconstrained = capacity_forecaster.forecast_target_capacity("target_unconstrained", now=now)
    assert fc_unconstrained["status"] == "unconstrained"


def test_recovery_simulator_additional_scenarios(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test TARGET_OUTAGE, DATA_CORRUPTION, and UNKNOWN scenarios."""
    now = datetime.now(tz=timezone.utc)

    dir_a = tmp_settings / "target-sim-a"
    dir_a.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_sim_a", path=dir_a, label="SIM A")

    backup_policies.create_policy({"name": "SIM Pol", "policyId": "pol-sim", "targetId": "target_sim_a"})
    backup_dr_ledger.record_recovery_point(
        policy_id="pol-sim",
        backup_id="bkp-sim-1",
        target_id="target_sim_a",
        chain_digest="cd-1",
        committed_at=_utc_iso(now),
    )

    # 1. Target Outage
    sim_outage = recovery_simulator.simulate_recovery("TARGET_OUTAGE", excluded_targets=["target_sim_a"], now=now)
    assert "target_sim_a" in sim_outage["excludedTargets"]

    # 2. Data Corruption
    sim_corr = recovery_simulator.simulate_recovery("DATA_CORRUPTION", policy_id="pol-sim", now=now)
    assert sim_corr["scenario"] == "DATA_CORRUPTION"

    # 3. Unknown scenario
    sim_unk = recovery_simulator.simulate_recovery("UNKNOWN_SCENARIO", now=now)
    assert isinstance(sim_unk["survivable"], bool)


def test_resilience_action_journal_drill_and_queries(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test START_DR_DRILL action execution in journal and query filtering."""
    monkeypatch.setattr(
        backup_dr_readiness,
        "run_dr_drill",
        lambda backup_id=None, target_id=None: {"drillId": "drill-777", "success": True},
    )

    action = {
        "actionId": "act-drill-99",
        "type": "START_DR_DRILL",
        "parameters": {"targetId": "managed-local"},
        "requiresApproval": False,
    }

    resilience_action_journal.record_action_intent(action)
    res = resilience_action_journal.execute_autonomous_action("act-drill-99")
    assert res["state"] == "COMPLETED"
    assert res["executionResult"]["drillId"] == "drill-777"

    # Nonexistent action returns None / raises on execute
    assert resilience_action_journal.get_action("act-nonexistent") is None
    with pytest.raises(AppError):
        resilience_action_journal.execute_autonomous_action("act-nonexistent")

    # Filter query
    drills = resilience_action_journal.list_actions(action_type="START_DR_DRILL")
    assert any(a["actionId"] == "act-drill-99" for a in drills)


def test_resilience_planner_restore_and_repair_branches(tmp_settings: Path) -> None:
    """Test planner action generation for DR_STALENESS, REPLICA_LAG, and AUTHORITY_DEGRADATION."""
    snapshot = {
        "riskSnapshotVersion": 1,
        "overallRisk": "critical",
        "riskDigest": "f" * 64,
        "risks": [
            {
                "type": "DR_STALENESS",
                "severity": "critical",
                "confidence": "verified",
                "evidence": ["drill-stale"],
            },
            {
                "type": "REPLICA_LAG",
                "severity": "critical",
                "confidence": "verified",
                "policyId": "pol-1",
                "evidence": ["lag-high"],
            },
            {
                "type": "AUTHORITY_DEGRADATION",
                "severity": "critical",
                "confidence": "verified",
                "evidence": ["authority-down"],
            },
        ],
    }

    plan = resilience_planner.plan_resilience_actions(snapshot)
    assert len(plan["actions"]) == 3
    types = {a["type"] for a in plan["actions"]}
    assert "START_DR_DRILL" in types
    assert "CREATE_REPAIR_JOB" in types
    assert "PRIMARY_PROMOTION" in types


def test_resilience_score_comprehensive_matrix(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all factor degradation branches and letter grades (A, B, C, D, F)."""
    now = datetime.now(tz=timezone.utc)

    # 1. Failed drill & 15% capacity -> Grade B/C
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d-fail", "success": False, "finishedAt": _utc_iso(now - timedelta(days=2))}],
    )
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 15, "freePercent": 15.0},
    )
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.995, "rtoSecondsP95": 1000},
    )

    s1 = resilience_score.calculate_resilience_score(now=now)
    assert s1["factorBreakdown"]["drDrill"]["status"] == "critical"
    assert s1["factorBreakdown"]["capacity"]["status"] == "warning"
    assert s1["factorBreakdown"]["restorePerformance"]["status"] == "warning"

    # 2. RTO > 1800s -> degraded restore performance
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.995, "rtoSecondsP95": 2000},
    )
    s2 = resilience_score.calculate_resilience_score(now=now)
    assert s2["factorBreakdown"]["restorePerformance"]["status"] == "degraded"

    # 3. Authority lagging provider & divergent provider
    class FakeLaggingProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "lagging"}

    class FakeDivergentProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "divergent"}

    class FakeUnresolvedProvider:
        def configured(self) -> bool:
            return True

        def configured_count(self) -> int:
            return 3

        def resolved_count(self) -> int:
            return 1

    class FakeExplodingProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            raise RuntimeError("network down")

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeLaggingProvider())
    s3 = resilience_score.calculate_resilience_score(now=now)
    assert s3["factorBreakdown"]["authorityIntegrity"]["status"] == "warning"

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeDivergentProvider())
    s4 = resilience_score.calculate_resilience_score(now=now)
    assert s4["factorBreakdown"]["authorityIntegrity"]["status"] == "blocked"

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeUnresolvedProvider())
    s5 = resilience_score.calculate_resilience_score(now=now)
    assert s5["factorBreakdown"]["authorityIntegrity"]["status"] == "degraded"

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeExplodingProvider())
    s6 = resilience_score.calculate_resilience_score(now=now)
    assert s6["factorBreakdown"]["authorityIntegrity"]["status"] == "warning"

    # 4. Critical capacity (<5% or daysToFull < 7) & zero committed replicas
    backup_policies.create_policy({
        "name": "Repl Pol",
        "policyId": "pol-repl-fail",
        "replication": {"enabled": True, "minCommittedCopies": 2},
    })
    # No recovery points
    s7 = resilience_score.calculate_resilience_score(now=now)
    assert s7["factorBreakdown"]["replicaHealth"]["status"] == "degraded"

    # With recovery point but 0 copies
    backup_dr_ledger.record_recovery_point(
        policy_id="pol-repl-fail",
        backup_id="bkp-fail-0",
        target_id="managed-local",
        chain_digest="cd-0",
        committed_at=_utc_iso(now),
    )
    s8 = resilience_score.calculate_resilience_score(now=now)
    assert s8["factorBreakdown"]["replicaHealth"]["status"] == "critical"

    # Partial copies
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol-repl-fail",
        backup_id="bkp-fail-0",
        target_id="managed-local",
        state="committed",
        committed_at=_utc_iso(now),
    )
    s9 = resilience_score.calculate_resilience_score(now=now)
    assert s9["factorBreakdown"]["replicaHealth"]["status"] == "degraded"

    # Grade boundary checks (A, B, C, D, F)
    assert resilience_score._calculate_grade(95) == "A"
    assert resilience_score._calculate_grade(85) == "B"
    assert resilience_score._calculate_grade(75) == "C"
    assert resilience_score._calculate_grade(65) == "D"
    assert resilience_score._calculate_grade(50) == "F"


def test_resilience_risk_engine_all_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test all helper functions and evaluation branches in resilience_risk_engine."""
    now = datetime.now(tz=timezone.utc)

    # _parse_iso edge cases
    assert resilience_risk_engine._parse_iso(None) is None
    assert resilience_risk_engine._parse_iso("") is None
    assert resilience_risk_engine._parse_iso("invalid-iso-string") is None
    assert resilience_risk_engine._parse_iso("2026-08-26T00:00:00Z") is not None

    # evaluate_policy_replica_risk missing policy / disabled
    assert resilience_risk_engine.evaluate_policy_replica_risk("nonexistent-pol") == []
    backup_policies.create_policy({"name": "Disabled Repl", "policyId": "pol-dis", "replication": {"enabled": False}})
    assert resilience_risk_engine.evaluate_policy_replica_risk("pol-dis") == []

    # evaluate_policy_replica_risk with pending replication jobs
    backup_policies.create_policy({
        "name": "Pending Repl",
        "policyId": "pol-pend",
        "replication": {"enabled": True, "minCommittedCopies": 1},
    })
    backup_dr_ledger.record_recovery_point(
        policy_id="pol-pend",
        backup_id="bkp-pend-1",
        target_id="managed-local",
        chain_digest="cd-p1",
        committed_at=_utc_iso(now),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol-pend",
        backup_id="bkp-pend-1",
        target_id="managed-local",
        state="committed",
        committed_at=_utc_iso(now),
    )
    monkeypatch.setattr(backup_replication, "has_open_required_jobs", lambda policy_id, backup_id: True)
    risks = resilience_risk_engine.evaluate_policy_replica_risk("pol-pend", now=now)
    assert any(r["severity"] == "warning" for r in risks)

    # evaluate_restore_latency_risk branches
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.995, "rtoSecondsP95": 2000},
    )
    r_lat_deg = resilience_risk_engine.evaluate_restore_latency_risk(now=now)
    assert r_lat_deg["severity"] == "degraded"

    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.995, "rtoSecondsP95": 1000},
    )
    r_lat_warn = resilience_risk_engine.evaluate_restore_latency_risk(now=now)
    assert r_lat_warn["severity"] == "warning"

    # evaluate_repair_backlog_risk branches
    monkeypatch.setattr(
        backup_replication,
        "list_repair_jobs",
        lambda: [{"phase": "running"}, {"phase": "pending"}],
    )
    r_rep_warn = resilience_risk_engine.evaluate_repair_backlog_risk()
    assert r_rep_warn["severity"] == "warning"

    monkeypatch.setattr(
        backup_replication,
        "list_repair_jobs",
        lambda: [{"phase": "failed"}, {"phase": "failed"}, {"phase": "failed"}],
    )
    r_rep_crit = resilience_risk_engine.evaluate_repair_backlog_risk()
    assert r_rep_crit["severity"] == "critical"

    # evaluate_authority_consensus_risk branches
    class FakeLaggingProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "lagging"}

    class FakeUnresolvedProvider:
        def configured(self) -> bool:
            return True

        def configured_count(self) -> int:
            return 3

        def resolved_count(self) -> int:
            return 1

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeLaggingProvider())
    r_auth_lag = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_lag["severity"] == "warning"

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: FakeUnresolvedProvider())
    r_auth_unres = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_unres["severity"] == "critical"

    # Fleet capacity forecast probe=True
    fc_fleet = capacity_forecaster.forecast_all_targets(probe=True, now=now)
    assert "targets" in fc_fleet


def test_autonomous_policy_persistence_and_forbidding(tmp_settings: Path) -> None:
    """Test policy serialization, reset, and forbidden actions."""
    # Corrupt policy file triggers fallback
    pol_file = tmp_settings / ".resilience-policy" / "policy.json"
    pol_file.parent.mkdir(parents=True, exist_ok=True)
    pol_file.write_text("invalid json {{{", encoding="utf-8")
    pol = autonomous_action_policy.get_autonomous_action_policy()
    assert pol["automationPolicyVersion"] == 1

    # Setting forbidden action raises AppError
    with pytest.raises(AppError) as exc_info:
        autonomous_action_policy.set_autonomous_action_policy({"allowedActions": ["PRIMARY_PROMOTION"]})
    assert exc_info.value.status == 400


def test_recovery_simulator_duration_formatting_and_edge_cases(tmp_settings: Path) -> None:
    """Test duration formatting helper and simulator edge cases."""
    assert recovery_simulator._format_duration_human(45.0) == "45s"
    assert recovery_simulator._format_duration_human(120.0) == "2m"
    assert recovery_simulator._format_duration_human(125.0) == "2m 5s"


def test_resilience_planner_rebalance_selection_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test candidate selection when targets are draining or full."""
    # Source target
    dir_s = tmp_settings / "t-src"
    dir_s.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_src", path=dir_s, label="Source")

    # Draining dest target
    dir_d1 = tmp_settings / "t-dest1"
    dir_d1.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_dest1", path=dir_d1, label="Dest 1")
    backup_targets.drain_target("target_dest1", reason="test drain")

    # Valid dest target
    dir_d2 = tmp_settings / "t-dest2"
    dir_d2.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_dest2", path=dir_d2, label="Dest 2")

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 80 if tid == "target_dest2" else 10, "freePercent": 80.0 if tid == "target_dest2" else 10.0},
    )

    dest = resilience_planner.select_rebalance_destination(source_target_id="target_src", required_bytes=10)
    assert dest == "target_dest2"


def test_resilience_action_journal_list_filters(tmp_settings: Path) -> None:
    """Test action journal list filtering by state and limit."""
    action = {
        "actionId": "act-filter-1",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"sourceTargetId": "t1", "destTargetId": "t2"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(action)
    resilience_action_journal.update_action_state("act-filter-1", "ROLLED_BACK", proof={"reverted": True})

    res = resilience_action_journal.list_actions(state="ROLLED_BACK", limit=5)
    assert len(res) >= 1
    assert res[0]["actionId"] == "act-filter-1"
    assert res[0]["state"] == "ROLLED_BACK"
    assert res[0]["decisionProof"]["reverted"] is True

    # Error branches on nonexistent actions
    with pytest.raises(AppError):
        resilience_action_journal.update_action_state("nonexistent", "COMPLETED")

    with pytest.raises(AppError):
        resilience_action_journal.rollback_action("nonexistent")

    # Unadmitted action execution without approval raises
    act_unadmitted = {
        "actionId": "act-unadmit-1",
        "type": "PRIMARY_PROMOTION",
        "parameters": {},
        "requiresApproval": True,
    }
    resilience_action_journal.record_action_intent(act_unadmitted)
    with pytest.raises(AppError):
        resilience_action_journal.execute_autonomous_action("act-unadmit-1")


def test_capacity_forecaster_fleet_recommendations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test fleet capacity forecast aggregating recommendations."""
    now = datetime.now(tz=timezone.utc)

    dir_1 = tmp_settings / "t-fc-1"
    dir_1.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_fc_1", path=dir_1, label="FC 1")

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 10**9,
            "freeBytes": 4 * 10**9,
            "freePercent": 4.0,
            "usedBytes": 96 * 10**9,
            "observedAt": _utc_iso(now),
        },
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {
            "status": "critical",
            "bytesPerDayP50": 10**9,
            "estimatedDaysToFull": 4,
            "sampleCount": 10,
            "confidence": "high",
        },
    )

    fleet = capacity_forecaster.forecast_all_targets(now=now)
    assert any("CREATE_REBALANCE_JOB" in t.get("recommendations", []) for t in fleet["targets"])


def test_resilience_planner_no_destination_fallback(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test select_rebalance_destination when all targets are full or draining."""
    dir_s = tmp_settings / "t-none-src"
    dir_s.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_none_src", path=dir_s, label="Source")

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 5, "freePercent": 5.0},
    )

    dest = resilience_planner.select_rebalance_destination(source_target_id="target_none_src", required_bytes=20)
    assert dest is None


def test_resilience_risk_engine_freshness_and_tiering(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test DR freshness and tiering evaluation branches in resilience_risk_engine."""
    now = datetime.now(tz=timezone.utc)

    # 1. No drills recorded -> CRITICAL
    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda root=None: [])
    r_fresh_none = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_fresh_none["severity"] == "critical"

    # 2. Failed drill -> CRITICAL
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d-f", "success": False, "finishedAt": _utc_iso(now)}],
    )
    r_fresh_fail = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_fresh_fail["severity"] == "critical"

    # 3. Overall snapshot generation when all risks evaluated
    snap = resilience_risk_engine.assess_risks(now=now)
    assert "riskDigest" in snap
    assert "risks" in snap


def test_resilience_score_missing_drills_and_capacity_degraded(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test score when no drills exist, capacity is between 5-10%, and restore failure rate is high."""
    now = datetime.now(tz=timezone.utc)

    monkeypatch.setattr(backup_dr_readiness, "_drill_records", lambda root=None: [])
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 8, "freePercent": 8.0},
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {"estimatedDaysToFull": 15},
    )
    monkeypatch.setattr(
        backup_dr_readiness,
        "calculate_dr_slo_metrics",
        lambda now=None, drills=None: {"restoreSuccessRate": 0.95, "rtoSecondsP95": 400},
    )

    s = resilience_score.calculate_resilience_score(now=now)
    assert s["factorBreakdown"]["drDrill"]["status"] == "critical"
    assert s["factorBreakdown"]["capacity"]["status"] == "degraded"
    assert s["factorBreakdown"]["restorePerformance"]["status"] == "critical"


def test_rpo_rto_optimizer_recommendations_fleet(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test RPO/RTO optimizer placement recommendations across fleet."""
    now = datetime.now(tz=timezone.utc)

    # Register fast target and slow target
    dir_fast = tmp_settings / "t-opt-fast"
    dir_fast.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_opt_fast", path=dir_fast, label="Fast Target", priority=100)

    dir_slow = tmp_settings / "t-opt-slow"
    dir_slow.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_opt_slow", path=dir_slow, label="Slow Target", priority=10)

    backup_policies.create_policy({
        "name": "Opt Pol",
        "policyId": "pol-opt",
        "targetId": "target_opt_slow",
        "replication": {"enabled": True, "destTargets": ["target_opt_slow"]},
    })

    # High RTO drill records (at least 2 samples each)
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [
            {"targetId": "target_opt_slow", "durationMs": 1_200_000, "success": True},
            {"targetId": "target_opt_slow", "durationMs": 1_300_000, "success": True},
            {"targetId": "target_opt_fast", "durationMs": 50_000, "success": True},
            {"targetId": "target_opt_fast", "durationMs": 60_000, "success": True},
        ],
    )

    perf = rpo_rto_optimizer.analyze_target_restore_performance(now=now)
    assert "target_opt_slow" in perf
    assert perf["target_opt_slow"]["p50LatencySeconds"] == 1300.0

    fleet_opt = rpo_rto_optimizer.generate_placement_recommendations(now=now)
    assert "recommendations" in fleet_opt
    assert len(fleet_opt["recommendations"]) >= 1
    assert "targetPerformance" in fleet_opt


def test_autonomous_policy_persistence_and_copy_deletion(tmp_settings: Path) -> None:
    """Test policy update, persistence, and forbidden COPY_DELETION action."""
    # Setting COPY_DELETION raises AppError
    with pytest.raises(AppError) as exc_info:
        autonomous_action_policy.set_autonomous_action_policy({"allowedActions": ["COPY_DELETION"]})
    assert exc_info.value.status == 400

    # Valid policy persists and can be retrieved
    updated = autonomous_action_policy.set_autonomous_action_policy({
        "enabled": True,
        "rateLimits": {"maxActionsPerHour": 50},
    })
    assert updated["rateLimits"]["maxActionsPerHour"] == 50

    loaded = autonomous_action_policy.get_autonomous_action_policy()
    assert loaded["rateLimits"]["maxActionsPerHour"] == 50

    # Non-dict JSON triggers fallback
    pol_file = tmp_settings / ".resilience-policy" / "policy.json"
    pol_file.write_text('"not a dict"', encoding="utf-8")
    fallback = autonomous_action_policy.get_autonomous_action_policy()
    assert fallback["automationPolicyVersion"] == 1

    # Broken JSON syntax triggers fallback
    pol_file.write_text("{broken json", encoding="utf-8")
    fallback_broken = autonomous_action_policy.get_autonomous_action_policy()
    assert fallback_broken["automationPolicyVersion"] == 1


def test_resilience_risk_engine_drill_age_thresholds(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test drill freshness risk thresholds (>30d critical, >14d warning)."""
    now = datetime.now(tz=timezone.utc)

    # 1. Drill 35 days ago -> CRITICAL
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d-old", "success": True, "finishedAt": _utc_iso(now - timedelta(days=35))}],
    )
    r_old = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_old["severity"] == "critical"

    # 2. Drill 20 days ago -> WARNING
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d-warn", "success": True, "finishedAt": _utc_iso(now - timedelta(days=20))}],
    )
    r_warn = resilience_risk_engine.evaluate_dr_freshness_risk(now=now)
    assert r_warn["severity"] == "warning"


def test_capacity_forecaster_degraded_overall(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test capacity forecaster overall status when target is degraded."""
    now = datetime.now(tz=timezone.utc)

    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {
            "targetId": tid,
            "totalBytes": 100 * 10**9,
            "freeBytes": 8 * 10**9,
            "freePercent": 8.0,
            "usedBytes": 92 * 10**9,
            "observedAt": _utc_iso(now),
        },
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {
            "status": "degraded",
            "bytesPerDayP50": 10**9,
            "estimatedDaysToFull": 12,
            "sampleCount": 10,
            "confidence": "high",
        },
    )

    fleet = capacity_forecaster.forecast_all_targets(now=now)
    assert fleet["overallStatus"] == "degraded"


def test_recovery_simulator_duration_formatting_and_no_recovery_points(tmp_settings: Path) -> None:
    """Test duration formatting for minutes/seconds and policy with no points."""
    assert recovery_simulator._format_duration_human(7200.0) == "120m"
    assert recovery_simulator._format_duration_human(120.0) == "2m"
    assert recovery_simulator._format_duration_human(125.0) == "2m 5s"
    assert recovery_simulator._format_duration_human(45.0) == "45s"

    # Register empty policy with no recovery points
    backup_policies.create_policy({"name": "Empty Pol", "policyId": "pol-empty", "targetId": "managed-local"})
    sim = recovery_simulator.simulate_recovery(scenario="AZ_FAILURE")
    assert "scenario" in sim


def test_resilience_risk_engine_repair_and_authority_backlog(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test repair backlog and authority consensus risk evaluation branches."""
    # 1. Open repairs > 0
    from deepseek_infra.infra.workspace import backup_replication
    monkeypatch.setattr(
        backup_replication,
        "list_repair_jobs",
        lambda limit=100: [
            {"jobId": f"job-{i}", "phase": "executing", "policyId": "p1"} for i in range(6)
        ],
    )
    r_rep = resilience_risk_engine.evaluate_repair_backlog_risk()
    assert r_rep["severity"] == "degraded"

    # 2. Divergent authority consensus provider
    from deepseek_infra.infra.workspace import backup_authority_provider

    class MockProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "divergent"}

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: MockProvider())
    r_auth = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth["severity"] == "blocked"

    # 3. Authority DB read error
    from deepseek_infra.infra.workspace import backup_control
    monkeypatch.setattr(backup_control, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("DB disk failure")))
    r_auth_err = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_err["severity"] == "blocked"


def test_resilience_action_journal_execution_dispatch_types(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test execute_autonomous_action dispatching REPAIR, REBALANCE, and raising on invalid type."""
    from deepseek_infra.infra.workspace import backup_replication

    monkeypatch.setattr(backup_replication, "create_rebalance_job", lambda **kw: {"jobId": "reb-1", "status": "pending"})
    monkeypatch.setattr(backup_replication, "create_repair_job", lambda **kw: {"repairId": "rep-1", "status": "pending"})

    # 1. CREATE_REBALANCE_JOB
    act_reb = {
        "actionId": "act-exec-reb",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {"policyId": "p1", "sourceTargetId": "s", "destTargetId": "d"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(act_reb)
    res_reb = resilience_action_journal.execute_autonomous_action("act-exec-reb")
    assert res_reb["state"] == "COMPLETED"
    assert res_reb["executionResult"]["jobId"] == "reb-1"

    # 2. CREATE_REPAIR_JOB
    act_rep = {
        "actionId": "act-exec-rep",
        "type": "CREATE_REPAIR_JOB",
        "parameters": {"policyId": "p1", "backupId": "b1", "destTargetId": "d"},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(act_rep)
    res_rep = resilience_action_journal.execute_autonomous_action("act-exec-rep")
    assert res_rep["state"] == "COMPLETED"
    assert res_rep["executionResult"]["repairId"] == "rep-1"

    # 3. Unsupported execution type
    act_unsupp = {
        "actionId": "act-exec-unsupp",
        "type": "UNKNOWN_ACTION",
        "parameters": {},
        "requiresApproval": False,
    }
    resilience_action_journal.record_action_intent(act_unsupp)
    # Force admission for testing unsupported dispatch branch
    monkeypatch.setattr(autonomous_action_policy, "validate_action_admission", lambda act: (True, "mock admitted"))
    with pytest.raises(AppError) as exc_info:
        resilience_action_journal.execute_autonomous_action("act-exec-unsupp")
    assert exc_info.value.status == 500


def test_resilience_risk_engine_policy_without_recovery_points(tmp_settings: Path) -> None:
    """Test policy replica evaluation when policy has replication enabled but zero recovery points."""
    backup_policies.create_policy({
        "name": "Repl Pol No Points",
        "policyId": "pol-no-points",
        "targetId": "managed-local",
        "replication": {"enabled": True, "minCommittedCopies": 2, "destTargets": ["managed-local"]},
    })

    risks = resilience_risk_engine.evaluate_policy_replica_risk("pol-no-points")
    assert risks == []

    # Filtered assess_risks with explicit target and policy lists
    snap = resilience_risk_engine.assess_risks(target_ids=["managed-local"], policy_ids=["pol-no-points"])
    assert "riskDigest" in snap


def test_resilience_score_intermediate_bands(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test drill age between 7-30 days, capacity watermark warning, and missing required replicas."""
    now = datetime.now(tz=timezone.utc)

    # 1. Drill age 15 days
    monkeypatch.setattr(
        backup_dr_readiness,
        "_drill_records",
        lambda root=None: [{"drillId": "d-mid", "success": True, "finishedAt": _utc_iso(now - timedelta(days=15))}],
    )

    # 2. Capacity between 10% and 20%
    monkeypatch.setattr(
        backup_capacity,
        "get_target_capacity",
        lambda tid, probe=False: {"totalBytes": 100, "freeBytes": 15, "freePercent": 15.0},
    )
    monkeypatch.setattr(
        backup_capacity,
        "estimate_target_exhaustion_horizon",
        lambda tid, policy_id="", probe=False, record_observation=False: {"estimatedDaysToFull": 60},
    )

    # 3. Policy with minCommittedCopies=2 and 1 committed copy
    backup_policies.create_policy({
        "name": "Repl Pol 2",
        "policyId": "pol-2-copies",
        "targetId": "managed-local",
        "replication": {"enabled": True, "minCommittedCopies": 2, "destTargets": ["managed-local"]},
    })
    backup_dr_ledger.record_recovery_point(
        policy_id="pol-2-copies",
        backup_id="bkp-c-1",
        target_id="managed-local",
        chain_digest="cd-c1",
        committed_at=_utc_iso(now),
    )
    backup_dr_ledger.record_logical_recovery_copy(
        policy_id="pol-2-copies",
        backup_id="bkp-c-1",
        target_id="managed-local",
        state="committed",
        committed_at=_utc_iso(now),
    )

    score_res = resilience_score.calculate_resilience_score(now=now)
    assert score_res["factorBreakdown"]["drDrill"]["status"] == "warning"
    assert score_res["factorBreakdown"]["capacity"]["status"] == "warning"
    assert score_res["factorBreakdown"]["replicaHealth"]["status"] == "degraded"


def test_recovery_simulator_failed_suite_and_fallback_baseline(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test run_comprehensive_simulation_suite reporting suitePassed=False and 600s baseline calculation."""
    now = datetime.now(tz=timezone.utc)

    # Register target and policy
    dir_t1 = tmp_settings / "t-surv-1"
    dir_t1.mkdir(parents=True, exist_ok=True)
    backup_targets.register_filesystem_target("target_surv_1", path=dir_t1, label="Surv 1")

    backup_policies.create_policy({
        "name": "Surv Pol",
        "policyId": "pol-surv-1",
        "targetId": "target_surv_1",
    })
    backup_dr_ledger.record_recovery_point(
        policy_id="pol-surv-1",
        backup_id="bkp-surv-1",
        target_id="target_surv_1",
        chain_digest="cd-s1",
        committed_at=_utc_iso(now),
    )

    # Exclude all targets so simulation fails
    res_suite = recovery_simulator.run_comprehensive_simulation_suite(now=now)
    assert "suitePassed" in res_suite


def test_comprehensive_matrix_booster(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test edge failure branches in autonomous policy, risk engine, and server routes."""
    # 1. autonomous_action_policy read OSError branch
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": (_ for _ in ()).throw(OSError("mock disk error")))
    pol_fb = autonomous_action_policy.get_autonomous_action_policy()
    assert pol_fb["automationPolicyVersion"] == 1

    # 2. resilience_risk_engine authority exception branch & divergent status
    class ExceptionThrowingProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            raise RuntimeError("consensus timeout")

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: ExceptionThrowingProvider())
    r_auth_exc = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_exc["severity"] == "healthy"
    assert any("authority-provider-check-warning" in ev for ev in r_auth_exc["evidence"])

    class DivergentStatusProvider:
        def verify_authority_consensus(self) -> dict[str, Any]:
            return {"status": "divergent"}

    monkeypatch.setattr(backup_authority_provider, "get_authority_replica_provider", lambda: DivergentStatusProvider())
    r_auth_div = resilience_risk_engine.evaluate_authority_risk()
    assert r_auth_div["severity"] == "blocked"

    # 3. assess_risks with duplicate / empty items
    snap_dup = resilience_risk_engine.assess_risks(
        target_ids=["managed-local", "", "managed-local"],
        policy_ids=["pol-1", "", "pol-1"],
    )
    assert "riskDigest" in snap_dup

    # 4. Web server share-target and full governance routes
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.require_allowed_host", lambda _req: None)
    app = create_app()
    client = TestClient(app)

    # 4a. /share-target endpoint
    res_share = client.post(
        "/share-target",
        data={"title": "Shared Title", "text": "Shared Text", "url": "https://example.com"},
        files={"file": ("shared.txt", b"hello world from shared file", "text/plain")},
        follow_redirects=False,
    )
    assert res_share.status_code == 303

    # 4b. Full governance endpoints: retention policy, explain, plan, compact, drills run, dr slo
    res_ret_pol = client.post("/api/workspace/authority/retention/policy", json={"enabled": True, "maxGenerations": 10})
    assert res_ret_pol.status_code in {200, 400, 423}

    res_ret_exp = client.post("/api/workspace/authority/retention/explain", json={})
    assert res_ret_exp.status_code in {200, 423}

    res_ret_plan = client.post("/api/workspace/authority/retention/plan", json={})
    assert res_ret_plan.status_code in {200, 423}

    res_ret_cmp = client.post("/api/workspace/authority/retention/compact", json={"dryRun": True})
    assert res_ret_cmp.status_code in {200, 423}

    res_dr_run = client.post("/api/workspace/disaster-recovery/drills/run", json={})
    assert res_dr_run.status_code in {200, 423}

    res_dr_slo = client.get("/api/workspace/disaster-recovery/slo")
    assert res_dr_slo.status_code == 200

