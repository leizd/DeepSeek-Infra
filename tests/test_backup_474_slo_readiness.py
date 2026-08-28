"""Durable Fleet SLO, burn-rate, maintenance-window, and readiness contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    resilience_action_journal,
    resilience_fleet_readiness,
    resilience_fleet_scheduler,
    resilience_risk_engine,
    resilience_risk_observations,
    resilience_scheduler_service,
    resilience_slo_ledger,
)
from deepseek_infra.web.server import create_server


def _action(action_id: str, action_type: str, *, severity: str = "warning") -> dict[str, object]:
    return {
        "actionId": action_id,
        "type": action_type,
        "severity": severity,
        "parameters": {
            "policyId": f"policy-{action_id}",
            "backupId": f"backup-{action_id}",
            "targetId": f"target-{action_id}",
            "maintenanceWindow": {"timezone": "UTC", "start": "01:00", "end": "05:00"},
        },
    }


def test_fleet_slo_samples_persist_and_compute_percentiles(tmp_settings: Path) -> None:
    observed = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for index, value in enumerate((100.0, 200.0, 300.0)):
        resilience_slo_ledger.record_sample(
            resilience_slo_ledger.RISK_CLEAR_LATENCY_MS,
            value,
            observed_at=observed + timedelta(seconds=index),
            sample_key=f"risk-clear-{index}",
        )
    resilience_slo_ledger.record_sample(
        resilience_slo_ledger.REPAIR_TIME_MS,
        450.0,
        observed_at=observed,
        sample_key="repair-one",
    )

    # A new connection to the same SQLite file is the restart boundary.
    samples = resilience_slo_ledger.list_samples(resilience_slo_ledger.RISK_CLEAR_LATENCY_MS)
    snapshot = resilience_slo_ledger.get_fleet_slo_snapshot(now=observed + timedelta(minutes=1))

    assert [sample["value"] for sample in samples] == [100.0, 200.0, 300.0]
    assert snapshot["riskClearP95Ms"] == pytest.approx(290.0)
    assert snapshot["repairP95Ms"] == 450.0
    assert snapshot["sampleCounts"][resilience_slo_ledger.RISK_CLEAR_LATENCY_MS] == 3


def test_fast_and_slow_error_budget_burn_rates_are_computed(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    resilience_slo_ledger.record_burn_observation(
        resilience_slo_ledger.CRITICAL_DURABILITY_RISK_MINUTES,
        bad_units=20,
        total_units=100,
        observed_at=now - timedelta(minutes=30),
        observation_key="fast-critical-risk",
    )
    resilience_slo_ledger.record_burn_observation(
        resilience_slo_ledger.CRITICAL_DURABILITY_RISK_MINUTES,
        bad_units=180,
        total_units=2300,
        observed_at=now - timedelta(hours=2),
        observation_key="slow-critical-risk",
    )

    result = resilience_slo_ledger.compute_burn_rates(now=now)

    assert result["fast"] == pytest.approx(20.0)
    assert result["slow"] == pytest.approx(200.0 / 2400.0 / 0.01)
    assert result["status"] == "CRITICAL"
    assert result["criticalIndicators"] == [resilience_slo_ledger.CRITICAL_DURABILITY_RISK_MINUTES]


def test_risk_clear_and_action_claim_takeover_are_measured(tmp_settings: Path) -> None:
    opened_at = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
    risk = {
        "type": "REPLICA_LAG",
        "severity": "critical",
        "policyId": "policy-slo",
        "backupId": "backup-slo",
        "targetId": "target-slo",
        "detectedAt": "2026-08-19T23:59:55Z",
    }
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "open", "risks": [risk]},
        now=opened_at,
    )
    cleared = dict(risk)
    cleared["severity"] = "healthy"
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "clear", "risks": [cleared]},
        now=opened_at + timedelta(seconds=30),
    )

    action = _action("takeover-slo", "CREATE_REPAIR_JOB", severity="critical")
    resilience_action_journal.record_action_intent(action, now=opened_at)
    claimed, first, _ = resilience_action_journal.admit_and_claim_action(
        "takeover-slo",
        owner_instance_id="worker-a",
        lease_seconds=5,
        now=opened_at + timedelta(seconds=10),
        enforce_budgets=False,
    )
    assert claimed and first is not None
    takeover, second, _ = resilience_action_journal.admit_and_claim_action(
        "takeover-slo",
        owner_instance_id="worker-b",
        lease_seconds=5,
        now=opened_at + timedelta(seconds=17),
        enforce_budgets=False,
    )
    assert takeover and second is not None

    assert resilience_slo_ledger.list_samples(resilience_slo_ledger.RISK_DETECTION_LATENCY_MS)[0]["value"] == 5000.0
    assert resilience_slo_ledger.list_samples(resilience_slo_ledger.RISK_CLEAR_LATENCY_MS)[0]["value"] == 30000.0
    assert resilience_slo_ledger.list_samples(resilience_slo_ledger.REMEDIATION_QUEUE_DELAY_MS)[0]["value"] == 10000.0
    assert resilience_slo_ledger.list_samples(resilience_slo_ledger.LEASE_TAKEOVER_TIME_MS)[0]["value"] == 2000.0


def test_maintenance_windows_block_background_work_and_allow_critical_overrides(tmp_settings: Path) -> None:
    outside = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    rebalance = _action("rebalance-window", "CREATE_REBALANCE_JOB")
    repair = _action("repair-window", "CREATE_REPAIR_JOB", severity="critical")
    drill = _action("drill-window", "START_DR_DRILL", severity="critical")
    drill["parameters"]["drStalenessCritical"] = True  # type: ignore[index]

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": "maintenance", "risks": []},
        candidate_actions=[rebalance, repair, drill],
        now=outside,
    )

    blocked = {item["actionId"]: item for item in schedule["unschedulableActions"]}
    assert blocked["rebalance-window"]["unschedulableReason"] == "OUTSIDE_MAINTENANCE_WINDOW"
    assigned = {item["actionId"]: item for wave in schedule["executionWaves"] for item in wave["actions"]}
    assert assigned["repair-window"]["maintenanceWindowDecision"]["reason"] == "CRITICAL_DURABILITY_OVERRIDE"
    assert assigned["drill-window"]["maintenanceWindowDecision"]["reason"] == "CRITICAL_DR_STALENESS_OVERRIDE"


def test_terminal_repair_records_duration_and_remediation_outcome(tmp_settings: Path) -> None:
    started = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
    action = _action("repair-duration", "CREATE_REPAIR_JOB", severity="critical")
    resilience_action_journal.record_action_intent(action, now=started)
    claimed, claimed_action, _ = resilience_action_journal.admit_and_claim_action(
        "repair-duration",
        owner_instance_id="worker-duration",
        now=started + timedelta(seconds=10),
        enforce_budgets=False,
    )
    assert claimed and claimed_action is not None

    resilience_action_journal.update_action_state(
        "repair-duration",
        "SUCCEEDED",
        execution_epoch=int(claimed_action["executionEpoch"]),
        claim_token=str(claimed_action["claimToken"]),
        now=started + timedelta(seconds=40),
    )

    duration = resilience_slo_ledger.list_samples(resilience_slo_ledger.REPAIR_TIME_MS)
    assert duration[0]["value"] == 30000.0
    burn = resilience_slo_ledger.compute_burn_rates(now=started + timedelta(seconds=40))
    assert burn["byIndicator"][resilience_slo_ledger.FAILED_REMEDIATION_RATIO]["fast"] == 0.0


def test_fleet_readiness_api_is_authenticated_and_source_backed(tmp_settings: Path) -> None:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    resilience_slo_ledger.record_sample(
        resilience_slo_ledger.REPAIR_TIME_MS,
        1250,
        observed_at=now,
        sample_key="readiness-repair",
    )
    resilience_slo_ledger.record_evidence_verification(
        proof_sha256="a" * 64,
        scenario="real-three-minio-autonomous-remediation",
        verified_at=now - timedelta(seconds=10),
    )
    resilience_scheduler_service.record_schedule_snapshot(
        {
            "scheduleId": "schedule-readiness",
            "executionWaves": [{"waveIndex": 0, "actions": [{"actionId": "a"}]}],
            "deferredCount": 0,
            "unschedulableCount": 0,
        },
        scheduled_at=now,
    )

    samples_before_get = resilience_slo_ledger.list_samples(resilience_slo_ledger.DR_READINESS_AGE_HOURS)
    direct = resilience_fleet_readiness.get_fleet_readiness(now=now)
    assert direct["slo"]["repairP95Ms"] == 1250.0
    assert direct["scheduler"]["waves"][0]["waveIndex"] == 0
    assert direct["evidence"]["proofFreshnessSeconds"] == 10.0

    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert client.get("/api/workspace/resilience/readiness").status_code == 401
    response = client.get(
        "/api/workspace/resilience/readiness",
        headers={"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"},
    )
    assert response.status_code == 200
    assert response.json()["slo"]["repairP95Ms"] == 1250.0
    assert resilience_slo_ledger.list_samples(resilience_slo_ledger.DR_READINESS_AGE_HOURS) == samples_before_get


def test_risk_control_loop_persists_dr_freshness_without_operator_read(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        resilience_risk_engine,
        "evaluate_dr_freshness_risk",
        lambda **_kwargs: {
            "type": "DR_STALENESS",
            "severity": "healthy",
            "confidence": "verified",
            "evidence": ["dr-drill-fresh:2.0d<=7d"],
            "details": {"lastSuccessfulDrillAgeDays": 2.0},
        },
    )
    monkeypatch.setattr(
        resilience_risk_engine,
        "evaluate_restore_latency_risk",
        lambda **_kwargs: {
            "type": "RESTORE_LATENCY_BREACH",
            "severity": "healthy",
            "confidence": "high",
            "evidence": ["restore-latency-within-bounds"],
            "details": {},
        },
    )
    monkeypatch.setattr(
        resilience_risk_engine,
        "evaluate_repair_backlog_risk",
        lambda: {
            "type": "REPAIR_BACKLOG",
            "severity": "healthy",
            "confidence": "high",
            "evidence": ["repair-backlog-clear"],
            "details": {},
        },
    )
    monkeypatch.setattr(
        resilience_risk_engine,
        "evaluate_authority_risk",
        lambda: {
            "type": "AUTHORITY_DEGRADATION",
            "severity": "healthy",
            "confidence": "verified",
            "evidence": ["authority-consensus-verified"],
            "details": {},
        },
    )

    resilience_risk_engine.assess_risks(target_ids=[], policy_ids=[], now=now)

    samples = resilience_slo_ledger.list_samples(resilience_slo_ledger.DR_READINESS_AGE_HOURS)
    assert len(samples) == 1
    assert samples[0]["value"] == 48.0
    assert samples[0]["sampleKey"] == "dr-readiness:2026-08-28T12:00:00Z"


def test_fleet_readiness_classifies_durable_critical_and_degraded_risks(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    assert resilience_fleet_readiness._parse_iso(None) is None  # noqa: SLF001
    assert resilience_fleet_readiness._parse_iso("not-a-time") is None  # noqa: SLF001
    assert resilience_fleet_readiness._parse_iso("2026-08-28T12:00:00") is None  # noqa: SLF001

    critical = {
        "type": "REPLICA_LAG",
        "severity": "critical",
        "policyId": "policy-readiness",
        "backupId": "backup-readiness",
        "targetId": "target-readiness",
    }
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "critical", "risks": [critical]},
        now=now - timedelta(hours=2),
    )
    critical_readiness = resilience_fleet_readiness.get_fleet_readiness(now=now)
    assert critical_readiness["fleetReadiness"] == "CRITICAL"
    assert critical_readiness["riskDebt"]["critical"] == 1
    assert critical_readiness["riskDebt"]["oldestRiskAgeSeconds"] == 7200.0

    warning = {**critical, "severity": "warning"}
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "warning", "risks": [warning]},
        now=now,
    )
    monkeypatch.setattr(
        resilience_slo_ledger,
        "compute_burn_rates",
        lambda **_kwargs: {"status": "OK", "fast": 0.0, "slow": 0.0},
    )
    degraded_readiness = resilience_fleet_readiness.get_fleet_readiness(now=now)
    assert degraded_readiness["fleetReadiness"] == "DEGRADED"
    assert degraded_readiness["riskDebt"]["critical"] == 0


def test_slo_ledger_fails_closed_on_invalid_samples_and_empty_budget_windows(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    assert resilience_slo_ledger._parse_iso("not-a-time") is None  # noqa: SLF001
    assert resilience_slo_ledger._parse_iso("2026-08-28T12:00:00") is None  # noqa: SLF001
    assert resilience_slo_ledger._percentile([], 0.95) == 0.0  # noqa: SLF001

    with pytest.raises(ValueError, match="metric_name is required"):
        resilience_slo_ledger.record_sample("", 1)
    with pytest.raises(ValueError, match="finite and non-negative"):
        resilience_slo_ledger.record_sample("invalid", -1)
    with pytest.raises(ValueError, match="finite and non-negative"):
        resilience_slo_ledger.record_sample("invalid", float("inf"))

    resilience_slo_ledger.record_sample("filtered", 1, observed_at=now, sample_key="filtered-one")
    assert resilience_slo_ledger.list_samples(since=now + timedelta(seconds=1)) == []

    with pytest.raises(ValueError, match="bad_units cannot exceed total_units"):
        resilience_slo_ledger.record_burn_observation("invalid", bad_units=2, total_units=1)
    resilience_slo_ledger.record_burn_observation(
        "zero-total",
        bad_units=0,
        total_units=0,
        observed_at=now,
        observation_key="zero-total",
    )
    burn = resilience_slo_ledger.compute_burn_rates(now=now)
    assert burn["byIndicator"]["zero-total"] == {"fast": 0.0, "slow": 0.0}
    with pytest.raises(ValueError, match="errorBudgetFraction"):
        resilience_slo_ledger.compute_burn_rates(now=now, config_override={"errorBudgetFraction": 0.0})
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        resilience_slo_ledger.record_evidence_verification(proof_sha256="INVALID", scenario="invalid")
