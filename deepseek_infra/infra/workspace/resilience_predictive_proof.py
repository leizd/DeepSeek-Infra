"""Typed, independently recomputable predictive planning proof (4.7.6 Gate J)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Sequence

PREDICTIVE_PLANNING_PROOF_SCHEMA = "predictive-planning-proof-v1"
CORE_PREDICTIVE_PROOF_CHECKS = (
    "realCapacityChangesDriveForecast",
    "realMinioInventoryUnchangedByWhatIf",
    "predictiveProofBindsCapacityObservationSet",
    "predictiveProofBindsForecastRecord",
    "predictiveProofBindsForecastBacktest",
    "predictiveProofBindsFreshStateBundle",
    "predictiveProofBindsPreAndPostState",
    "predictiveProofRejectsSelfReportedZeroMutation",
)
PROMOTED_PREDICTIVE_CLAIM_CHECKS = (
    "capacityForecastUsesDurableObservations",
    "forecastBacktestErrorIsPersisted",
    "costModelUsesVersionedPriceCatalog",
    "optimizerNeverReducesMinCommittedCopies",
    "optimizerNeverReducesMinFailureDomains",
    "whatIfProducesNoStorageWrites",
    "whatIfProducesNoStorageDeletes",
    "whatIfDoesNotMutateAuthority",
    "whatIfDoesNotMutateActionJournal",
    "whatIfBindsObservedFleetSnapshot",
    "whatIfIncludesRunningEffects",
    "whatIfIncludesMaintenanceWindows",
    "optimizationProofBindsForecastDigest",
    "optimizationProofBindsPriceCatalogDigest",
    "optimizationProofBindsAuthorityHead",
    "optimizationProofRecomputesSafetyConstraints",
)
PREDICTIVE_PROOF_CHECKS = CORE_PREDICTIVE_PROOF_CHECKS + PROMOTED_PREDICTIVE_CLAIM_CHECKS
_STATE_DOMAINS = ("storage", "authority", "actionJournal", "policy", "target")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: Any) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is not None


def _risk_digest(snapshot: dict[str, Any]) -> str:
    raw_risks = snapshot.get("risks")
    risks = raw_risks if isinstance(raw_risks, list) else []
    payload = {
        "riskSnapshotVersion": snapshot.get("riskSnapshotVersion", "risk-snapshot-v1"),
        "overallRisk": str(snapshot.get("overallRisk", "healthy")),
        "risks": sorted(
            [
                {
                    "type": str(item.get("type", "")),
                    "target": str(item.get("target") or ""),
                    "policyId": str(item.get("policyId") or ""),
                    "severity": str(item.get("severity", "healthy")),
                    "confidence": str(item.get("confidence", "verified")),
                    "evidence": sorted(str(value) for value in (item.get("evidence") or [])),
                }
                for item in risks
                if isinstance(item, dict)
            ],
            key=lambda item: (item["severity"], item["type"], item["target"], item["policyId"]),
            reverse=True,
        ),
    }
    return _digest(payload)


def _observation_set_digest(observations: Sequence[dict[str, Any]], *, forecast_record: dict[str, Any]) -> str:
    ordered = sorted(observations, key=lambda item: (str(item.get("observedAt") or ""), str(item.get("observationKey") or "")))
    return _digest(
        {
            "targetId": str(forecast_record.get("targetId") or ""),
            "targetIncarnation": str(forecast_record.get("targetIncarnation") or ""),
            "capacityRevision": str(forecast_record.get("capacityRevision") or ""),
            "observations": [
                {
                    "observationKey": str(item.get("observationKey") or ""),
                    "observationDigest": str(item.get("observationDigest") or ""),
                }
                for item in ordered
            ],
        }
    )


def _backtest_set_digest(backtests: Sequence[dict[str, Any]], *, forecast_record: dict[str, Any]) -> str:
    ordered = sorted(backtests, key=lambda item: (str(item.get("evaluatedAt") or ""), str(item.get("backtestKey") or "")))
    return _digest(
        {
            "targetId": str(forecast_record.get("targetId") or ""),
            "targetIncarnation": str(forecast_record.get("targetIncarnation") or ""),
            "capacityRevision": str(forecast_record.get("capacityRevision") or ""),
            "backtests": ordered,
        }
    )


def build_predictive_planning_proof(
    *,
    authoritative_inputs: dict[str, Any],
    whatif_result: dict[str, Any],
    capacity_observations: Sequence[dict[str, Any]],
    forecast_backtests: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build and self-validate one typed payload from production snapshots and measured state."""
    source_snapshot = copy.deepcopy(authoritative_inputs.get("observedSnapshot") or {})
    forecast_record = copy.deepcopy(authoritative_inputs.get("forecastRecord") or {})
    price_catalog = copy.deepcopy(authoritative_inputs.get("priceCatalog") or {})
    fresh_state = copy.deepcopy(authoritative_inputs.get("freshStateBundle") or {})
    candidate_plan = copy.deepcopy(whatif_result.get("candidatePlan") or {})
    simulation = copy.deepcopy(whatif_result.get("simulation") or {})
    observations = [copy.deepcopy(item) for item in capacity_observations]
    backtests = [copy.deepcopy(item) for item in forecast_backtests]
    raw_forecast = forecast_record.get("forecast")
    forecast: dict[str, Any] = raw_forecast if isinstance(raw_forecast, dict) else {}
    payload: dict[str, Any] = {
        "schema": PREDICTIVE_PLANNING_PROOF_SCHEMA,
        "sourceSnapshot": source_snapshot,
        "sourceSnapshotDigest": str(source_snapshot.get("riskDigest") or ""),
        "authorityHeadDigest": str(authoritative_inputs.get("authorityHeadDigest") or ""),
        "capacityObservations": observations,
        "capacityObservationSetDigest": str(forecast_record.get("capacityObservationSetDigest") or ""),
        "forecastRecord": forecast_record,
        "forecastDigest": str(forecast_record.get("forecastDigest") or forecast.get("forecastDigest") or ""),
        "forecastBacktests": backtests,
        "forecastBacktestDigest": _backtest_set_digest(backtests, forecast_record=forecast_record),
        "priceCatalog": price_catalog,
        "priceCatalogDigest": str(price_catalog.get("priceCatalogDigest") or ""),
        "candidatePlan": candidate_plan,
        "candidatePlanDigest": str(candidate_plan.get("candidatePlanDigest") or ""),
        "freshStateBundle": fresh_state,
        "freshStateBundleDigest": str(authoritative_inputs.get("freshStateBundleDigest") or ""),
        "simulation": simulation,
        "whatIfResult": copy.deepcopy(whatif_result),
        "whatIfDigest": str(whatif_result.get("whatIfDigest") or ""),
    }
    payload["proofDigest"] = _digest(payload)
    errors = validate_predictive_planning_proof(payload)
    if errors:
        raise ValueError("invalid predictive planning proof: " + "; ".join(errors))
    return payload


