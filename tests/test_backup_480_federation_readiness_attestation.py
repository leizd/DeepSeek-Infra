from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    federation_identity,
    federation_peer_trust,
    federation_readiness_attestation,
    resilience_federation_readiness,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
ROOT_PASSPHRASE = b"peer-root-passphrase-for-readiness-tests"
SIGNER_PASSPHRASE = b"peer-signer-passphrase-readiness"
WIRES = ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"]


def _metadata() -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": "cn-north-1",
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _snapshot(*, generated_at: datetime = NOW) -> dict[str, object]:
    snapshot = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-b",
        wire_compatibility=WIRES,
        available_failure_domains=["fleet-b-site-1", "fleet-b-site-2"],
        forecast_headroom=4_096,
        cost_class="warm",
        readiness="READY",
        now=generated_at,
    )
    snapshot["riskDetails"] = {
        "coverage": {"committed": 2, "required": 2},
        "subjects": [{"riskId": "risk-1", "severity": "LOW"}],
    }
    snapshot["snapshotDigest"] = resilience_federation_readiness._snapshot_digest(snapshot)
    return snapshot


def _trust_fixture(
    tmp_path: Path,
) -> tuple[
    federation_peer_trust.PeerTrustRegistry,
    dict[str, object],
    federation_identity.OnlineFleetSigner,
    dict[str, object],
]:
    local_identity = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=tmp_path / "local" / "root.bundle.json",
        passphrase=b"local-root-passphrase-readiness",
        now=NOW - timedelta(hours=2),
    )
    peer_root = tmp_path / "peer" / "root.bundle.json"
    peer_identity = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=peer_root,
        passphrase=ROOT_PASSPHRASE,
        now=NOW - timedelta(hours=2),
    )
    signer_bundle = tmp_path / "peer" / "readiness-signer.bundle.json"
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=peer_root,
        root_passphrase=ROOT_PASSPHRASE,
        signer_bundle_path=signer_bundle,
        signer_passphrase=SIGNER_PASSPHRASE,
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(federation_identity.PURPOSE_READINESS_ATTESTATION,),
    )
    signer = federation_identity.load_online_signer(
        signer_bundle,
        SIGNER_PASSPHRASE,
        root_identity=peer_identity,
        now=NOW,
    )
    registry = federation_peer_trust.PeerTrustRegistry(tmp_path / "trust" / "peers.sqlite3", local_identity)
    registry.pin_peer(
        peer_identity,
        expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
        metadata=_metadata(),
        operator_id="operator-1",
        now=NOW - timedelta(minutes=30),
    )
    registry.verify_peer(
        "fleet-b",
        peer_identity,
        actor="challenge-verifier",
        now=NOW - timedelta(minutes=29),
    )
    registry.activate_peer("fleet-b", actor="operator-1", now=NOW - timedelta(minutes=28))
    registry.accept_online_signer("fleet-b", certificate, actor="operator-1", now=NOW)
    return registry, local_identity, signer, certificate


