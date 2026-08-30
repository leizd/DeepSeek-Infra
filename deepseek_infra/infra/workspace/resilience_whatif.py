"""Side-effect-free fleet what-if simulation (4.7.5 Gate K)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from deepseek_infra.infra.workspace import (
    resilience_optimizer_inputs,
    resilience_placement_optimizer,
    resilience_simulation_capability,
    resilience_state_digests,
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _evaluate_authoritative(inputs: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(inputs.get("candidate") or {})
    baseline = dict(inputs.get("baseline") or {})
    catalog = inputs.get("priceCatalog") if isinstance(inputs.get("priceCatalog"), dict) else None
    raw_forecast = inputs.get("forecast")
    forecast: dict[str, Any] = raw_forecast if isinstance(raw_forecast, dict) else {}
    evaluation = resilience_placement_optimizer.evaluate_candidate(
        candidate,
        baseline=baseline,
        catalog=catalog,
    )
    plan = resilience_placement_optimizer.optimize_placement(
        baseline=baseline,
        candidates=[candidate],
        catalog=catalog,
        source_snapshot_digest=str((inputs.get("observedSnapshot") or {}).get("riskDigest") or ""),
        authority_head_digest=str(inputs.get("authorityHeadDigest") or ""),
        forecast_digest=str(forecast.get("forecastDigest") or ""),
    )
    return {"evaluation": evaluation, "candidatePlan": plan}


def _changed_domains(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    raw_before = before.get("digests")
    raw_after = after.get("digests")
    before_digests: dict[str, Any] = raw_before if isinstance(raw_before, dict) else {}
    after_digests: dict[str, Any] = raw_after if isinstance(raw_after, dict) else {}
    return sorted(
        domain
        for domain in set(before_digests) | set(after_digests)
        if before_digests.get(domain) != after_digests.get(domain)
    )


def _finish_simulation(
    inputs: dict[str, Any],
    *,
    evaluated: dict[str, Any],
    capability: resilience_simulation_capability.SimulationCapability,
    before: dict[str, Any],
    after: dict[str, Any],
    violation: resilience_simulation_capability.SimulationViolation | None,
    now: datetime | None,
) -> dict[str, Any]:
    audit = capability.audit()
    changed = _changed_domains(before, after)
    state_unchanged = before.get("stateDigest") == after.get("stateDigest") and not changed
    simulation = {
        **audit,
        "preStateDigest": str(before.get("stateDigest") or ""),
        "postStateDigest": str(after.get("stateDigest") or ""),
        "preStateDigests": dict(before.get("digests") or {}),
        "postStateDigests": dict(after.get("digests") or {}),
        "storageInventoryBefore": list(before.get("storageInventory") or []),
        "storageInventoryAfter": list(after.get("storageInventory") or []),
        "stateUnchanged": state_unchanged,
        "changedDomains": changed,
    }
    raw_evaluation_value = evaluated.get("evaluation")
    raw_evaluation: dict[str, Any] = raw_evaluation_value if isinstance(raw_evaluation_value, dict) else {}
    status = "SIMULATION_VIOLATION" if violation is not None or audit["attemptedMutationCount"] > 0 or not state_unchanged else (
        "OK" if raw_evaluation.get("accepted") is True else "REJECTED"
    )
    raw_cost = raw_evaluation.get("cost")
    cost: dict[str, Any] = raw_cost if isinstance(raw_cost, dict) else {}
    raw_forecast = inputs.get("forecast")
    forecast = raw_forecast if isinstance(raw_forecast, dict) else {}
    raw_observed = inputs.get("observedSnapshot")
    observed = raw_observed if isinstance(raw_observed, dict) else {}
    raw_candidate = inputs.get("candidate")
    candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
    raw_baseline = inputs.get("baseline")
    baseline = raw_baseline if isinstance(raw_baseline, dict) else {}
    raw_catalog = inputs.get("priceCatalog")
    catalog = raw_catalog if isinstance(raw_catalog, dict) else {}
    payload: dict[str, Any] = {
        "status": status,
        "violations": list(raw_evaluation.get("violations") or []),
        "capacity": {
            "forecastFreeBytes": candidate.get("forecastFreeBytes"),
            "forecastStatus": forecast.get("forecastStatus"),
            "p50FreeBytes": forecast.get("p50FreeBytes"),
            "p90FreeBytes": forecast.get("p90FreeBytes"),
        },
        "cost": {
            "status": cost.get("status"),
            "monthlyCost": cost.get("monthlyCost"),
            "storage": cost.get("storage"),
            "egress": cost.get("egress"),
            "replicationTransfer": cost.get("replicationTransfer"),
            "retrieval": cost.get("retrieval"),
        },
        "failureDomains": candidate.get("failureDomains"),
        "blastRadius": {
            "committedCopies": candidate.get("committedCopies"),
            "minCommittedCopies": baseline.get("minCommittedCopies"),
            "passed": raw_evaluation.get("accepted") is True,
        },
        "maintenanceWindows": list(inputs.get("maintenanceWindows") or []),
        "riskVector": observed.get("overallRisk") or observed.get("riskVector"),
        "runningEffects": list(inputs.get("runningEffects") or []),
        "sourceSnapshotDigest": str(observed.get("riskDigest") or observed.get("snapshotDigest") or ""),
        "forecastDigest": forecast.get("forecastDigest"),
        "priceCatalogDigest": catalog.get("priceCatalogDigest"),
        "authorityHeadDigest": inputs.get("authorityHeadDigest"),
        "freshStateBundleDigest": inputs.get("freshStateBundleDigest"),
        "optimizerInputDigest": inputs.get("optimizerInputDigest"),
        "candidatePlan": evaluated.get("candidatePlan"),
        "simulation": simulation,
        "sideEffectsObserved": int(audit["attemptedMutationCount"]) + len(changed),
        "generatedAt": _utc_iso(now),
    }
    domain_counts = {
        domain: sum(1 for item in audit["attemptedWrites"] if item.get("domain") == domain)
        for domain in ("storage", "authority", "action-journal", "policy", "target")
    }
    payload.update(
        {
            "s3PutCount": sum(
                1
                for item in audit["attemptedWrites"]
                if item.get("domain") == "storage" and "put" in str(item.get("operation") or "")
            ),
            "s3DeleteCount": sum(
                1
                for item in audit["attemptedWrites"]
                if item.get("domain") == "storage" and "delete" in str(item.get("operation") or "")
            ),
            "authorityMutationCount": domain_counts["authority"],
            "actionJournalMutationCount": domain_counts["action-journal"],
            "policyMutationCount": domain_counts["policy"],
            "targetMutationCount": domain_counts["target"],
        }
    )
    payload["whatIfDigest"] = _digest({key: value for key, value in payload.items() if key != "whatIfDigest"})
    return payload


def simulate_authoritative_inputs(
    inputs: dict[str, Any],
    *,
    evaluator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    now: datetime | None = None,
    require_complete_state: bool = True,
) -> dict[str, Any]:
    """Run one optimizer evaluation inside a write-deny capability sandbox."""
    target_ids = [str(item) for item in list(inputs.get("targetIds") or []) if str(item)]
    before = resilience_state_digests.capture_mutation_state(target_ids, require_complete=require_complete_state)
    capability = resilience_simulation_capability.SimulationCapability(inputs)
    violation: resilience_simulation_capability.SimulationViolation | None = None
    evaluated: dict[str, Any] = {}
    try:
        with capability.activate():
            evaluated = (evaluator or _evaluate_authoritative)(capability.inputs())
    except resilience_simulation_capability.SimulationViolation as exc:
        violation = exc
    after = resilience_state_digests.capture_mutation_state(target_ids, require_complete=require_complete_state)
    return _finish_simulation(
        inputs,
        evaluated=evaluated,
        capability=capability,
        before=before,
        after=after,
        violation=violation,
        now=now,
    )


def simulate_candidate(candidate: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build production truth and evaluate it within one audited read-only boundary."""
    before = resilience_state_digests.capture_mutation_state()
    inputs: dict[str, Any] = {"hypotheticalDelta": dict(candidate)}
    capability = resilience_simulation_capability.SimulationCapability(inputs)
    evaluated: dict[str, Any] = {}
    violation: resilience_simulation_capability.SimulationViolation | None = None
    pending_error: Exception | None = None
    try:
        with capability.activate():
            inputs = resilience_optimizer_inputs.build_authoritative_optimizer_inputs(candidate, now=now)
            capability.bind_inputs(inputs)
            evaluated = _evaluate_authoritative(capability.inputs())
    except resilience_simulation_capability.SimulationViolation as exc:
        violation = exc
    except Exception as exc:  # The post-state comparison must still run before the source error escapes.
        pending_error = exc
    after = resilience_state_digests.capture_mutation_state()
    if pending_error is not None and capability.audit()["attemptedMutationCount"] == 0 and not _changed_domains(before, after):
        raise pending_error
    return _finish_simulation(
        inputs,
        evaluated=evaluated,
        capability=capability,
        before=before,
        after=after,
        violation=violation,
        now=now,
    )


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
    """Compatibility wrapper; production API uses :func:`simulate_candidate`."""
    inputs = {
        "candidate": candidate,
        "baseline": baseline,
        "observedSnapshot": observed_snapshot,
        "forecast": forecast,
        "priceCatalog": price_catalog or {},
        "runningEffects": list(running_effects or []),
        "maintenanceWindows": list(maintenance_windows or []),
        "authorityHeadDigest": "",
        "freshStateBundleDigest": "",
        "optimizerInputDigest": "",
        "targetIds": [str(candidate.get("targetId") or "")],
    }
    return simulate_authoritative_inputs(inputs, now=now, require_complete_state=False)


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
