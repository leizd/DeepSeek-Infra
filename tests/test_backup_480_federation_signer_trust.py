from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import federation_identity, federation_peer_trust


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
ROOT_PASSPHRASE = b"peer-root-passphrase-for-tests"
SIGNER_PASSPHRASE = b"peer-signer-passphrase-for-tests"


def _metadata() -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": "cn-north-1",
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _active_peer(
    tmp_path: Path,
) -> tuple[federation_peer_trust.PeerTrustRegistry, dict[str, object], Path]:
    local_identity = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=tmp_path / "local" / "root.bundle.json",
        passphrase=b"local-root-passphrase-for-tests",
        now=NOW - timedelta(hours=2),
    )
    peer_root = tmp_path / "peer" / "root.bundle.json"
    peer_identity = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=peer_root,
        passphrase=ROOT_PASSPHRASE,
        now=NOW - timedelta(hours=2),
    )
    registry = federation_peer_trust.PeerTrustRegistry(tmp_path / "trust" / "peers.sqlite3", local_identity)
    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW - timedelta(hours=1),
    )
    registry.verify_peer(
        "fleet-b",
        peer_identity,
        actor="challenge-verifier",
        now=NOW - timedelta(minutes=59),
    )
    registry.activate_peer("fleet-b", actor="operator-1", now=NOW - timedelta(minutes=58))
    return registry, peer_identity, peer_root


def _issue(
    tmp_path: Path,
    peer_root: Path,
    *,
    sequence: int,
    label: str,
    not_before: datetime,
    expires_at: datetime,
    purposes: tuple[str, ...],
) -> dict[str, object]:
    return federation_identity.issue_online_signer(
        root_bundle_path=peer_root,
        root_passphrase=ROOT_PASSPHRASE,
        signer_bundle_path=tmp_path / "peer" / f"signer-{label}.bundle.json",
        signer_passphrase=SIGNER_PASSPHRASE,
        sequence=sequence,
        not_before=not_before,
        expires_at=expires_at,
        purposes=purposes,
    )


