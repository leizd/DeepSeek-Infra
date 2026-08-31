"""Extra branches for durable planning modules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    resilience_action_journal,
    resilience_capacity_forecast,
    resilience_capacity_history,
    resilience_cost_model,
    resilience_forecast_backtest,
    resilience_fresh_state,
    resilience_placement_optimizer,
    resilience_risk_observations,
    resilience_scheduler_service,
    resilience_wave_executor,
)


def test_wave_executor_skips_malformed_waves_and_lists_state(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maintenance_allowed = True

    def fresh_bundle(schedule: dict[str, object], wave_actions: list[dict[str, object]], *, now: object = None) -> dict[str, object]:
        del wave_actions, now
        return {
            "riskDigest": schedule.get("riskDigest"),
            "authorityHeadDigest": schedule.get("authorityHeadDigest"),
            "authorityState": {"workersAllowed": True, "mutationsAllowed": True},
            "maintenanceDecisions": [{"allowed": maintenance_allowed}],
            "budgets": {"admitted": True},
            "blastSimulation": {"passed": True},
        }

    monkeypatch.setattr(resilience_fresh_state, "build_fresh_state_bundle", fresh_bundle)
    monkeypatch.setattr(
        resilience_action_journal,
        "execute_autonomous_action",
        lambda action_id, **kwargs: {
            "actionId": action_id,
            "state": "SUCCEEDED",
            "executionEpoch": 1,
            "effectHandle": {"kind": "repair", "repairId": f"repair-{action_id}"},
            "verificationResult": {"verified": True},
        },
    )
    monkeypatch.setattr(
        resilience_scheduler_service,
        "settle_action_from_effect",
        lambda action_id, *, consumed_at=None: {"actionId": action_id, "status": "CONSUMED"},
    )
    schedule = {
        "scheduleId": "cov-sched",
        "riskDigest": "risk-cov",
        "authorityHeadDigest": "auth-cov",
        "executionWaves": [
            "not-a-wave",
            {"waveIndex": None, "actions": []},
            {"waveIndex": "x", "actions": []},
            {
                "waveIndex": 0,
                "actions": ["nope", {}, {"actionId": "ok-a", "parameters": {"policyId": "p"}}],
            },
        ],
    }
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest="auth-cov")
    assert resilience_wave_executor.list_waves("cov-sched")[0]["waveIndex"] == 0
    assert [item["actionId"] for item in resilience_wave_executor.list_wave_actions("cov-sched", 0)] == ["ok-a"]
    admitted = resilience_wave_executor.admit_wave("cov-sched")
    assert admitted["admitted"] is True
    again = resilience_wave_executor.admit_wave("cov-sched", 0)
    assert again["admitted"] is False
    with pytest.raises(ValueError, match="unknown wave"):
        resilience_wave_executor.admit_wave("cov-sched", 9)
    verified = resilience_wave_executor.run_next_wave("cov-sched")
    assert verified["status"] == "COMPLETED"
    assert resilience_wave_executor.get_schedule("cov-sched")["status"] == "COMPLETED"  # type: ignore[index]
    empty = resilience_wave_executor.persist_planned_schedule({"scheduleId": "empty-waves", "executionWaves": []})
    none_pending = resilience_wave_executor.admit_wave("empty-waves")
    assert none_pending["admitted"] is False
    assert empty["scheduleId"] == "empty-waves"
    maint = {
        "scheduleId": "maint-sched",
        "riskDigest": "risk-maint",
        "authorityHeadDigest": "auth-maint",
        "executionWaves": [{"waveIndex": 0, "actions": [{"actionId": "maint-a", "parameters": {"policyId": "p"}}]}],
    }
    resilience_wave_executor.persist_planned_schedule(maint, authority_head_digest="auth-maint")
    maintenance_allowed = False
    blocked = resilience_wave_executor.admit_wave("maint-sched", 0)
    assert blocked["admitted"] is False


def test_forecast_helpers_and_insufficient_span(tmp_settings: Path) -> None:
    assert resilience_capacity_forecast._parse_iso(None) is None  # noqa: SLF001
    assert resilience_capacity_forecast._parse_iso("nope") is None  # noqa: SLF001
    assert resilience_capacity_forecast._parse_iso("2026-08-29T00:00:00") is None  # noqa: SLF001
    assert resilience_capacity_forecast._percentile([], 0.5) == 0.0  # noqa: SLF001
    assert resilience_capacity_forecast._days_to_watermark(10, 100, 0) is None  # noqa: SLF001
    assert resilience_capacity_forecast._days_to_watermark(10, 100, 10) == 0.0  # noqa: SLF001
    start = datetime(2026, 8, 29, tzinfo=timezone.utc)
    for index in range(3):
        resilience_capacity_history.record_capacity_observation(
            "span-short",
            used_bytes=10 + index,
            free_bytes=90,
            total_bytes=100,
            observed_at=start + timedelta(minutes=index),
            observation_key=f"short-{index}",
        )
    short = resilience_capacity_forecast.forecast_target_capacity("span-short")
    assert short["forecastStatus"] == "INSUFFICIENT_DATA"
    assert resilience_capacity_history.list_capacity_observations("span-short", since=start)
    resilience_capacity_history.record_capacity_observation(
        "bad-time",
        used_bytes=1,
        free_bytes=1,
        total_bytes=2,
        observed_at=start,
        observation_key="bad-time-1",
    )
    with resilience_capacity_history._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE resilience_capacity_observations SET observed_at = 'not-a-time' WHERE target_id = 'bad-time'"
        )
    invalid = resilience_capacity_forecast.forecast_target_capacity("bad-time")
    assert invalid["forecastStatus"] == "INSUFFICIENT_DATA"


def test_scheduler_and_risk_helpers(tmp_settings: Path) -> None:
    action = {
        "actionId": "cov-res",
        "waveIndex": "nope",
        "executionEpoch": "nope",
        "parameters": {"policyId": "p-cov", "estimatedBytes": 8},
    }
    resilience_scheduler_service.reserve_scheduled_actions([action], schedule_id="sched-cov")
    reservation = resilience_scheduler_service.get_reservation("cov-res")
    assert reservation is not None and reservation["waveIndex"] == 0
    listed = resilience_scheduler_service.list_policy_service()
    assert listed == {}
    filtered = resilience_scheduler_service.list_reservations(schedule_id="sched-cov", status="RESERVED")
    assert filtered[0]["actionId"] == "cov-res"
    expired = resilience_scheduler_service.release_action_reservation("cov-res", reason="EXPIRED")
    assert expired is not None and expired["status"] == "EXPIRED"
    assert resilience_scheduler_service.get_reservation("cov-res")["status"] == "EXPIRED"  # type: ignore[index]
    resilience_scheduler_service.record_schedule_snapshot({"scheduleId": "snap-only", "executionWaves": []})
    assert resilience_scheduler_service.get_latest_schedule_snapshot()["scheduleId"] == "snap-only"  # type: ignore[index]
    subject = {"type": "CAPACITY_EXHAUSTION", "targetId": "t-cov"}
    status, reason = resilience_risk_observations.infer_absent_closure_reason(
        subject,
        coverage={"CAPACITY_EXHAUSTION": {"targets": ["t-cov"], "complete": True}},
    )
    assert status == "CLEARED" and reason == "HEALTHY"
    assert resilience_risk_observations._coverage_entry(None, "REPLICA_LAG") is None  # noqa: SLF001
    assert resilience_risk_observations._coverage_entry({"REPLICA_LAG": "nope"}, "REPLICA_LAG") is None  # noqa: SLF001
    assert resilience_cost_model.get_price_catalog(1) is None
    with pytest.raises(ValueError, match="targetId"):
        resilience_forecast_backtest.record_forecast_backtest("", predicted_p50_free=1, predicted_p90_free=1, actual_free=1, horizon_days=7)
    unsafe = resilience_placement_optimizer.evaluate_candidate(
        {
            "targetId": "t",
            "committedCopies": 3,
            "failureDomains": 2,
            "forecastFreeBytes": 1,
            "breaksDrDependency": True,
            "mutatesAuthority": True,
        },
        baseline={"minCommittedCopies": 1, "minFailureDomains": 1, "committedCopies": 1, "failureDomains": 1, "forecastSafetyHeadroomBytes": 10},
        catalog=None,
    )
    assert "ACTIVE_DR_DEPENDENCY_BROKEN" in unsafe["violations"]
    assert "AUTHORITY_MUTATION_FORBIDDEN" in unsafe["violations"]
    assert "FORECAST_SAFETY_HEADROOM_REDUCED" in unsafe["violations"]