def capture_predictive_planning_proof(
    *,
    authoritative_inputs: dict[str, Any],
    whatif_result: dict[str, Any],
) -> dict[str, Any]:
    """Read the exact durable series bound by the optimizer and build its typed proof."""
    from deepseek_infra.infra.workspace import resilience_capacity_history, resilience_forecast_backtest

    raw_record = authoritative_inputs.get("forecastRecord")
    record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
    target_id = str(record.get("targetId") or "")
    incarnation = str(record.get("targetIncarnation") or "")
    revision = str(record.get("capacityRevision") or "")
    if not target_id or not incarnation or not revision:
        raise ValueError("forecast record series binding is required")
    observations = resilience_capacity_history.list_capacity_observations(
        target_id,
        target_incarnation=incarnation,
        capacity_revision=revision,
        limit=100000,
    )
    backtests = resilience_forecast_backtest.list_forecast_backtests(
        target_id,
        target_incarnation=incarnation,
        capacity_revision=revision,
        limit=100000,
    )
    return build_predictive_planning_proof(
        authoritative_inputs=authoritative_inputs,
        whatif_result=whatif_result,
        capacity_observations=observations,
        forecast_backtests=backtests,
    )


def _as_dict(value: Any, field: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{field}-must-be-object")
        return {}
    return value


def _as_list(value: Any, field: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{field}-must-be-list")
        return []
    return value


def _as_int(value: Any, field: str, errors: list[str]) -> int | None:
    if isinstance(value, bool):
        errors.append(f"{field}-must-be-integer")
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{field}-must-be-integer")
        return None


def _same_number(actual: Any, expected: float | int | None) -> bool:
    if expected is None:
        return actual is None
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9)


def _validate_source_and_fresh_state(payload: dict[str, Any], errors: list[str]) -> None:
    source = _as_dict(payload.get("sourceSnapshot"), "source-snapshot", errors)
    declared_source = str(payload.get("sourceSnapshotDigest") or "")
    computed_source = _risk_digest(source)
    if source and str(source.get("riskDigest") or "") != computed_source:
        errors.append("source-snapshot-digest-mismatch")
    if declared_source != computed_source:
        errors.append("source-snapshot-binding-mismatch")

    fresh = _as_dict(payload.get("freshStateBundle"), "fresh-state-bundle", errors)
    authority = str(payload.get("authorityHeadDigest") or "")
    for field, value in (
        ("sourceSnapshotDigest", declared_source),
        ("authorityHeadDigest", authority),
        ("freshStateBundleDigest", payload.get("freshStateBundleDigest")),
    ):
        if not _is_sha256(value):
            errors.append(f"invalid-sha256:{field}")
    if not fresh:
        return
    components = {
        "capacitySnapshotDigest": fresh.get("capacitySnapshot"),
        "runningEffectsDigest": fresh.get("runningEffects"),
        "budgetRevision": fresh.get("budgets"),
        "maintenanceDecisionDigest": fresh.get("maintenanceDecisions"),
        "blastSimulationDigest": fresh.get("blastSimulation"),
    }
    for digest_field, value in components.items():
        if str(fresh.get(digest_field) or "") != _digest(value):
            errors.append(f"fresh-state-component-digest-mismatch:{digest_field}")
    if str(fresh.get("authorityHeadDigest") or "") != authority:
        errors.append("fresh-state-authority-binding-mismatch")
    authority_state = _as_dict(fresh.get("authorityState"), "authority-state", errors)
    if authority_state and str(authority_state.get("canonicalDigest") or "") != authority:
        errors.append("authority-head-state-mismatch")
    if str(fresh.get("riskDigest") or "") != declared_source:
        errors.append("fresh-state-risk-binding-mismatch")
    if fresh.get("riskSnapshot") != source:
        errors.append("fresh-state-risk-snapshot-mismatch")
    binding = {
        "authorityHeadDigest": fresh.get("authorityHeadDigest"),
        "riskDigest": fresh.get("riskDigest"),
        "capacitySnapshotDigest": fresh.get("capacitySnapshotDigest"),
        "runningEffectsDigest": fresh.get("runningEffectsDigest"),
        "budgetRevision": fresh.get("budgetRevision"),
        "maintenanceDecisionDigest": fresh.get("maintenanceDecisionDigest"),
        "blastSimulationDigest": fresh.get("blastSimulationDigest"),
        "observedAt": fresh.get("observedAt"),
    }
    expected = _digest(binding)
    if str(fresh.get("freshStateBundleDigest") or "") != expected:
        errors.append("fresh-state-bundle-digest-mismatch")
    if str(payload.get("freshStateBundleDigest") or "") != expected:
        errors.append("fresh-state-top-level-binding-mismatch")


def _validate_observations_and_forecast(payload: dict[str, Any], errors: list[str]) -> None:
    raw_observations = _as_list(payload.get("capacityObservations"), "capacity-observations", errors)
    observations: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_observations):
        observation = _as_dict(raw, f"capacity-observation-{index}", errors)
        if not observation:
            continue
        observations.append(observation)
        declared = str(observation.get("observationDigest") or "")
        expected = _digest({key: value for key, value in observation.items() if key != "observationDigest"})
        if declared != expected:
            errors.append(f"observation-digest-mismatch:{index}")
        if str(observation.get("source") or "") != "minio-probe":
            errors.append(f"observation-not-production-probe:{index}")
        if str(observation.get("probeSource") or "") in {"", "caller", "manual"}:
            errors.append(f"observation-probe-source-untrusted:{index}")
    if len(observations) < 3 or len({int(item.get("usedBytes") or 0) for item in observations}) < 2:
        errors.append("capacity-observations-did-not-change")

    record = _as_dict(payload.get("forecastRecord"), "forecast-record", errors)
    forecast = _as_dict(record.get("forecast"), "forecast", errors)
    if not observations or not record or not forecast:
        return
    target_id = str(record.get("targetId") or "")
    incarnation = str(record.get("targetIncarnation") or "")
    revision = str(record.get("capacityRevision") or "")
    for index, observation in enumerate(observations):
        if (
            str(observation.get("targetId") or "") != target_id
            or str(observation.get("targetIncarnation") or "") != incarnation
            or str(observation.get("capacityRevision") or "") != revision
        ):
            errors.append(f"observation-series-binding-mismatch:{index}")
    expected_set = _observation_set_digest(observations, forecast_record=record)
    for field, value in (
        ("payload", payload.get("capacityObservationSetDigest")),
        ("record", record.get("capacityObservationSetDigest")),
        ("forecast", forecast.get("capacityObservationSetDigest")),
    ):
        if str(value or "") != expected_set:
            errors.append(f"capacity-observation-set-digest-mismatch:{field}")

    expected_forecast = _digest({key: value for key, value in forecast.items() if key != "forecastDigest"})
    for field, value in (
        ("payload", payload.get("forecastDigest")),
        ("record", record.get("forecastDigest")),
        ("forecast", forecast.get("forecastDigest")),
    ):
        if str(value or "") != expected_forecast:
            errors.append(f"forecast-digest-mismatch:{field}")
    for field in ("targetId", "targetIncarnation", "capacityRevision", "horizonDays", "p50FreeBytes", "p90FreeBytes"):
        if record.get(field) != forecast.get(field):
            errors.append(f"forecast-record-field-mismatch:{field}")
    if str(forecast.get("forecastStatus") or "") != "OK":
        errors.append("forecast-record-not-ok")
    if str(record.get("status") or "") not in {"ACTIVE", "DUE"}:
        errors.append("forecast-record-not-current")
    binding = {
        "targetId": record.get("targetId"),
        "targetIncarnation": record.get("targetIncarnation"),
        "capacityRevision": record.get("capacityRevision"),
        "horizonDays": record.get("horizonDays"),
        "forecastedAt": record.get("forecastedAt"),
        "evaluationDueAt": record.get("evaluationDueAt"),
        "forecastDigest": record.get("forecastDigest"),
        "capacityObservationSetDigest": record.get("capacityObservationSetDigest"),
    }
    if str(record.get("forecastId") or "") != f"forecast:{_digest(binding)}":
        errors.append("forecast-id-binding-mismatch")


