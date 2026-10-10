from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import federation_challenge, federation_identity, federation_peer_trust


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)


def _metadata(*, region: str) -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": region,
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _two_fleets(
    tmp_path: Path,
) -> tuple[
    federation_peer_trust.PeerTrustRegistry,
    federation_peer_trust.PeerTrustRegistry,
    federation_identity.OnlineFleetSigner,
    federation_identity.OnlineFleetSigner,
    dict[str, object],
    dict[str, object],
]:
    root_a = tmp_path / "fleet-a" / "root.bundle.json"
    root_b = tmp_path / "fleet-b" / "root.bundle.json"
    passphrase_a = b"fleet-a-root-passphrase-session"
    passphrase_b = b"fleet-b-root-passphrase-session"
    identity_a = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_a,
        passphrase=passphrase_a,
        now=NOW - timedelta(hours=2),
    )
    identity_b = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=root_b,
        passphrase=passphrase_b,
        now=NOW - timedelta(hours=2),
    )
    bundle_a = tmp_path / "fleet-a" / "session-signer.bundle.json"
    bundle_b = tmp_path / "fleet-b" / "session-signer.bundle.json"
    certificate_a = federation_identity.issue_online_signer(
        root_bundle_path=root_a,
        root_passphrase=passphrase_a,
        signer_bundle_path=bundle_a,
        signer_passphrase=b"fleet-a-signer-passphrase-session",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(federation_identity.PURPOSE_SESSION_AUTHENTICATION,),
    )
    certificate_b = federation_identity.issue_online_signer(
        root_bundle_path=root_b,
        root_passphrase=passphrase_b,
        signer_bundle_path=bundle_b,
        signer_passphrase=b"fleet-b-signer-passphrase-session",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(federation_identity.PURPOSE_SESSION_AUTHENTICATION,),
    )
    signer_a = federation_identity.load_online_signer(
        bundle_a,
        b"fleet-a-signer-passphrase-session",
        root_identity=identity_a,
        now=NOW,
    )
    signer_b = federation_identity.load_online_signer(
        bundle_b,
        b"fleet-b-signer-passphrase-session",
        root_identity=identity_b,
        now=NOW,
    )
    registry_a = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-a" / "trust.sqlite3", identity_a)
    registry_b = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-b" / "trust.sqlite3", identity_b)
    registry_a.pin_peer(
        identity_b,
        expected_root_fingerprint=str(identity_b["rootFingerprint"]),
        metadata=_metadata(region="cn-south-1"),
        operator_id="operator-a",
        now=NOW - timedelta(minutes=30),
    )
    registry_a.verify_peer("fleet-b", identity_b, actor="verifier-a", now=NOW - timedelta(minutes=29))
    registry_a.activate_peer("fleet-b", actor="operator-a", now=NOW - timedelta(minutes=28))
    registry_a.accept_online_signer("fleet-b", certificate_b, actor="operator-a", now=NOW)
    registry_b.pin_peer(
        identity_a,
        expected_root_fingerprint=str(identity_a["rootFingerprint"]),
        metadata=_metadata(region="cn-north-1"),
        operator_id="operator-b",
        now=NOW - timedelta(minutes=30),
    )
    registry_b.verify_peer("fleet-a", identity_a, actor="verifier-b", now=NOW - timedelta(minutes=29))
    registry_b.activate_peer("fleet-a", actor="operator-b", now=NOW - timedelta(minutes=28))
    registry_b.accept_online_signer("fleet-a", certificate_a, actor="operator-b", now=NOW)
    return registry_a, registry_b, signer_a, signer_b, identity_a, identity_b


def _exchange(
    registry_a: federation_peer_trust.PeerTrustRegistry,
    registry_b: federation_peer_trust.PeerTrustRegistry,
    signer_a: federation_identity.OnlineFleetSigner,
    signer_b: federation_identity.OnlineFleetSigner,
) -> tuple[dict[str, object], dict[str, object]]:
    challenge = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
    )
    response = federation_challenge.respond_to_federation_challenge(
        challenge,
        peer_registry=registry_b,
        responder_signer=signer_b,
        now=NOW + timedelta(seconds=1),
    )
    return challenge, response


