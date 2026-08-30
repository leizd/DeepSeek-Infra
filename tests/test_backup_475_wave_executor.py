"""Durable multi-wave execution and per-wave revalidation (Gates C-D)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    resilience_fresh_state,
    resilience_scheduler_service,
    resilience_wave_executor,
)


def _schedule() -> dict[str, object]:
    return {
        "scheduleId": "wave-sched",
        "riskDigest": "risk-aaa",
        "authorityHeadDigest": "auth-aaa",
        "executionWaves": [
            {
                "waveIndex": 0,
                "actions": [
                    {"actionId": "repair-a", "parameters": {"policyId": "p1", "estimatedBytes": 100}},
                ],
            },
            {
                "waveIndex": 1,
                "actions": [
                    {"actionId": "rebalance-a", "parameters": {"policyId": "p1", "estimatedBytes": 200}},
                ],
            },
        ],
    }


def _fresh_bundle(
    *,
    risk_digest: str = "risk-aaa",
    authority_digest: str = "auth-aaa",
    budget_admitted: bool = True,
    blast_passed: bool = True,
) -> dict[str, object]:
    return {
        "authorityHeadDigest": authority_digest,
        "riskDigest": risk_digest,
        "authorityState": {"workersAllowed": True, "mutationsAllowed": True},
        "maintenanceDecisions": [{"actionId": "test", "allowed": True}],
        "budgets": {"admitted": budget_admitted},
        "blastSimulation": {"passed": blast_passed},
        "freshStateBundleDigest": "f" * 64,
    }


def _use_fresh_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    risk_digest: str = "risk-aaa",
    authority_digest: str = "auth-aaa",
    budget_admitted: bool = True,
    blast_passed: bool = True,
) -> None:
    monkeypatch.setattr(
        resilience_fresh_state,
        "build_fresh_state_bundle",
        lambda schedule, wave_actions, *, now=None: _fresh_bundle(
            risk_digest=risk_digest,
            authority_digest=authority_digest,
            budget_admitted=budget_admitted,
            blast_passed=blast_passed,
        ),
    )


def test_wave_one_cannot_start_before_wave_zero_verified(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resilience_wave_executor.persist_planned_schedule(_schedule(), authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch)
    blocked = resilience_wave_executor.admit_wave("wave-sched", 1)
    assert blocked["admitted"] is False
    assert blocked["reason"] == "PREDECESSOR_WAVE_NOT_VERIFIED"
    first = resilience_wave_executor.admit_wave("wave-sched", 0)
    assert first["admitted"] is True
    still_blocked = resilience_wave_executor.admit_wave("wave-sched", 1)
    assert still_blocked["admitted"] is False


def test_verified_wave_zero_releases_wave_one_and_charges_bytes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _schedule()
    assigned = [
        {"actionId": "repair-a", "parameters": {"policyId": "p1", "estimatedBytes": 100}},
        {"actionId": "rebalance-a", "parameters": {"policyId": "p1", "estimatedBytes": 200}},
    ]
    resilience_scheduler_service.record_schedule_result(plan, assigned)
    resilience_wave_executor.persist_planned_schedule(plan, authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch)
    resilience_wave_executor.admit_wave("wave-sched", 0)
    verified = resilience_wave_executor.verify_wave_action(
        "wave-sched",
        "repair-a",
        success=True,
        actual_bytes=50,
        actual_duration_ms=9,
        actual_traffic_class="repair",
    )
    assert verified["status"] == "VERIFIED_SUCCESS"
    second = resilience_wave_executor.admit_wave("wave-sched", 1)
    assert second["admitted"] is True
    state = resilience_scheduler_service.get_policy_service("p1")
    assert state is not None and state["bytesServed"] == 50
    assert state["actionsServed"] == 1


def test_failed_wave_pauses_downstream_and_stale_requires_replan(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _schedule()
    resilience_scheduler_service.record_schedule_result(plan, plan["executionWaves"][0]["actions"])  # type: ignore[index]
    resilience_wave_executor.persist_planned_schedule(plan, authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch)
    resilience_wave_executor.admit_wave("wave-sched", 0)
    failed = resilience_wave_executor.verify_wave_action("wave-sched", "repair-a", success=False)
    assert failed["status"] == "FAILED"
    downstream = resilience_wave_executor.admit_wave("wave-sched", 1)
    assert downstream["admitted"] is False
    assert downstream["reason"] == "FAILED"

    other = {
        "scheduleId": "stale-sched",
        "riskDigest": "old-risk",
        "authorityHeadDigest": "old-auth",
        "executionWaves": [{"waveIndex": 0, "actions": [{"actionId": "stale-a", "parameters": {"policyId": "p2"}}]}],
    }
    resilience_scheduler_service.record_schedule_result(other, other["executionWaves"][0]["actions"])  # type: ignore[index]
    resilience_wave_executor.persist_planned_schedule(other, authority_head_digest="old-auth")
    _use_fresh_bundle(monkeypatch, risk_digest="new-risk", authority_digest="old-auth")
    stale = resilience_wave_executor.admit_wave("stale-sched", 0)
    assert stale["admitted"] is False
    assert stale["status"] == "PAUSED_REPLAN"
    assert stale["reason"] == "STALE"
    assert resilience_scheduler_service.get_reservation("stale-a")["status"] == "RELEASED"  # type: ignore[index]


def test_wave_revalidates_authority_budget_and_blast(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def plan_for(schedule_id: str) -> dict[str, object]:
        payload = _schedule()
        payload["scheduleId"] = schedule_id
        payload["executionWaves"] = [
            {"waveIndex": 0, "actions": [{"actionId": f"{schedule_id}-a", "parameters": {"policyId": "p1"}}]}
        ]
        return payload

    auth_plan = plan_for("auth-sched")
    resilience_wave_executor.persist_planned_schedule(auth_plan, authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch, authority_digest="auth-bbb")
    authority = resilience_wave_executor.admit_wave("auth-sched", 0)
    assert authority["admitted"] is False
    blast_plan = plan_for("blast-sched")
    resilience_wave_executor.persist_planned_schedule(blast_plan, authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch, blast_passed=False)
    blast = resilience_wave_executor.admit_wave("blast-sched", 0)
    assert blast["admitted"] is False
    budget_plan = plan_for("budget-sched")
    resilience_wave_executor.persist_planned_schedule(budget_plan, authority_head_digest="auth-aaa")
    _use_fresh_bundle(monkeypatch, budget_admitted=False)
    budget = resilience_wave_executor.admit_wave("budget-sched", 0)
    assert budget["admitted"] is False
    ok_plan = plan_for("ok-sched")
    resilience_wave_executor.persist_planned_schedule(ok_plan, authority_head_digest="auth-aaa")
    preempted = resilience_wave_executor.preempt_wave_action("ok-sched", "ok-sched-a")
    assert preempted["status"] == "PREEMPTED"
    assert resilience_wave_executor.get_schedule("missing") is None
    with pytest.raises(ValueError, match="scheduleId is required"):
        resilience_wave_executor.persist_planned_schedule({})
    with pytest.raises(ValueError, match="unknown schedule"):
        resilience_wave_executor.admit_wave("missing")
    with pytest.raises(ValueError, match="unknown wave action"):
        resilience_wave_executor.verify_wave_action("wave-sched", "nope", success=True)