def _validate_backtests(payload: dict[str, Any], errors: list[str]) -> None:
    record = _as_dict(payload.get("forecastRecord"), "forecast-record", errors)
    forecast = _as_dict(record.get("forecast"), "forecast", errors)
    raw_backtests = _as_list(payload.get("forecastBacktests"), "forecast-backtests", errors)
    backtests: list[dict[str, Any]] = []
    if not raw_backtests:
        errors.append("forecast-backtests-empty")
        return
    raw_observations = payload.get("capacityObservations")
    observation_keys = {
        str(item.get("observationKey") or "")
        for item in (raw_observations if isinstance(raw_observations, list) else [])
        if isinstance(item, dict)
    }
    for index, raw in enumerate(raw_backtests):
        backtest = _as_dict(raw, f"forecast-backtest-{index}", errors)
        if not backtest:
            continue
        backtests.append(backtest)
        p50 = _as_int(backtest.get("predictedP50FreeBytes"), f"backtest-{index}-p50", errors)
        p90 = _as_int(backtest.get("predictedP90FreeBytes"), f"backtest-{index}-p90", errors)
        actual = _as_int(backtest.get("actualFreeBytes"), f"backtest-{index}-actual", errors)
        if p50 is not None and p90 is not None and actual is not None:
            mae = float(abs(p50 - actual))
            mape = None if actual == 0 else mae / abs(actual)
            bias = float(p50 - actual)
            interval_hit = actual >= p90
            for field, expected in (("mae", mae), ("mape", mape), ("bias", bias)):
                if not _same_number(backtest.get(field), expected):
                    errors.append(f"forecast-backtest-metric-mismatch:{index}:{field}")
            if backtest.get("intervalHit") is not interval_hit:
                errors.append(f"forecast-backtest-metric-mismatch:{index}:intervalHit")
        if (
            str(backtest.get("targetId") or "") != str(record.get("targetId") or "")
            or str(backtest.get("targetIncarnation") or "") != str(record.get("targetIncarnation") or "")
            or str(backtest.get("capacityRevision") or "") != str(record.get("capacityRevision") or "")
        ):
            errors.append(f"forecast-backtest-series-binding-mismatch:{index}")
        if str(backtest.get("actualObservationKey") or "") not in observation_keys:
            errors.append(f"forecast-backtest-observation-binding-mismatch:{index}")
        if not _is_sha256(backtest.get("forecastDigest")):
            errors.append(f"forecast-backtest-invalid-forecast-digest:{index}")

    expected_set = _backtest_set_digest(backtests, forecast_record=record)
    if str(payload.get("forecastBacktestDigest") or "") != expected_set:
        errors.append("forecast-backtest-digest-mismatch")
    if not backtests or not forecast:
        return
    mae = round(sum(float(item["mae"]) for item in backtests) / len(backtests), 3)
    mape_values = [float(item["mape"]) for item in backtests if item.get("mape") is not None]
    mape = None if not mape_values else round(sum(mape_values) / len(mape_values), 6)
    bias = round(sum(float(item["bias"]) for item in backtests) / len(backtests), 3)
    coverage = round(sum(1 if item.get("intervalHit") is True else 0 for item in backtests) / len(backtests), 6)
    calibration_result: dict[str, Any] = {
        "targetId": str(record.get("targetId") or ""),
        "samples": len(backtests),
        "mae": mae,
        "mape": mape,
        "bias": bias,
        "intervalCoverage": coverage,
        "overoptimistic": bias > 0,
    }
    calibration_digest = _digest(
        {
            **calibration_result,
            "backtests": [
                {
                    "backtestKey": str(item.get("backtestKey") or ""),
                    "forecastDigest": str(item.get("forecastDigest") or ""),
                    "actualObservationKey": str(item.get("actualObservationKey") or ""),
                }
                for item in sorted(backtests, key=lambda item: (str(item.get("evaluatedAt") or ""), str(item.get("backtestKey") or "")))
            ],
        }
    )
    calibration = _as_dict(forecast.get("calibration"), "forecast-calibration", errors)
    for field in ("samples", "mae", "mape", "bias", "intervalCoverage"):
        expected_value = calibration_result[field]
        if not isinstance(expected_value, (int, float)) and expected_value is not None:
            errors.append(f"forecast-calibration-mismatch:{field}")
        elif not _same_number(calibration.get(field), expected_value):
            errors.append(f"forecast-calibration-mismatch:{field}")
    if str(calibration.get("calibrationDigest") or "") != calibration_digest:
        errors.append("forecast-calibration-digest-mismatch")