def test_bilateral_challenge_response_binds_nonce_fleets_signers_time_and_purpose(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    challenge, response = _exchange(registry_a, registry_b, signer_a, signer_b)

    assert challenge["schema"] == "federation-challenge-v1"
    assert challenge["fleetId"] == challenge["sourceFleetId"] == "fleet-a"
    assert challenge["destinationFleetId"] == "fleet-b"
    assert challenge["sessionPurpose"] == federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY
    assert response["schema"] == "federation-challenge-response-v1"
    assert response["fleetId"] == response["destinationFleetId"] == "fleet-b"
    assert response["sourceFleetId"] == "fleet-a"
    assert response["nonce"] == challenge["nonce"]
    assert response["challengeDigest"] == federation_challenge.challenge_digest(challenge)
    assert response["sessionPurpose"] == challenge["sessionPurpose"]

    session = federation_challenge.verify_federation_challenge_response(
        challenge,
        response,
        peer_registry=registry_a,
        now=NOW + timedelta(seconds=2),
    )
    assert session["schema"] == "federation-session-v1"
    assert session["status"] == "AUTHENTICATED"
    assert session["sourceFleetId"] == "fleet-a"
    assert session["destinationFleetId"] == "fleet-b"
    assert session["nonceDigest"] == federation_challenge.nonce_digest(str(challenge["nonce"]))


def test_challenge_nonce_is_random_durable_single_use_and_response_is_reconcilable(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, identity_a, identity_b = _two_fleets(tmp_path)
    first, response = _exchange(registry_a, registry_b, signer_a, signer_b)
    second = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_FEDERATED_DR,
        now=NOW,
    )
    assert first["nonce"] != second["nonce"]
    raw_nonce = base64.urlsafe_b64decode(str(first["nonce"]) + "=")
    assert len(raw_nonce) == 32

    with pytest.raises(federation_challenge.FederationChallengeError) as inbound_replay:
        federation_challenge.respond_to_federation_challenge(
            first,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW + timedelta(seconds=2),
        )
    assert inbound_replay.value.code == "FEDERATION_CHALLENGE_NONCE_REPLAY"
    inbound = registry_b.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_INBOUND,
        federation_challenge.nonce_digest(str(first["nonce"])),
    )
    assert inbound is not None
    assert inbound["state"] == "RESPONDED"
    assert inbound["response"] == response

    federation_challenge.verify_federation_challenge_response(
        first,
        response,
        peer_registry=registry_a,
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as outbound_replay:
        federation_challenge.verify_federation_challenge_response(
            first,
            response,
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=3),
        )
    assert outbound_replay.value.code == "FEDERATION_CHALLENGE_NONCE_REPLAY"

    restarted_a = federation_peer_trust.PeerTrustRegistry(registry_a.db_path, identity_a)
    restarted_b = federation_peer_trust.PeerTrustRegistry(registry_b.db_path, identity_b)
    assert restarted_a.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_OUTBOUND,
        federation_challenge.nonce_digest(str(first["nonce"])),
    )["state"] == "CONSUMED"  # type: ignore[index]
    assert restarted_b.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_INBOUND,
        federation_challenge.nonce_digest(str(first["nonce"])),
    )["response"] == response  # type: ignore[index]


