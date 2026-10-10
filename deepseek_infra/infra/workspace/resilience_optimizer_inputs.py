"""Authoritative present-truth builder for production What-If requests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_dr_ledger,
    resilience_cost_model,
    resilience_forecast_registry,
    resilience_fresh_state,
)

_ALLOWED_CANDIDATE_FIELDS = {
    "policyId",
    "targetId",
    "sourceTargetId",
    "destTargetId",
    "backupId",
    "operation",
    "committedCopiesDelta",
    "failureDomainsDelta",
    "storedBytes",
    "additionalStoredBytes",
    "replicationBytes",
    "egressBytes",
    "retrievalBytes",
    "requestCount",
    "forecastHorizonDays",
}
_OPERATIONS = {"KEEP", "ADD_REPLICA", "REBALANCE"}


class AuthoritativeInputUnavailable(RuntimeError):
    """A mandatory production truth source is absent or malformed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(candidate: dict[str, Any], field: str, *, default: int = 0, allow_negative: bool = False) -> int:
    value = candidate.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"candidate.{field} must be an integer")
    if not allow_negative and value < 0:
        raise ValueError(f"candidate.{field} must be non-negative")
    return value


def _validate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict) or not candidate:
        raise ValueError("candidate is required")
    unknown = sorted(set(candidate) - _ALLOWED_CANDIDATE_FIELDS)
    if unknown:
        raise ValueError(f"caller-controlled present truth is forbidden: {', '.join(unknown)}")
    policy_id = str(candidate.get("policyId") or "").strip()
    target_id = str(candidate.get("targetId") or candidate.get("destTargetId") or "").strip()
    if not policy_id:
        raise ValueError("candidate.policyId is required")
    if not target_id:
        raise ValueError("candidate.targetId or candidate.destTargetId is required")
    operation = str(candidate.get("operation") or "KEEP").strip().upper()
    if operation not in _OPERATIONS:
        raise ValueError("candidate.operation must be KEEP, ADD_REPLICA, or REBALANCE")
    horizon = _integer(candidate, "forecastHorizonDays", default=90)
    if horizon not in {30, 90}:
        raise ValueError("candidate.forecastHorizonDays must be 30 or 90")
    for field in ("storedBytes", "additionalStoredBytes", "replicationBytes", "egressBytes", "retrievalBytes", "requestCount"):
        _integer(candidate, field)
    _integer(candidate, "committedCopiesDelta", allow_negative=True)
    _integer(candidate, "failureDomainsDelta", allow_negative=True)
    return {
        **candidate,
        "policyId": policy_id,
        "targetId": target_id,
        "operation": operation,
        "forecastHorizonDays": horizon,
    }


def _read_baseline(policy: dict[str, Any]) -> dict[str, Any]:
    policy_id = str(policy.get("policyId") or "")
    raw_replication = policy.get("replication")
    replication: dict[str, Any] = raw_replication if isinstance(raw_replication, dict) else {}
    raw_placement = policy.get("placement")
    placement: dict[str, Any] = raw_placement if isinstance(raw_placement, dict) else {}
    try:
        latest = backup_dr_ledger.latest_recovery_point(policy_id=policy_id)
        backup_id = str((latest or {}).get("backupId") or "")
        copies = (
            backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id, limit=1000)
            if backup_id
            else []
        )
        targets = {str(item.get("targetId") or ""): item for item in backup_control.list_targets()}
    except Exception as exc:
        raise AuthoritativeInputUnavailable(f"BASELINE_TRUTH_UNAVAILABLE: {type(exc).__name__}: {exc}") from exc
    committed = [item for item in copies if item.get("recoverable") is True and str(item.get("state") or "healthy") == "healthy"]
    domains: set[str] = set()
    for copy in committed:
        target_id = str(copy.get("targetId") or "")
        target = targets.get(target_id)
        if target is None:
            raise AuthoritativeInputUnavailable(f"BASELINE_TARGET_UNAVAILABLE: {target_id}")
        domain = str(target.get("failureDomain") or "").strip()
        if not domain:
            raise AuthoritativeInputUnavailable(f"BASELINE_FAILURE_DOMAIN_UNAVAILABLE: {target_id}")
        domains.add(domain)
    return {
        "policyId": policy_id,
        "backupId": backup_id or None,
        "policyRevision": int(policy.get("policyRevision") or 0),
        "minCommittedCopies": int(replication.get("minCommittedCopies") or 1),
        "minFailureDomains": int(replication.get("minFailureDomains") or 1),
        "committedCopies": len(committed),
        "failureDomains": len(domains),
        "failureDomainIds": sorted(domains),
        "forecastSafetyHeadroomBytes": int(placement.get("minFreeBytes") or 0),
    }


