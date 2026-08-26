"""Global Recovery Intelligence - Continuous Resilience Score (4.7.0 P1-3).

Calculates a comprehensive 0-100 DR Credit Score across five weighted factors:
1. DR Drill Recency & Success (25%)
2. Replica Health & Failure Domain Dispersion (25%)
3. Target Capacity Horizons & Watermarks (20%)
4. Restore Performance & RTO SLO (15%)
5. Authority Integrity & Consensus (15%)

Assigns categorical letter grades (A / B / C / D / F) and surfaces actionable weaknesses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_authority_provider,
    backup_capacity,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_targets,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _calculate_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def calculate_resilience_score(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the Continuous Resilience Score (0-100) and detailed factor breakdown."""
    current = now or datetime.now(tz=timezone.utc)
    weaknesses: list[str] = []

    # 1. DR Drill (Weight: 25)
    drills = backup_dr_readiness._drill_records()  # noqa: SLF001
    successful_drills = [d for d in drills if d.get("success") is True or str(d.get("status") or "").lower() == "pass"]
    drill_score = 0
    drill_status = "critical"

    if successful_drills:
        latest = successful_drills[0]
        f_time = backup_dr_readiness._parse_time(latest.get("finishedAt") or latest.get("startedAt"))  # noqa: SLF001
        age_days = (current - f_time).total_seconds() / 86400.0 if f_time else 999.0
        if age_days < 7.0:
            drill_score = 25
            drill_status = "healthy"
        elif age_days <= 30.0:
            drill_score = 15
            drill_status = "warning"
            weaknesses.append("dr-drill-older-than-7-days")
        else:
            drill_score = 5
            drill_status = "critical"
            weaknesses.append("dr-drill-stale-over-30-days")
    else:
        drill_score = 0
        drill_status = "critical"
        weaknesses.append("no-successful-dr-drills")

    # 2. Replica Health (Weight: 25)
    policies = backup_policies.list_policies()
    replica_score = 25
    replica_status = "healthy"

    if not policies:
        replica_score = 25
    else:
        for p in policies:
            pid = str(p.get("policyId") or "")
            repl_cfg = p.get("replication") if isinstance(p, dict) else None
            if isinstance(repl_cfg, dict) and repl_cfg.get("enabled"):
                min_copies = int(repl_cfg.get("minCommittedCopies") or 1)
                latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=pid)
                if not latest_pt:
                    replica_score = min(replica_score, 10)
                    replica_status = "degraded"
                    weaknesses.append(f"policy-{pid}-no-recovery-point")
                    continue
                copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=pid, backup_id=str(latest_pt.get("backupId") or ""))
                committed = [c for c in copies if str(c.get("status") or c.get("state") or "").lower() in {"committed", "active", "healthy"}]
                if len(committed) < min_copies:
                    if len(committed) == 0:
                        replica_score = 0
                        replica_status = "critical"
                        weaknesses.append(f"policy-{pid}-zero-committed-replicas")
                    else:
                        replica_score = min(replica_score, 15)
                        replica_status = "degraded"
                        weaknesses.append(f"policy-{pid}-missing-required-replicas")

    # 3. Capacity (Weight: 20)
    targets = backup_targets.list_targets()
    all_target_ids = ["managed-local"] + [str(t.get("targetId") or "") for t in targets]
    capacity_score = 20
    capacity_status = "healthy"

    for tid in all_target_ids:
        if not tid:
            continue
        cap = backup_capacity.get_target_capacity(tid, probe=False)
        horizon = backup_capacity.estimate_target_exhaustion_horizon(tid, policy_id="", probe=False, record_observation=False)
        free_pct = cap.get("freePercent")
        days_to_full = horizon.get("estimatedDaysToFull")

        if free_pct is not None:
            if free_pct < 5.0 or (isinstance(days_to_full, int) and days_to_full < 7):
                capacity_score = min(capacity_score, 0)
                capacity_status = "critical"
                weaknesses.append(f"{tid}-capacity-exhaustion-imminent")
            elif free_pct < 10.0 or (isinstance(days_to_full, int) and days_to_full < 30):
                capacity_score = min(capacity_score, 8)
                capacity_status = "degraded"
                weaknesses.append(f"{tid}-capacity-under-10-percent")
            elif free_pct <= 20.0:
                capacity_score = min(capacity_score, 14)
                if capacity_status == "healthy":
                    capacity_status = "warning"
                weaknesses.append(f"{tid}-capacity-watermark-warning")

    # 4. Restore Performance (Weight: 15)
    slo = backup_dr_readiness.calculate_dr_slo_metrics(now=current)
    restore_score = 15
    restore_status = "healthy"

    success_rate = slo.get("restoreSuccessRate")
    rto_p95 = slo.get("rtoSecondsP95")

    if success_rate is not None and isinstance(success_rate, (int, float)) and success_rate < 0.99:
        restore_score = 0
        restore_status = "critical"
        weaknesses.append(f"restore-success-rate-low:{success_rate:.1%}")
    elif rto_p95 is not None and isinstance(rto_p95, (int, float)) and rto_p95 > 1800:
        restore_score = 5
        restore_status = "degraded"
        weaknesses.append(f"restore-rto-high:{rto_p95}s")
    elif rto_p95 is not None and isinstance(rto_p95, (int, float)) and rto_p95 > 900:
        restore_score = 10
        restore_status = "warning"
        weaknesses.append(f"restore-rto-warning:{rto_p95}s")

    # 5. Authority Integrity (Weight: 15)
    auth_score = 15
    auth_status = "healthy"

    provider = backup_authority_provider.get_authority_replica_provider()
    if provider is not None:
        try:
            verify_fn = getattr(provider, "verify_authority_consensus", None)
            if callable(verify_fn):
                p_res = verify_fn()
                if isinstance(p_res, dict):
                    if p_res.get("status") == "divergent":
                        auth_score = 0
                        auth_status = "blocked"
                        weaknesses.append("authority-replica-fork-detected")
                    elif p_res.get("status") == "lagging":
                        auth_score = 10
                        auth_status = "warning"
                        weaknesses.append("authority-replica-lag")
            elif provider.configured() and provider.resolved_count() < provider.configured_count():
                auth_score = 5
                auth_status = "degraded"
                weaknesses.append("authority-replicas-unresolved")
        except Exception:
            auth_score = 10
            auth_status = "warning"

    total_score = max(0, min(100, drill_score + replica_score + capacity_score + restore_score + auth_score))
    grade = _calculate_grade(total_score)

    factor_breakdown = {
        "drDrill": {"score": drill_score, "max": 25, "status": drill_status},
        "replicaHealth": {"score": replica_score, "max": 25, "status": replica_status},
        "capacity": {"score": capacity_score, "max": 20, "status": capacity_status},
        "restorePerformance": {"score": restore_score, "max": 15, "status": restore_status},
        "authorityIntegrity": {"score": auth_score, "max": 15, "status": auth_status},
    }

    return {
        "score": total_score,
        "grade": grade,
        "factorBreakdown": factor_breakdown,
        "weaknesses": weaknesses,
        "generatedAt": _utc_iso(current),
    }
