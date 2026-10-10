from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from deepseek_infra.infra.workspace import (
    evidence_proof,
    federation_challenge,
    federation_identity,
    federation_peer_trust,
    federation_readiness_attestation,
    federation_trust_proof,
    resilience_federation_readiness,
)


NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _metadata(region: str) -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": region,
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _snapshot(fleet_id: str, generated_at: datetime) -> dict[str, Any]:
    return resilience_federation_readiness.build_federation_snapshot(
        fleet_id=fleet_id,
        wire_compatibility=["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
        available_failure_domains=[f"{fleet_id}-site-1", f"{fleet_id}-site-2"],
        forecast_headroom=4_096,
        cost_class="warm",
        readiness="READY",
        now=generated_at,
    )


def _issue_signer(
    root_path: Path,
    root_passphrase: bytes,
    signer_path: Path,
    signer_passphrase: bytes,
    *,
    sequence: int,
) -> dict[str, Any]:
    return federation_identity.issue_online_signer(
        root_bundle_path=root_path,
        root_passphrase=root_passphrase,
        signer_bundle_path=signer_path,
        signer_passphrase=signer_passphrase,
        sequence=sequence,
        not_before=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=2),
    )


def _pin_bilateral(
    registry_a: federation_peer_trust.PeerTrustRegistry,
    registry_b: federation_peer_trust.PeerTrustRegistry,
    identity_a: dict[str, Any],
    identity_b: dict[str, Any],
) -> None:
    registry_a.pin_peer(
        identity_b,
        expected_root_fingerprint=str(identity_b["rootFingerprint"]),
        metadata=_metadata("cn-south-1"),
        operator_id="operator-a",
        now=NOW - timedelta(minutes=40),
    )
    registry_a.verify_peer("fleet-b", identity_b, actor="verifier-a", now=NOW - timedelta(minutes=39))
    registry_a.activate_peer("fleet-b", actor="operator-a", now=NOW - timedelta(minutes=38))
    registry_b.pin_peer(
        identity_a,
        expected_root_fingerprint=str(identity_a["rootFingerprint"]),
        metadata=_metadata("cn-north-1"),
        operator_id="operator-b",
        now=NOW - timedelta(minutes=40),
    )
    registry_b.verify_peer("fleet-a", identity_a, actor="verifier-b", now=NOW - timedelta(minutes=39))
    registry_b.activate_peer("fleet-a", actor="operator-b", now=NOW - timedelta(minutes=38))


def _state_digest(registry: federation_peer_trust.PeerTrustRegistry, peer_fleet_id: str) -> str:
    peer = registry.get_peer(peer_fleet_id)
    return _digest(
        {
            "peer": peer,
            "signers": registry.list_online_signers(peer_fleet_id) if peer is not None else [],
            "readiness": registry.get_readiness_high_water(peer_fleet_id) if peer is not None else None,
        }
    )


def _observe_failure(
    *,
    claim: str,
    code: str,
    document: dict[str, Any],
    state: Callable[[], str],
    call: Callable[[], Any],
) -> dict[str, Any]:
    before = state()
    with pytest.raises(RuntimeError) as rejected:
        call()
    assert getattr(rejected.value, "code", None) == code
    after = state()
    assert after == before
    return {
        "claim": claim,
        "code": code,
        "preStateDigest": before,
        "postStateDigest": after,
        "documentDigest": _digest(document),
        "document": copy.deepcopy(document),
    }


