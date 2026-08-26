"""Global Recovery Intelligence - Risk Assessment Engine (4.7.0 P0-1, P0-2).

Scans operational evidence metrics (capacity watermarks, forecast horizons,
replica lag, DR drill staleness, restore latency SLA compliance, failure
domain compliance, repair backlog, and authority consensus) to generate
a typed, deterministic RiskSnapshot bound by a canonical riskDigest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_replication,
    backup_targets,
)

RISK_SNAPSHOT_VERSION = 1

SEVERITY_ORDER: dict[str, int] = {
    "healthy": 1,
    "warning": 2,
    "degraded": 3,
    "critical": 4,
    "blocked": 5,
}


class RiskSeverity(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    BLOCKED = "blocked"


class RiskType(str, Enum):
    CAPACITY_EXHAUSTION = "CAPACITY_EXHAUSTION"
    REPLICA_LAG = "REPLICA_LAG"
    DR_STALENESS = "DR_STALENESS"
    RESTORE_LATENCY_BREACH = "RESTORE_LATENCY_BREACH"
    FAILURE_DOMAIN_VIOLATION = "FAILURE_DOMAIN_VIOLATION"
    REPAIR_BACKLOG = "REPAIR_BACKLOG"
    AUTHORITY_DEGRADATION = "AUTHORITY_DEGRADATION"


class RiskConfidence(str, Enum):
    VERIFIED = "verified"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNAVAILABLE = "unavailable"


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def compute_risk_digest(snapshot: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of a risk snapshot."""
    payload = {
        "riskSnapshotVersion": snapshot.get("riskSnapshotVersion", RISK_SNAPSHOT_VERSION),
        "overallRisk": str(snapshot.get("overallRisk", "healthy")),
        "risks": sorted(
            [
                {
                    "type": str(r.get("type", "")),
                    "target": str(r.get("target") or ""),
                    "policyId": str(r.get("policyId") or ""),
                    "severity": str(r.get("severity", "healthy")),
                    "confidence": str(r.get("confidence", "verified")),
                    "evidence": sorted([str(e) for e in r.get("evidence", [])]),
                }
                for r in snapshot.get("risks", [])
            ],
            key=lambda x: (x["severity"], x["type"], x["target"], x["policyId"]),
            reverse=True,
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def evaluate_target_capacity_risk(
    target_id: str,
    *,
    probe: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate capacity exhaustion risk for a target."""
    cap = backup_capacity.get_target_capacity(target_id, probe=probe)
    horizon = backup_capacity.estimate_target_exhaustion_horizon(target_id, policy_id="", probe=probe, record_observation=probe)

    free_pct = cap.get("freePercent")
    free_bytes = cap.get("freeBytes")
    total_bytes = cap.get("totalBytes")
    days_to_full = horizon.get("estimatedDaysToFull")

    evidence: list[str] = []
    details: dict[str, Any] = {
        "targetId": target_id,
        "freePercent": free_pct,
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "estimatedDaysToFull": days_to_full,
        "forecastStatus": horizon.get("forecastStatus"),
    }

    if free_pct is None or total_bytes is None:
        return {
            "type": RiskType.CAPACITY_EXHAUSTION.value,
            "target": target_id,
            "severity": RiskSeverity.HEALTHY.value,
            "confidence": RiskConfidence.HIGH.value,
            "evidence": ["unconstrained-quota"],
            "details": details,
        }

    severity = RiskSeverity.HEALTHY.value
    if free_pct < 5.0 or (isinstance(days_to_full, int) and days_to_full < 7):
        severity = RiskSeverity.CRITICAL.value
        evidence.append(f"free-space-critical:{free_pct:.1f}%<5.0% or horizon={days_to_full}d<7d")
    elif free_pct < 10.0 or (isinstance(days_to_full, int) and days_to_full < 30):
        severity = RiskSeverity.DEGRADED.value
        evidence.append(f"free-space-degraded:{free_pct:.1f}%<10.0% or horizon={days_to_full}d<30d")
    elif free_pct <= 20.0:
        severity = RiskSeverity.WARNING.value
        evidence.append(f"free-space-warning:{free_pct:.1f}%<=20.0%")
    else:
        evidence.append(f"free-space-healthy:{free_pct:.1f}%>20.0%")

    confidence = RiskConfidence.VERIFIED.value if cap.get("observedAt") or horizon.get("confidence") == "high" else RiskConfidence.HIGH.value
    return {
        "type": RiskType.CAPACITY_EXHAUSTION.value,
        "target": target_id,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "details": details,
    }


def evaluate_policy_replica_risk(
    policy_id: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Evaluate replica lag and failure domain risks for a policy."""
    try:
        policy = backup_policies.get_policy(policy_id)
    except AppError:
        return []
    if not policy:
        return []

    risks: list[dict[str, Any]] = []
    repl_cfg = policy.get("replication") if isinstance(policy, dict) else None
    if not isinstance(repl_cfg, dict) or not repl_cfg.get("enabled"):
        return risks

    min_copies = int(repl_cfg.get("minCommittedCopies") or 1)
    min_fds = int(repl_cfg.get("minFailureDomains") or 1)

    latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=policy_id)
    if not latest_pt:
        return risks

    backup_id = str(latest_pt.get("backupId") or "")
    copies = backup_dr_ledger.list_logical_recovery_copies(
        policy_id=policy_id,
        backup_id=backup_id,
    )

    committed_copies = [c for c in copies if str(c.get("status") or c.get("state") or "").lower() in {"committed", "active", "healthy"}]
    domains: set[str] = set()
    for c in committed_copies:
        dom = str(c.get("failureDomain") or c.get("targetId") or "")
        if dom:
            domains.add(dom)

    # 1. Replica copy count risk
    copy_evidence: list[str] = []
    if len(committed_copies) < min_copies:
        if len(committed_copies) == 0:
            rep_sev = RiskSeverity.CRITICAL.value
            copy_evidence.append(f"zero-committed-copies:0<{min_copies}")
        else:
            rep_sev = RiskSeverity.DEGRADED.value
            copy_evidence.append(f"insufficient-committed-copies:{len(committed_copies)}<{min_copies}")
    else:
        # Check for open required replica jobs (lagging replication)
        open_jobs = backup_replication.has_open_required_jobs(policy_id=policy_id, backup_id=backup_id)
        if open_jobs:
            rep_sev = RiskSeverity.WARNING.value
            copy_evidence.append("pending-replication-jobs")
        else:
            rep_sev = RiskSeverity.HEALTHY.value
            copy_evidence.append(f"committed-copies-satisfied:{len(committed_copies)}>={min_copies}")

    risks.append(
        {
            "type": RiskType.REPLICA_LAG.value,
            "policyId": policy_id,
            "severity": rep_sev,
            "confidence": RiskConfidence.VERIFIED.value,
            "evidence": copy_evidence,
            "details": {
                "backupId": backup_id,
                "committedCopies": len(committed_copies),
                "requiredCopies": min_copies,
            },
        }
    )

    # 2. Failure domain violation risk
    fd_evidence: list[str] = []
    if len(domains) < min_fds:
        fd_sev = RiskSeverity.DEGRADED.value if len(domains) > 0 else RiskSeverity.CRITICAL.value
        fd_evidence.append(f"failure-domain-deficit:{len(domains)}<{min_fds}")
    else:
        fd_sev = RiskSeverity.HEALTHY.value
        fd_evidence.append(f"failure-domains-satisfied:{len(domains)}>={min_fds}")

    risks.append(
        {
            "type": RiskType.FAILURE_DOMAIN_VIOLATION.value,
            "policyId": policy_id,
            "severity": fd_sev,
            "confidence": RiskConfidence.VERIFIED.value,
            "evidence": fd_evidence,
            "details": {
                "failureDomainsPresent": sorted(list(domains)),
                "requiredFailureDomains": min_fds,
            },
        }
    )

    return risks


def evaluate_dr_freshness_risk(
    policy_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate DR drill freshness and staleness risk."""
    current = now or datetime.now(tz=timezone.utc)
    drills = backup_dr_readiness._drill_records()  # noqa: SLF001
    successful_drills: list[dict[str, Any]] = []

    for d in drills:
        if d.get("success") is True or str(d.get("status") or "").lower() == "pass":
            successful_drills.append(d)

    evidence: list[str] = []
    if not successful_drills:
        return {
            "type": RiskType.DR_STALENESS.value,
            "policyId": policy_id,
            "severity": RiskSeverity.CRITICAL.value,
            "confidence": RiskConfidence.VERIFIED.value,
            "evidence": ["no-successful-dr-drills-recorded"],
            "details": {"lastSuccessfulDrillAgeDays": None},
        }

    latest_drill = successful_drills[0]
    finished_at = _parse_iso(latest_drill.get("finishedAt") or latest_drill.get("startedAt"))
    if finished_at is None:
        age_days = 999.0
    else:
        age_days = max(0.0, (current - finished_at).total_seconds() / 86400.0)

    details = {
        "lastDrillId": latest_drill.get("drillId"),
        "lastSuccessfulDrillAgeDays": round(age_days, 2),
        "finishedAt": str(latest_drill.get("finishedAt") or ""),
    }

    if age_days > 30.0:
        severity = RiskSeverity.CRITICAL.value
        evidence.append(f"dr-drill-stale:{age_days:.1f}d>30d")
    elif age_days > 7.0:
        severity = RiskSeverity.WARNING.value
        evidence.append(f"dr-drill-warning:{age_days:.1f}d>7d")
    else:
        severity = RiskSeverity.HEALTHY.value
        evidence.append(f"dr-drill-fresh:{age_days:.1f}d<=7d")

    return {
        "type": RiskType.DR_STALENESS.value,
        "policyId": policy_id,
        "severity": severity,
        "confidence": RiskConfidence.VERIFIED.value,
        "evidence": evidence,
        "details": details,
    }


def evaluate_restore_latency_risk(
    policy_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate restore latency SLA / RTO breach risk."""
    slo = backup_dr_readiness.calculate_dr_slo_metrics(now=now)
    rto_p95 = slo.get("rtoSecondsP95")
    evidence: list[str] = []
    details = {
        "rtoSecondsP50": slo.get("rtoSecondsP50"),
        "rtoSecondsP95": rto_p95,
        "rtoSecondsP99": slo.get("rtoSecondsP99"),
        "restoreSuccessRate": slo.get("restoreSuccessRate"),
    }

    success_rate = slo.get("restoreSuccessRate")
    if success_rate is not None and isinstance(success_rate, (int, float)) and success_rate < 0.99:
        severity = RiskSeverity.CRITICAL.value
        evidence.append(f"restore-success-rate-low:{success_rate:.2%}<99%")
    elif rto_p95 is not None and isinstance(rto_p95, (int, float)) and rto_p95 > 1800:
        severity = RiskSeverity.DEGRADED.value
        evidence.append(f"rto-p95-exceeded:{rto_p95}s>1800s")
    elif rto_p95 is not None and isinstance(rto_p95, (int, float)) and rto_p95 > 900:
        severity = RiskSeverity.WARNING.value
        evidence.append(f"rto-p95-warning:{rto_p95}s>900s")
    else:
        severity = RiskSeverity.HEALTHY.value
        evidence.append("restore-latency-within-bounds")

    return {
        "type": RiskType.RESTORE_LATENCY_BREACH.value,
        "policyId": policy_id,
        "severity": severity,
        "confidence": RiskConfidence.HIGH.value,
        "evidence": evidence,
        "details": details,
    }


def evaluate_repair_backlog_risk() -> dict[str, Any]:
    """Evaluate repair job backlog and failed repair instances."""
    repairs = backup_replication.list_repair_jobs()
    open_repairs = [r for r in repairs if str(r.get("phase", "")).lower() not in {"complete", "resolved"}]
    failed_repairs = [r for r in open_repairs if str(r.get("phase", "")).lower() == "failed"]

    evidence: list[str] = []
    details = {
        "totalRepairs": len(repairs),
        "openRepairs": len(open_repairs),
        "failedRepairs": len(failed_repairs),
    }

    if len(failed_repairs) >= 3:
        severity = RiskSeverity.CRITICAL.value
        evidence.append(f"excessive-failed-repairs:{len(failed_repairs)}>=3")
    elif len(failed_repairs) > 0 or len(open_repairs) >= 5:
        severity = RiskSeverity.DEGRADED.value
        evidence.append(f"repair-backlog-accumulating:open={len(open_repairs)},failed={len(failed_repairs)}")
    elif len(open_repairs) > 0:
        severity = RiskSeverity.WARNING.value
        evidence.append(f"repairs-in-progress:open={len(open_repairs)}")
    else:
        severity = RiskSeverity.HEALTHY.value
        evidence.append("no-repair-backlog")

    return {
        "type": RiskType.REPAIR_BACKLOG.value,
        "severity": severity,
        "confidence": RiskConfidence.VERIFIED.value,
        "evidence": evidence,
        "details": details,
    }


def evaluate_authority_risk() -> dict[str, Any]:
    """Evaluate authority consensus, anchor drift, and fail-closed state via canonical authority_verify()."""
    evidence: list[str] = []
    details: dict[str, Any] = {}

    try:
        with backup_control._connect() as conn:  # noqa: SLF001
            row = conn.execute("SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1").fetchone()
            head_gen = row[0] if row else 0
            head_digest = row[1] if row else None
            details["authorityGeneration"] = head_gen
            details["authorityHeadDigest"] = head_digest
    except Exception as exc:
        return {
            "type": RiskType.AUTHORITY_DEGRADATION.value,
            "severity": RiskSeverity.BLOCKED.value,
            "confidence": RiskConfidence.HIGH.value,
            "evidence": [f"authority-db-unreadable:{exc}"],
            "details": {"error": str(exc)},
        }

    from deepseek_infra.infra.workspace import backup_control_recovery

    try:
        v_res = backup_control_recovery.authority_verify()
        overall = str(v_res.get("overall") or "HEALTHY").upper()
        issues = list(v_res.get("issues") or [])
        details["authorityVerify"] = {
            "overall": overall,
            "issues": issues,
            "configuredReplicaCount": v_res.get("configuredReplicaCount"),
            "resolvedReplicaCount": v_res.get("resolvedReplicaCount"),
            "replicaCount": len(v_res.get("replicas") or []),
        }
        for iss in issues:
            evidence.append(f"authority-issue:{iss}")

        if overall == "DIVERGENT":
            evidence.append("authority-cross-replica-fork-detected")
            return {
                "type": RiskType.AUTHORITY_DEGRADATION.value,
                "severity": RiskSeverity.BLOCKED.value,
                "confidence": RiskConfidence.VERIFIED.value,
                "evidence": evidence,
                "details": details,
            }
        elif overall in {"UNAVAILABLE", "DURABILITY_UNSATISFIED"}:
            evidence.append(f"authority-{overall.lower()}-critical")
            return {
                "type": RiskType.AUTHORITY_DEGRADATION.value,
                "severity": RiskSeverity.CRITICAL.value,
                "confidence": RiskConfidence.VERIFIED.value,
                "evidence": evidence,
                "details": details,
            }
        elif overall == "DEGRADED":
            severity = (
                RiskSeverity.WARNING.value
                if len(issues) == 1 and ("authority-replica-lag-detected" in issues or any("lag" in str(i) for i in issues))
                else RiskSeverity.DEGRADED.value
            )
            evidence.append("authority-degraded-detected")
            return {
                "type": RiskType.AUTHORITY_DEGRADATION.value,
                "severity": severity,
                "confidence": RiskConfidence.VERIFIED.value,
                "evidence": evidence,
                "details": details,
            }
    except Exception as exc:
        evidence.append(f"authority-verify-exception:{exc}")
        return {
            "type": RiskType.AUTHORITY_DEGRADATION.value,
            "severity": RiskSeverity.BLOCKED.value,
            "confidence": RiskConfidence.HIGH.value,
            "evidence": evidence,
            "details": {"error": str(exc)},
        }

    evidence.append("authority-consensus-verified")
    return {
        "type": RiskType.AUTHORITY_DEGRADATION.value,
        "severity": RiskSeverity.HEALTHY.value,
        "confidence": RiskConfidence.VERIFIED.value,
        "evidence": evidence,
        "details": details,
    }


def assess_risks(
    *,
    target_ids: list[str] | None = None,
    policy_ids: list[str] | None = None,
    probe: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute end-to-end Risk Assessment across all storage and recovery dimensions."""
    current = now or datetime.now(tz=timezone.utc)
    all_risks: list[dict[str, Any]] = []

    # 1. Capacity risks across targets
    targets = backup_targets.list_targets()
    effective_targets = target_ids if target_ids is not None else ["managed-local"] + [str(t.get("targetId") or "") for t in targets]
    seen_targets: set[str] = set()
    for tid in effective_targets:
        if not tid or tid in seen_targets:
            continue
        seen_targets.add(tid)
        cap_risk = evaluate_target_capacity_risk(tid, probe=probe, now=current)
        all_risks.append(cap_risk)

    # 2. Replication and Failure Domain risks across policies
    policies = backup_policies.list_policies()
    effective_policies = policy_ids if policy_ids is not None else [str(p.get("policyId") or "") for p in policies]
    seen_policies: set[str] = set()
    for pid in effective_policies:
        if not pid or pid in seen_policies:
            continue
        seen_policies.add(pid)
        pol_risks = evaluate_policy_replica_risk(pid, now=current)
        all_risks.extend(pol_risks)

    # 3. DR Staleness risk
    dr_risk = evaluate_dr_freshness_risk(now=current)
    all_risks.append(dr_risk)

    # 4. Restore Latency risk
    rto_risk = evaluate_restore_latency_risk(now=current)
    all_risks.append(rto_risk)

    # 5. Repair Backlog risk
    repair_risk = evaluate_repair_backlog_risk()
    all_risks.append(repair_risk)

    # 6. Authority Integrity risk
    auth_risk = evaluate_authority_risk()
    all_risks.append(auth_risk)

    # Determine overall risk based on worst severity
    worst_severity = RiskSeverity.HEALTHY.value
    worst_order = SEVERITY_ORDER[worst_severity]
    for r in all_risks:
        sev = str(r.get("severity", RiskSeverity.HEALTHY.value)).lower()
        order = SEVERITY_ORDER.get(sev, 1)
        if order > worst_order:
            worst_order = order
            worst_severity = sev

    snapshot: dict[str, Any] = {
        "riskSnapshotVersion": RISK_SNAPSHOT_VERSION,
        "generatedAt": _utc_iso(current),
        "overallRisk": worst_severity,
        "risks": all_risks,
    }
    snapshot["riskDigest"] = compute_risk_digest(snapshot)
    return snapshot