def _candidate_action(candidate: dict[str, Any]) -> dict[str, Any]:
    operation = str(candidate["operation"])
    action_type = {
        "KEEP": "NOOP",
        "ADD_REPLICA": "CREATE_REPAIR_JOB",
        "REBALANCE": "CREATE_REBALANCE_JOB",
    }[operation]
    params = {
        "policyId": candidate["policyId"],
        "backupId": str(candidate.get("backupId") or ""),
        "targetId": candidate["targetId"],
        "sourceTargetId": str(candidate.get("sourceTargetId") or ""),
        "destTargetId": str(candidate.get("destTargetId") or candidate["targetId"]),
    }
    return {
        "actionId": "whatif-candidate",
        "type": action_type,
        "policyId": candidate["policyId"],
        "backupId": params["backupId"],
        "parameters": params,
    }


def build_authoritative_optimizer_inputs(
    candidate: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind hypothetical deltas to current production truth; missing truth fails closed."""
    proposed = _validate_candidate(candidate)
    current = now or datetime.now(tz=timezone.utc)
    try:
        policy = backup_control.get_policy(str(proposed["policyId"]))
    except Exception as exc:
        raise AuthoritativeInputUnavailable(f"POLICY_TRUTH_UNAVAILABLE: {type(exc).__name__}: {exc}") from exc
    if not isinstance(policy, dict) or not policy:
        raise AuthoritativeInputUnavailable("POLICY_TRUTH_UNAVAILABLE")
    baseline = _read_baseline(policy)

    forecast_record = resilience_forecast_registry.get_current_forecast(
        str(proposed["targetId"]),
        horizon_days=int(proposed["forecastHorizonDays"]),
    )
    if not isinstance(forecast_record, dict) or forecast_record.get("status") not in {"ACTIVE", "DUE"}:
        raise AuthoritativeInputUnavailable("FORECAST_REGISTRY_UNAVAILABLE")
    raw_forecast = forecast_record.get("forecast")
    forecast = dict(raw_forecast) if isinstance(raw_forecast, dict) and raw_forecast else {
        "targetId": forecast_record.get("targetId"),
        "horizonDays": forecast_record.get("horizonDays"),
        "forecastStatus": "OK",
        "p50FreeBytes": forecast_record.get("p50FreeBytes"),
        "p90FreeBytes": forecast_record.get("p90FreeBytes"),
        "forecastDigest": forecast_record.get("forecastDigest"),
        "capacityObservationSetDigest": forecast_record.get("capacityObservationSetDigest"),
    }
    if forecast.get("forecastStatus") != "OK" or not str(forecast.get("forecastDigest") or ""):
        raise AuthoritativeInputUnavailable("FORECAST_REGISTRY_UNAVAILABLE")

    catalog = resilience_cost_model.get_price_catalog()
    targets = catalog.get("targets") if isinstance(catalog, dict) else None
    if not isinstance(catalog, dict) or not str(catalog.get("priceCatalogDigest") or "") or not isinstance(targets, dict):
        raise AuthoritativeInputUnavailable("PRICE_CATALOG_UNAVAILABLE")
    if not isinstance(targets.get(str(proposed["targetId"])), dict):
        raise AuthoritativeInputUnavailable("TARGET_PRICE_UNAVAILABLE")

    action = _candidate_action(proposed)
    try:
        fresh = resilience_fresh_state.build_fresh_state_bundle(
            {"scheduleId": "whatif", "source": "authoritative-optimizer"},
            [action],
            now=current,
        )
    except resilience_fresh_state.FreshStateUnavailable as exc:
        raise AuthoritativeInputUnavailable(exc.reason) from exc
    if not isinstance(fresh, dict) or not str(fresh.get("freshStateBundleDigest") or ""):
        raise AuthoritativeInputUnavailable("FRESH_STATE_BUNDLE_UNAVAILABLE")

    evaluated_candidate = {
        "policyId": proposed["policyId"],
        "targetId": proposed["targetId"],
        "operation": proposed["operation"],
        "committedCopies": max(0, int(baseline["committedCopies"]) + int(proposed.get("committedCopiesDelta") or 0)),
        "failureDomains": max(0, int(baseline["failureDomains"]) + int(proposed.get("failureDomainsDelta") or 0)),
        "storedBytes": int(proposed.get("storedBytes") or 0),
        "replicationBytes": int(proposed.get("replicationBytes") or 0),
        "egressBytes": int(proposed.get("egressBytes") or 0),
        "retrievalBytes": int(proposed.get("retrievalBytes") or 0),
        "requestCount": int(proposed.get("requestCount") or 0),
        "forecastFreeBytes": max(0, int(forecast.get("p90FreeBytes") or 0) - int(proposed.get("additionalStoredBytes") or 0)),
        "breaksDrDependency": (fresh.get("blastSimulation") or {}).get("passed") is not True,
        "mutatesAuthority": False,
    }
    capacity_snapshot = fresh.get("capacitySnapshot") if isinstance(fresh.get("capacitySnapshot"), dict) else {}
    raw_targets = capacity_snapshot.get("targets") if isinstance(capacity_snapshot, dict) else []
    try:
        authoritative_targets = backup_control.list_targets()
        authoritative_target_ids = {
            str(item.get("targetId") or "")
            for item in authoritative_targets
            if str(item.get("targetId") or "")
        }
    except Exception as exc:
        raise AuthoritativeInputUnavailable(f"TARGET_TRUTH_UNAVAILABLE: {type(exc).__name__}: {exc}") from exc
    target_ids = sorted(
        authoritative_target_ids
        | {
            str(item.get("targetId") or "")
            for item in (raw_targets if isinstance(raw_targets, list) else [])
            if isinstance(item, dict) and str(item.get("targetId") or "")
        }
        | {str(proposed["targetId"])}
    )
    result: dict[str, Any] = {
        "candidate": evaluated_candidate,
        "hypotheticalDelta": proposed,
        "baseline": baseline,
        "observedSnapshot": fresh.get("riskSnapshot"),
        "forecast": forecast,
        "forecastRecord": forecast_record,
        "priceCatalog": catalog,
        "authorityHeadDigest": str(fresh.get("authorityHeadDigest") or ""),
        "freshStateBundleDigest": str(fresh.get("freshStateBundleDigest") or ""),
        "freshStateBundle": fresh,
        "runningEffects": list(fresh.get("runningEffects") or []),
        "maintenanceWindows": list(fresh.get("maintenanceDecisions") or []),
        "capacitySnapshot": capacity_snapshot,
        "targetMetadata": authoritative_targets,
        "budgets": fresh.get("budgets"),
        "blastSimulation": fresh.get("blastSimulation"),
        "targetIds": target_ids,
        "observedAt": str(fresh.get("observedAt") or _utc_iso(current)),
    }
    result["optimizerInputDigest"] = resilience_fresh_state.canonical_digest(result)
    return result