def _trust_proof_fixture(tmp_path: Path) -> dict[str, Any]:
    root_a = tmp_path / "fleet-a" / "root.bundle.json"
    root_b = tmp_path / "fleet-b" / "root.bundle.json"
    root_c = tmp_path / "fleet-c" / "root.bundle.json"
    root_passphrase_a = b"fleet-a-root-passphrase-trust-proof"
    root_passphrase_b = b"fleet-b-root-passphrase-trust-proof"
    root_passphrase_c = b"fleet-c-root-passphrase-trust-proof"
    identity_a = federation_identity.create_fleet_root(
        "fleet-a", bundle_path=root_a, passphrase=root_passphrase_a, now=NOW - timedelta(hours=3)
    )
    identity_b = federation_identity.create_fleet_root(
        "fleet-b", bundle_path=root_b, passphrase=root_passphrase_b, now=NOW - timedelta(hours=3)
    )
    identity_c = federation_identity.create_fleet_root(
        "fleet-c", bundle_path=root_c, passphrase=root_passphrase_c, now=NOW - timedelta(hours=3)
    )

    signer_path_a = tmp_path / "fleet-a" / "signer.bundle.json"
    signer_path_b_old = tmp_path / "fleet-b" / "signer-old.bundle.json"
    signer_path_b = tmp_path / "fleet-b" / "signer-active.bundle.json"
    signer_path_c = tmp_path / "fleet-c" / "signer.bundle.json"
    signer_passphrase_a = b"fleet-a-signer-passphrase-trust-proof"
    signer_passphrase_b_old = b"fleet-b-old-signer-passphrase-proof"
    signer_passphrase_b = b"fleet-b-active-signer-passphrase-proof"
    signer_passphrase_c = b"fleet-c-signer-passphrase-trust-proof"
    certificate_a = _issue_signer(root_a, root_passphrase_a, signer_path_a, signer_passphrase_a, sequence=1)
    certificate_b_old = _issue_signer(root_b, root_passphrase_b, signer_path_b_old, signer_passphrase_b_old, sequence=1)
    certificate_b = _issue_signer(root_b, root_passphrase_b, signer_path_b, signer_passphrase_b, sequence=2)
    _issue_signer(root_c, root_passphrase_c, signer_path_c, signer_passphrase_c, sequence=1)
    signer_a = federation_identity.load_online_signer(
        signer_path_a, signer_passphrase_a, root_identity=identity_a, now=NOW
    )
    signer_b_old = federation_identity.load_online_signer(
        signer_path_b_old, signer_passphrase_b_old, root_identity=identity_b, now=NOW
    )
    signer_b = federation_identity.load_online_signer(
        signer_path_b, signer_passphrase_b, root_identity=identity_b, now=NOW
    )
    signer_c = federation_identity.load_online_signer(
        signer_path_c, signer_passphrase_c, root_identity=identity_c, now=NOW
    )

    registry_a = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-a" / "trust.sqlite3", identity_a)
    registry_b = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-b" / "trust.sqlite3", identity_b)
    _pin_bilateral(registry_a, registry_b, identity_a, identity_b)
    registry_a.accept_online_signer("fleet-b", certificate_b_old, actor="operator-a", now=NOW - timedelta(minutes=30))
    registry_a.revoke_online_signer(
        "fleet-b",
        str(certificate_b_old["signerKeyId"]),
        actor="operator-a",
        reason="rotation",
        revoked_at=NOW - timedelta(minutes=20),
    )
    registry_a.accept_online_signer("fleet-b", certificate_b, actor="operator-a", now=NOW - timedelta(minutes=10))
    registry_b.accept_online_signer("fleet-a", certificate_a, actor="operator-b", now=NOW - timedelta(minutes=10))

    readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _snapshot("fleet-b", NOW),
        sequence=2,
        signed_at=NOW,
        expires_at=NOW + timedelta(minutes=4),
    )
    federation_readiness_attestation.verify_and_record_readiness_attestation(
        readiness,
        peer_registry=registry_a,
        expected_peer_fleet_id="fleet-b",
        now=NOW,
    )
    replayed_readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _snapshot("fleet-b", NOW - timedelta(seconds=1)),
        sequence=1,
        signed_at=NOW,
        expires_at=NOW + timedelta(minutes=4),
    )
    expired_readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _snapshot("fleet-b", NOW - timedelta(minutes=1)),
        sequence=3,
        signed_at=NOW - timedelta(minutes=1),
        expires_at=NOW - timedelta(seconds=1),
    )
    future_readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _snapshot("fleet-b", NOW),
        sequence=3,
        signed_at=NOW + timedelta(seconds=45),
        expires_at=NOW + timedelta(minutes=2),
    )
    revoked_readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_b_old,
        _snapshot("fleet-b", NOW),
        sequence=3,
        signed_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
    )
    tofu_readiness = federation_readiness_attestation.issue_readiness_attestation(
        signer_c,
        _snapshot("fleet-c", NOW),
        sequence=1,
        signed_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
    )

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
    federation_challenge.verify_federation_challenge_response(
        challenge,
        response,
        peer_registry=registry_a,
        now=NOW + timedelta(seconds=2),
    )

    def state_a() -> str:
        return _state_digest(registry_a, "fleet-b")

    def state_b() -> str:
        return _state_digest(registry_b, "fleet-a")

    failures = [
        _observe_failure(
            claim="trustOnFirstUseIsRejected",
            code="FEDERATION_PEER_NOT_PINNED",
            document={"fleetIdentity": identity_c, "attestation": tofu_readiness},
            state=state_a,
            call=lambda: federation_readiness_attestation.verify_and_record_readiness_attestation(
                tofu_readiness,
                peer_registry=registry_a,
                expected_peer_fleet_id="fleet-c",
                now=NOW,
            ),
        ),
        _observe_failure(
            claim="readinessSequenceReplayIsRejected",
            code="FEDERATION_READINESS_SEQUENCE_REPLAY",
            document=replayed_readiness,
            state=state_a,
            call=lambda: federation_readiness_attestation.verify_and_record_readiness_attestation(
                replayed_readiness,
                peer_registry=registry_a,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            ),
        ),
        _observe_failure(
            claim="expiredReadinessAttestationIsRejected",
            code="FEDERATION_READINESS_ATTESTATION_EXPIRED",
            document=expired_readiness,
            state=state_a,
            call=lambda: federation_readiness_attestation.verify_and_record_readiness_attestation(
                expired_readiness,
                peer_registry=registry_a,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            ),
        ),
        _observe_failure(
            claim="futureReadinessAttestationBeyondSkewIsRejected",
            code="FEDERATION_READINESS_ATTESTATION_FROM_FUTURE",
            document=future_readiness,
            state=state_a,
            call=lambda: federation_readiness_attestation.verify_and_record_readiness_attestation(
                future_readiness,
                peer_registry=registry_a,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            ),
        ),
        _observe_failure(
            claim="challengeNonceReplayIsRejected",
            code="FEDERATION_CHALLENGE_NONCE_REPLAY",
            document=challenge,
            state=state_b,
            call=lambda: federation_challenge.respond_to_federation_challenge(
                challenge,
                peer_registry=registry_b,
                responder_signer=signer_b,
                now=NOW + timedelta(seconds=2),
            ),
        ),
        _observe_failure(
            claim="revokedFederationSignerIsRejected",
            code="FEDERATION_SIGNER_REVOKED",
            document=revoked_readiness,
            state=state_a,
            call=lambda: federation_readiness_attestation.verify_and_record_readiness_attestation(
                revoked_readiness,
                peer_registry=registry_a,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            ),
        ),
    ]

    proof = federation_trust_proof.build_federation_trust_proof(
        validated_at=NOW + timedelta(seconds=2),
        source_fleet_identity=identity_a,
        destination_fleet_identity=identity_b,
        source_peer_trust=registry_a.get_peer("fleet-b"),
        destination_peer_trust=registry_b.get_peer("fleet-a"),
        source_signer_trust=registry_b.get_online_signer("fleet-a", str(certificate_a["signerKeyId"])),
        destination_signer_trust=registry_a.get_online_signer("fleet-b", str(certificate_b["signerKeyId"])),
        revoked_destination_signer_trust=registry_a.get_online_signer("fleet-b", str(certificate_b_old["signerKeyId"])),
        readiness_attestation=readiness,
        readiness_high_water=registry_a.get_readiness_high_water("fleet-b"),
        challenge=challenge,
        challenge_response=response,
        age_recipients={"fleet-a": "age1fleetasourcepublicrecipient", "fleet-b": "age1fleetbdestinationpublicrecipient"},
        authority_identity_digests={"fleet-a": _digest({"authority": "fleet-a"}), "fleet-b": _digest({"authority": "fleet-b"})},
        failure_observations=failures,
    )
    return {"proof": proof, "identityA": identity_a, "identityB": identity_b}