def _validate_catalog_and_plan(payload: dict[str, Any], errors: list[str]) -> None:
    catalog = _as_dict(payload.get("priceCatalog"), "price-catalog", errors)
    expected_catalog = _digest(
        {
            "priceCatalogVersion": int(catalog.get("priceCatalogVersion") or 0),
            "targets": catalog.get("targets") or {},
        }
    )
    if str(catalog.get("priceCatalogDigest") or "") != expected_catalog:
        errors.append("price-catalog-digest-mismatch:catalog")
    if str(payload.get("priceCatalogDigest") or "") != expected_catalog:
        errors.append("price-catalog-digest-mismatch:payload")

    plan = _as_dict(payload.get("candidatePlan"), "candidate-plan", errors)
    baseline = _as_dict(plan.get("baseline"), "candidate-plan-baseline", errors)
    selected = _as_dict(plan.get("selected"), "candidate-plan-selected", errors)
    candidate = _as_dict(selected.get("candidate"), "candidate-plan-candidate", errors)
    if not plan or not baseline or not selected or not candidate:
        return
    expected_plan = _digest(
        {
            "selected": candidate,
            "baseline": baseline,
            "sourceSnapshotDigest": plan.get("sourceSnapshotDigest"),
            "authorityHeadDigest": plan.get("authorityHeadDigest"),
            "forecastDigest": plan.get("forecastDigest"),
            "priceCatalogDigest": plan.get("priceCatalogDigest"),
        }
    )
    if str(plan.get("candidatePlanDigest") or "") != expected_plan:
        errors.append("candidate-plan-digest-mismatch:plan")
    if str(payload.get("candidatePlanDigest") or "") != expected_plan:
        errors.append("candidate-plan-digest-mismatch:payload")
    for field in ("sourceSnapshotDigest", "authorityHeadDigest", "forecastDigest", "priceCatalogDigest"):
        if str(plan.get(field) or "") != str(payload.get(field) or ""):
            errors.append(f"candidate-plan-input-binding-mismatch:{field}")
    if str(plan.get("status") or "") != "OK" or selected.get("accepted") is not True:
        errors.append("candidate-plan-not-accepted")
    if selected.get("violations") not in ([], None):
        errors.append("candidate-plan-declared-violations")

    candidate_copies = _as_int(candidate.get("committedCopies"), "candidate-committed-copies", errors)
    candidate_domains = _as_int(candidate.get("failureDomains"), "candidate-failure-domains", errors)
    min_copies = _as_int(baseline.get("minCommittedCopies"), "baseline-min-committed-copies", errors)
    min_domains = _as_int(baseline.get("minFailureDomains"), "baseline-min-failure-domains", errors)
    baseline_copies = _as_int(baseline.get("committedCopies"), "baseline-committed-copies", errors)
    baseline_domains = _as_int(baseline.get("failureDomains"), "baseline-failure-domains", errors)
    if candidate_copies is not None and min_copies is not None and candidate_copies < min_copies:
        errors.append("unsafe-plan-min-committed-copies")
    if candidate_domains is not None and min_domains is not None and candidate_domains < min_domains:
        errors.append("unsafe-plan-min-failure-domains")
    if candidate_copies is not None and baseline_copies is not None and candidate_copies < baseline_copies:
        errors.append("unsafe-plan-baseline-committed-copies")
    if candidate_domains is not None and baseline_domains is not None and candidate_domains < baseline_domains:
        errors.append("unsafe-plan-baseline-failure-domains")
    forecast_free = _as_int(candidate.get("forecastFreeBytes"), "candidate-forecast-free-bytes", errors)
    headroom = _as_int(baseline.get("forecastSafetyHeadroomBytes"), "baseline-forecast-headroom-bytes", errors)
    if forecast_free is not None and headroom is not None and forecast_free < headroom:
        errors.append("unsafe-plan-forecast-headroom")
    if candidate.get("breaksDrDependency") is True:
        errors.append("unsafe-plan-breaks-dr-dependency")
    if candidate.get("mutatesAuthority") is True:
        errors.append("unsafe-plan-mutates-authority")


