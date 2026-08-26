"""Global Recovery Intelligence - RPO/RTO Placement Optimizer (4.7.0 P1-1).

Analyzes historical restore latency, transfer bandwidth, and RPO/RTO metrics
across targets and recovery classes to generate non-destructive placement
and restore priority recommendations without directly modifying policies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_policies,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def analyze_target_restore_performance(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Profile historical restore latency and transfer performance per target."""
    drills = backup_dr_readiness._drill_records()  # noqa: SLF001
    target_stats: dict[str, list[float]] = {}

    for d in drills:
        tid = str(d.get("targetId") or "managed-local")
        dur_ms = d.get("durationMs") or d.get("restoreDurationMs")
        if isinstance(dur_ms, (int, float)) and dur_ms > 0:
            target_stats.setdefault(tid, []).append(float(dur_ms) / 1000.0)

    performance: dict[str, dict[str, Any]] = {}
    for tid, latencies in target_stats.items():
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]
        performance[tid] = {
            "sampleCount": len(latencies),
            "p50LatencySeconds": round(p50, 2),
            "p90LatencySeconds": round(p90, 2),
            "minLatencySeconds": round(latencies[0], 2),
            "maxLatencySeconds": round(latencies[-1], 2),
        }

    return performance


def generate_placement_recommendations(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Produce non-destructive RPO/RTO optimization recommendations."""
    perf = analyze_target_restore_performance(now=now)
    policies = backup_policies.list_policies()
    recommendations: list[dict[str, Any]] = []

    # Identify best performing targets by P90 restore latency
    sorted_targets = sorted(
        [(tid, stats["p90LatencySeconds"], stats["sampleCount"]) for tid, stats in perf.items() if stats.get("sampleCount", 0) >= 2],
        key=lambda x: x[1],
    )

    if len(sorted_targets) >= 2:
        fastest_tid, fastest_rto, _ = sorted_targets[0]
        slowest_tid, slowest_rto, _ = sorted_targets[-1]

        if slowest_rto > fastest_rto * 1.5:  # 50% slower
            for p in policies:
                pid = str(p.get("policyId") or "")
                pol_target = str(p.get("targetId") or "managed-local")
                if pol_target == slowest_tid:
                    recommendations.append(
                        {
                            "type": "PREFERRED_RESTORE_TARGET_ADVISORY",
                            "policyId": pid,
                            "currentPrimaryTarget": slowest_tid,
                            "recommendedTarget": fastest_tid,
                            "reason": f"Target '{fastest_tid}' demonstrates lower P90 restore latency ({fastest_rto:.1f}s) vs '{slowest_tid}' ({slowest_rto:.1f}s)",
                            "currentP90RtoSeconds": slowest_rto,
                            "projectedP90RtoSeconds": fastest_rto,
                            "confidence": "high",
                        }
                    )

    return {
        "optimizerVersion": 1,
        "generatedAt": _utc_iso(now),
        "recommendations": recommendations,
        "targetPerformance": perf,
    }