def test_online_signer_rotation_is_monotonic_idempotent_and_allows_bounded_overlap(tmp_path: Path) -> None:
    registry, _, peer_root = _active_peer(tmp_path)
    readiness = federation_identity.PURPOSE_READINESS_ATTESTATION
    first = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="one",
        not_before=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(hours=1),
        purposes=(readiness,),
    )
    third = _issue(
        tmp_path,
        peer_root,
        sequence=3,
        label="three",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=2),
        purposes=(readiness,),
    )

    first_record = registry.accept_online_signer("fleet-b", first, actor="operator-1", now=NOW)
    third_record = registry.accept_online_signer("fleet-b", third, actor="operator-1", now=NOW)
    assert registry.accept_online_signer("fleet-b", third, actor="operator-1", now=NOW) == third_record
    assert first_record["sequence"] == 1
    assert third_record["sequence"] == 3
    assert registry.authorize_online_signer(
        "fleet-b",
        first,
        purpose=readiness,
        mode=federation_peer_trust.AUTHORIZATION_CURRENT,
        validation_time=NOW + timedelta(minutes=5),
    )["signerKeyId"] == first["signerKeyId"]
    assert registry.authorize_online_signer(
        "fleet-b",
        third,
        purpose=readiness,
        mode=federation_peer_trust.AUTHORIZATION_CURRENT,
        validation_time=NOW + timedelta(minutes=5),
    )["signerKeyId"] == third["signerKeyId"]

    late_second = _issue(
        tmp_path,
        peer_root,
        sequence=2,
        label="late-two",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=2),
        purposes=(readiness,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as replay:
        registry.accept_online_signer("fleet-b", late_second, actor="operator-1", now=NOW)
    assert replay.value.code == "FEDERATION_SIGNER_SEQUENCE_REPLAY"

    conflicting_third = _issue(
        tmp_path,
        peer_root,
        sequence=3,
        label="conflicting-three",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=2),
        purposes=(readiness,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as conflict:
        registry.accept_online_signer("fleet-b", conflicting_third, actor="operator-1", now=NOW)
    assert conflict.value.code == "FEDERATION_SIGNER_SEQUENCE_CONFLICT"
    assert [record["sequence"] for record in registry.list_online_signers("fleet-b")] == [1, 3]


def test_online_signer_enforces_certificate_window_and_root_signed_purposes(tmp_path: Path) -> None:
    registry, peer_identity, peer_root = _active_peer(tmp_path)
    readiness = federation_identity.PURPOSE_READINESS_ATTESTATION
    ingress = federation_identity.PURPOSE_INGRESS_GRANT

    future = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="future",
        not_before=NOW + timedelta(minutes=10),
        expires_at=NOW + timedelta(hours=1),
        purposes=(readiness,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as future_error:
        registry.accept_online_signer("fleet-b", future, actor="operator-1", now=NOW)
    assert future_error.value.code == "FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE"

    expired = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="expired",
        not_before=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
        purposes=(readiness,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as expired_error:
        registry.accept_online_signer("fleet-b", expired, actor="operator-1", now=NOW)
    assert expired_error.value.code == "FEDERATION_SIGNER_CERTIFICATE_EXPIRED"

    restricted = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="restricted",
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        purposes=(readiness,),
    )
    assert restricted["purposes"] == [readiness]
    assert federation_identity.validate_online_signer_certificate(
        restricted,
        peer_identity,
        now=NOW,
        required_purpose=ingress,
    ) == ["FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"]
    assert federation_identity.validate_online_signer_certificate(
        restricted,
        peer_identity,
        now=NOW,
        required_purpose="CROSS_FLEET_DELETE",
    ) == ["FEDERATION_SIGNER_PURPOSE_INVALID"]
    duplicate_purpose = {**restricted, "purposes": [readiness, readiness]}
    assert "FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID" in federation_identity.validate_online_signer_certificate(
        duplicate_purpose,
        peer_identity,
        now=NOW,
    )
    registry.accept_online_signer("fleet-b", restricted, actor="operator-1", now=NOW)
    with pytest.raises(federation_peer_trust.FederationTrustError) as wrong_purpose:
        registry.authorize_online_signer(
            "fleet-b",
            restricted,
            purpose=ingress,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
        )
    assert wrong_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"

    signer = federation_identity.load_online_signer(
        tmp_path / "peer" / "signer-restricted.bundle.json",
        SIGNER_PASSPHRASE,
        root_identity=peer_identity,
        now=NOW,
    )
    with pytest.raises(federation_identity.FederationIdentityError) as local_wrong_purpose:
        federation_identity.sign_federation_document(
            signer,
            {"schema": "federation-readiness-attestation-v1", "fleetId": "fleet-b"},
            purpose=ingress,
        )
    assert local_wrong_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"

    with pytest.raises(federation_identity.FederationIdentityError) as invalid_purpose:
        _issue(
            tmp_path,
            peer_root,
            sequence=2,
            label="invalid-purpose",
            not_before=NOW,
            expires_at=NOW + timedelta(hours=1),
            purposes=("CROSS_FLEET_DELETE",),
        )
    assert invalid_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_INVALID"
    with pytest.raises(federation_identity.FederationIdentityError) as empty_purpose:
        _issue(
            tmp_path,
            peer_root,
            sequence=2,
            label="empty-purpose",
            not_before=NOW,
            expires_at=NOW + timedelta(hours=1),
            purposes=(),
        )
    assert empty_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_INVALID"


def test_signer_revocation_blocks_current_authorization_but_preserves_pre_revocation_history(tmp_path: Path) -> None:
    registry, _, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_REPLICA_ATTESTATION
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="replica",
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    revoked_at = NOW + timedelta(minutes=30)
    revoked = registry.revoke_online_signer(
        "fleet-b",
        str(certificate["signerKeyId"]),
        actor="operator-1",
        reason="online-key-incident",
        revoked_at=revoked_at,
    )
    assert revoked["revokedAt"] == "2026-09-01T03:30:00Z"
    assert registry.revoke_online_signer(
        "fleet-b",
        str(certificate["signerKeyId"]),
        actor="operator-1",
        reason="duplicate-revocation",
        revoked_at=revoked_at + timedelta(minutes=1),
    ) == revoked

    with pytest.raises(federation_peer_trust.FederationTrustError) as current:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=revoked_at + timedelta(seconds=1),
        )
    assert current.value.code == "FEDERATION_SIGNER_REVOKED"
    assert registry.authorize_online_signer(
        "fleet-b",
        certificate,
        purpose=purpose,
        mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
        validation_time=revoked_at + timedelta(days=1),
        signed_at=revoked_at - timedelta(seconds=1),
    )["historicalAuthorizationAt"] == "2026-09-01T03:29:59Z"
    with pytest.raises(federation_peer_trust.FederationTrustError) as after_revocation:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
            validation_time=revoked_at + timedelta(days=1),
            signed_at=revoked_at,
        )
    assert after_revocation.value.code == "FEDERATION_SIGNER_REVOKED_AT_SIGNING_TIME"


def test_root_revocation_blocks_new_sessions_with_explicit_historical_cutoff(tmp_path: Path) -> None:
    registry, _, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_SESSION_AUTHENTICATION
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="session",
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    revoked_at = NOW + timedelta(minutes=30)
    registry.revoke_peer("fleet-b", actor="operator-1", reason="root-key-incident", now=revoked_at)

    with pytest.raises(federation_peer_trust.FederationTrustError) as current:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=revoked_at + timedelta(seconds=1),
        )
    assert current.value.code == "FEDERATION_PEER_REVOKED"
    assert registry.authorize_online_signer(
        "fleet-b",
        certificate,
        purpose=purpose,
        mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
        validation_time=revoked_at + timedelta(days=1),
        signed_at=revoked_at - timedelta(seconds=1),
    )["historicalAuthorizationAt"] == "2026-09-01T03:29:59Z"
    with pytest.raises(federation_peer_trust.FederationTrustError) as after_root_revocation:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
            validation_time=revoked_at + timedelta(days=1),
            signed_at=revoked_at,
        )
    assert after_root_revocation.value.code == "FEDERATION_ROOT_REVOKED_AT_SIGNING_TIME"


