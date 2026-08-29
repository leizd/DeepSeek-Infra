"""Time-windowed Fleet SLO and risk-debt score (4.7.5 Gate E)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import (
    resilience_fleet_readiness,
    resilience_risk_observations,
    resilience_slo_ledger,
)


def test_fleet_slo_exposes_named_windows_and_insufficient_data(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=40)
    resilience_slo_ledger.record_sample(
        resilience_slo_ledger.REPAIR_TIME_MS,
        900.0,
        observed_at=old,
        sample_key="ancient-repair",
    )
    resilience_slo_ledger.record_sample(
        resilience_slo_ledger.REPAIR_TIME_MS,
        100.0,
        observed_at=now - timedelta(minutes=10),
        sample_key="recent-repair",
    )
    snapshot = resilience_slo_ledger.get_fleet_slo_snapshot(now=now)
    assert set(snapshot["windows"]) >= {"1h", "24h", "7d", "30d", "lifetime"}
    assert snapshot["windows"]["1h"][resilience_slo_ledger.REPAIR_TIME_MS]["status"] == "OK"
    assert snapshot["windows"]["1h"][resilience_slo_ledger.REPAIR_TIME_MS]["samples"] == 1
    assert snapshot["windows"]["lifetime"][resilience_slo_ledger.REPAIR_TIME_MS]["samples"] == 2
    empty = snapshot["windows"]["1h"].get(resilience_slo_ledger.REBALANCE_TIME_MS)
    assert empty is None or empty["status"] == "INSUFFICIENT_DATA"
    missing = resilience_slo_ledger._window_metric_summary([], now=now, window_seconds=3600.0)  # noqa: SLF001
    assert missing["status"] == "INSUFFICIENT_DATA"
    assert missing["p95"] is None
    assert snapshot["repairP95Ms"] == 100.0


def test_readiness_risk_debt_total_is_score_not_count(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "debt",
            "risks": [
                {"type": "REPLICA_LAG", "severity": "critical", "policyId": "p-debt", "backupId": "b1"},
                {"type": "DR_STALENESS", "severity": "warning", "policyId": "p-debt-2"},
            ],
        },
        now=now - timedelta(days=2),
    )
    readiness = resilience_fleet_readiness.get_fleet_readiness(now=now)
    assert readiness["riskDebt"]["openCount"] == 2
    assert readiness["riskDebt"]["total"] > 2
    assert readiness["riskDebt"]["critical"] == 1