@pytest.fixture(scope="module")
def trust_proof_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _trust_proof_fixture(tmp_path_factory.mktemp("federation-trust-proof"))


def _mutated_errors(proof: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> list[str]:
    candidate = copy.deepcopy(proof)
    mutate(candidate)
    candidate["proofDigest"] = federation_trust_proof.proof_digest(candidate)
    return federation_trust_proof.validate_federation_trust_proof(candidate)


def _update_documents(proof: dict[str, Any], updates: dict[str, dict[str, Any]]) -> None:
    for field, values in updates.items():
        proof[field].update(values)


def test_federation_trust_proof_recomputes_root_chain_readiness_challenge_and_failures(
    trust_proof_fixture: dict[str, Any],
) -> None:
    fixture = trust_proof_fixture
    proof = fixture["proof"]

    assert proof["schema"] == "federation-trust-proof-v1"
    assert federation_trust_proof.validate_federation_trust_proof(proof) == []
    for check_name in federation_trust_proof.FEDERATION_TRUST_PROOF_CHECKS:
        assert evidence_proof.VALIDATORS[check_name] is evidence_proof.validate_typed_federation_trust_proof
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []


def test_federation_trust_proof_rejects_tamper_secret_and_self_reported_failures(
    trust_proof_fixture: dict[str, Any],
) -> None:
    proof = trust_proof_fixture["proof"]

    tampered = copy.deepcopy(proof)
    tampered["readinessAttestation"]["snapshot"]["forecastHeadroom"] = 0
    tampered["proofDigest"] = federation_trust_proof.proof_digest(tampered)
    assert "readiness-signature-invalid" in federation_trust_proof.validate_federation_trust_proof(tampered)
    assert "readiness-signature-invalid" in evidence_proof.validate_check(
        "federationTrustProofIsSemanticallyValidated",
        {"status": "PASS", "evidence": tampered},
    )

    self_reported = copy.deepcopy(proof)
    self_reported["failureObservations"][0]["document"] = {}
    self_reported["failureObservations"][0]["documentDigest"] = _digest({})
    self_reported["proofDigest"] = federation_trust_proof.proof_digest(self_reported)
    assert "tofu-evidence-invalid" in federation_trust_proof.validate_federation_trust_proof(self_reported)

    leaked = copy.deepcopy(proof)
    leaked["ageRecipients"]["fleet-a"] = "AGE-SECRET-KEY-1LEAKED"
    leaked["proofDigest"] = federation_trust_proof.proof_digest(leaked)
    assert "federation-proof-contains-secret" in federation_trust_proof.validate_federation_trust_proof(leaked)

    digest_tamper = copy.deepcopy(proof)
    digest_tamper["proofDigest"] = "sha256:" + ("f" * 64)
    assert "proof-digest-mismatch" in federation_trust_proof.validate_federation_trust_proof(digest_tamper)


def test_federation_trust_proof_scalar_and_builder_guards(trust_proof_fixture: dict[str, Any]) -> None:
    proof = trust_proof_fixture["proof"]
    with pytest.raises(ValueError, match="timestamp-invalid"):
        federation_trust_proof._utc_iso(NOW.replace(tzinfo=None))

    for value in (None, "not-a-time", "2026-09-01T08:00:00", "2026-09-01T10:00:00+02:00"):
        errors: list[str] = []
        assert federation_trust_proof._parse_timestamp(value, "field", errors) is None
        assert errors == ["invalid-timestamp:field"]
    digest_errors: list[str] = []
    assert federation_trust_proof._typed_digest("not-a-digest", "field", digest_errors) == ""
    assert digest_errors == ["invalid-sha256:field"]
    assert federation_trust_proof.validate_federation_trust_proof([]) == ["federation-trust-proof-must-be-object"]
    assert federation_trust_proof.validate_federation_trust_proof({"value": float("nan")}) == [
        "federation-proof-canonical-payload-invalid"
    ]

    with pytest.raises(ValueError, match="invalid federation trust proof"):
        federation_trust_proof.build_federation_trust_proof(
            validated_at=NOW + timedelta(seconds=2),
            source_fleet_identity=proof["sourceFleetIdentity"],
            destination_fleet_identity=proof["destinationFleetIdentity"],
            source_peer_trust=None,
            destination_peer_trust=proof["destinationPeerTrust"],
            source_signer_trust=proof["sourceSignerTrust"],
            destination_signer_trust=proof["destinationSignerTrust"],
            revoked_destination_signer_trust=proof["revokedDestinationSignerTrust"],
            readiness_attestation=proof["readinessAttestation"],
            readiness_high_water=proof["readinessHighWater"],
            challenge=proof["challenge"],
            challenge_response=proof["challengeResponse"],
            age_recipients=proof["ageRecipients"],
            authority_identity_digests=proof["authorityIdentityDigests"],
            failure_observations=proof["failureObservations"],
        )


def test_federation_trust_proof_peer_and_signer_tamper_matrix(trust_proof_fixture: dict[str, Any]) -> None:
    proof = trust_proof_fixture["proof"]
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.pop("sourcePeerTrust"), "federation-trust-proof-fields-invalid"),
        (lambda item: item.__setitem__("schema", "self-declared-pass"), "federation-trust-proof-schema-invalid"),
        (lambda item: item.__setitem__("sourceFleetIdentity", {}), "source-identity-invalid"),
        (lambda item: item.__setitem__("sourcePeerTrust", None), "source-peer-trust-must-be-object"),
        (lambda item: item["sourcePeerTrust"].__setitem__("extra", True), "source-peer-trust-fields-invalid"),
        (lambda item: item["sourcePeerTrust"].__setitem__("schema", "wrong"), "source-peer-trust-schema-invalid"),
        (lambda item: item["sourcePeerTrust"].__setitem__("state", "SUSPENDED"), "source-peer-trust-not-active"),
        (lambda item: item["sourcePeerTrust"].__setitem__("peerFleetId", "fleet-c"), "source-peer-trust-fleet-mismatch"),
        (
            lambda item: item["sourcePeerTrust"].__setitem__("fleetIdentity", item["sourceFleetIdentity"]),
            "source-peer-trust-identity-binding-mismatch",
        ),
        (lambda item: item["sourcePeerTrust"].__setitem__("rootKeyId", "fed-root-wrong"), "source-peer-trust-root-key-mismatch"),
        (
            lambda item: item["sourcePeerTrust"].__setitem__("rootFingerprint", "sha256:" + ("0" * 64)),
            "source-peer-trust-root-fingerprint-mismatch",
        ),
        (lambda item: item["sourcePeerTrust"].__setitem__("pinnedMetadata", None), "source-peer-trust-metadata-invalid"),
        (
            lambda item: item["sourcePeerTrust"].__setitem__("metadataDigest", "sha256:" + ("0" * 64)),
            "source-peer-trust-metadata-digest-mismatch",
        ),
        (lambda item: item["sourcePeerTrust"].__setitem__("pinnedBy", ""), "source-peer-trust-operator-pin-incomplete"),
        (lambda item: item["sourcePeerTrust"].__setitem__("revokedAt", "2026-09-01T08:00:00Z"), "source-peer-trust-revoked"),
        (lambda item: item.__setitem__("sourceSignerTrust", None), "source-signer-trust-must-be-object"),
        (lambda item: item["sourceSignerTrust"].__setitem__("extra", True), "source-signer-trust-fields-invalid"),
        (lambda item: item["sourceSignerTrust"].__setitem__("schema", "wrong"), "source-signer-trust-schema-invalid"),
        (lambda item: item["sourceSignerTrust"].__setitem__("peerFleetId", "fleet-c"), "source-signer-trust-fleet-mismatch"),
        (lambda item: item["sourceSignerTrust"].__setitem__("certificate", None), "source-signer-trust-certificate-must-be-object"),
        (lambda item: item["sourceSignerTrust"].__setitem__("signerKeyId", "fed-signer-wrong"), "source-signer-trust-key-binding-mismatch"),
        (lambda item: item["sourceSignerTrust"].__setitem__("sequence", 9), "source-signer-trust-sequence-binding-mismatch"),
        (
            lambda item: item["sourceSignerTrust"].__setitem__("certificateDigest", "sha256:" + ("0" * 64)),
            "source-signer-trust-certificate-digest-mismatch",
        ),
        (
            lambda item: item["sourceSignerTrust"]["certificate"].__setitem__("purposes", []),
            "source-signer-trust-purpose-missing",
        ),
        (
            lambda item: item["sourceSignerTrust"].__setitem__("revokedAt", "2026-09-01T07:59:00Z"),
            "source-signer-trust-unexpectedly-revoked",
        ),
        (
            lambda item: item["revokedDestinationSignerTrust"].__setitem__("acceptedAt", "invalid"),
            "invalid-timestamp:revoked-destination-signer-trust.acceptedAt",
        ),
        (
            lambda item: item["revokedDestinationSignerTrust"].__setitem__("revokedAt", "2026-09-01T09:00:00Z"),
            "revoked-destination-signer-trust-revocation-from-future",
        ),
        (
            lambda item: item["revokedDestinationSignerTrust"].__setitem__("revokedBy", ""),
            "revoked-destination-signer-trust-revocation-incomplete",
        ),
        (
            lambda item: item["destinationSignerTrust"].__setitem__(
                "sequence", item["revokedDestinationSignerTrust"]["sequence"]
            ),
            "signer-rotation-sequence-not-increased",
        ),
        (
            lambda item: item["destinationSignerTrust"].__setitem__(
                "signerKeyId", item["revokedDestinationSignerTrust"]["signerKeyId"]
            ),
            "rotated-signer-key-not-distinct",
        ),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert any(error == expected or error.startswith(expected + ":") for error in errors), errors


