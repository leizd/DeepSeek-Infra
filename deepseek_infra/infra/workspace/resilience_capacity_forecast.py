"""30/90-day P50/P90 capacity forecasts from durable observations (4.7.5 Gate G)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import resilience_capacity_history

MIN_SAMPLES = 3
MIN_SPAN_SECONDS = 86400.0
WARNING_FREE_FRACTION = 0.20


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _insufficient(
    target_id: str,
    horizon_days: int,
    *,
    now: datetime,
    sample_count: int,
    window_days: float,
    target_incarnation: str | None,
    capacity_revision: str | None,
    observation_set_digest: str,
) -> dict[str, Any]:
    payload = {
        "targetId": target_id,
        "targetIncarnation": target_incarnation,
        "capacityRevision": capacity_revision,
        "horizonDays": horizon_days,
        "forecastStatus": "INSUFFICIENT_DATA",
        "p50FreeBytes": None,
        "p90FreeBytes": None,
        "daysToWarningWatermarkP50": None,
        "daysToWarningWatermarkP90": None,
        "observationWindowDays": round(window_days, 3),
        "sampleCount": sample_count,
        "confidence": "unavailable",
        "calibration": {"samples": 0, "mae": None, "mape": None, "bias": None, "intervalCoverage": None},
        "capacityObservationSetDigest": observation_set_digest,
        "generatedAt": _utc_iso(now),
    }
    payload["forecastDigest"] = _digest(payload)
    return payload


def _days_to_watermark(free_bytes: int, total_bytes: int, bytes_per_day: float) -> float | None:
    if bytes_per_day <= 0 or total_bytes <= 0:
        return None
    watermark = int(total_bytes * WARNING_FREE_FRACTION)
    remaining = free_bytes - watermark
    if remaining <= 0:
        return 0.0
    return round(remaining / bytes_per_day, 3)


def forecast_target_capacity(
    target_id: str,
    *,
    horizon_days: int = 90,
    now: datetime | None = None,
    target_incarnation: str | None = None,
    capacity_revision: str | None = None,
) -> dict[str, Any]:
    """Forecast P50/P90 free-space headroom. Never invents a trend from insufficient samples."""
    current = now or datetime.now(tz=timezone.utc)
    horizon = max(1, int(horizon_days))
    series = resilience_capacity_history.latest_capacity_series(target_id)
    incarnation = target_incarnation if target_incarnation is not None else (series or {}).get("targetIncarnation")
    revision = capacity_revision if capacity_revision is not None else (series or {}).get("capacityRevision")
    observations = resilience_capacity_history.list_capacity_observations(
        target_id,
        target_incarnation=incarnation,
        capacity_revision=revision,
    )
    observation_set_digest = _digest(
        {
            "targetId": target_id,
            "targetIncarnation": incarnation,
            "capacityRevision": revision,
            "observations": [
                {"observationKey": item["observationKey"], "observationDigest": item["observationDigest"]}
                for item in observations
            ],
        }
    )
    if len(observations) < MIN_SAMPLES:
        return _insufficient(
            target_id,
            horizon,
            now=current,
            sample_count=len(observations),
            window_days=0.0,
            target_incarnation=incarnation,
            capacity_revision=revision,
            observation_set_digest=observation_set_digest,
        )
    first_at = _parse_iso(observations[0]["observedAt"])
    last_at = _parse_iso(observations[-1]["observedAt"])
    if first_at is None or last_at is None:
        return _insufficient(
            target_id,
            horizon,
            now=current,
            sample_count=len(observations),
            window_days=0.0,
            target_incarnation=incarnation,
            capacity_revision=revision,
            observation_set_digest=observation_set_digest,
        )
    span_seconds = max(0.0, (last_at - first_at).total_seconds())
    window_days = span_seconds / 86400.0
    if span_seconds < MIN_SPAN_SECONDS:
        return _insufficient(
            target_id,
            horizon,
            now=current,
            sample_count=len(observations),
            window_days=window_days,
            target_incarnation=incarnation,
            capacity_revision=revision,
            observation_set_digest=observation_set_digest,
        )

    daily_growth: list[float] = []
    for previous, item in zip(observations, observations[1:]):
        prev_at = _parse_iso(previous["observedAt"])
        item_at = _parse_iso(item["observedAt"])
        if prev_at is None or item_at is None:
            continue
        elapsed_days = max(1e-9, (item_at - prev_at).total_seconds() / 86400.0)
        delta = int(item["usedBytes"]) - int(previous["usedBytes"])
        daily_growth.append(delta / elapsed_days)
    if len(daily_growth) < 2:
        return _insufficient(
            target_id,
            horizon,
            now=current,
            sample_count=len(observations),
            window_days=window_days,
            target_incarnation=incarnation,
            capacity_revision=revision,
            observation_set_digest=observation_set_digest,
        )

    p50_rate = _percentile(daily_growth, 0.50)
    p90_rate = _percentile(daily_growth, 0.90)
    latest = observations[-1]
    free_bytes = int(latest["freeBytes"])
    total_bytes = int(latest["totalBytes"])
    p50_free = max(0, int(round(free_bytes - p50_rate * horizon)))
    p90_free = max(0, int(round(free_bytes - p90_rate * horizon)))
    confidence = "high" if len(observations) >= 30 and window_days >= 14 else ("medium" if len(observations) >= 7 else "low")
    from deepseek_infra.infra.workspace import resilience_forecast_backtest

    backtest = resilience_forecast_backtest.summarize_backtest(
        target_id,
        target_incarnation=incarnation,
        capacity_revision=revision,
    )
    if backtest.get("overoptimistic") is True:
        confidence = "low" if confidence != "unavailable" else confidence
    payload = {
        "targetId": target_id,
        "targetIncarnation": incarnation,
        "capacityRevision": revision,
        "horizonDays": horizon,
        "forecastStatus": "OK",
        "p50FreeBytes": p50_free,
        "p90FreeBytes": p90_free,
        "daysToWarningWatermarkP50": _days_to_watermark(free_bytes, total_bytes, p50_rate),
        "daysToWarningWatermarkP90": _days_to_watermark(free_bytes, total_bytes, p90_rate),
        "observationWindowDays": round(window_days, 3),
        "sampleCount": len(observations),
        "confidence": confidence,
        "calibration": {
            "samples": backtest["samples"],
            "mae": backtest["mae"],
            "mape": backtest["mape"],
            "bias": backtest["bias"],
            "intervalCoverage": backtest["intervalCoverage"],
            "calibrationDigest": backtest.get("calibrationDigest"),
        },
        "capacityObservationSetDigest": observation_set_digest,
        "p50GrowthBytesPerDay": round(p50_rate, 3),
        "p90GrowthBytesPerDay": round(p90_rate, 3),
        "generatedAt": _utc_iso(current),
    }
    payload["forecastDigest"] = _digest({k: v for k, v in payload.items() if k != "forecastDigest"})
    return payload


def forecast_all_targets(*, horizon_days: int = 90, now: datetime | None = None) -> dict[str, Any]:
    observations = resilience_capacity_history.list_capacity_observations()
    target_ids = sorted({str(item["targetId"]) for item in observations})
    forecasts = [forecast_target_capacity(target_id, horizon_days=horizon_days, now=now) for target_id in target_ids]
    return {
        "horizonDays": horizon_days,
        "targets": forecasts,
        "generatedAt": _utc_iso(now),
    }
