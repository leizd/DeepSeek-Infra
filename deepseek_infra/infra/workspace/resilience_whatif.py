"""Side-effect-free fleet what-if simulation (4.7.5 Gate K)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import resilience_placement_optimizer

MUTATION_COUNTERS = {
    "s3PutCount": 0,
    "s3DeleteCount": 0,
    "authorityMutationCount": 0,
    "actionJournalMutationCount": 0,
}


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def simulate_fleet(
    *,
    observed_snapshot: dict[str, Any],
    forecast: dict[str, Any],
    price_catalog: dict[str, Any] | None,
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    running_effects: list[dict[str, Any]] | None = None,
    maintenance_windows: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure simulation: never writes storage, authority, or the action journal."""
    snapshot_digest = str(observed_snapshot.get("riskDigest") or observed_snapshot.get("snapshotDigest") or "")
    evaluation = resilience_placement_optimizer.evaluate_candidate(
        candidate,
        baseline=baseline,
        catalog=price_catalog,
    )
    cost = evaluation["cost"]
    if cost.get("status") != "OK":
        monthly_cost = None
        cost_status = "UNKNOWN_COST"
    else:
        monthly_cost = cost.get("monthlyCost")
        cost_status = "OK"
    payload = {
        "status": "OK" if evaluation["accepted"] else "REJECTED",
        "violations": evaluation["violations"],
        "capacity": {
            "forecastFreeBytes": candidate.get("forecastFreeBytes"),
            "forecastStatus": forecast.get("forecastStatus"),
            "p50FreeBytes": forecast.get("p50FreeBytes"),
            "p90FreeBytes": forecast.get("p90FreeBytes"),
        },
        "cost": {
            "status": cost_status,
            "monthlyCost": monthly_cost,
            "storage": cost.get("storage"),
            "egress": cost.get("egress"),
            "replicationTransfer": cost.get("replicationTransfer"),
            "retrieval": cost.get("retrieval"),
        },
        "failureDomains": candidate.get("failureDomains"),
        "blastRadius": {
            "committedCopies": candidate.get("committedCopies"),
            "minCommittedCopies": baseline.get("minCommittedCopies"),
            "passed": evaluation["accepted"],
        },
        "transferBudget": candidate.get("transferBudget") or {},
        "maintenanceWindows": list(maintenance_windows or []),
        "riskVector": observed_snapshot.get("overallRisk") or observed_snapshot.get("riskVector"),
        "runningEffects": list(running_effects or []),
        "sourceSnapshotDigest": snapshot_digest,
        "forecastDigest": forecast.get("forecastDigest"),
        "priceCatalogDigest": None if price_catalog is None else price_catalog.get("priceCatalogDigest"),
        "s3PutCount": 0,
        "s3DeleteCount": 0,
        "authorityMutationCount": 0,
        "actionJournalMutationCount": 0,
        "sideEffectsObserved": 0,
        "generatedAt": _utc_iso(now),
    }
    payload["whatIfDigest"] = _digest({k: v for k, v in payload.items() if k != "whatIfDigest"})
    return payload


def build_optimization_proof(
    *,
    observed_snapshot: dict[str, Any],
    forecast: dict[str, Any],
    price_catalog: dict[str, Any],
    candidate_plan: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    simulation: dict[str, Any],
) -> dict[str, Any]:
    selected = candidate_plan.get("selected") or {}
    raw_candidate = selected.get("candidate") if isinstance(selected, dict) else {}
    candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
    durability = {
        "copiesPreserved": int(candidate.get("committedCopies") or 0) >= int(before.get("committedCopies") or 0),
        "failureDomainsPreserved": int(candidate.get("failureDomains") or 0) >= int(before.get("failureDomains") or 0),
    }
    payload = {
        "schema": "placement-optimization-proof-v1",
        "sourceSnapshotDigest": str(
            observed_snapshot.get("riskDigest") or observed_snapshot.get("snapshotDigest") or ""
        ),
        "authorityHeadDigest": str(candidate_plan.get("authorityHeadDigest") or ""),
        "forecastDigest": str(forecast.get("forecastDigest") or ""),
        "priceCatalogDigest": str(price_catalog.get("priceCatalogDigest") or ""),
        "candidatePlanDigest": str(candidate_plan.get("candidatePlanDigest") or ""),
        "before": before,
        "after": after,
        "durability": durability,
        "sideEffectsObserved": int(simulation.get("sideEffectsObserved") or 0),
        "s3PutCount": int(simulation.get("s3PutCount") or 0),
        "s3DeleteCount": int(simulation.get("s3DeleteCount") or 0),
        "authorityMutationCount": int(simulation.get("authorityMutationCount") or 0),
        "actionJournalMutationCount": int(simulation.get("actionJournalMutationCount") or 0),
    }
    payload["proofDigest"] = _digest(payload)
    return payload
