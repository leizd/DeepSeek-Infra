from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_publish,
    backup_target_store,
    federated_replica_attestation,
    federation_identity,
    federation_peer_trust,
    federation_transfer_journal,
)
from tests.test_backup_480_federated_replica_commit import TARGET_ID, _commit, _memory_target, _prepared_fixture
from tests.test_backup_480_federated_replica_receiver import NOW, _metadata


def _storage_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _advance_sender_to_remote_verifying(fixture: dict[str, Any], root: Path) -> federation_transfer_journal.FederatedTransferJournal:
    journal = federation_transfer_journal.FederatedTransferJournal(root / "fleet-a" / "transfers.sqlite3", fixture["identityA"])
    transfer = journal.persist_proposed_transfer(
        transfer_id=fixture["transferId"],
        source_fleet_id="fleet-a",
        destination_fleet_id="fleet-b",
        policy_id=str(fixture["receipt"]["policyId"]),
        backup_id=str(fixture["receipt"]["backupId"]),
        object_set_digest=fixture["federationDigest"],
        now=NOW,
    )
    for offset, state in enumerate(
        (
            federation_transfer_journal.STATE_GRANT_REQUESTED,
            federation_transfer_journal.STATE_GRANT_VERIFIED,
            federation_transfer_journal.STATE_TRANSFERRING,
            federation_transfer_journal.STATE_REMOTE_VERIFYING,
        ),
        start=1,
    ):
        transfer = journal.advance_transfer(
            fixture["transferId"],
            expected_revision=int(transfer["revision"]),
            next_state=state,
            details={"phase": state},
            now=NOW + timedelta(seconds=offset),
        )
    return journal


