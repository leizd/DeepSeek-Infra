"""Versioned cost model and durability-constrained optimizer (Gates I, J, M)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    resilience_cost_model,
    resilience_placement_optimizer,
)


def _catalog(tmp_settings: Path) -> dict[str, object]:
    return resilience_cost_model.put_price_catalog(
        {
            "priceCatalogVersion": 1,
            "targets": {
                "target-a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01, "requestCost": 0},
                "target-b": {"storagePerGiBMonth": 0.01, "egressPerGiB": 0.02, "requestCost": 0},
            },
        }
    )


def test_cost_model_includes_storage_egress_and_unknown_is_not_zero(tmp_settings: Path) -> None:
    catalog = _catalog(tmp_settings)
    assert catalog["priceCatalogDigest"]
    priced = resilience_cost_model.estimate_target_cost(
        "target-a",
        stored_bytes=1024**3,
        replication_bytes=1024**3,
        egress_bytes=1024**3,
        retrieval_bytes=1024**3,
        catalog=catalog,
    )
    assert priced["status"] == "OK"
    assert priced["storage"] == 0.02
    assert priced["egress"] == 0.01
    assert priced["monthlyCost"] > 0
    unknown = resilience_cost_model.estimate_target_cost("target-missing", stored_bytes=1024**3, catalog=catalog)
    assert unknown["status"] == "UNKNOWN_COST"
    assert unknown["monthlyCost"] is None
    none = resilience_cost_model.estimate_target_cost("target-a")
    loaded = resilience_cost_model.get_price_catalog()
    assert loaded is not None and loaded["priceCatalogVersion"] == 1
    assert none["status"] == "OK"
    empty = resilience_cost_model.estimate_target_cost("x")
    assert empty["status"] in {"OK", "UNKNOWN_COST"}


def test_optimizer_rejects_unsafe_cheaper_plan_and_is_deterministic(tmp_settings: Path) -> None:
    catalog = _catalog(tmp_settings)
    baseline = {
        "minCommittedCopies": 3,
        "minFailureDomains": 2,
        "committedCopies": 3,
        "failureDomains": 2,
        "forecastSafetyHeadroomBytes": 100,
    }
    unsafe = {
        "targetId": "target-b",
        "committedCopies": 2,
        "failureDomains": 1,
        "storedBytes": 1,
        "forecastFreeBytes": 1000,
    }
    safe = {
        "targetId": "target-a",
        "committedCopies": 3,
        "failureDomains": 2,
        "storedBytes": 1024**3,
        "replicationBytes": 0,
        "egressBytes": 0,
        "forecastFreeBytes": 200,
    }
    cheaper_safe = {
        "targetId": "target-b",
        "committedCopies": 3,
        "failureDomains": 2,
        "storedBytes": 1024**3,
        "forecastFreeBytes": 400,
    }
    rejected = resilience_placement_optimizer.evaluate_candidate(unsafe, baseline=baseline, catalog=catalog)
    assert rejected["accepted"] is False
    assert "MIN_COMMITTED_COPIES_REDUCED" in rejected["violations"]
    first = resilience_placement_optimizer.optimize_placement(
        baseline=baseline,
        candidates=[unsafe, safe, cheaper_safe],
        catalog=catalog,
        source_snapshot_digest="a" * 64,
        authority_head_digest="b" * 64,
        forecast_digest="c" * 64,
    )
    second = resilience_placement_optimizer.optimize_placement(
        baseline=baseline,
        candidates=[cheaper_safe, safe, unsafe],
        catalog=catalog,
        source_snapshot_digest="a" * 64,
        authority_head_digest="b" * 64,
        forecast_digest="c" * 64,
    )
    assert first["status"] == "OK"
    assert first["selected"]["candidate"]["targetId"] == "target-b"
    assert first["candidatePlanDigest"] == second["candidatePlanDigest"]
    none_safe = resilience_placement_optimizer.optimize_placement(baseline=baseline, candidates=[unsafe], catalog=catalog)
    assert none_safe["status"] == "NO_SAFE_CANDIDATE"
    realized = resilience_placement_optimizer.record_realized_optimization(
        first["candidatePlanDigest"],
        predicted_savings=15.0,
        realized_savings=12.0,
    )
    assert realized["predictionError"] == -3.0
    assert resilience_placement_optimizer.get_realized_optimization(first["candidatePlanDigest"])["predictedSavings"] == 15.0  # type: ignore[index]
    assert resilience_placement_optimizer.get_realized_optimization("missing") is None
    with pytest.raises(ValueError, match="planDigest"):
        resilience_placement_optimizer.record_realized_optimization("", predicted_savings=1, realized_savings=1)
    with pytest.raises(ValueError, match="priceCatalogVersion"):
        resilience_cost_model.put_price_catalog({"priceCatalogVersion": 0, "targets": {"a": {}}})
    with pytest.raises(ValueError, match="targets"):
        resilience_cost_model.put_price_catalog({"priceCatalogVersion": 2, "targets": {}})
