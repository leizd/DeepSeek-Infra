"""Predictive planning evidence names and semantic validators."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import evidence_proof, resilience_risk_engine
from deepseek_infra.web.server import create_server
from scripts import run_storage_control_plane_minio_e2e


PREDICTIVE_CHECK_NAMES = (
    "absentAuthoritativeRiskIsClosedOrRetired",
    "supersededBackupRiskCannotRemainOpenForever",
    "policyDisabledRiskIsRetired",
    "unknownCoverageDoesNotImplicitlyClearRisk",
    "schedulerReservationDoesNotCountAsConsumedService",
    "preemptedActionReleasesFairnessReservation",
    "completedActionChargesObservedBytesExactlyOnce",
    "serviceConsumptionSurvivesRestart",
    "waveOneCannotStartBeforeWaveZeroVerified",
    "failedWavePausesDownstreamActions",
    "staleWaveRequiresReplan",
    "waveRevalidatesFreshRiskBeforeExecution",
    "waveRevalidatesAuthorityBeforeExecution",
    "waveRevalidatesBlastRadiusBeforeExecution",
    "fleetSloExposes1h24h7d30dWindows",
    "insufficientSloSamplesAreExplicit",
    "capacityForecastUsesDurableObservations",
    "forecastWithInsufficientSamplesFailsClosed",
    "thirtyDayCapacityForecastProduced",
    "ninetyDayCapacityForecastProduced",
    "forecastProvidesP50AndP90Headroom",
    "forecastBacktestErrorIsPersisted",
    "overoptimisticForecastLowersConfidence",
    "costModelUsesVersionedPriceCatalog",
    "unknownTargetPriceDoesNotBecomeZero",
    "egressCostIsIncluded",
    "storageCostIsIncluded",
    "optimizerNeverReducesMinCommittedCopies",
    "optimizerNeverReducesMinFailureDomains",
    "optimizerRejectsUnsafeCheaperPlan",
    "candidatePlanIsDeterministicForSameInputs",
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
    "federationSnapshotContainsNoCredentials",
    "federationSnapshotIsDigestBound",
    "incompatibleFleetWireVersionFailsClosed",
    "federatedSimulationCannotMutateRemoteFleet",
)


def _passing_evidence(name: str) -> dict[str, object]:
    digest = "a" * 64
    base: dict[str, object] = {
        "status": "SUPERSEDED",
        "closureReason": "SUPERSEDED_BACKUP",
        "coverageComplete": True,
        "previousBackupId": "backup-100",
        "reservationStatus": "RESERVED",
        "actionsServed": 0,
        "releaseReason": "PREEMPTED",
        "actualBytes": 12,
        "virtualRuntime": 1.0,
        "admitted": False,
        "reason": "PREDECESSOR_WAVE_NOT_VERIFIED",
        "scheduleStatus": "PAUSED_REPLAN",
        "revalidatedRisk": True,
        "revalidatedAuthority": True,
        "revalidatedBlastRadius": True,
        "windows": ["1h", "24h", "7d", "30d", "lifetime"],
        "forecastStatus": "INSUFFICIENT_DATA",
        "sampleCount": 0,
        "horizonDays": 30 if name.startswith("thirty") else 90,
        "p50FreeBytes": 10,
        "p90FreeBytes": 8,
        "mae": 1.0,
        "bias": 2.0,
        "overoptimistic": True,
        "confidence": "low",
        "priceCatalogVersion": 1,
        "priceCatalogDigest": digest,
        "forecastDigest": digest,
        "authorityHeadDigest": digest,
        "sourceSnapshotDigest": digest,
        "snapshotDigest": digest,
        "candidatePlanDigest": digest,
        "repeatDigest": digest,
        "egress": 0.01,
        "storage": 0.02,
        "accepted": False,
        "violations": ["MIN_COMMITTED_COPIES_REDUCED"],
        "s3PutCount": 0,
        "s3DeleteCount": 0,
        "authorityMutationCount": 0,
        "actionJournalMutationCount": 0,
        "sideEffectsObserved": 0,
        "runningEffects": [],
        "maintenanceWindows": [],
        "durability": {"copiesPreserved": True, "failureDomainsPreserved": True},
        "forbiddenKeys": [],
        "remoteMutations": 0,
    }
    if name == "unknownCoverageDoesNotImplicitlyClearRisk":
        base["status"] = "UNKNOWN_COVERAGE"
        base["closureReason"] = "UNKNOWN_COVERAGE"
    if name == "schedulerReservationDoesNotCountAsConsumedService":
        base["reservationStatus"] = "RESERVED"
        base["actionsServed"] = 0
    if name == "preemptedActionReleasesFairnessReservation":
        base["reservationStatus"] = "RELEASED"
    if name == "completedActionChargesObservedBytesExactlyOnce":
        base["actionsServed"] = 1
    if name == "insufficientSloSamplesAreExplicit":
        base["status"] = "INSUFFICIENT_DATA"
    if name == "unknownTargetPriceDoesNotBecomeZero":
        base["status"] = "UNKNOWN_COST"
        base["monthlyCost"] = None
    if name == "incompatibleFleetWireVersionFailsClosed":
        base["status"] = "INCOMPATIBLE"
    if name == "staleWaveRequiresReplan":
        base["scheduleStatus"] = "PAUSED_REPLAN"
    return base


def test_475_required_check_names_are_locked() -> None:
    assert set(PREDICTIVE_CHECK_NAMES) <= set(run_storage_control_plane_minio_e2e.CHECK_SCENARIOS)
    assert all(
        run_storage_control_plane_minio_e2e.CHECK_SCENARIOS[name]
        == run_storage_control_plane_minio_e2e.PREDICTIVE_FLEET_SCENARIO
        for name in PREDICTIVE_CHECK_NAMES
    )
    for name in PREDICTIVE_CHECK_NAMES:
        errors = evidence_proof.validate_check(name, {"status": "PASS", "evidence": _passing_evidence(name)})
        assert errors == [], (name, errors)
    assert evidence_proof.validate_check(
        "unknownCoverageDoesNotImplicitlyClearRisk",
        {"status": "PASS", "evidence": {**_passing_evidence("unknownCoverageDoesNotImplicitlyClearRisk"), "status": "CLEARED"}},
    )
    assert evidence_proof.validate_predictive_planning_proof({}, "absentAuthoritativeRiskIsClosedOrRetired")


def test_assess_risks_includes_authoritative_coverage(tmp_settings: Path) -> None:
    snapshot = resilience_risk_engine.assess_risks(target_ids=[], policy_ids=[])
    assert snapshot["coverage"]["CAPACITY_EXHAUSTION"]["complete"] is False
    assert snapshot["coverage"]["DR_STALENESS"]["complete"] is True


def test_predictive_api_routes_are_authenticated(tmp_settings: Path) -> None:
    server, _ = create_server(0, host="127.0.0.1")
    client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert client.get("/api/workspace/resilience/waves").status_code == 401
    assert client.post("/api/workspace/resilience/waves/run", json={}).status_code == 401
    headers = {"Authorization": f"Bearer {settings.auth.token}", "X-DeepSeek-Client": "test"}
    waves = client.get("/api/workspace/resilience/waves", headers=headers)
    assert waves.status_code == 200
    forecast = client.get("/api/workspace/resilience/capacity-forecast", headers=headers)
    assert forecast.status_code == 200
    federation = client.get("/api/workspace/resilience/federation", headers=headers)
    assert federation.status_code == 200
    assert "credential" not in federation.json()
    whatif = client.post(
        "/api/workspace/resilience/whatif",
        headers=headers,
        json={
            "observedSnapshot": {"riskDigest": "a" * 64},
            "forecast": {"forecastStatus": "INSUFFICIENT_DATA"},
            "candidate": {"targetId": "t", "committedCopies": 1, "failureDomains": 1},
            "baseline": {"minCommittedCopies": 1, "minFailureDomains": 1, "committedCopies": 1, "failureDomains": 1},
            "runningEffects": [],
            "maintenanceWindows": [],
        },
    )
    assert whatif.status_code == 200
    assert whatif.json()["s3PutCount"] == 0
    missing = client.post("/api/workspace/resilience/waves/admit", headers=headers, json={})
    assert missing.status_code == 400
    missing_run = client.post("/api/workspace/resilience/waves/run", headers=headers, json={})
    assert missing_run.status_code == 400
