"""Typed predictive planning proof and independent semantic validation (4.7.6 Gate J)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import (
    evidence_proof,
    resilience_capacity_history,
    resilience_forecast_backtest,
    resilience_placement_optimizer,
    resilience_predictive_proof,
    resilience_risk_engine,
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _observation(index: int, *, used_bytes: int) -> dict[str, Any]:
    payload = {
        "observationKey": f"capacity:target-a:inc-a:2026-01-{index + 1:02d}T00:00:00Z",
        "targetId": "target-a",
        "observedAt": f"2026-01-{index + 1:02d}T00:00:00Z",
        "usedBytes": used_bytes,
        "freeBytes": 10_000 - used_bytes,
        "totalBytes": 10_000,
        "backupBytesWritten": max(0, used_bytes - 1_000),
        "replicationBytesIn": 0,
        "replicationBytesOut": 0,
        "rebalanceBytesIn": 0,
        "rebalanceBytesOut": 0,
        "activePolicies": 1,
        "source": "minio-probe",
        "probeSource": "s3-list-objects-v2",
        "targetIncarnation": "inc-a",
        "capacityRevision": "revision-a",
        "provenance": {"endpoint": "minio-a", "bucket": "backup-a"},
    }
    payload["observationDigest"] = _digest(payload)
    return payload


def _proof_inputs() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    observations = [_observation(index, used_bytes=1_000 + index * 100) for index in range(4)]
    observation_set_digest = _digest(
        {
            "targetId": "target-a",
            "targetIncarnation": "inc-a",
            "capacityRevision": "revision-a",
            "observations": [
                {"observationKey": item["observationKey"], "observationDigest": item["observationDigest"]}
                for item in observations
            ],
        }
    )
    backtests = [
        {
            "backtestKey": "backtest:forecast-prior",
            "forecastId": "forecast-prior",
            "targetId": "target-a",
            "forecastedAt": "2025-12-01T00:00:00Z",
            "evaluatedAt": "2026-01-04T00:00:00Z",
            "horizonDays": 30,
            "predictedP50FreeBytes": 8_650,
            "predictedP90FreeBytes": 8_500,
            "actualFreeBytes": 8_700,
            "mae": 50.0,
            "mape": 50.0 / 8_700.0,
            "bias": -50.0,
            "intervalHit": True,
            "targetIncarnation": "inc-a",
            "capacityRevision": "revision-a",
            "forecastDigest": "d" * 64,
            "actualObservationKey": observations[-1]["observationKey"],
        }
    ]
    calibration = {
        "targetId": "target-a",
        "samples": 1,
        "mae": 50.0,
        "mape": round(50.0 / 8_700.0, 6),
        "bias": -50.0,
        "intervalCoverage": 1.0,
        "overoptimistic": False,
    }
    calibration_digest = _digest(
        {
            **calibration,
            "backtests": [
                {
                    "backtestKey": backtests[0]["backtestKey"],
                    "forecastDigest": backtests[0]["forecastDigest"],
                    "actualObservationKey": backtests[0]["actualObservationKey"],
                }
            ],
        }
    )
    forecast = {
        "targetId": "target-a",
        "targetIncarnation": "inc-a",
        "capacityRevision": "revision-a",
        "horizonDays": 30,
        "forecastStatus": "OK",
        "p50FreeBytes": 5_700,
        "p90FreeBytes": 5_400,
        "daysToWarningWatermarkP50": 37.0,
        "daysToWarningWatermarkP90": 34.0,
        "observationWindowDays": 3.0,
        "sampleCount": 4,
        "confidence": "low",
        "calibration": {
            "samples": 1,
            "mae": 50.0,
            "mape": round(50.0 / 8_700.0, 6),
            "bias": -50.0,
            "intervalCoverage": 1.0,
            "calibrationDigest": calibration_digest,
        },
        "capacityObservationSetDigest": observation_set_digest,
        "p50GrowthBytesPerDay": 100.0,
        "p90GrowthBytesPerDay": 100.0,
        "generatedAt": "2026-01-04T00:00:00Z",
    }
    forecast["forecastDigest"] = _digest(forecast)
    forecast_binding = {
        "targetId": "target-a",
        "targetIncarnation": "inc-a",
        "capacityRevision": "revision-a",
        "horizonDays": 30,
        "forecastedAt": "2026-01-04T00:00:00Z",
        "evaluationDueAt": "2026-02-03T00:00:00Z",
        "forecastDigest": forecast["forecastDigest"],
        "capacityObservationSetDigest": observation_set_digest,
    }
    forecast_record = {
        "forecastId": f"forecast:{_digest(forecast_binding)}",
        **forecast_binding,
        "p50FreeBytes": forecast["p50FreeBytes"],
        "p90FreeBytes": forecast["p90FreeBytes"],
        "status": "ACTIVE",
        "actualObservationKey": "",
        "backtestKey": "",
        "forecast": forecast,
    }
    risk_snapshot: dict[str, Any] = {
        "riskSnapshotVersion": "risk-snapshot-v1",
        "overallRisk": "warning",
        "risks": [],
    }
    risk_snapshot["riskDigest"] = resilience_risk_engine.compute_risk_digest(risk_snapshot)
    capacity_snapshot = {"targets": [{"targetId": "target-a", "freeBytes": 8_700}]}
    running_effects: list[dict[str, Any]] = []
    budgets = {"admitted": True, "transferBudget": {"availableBytes": 10_000}}
    maintenance = [{"actionId": "whatif-candidate", "allowed": True}]
    blast = {"passed": True, "evaluations": []}
    fresh_binding = {
        "authorityHeadDigest": "a" * 64,
        "riskDigest": risk_snapshot["riskDigest"],
        "capacitySnapshotDigest": _digest(capacity_snapshot),
        "runningEffectsDigest": _digest(running_effects),
        "budgetRevision": _digest(budgets),
        "maintenanceDecisionDigest": _digest(maintenance),
        "blastSimulationDigest": _digest(blast),
        "observedAt": "2026-01-04T00:00:00Z",
    }
    fresh_state = {
        **fresh_binding,
        "authorityState": {"canonicalDigest": "a" * 64, "canonicalGeneration": 7},
        "riskSnapshot": risk_snapshot,
        "capacitySnapshot": capacity_snapshot,
        "runningEffects": running_effects,
        "budgets": budgets,
        "maintenanceDecisions": maintenance,
        "blastSimulation": blast,
        "freshStateBundleDigest": _digest(fresh_binding),
    }
    price_catalog = {
        "priceCatalogVersion": 4,
        "targets": {"target-a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}},
    }
    price_catalog["priceCatalogDigest"] = _digest(
        {"priceCatalogVersion": price_catalog["priceCatalogVersion"], "targets": price_catalog["targets"]}
    )
    baseline = {
        "minCommittedCopies": 2,
        "minFailureDomains": 2,
        "committedCopies": 2,
        "failureDomains": 2,
        "forecastSafetyHeadroomBytes": 100,
    }
    candidate = {
        "targetId": "target-a",
        "committedCopies": 2,
        "failureDomains": 2,
        "storedBytes": 1024,
        "forecastFreeBytes": 5_400,
        "breaksDrDependency": False,
        "mutatesAuthority": False,
    }
    plan = resilience_placement_optimizer.optimize_placement(
        baseline=baseline,
        candidates=[candidate],
        catalog=price_catalog,
        source_snapshot_digest=str(risk_snapshot["riskDigest"]),
        authority_head_digest="a" * 64,
        forecast_digest=str(forecast["forecastDigest"]),
    )
    domain_digests = {
        "storage": "1" * 64,
        "authority": "2" * 64,
        "actionJournal": "3" * 64,
        "policy": "4" * 64,
        "target": "5" * 64,
    }
    state_digest = _digest(domain_digests)
    measured_simulation = {
        "attemptedWrites": [],
        "blockedWrites": [],
        "attemptedMutationCount": 0,
        "blockedMutationCount": 0,
        "preStateDigest": state_digest,
        "postStateDigest": state_digest,
        "preStateDigests": domain_digests,
        "postStateDigests": dict(domain_digests),
        "storageInventoryBefore": [{"targetId": "target-a", "objectCount": 9, "inventoryDigest": "6" * 64}],
        "storageInventoryAfter": [{"targetId": "target-a", "objectCount": 9, "inventoryDigest": "6" * 64}],
        "stateUnchanged": True,
        "changedDomains": [],
    }
    whatif = {
        "status": "OK",
        "candidatePlan": plan,
        "simulation": measured_simulation,
        "sourceSnapshotDigest": risk_snapshot["riskDigest"],
        "forecastDigest": forecast["forecastDigest"],
        "priceCatalogDigest": price_catalog["priceCatalogDigest"],
        "authorityHeadDigest": "a" * 64,
        "freshStateBundleDigest": fresh_state["freshStateBundleDigest"],
        "sideEffectsObserved": 0,
        "generatedAt": "2026-01-04T00:00:00Z",
    }
    whatif["whatIfDigest"] = _digest(whatif)
    inputs = {
        "observedSnapshot": risk_snapshot,
        "forecast": forecast,
        "forecastRecord": forecast_record,
        "priceCatalog": price_catalog,
        "authorityHeadDigest": "a" * 64,
        "freshStateBundleDigest": fresh_state["freshStateBundleDigest"],
        "freshStateBundle": fresh_state,
        "baseline": baseline,
    }
    return inputs, whatif, observations, backtests


def _valid_proof() -> dict[str, Any]:
    inputs, simulation, observations, backtests = _proof_inputs()
    return resilience_predictive_proof.build_predictive_planning_proof(
        authoritative_inputs=inputs,
        whatif_result=simulation,
        capacity_observations=observations,
        forecast_backtests=backtests,
    )


def _rebind_proof(proof: dict[str, Any]) -> None:
    proof["proofDigest"] = _digest({key: value for key, value in proof.items() if key != "proofDigest"})


def test_predictive_planning_proof_binds_all_authoritative_inputs(tmp_settings: Path) -> None:
    proof = _valid_proof()

    assert proof["schema"] == "predictive-planning-proof-v1"
    assert resilience_predictive_proof.validate_predictive_planning_proof(proof) == []
    assert evidence_proof.EVIDENCE_PROOF_SCHEMA == "evidence-proof-v2"
    for check_name in resilience_predictive_proof.PREDICTIVE_PROOF_CHECKS:
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []


def test_predictive_proof_capture_reads_exact_durable_series(tmp_settings: Path, monkeypatch: Any) -> None:
    inputs, simulation, observations, backtests = _proof_inputs()
    calls: list[tuple[str, str, str]] = []

    def read_observations(target_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((target_id, str(kwargs["target_incarnation"]), str(kwargs["capacity_revision"])))
        return observations

    def read_backtests(target_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((target_id, str(kwargs["target_incarnation"]), str(kwargs["capacity_revision"])))
        return backtests

    monkeypatch.setattr(resilience_capacity_history, "list_capacity_observations", read_observations)
    monkeypatch.setattr(resilience_forecast_backtest, "list_forecast_backtests", read_backtests)

    proof = resilience_predictive_proof.capture_predictive_planning_proof(
        authoritative_inputs=inputs,
        whatif_result=simulation,
    )

    assert calls == [("target-a", "inc-a", "revision-a"), ("target-a", "inc-a", "revision-a")]
    assert proof["capacityObservations"] == observations
    assert proof["forecastBacktests"] == backtests


def test_predictive_proof_rejects_observation_forecast_catalog_and_plan_tamper(tmp_settings: Path) -> None:
    tampered_observation = _valid_proof()
    tampered_observation["capacityObservations"][0]["usedBytes"] += 1
    _rebind_proof(tampered_observation)
    assert any("observation-digest-mismatch" in error for error in resilience_predictive_proof.validate_predictive_planning_proof(tampered_observation))

    tampered_forecast = _valid_proof()
    tampered_forecast["forecastRecord"]["forecast"]["p90FreeBytes"] -= 1
    _rebind_proof(tampered_forecast)
    assert any("forecast-digest-mismatch" in error for error in resilience_predictive_proof.validate_predictive_planning_proof(tampered_forecast))

    tampered_catalog = _valid_proof()
    tampered_catalog["priceCatalog"]["targets"]["target-a"]["egressPerGiB"] = 0
    _rebind_proof(tampered_catalog)
    assert any("price-catalog-digest-mismatch" in error for error in resilience_predictive_proof.validate_predictive_planning_proof(tampered_catalog))

    tampered_plan = _valid_proof()
    tampered_plan["candidatePlan"]["selected"]["candidate"]["committedCopies"] = 1
    plan = tampered_plan["candidatePlan"]
    plan["candidatePlanDigest"] = _digest(
        {
            "selected": plan["selected"]["candidate"],
            "baseline": plan["baseline"],
            "sourceSnapshotDigest": plan["sourceSnapshotDigest"],
            "authorityHeadDigest": plan["authorityHeadDigest"],
            "forecastDigest": plan["forecastDigest"],
            "priceCatalogDigest": plan["priceCatalogDigest"],
        }
    )
    tampered_plan["candidatePlanDigest"] = plan["candidatePlanDigest"]
    tampered_plan["whatIfResult"]["candidatePlan"] = copy.deepcopy(plan)
    whatif = tampered_plan["whatIfResult"]
    whatif["whatIfDigest"] = _digest({key: value for key, value in whatif.items() if key != "whatIfDigest"})
    tampered_plan["whatIfDigest"] = whatif["whatIfDigest"]
    _rebind_proof(tampered_plan)
    errors = resilience_predictive_proof.validate_predictive_planning_proof(tampered_plan)
    assert "unsafe-plan-min-committed-copies" in errors


def test_predictive_proof_rejects_state_change_and_self_reported_zero_mutation(tmp_settings: Path) -> None:
    changed = _valid_proof()
    changed["simulation"]["postStateDigests"]["policy"] = "9" * 64
    changed["simulation"]["postStateDigest"] = _digest(changed["simulation"]["postStateDigests"])
    changed["whatIfResult"]["simulation"] = copy.deepcopy(changed["simulation"])
    whatif = changed["whatIfResult"]
    whatif["whatIfDigest"] = _digest({key: value for key, value in whatif.items() if key != "whatIfDigest"})
    changed["whatIfDigest"] = whatif["whatIfDigest"]
    _rebind_proof(changed)
    assert "simulation-state-changed" in resilience_predictive_proof.validate_predictive_planning_proof(changed)

    self_reported = _valid_proof()
    self_reported["simulation"]["attemptedWrites"] = [{"domain": "storage", "operation": "put_if_absent"}]
    self_reported["simulation"]["attemptedMutationCount"] = 0
    self_reported["whatIfResult"]["simulation"] = copy.deepcopy(self_reported["simulation"])
    whatif = self_reported["whatIfResult"]
    whatif["whatIfDigest"] = _digest({key: value for key, value in whatif.items() if key != "whatIfDigest"})
    self_reported["whatIfDigest"] = whatif["whatIfDigest"]
    _rebind_proof(self_reported)
    errors = resilience_predictive_proof.validate_predictive_planning_proof(self_reported)
    assert "simulation-attempt-count-mismatch" in errors
    assert "simulation-attempted-mutation" in errors


def test_predictive_payload_remains_inside_unchanged_evidence_v2_envelope(tmp_settings: Path, tmp_path: Path) -> None:
    path = evidence_proof.write_evidence_proof(
        tmp_path / "predictive.json",
        scenario="real-three-minio-predictive-planning",
        checks={
            name: {"status": "PASS", "evidence": _valid_proof()}
            for name in resilience_predictive_proof.PREDICTIVE_PROOF_CHECKS
        },
    )

    envelope = evidence_proof.load_evidence_proof(path, expected_scenario="real-three-minio-predictive-planning")
    assert envelope["schema"] == "evidence-proof-v2"
