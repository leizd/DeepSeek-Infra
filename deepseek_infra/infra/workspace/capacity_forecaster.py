"""Global Recovery Intelligence - Predictive Capacity Planning (4.7.0 P0-6).

Multi-horizon predictive capacity forecasting over 7d, 30d, and 90d intervals
based on elapsed-time physical growth observations and sample time series.
Triggers proactive risk warnings and rebalance recommendations without ever
initiating automatic data deletions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_targets,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def forecast_target_capacity(
    target_id: str,
    *,
    probe: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate multi-horizon (7d, 30d, 90d) capacity forecasts for a target."""
    cap = backup_capacity.get_target_capacity(target_id, probe=probe)
    horizon = backup_capacity.estimate_target_exhaustion_horizon(
        target_id,
        policy_id="",
        probe=probe,
        record_observation=probe,
    )

    total_bytes = cap.get("totalBytes")
    free_bytes = cap.get("freeBytes")
    used_bytes = cap.get("usedBytes")

    if total_bytes is None or free_bytes is None:
        return {
            "target": target_id,
            "targetId": target_id,
            "status": "unconstrained",
            "totalBytes": None,
            "currentUsedBytes": used_bytes,
            "currentUsedPercent": None,
            "growthRateBytesPerDay": None,
            "forecast": {
                "7d": {"usedPercent": None, "usedBytes": None, "confidence": 1.0},
                "30d": {"usedPercent": None, "usedBytes": None, "confidence": 1.0},
                "90d": {"usedPercent": None, "usedBytes": None, "confidence": 1.0},
            },
            "estimatedDaysToFull": None,
            "confidence": "unavailable",
            "recommendations": [],
            "generatedAt": _utc_iso(now),
        }

    daily_rate = float(horizon.get("bytesPerDayP50") or horizon.get("dailyIngressEstimateBytes") or 0.0)
    cur_used = int(used_bytes) if used_bytes is not None else max(0, total_bytes - free_bytes)
    cur_pct = round((cur_used / total_bytes) * 100.0, 2) if total_bytes > 0 else 0.0

    # Project 7d, 30d, 90d
    horizons = [(7, "7d", 0.95), (30, "30d", 0.91), (90, "90d", 0.82)]
    forecast_dict: dict[str, dict[str, Any]] = {}
    recommendations: list[str] = []

    for days, label, base_conf in horizons:
        projected_growth = int(daily_rate * days)
        proj_used = min(total_bytes, cur_used + projected_growth) if daily_rate > 0 else cur_used
        proj_pct = round((proj_used / total_bytes) * 100.0, 2) if total_bytes > 0 else 0.0

        sample_cnt = horizon.get("sampleCount", 0) or 0
        conf_factor = 1.0 if sample_cnt >= 10 else 0.85 if sample_cnt >= 3 else 0.60
        effective_conf = round(base_conf * conf_factor, 2)

        forecast_dict[label] = {
            "usedPercent": proj_pct,
            "usedBytes": proj_used,
            "confidence": effective_conf,
        }

    days_to_full = horizon.get("estimatedDaysToFull")
    if (isinstance(days_to_full, int) and days_to_full < 30) or cur_pct >= 85.0:
        recommendations.append("CREATE_REBALANCE_JOB")
    if (isinstance(days_to_full, int) and days_to_full < 14) or cur_pct >= 90.0:
        recommendations.append("EXPAND_TARGET_STORAGE")

    return {
        "target": target_id,
        "targetId": target_id,
        "status": horizon.get("status", "healthy"),
        "totalBytes": total_bytes,
        "currentUsedBytes": cur_used,
        "currentUsedPercent": cur_pct,
        "growthRateBytesPerDay": int(daily_rate),
        "forecast": forecast_dict,
        "estimatedDaysToFull": days_to_full,
        "daysToSoftWatermark": horizon.get("daysToSoftWatermark"),
        "daysToHardWatermark": horizon.get("daysToHardWatermark"),
        "confidence": horizon.get("confidence", "medium"),
        "recommendations": recommendations,
        "generatedAt": _utc_iso(now),
    }


def forecast_all_targets(
    *,
    probe: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate predictive capacity forecasts across all configured targets."""
    targets = backup_targets.list_targets()
    all_target_ids = ["managed-local"] + [str(t.get("targetId") or "") for t in targets]
    seen: set[str] = set()
    forecasts: list[dict[str, Any]] = []

    overall_status = "healthy"
    for tid in all_target_ids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        fc = forecast_target_capacity(tid, probe=probe, now=now)
        forecasts.append(fc)
        stat = fc.get("status")
        if stat == "critical":
            overall_status = "critical"
        elif stat == "degraded" and overall_status != "critical":
            overall_status = "degraded"

    return {
        "overallStatus": overall_status,
        "targets": forecasts,
        "generatedAt": _utc_iso(now),
    }
