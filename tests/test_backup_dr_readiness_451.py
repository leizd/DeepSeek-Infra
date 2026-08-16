"""Unit tests for Scope-Aware DR Readiness & Objectives (Recovery Assurance Gate D)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
)


def test_evaluate_scope_readiness_no_recovery_points(tmp_settings: Path) -> None:
    scope_eval = backup_dr_readiness.evaluate_scope_readiness(
        target_id="target_1",
        policy_id="policy_1",
    )
    assert scope_eval["status"] == "blocked"
    assert scope_eval["reason"] == "no-policy-recovery-point"
    assert scope_eval["recoverable"] is False


def test_evaluate_scope_readiness_available_and_objectives(tmp_settings: Path) -> None:
    # Record target evidence
    backup_dr_ledger.record_target_evidence(
        target_id="target_1",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=True,
        integrity_mode="full-readback",
        status="ok",
    )

    # Record recovery point
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="policy_1",
        backup_id="bk_001",
        committed_at="2026-08-15T00:00:00Z",
        snapshot_kind="full",
        parent_backup_id=None,
        chain_digest="hash001",
        chain_length=1,
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
        verified_at="2026-08-15T00:00:00Z",
        storage_protocol="object-set-v1",
    )

    # Record scrub evidence (policy-scoped)
    backup_dr_ledger.record_scrub_evidence(
        target_id="target_1",
        backup_id="bk_001",
        policy_id="policy_1",
        observed_at="2026-08-15T00:00:00Z",
        result="success",
    )

    # Record drill evidence
    backup_dr_ledger.record_drill_evidence(
        target_id="target_1",
        policy_id="policy_1",
        backup_id="bk_001",
        drill_kind="manual",
        observed_at="2026-08-15T00:00:00Z",
        result="success",
        work_class="recovery-drill",
        stage_durations={"totalMs": 200},
    )

    # Evaluate with generous objectives
    scope_eval = backup_dr_readiness.evaluate_scope_readiness(
        target_id="target_1",
        policy_id="policy_1",
        recovery_objectives={
            "maxRpoSeconds": 7 * 24 * 3600,
            "maxScrubAgeSeconds": 30 * 24 * 3600,
            "maxDrillAgeSeconds": 90 * 24 * 3600,
        },
    )
    assert scope_eval["status"] == "available"
    assert scope_eval["reason"] is None
    assert scope_eval["recoverable"] is True
    assert scope_eval["rtoEstimate"]["isSla"] is False


def test_evaluate_scope_readiness_objective_breached(tmp_settings: Path) -> None:
    # Recovery point committed long ago
    backup_dr_ledger.record_recovery_point(
        target_id="target_old",
        policy_id="policy_old",
        backup_id="bk_old",
        committed_at="2020-01-01T00:00:00Z",
        snapshot_kind="full",
        parent_backup_id=None,
        chain_digest="hash_old",
        chain_length=1,
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
        verified_at="2020-01-01T00:00:00Z",
        storage_protocol="object-set-v1",
    )

    # RPO breached
    scope_eval = backup_dr_readiness.evaluate_scope_readiness(
        target_id="target_old",
        policy_id="policy_old",
        recovery_objectives={"maxRpoSeconds": 3600},  # 1 hour
    )
    assert scope_eval["status"] == "objective-breached"
    assert scope_eval["reason"] == "rpo-objective-breached"


def test_evaluate_scope_readiness_scrub_and_drill_failures(tmp_settings: Path) -> None:
    target_id = "target_degraded"
    policy_id = "policy_degraded"

    backup_dr_ledger.record_recovery_point(
        target_id=target_id,
        policy_id=policy_id,
        backup_id="bk_deg",
        committed_at="2026-08-15T00:00:00Z",
        snapshot_kind="full",
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
    )

    # Failed scrub marks degraded
    backup_dr_ledger.record_scrub_evidence(
        target_id=target_id,
        backup_id="bk_deg",
        policy_id=policy_id,
        observed_at="2026-08-15T00:00:00Z",
        result="failed",
    )
    res_scrub = backup_dr_readiness.evaluate_scope_readiness(target_id, policy_id)
    assert res_scrub["status"] == "degraded"
    assert "scrub-failed" in res_scrub["reasons"]

    # Failed drill marks degraded
    backup_dr_ledger.record_drill_evidence(
        target_id=target_id,
        policy_id=policy_id,
        backup_id="bk_deg",
        observed_at="2026-08-15T00:00:00Z",
        result="failed",
    )
    res_drill = backup_dr_readiness.evaluate_scope_readiness(target_id, policy_id)
    assert res_drill["status"] == "degraded"
    assert "drill-failed" in res_drill["reasons"]


def test_readiness_status_rollup_worst_status(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_policies.backup_targets, "get_target", lambda tid: {"targetId": tid, "kind": "filesystem"})
    # Target 1 is available
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="policy_1",
        backup_id="bk_t1",
        committed_at="2026-08-15T00:00:00Z",
        snapshot_kind="full",
        parent_backup_id=None,
        chain_digest="h1",
        chain_length=1,
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
        verified_at="2026-08-15T00:00:00Z",
        storage_protocol="object-set-v1",
    )

    # Target 2 is blocked (no recovery points)
    # Register policy p2 targeting target_2
    backup_policies.create_policy(
        {
            "policyId": "p2",
            "name": "Policy 2",
            "targetId": "target_2",
        }
    )

    status = backup_dr_readiness.readiness_status()
    # Workspace status should rollup to worst ("blocked" because p2 on t2 is blocked)
    assert status["status"] == "blocked"
    assert "scopes" in status
    assert len(status["scopes"]) >= 2
    assert "recoveryLeases" in status
