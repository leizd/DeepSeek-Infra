"""Durable Risk Observation and persistent fair scheduler contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import (
    resilience_fleet_scheduler,
    resilience_planner,
    resilience_risk_observations,
    resilience_scheduler_service,
)


def _snapshot(*, severity: str, generated_at: datetime) -> dict[str, object]:
    return {
        "riskSnapshotVersion": 1,
        "riskDigest": f"digest-{severity}-{generated_at.timestamp()}",
        "generatedAt": generated_at.isoformat(),
        "overallRisk": severity,
        "risks": [
            {
                "type": "DR_STALENESS",
                "policyId": "policy-risk",
                "backupId": "backup-risk",
                "targetId": "target-risk",
                "failureDomain": "zone-risk",
                "severity": severity,
                "confidence": "verified",
                "evidence": [f"risk-{severity}"],
            }
        ],
    }


def test_risk_first_seen_persists_and_debt_ages_across_planner_runs(tmp_settings: Path) -> None:
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    later = first + timedelta(days=14)
    first_snapshot = _snapshot(severity="degraded", generated_at=first)
    later_snapshot = _snapshot(severity="degraded", generated_at=later)

    resilience_risk_observations.observe_risk_snapshot(first_snapshot, now=first)
    plan_first = resilience_planner.plan_resilience_actions(first_snapshot)
    resilience_risk_observations.observe_risk_snapshot(later_snapshot, now=later)
    plan_later = resilience_planner.plan_resilience_actions(later_snapshot)

    first_action = plan_first["actions"][0]
    later_action = plan_later["actions"][0]
    assert first_action["riskSubjectDigest"] == later_action["riskSubjectDigest"]
    assert first_action["riskFirstSeenAt"] == later_action["riskFirstSeenAt"]
    assert later_action["riskObservationCount"] == 2
    debt = resilience_fleet_scheduler.compute_risk_debt(later_action, now=later)
    assert debt["ageDays"] == 14.0
    assert debt["ageSource"] == "risk-observation-ledger"


def test_cleared_risk_stops_debt_and_reopen_uses_new_open_interval(tmp_settings: Path) -> None:
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cleared_at = first + timedelta(days=3)
    reopened_at = first + timedelta(days=10)
    opened = _snapshot(severity="warning", generated_at=first)
    cleared = _snapshot(severity="healthy", generated_at=cleared_at)
    reopened = _snapshot(severity="critical", generated_at=reopened_at)

    resilience_risk_observations.observe_risk_snapshot(opened, now=first)
    cleared_records = resilience_risk_observations.observe_risk_snapshot(cleared, now=cleared_at)
    assert cleared_records[0]["status"] == "CLEARED"
    assert cleared_records[0]["lastClearedAt"].startswith("2026-08-04")

    reopened_records = resilience_risk_observations.observe_risk_snapshot(reopened, now=reopened_at)
    record = reopened_records[0]
    assert record["status"] == "REOPENED"
    assert record["reopenCount"] == 1
    assert record["firstSeenAt"].startswith("2026-08-01")
    assert record["openSinceAt"].startswith("2026-08-11")

    plan = resilience_planner.plan_resilience_actions(reopened)
    action = plan["actions"][0]
    assert action["riskLifecycleFirstSeenAt"].startswith("2026-08-01")
    assert action["riskFirstSeenAt"].startswith("2026-08-11")
    assert resilience_fleet_scheduler.compute_risk_debt(action, now=reopened_at)["ageSeconds"] == 0.0


def test_production_scheduler_uses_and_updates_persistent_fairness(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    resilience_scheduler_service.record_scheduled_actions(
        [
            {
                "actionId": f"served-a-{index}",
                "type": "CREATE_REPAIR_JOB",
                "parameters": {"policyId": "policy-a", "backupId": f"backup-a-{index}", "destTargetId": f"target-a-{index}"},
            }
            for index in range(5)
        ],
        scheduled_at=now - timedelta(minutes=10),
    )
    actions = [
        {
            "actionId": "next-a",
            "type": "CREATE_REPAIR_JOB",
            "severity": "warning",
            "riskFirstSeenAt": now.isoformat(),
            "parameters": {"policyId": "policy-a", "backupId": "next-backup-a", "destTargetId": "next-target-a"},
        },
        {
            "actionId": "next-b",
            "type": "CREATE_REPAIR_JOB",
            "severity": "warning",
            "riskFirstSeenAt": now.isoformat(),
            "parameters": {"policyId": "policy-b", "backupId": "next-backup-b", "destTargetId": "next-target-b"},
        },
    ]

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "risk-fairness", "risks": []},
        candidate_actions=actions,
        now=now,
    )

    ordered_ids = [action["actionId"] for wave in schedule["executionWaves"] for action in wave["actions"]]
    assert ordered_ids[0] == "next-b"
    state_a = resilience_scheduler_service.get_policy_service("policy-a")
    state_b = resilience_scheduler_service.get_policy_service("policy-b")
    assert state_a is not None and state_a["actionsServed"] == 6
    assert state_b is not None and state_b["actionsServed"] == 1
    assert state_a["virtualRuntime"] > state_b["virtualRuntime"]