def _attestation_fixture(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequence: int = 1,
) -> dict[str, Any]:
    fixture = _prepared_fixture(tmp_settings)
    target, store = _memory_target()
    monkeypatch.setattr(
        backup_publish,
        "_utc_iso",
        lambda: (NOW + timedelta(seconds=9)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    committed = _commit(fixture, monkeypatch, target)
    sender_journal = _advance_sender_to_remote_verifying(fixture, tmp_settings)
    attestation = federated_replica_attestation.issue_replica_attestation(
        signer=fixture["signerB"],
        receiver=fixture["receiver"],
        transfer_id=fixture["transferId"],
        remote_target_id=TARGET_ID,
        failure_domain_metadata=_metadata(region="cn-south-1"),
        sequence=sequence,
        signed_at=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=71),
    )
    return {
        **fixture,
        "target": target,
        "store": store,
        "committed": committed,
        "senderJournal": sender_journal,
        "attestation": attestation,
        "receiptBytes": _storage_bytes(committed.receipt),
        "commitBytes": _storage_bytes(committed.commit),
    }


def _verify(fixture: dict[str, Any], *, attestation: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return federated_replica_attestation.verify_and_record_replica_attestation(
        fixture["attestation"] if attestation is None else attestation,
        peer_registry=fixture["registryA"],
        sender_journal=fixture["senderJournal"],
        source_receipt=fixture["receipt"],
        remote_receipt_bytes=fixture["receiptBytes"],
        remote_commit_bytes=fixture["commitBytes"],
        now=NOW + timedelta(seconds=12),
        **kwargs,
    )


def _resign(fixture: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: copy.deepcopy(value)
        for key, value in fixture["attestation"].items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    payload.update(changes)
    return federation_identity.sign_federation_document(
        fixture["signerB"],
        payload,
        purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )


def _assert_attestation_error(call: Any, code: str) -> None:
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as rejected:
        call()
    assert rejected.value.code == code


def test_signed_replica_attestation_binds_real_receipt_commit_and_sender_recording(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    attestation = fixture["attestation"]
    committed = fixture["committed"]

    assert set(attestation) == federated_replica_attestation.REPLICA_ATTESTATION_FIELDS
    assert attestation["schema"] == federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA
    assert attestation["fleetId"] == attestation["destinationFleetId"] == "fleet-b"
    assert attestation["sourceFleetId"] == "fleet-a"
    assert attestation["transferId"] == fixture["transferId"]
    assert attestation["backupId"] == fixture["package"].backup_id
    assert attestation["objectSetDigest"] == fixture["federationDigest"]
    assert attestation["remoteTargetId"] == TARGET_ID
    assert attestation["remoteReceiptDigest"] == "sha256:" + hashlib.sha256(fixture["receiptBytes"]).hexdigest()
    assert attestation["remoteCommitDigest"] == "sha256:" + hashlib.sha256(fixture["commitBytes"]).hexdigest()
    assert attestation["remoteCommitDigest"] != "sha256:" + str(committed.commit["commitHash"])
    assert attestation["failureDomain"] == federated_replica_attestation.failure_domain_from_metadata(
        _metadata(region="cn-south-1")
    )

    verified = _verify(fixture)
    assert verified == attestation
    sender_transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert sender_transfer is not None
    assert sender_transfer["role"] == federation_transfer_journal.ROLE_SENDER
    assert sender_transfer["state"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    assert sender_transfer["stateDetails"]["attestationDigest"] == federated_replica_attestation.attestation_digest(attestation)
    assert sender_transfer["stateDetails"]["remoteReceiptDigest"] == attestation["remoteReceiptDigest"]
    assert sender_transfer["stateDetails"]["remoteCommitDigest"] == attestation["remoteCommitDigest"]
    accepted = fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"])
    assert accepted is not None and accepted["attestation"] == attestation

    assert _verify(fixture) == attestation
    assert len(fixture["senderJournal"].list_transfer_events(fixture["transferId"])) == 6


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("sourceFleetId", "fleet-c", "FEDERATION_REPLICA_ATTESTATION_SOURCE_FLEET_MISMATCH"),
        ("destinationFleetId", "fleet-c", "FEDERATION_REPLICA_ATTESTATION_DESTINATION_FLEET_MISMATCH"),
        ("backupId", "different-backup", "FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_MISMATCH"),
        ("objectSetDigest", "sha256:" + ("f" * 64), "FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_MISMATCH"),
        ("remoteTargetId", "different-target", "FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_MISMATCH"),
        ("failureDomain", "federation-peer-domain:sha256:" + ("f" * 64), "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_MISMATCH"),
    ),
)
def test_validly_signed_replica_attestation_binding_changes_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    code: str,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    forged = _resign(fixture, {field: value})

    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as rejected:
        _verify(fixture, attestation=forged)

    assert rejected.value.code == code
    assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING
    assert fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"]) is None


def test_tampered_signature_and_remote_documents_never_record_remote_commit(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for corruption, expected in (
        ("signature", "FEDERATION_DOCUMENT_SIGNATURE_INVALID"),
        ("receipt", "FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_MISMATCH"),
        ("commit", "FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_MISMATCH"),
        ("noncanonical-receipt", "FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID"),
    ):
        fixture = _attestation_fixture(tmp_settings / corruption, monkeypatch)
        attestation = copy.deepcopy(fixture["attestation"])
        if corruption == "signature":
            attestation["signature"] = ("A" if attestation["signature"][0] != "A" else "B") + attestation["signature"][1:]
        elif corruption == "receipt":
            receipt = json.loads(fixture["receiptBytes"])
            receipt["targetId"] = "tampered-target"
            fixture["receiptBytes"] = _storage_bytes(receipt)
        elif corruption == "commit":
            commit = json.loads(fixture["commitBytes"])
            commit["targetGeneration"] = int(commit["targetGeneration"]) + 1
            fixture["commitBytes"] = _storage_bytes(commit)
        else:
            fixture["receiptBytes"] = json.dumps(json.loads(fixture["receiptBytes"]), sort_keys=True).encode("utf-8")

        with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as rejected:
            _verify(fixture, attestation=attestation)

        assert rejected.value.code == expected
        assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING


def test_replica_attestation_time_window_sequence_and_current_trust_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings / "time", monkeypatch)
    expired = _resign(
        fixture,
        {
            "signedAt": (NOW - timedelta(minutes=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "expiresAt": (NOW - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    )
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as expired_error:
        _verify(fixture, attestation=expired)
    assert expired_error.value.code == "FEDERATION_REPLICA_ATTESTATION_EXPIRED"

    future = _resign(
        fixture,
        {
            "signedAt": (NOW + timedelta(minutes=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "expiresAt": (NOW + timedelta(minutes=3)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    )
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as future_error:
        _verify(fixture, attestation=future)
    assert future_error.value.code == "FEDERATION_REPLICA_ATTESTATION_FROM_FUTURE"

    fixture["registryA"].revoke_online_signer(
        "fleet-b",
        str(fixture["certificateB"]["signerKeyId"]),
        actor="operator-a",
        reason="incident",
        revoked_at=NOW + timedelta(seconds=11),
    )
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as revoked:
        _verify(fixture)
    assert revoked.value.code == "FEDERATION_SIGNER_REVOKED"


def test_replica_attestation_sequence_registry_is_idempotent_and_monotonic(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch, sequence=2)
    _verify(fixture)
    digest = federated_replica_attestation.attestation_digest(fixture["attestation"])
    assert fixture["registryA"].record_replica_attestation(
        "fleet-b",
        signer_key_id=str(fixture["attestation"]["signerKeyId"]),
        transfer_id=fixture["transferId"],
        sequence=2,
        attestation_digest=digest,
        attestation=fixture["attestation"],
        accepted_at=NOW + timedelta(seconds=13),
    )["attestationDigest"] == digest

    unsigned_tamper = {**fixture["attestation"], "remoteTargetId": "unsigned-tamper"}
    with pytest.raises(federation_peer_trust.FederationTrustError) as signature_bypass:
        fixture["registryA"].record_replica_attestation(
            "fleet-b",
            signer_key_id=str(fixture["attestation"]["signerKeyId"]),
            transfer_id=fixture["transferId"],
            sequence=2,
            attestation_digest=federated_replica_attestation.attestation_digest(unsigned_tamper),
            attestation=unsigned_tamper,
            accepted_at=NOW + timedelta(seconds=13),
        )
    assert signature_bypass.value.code == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"

    conflicting = _resign(fixture, {"remoteTargetId": "other-target"})
    with pytest.raises(federation_peer_trust.FederationTrustError) as identity_conflict:
        fixture["registryA"].record_replica_attestation(
            "fleet-b",
            signer_key_id=str(fixture["attestation"]["signerKeyId"]),
            transfer_id=fixture["transferId"],
            sequence=2,
            attestation_digest=federated_replica_attestation.attestation_digest(conflicting),
            attestation=conflicting,
            accepted_at=NOW + timedelta(seconds=13),
        )
    assert identity_conflict.value.code == "FEDERATION_REPLICA_ATTESTATION_IDENTITY_CONFLICT"

    for sequence, transfer_id, expected in (
        (2, "sha256:" + ("2" * 64), "FEDERATION_REPLICA_ATTESTATION_SEQUENCE_CONFLICT"),
        (1, "sha256:" + ("3" * 64), "FEDERATION_REPLICA_ATTESTATION_SEQUENCE_REPLAY"),
    ):
        replayed = _resign(fixture, {"transferId": transfer_id, "sequence": sequence})
        with pytest.raises(federation_peer_trust.FederationTrustError) as sequence_error:
            fixture["registryA"].record_replica_attestation(
                "fleet-b",
                signer_key_id=str(fixture["attestation"]["signerKeyId"]),
                transfer_id=transfer_id,
                sequence=sequence,
                attestation_digest=federated_replica_attestation.attestation_digest(replayed),
                attestation=replayed,
                accepted_at=NOW + timedelta(seconds=13),
            )
        assert sequence_error.value.code == expected


def test_replica_attestation_primitive_validators_reject_noncanonical_and_unbounded_inputs() -> None:
    invalid_calls = (
        (lambda: federated_replica_attestation._normalize({1: "bad"}), "FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_replica_attestation._canonical_json({1: "bad"}), "FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_replica_attestation._utc_iso(datetime(2026, 9, 1)), "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"),
        (lambda: federated_replica_attestation._parse_timestamp(None), "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"),
        (lambda: federated_replica_attestation._parse_timestamp("not-time"), "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"),
        (lambda: federated_replica_attestation._parse_timestamp("2026-09-01T08:00:00"), "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"),
        (lambda: federated_replica_attestation._parse_timestamp("2026-09-01T08:00:00+00:00"), "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"),
        (lambda: federated_replica_attestation._fleet_id("Fleet A"), "FEDERATION_REPLICA_ATTESTATION_FLEET_ID_INVALID"),
        (lambda: federated_replica_attestation._control_id("bad/id", code="BAD_CONTROL"), "BAD_CONTROL"),
        (lambda: federated_replica_attestation._typed_digest("bad", code="BAD_TYPED"), "BAD_TYPED"),
        (lambda: federated_replica_attestation._plain_digest("bad", code="BAD_PLAIN"), "BAD_PLAIN"),
        (lambda: federated_replica_attestation._sequence(True), "FEDERATION_REPLICA_ATTESTATION_SEQUENCE_INVALID"),
        (lambda: federated_replica_attestation._sequence(0), "FEDERATION_REPLICA_ATTESTATION_SEQUENCE_INVALID"),
        (lambda: federated_replica_attestation.failure_domain_from_metadata({}), "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID"),
        (
            lambda: federated_replica_attestation.failure_domain_from_metadata({**_metadata(region="x"), "region": ""}),
            "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID",
        ),
        (
            lambda: federated_replica_attestation._validate_window(NOW, NOW),
            "FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID",
        ),
        (
            lambda: federated_replica_attestation._validate_window(NOW, NOW + timedelta(seconds=301)),
            "FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID",
        ),
        (
            lambda: federated_replica_attestation._parse_storage_document(
                b"",
                maximum_bytes=10,
                invalid_code="BAD_DOCUMENT",
                encoding_code="BAD_ENCODING",
            ),
            "BAD_DOCUMENT",
        ),
        (
            lambda: federated_replica_attestation._parse_storage_document(
                b"\xff",
                maximum_bytes=10,
                invalid_code="BAD_DOCUMENT",
                encoding_code="BAD_ENCODING",
            ),
            "BAD_DOCUMENT",
        ),
        (
            lambda: federated_replica_attestation._parse_storage_document(
                b"[]\n",
                maximum_bytes=10,
                invalid_code="BAD_DOCUMENT",
                encoding_code="BAD_ENCODING",
            ),
            "BAD_DOCUMENT",
        ),
        (
            lambda: federated_replica_attestation._parse_storage_document(
                b"{}",
                maximum_bytes=10,
                invalid_code="BAD_DOCUMENT",
                encoding_code="BAD_ENCODING",
            ),
            "BAD_ENCODING",
        ),
    )
    for call, code in invalid_calls:
        _assert_attestation_error(call, code)


def test_replica_attestation_independent_storage_semantics_reject_forged_source_and_remote_documents(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert transfer is not None

    source_variants: list[tuple[dict[str, Any], str]] = []
    missing = copy.deepcopy(fixture["receipt"])
    missing.pop("createdAt")
    source_variants.append((missing, "FEDERATION_REPLICA_SOURCE_RECEIPT_V4_INVALID"))
    bad_object_digest = copy.deepcopy(fixture["receipt"])
    bad_object_digest["objectSetDigest"] = "bad"
    source_variants.append((bad_object_digest, "FEDERATION_REPLICA_SOURCE_OBJECT_SET_DIGEST_INVALID"))
    bad_control_digest = copy.deepcopy(fixture["receipt"])
    bad_control_digest["controlObjectDigest"] = "bad"
    source_variants.append((bad_control_digest, "FEDERATION_REPLICA_SOURCE_CONTROL_OBJECT_DIGEST_INVALID"))
    bad_binding = copy.deepcopy(fixture["receipt"])
    bad_binding["creationVerified"] = False
    source_variants.append((bad_binding, "FEDERATION_REPLICA_SOURCE_RECEIPT_BINDING_INVALID"))
    bad_inventory = copy.deepcopy(fixture["receipt"])
    bad_inventory["size"] = int(bad_inventory["size"]) + 1
    source_variants.append((bad_inventory, "FEDERATION_REPLICA_SOURCE_OBJECT_INVENTORY_INVALID"))
    missing_control = copy.deepcopy(fixture["receipt"])
    missing_control["controlObjectDigest"] = "f" * 64
    source_variants.append((missing_control, "FEDERATION_REPLICA_SOURCE_OBJECT_INVENTORY_INVALID"))
    for source, code in source_variants:
        _assert_attestation_error(
            lambda source=source: federated_replica_attestation._source_receipt_semantics(source, transfer),
            code,
        )

    receipt_variants: list[tuple[dict[str, Any], str]] = []
    receipt_missing = copy.deepcopy(fixture["committed"].receipt)
    receipt_missing.pop("pinned")
    receipt_variants.append((receipt_missing, "FEDERATION_REPLICA_REMOTE_RECEIPT_V4_INVALID"))
    receipt_rebound = copy.deepcopy(fixture["committed"].receipt)
    receipt_rebound["snapshotKind"] = "forged"
    receipt_variants.append((receipt_rebound, "FEDERATION_REPLICA_REMOTE_RECEIPT_BINDING_INVALID"))
    receipt_future = copy.deepcopy(fixture["committed"].receipt)
    receipt_future["createdAt"] = (NOW + timedelta(seconds=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
    receipt_variants.append((receipt_future, "FEDERATION_REPLICA_REMOTE_RECEIPT_TIMESTAMP_INVALID"))
    for receipt, code in receipt_variants:
        receipt_bytes = _storage_bytes(receipt)
        bound_attestation = {
            **fixture["attestation"],
            "remoteReceiptDigest": "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
        }
        _assert_attestation_error(
            lambda receipt_bytes=receipt_bytes, bound_attestation=bound_attestation: federated_replica_attestation._validate_remote_documents(
                source_receipt=fixture["receipt"],
                remote_receipt_bytes=receipt_bytes,
                remote_commit_bytes=fixture["commitBytes"],
                transfer=transfer,
                attestation=bound_attestation,
            ),
            code,
        )

    for mutation, code in (("shape", "FEDERATION_REPLICA_REMOTE_COMMIT_V4_INVALID"), ("binding", "FEDERATION_REPLICA_REMOTE_COMMIT_BINDING_INVALID")):
        commit = copy.deepcopy(fixture["committed"].commit)
        if mutation == "shape":
            commit.pop("previousCommitHash")
        else:
            commit["targetGeneration"] = 0
        commit_bytes = _storage_bytes(commit)
        bound_attestation = {
            **fixture["attestation"],
            "remoteCommitDigest": "sha256:" + hashlib.sha256(commit_bytes).hexdigest(),
        }
        _assert_attestation_error(
            lambda commit_bytes=commit_bytes, bound_attestation=bound_attestation: federated_replica_attestation._validate_remote_documents(
                source_receipt=fixture["receipt"],
                remote_receipt_bytes=fixture["receiptBytes"],
                remote_commit_bytes=commit_bytes,
                transfer=transfer,
                attestation=bound_attestation,
            ),
            code,
        )


def test_replica_attestation_semantic_validator_rejects_malformed_signed_claims(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    peer = fixture["registryA"].get_peer("fleet-b")
    assert transfer is not None and peer is not None
    metadata = peer["pinnedMetadata"]
    base = fixture["attestation"]

    cases: list[tuple[dict[str, Any], str, dict[str, Any], Any]] = [
        ({"sourceFleetId": "Fleet-Bad"}, "FEDERATION_REPLICA_ATTESTATION_FLEET_ID_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"backupId": "bad/id"}, "FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"objectSetDigest": "bad"}, "FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"remoteTargetId": "bad/id"}, "FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"remoteReceiptDigest": "bad"}, "FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"remoteCommitDigest": "bad"}, "FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"failureDomain": "Mars"}, "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"sequence": 0}, "FEDERATION_REPLICA_ATTESTATION_SEQUENCE_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"signedAt": "bad"}, "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID", transfer, NOW + timedelta(seconds=12)),
        ({"expiresAt": base["signedAt"]}, "FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID", transfer, NOW + timedelta(seconds=12)),
        (
            {"committedAt": (NOW + timedelta(seconds=20)).isoformat(timespec="seconds").replace("+00:00", "Z")},
            "FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID",
            transfer,
            NOW + timedelta(seconds=12),
        ),
    ]
    certificate_expires = federated_replica_attestation._parse_timestamp(base["signerCertificate"]["expiresAt"])
    near_expiry = certificate_expires - timedelta(seconds=100)
    cases.append(
        (
            {
                "signedAt": near_expiry.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "expiresAt": (certificate_expires + timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            "FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID",
            transfer,
            near_expiry,
        )
    )
    for changes, code, bound_transfer, current in cases:
        claim = {**base, **changes}
        _assert_attestation_error(
            lambda claim=claim, bound_transfer=bound_transfer, current=current: federated_replica_attestation._attestation_semantics(
                claim,
                transfer=bound_transfer,
                pinned_metadata=metadata,
                now=current,
                max_future_skew_seconds=30,
            ),
            code,
        )

    unrelated_transfer = {**transfer, "transferId": "sha256:" + ("f" * 64)}
    unrelated_claim = {**base, "transferId": unrelated_transfer["transferId"]}
    _assert_attestation_error(
        lambda: federated_replica_attestation._attestation_semantics(
            unrelated_claim,
            transfer=unrelated_transfer,
            pinned_metadata=metadata,
            now=NOW + timedelta(seconds=12),
            max_future_skew_seconds=30,
        ),
        "FEDERATION_TRANSFER_ID_INVALID",
    )


def test_replica_attestation_verifier_shape_identity_atomic_failure_and_later_state_replay(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    invalid_attestation: Any = []

    _assert_attestation_error(
        lambda: federated_replica_attestation._require_local_signer(
            fixture["signerB"],
            fixture["identityA"],
            signed_at=NOW + timedelta(seconds=11),
            expires_at=NOW + timedelta(seconds=71),
        ),
        "FEDERATION_REPLICA_ATTESTATION_LOCAL_SIGNER_MISMATCH",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(federation_identity, "validate_online_signer_certificate", lambda *_args, **_kwargs: ["CERT_BAD"])
        _assert_attestation_error(
            lambda: federated_replica_attestation._require_local_signer(
                fixture["signerB"],
                fixture["identityB"],
                signed_at=NOW + timedelta(seconds=11),
                expires_at=NOW + timedelta(seconds=71),
            ),
            "CERT_BAD",
        )
    certificate_expiry = federated_replica_attestation._parse_timestamp(fixture["certificateB"]["expiresAt"])
    _assert_attestation_error(
        lambda: federated_replica_attestation._require_local_signer(
            fixture["signerB"],
            fixture["identityB"],
            signed_at=NOW + timedelta(seconds=11),
            expires_at=certificate_expiry + timedelta(seconds=1),
        ),
        "FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID",
    )

    no_certificate = {**fixture["attestation"], "signerCertificate": None}
    _assert_attestation_error(
        lambda: federated_replica_attestation._verify_signature_and_trust(
            no_certificate,
            peer_registry=fixture["registryA"],
            destination_fleet_id="fleet-b",
            now=NOW + timedelta(seconds=12),
        ),
        "FEDERATION_REPLICA_ATTESTATION_SIGNER_CERTIFICATE_INVALID",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "get_peer", lambda _peer: None)
        _assert_attestation_error(
            lambda: federated_replica_attestation._verify_signature_and_trust(
                fixture["attestation"],
                peer_registry=fixture["registryA"],
                destination_fleet_id="fleet-b",
                now=NOW + timedelta(seconds=12),
            ),
            "FEDERATION_PEER_NOT_PINNED",
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "get_peer", lambda _peer: {"fleetIdentity": None})
        _assert_attestation_error(
            lambda: federated_replica_attestation._verify_signature_and_trust(
                fixture["attestation"],
                peer_registry=fixture["registryA"],
                destination_fleet_id="fleet-b",
                now=NOW + timedelta(seconds=12),
            ),
            "FEDERATION_PEER_IDENTITY_INVALID",
        )

    _assert_attestation_error(
        lambda: federated_replica_attestation.verify_and_record_replica_attestation(
            invalid_attestation,
            peer_registry=fixture["registryA"],
            sender_journal=fixture["senderJournal"],
            source_receipt=fixture["receipt"],
            remote_receipt_bytes=fixture["receiptBytes"],
            remote_commit_bytes=fixture["commitBytes"],
            now=NOW + timedelta(seconds=12),
        ),
        "FEDERATION_REPLICA_ATTESTATION_INVALID",
    )
    oversized = copy.deepcopy(fixture["attestation"])
    oversized["signerCertificate"]["padding"] = "x" * federated_replica_attestation.MAX_REPLICA_ATTESTATION_BYTES
    _assert_attestation_error(
        lambda: _verify(fixture, attestation=oversized),
        "FEDERATION_REPLICA_ATTESTATION_TOO_LARGE",
    )
    missing = copy.deepcopy(fixture["attestation"])
    missing.pop("signature")
    _assert_attestation_error(lambda: _verify(fixture, attestation=missing), "FEDERATION_REPLICA_ATTESTATION_FIELDS_INVALID")
    unknown = {**fixture["attestation"], "transferId": "sha256:" + ("f" * 64)}
    _assert_attestation_error(lambda: _verify(fixture, attestation=unknown), "FEDERATION_TRANSFER_NOT_FOUND")
    sender_transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    sender_peer = fixture["registryA"].get_peer("fleet-b")
    assert sender_transfer is not None and sender_peer is not None
    mismatched_transfer = {**sender_transfer, "transferId": "sha256:" + ("f" * 64)}
    _assert_attestation_error(
        lambda: federated_replica_attestation._attestation_semantics(
            fixture["attestation"],
            transfer=mismatched_transfer,
            pinned_metadata=sender_peer["pinnedMetadata"],
            now=NOW + timedelta(seconds=12),
            max_future_skew_seconds=30,
        ),
        "FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_MISMATCH",
    )
    _assert_attestation_error(
        lambda: federated_replica_attestation.verify_and_record_replica_attestation(
            fixture["attestation"],
            peer_registry=fixture["registry"],
            sender_journal=fixture["journal"],
            source_receipt=fixture["receipt"],
            remote_receipt_bytes=fixture["receiptBytes"],
            remote_commit_bytes=fixture["commitBytes"],
            now=NOW + timedelta(seconds=12),
        ),
        "FEDERATION_REPLICA_ATTESTATION_SENDER_IDENTITY_MISMATCH",
    )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["registryA"],
            "record_replica_attestation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(federation_peer_trust.FederationTrustError("RECORD_FAILED")),
        )
        _assert_attestation_error(lambda: _verify(fixture), "RECORD_FAILED")
    unchanged = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert unchanged is not None and unchanged["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING

    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["registryA"],
            "require_active_peer",
            lambda _peer: (_ for _ in ()).throw(federation_peer_trust.FederationTrustError("PEER_CHANGED")),
        )
        _assert_attestation_error(lambda: _verify(fixture), "PEER_CHANGED")
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "require_active_peer", lambda _peer: {"pinnedMetadata": None})
        _assert_attestation_error(lambda: _verify(fixture), "FEDERATION_PEER_METADATA_INVALID")

    assert _verify(fixture) == fixture["attestation"]
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    transfer = fixture["senderJournal"].advance_transfer(
        fixture["transferId"],
        expected_revision=int(transfer["revision"]),
        next_state=federation_transfer_journal.STATE_LOCAL_RECORDED,
        details={"recorded": True},
        now=NOW + timedelta(seconds=13),
    )
    assert _verify(fixture) == fixture["attestation"]
    later = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert later is not None and later["state"] == federation_transfer_journal.STATE_LOCAL_RECORDED

    proposed = {**transfer, "state": federation_transfer_journal.STATE_PROPOSED}
    _assert_attestation_error(
        lambda: federated_replica_attestation._require_sender_state(
            fixture["senderJournal"],
            proposed,
            federated_replica_attestation._sender_remote_commit_details(fixture["attestation"]),
        ),
        "FEDERATION_REPLICA_ATTESTATION_SENDER_STATE_INVALID",
    )
    conflicting = {**transfer, "state": federation_transfer_journal.STATE_REMOTE_COMMITTED}
    _assert_attestation_error(
        lambda: federated_replica_attestation._require_sender_state(
            fixture["senderJournal"],
            conflicting,
            {"different": True},
        ),
        "FEDERATION_REPLICA_ATTESTATION_LOCAL_RECORD_CONFLICT",
    )


def test_replica_attestation_requires_provider_commit_and_exact_pinned_metadata(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _store = _memory_target()
    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)

    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as uncommitted:
        federated_replica_attestation.issue_replica_attestation(
            signer=fixture["signerB"],
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            remote_target_id=TARGET_ID,
            failure_domain_metadata=_metadata(region="cn-south-1"),
            sequence=1,
            signed_at=NOW + timedelta(seconds=11),
            expires_at=NOW + timedelta(seconds=71),
        )
    assert uncommitted.value.code == "FEDERATION_REPLICA_NOT_REMOTE_COMMITTED"

    committed = _commit(fixture, monkeypatch, target)
    sender_journal = _advance_sender_to_remote_verifying(fixture, tmp_settings)
    attestation = federated_replica_attestation.issue_replica_attestation(
        signer=fixture["signerB"],
        receiver=fixture["receiver"],
        transfer_id=fixture["transferId"],
        remote_target_id=TARGET_ID,
        failure_domain_metadata=_metadata(region="wrong-region"),
        sequence=1,
        signed_at=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=71),
    )
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as metadata_mismatch:
        federated_replica_attestation.verify_and_record_replica_attestation(
            attestation,
            peer_registry=fixture["registryA"],
            sender_journal=sender_journal,
            source_receipt=fixture["receipt"],
            remote_receipt_bytes=_storage_bytes(committed.receipt),
            remote_commit_bytes=_storage_bytes(committed.commit),
            now=NOW + timedelta(seconds=12),
        )
    assert metadata_mismatch.value.code == "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_MISMATCH"
    sender_transfer = sender_journal.get_transfer(fixture["transferId"])
    assert sender_transfer is not None
    assert sender_transfer["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING


def test_replica_attestation_rejects_non_provider_target_even_if_store_exists(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target = backup_publish.ResolvedTarget(
        target_id=TARGET_ID,
        root=tmp_settings / "filesystem",
        managed=False,
        kind="filesystem",
        store=backup_target_store.MemoryTargetStore(),
    )
    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)
    with pytest.raises(federated_replica_attestation.FederatedReplicaAttestationError) as rejected:
        federated_replica_attestation.issue_replica_attestation(
            signer=fixture["signerB"],
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            remote_target_id=TARGET_ID,
            failure_domain_metadata=_metadata(region="cn-south-1"),
            sequence=1,
            signed_at=NOW + timedelta(seconds=11),
            expires_at=NOW + timedelta(seconds=71),
        )
    assert rejected.value.code in {
        "FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED",
        "FEDERATION_REPLICA_NOT_REMOTE_COMMITTED",
    }