def test_signer_trust_survives_restart_and_rejects_unpinned_or_tampered_certificates(tmp_path: Path) -> None:
    registry, peer_identity, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_DR_ATTESTATION
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="dr",
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    local_identity = federation_identity.read_fleet_identity(tmp_path / "local" / "root.bundle.json")
    restarted = federation_peer_trust.PeerTrustRegistry(registry.db_path, local_identity)
    assert restarted.get_online_signer("fleet-b", str(certificate["signerKeyId"])) is not None
    assert restarted.authorize_online_signer(
        "fleet-b",
        certificate,
        purpose=purpose,
        mode=federation_peer_trust.AUTHORIZATION_CURRENT,
        validation_time=NOW,
    )["certificate"]["rootFingerprint"] == peer_identity["rootFingerprint"]

    tampered = {**certificate, "expiresAt": "2027-09-01T03:00:00Z"}
    with pytest.raises(federation_peer_trust.FederationTrustError) as tamper:
        restarted.authorize_online_signer(
            "fleet-b",
            tampered,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
        )
    assert tamper.value.code == "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown:
        restarted.revoke_online_signer(
            "fleet-b",
            "fed-signer-" + ("0" * 24),
            actor="operator-1",
            reason="unknown-key",
            revoked_at=NOW,
        )
    assert unknown.value.code == "FEDERATION_SIGNER_NOT_ACCEPTED"


