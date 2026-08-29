"""Read-only federation readiness (4.7.5 Gate N)."""

from __future__ import annotations

from deepseek_infra.infra.workspace import resilience_federation_readiness


def test_federation_snapshot_is_digest_bound_and_credential_free() -> None:
    snapshot = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-b",
        wire_compatibility=["object-set-v1", "receipt-v4", "commit-v4"],
        available_failure_domains=["az-1", "az-2"],
        forecast_headroom=123,
        cost_class="warm",
        readiness="READY",
    )
    assert snapshot["snapshotDigest"]
    assert "credential" not in snapshot
    local = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-a",
        wire_compatibility=["object-set-v1"],
        available_failure_domains=["az-1"],
        forecast_headroom=10,
        cost_class="hot",
        readiness="READY",
    )
    simulated = resilience_federation_readiness.simulate_federated_placement(local, snapshot)
    assert simulated["remoteMutations"] == 0
    assert simulated["status"] == "SIMULATED"


def test_incompatible_wire_and_credentials_fail_closed() -> None:
    bad = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-x",
        wire_compatibility=["object-set-v2"],
        available_failure_domains=[],
        forecast_headroom=0,
        cost_class="cold",
        readiness="DEGRADED",
    )
    assert bad["status"] == "INCOMPATIBLE"
    local = {"snapshotDigest": "a" * 64, "availableFailureDomains": []}
    closed = resilience_federation_readiness.simulate_federated_placement(local, bad)
    assert closed["status"] == "INCOMPATIBLE"
    assert closed["remoteMutations"] == 0
    injected = {**bad, "status": "OK", "secretToken": "nope"}
    rejected = resilience_federation_readiness.simulate_federated_placement(local, injected)
    assert rejected["status"] == "REJECTED"