def _validate_simulation(payload: dict[str, Any], errors: list[str]) -> None:
    simulation = _as_dict(payload.get("simulation"), "simulation", errors)
    whatif = _as_dict(payload.get("whatIfResult"), "what-if-result", errors)
    if not simulation or not whatif:
        return
    expected_whatif = _digest({key: value for key, value in whatif.items() if key != "whatIfDigest"})
    if str(whatif.get("whatIfDigest") or "") != expected_whatif:
        errors.append("what-if-digest-mismatch:result")
    if str(payload.get("whatIfDigest") or "") != expected_whatif:
        errors.append("what-if-digest-mismatch:payload")
    if whatif.get("simulation") != simulation:
        errors.append("what-if-simulation-binding-mismatch")
    if whatif.get("candidatePlan") != payload.get("candidatePlan"):
        errors.append("what-if-candidate-plan-binding-mismatch")
    for field in ("sourceSnapshotDigest", "authorityHeadDigest", "forecastDigest", "priceCatalogDigest", "freshStateBundleDigest"):
        if str(whatif.get(field) or "") != str(payload.get(field) or ""):
            errors.append(f"what-if-input-binding-mismatch:{field}")

    before = _as_dict(simulation.get("preStateDigests"), "simulation-pre-state-digests", errors)
    after = _as_dict(simulation.get("postStateDigests"), "simulation-post-state-digests", errors)
    for field, digests in (("pre", before), ("post", after)):
        for domain in _STATE_DOMAINS:
            if not _is_sha256(digests.get(domain)):
                errors.append(f"simulation-{field}-state-digest-invalid:{domain}")
    expected_before = _digest(before)
    expected_after = _digest(after)
    if str(simulation.get("preStateDigest") or "") != expected_before:
        errors.append("simulation-pre-state-digest-mismatch")
    if str(simulation.get("postStateDigest") or "") != expected_after:
        errors.append("simulation-post-state-digest-mismatch")
    if (
        before != after
        or expected_before != expected_after
        or simulation.get("storageInventoryBefore") != simulation.get("storageInventoryAfter")
        or simulation.get("stateUnchanged") is not True
        or simulation.get("changedDomains") != []
    ):
        errors.append("simulation-state-changed")

    attempted = _as_list(simulation.get("attemptedWrites"), "simulation-attempted-writes", errors)
    blocked = _as_list(simulation.get("blockedWrites"), "simulation-blocked-writes", errors)
    attempted_count = _as_int(simulation.get("attemptedMutationCount"), "simulation-attempted-mutation-count", errors)
    blocked_count = _as_int(simulation.get("blockedMutationCount"), "simulation-blocked-mutation-count", errors)
    if attempted_count is not None and attempted_count != len(attempted):
        errors.append("simulation-attempt-count-mismatch")
    if blocked_count is not None and blocked_count != len(blocked):
        errors.append("simulation-blocked-count-mismatch")
    if attempted or attempted_count not in {0}:
        errors.append("simulation-attempted-mutation")
    if blocked or blocked_count not in {0}:
        errors.append("simulation-blocked-mutation")
    raw_changed_domains = simulation.get("changedDomains")
    changed_count = len(raw_changed_domains) if isinstance(raw_changed_domains, list) else 1
    expected_side_effects = len(attempted) + changed_count
    side_effects = _as_int(whatif.get("sideEffectsObserved"), "what-if-side-effects-observed", errors)
    if side_effects != expected_side_effects:
        errors.append("what-if-side-effect-count-mismatch")
    if str(whatif.get("status") or "") != "OK":
        errors.append("what-if-result-not-ok")


