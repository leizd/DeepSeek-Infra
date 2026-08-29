"""Side-effect-free what-if simulation and optimization proof (4.7.5 Gates K-L)."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.workspace import (
    evidence_proof,
    resilience_cost_model,
    resilience_placement_optimizer,
    resilience_whatif,
)


def test_what_if_is_zero_mutation_and_binds_snapshot(tmp_settings: Path) -> None:
    catalog = resilience_cost_model.put_price_catalog(
        {
            "priceCatalogVersion": 1,
            "targets": {"target-a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}},
        }
    )
    baseline = {
        "minCommittedCopies": 2,
        "minFailureDomains": 2,
        "committedCopies": 2,
        "failureDomains": 2,
        "forecastSafetyHeadroomBytes": 10,
    }
    candidate = {
        "targetId": "target-a",
        "committedCopies": 2,
        "failureDomains": 2,
        "storedBytes": 1024**3,
        "forecastFreeBytes": 50,
    }
    snapshot = {"riskDigest": "d" * 64, "overallRisk": "warning"}
    forecast = {
        "forecastStatus": "OK",
        "p50FreeBytes": 80,
        "p90FreeBytes": 40,
        "forecastDigest": "e" * 64,
    }
    simulation = resilience_whatif.simulate_fleet(
        observed_snapshot=snapshot,
        forecast=forecast,
        price_catalog=catalog,
        candidate=candidate,
        baseline=baseline,
        running_effects=[{"actionId": "running-1"}],
        maintenance_windows=[{"start": "01:00", "end": "05:00"}],
    )
    assert simulation["s3PutCount"] == 0
    assert simulation["s3DeleteCount"] == 0
    assert simulation["authorityMutationCount"] == 0
    assert simulation["actionJournalMutationCount"] == 0
    assert simulation["sideEffectsObserved"] == 0
    assert simulation["sourceSnapshotDigest"] == "d" * 64
    assert simulation["runningEffects"] == [{"actionId": "running-1"}]
    assert simulation["maintenanceWindows"]
    plan = resilience_placement_optimizer.optimize_placement(
        baseline=baseline,
        candidates=[candidate],
        catalog=catalog,
        source_snapshot_digest="d" * 64,
        authority_head_digest="f" * 64,
        forecast_digest="e" * 64,
    )
    proof = resilience_whatif.build_optimization_proof(
        observed_snapshot=snapshot,
        forecast=forecast,
        price_catalog=catalog,
        candidate_plan=plan,
        before={"monthlyCost": 1000, "p90HeadroomBytes": 100, "committedCopies": 2, "failureDomains": 2},
        after={"monthlyCost": 850, "p90HeadroomBytes": 200, "committedCopies": 2, "failureDomains": 2},
        simulation=simulation,
    )
    assert proof["schema"] == "placement-optimization-proof-v1"
    assert proof["sideEffectsObserved"] == 0
    assert evidence_proof.validate_predictive_planning_proof(
        {
            "forecastDigest": proof["forecastDigest"],
            "priceCatalogDigest": proof["priceCatalogDigest"],
            "authorityHeadDigest": proof["authorityHeadDigest"],
            "durability": proof["durability"],
        },
        "optimizationProofRecomputesSafetyConstraints",
    ) == []