def test_federation_trust_proof_readiness_and_challenge_tamper_matrix(trust_proof_fixture: dict[str, Any]) -> None:
    proof = trust_proof_fixture["proof"]
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("readinessAttestation", None), "readiness-must-be-object"),
        (lambda item: item["readinessAttestation"].pop("schema"), "readiness-fields-invalid"),
        (
            lambda item: item["readinessAttestation"].__setitem__("signerCertificate", None),
            "readiness-certificate-must-be-object",
        ),
        (lambda item: item["readinessAttestation"].__setitem__("fleetId", "fleet-c"), "readiness-fleet-mismatch"),
        (lambda item: item["readinessAttestation"].__setitem__("sequence", True), "readiness-sequence-invalid"),
        (
            lambda item: item["readinessAttestation"].__setitem__("signedAt", "invalid"),
            "invalid-timestamp:readiness.signedAt",
        ),
        (
            lambda item: item["readinessAttestation"].__setitem__(
                "expiresAt", item["readinessAttestation"]["signedAt"]
            ),
            "readiness-lifetime-invalid",
        ),
        (
            lambda item: item["readinessAttestation"].__setitem__("expiresAt", "2026-09-01T08:00:01Z"),
            "readiness-expired",
        ),
        (
            lambda item: _update_documents(
                item,
                {"readinessAttestation": {"signedAt": "2026-09-01T08:01:00Z", "expiresAt": "2026-09-01T08:02:00Z"}},
            ),
            "readiness-from-future",
        ),
        (
            lambda item: item["readinessHighWater"].__setitem__("peerFleetId", "fleet-c"),
            "readiness-high-water-binding-mismatch",
        ),
        (
            lambda item: _update_documents(
                item,
                {"readinessAttestation": {"signedAt": "2026-09-01T09:59:59Z", "expiresAt": "2026-09-01T10:00:01Z"}},
            ),
            "readiness-signer-window-invalid",
        ),
        (lambda item: item.__setitem__("challenge", None), "challenge-must-be-object"),
        (lambda item: item["challenge"].__setitem__("extra", True), "challenge-fields-invalid"),
        (lambda item: item["challengeResponse"].__setitem__("extra", True), "challenge-response-fields-invalid"),
        (
            lambda item: item["challenge"].__setitem__("signerKeyId", "fed-signer-wrong"),
            "challenge-source-signer-mismatch",
        ),
        (
            lambda item: item["challengeResponse"].__setitem__("signerKeyId", "fed-signer-wrong"),
            "challenge-destination-signer-mismatch",
        ),
        (lambda item: item["challenge"].__setitem__("fleetId", "fleet-c"), "challenge-fleet-binding-mismatch"),
        (lambda item: item["challenge"].__setitem__("nonce", "invalid"), "challenge-nonce-invalid"),
        (lambda item: item["challengeResponse"].__setitem__("nonce", "different"), "challenge-nonce-binding-mismatch"),
        (
            lambda item: item["challengeResponse"].__setitem__("challengeDigest", "sha256:" + ("0" * 64)),
            "challenge-digest-binding-mismatch",
        ),
        (
            lambda item: item["challengeResponse"].__setitem__("sessionPurpose", federation_challenge.SESSION_PURPOSE_FEDERATED_DR),
            "challenge-purpose-binding-mismatch",
        ),
        (
            lambda item: item["challenge"].__setitem__("issuedAt", "2026-09-01T08:00:03Z"),
            "challenge-time-binding-invalid",
        ),
        (
            lambda item: _update_documents(
                item,
                {
                    "challenge": {"expiresAt": "2026-09-01T08:05:00Z"},
                    "challengeResponse": {"expiresAt": "2026-09-01T08:05:00Z"},
                },
            ),
            "challenge-lifetime-invalid",
        ),
        (
            lambda item: item["challengeResponse"].__setitem__("expiresAt", "2026-09-01T08:01:59Z"),
            "challenge-expiry-binding-mismatch",
        ),
        (
            lambda item: item["challenge"].__setitem__("issuedAt", "2026-09-01T05:59:59Z"),
            "challenge-source-signer-window-invalid",
        ),
        (
            lambda item: item["challengeResponse"].__setitem__("respondedAt", "2026-09-01T05:59:59Z"),
            "challenge-destination-signer-window-invalid",
        ),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert expected in errors, errors