def _attestation(
    signer: federation_identity.OnlineFleetSigner,
    *,
    sequence: int,
    snapshot: dict[str, object] | None = None,
    signed_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return federation_readiness_attestation.issue_readiness_attestation(
        signer,
        _snapshot(generated_at=signed_at) if snapshot is None else snapshot,
        sequence=sequence,
        signed_at=signed_at,
        expires_at=expires_at or signed_at + timedelta(minutes=4),
    )


def test_readiness_attestation_binds_complete_canonical_snapshot_and_root_chain(tmp_path: Path) -> None:
    registry, _, signer, certificate = _trust_fixture(tmp_path)
    snapshot = _snapshot()
    attestation = _attestation(signer, sequence=1, snapshot=snapshot)

    assert attestation["schema"] == "federation-readiness-attestation-v1"
    assert attestation["fleetId"] == "fleet-b"
    assert attestation["sequence"] == 1
    assert attestation["snapshot"] == snapshot
    assert attestation["snapshotDigest"] == snapshot["snapshotDigest"]
    assert attestation["signerCertificate"] == certificate
    assert attestation["signerKeyId"] == certificate["signerKeyId"]
    assert "riskDigest" not in attestation

    verified = federation_readiness_attestation.verify_and_record_readiness_attestation(
        attestation,
        peer_registry=registry,
        expected_peer_fleet_id="fleet-b",
        now=NOW,
    )
    assert verified == attestation
    assert registry.get_readiness_high_water("fleet-b") == {
        "schema": "federation-readiness-sequence-v1",
        "peerFleetId": "fleet-b",
        "highSequence": 1,
        "signerKeyId": certificate["signerKeyId"],
        "attestationDigest": federation_readiness_attestation.attestation_digest(attestation),
        "acceptedAt": "2026-09-01T04:00:00Z",
        "revision": 1,
    }


def test_readiness_attestation_rejects_nested_tamper_and_risk_projection_substitution(tmp_path: Path) -> None:
    registry, _, signer, _ = _trust_fixture(tmp_path)
    attestation = _attestation(signer, sequence=1)
    tampered = copy.deepcopy(attestation)
    tampered["snapshot"]["riskDetails"]["coverage"]["committed"] = 0  # type: ignore[index]
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as tamper:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            tampered,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert tamper.value.code == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"

    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as projection:
        federation_readiness_attestation.issue_readiness_attestation(
            signer,
            {"riskDigest": "sha256:not-a-full-snapshot"},
            sequence=1,
            signed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
    assert projection.value.code == "FEDERATION_READINESS_SNAPSHOT_SCHEMA_INVALID"


def test_readiness_sequence_replay_is_durably_rejected_across_restart(tmp_path: Path) -> None:
    registry, local_identity, signer, _ = _trust_fixture(tmp_path)
    first = _attestation(signer, sequence=7)
    federation_readiness_attestation.verify_and_record_readiness_attestation(
        first,
        peer_registry=registry,
        expected_peer_fleet_id="fleet-b",
        now=NOW,
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as replay:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            first,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert replay.value.code == "FEDERATION_READINESS_SEQUENCE_REPLAY"

    conflicting = _attestation(signer, sequence=7, signed_at=NOW + timedelta(seconds=1))
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as sequence_conflict:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            conflicting,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW + timedelta(seconds=1),
        )
    assert sequence_conflict.value.code == "FEDERATION_READINESS_SEQUENCE_CONFLICT"

    restarted = federation_peer_trust.PeerTrustRegistry(registry.db_path, local_identity)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as restart_replay:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            first,
            peer_registry=restarted,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert restart_replay.value.code == "FEDERATION_READINESS_SEQUENCE_REPLAY"

    second = _attestation(signer, sequence=8, signed_at=NOW + timedelta(seconds=1))
    assert federation_readiness_attestation.verify_and_record_readiness_attestation(
        second,
        peer_registry=restarted,
        expected_peer_fleet_id="fleet-b",
        now=NOW + timedelta(seconds=1),
    )["sequence"] == 8
    assert restarted.get_readiness_high_water("fleet-b")["highSequence"] == 8  # type: ignore[index]


def test_readiness_attestation_rejects_expiry_future_time_wrong_fleet_and_invalid_lifetime(tmp_path: Path) -> None:
    registry, _, signer, _ = _trust_fixture(tmp_path)
    expired = _attestation(
        signer,
        sequence=1,
        signed_at=NOW - timedelta(minutes=4),
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as expired_error:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            expired,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert expired_error.value.code == "FEDERATION_READINESS_ATTESTATION_EXPIRED"

    future = _attestation(
        signer,
        sequence=2,
        signed_at=NOW + timedelta(seconds=31),
        expires_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as future_error:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            future,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
            max_future_skew_seconds=30,
        )
    assert future_error.value.code == "FEDERATION_READINESS_ATTESTATION_FROM_FUTURE"

    valid = _attestation(signer, sequence=3)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as wrong_fleet:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            valid,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-c",
            now=NOW,
        )
    assert wrong_fleet.value.code == "FEDERATION_READINESS_FLEET_MISMATCH"

    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as lifetime:
        _attestation(
            signer,
            sequence=4,
            expires_at=NOW + timedelta(minutes=6),
        )
    assert lifetime.value.code == "FEDERATION_READINESS_ATTESTATION_LIFETIME_INVALID"


def test_readiness_verification_rechecks_current_signer_and_root_authorization(tmp_path: Path) -> None:
    registry, _, signer, certificate = _trust_fixture(tmp_path)
    attestation = _attestation(signer, sequence=1)
    registry.revoke_online_signer(
        "fleet-b",
        str(certificate["signerKeyId"]),
        actor="operator-1",
        reason="signer-incident",
        revoked_at=NOW,
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as signer_revoked:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            attestation,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert signer_revoked.value.code == "FEDERATION_SIGNER_REVOKED"
    with pytest.raises(federation_peer_trust.FederationTrustError) as atomic_recheck:
        registry.record_readiness_sequence(
            "fleet-b",
            signer_key_id=str(certificate["signerKeyId"]),
            sequence=1,
            attestation_digest=federation_readiness_attestation.attestation_digest(attestation),
            accepted_at=NOW,
        )
    assert atomic_recheck.value.code == "FEDERATION_SIGNER_REVOKED"


def test_readiness_issue_and_verify_reject_malformed_sequences_timestamps_and_snapshots(tmp_path: Path) -> None:
    registry, _, signer, _ = _trust_fixture(tmp_path)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as sequence:
        _attestation(signer, sequence=0)
    assert sequence.value.code == "FEDERATION_READINESS_SEQUENCE_INVALID"
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as naive_time:
        _attestation(signer, sequence=1, signed_at=datetime(2026, 9, 1, 4, 0))
    assert naive_time.value.code == "FEDERATION_READINESS_TIMESTAMP_INVALID"

    malformed = _attestation(signer, sequence=1)
    malformed["snapshotDigest"] = "0" * 64
    unsigned = {
        key: value
        for key, value in malformed.items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    resigned = federation_identity.sign_federation_document(
        signer,
        unsigned,
        purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as digest:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            resigned,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert digest.value.code == "FEDERATION_READINESS_SNAPSHOT_DIGEST_INVALID"

    with pytest.raises(federation_peer_trust.FederationTrustError) as journal_sequence:
        registry.record_readiness_sequence(
            "fleet-b",
            signer_key_id=signer.signer_key_id,
            sequence=0,
            attestation_digest="sha256:" + ("0" * 64),
            accepted_at=NOW,
        )
    assert journal_sequence.value.code == "FEDERATION_READINESS_SEQUENCE_INVALID"
    with pytest.raises(federation_peer_trust.FederationTrustError) as journal_digest:
        registry.record_readiness_sequence(
            "fleet-b",
            signer_key_id=signer.signer_key_id,
            sequence=1,
            attestation_digest="not-a-digest",
            accepted_at=NOW,
        )
    assert journal_digest.value.code == "FEDERATION_READINESS_ATTESTATION_DIGEST_INVALID"


def test_readiness_canonical_json_and_timestamp_boundaries_are_unambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    assert federation_readiness_attestation._canonical_json({"ratio": 1.25}) == '{"ratio":1.25}'
    invalid_payloads = (
        {"value": float("nan")},
        {"value": "\ud800"},
        {1: "ambiguous"},
        {"\ud800": "ambiguous"},
        {"tuple": ("not", "a", "json-array")},
    )
    for payload in invalid_payloads:
        with pytest.raises(federation_readiness_attestation.FederationReadinessError) as invalid:
            federation_readiness_attestation._canonical_json(payload)
        assert invalid.value.code == "FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID"

    for timestamp in (None, "not-a-time", "2026-09-01T04:00:00", "2026-09-01T04:00:00+00:00"):
        with pytest.raises(federation_readiness_attestation.FederationReadinessError) as invalid_time:
            federation_readiness_attestation._parse_timestamp(timestamp)
        assert invalid_time.value.code == "FEDERATION_READINESS_TIMESTAMP_INVALID"
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as invalid_digest:
        federation_readiness_attestation._snapshot_digest("sha256:" + ("0" * 64))
    assert invalid_digest.value.code == "FEDERATION_READINESS_SNAPSHOT_DIGEST_INVALID"

    def fail_json(*_args: object, **_kwargs: object) -> str:
        raise ValueError("forced-json-failure")

    monkeypatch.setattr(federation_readiness_attestation.json, "dumps", fail_json)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as serializer_failure:
        federation_readiness_attestation._canonical_json({"safe": "value"})
    assert serializer_failure.value.code == "FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID"


def test_readiness_snapshot_and_envelope_semantics_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _, signer, certificate = _trust_fixture(tmp_path)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as snapshot_type:
        federation_readiness_attestation.issue_readiness_attestation(
            signer,
            None,  # type: ignore[arg-type]
            sequence=1,
            signed_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
    assert snapshot_type.value.code == "FEDERATION_READINESS_SNAPSHOT_INVALID"

    missing = _snapshot()
    del missing["readiness"]
    missing["snapshotDigest"] = resilience_federation_readiness._snapshot_digest(missing)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as missing_fields:
        _attestation(signer, sequence=1, snapshot=missing)
    assert missing_fields.value.code == "FEDERATION_READINESS_SNAPSHOT_FIELDS_MISSING"

    wrong_fleet = _snapshot()
    wrong_fleet["fleetId"] = "fleet-c"
    wrong_fleet["snapshotDigest"] = resilience_federation_readiness._snapshot_digest(wrong_fleet)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as snapshot_fleet:
        _attestation(signer, sequence=1, snapshot=wrong_fleet)
    assert snapshot_fleet.value.code == "FEDERATION_READINESS_SNAPSHOT_FLEET_MISMATCH"

    credentialed = _snapshot()
    credentialed["apiToken"] = "must-never-be-signed"
    credentialed["snapshotDigest"] = resilience_federation_readiness._snapshot_digest(credentialed)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as credentials:
        _attestation(signer, sequence=1, snapshot=credentialed)
    assert credentials.value.code == "FEDERATION_READINESS_SNAPSHOT_CONTAINS_CREDENTIALS"

    near_certificate_expiry = NOW + timedelta(hours=1, minutes=59)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as signer_window:
        _attestation(
            signer,
            sequence=1,
            signed_at=near_certificate_expiry,
            expires_at=near_certificate_expiry + timedelta(minutes=4),
        )
    assert signer_window.value.code == "FEDERATION_READINESS_SIGNER_WINDOW_INVALID"

    wrong_purpose_certificate = {**certificate, "purposes": [federation_identity.PURPOSE_INGRESS_GRANT]}
    wrong_purpose_signer = federation_identity.OnlineFleetSigner(signer._private_key, wrong_purpose_certificate)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as local_purpose:
        _attestation(wrong_purpose_signer, sequence=1)
    assert local_purpose.value.code == "FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"

    valid = _attestation(signer, sequence=1)
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as envelope_type:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            None,  # type: ignore[arg-type]
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert envelope_type.value.code == "FEDERATION_READINESS_ATTESTATION_INVALID"
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as fields:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            {**valid, "unknownField": True},
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert fields.value.code == "FEDERATION_READINESS_ATTESTATION_FIELDS_INVALID"
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as schema:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            {**valid, "schema": "wrong-schema"},
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert schema.value.code == "FEDERATION_READINESS_ATTESTATION_SCHEMA_INVALID"
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as certificate_type:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            {**valid, "signerCertificate": None},
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert certificate_type.value.code == "FEDERATION_READINESS_SIGNER_CERTIFICATE_INVALID"

    with monkeypatch.context() as context:
        context.setattr(registry, "get_peer", lambda _fleet_id: None)
        with pytest.raises(federation_readiness_attestation.FederationReadinessError) as unknown_peer:
            federation_readiness_attestation.verify_and_record_readiness_attestation(
                valid,
                peer_registry=registry,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            )
        assert unknown_peer.value.code == "FEDERATION_PEER_NOT_PINNED"
    with monkeypatch.context() as context:
        context.setattr(registry, "get_peer", lambda _fleet_id: {"fleetIdentity": None})
        with pytest.raises(federation_readiness_attestation.FederationReadinessError) as malformed_peer:
            federation_readiness_attestation.verify_and_record_readiness_attestation(
                valid,
                peer_registry=registry,
                expected_peer_fleet_id="fleet-b",
                now=NOW,
            )
        assert malformed_peer.value.code == "FEDERATION_PEER_IDENTITY_INVALID"

    before_certificate = NOW - timedelta(hours=1, minutes=1)
    pre_certificate_snapshot = _snapshot(generated_at=before_certificate)
    pre_certificate_payload = {
        key: value
        for key, value in valid.items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    pre_certificate_payload.update(
        {
            "snapshot": pre_certificate_snapshot,
            "snapshotDigest": pre_certificate_snapshot["snapshotDigest"],
            "signedAt": "2026-09-01T02:59:00Z",
            "expiresAt": "2026-09-01T03:03:00Z",
        }
    )
    maliciously_backdated = federation_identity.sign_federation_document(
        signer,
        pre_certificate_payload,
        purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
    )
    with pytest.raises(federation_readiness_attestation.FederationReadinessError) as backdated:
        federation_readiness_attestation.verify_and_record_readiness_attestation(
            maliciously_backdated,
            peer_registry=registry,
            expected_peer_fleet_id="fleet-b",
            now=NOW,
        )
    assert backdated.value.code == "FEDERATION_READINESS_SIGNER_WINDOW_INVALID"
