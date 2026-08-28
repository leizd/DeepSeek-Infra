"""Source-backed operator projection for durable Fleet resilience readiness."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    resilience_risk_observations,
    resilience_scheduler_service,
    resilience_slo_ledger,
)


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


def get_fleet_readiness(*, now: datetime | None = None) -> dict[str, Any]:
    """Assemble current readiness without trusting a self-reported PASS flag."""
    current = now or datetime.now(tz=timezone.utc)
    open_risks = resilience_risk_observations.list_open_observations()
    open_ages = []
    for risk in open_risks:
        opened = _parse_iso(risk.get("openSinceAt"))
        if opened is not None:
            open_ages.append(max(0.0, (current - opened).total_seconds()))
    critical_risks = [
        risk for risk in open_risks if str(risk.get("currentSeverity") or "").lower() in {"critical", "blocked"}
    ]

    dr_slo = backup_dr_readiness.calculate_dr_slo_metrics(now=current)
    dr_freshness_hours = max(0.0, float(dr_slo.get("evidenceFreshnessDays") or 0.0) * 24.0)
    resilience_slo_ledger.record_sample(
        resilience_slo_ledger.DR_READINESS_AGE_HOURS,
        dr_freshness_hours,
        observed_at=current,
        sample_key=f"dr-readiness:{_utc_iso(current)}",
    )
    slo = resilience_slo_ledger.get_fleet_slo_snapshot(now=current)
    burn_rate = resilience_slo_ledger.compute_burn_rates(now=current)

    schedule = resilience_scheduler_service.get_latest_schedule_snapshot() or {}
    raw_waves = schedule.get("executionWaves")
    waves = raw_waves if isinstance(raw_waves, list) else []
    deferred = int(schedule.get("deferredCount") or 0)
    unschedulable = int(schedule.get("unschedulableCount") or 0)

    evidence_sample = resilience_slo_ledger.latest_evidence_verification()
    verified_at = _parse_iso(evidence_sample.get("observedAt")) if evidence_sample else None
    proof_freshness = max(0.0, (current - verified_at).total_seconds()) if verified_at is not None else None
    evidence_metadata = evidence_sample.get("metadata") if evidence_sample else {}
    metadata: dict[str, Any] = evidence_metadata if isinstance(evidence_metadata, dict) else {}

    if critical_risks or burn_rate["status"] == "CRITICAL":
        readiness = "CRITICAL"
    elif open_risks or unschedulable or not dr_slo.get("overallSloCompliant") or evidence_sample is None:
        readiness = "DEGRADED"
    else:
        readiness = "READY"

    return {
        "fleetReadiness": readiness,
        "slo": slo,
        "riskDebt": {
            "total": len(open_risks),
            "critical": len(critical_risks),
            "oldestRiskAgeSeconds": round(max(open_ages, default=0.0), 3),
        },
        "scheduler": {
            "scheduleId": schedule.get("scheduleId"),
            "waves": waves,
            "deferred": deferred,
            "unschedulable": unschedulable,
        },
        "burnRate": burn_rate,
        "evidence": {
            "lastVerifiedAt": evidence_sample.get("observedAt") if evidence_sample else None,
            "proofFreshnessSeconds": round(proof_freshness, 3) if proof_freshness is not None else None,
            "proofSha256": metadata.get("proofSha256"),
            "scenario": metadata.get("scenario"),
        },
        "generatedAt": _utc_iso(current),
    }