def _change_failure_document(
    proof: dict[str, Any],
    claim: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    observation = next(item for item in proof["failureObservations"] if item["claim"] == claim)
    mutate(observation["document"])
    observation["documentDigest"] = _digest(observation["document"])


def test_federation_trust_proof_failure_observation_tamper_matrix(trust_proof_fixture: dict[str, Any]) -> None:
    proof = trust_proof_fixture["proof"]
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("failureObservations", None), "failure-observations-must-be-list"),
        (lambda item: item["failureObservations"].pop(), "failure-observation-inventory-mismatch"),
        (lambda item: item["failureObservations"][0].__setitem__("extra", True), "failure-fields-invalid:trustOnFirstUseIsRejected"),
        (lambda item: item["failureObservations"][0].__setitem__("code", "SELF_REPORTED_PASS"), "failure-code-mismatch:trustOnFirstUseIsRejected"),
        (lambda item: item["failureObservations"][0].__setitem__("preStateDigest", "invalid"), "invalid-sha256:trustOnFirstUseIsRejected.preStateDigest"),
        (
            lambda item: item["failureObservations"][0].__setitem__("postStateDigest", "sha256:" + ("0" * 64)),
            "failure-mutated-state:trustOnFirstUseIsRejected",
        ),
        (
            lambda item: item["failureObservations"][0].__setitem__("documentDigest", "sha256:" + ("0" * 64)),
            "failure-document-digest-mismatch:trustOnFirstUseIsRejected",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "trustOnFirstUseIsRejected",
                lambda document: document.clear(),
            ),
            "tofu-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "trustOnFirstUseIsRejected",
                lambda document: document.__setitem__("fleetIdentity", item["destinationFleetIdentity"]),
            ),
            "tofu-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "readinessSequenceReplayIsRejected",
                lambda document: document.__setitem__("sequence", item["readinessHighWater"]["highSequence"]),
            ),
            "readiness-replay-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "expiredReadinessAttestationIsRejected",
                lambda document: document.__setitem__("expiresAt", "2026-09-01T08:03:00Z"),
            ),
            "expired-readiness-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "futureReadinessAttestationBeyondSkewIsRejected",
                lambda document: document.__setitem__("signedAt", "2026-09-01T08:00:02Z"),
            ),
            "future-readiness-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "challengeNonceReplayIsRejected",
                lambda document: document.__setitem__("nonce", "different"),
            ),
            "challenge-replay-evidence-invalid",
        ),
        (
            lambda item: _change_failure_document(
                item,
                "revokedFederationSignerIsRejected",
                lambda document: document.__setitem__("signerKeyId", item["destinationSignerTrust"]["signerKeyId"]),
            ),
            "revoked-signer-evidence-invalid",
        ),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert expected in errors, errors


