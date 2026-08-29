"""Durable capacity observations, 30/90-day forecast, and backtests (Gates F-H)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    resilience_capacity_forecast,
    resilience_capacity_history,
    resilience_forecast_backtest,
)


def _series(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for index in range(8):
        used = 1000 + index * 100
        resilience_capacity_history.record_capacity_observation(
            "target-a",
            used_bytes=used,
            free_bytes=9000 - index * 100,
            total_bytes=10000,
            observed_at=start + timedelta(days=index),
            backup_bytes_written=10,
            replication_bytes_in=1,
            replication_bytes_out=2,
            rebalance_bytes_in=3,
            rebalance_bytes_out=4,
            active_policies=2,
            observation_key=f"obs-{index}",
        )


def test_capacity_observation_requires_target(tmp_settings: Path) -> None:
    with pytest.raises(ValueError, match="targetId"):
        resilience_capacity_history.record_capacity_observation("", used_bytes=1, free_bytes=1, total_bytes=2)


def test_forecast_uses_durable_observations_and_insufficient_fails_closed(tmp_settings: Path) -> None:
    empty = resilience_capacity_forecast.forecast_target_capacity("missing")
    assert empty["forecastStatus"] == "INSUFFICIENT_DATA"
    assert empty["p50FreeBytes"] is None
    resilience_capacity_history.record_capacity_observation(
        "tiny",
        used_bytes=1,
        free_bytes=9,
        total_bytes=10,
        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    still_empty = resilience_capacity_forecast.forecast_target_capacity("tiny")
    assert still_empty["forecastStatus"] == "INSUFFICIENT_DATA"
    _series(tmp_settings)
    thirty = resilience_capacity_forecast.forecast_target_capacity("target-a", horizon_days=30)
    ninety = resilience_capacity_forecast.forecast_target_capacity("target-a", horizon_days=90)
    assert thirty["forecastStatus"] == "OK"
    assert thirty["horizonDays"] == 30
    assert ninety["horizonDays"] == 90
    assert thirty["p50FreeBytes"] is not None and thirty["p90FreeBytes"] is not None
    assert thirty["p90FreeBytes"] <= thirty["p50FreeBytes"]
    assert thirty["sampleCount"] == 8
    listed = resilience_capacity_history.list_capacity_observations("target-a")
    assert listed[0]["backupBytesWritten"] == 10
    all_targets = resilience_capacity_forecast.forecast_all_targets(horizon_days=30)
    assert all_targets["targets"][0]["targetId"] == "target-a"


def test_forecast_backtest_persists_error_and_lowers_confidence(tmp_settings: Path) -> None:
    _series(tmp_settings)
    resilience_forecast_backtest.record_forecast_backtest(
        "target-a",
        predicted_p50_free=8000,
        predicted_p90_free=7000,
        actual_free=1000,
        horizon_days=30,
        forecasted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        evaluated_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    summary = resilience_forecast_backtest.summarize_backtest("target-a")
    assert summary["mae"] == 7000.0
    assert summary["bias"] > 0
    assert summary["overoptimistic"] is True
    forecast = resilience_capacity_forecast.forecast_target_capacity("target-a", horizon_days=30)
    assert forecast["confidence"] == "low"
    empty = resilience_forecast_backtest.summarize_backtest("none")
    assert empty["samples"] == 0
    assert empty["overoptimistic"] is False
