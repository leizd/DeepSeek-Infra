from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import federation_identity, federation_peer_trust


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _identity(tmp_path: Path, name: str, fleet_id: str) -> dict[str, object]:
    return federation_identity.create_fleet_root(
        fleet_id,
        bundle_path=tmp_path / name / "root.bundle.json",
        passphrase=(f"{name}-root-passphrase-for-tests").encode("utf-8"),
        now=NOW,
    )


def _metadata(**overrides: str) -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": "cn-north-1",
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
        **overrides,
    }


def _registry(tmp_path: Path) -> tuple[federation_peer_trust.PeerTrustRegistry, dict[str, object]]:
    local_identity = _identity(tmp_path, "local", "fleet-a")
    return federation_peer_trust.PeerTrustRegistry(tmp_path / "trust" / "peers.sqlite3", local_identity), local_identity


def test_peer_trust_requires_explicit_operator_pin_and_rejects_tofu(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    peer_identity = _identity(tmp_path, "peer", "fleet-b")

    with pytest.raises(federation_peer_trust.FederationTrustError) as discovered:
        registry.verify_peer("fleet-b", peer_identity, actor="network-discovery", now=NOW)
    assert discovered.value.code == "FEDERATION_PEER_NOT_PINNED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as activated:
        registry.activate_peer("fleet-b", actor="network-discovery", now=NOW)
    assert activated.value.code == "FEDERATION_PEER_NOT_PINNED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as active_lookup:
        registry.require_active_peer("fleet-b")
    assert active_lookup.value.code == "FEDERATION_PEER_NOT_PINNED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as missing_pin:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=None,
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert missing_pin.value.code == "FEDERATION_PEER_ROOT_PIN_REQUIRED"

    pinned = registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )

    assert pinned["schema"] == "federation-peer-trust-record-v1"
    assert pinned["peerFleetId"] == "fleet-b"
    assert pinned["state"] == "PENDING"
    assert pinned["rootFingerprint"] == peer_identity["rootFingerprint"]
    assert pinned["pinnedMetadata"] == _metadata()
    assert pinned["pinnedBy"] == "operator-1"
    assert pinned["revision"] == 1
    assert registry.list_events("fleet-b") == [
        {
            "sequence": 1,
            "peerFleetId": "fleet-b",
            "eventType": "ROOT_PINNED",
            "previousState": None,
            "nextState": "PENDING",
            "actor": "operator-1",
            "reason": "operator-pinned-root",
            "occurredAt": "2026-09-01T02:00:00Z",
        }
    ]


def test_peer_trust_state_machine_is_verified_before_active_and_revocation_is_terminal(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    peer_identity = _identity(tmp_path, "peer", "fleet-b")
    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )

    with pytest.raises(federation_peer_trust.FederationTrustError) as not_verified:
        registry.activate_peer("fleet-b", actor="operator-1", now=NOW + timedelta(seconds=1))
    assert not_verified.value.code == "FEDERATION_PEER_NOT_VERIFIED"

    verified = registry.verify_peer("fleet-b", peer_identity, actor="challenge-verifier", now=NOW + timedelta(seconds=2))
    assert verified["state"] == "VERIFIED"
    assert verified["verifiedAt"] == "2026-09-01T02:00:02Z"
    active = registry.activate_peer("fleet-b", actor="operator-1", now=NOW + timedelta(seconds=3))
    assert active["state"] == "ACTIVE"
    assert registry.activate_peer("fleet-b", actor="operator-2", now=NOW + timedelta(seconds=3)) == active
    assert registry.require_active_peer("fleet-b", presented_identity=peer_identity)["state"] == "ACTIVE"

    suspended = registry.suspend_peer(
        "fleet-b",
        actor="operator-1",
        reason="incident-containment",
        now=NOW + timedelta(seconds=4),
    )
    assert suspended["state"] == "SUSPENDED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as suspended_error:
        registry.require_active_peer("fleet-b")
    assert suspended_error.value.code == "FEDERATION_PEER_NOT_ACTIVE"

    revoked = registry.revoke_peer(
        "fleet-b",
        actor="operator-2",
        reason="root-compromise",
        now=NOW + timedelta(seconds=5),
    )
    assert revoked["state"] == "REVOKED"
    assert revoked["revokedAt"] == "2026-09-01T02:00:05Z"
    assert registry.revoke_peer(
        "fleet-b",
        actor="operator-2",
        reason="root-compromise",
        now=NOW + timedelta(seconds=6),
    ) == revoked
    with pytest.raises(federation_peer_trust.FederationTrustError) as revoked_error:
        registry.require_active_peer("fleet-b")
    assert revoked_error.value.code == "FEDERATION_PEER_REVOKED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as terminal:
        registry.activate_peer("fleet-b", actor="operator-1", now=NOW + timedelta(seconds=6))
    assert terminal.value.code == "FEDERATION_PEER_REVOKED"

    states = [(event["previousState"], event["nextState"]) for event in registry.list_events("fleet-b")]
    assert states == [(None, "PENDING"), ("PENDING", "VERIFIED"), ("VERIFIED", "ACTIVE"), ("ACTIVE", "SUSPENDED"), ("SUSPENDED", "REVOKED")]