def test_signer_authorization_rejects_ambiguous_modes_inactive_roots_and_unaccepted_keys(tmp_path: Path) -> None:
    registry, _, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_SESSION_AUTHENTICATION
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="authorization-boundaries",
        not_before=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=2),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)

    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_mode:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode="AUTO",
            validation_time=NOW,
        )
    assert invalid_mode.value.code == "FEDERATION_SIGNER_AUTHORIZATION_MODE_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unexpected_history:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
            signed_at=NOW,
        )
    assert unexpected_history.value.code == "FEDERATION_SIGNER_HISTORICAL_TIME_UNEXPECTED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as missing_history:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
            validation_time=NOW,
        )
    assert missing_history.value.code == "FEDERATION_SIGNER_HISTORICAL_TIME_REQUIRED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as pre_activation:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
            validation_time=NOW,
            signed_at=NOW - timedelta(minutes=59),
        )
    assert pre_activation.value.code == "FEDERATION_ROOT_NOT_ACTIVE_AT_SIGNING_TIME"

    unaccepted = _issue(
        tmp_path,
        peer_root,
        sequence=2,
        label="unaccepted",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        purposes=(purpose,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as unaccepted_error:
        registry.authorize_online_signer(
            "fleet-b",
            unaccepted,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
        )
    assert unaccepted_error.value.code == "FEDERATION_SIGNER_NOT_ACCEPTED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_peer:
        registry.authorize_online_signer(
            "fleet-z",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
        )
    assert unknown_peer.value.code == "FEDERATION_PEER_NOT_PINNED"

    suspended_at = NOW + timedelta(minutes=10)
    registry.suspend_peer("fleet-b", actor="operator-1", reason="incident-review", now=suspended_at)
    with pytest.raises(federation_peer_trust.FederationTrustError) as inactive:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=suspended_at,
        )
    assert inactive.value.code == "FEDERATION_PEER_NOT_ACTIVE"
    with pytest.raises(federation_peer_trust.FederationTrustError) as suspended_history:
        registry.authorize_online_signer(
            "fleet-b",
            certificate,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_HISTORICAL_PROOF,
            validation_time=suspended_at + timedelta(days=1),
            signed_at=suspended_at,
        )
    assert suspended_history.value.code == "FEDERATION_ROOT_SUSPENDED_AT_SIGNING_TIME"


def test_signer_acceptance_requires_active_pinned_root_and_immutable_certificate_identity(tmp_path: Path) -> None:
    registry, peer_identity, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_READINESS_ATTESTATION
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="immutable",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)

    _, root_key = federation_identity._load_root_key(peer_root, ROOT_PASSPHRASE)
    reissued_payload = {
        **{key: value for key, value in certificate.items() if key != "rootSignature"},
        "sequence": 2,
    }
    reissued = {
        **reissued_payload,
        "rootSignature": federation_identity._b64url_encode(
            root_key.sign(federation_identity._certificate_message(reissued_payload))
        ),
    }
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_conflict:
        registry.accept_online_signer("fleet-b", reissued, actor="operator-1", now=NOW)
    assert identity_conflict.value.code == "FEDERATION_SIGNER_IDENTITY_CONFLICT"
    with pytest.raises(federation_peer_trust.FederationTrustError) as authorization_conflict:
        registry.authorize_online_signer(
            "fleet-b",
            reissued,
            purpose=purpose,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=NOW,
        )
    assert authorization_conflict.value.code == "FEDERATION_SIGNER_CERTIFICATE_CONFLICT"

    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_accept:
        registry.accept_online_signer("fleet-z", certificate, actor="operator-1", now=NOW)
    assert unknown_accept.value.code == "FEDERATION_PEER_NOT_PINNED"

    pending_root = tmp_path / "pending" / "root.bundle.json"
    pending_identity = federation_identity.create_fleet_root(
        "fleet-c",
        bundle_path=pending_root,
        passphrase=b"pending-root-passphrase-for-tests",
        now=NOW - timedelta(hours=1),
    )
    registry.pin_peer(
        pending_identity,
        expected_root_fingerprint=str(pending_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW,
    )
    pending_certificate = federation_identity.issue_online_signer(
        root_bundle_path=pending_root,
        root_passphrase=b"pending-root-passphrase-for-tests",
        signer_bundle_path=tmp_path / "pending" / "signer.bundle.json",
        signer_passphrase=SIGNER_PASSPHRASE,
        sequence=1,
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        purposes=(purpose,),
    )
    with pytest.raises(federation_peer_trust.FederationTrustError) as pending:
        registry.accept_online_signer("fleet-c", pending_certificate, actor="operator-1", now=NOW)
    assert pending.value.code == "FEDERATION_PEER_NOT_ACTIVE"

    registry.verify_peer("fleet-c", pending_identity, actor="challenge-verifier", now=NOW)
    registry.activate_peer("fleet-c", actor="operator-1", now=NOW)
    _, pending_root_key = federation_identity._load_root_key(
        pending_root,
        b"pending-root-passphrase-for-tests",
    )
    cross_fleet_payload = {
        "schema": federation_identity.ONLINE_SIGNER_CERTIFICATE_SCHEMA,
        "fleetId": "fleet-c",
        "rootKeyId": pending_identity["rootKeyId"],
        "rootFingerprint": pending_identity["rootFingerprint"],
        "signerKeyId": certificate["signerKeyId"],
        "signerPublicKey": certificate["signerPublicKey"],
        "signatureAlgorithm": federation_identity.SIGNATURE_ALGORITHM,
        "purposes": [purpose],
        "sequence": 1,
        "issuedAt": "2026-09-01T03:00:00Z",
        "notBefore": "2026-09-01T03:00:00Z",
        "expiresAt": "2026-09-01T04:00:00Z",
    }
    cross_fleet_certificate = {
        **cross_fleet_payload,
        "rootSignature": federation_identity._b64url_encode(
            pending_root_key.sign(federation_identity._certificate_message(cross_fleet_payload))
        ),
    }
    with pytest.raises(federation_peer_trust.FederationTrustError) as signer_collision:
        registry.accept_online_signer("fleet-c", cross_fleet_certificate, actor="operator-1", now=NOW)
    assert signer_collision.value.code == "FEDERATION_SIGNER_FLEET_COLLISION"

    registry.revoke_peer("fleet-b", actor="operator-1", reason="root-incident", now=NOW)
    with pytest.raises(federation_peer_trust.FederationTrustError) as revoked:
        registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"
    assert peer_identity["rootFingerprint"] == certificate["rootFingerprint"]


def test_signer_registry_audit_and_input_boundaries_fail_closed(tmp_path: Path) -> None:
    registry, _, peer_root = _active_peer(tmp_path)
    purpose = federation_identity.PURPOSE_EVIDENCE
    certificate = _issue(
        tmp_path,
        peer_root,
        sequence=1,
        label="audit",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        purposes=(purpose,),
    )
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    registry.revoke_online_signer(
        "fleet-b",
        str(certificate["signerKeyId"]),
        actor="operator-1",
        reason="rotation-complete",
        revoked_at=NOW + timedelta(minutes=10),
    )
    assert [event["eventType"] for event in registry.list_online_signer_events("fleet-b")] == [
        "SIGNER_CERTIFICATE_ACCEPTED",
        "SIGNER_REVOKED",
    ]

    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_key:
        registry.get_online_signer("fleet-b", "not-a-key-id")
    assert invalid_key.value.code == "FEDERATION_SIGNER_KEY_ID_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_revoke:
        registry.revoke_online_signer(
            "fleet-z",
            "fed-signer-" + ("0" * 24),
            actor="operator-1",
            reason="unknown-peer",
            revoked_at=NOW,
        )
    assert unknown_revoke.value.code == "FEDERATION_PEER_NOT_PINNED"

    for invalid_timestamp in (None, "not-a-time", "2026-09-01T03:00:00"):
        with pytest.raises(federation_peer_trust.FederationTrustError) as timestamp_error:
            federation_peer_trust._stored_timestamp(invalid_timestamp, code="BAD_STORED_TIME")
        assert timestamp_error.value.code == "BAD_STORED_TIME"
