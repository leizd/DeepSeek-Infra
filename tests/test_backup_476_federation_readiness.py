"""Read-only federation freshness, identity, and integrity hardening (Gate M)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from deepseek_infra.infra.workspace import resilience_federation_readiness

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
WIRES = ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"]


def _snapshot(fleet_id: str, *, now: datetime = NOW) -> dict[str, object]:
    return resilience_federation_readiness.build_federation_snapshot(
        fleet_id=fleet_id,
        wire_compatibility=WIRES,
        available_failure_domains=[f"{fleet_id}-az-1"],
        forecast_headroom=1_000,
        cost_class="warm",
        readiness="READY",
        now=now,
    )


def test_federated_simulation_rejects_tampered_and_stale_snapshots() -> None:
    local = _snapshot("fleet-a")
    remote = _snapshot("fleet-b")
    tampered = {**remote, "forecastHeadroom": 0}

    rejected = resilience_federation_readiness.simulate_federated_placement(local, tampered, now=NOW)
    stale = resilience_federation_readiness.simulate_federated_placement(
        local,
        _snapshot("fleet-b", now=NOW - timedelta(minutes=10)),
        now=NOW,
        max_snapshot_age_seconds=300,
    )

    assert rejected["status"] == "REJECTED"
    assert rejected["reason"] == "federationSnapshotDigestInvalid"
    assert stale["status"] == "STALE"
    assert stale["reason"] == "federationSnapshotExpired"
    assert rejected["remoteMutations"] == stale["remoteMutations"] == 0


def test_federated_simulation_binds_distinct_fleet_identity_and_does_not_mutate_inputs() -> None:
    local = _snapshot("fleet-a")
    remote = _snapshot("fleet-b")
    local_before = copy.deepcopy(local)
    remote_before = copy.deepcopy(remote)

    simulated = resilience_federation_readiness.simulate_federated_placement(local, remote, now=NOW)
    same_fleet = resilience_federation_readiness.simulate_federated_placement(local, local, now=NOW)

    assert simulated["status"] == "SIMULATED"
    assert simulated["localFleetId"] == "fleet-a"
    assert simulated["remoteFleetId"] == "fleet-b"
    assert simulated["remoteMutations"] == 0
    assert local == local_before
    assert remote == remote_before
    assert same_fleet["status"] == "REJECTED"
    assert same_fleet["reason"] == "federationFleetIdentityCollision"


def test_federation_snapshot_requires_frozen_storage_wire_compatibility() -> None:
    incomplete = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-b",
        wire_compatibility=["object-set-v1"],
        available_failure_domains=[],
        forecast_headroom=None,
        cost_class="unknown",
        readiness="UNKNOWN",
        now=NOW,
    )

    assert incomplete["status"] == "INCOMPATIBLE"
    assert set(incomplete["missingRequiredWireVersions"]) == {"receipt-v4", "commit-v4", "fastcdc-v3"}