def test_peer_pin_is_idempotent_but_identity_and_root_collisions_fail_closed(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    peer_one = _identity(tmp_path, "peer-one", "fleet-b")
    first = registry.pin_peer(
        peer_one,
        expected_root_fingerprint=str(peer_one["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )
    replay = registry.pin_peer(
        peer_one,
        expected_root_fingerprint=str(peer_one["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-2",
        now=NOW + timedelta(days=1),
    )
    assert replay == first
    assert len(registry.list_events("fleet-b")) == 1

    peer_same_fleet_other_root = _identity(tmp_path, "peer-two", "fleet-b")
    with pytest.raises(federation_peer_trust.FederationTrustError) as fleet_collision:
        registry.pin_peer(
            peer_same_fleet_other_root,
            expected_root_fingerprint=str(peer_same_fleet_other_root["rootFingerprint"]),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert fleet_collision.value.code == "FEDERATION_FLEET_IDENTITY_COLLISION"

    root_rebound_to_other_fleet = {**peer_one, "fleetId": "fleet-c"}
    with pytest.raises(federation_peer_trust.FederationTrustError) as root_collision:
        registry.pin_peer(
            root_rebound_to_other_fleet,
            expected_root_fingerprint=str(peer_one["rootFingerprint"]),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert root_collision.value.code == "FEDERATION_FLEET_IDENTITY_COLLISION"


def test_peer_metadata_is_operator_pinned_immutable_and_not_remote_self_reported(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    peer_identity = _identity(tmp_path, "peer", "fleet-b")
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown:
        registry.assert_pinned_metadata("fleet-b", _metadata())
    assert unknown.value.code == "FEDERATION_PEER_NOT_PINNED"
    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )

    with pytest.raises(federation_peer_trust.FederationTrustError) as metadata_conflict:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata=_metadata(region="Mars"),
            operator_id="operator-1",
            now=NOW,
        )
    assert metadata_conflict.value.code == "FEDERATION_PEER_METADATA_CONFLICT"
    with pytest.raises(federation_peer_trust.FederationTrustError) as self_report:
        registry.assert_pinned_metadata("fleet-b", _metadata(region="Mars"))
    assert self_report.value.code == "FEDERATION_PEER_METADATA_MISMATCH"
    assert registry.assert_pinned_metadata("fleet-b", _metadata()) == _metadata()


def test_peer_trust_survives_restart_and_registry_is_bound_to_local_fleet_identity(tmp_path: Path) -> None:
    registry, local_identity = _registry(tmp_path)
    peer_identity = _identity(tmp_path, "peer", "fleet-b")
    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )
    registry.verify_peer("fleet-b", peer_identity, actor="challenge-verifier", now=NOW + timedelta(seconds=1))
    registry.activate_peer("fleet-b", actor="operator-1", now=NOW + timedelta(seconds=2))

    restarted = federation_peer_trust.PeerTrustRegistry(registry.db_path, local_identity)
    assert restarted.require_active_peer("fleet-b", presented_identity=peer_identity)["state"] == "ACTIVE"
    assert len(restarted.list_events("fleet-b")) == 3
    assert [record["peerFleetId"] for record in restarted.list_peers()] == ["fleet-b"]

    other_local_identity = _identity(tmp_path, "other-local", "fleet-z")
    with pytest.raises(federation_peer_trust.FederationTrustError) as local_collision:
        federation_peer_trust.PeerTrustRegistry(registry.db_path, other_local_identity)
    assert local_collision.value.code == "FEDERATION_LOCAL_IDENTITY_CONFLICT"


def test_peer_trust_rejects_self_pin_wrong_fingerprint_and_presented_identity_mismatch(tmp_path: Path) -> None:
    registry, local_identity = _registry(tmp_path)
    with pytest.raises(federation_peer_trust.FederationTrustError) as self_pin:
        registry.pin_peer(
            local_identity,
            expected_root_fingerprint=str(local_identity["rootFingerprint"]),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert self_pin.value.code == "FEDERATION_PEER_SELF_TRUST_FORBIDDEN"

    peer_identity = _identity(tmp_path, "peer", "fleet-b")
    with pytest.raises(federation_peer_trust.FederationTrustError) as wrong_pin:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint="sha256:" + ("0" * 64),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert wrong_pin.value.code == "FEDERATION_PEER_ROOT_PIN_MISMATCH"

    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )
    imposter = _identity(tmp_path, "imposter", "fleet-b")
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_mismatch:
        registry.verify_peer("fleet-b", imposter, actor="challenge-verifier", now=NOW)
    assert identity_mismatch.value.code == "FEDERATION_PEER_IDENTITY_MISMATCH"


def test_peer_trust_rejects_malformed_inputs_conflicts_and_invalid_transitions(tmp_path: Path) -> None:
    registry, local_identity = _registry(tmp_path)
    peer_identity = _identity(tmp_path, "peer", "fleet-b")

    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_identity:
        registry.pin_peer(
            {},
            expected_root_fingerprint="sha256:" + ("0" * 64),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert invalid_identity.value.code == "FEDERATION_PEER_IDENTITY_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_metadata:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata={"provider": "only-one-field"},
            operator_id="operator-1",
            now=NOW,
        )
    assert invalid_metadata.value.code == "FEDERATION_PEER_METADATA_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_actor:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata=_metadata(),
            operator_id=" operator-1",
            now=NOW,
        )
    assert invalid_actor.value.code == "FEDERATION_TRUST_ACTOR_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_time:
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata=_metadata(),
            operator_id="operator-1",
            now=datetime(2026, 9, 1, 2, 0),
        )
    assert invalid_time.value.code == "FEDERATION_TRUST_TIMESTAMP_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_fleet:
        registry.get_peer("FLEET-B")
    assert invalid_fleet.value.code == "FEDERATION_PEER_FLEET_ID_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_canonical:
        federation_peer_trust._canonical_json({"value": float("nan")})
    assert invalid_canonical.value.code == "FEDERATION_TRUST_CANONICAL_PAYLOAD_INVALID"

    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_transition:
        registry.suspend_peer("fleet-b", actor="operator-1", reason="not-active", now=NOW)
    assert invalid_transition.value.code == "FEDERATION_PEER_STATE_TRANSITION_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_reason:
        registry.revoke_peer("fleet-b", actor="operator-1", reason="bad\nreason", now=NOW)
    assert invalid_reason.value.code == "FEDERATION_TRUST_REASON_INVALID"

    changed_created_at = {**peer_identity, "createdAt": "2026-09-01T02:00:01Z"}
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_conflict:
        registry.pin_peer(
            changed_created_at,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata=_metadata(),
            operator_id="operator-1",
            now=NOW,
        )
    assert identity_conflict.value.code == "FEDERATION_PEER_IDENTITY_CONFLICT"

    registry.verify_peer("fleet-b", peer_identity, actor="challenge-verifier", now=NOW)
    registry.activate_peer("fleet-b", actor="operator-1", now=NOW)
    imposter = _identity(tmp_path, "active-imposter", "fleet-b")
    with pytest.raises(federation_peer_trust.FederationTrustError) as active_imposter:
        registry.require_active_peer("fleet-b", presented_identity=imposter)
    assert active_imposter.value.code == "FEDERATION_PEER_IDENTITY_MISMATCH"
    assert registry.require_active_peer("fleet-b", claimed_metadata=_metadata())["state"] == "ACTIVE"

    default_db = tmp_path / "default" / "peers.sqlite3"
    assert federation_peer_trust.open_peer_trust_registry(local_identity, db_path=default_db).db_path == default_db