def validate_predictive_planning_proof(payload: dict[str, Any]) -> list[str]:
    """Recompute every typed binding; return semantic errors instead of trusting declared PASS."""
    if not isinstance(payload, dict):
        return ["predictive-proof-must-be-object"]
    errors: list[str] = []
    if str(payload.get("schema") or "") != PREDICTIVE_PLANNING_PROOF_SCHEMA:
        errors.append("predictive-proof-schema-mismatch")
    for field in (
        "sourceSnapshotDigest",
        "authorityHeadDigest",
        "capacityObservationSetDigest",
        "forecastDigest",
        "forecastBacktestDigest",
        "priceCatalogDigest",
        "candidatePlanDigest",
        "freshStateBundleDigest",
        "whatIfDigest",
        "proofDigest",
    ):
        if not _is_sha256(payload.get(field)):
            errors.append(f"invalid-sha256:{field}")
    expected_proof = _digest({key: value for key, value in payload.items() if key != "proofDigest"})
    if str(payload.get("proofDigest") or "") != expected_proof:
        errors.append("predictive-proof-digest-mismatch")
    _validate_source_and_fresh_state(payload, errors)
    _validate_observations_and_forecast(payload, errors)
    _validate_backtests(payload, errors)
    _validate_catalog_and_plan(payload, errors)
    _validate_simulation(payload, errors)
    return errors