def test_federation_trust_proof_identity_separation_tamper_matrix(trust_proof_fixture: dict[str, Any]) -> None:
    proof = trust_proof_fixture["proof"]
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (
            lambda item: item.__setitem__("destinationFleetIdentity", item["sourceFleetIdentity"]),
            "fleet-identities-not-distinct",
        ),
        (lambda item: item.__setitem__("ageRecipients", None), "age-recipients-must-be-object"),
        (lambda item: item["ageRecipients"].pop("fleet-a"), "age-recipient-inventory-mismatch"),
        (lambda item: item["ageRecipients"].__setitem__("fleet-a", 7), "age-recipient-invalid:fleet-a"),
        (lambda item: item.__setitem__("authorityIdentityDigests", None), "authority-identity-digests-must-be-object"),
        (lambda item: item["authorityIdentityDigests"].pop("fleet-a"), "authority-identity-inventory-mismatch"),
        (lambda item: item["authorityIdentityDigests"].__setitem__("fleet-a", "invalid"), "invalid-sha256:authorityIdentityDigests.fleet-a"),
        (
            lambda item: item["authorityIdentityDigests"].__setitem__(
                "fleet-a", item["sourceFleetIdentity"]["rootFingerprint"]
            ),
            "authority-identity-not-distinct:fleet-a",
        ),
        (
            lambda item: item["authorityIdentityDigests"].__setitem__(
                "fleet-a", item["authorityIdentityDigests"]["fleet-b"]
            ),
            "authority-identities-not-distinct",
        ),
        (lambda item: item.__setitem__("validatedAt", None), "invalid-timestamp:validatedAt"),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert expected in errors, errors