def test_challenge_response_rejects_reflection_tamper_and_wrong_fleet_bindings(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    challenge, response = _exchange(registry_a, registry_b, signer_a, signer_b)

    with pytest.raises(federation_challenge.FederationChallengeError) as reflection:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            challenge,
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert reflection.value.code == "FEDERATION_CHALLENGE_RESPONSE_SCHEMA_INVALID"

    tampered = {**response, "nonce": federation_challenge.generate_nonce()}
    with pytest.raises(federation_challenge.FederationChallengeError) as tamper:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            tampered,
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert tamper.value.code == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"

    wrong_destination_payload = {
        key: value
        for key, value in challenge.items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    wrong_destination_payload["destinationFleetId"] = "fleet-c"
    wrong_destination = federation_identity.sign_federation_document(
        signer_a,
        wrong_destination_payload,
        purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as wrong_fleet:
        federation_challenge.respond_to_federation_challenge(
            wrong_destination,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW + timedelta(seconds=1),
        )
    assert wrong_fleet.value.code == "FEDERATION_CHALLENGE_DESTINATION_FLEET_MISMATCH"


def test_challenge_rejects_expiry_future_timestamp_expired_signer_and_revoked_peer(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    expired = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as expired_error:
        federation_challenge.respond_to_federation_challenge(
            expired,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW + timedelta(seconds=30),
        )
    assert expired_error.value.code == "FEDERATION_CHALLENGE_EXPIRED"

    future = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_FEDERATED_DR,
        now=NOW + timedelta(seconds=31),
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as future_error:
        federation_challenge.respond_to_federation_challenge(
            future,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW,
            max_future_skew_seconds=30,
        )
    assert future_error.value.code == "FEDERATION_CHALLENGE_FROM_FUTURE"

    late_issued_at = NOW + timedelta(hours=2, seconds=1)
    malicious_payload = {
        "schema": federation_challenge.CHALLENGE_SCHEMA,
        "fleetId": "fleet-a",
        "sourceFleetId": "fleet-a",
        "destinationFleetId": "fleet-b",
        "nonce": federation_challenge.generate_nonce(),
        "sessionPurpose": federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        "signerCertificate": signer_a.certificate,
        "issuedAt": "2026-09-01T07:00:01Z",
        "expiresAt": "2026-09-01T07:01:01Z",
    }
    expired_signer_challenge = federation_identity.sign_federation_document(
        signer_a,
        malicious_payload,
        purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as signer_expired:
        federation_challenge.respond_to_federation_challenge(
            expired_signer_challenge,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=late_issued_at,
        )
    assert signer_expired.value.code == "FEDERATION_SIGNER_CERTIFICATE_EXPIRED"

    valid = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
    )
    registry_b.revoke_peer("fleet-a", actor="operator-b", reason="root-incident", now=NOW)
    with pytest.raises(federation_challenge.FederationChallengeError) as revoked:
        federation_challenge.respond_to_federation_challenge(
            valid,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW + timedelta(seconds=1),
        )
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"


def test_challenge_canonical_nonce_purpose_and_timestamp_boundaries_fail_closed() -> None:
    for canonical_value in (("tuple",), {"value": float("nan")}, {1: "ambiguous"}):
        with pytest.raises(federation_challenge.FederationChallengeError) as canonical:
            federation_challenge._normalize(canonical_value)
        assert canonical.value.code == "FEDERATION_CHALLENGE_CANONICAL_PAYLOAD_INVALID"
        with pytest.raises(federation_challenge.FederationChallengeError) as digest:
            federation_challenge._digest(canonical_value)
        assert digest.value.code == "FEDERATION_CHALLENGE_CANONICAL_PAYLOAD_INVALID"

    for nonce_value in (None, "", "abc=", "*", "YWJj"):
        with pytest.raises(federation_challenge.FederationChallengeError) as nonce:
            federation_challenge._nonce(nonce_value)
        assert nonce.value.code == "FEDERATION_CHALLENGE_NONCE_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as fleet:
        federation_challenge._fleet_id(" fleet-a")
    assert fleet.value.code == "FEDERATION_CHALLENGE_FLEET_ID_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as purpose:
        federation_challenge._purpose("DELETE_REMOTE_REPLICA")
    assert purpose.value.code == "FEDERATION_CHALLENGE_PURPOSE_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as naive:
        federation_challenge._utc_iso(datetime(2026, 9, 1, 5, 0))
    assert naive.value.code == "FEDERATION_CHALLENGE_TIMESTAMP_INVALID"
    for timestamp_value in (None, "not-a-time", "2026-09-01T05:00:00", "2026-09-01T05:00:00+00:00"):
        with pytest.raises(federation_challenge.FederationChallengeError) as timestamp:
            federation_challenge._parse_timestamp(timestamp_value)
        assert timestamp.value.code == "FEDERATION_CHALLENGE_TIMESTAMP_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as window:
        federation_challenge._validate_window(NOW, NOW)
    assert window.value.code == "FEDERATION_CHALLENGE_LIFETIME_INVALID"


def test_challenge_issuance_rejects_local_signer_peer_and_window_misconfiguration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_a, _, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    with pytest.raises(federation_challenge.FederationChallengeError) as local_signer:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_b,
            destination_fleet_id="fleet-b",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW,
        )
    assert local_signer.value.code == "FEDERATION_CHALLENGE_LOCAL_SIGNER_MISMATCH"
    with pytest.raises(federation_challenge.FederationChallengeError) as reflection:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-a",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW,
        )
    assert reflection.value.code == "FEDERATION_CHALLENGE_REFLECTION_REJECTED"
    with pytest.raises(federation_challenge.FederationChallengeError) as unknown_peer:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-z",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW,
        )
    assert unknown_peer.value.code == "FEDERATION_PEER_NOT_PINNED"
    with pytest.raises(federation_challenge.FederationChallengeError) as lifetime:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-b",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW,
            expires_at=NOW + timedelta(seconds=121),
        )
    assert lifetime.value.code == "FEDERATION_CHALLENGE_LIFETIME_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as signer_window:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-b",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW + timedelta(hours=1, minutes=59),
        )
    assert signer_window.value.code == "FEDERATION_CHALLENGE_SIGNER_WINDOW_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as expired_signer:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-b",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW + timedelta(hours=2, seconds=1),
            expires_at=NOW + timedelta(hours=2, seconds=30),
        )
    assert expired_signer.value.code == "FEDERATION_SIGNER_CERTIFICATE_EXPIRED"

    def fail_record(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise federation_peer_trust.FederationTrustError("FORCED_JOURNAL_FAILURE")

    monkeypatch.setattr(registry_a, "record_outbound_federation_challenge", fail_record)
    with pytest.raises(federation_challenge.FederationChallengeError) as journal:
        federation_challenge.issue_federation_challenge(
            peer_registry=registry_a,
            challenger_signer=signer_a,
            destination_fleet_id="fleet-b",
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            now=NOW,
        )
    assert journal.value.code == "FORCED_JOURNAL_FAILURE"


def test_challenge_untrusted_document_semantics_and_response_journal_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_a, registry_b, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    challenge = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
    )
    for mutation, expected in (
        ({key: value for key, value in challenge.items() if key != "nonce"}, "FEDERATION_CHALLENGE_FIELDS_INVALID"),
        ({**challenge, "schema": "wrong-schema"}, "FEDERATION_CHALLENGE_SCHEMA_INVALID"),
        ({**challenge, "fleetId": "fleet-c"}, "FEDERATION_CHALLENGE_SOURCE_FLEET_MISMATCH"),
    ):
        with pytest.raises(federation_challenge.FederationChallengeError) as semantic:
            federation_challenge._challenge_semantics(
                mutation,
                expected_source_fleet_id="fleet-a",
                expected_destination_fleet_id="fleet-b",
                now=NOW,
                max_future_skew_seconds=30,
            )
        assert semantic.value.code == expected

    before_certificate = {
        **challenge,
        "issuedAt": "2026-09-01T03:59:00Z",
        "expiresAt": "2026-09-01T04:01:00Z",
    }
    with pytest.raises(federation_challenge.FederationChallengeError) as signer_window:
        federation_challenge._challenge_semantics(
            before_certificate,
            expected_source_fleet_id="fleet-a",
            expected_destination_fleet_id="fleet-b",
            now=NOW - timedelta(hours=1, minutes=1),
            max_future_skew_seconds=30,
        )
    assert signer_window.value.code == "FEDERATION_CHALLENGE_SIGNER_WINDOW_INVALID"

    with pytest.raises(federation_challenge.FederationChallengeError) as invalid_document:
        federation_challenge.respond_to_federation_challenge(
            [],  # type: ignore[arg-type]
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW,
        )
    assert invalid_document.value.code == "FEDERATION_CHALLENGE_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as wrong_schema:
        federation_challenge.respond_to_federation_challenge(
            {"schema": "wrong-schema"},
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW,
        )
    assert wrong_schema.value.code == "FEDERATION_CHALLENGE_SCHEMA_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as missing_certificate:
        federation_challenge.respond_to_federation_challenge(
            {**challenge, "signerCertificate": None},
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW,
        )
    assert missing_certificate.value.code == "FEDERATION_CHALLENGE_SIGNER_CERTIFICATE_INVALID"

    with monkeypatch.context() as context:
        context.setattr(registry_b, "get_peer", lambda _fleet_id: None)
        with pytest.raises(federation_challenge.FederationChallengeError) as unknown_peer:
            federation_challenge.respond_to_federation_challenge(
                challenge,
                peer_registry=registry_b,
                responder_signer=signer_b,
                now=NOW,
            )
        assert unknown_peer.value.code == "FEDERATION_PEER_NOT_PINNED"
    with monkeypatch.context() as context:
        context.setattr(registry_b, "get_peer", lambda _fleet_id: {"fleetIdentity": None})
        with pytest.raises(federation_challenge.FederationChallengeError) as malformed_peer:
            federation_challenge.respond_to_federation_challenge(
                challenge,
                peer_registry=registry_b,
                responder_signer=signer_b,
                now=NOW,
            )
        assert malformed_peer.value.code == "FEDERATION_PEER_IDENTITY_INVALID"

    def fail_response_record(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise federation_peer_trust.FederationTrustError("FORCED_RESPONSE_JOURNAL_FAILURE")

    monkeypatch.setattr(registry_b, "record_inbound_federation_response", fail_response_record)
    with pytest.raises(federation_challenge.FederationChallengeError) as journal:
        federation_challenge.respond_to_federation_challenge(
            challenge,
            peer_registry=registry_b,
            responder_signer=signer_b,
            now=NOW + timedelta(seconds=1),
        )
    assert journal.value.code == "FORCED_RESPONSE_JOURNAL_FAILURE"


def test_challenge_response_semantic_validator_rejects_every_binding_and_time_substitution(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, _, _ = _two_fleets(tmp_path)
    challenge, response = _exchange(registry_a, registry_b, signer_a, signer_b)

    def resign(overrides: Mapping[str, object]) -> dict[str, object]:
        payload = {
            key: value
            for key, value in response.items()
            if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
        }
        payload.update(overrides)
        return federation_identity.sign_federation_document(
            signer_b,
            payload,
            purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
        )

    with pytest.raises(federation_challenge.FederationChallengeError) as response_type:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            [],  # type: ignore[arg-type]
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert response_type.value.code == "FEDERATION_CHALLENGE_RESPONSE_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as fields:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            {key: value for key, value in response.items() if key != "nonce"},
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert fields.value.code == "FEDERATION_CHALLENGE_RESPONSE_FIELDS_INVALID"
    with pytest.raises(federation_challenge.FederationChallengeError) as local_source:
        federation_challenge.verify_federation_challenge_response(
            {**challenge, "sourceFleetId": "fleet-c"},
            response,
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert local_source.value.code == "FEDERATION_CHALLENGE_SOURCE_FLEET_MISMATCH"
    with pytest.raises(federation_challenge.FederationChallengeError) as certificate:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            {**response, "signerCertificate": None},
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=2),
        )
    assert certificate.value.code == "FEDERATION_CHALLENGE_RESPONSE_SIGNER_CERTIFICATE_INVALID"

    signed_mutations = (
        ({"destinationFleetId": "fleet-c"}, "FEDERATION_CHALLENGE_RESPONSE_DESTINATION_FLEET_MISMATCH"),
        ({"sourceFleetId": "fleet-c"}, "FEDERATION_CHALLENGE_RESPONSE_SOURCE_FLEET_MISMATCH"),
        ({"nonce": federation_challenge.generate_nonce()}, "FEDERATION_CHALLENGE_RESPONSE_NONCE_MISMATCH"),
        (
            {"sessionPurpose": federation_challenge.SESSION_PURPOSE_FEDERATED_DR},
            "FEDERATION_CHALLENGE_RESPONSE_PURPOSE_MISMATCH",
        ),
        ({"challengeDigest": "sha256:" + ("0" * 64)}, "FEDERATION_CHALLENGE_RESPONSE_BINDING_INVALID"),
        ({"respondedAt": "2026-09-01T04:59:59Z"}, "FEDERATION_CHALLENGE_RESPONSE_WINDOW_INVALID"),
        ({"respondedAt": "2026-09-01T05:00:31Z"}, "FEDERATION_CHALLENGE_RESPONSE_FROM_FUTURE"),
    )
    for overrides, expected in signed_mutations:
        with pytest.raises(federation_challenge.FederationChallengeError) as rejected:
            federation_challenge.verify_federation_challenge_response(
                challenge,
                resign(overrides),
                peer_registry=registry_a,
                now=NOW,
                max_future_skew_seconds=30,
            )
        assert rejected.value.code == expected

    with pytest.raises(federation_challenge.FederationChallengeError) as expired:
        federation_challenge.verify_federation_challenge_response(
            challenge,
            response,
            peer_registry=registry_a,
            now=NOW + timedelta(seconds=120),
        )
    assert expired.value.code == "FEDERATION_CHALLENGE_RESPONSE_EXPIRED"

    late_issued_at = NOW + timedelta(hours=1, minutes=59)
    late_expires_at = late_issued_at + timedelta(minutes=2)
    late_nonce = federation_challenge.generate_nonce()
    late_challenge_payload = {
        "schema": federation_challenge.CHALLENGE_SCHEMA,
        "fleetId": "fleet-a",
        "sourceFleetId": "fleet-a",
        "destinationFleetId": "fleet-b",
        "nonce": late_nonce,
        "sessionPurpose": federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        "signerCertificate": signer_a.certificate,
        "issuedAt": "2026-09-01T06:59:00Z",
        "expiresAt": "2026-09-01T07:01:00Z",
    }
    late_challenge = federation_identity.sign_federation_document(
        signer_a,
        late_challenge_payload,
        purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    )
    registry_a.record_outbound_federation_challenge(
        "fleet-b",
        nonce_digest=federation_challenge.nonce_digest(late_nonce),
        challenge_digest=federation_challenge.challenge_digest(late_challenge),
        challenge=late_challenge,
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        issued_at=late_issued_at,
        expires_at=late_expires_at,
    )
    late_response_payload = {
        "schema": federation_challenge.CHALLENGE_RESPONSE_SCHEMA,
        "fleetId": "fleet-b",
        "sourceFleetId": "fleet-a",
        "destinationFleetId": "fleet-b",
        "nonce": late_nonce,
        "challengeDigest": federation_challenge.challenge_digest(late_challenge),
        "sessionPurpose": federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        "signerCertificate": signer_b.certificate,
        "respondedAt": "2026-09-01T06:59:00Z",
        "expiresAt": "2026-09-01T07:01:00Z",
    }
    late_response = federation_identity.sign_federation_document(
        signer_b,
        late_response_payload,
        purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    )
    with pytest.raises(federation_challenge.FederationChallengeError) as signer_window:
        federation_challenge.verify_federation_challenge_response(
            late_challenge,
            late_response,
            peer_registry=registry_a,
            now=late_issued_at,
        )
    assert signer_window.value.code == "FEDERATION_CHALLENGE_RESPONSE_SIGNER_WINDOW_INVALID"


def test_peer_trust_challenge_journal_rejects_direct_bypass_and_conflicting_state(tmp_path: Path) -> None:
    registry_a, registry_b, signer_a, signer_b, identity_a, identity_b = _two_fleets(tmp_path)
    challenge, response = _exchange(registry_a, registry_b, signer_a, signer_b)
    nonce = str(challenge["nonce"])
    nonce_hash = federation_challenge.nonce_digest(nonce)
    challenge_hash = federation_challenge.challenge_digest(challenge)
    response_hash = federation_challenge.response_digest(response)
    issued_at = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
    expires_at = datetime(2026, 9, 1, 5, 2, tzinfo=UTC)

    with pytest.raises(federation_peer_trust.FederationTrustError) as direction:
        registry_a.get_federation_challenge("SIDEWAYS", nonce_hash)
    assert direction.value.code == "FEDERATION_CHALLENGE_DIRECTION_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as duplicate_outbound:
        registry_a.record_outbound_federation_challenge(
            "fleet-b",
            nonce_digest=nonce_hash,
            challenge_digest=challenge_hash,
            challenge=challenge,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    assert duplicate_outbound.value.code == "FEDERATION_CHALLENGE_NONCE_REPLAY"
    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_outbound:
        registry_a.record_outbound_federation_challenge(
            "fleet-b",
            nonce_digest="sha256:" + ("1" * 64),
            challenge_digest=challenge_hash,
            challenge=None,  # type: ignore[arg-type]
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    assert invalid_outbound.value.code == "FEDERATION_CHALLENGE_DOCUMENT_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as outbound_binding:
        registry_a.record_outbound_federation_challenge(
            "fleet-b",
            nonce_digest="sha256:" + ("2" * 64),
            challenge_digest=challenge_hash,
            challenge={**challenge, "destinationFleetId": "fleet-c"},
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    assert outbound_binding.value.code == "FEDERATION_CHALLENGE_FLEET_BINDING_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as outbound_window:
        registry_a.record_outbound_federation_challenge(
            "fleet-b",
            nonce_digest="sha256:" + ("3" * 64),
            challenge_digest=challenge_hash,
            challenge=challenge,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=issued_at,
        )
    assert outbound_window.value.code == "FEDERATION_CHALLENGE_WINDOW_INVALID"
    unknown_nonce = federation_challenge.generate_nonce()
    unknown_challenge = {**challenge, "destinationFleetId": "fleet-z", "nonce": unknown_nonce}
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_outbound:
        registry_a.record_outbound_federation_challenge(
            "fleet-z",
            nonce_digest=federation_challenge.nonce_digest(unknown_nonce),
            challenge_digest=federation_challenge.challenge_digest(unknown_challenge),
            challenge=unknown_challenge,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    assert unknown_outbound.value.code == "FEDERATION_PEER_NOT_PINNED"

    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_inbound:
        registry_b.record_inbound_federation_response(
            "fleet-a",
            peer_signer_key_id=str(challenge["signerKeyId"]),
            nonce_digest="sha256:" + ("5" * 64),
            challenge_digest=challenge_hash,
            challenge=None,  # type: ignore[arg-type]
            response_digest=response_hash,
            response=response,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
            responded_at=NOW + timedelta(seconds=1),
        )
    assert invalid_inbound.value.code == "FEDERATION_CHALLENGE_DOCUMENT_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as inbound_binding:
        registry_b.record_inbound_federation_response(
            "fleet-a",
            peer_signer_key_id=str(challenge["signerKeyId"]),
            nonce_digest="sha256:" + ("6" * 64),
            challenge_digest=challenge_hash,
            challenge={**challenge, "sourceFleetId": "fleet-c"},
            response_digest=response_hash,
            response=response,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
            responded_at=NOW + timedelta(seconds=1),
        )
    assert inbound_binding.value.code == "FEDERATION_CHALLENGE_FLEET_BINDING_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as duplicate_inbound:
        registry_b.record_inbound_federation_response(
            "fleet-a",
            peer_signer_key_id=str(challenge["signerKeyId"]),
            nonce_digest=nonce_hash,
            challenge_digest=challenge_hash,
            challenge=challenge,
            response_digest=response_hash,
            response=response,
            session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
            issued_at=issued_at,
            expires_at=expires_at,
            responded_at=NOW + timedelta(seconds=1),
        )
    assert duplicate_inbound.value.code == "FEDERATION_CHALLENGE_NONCE_REPLAY"

    with pytest.raises(federation_peer_trust.FederationTrustError) as invalid_response:
        registry_a.consume_outbound_federation_response(
            "fleet-b",
            peer_signer_key_id=str(response["signerKeyId"]),
            nonce_digest=nonce_hash,
            challenge_digest=challenge_hash,
            response_digest=response_hash,
            response=None,  # type: ignore[arg-type]
            consumed_at=NOW + timedelta(seconds=2),
        )
    assert invalid_response.value.code == "FEDERATION_CHALLENGE_DOCUMENT_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unknown_nonce_error:
        registry_a.consume_outbound_federation_response(
            "fleet-b",
            peer_signer_key_id=str(response["signerKeyId"]),
            nonce_digest="sha256:" + ("7" * 64),
            challenge_digest=challenge_hash,
            response_digest=response_hash,
            response=response,
            consumed_at=NOW + timedelta(seconds=2),
        )
    assert unknown_nonce_error.value.code == "FEDERATION_CHALLENGE_NONCE_UNKNOWN"
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_conflict:
        registry_a.consume_outbound_federation_response(
            "fleet-b",
            peer_signer_key_id=str(response["signerKeyId"]),
            nonce_digest=nonce_hash,
            challenge_digest="sha256:" + ("8" * 64),
            response_digest=response_hash,
            response=response,
            consumed_at=NOW + timedelta(seconds=2),
        )
    assert identity_conflict.value.code == "FEDERATION_CHALLENGE_IDENTITY_CONFLICT"

    with pytest.raises(federation_peer_trust.FederationTrustError) as wrong_peer_identity:
        registry_a.verify_peer("fleet-z", identity_b, actor="verifier-a", now=NOW)
    assert wrong_peer_identity.value.code == "FEDERATION_PEER_IDENTITY_MISMATCH"
    with pytest.raises(federation_peer_trust.FederationTrustError) as unaccepted_signer:
        registry_a.record_readiness_sequence(
            "fleet-b",
            signer_key_id="fed-signer-" + ("0" * 24),
            sequence=1,
            attestation_digest="sha256:" + ("9" * 64),
            accepted_at=NOW,
        )
    assert unaccepted_signer.value.code == "FEDERATION_SIGNER_NOT_ACCEPTED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as certificate_purpose:
        registry_a.record_readiness_sequence(
            "fleet-b",
            signer_key_id=str(response["signerKeyId"]),
            sequence=1,
            attestation_digest="sha256:" + ("a" * 64),
            accepted_at=NOW,
        )
    assert certificate_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"

    registry_a.suspend_peer("fleet-b", actor="operator-a", reason="session-review", now=NOW)
    with pytest.raises(federation_peer_trust.FederationTrustError) as inactive_peer:
        registry_a.record_readiness_sequence(
            "fleet-b",
            signer_key_id=str(response["signerKeyId"]),
            sequence=1,
            attestation_digest="sha256:" + ("b" * 64),
            accepted_at=NOW,
        )
    assert inactive_peer.value.code == "FEDERATION_PEER_NOT_ACTIVE"
    registry_b.revoke_peer("fleet-a", actor="operator-b", reason="root-incident", now=NOW)
    with pytest.raises(federation_peer_trust.FederationTrustError) as revoked_peer:
        registry_b.record_readiness_sequence(
            "fleet-a",
            signer_key_id=str(challenge["signerKeyId"]),
            sequence=1,
            attestation_digest="sha256:" + ("c" * 64),
            accepted_at=NOW,
        )
    assert revoked_peer.value.code == "FEDERATION_PEER_REVOKED"
    assert registry_a.local_identity == identity_a
