from __future__ import annotations

import copy
import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from deepseek_infra.infra.workspace import evidence_proof, federated_durability, federated_replica_proof
from tests.test_backup_480_federated_durability import _ledger, _policy, _record, _verified_fixture
from tests.test_backup_480_federated_replica_receiver import NOW


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _proof_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    copy_record = _record(fixture, ledger)
    policy = _policy()
    status = federated_durability.evaluate_federated_durability(
        policy=policy,
        backup_id=str(fixture["receipt"]["backupId"]),
        object_set_digest=fixture["federationDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=14),
    )
    receiver_transfer = fixture["journal"].get_transfer(fixture["transferId"])
    sender_transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    declaration = fixture["receiver"].get_declaration(fixture["transferId"])
    peer = fixture["registryA"].get_peer("fleet-b")
    accepted = fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"])
    assert receiver_transfer is not None and sender_transfer is not None and declaration is not None
    assert peer is not None and accepted is not None

    state_digest = _digest(
        {
            "receiverTransfer": receiver_transfer,
            "senderTransfer": sender_transfer,
            "acceptedReplica": accepted,
            "copyRecord": copy_record,
        }
    )
    tampered_attestation = copy.deepcopy(fixture["attestation"])
    tampered_attestation["signature"] = ("A" if tampered_attestation["signature"][0] != "A" else "B") + tampered_attestation[
        "signature"
    ][1:]
    failures = [
        {
            "claim": "expiredIngressGrantCannotWrite",
            "code": "FEDERATION_INGRESS_GRANT_EXPIRED",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"grant": fixture["grant"], "attemptedAt": "2026-09-01T08:01:01Z"},
        },
        {
            "claim": "ingressGrantCannotEscapeObjectPrefix",
            "code": "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"grant": fixture["grant"], "objectKey": "outside/escape.age", "byteCount": 1},
        },
        {
            "claim": "ingressGrantCannotExceedMaxBytes",
            "code": "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"grant": fixture["grant"], "byteCount": int(fixture["grant"]["maxBytes"]) + 1},
        },
        {
            "claim": "sameTransferIdDifferentDigestFailsClosed",
            "code": "FEDERATION_TRANSFER_IDENTITY_CONFLICT",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"transfer": receiver_transfer, "conflictingObjectSetDigest": "sha256:" + ("f" * 64)},
        },
        {
            "claim": "replayedIngressGrantFailsClosed",
            "code": "FEDERATION_INGRESS_GRANT_NONCE_REPLAY",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"grant": fixture["grant"], "replayedGrantId": fixture["grant"]["grantId"]},
        },
        {
            "claim": "tamperedReplicaAttestationFailsClosed",
            "code": "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
            "preStateDigest": state_digest,
            "postStateDigest": state_digest,
            "input": {"attestation": tampered_attestation},
        },
    ]
    for failure in failures:
        failure["preState"] = copy.deepcopy(
            {
                "receiverTransfer": receiver_transfer,
                "senderTransfer": sender_transfer,
                "acceptedReplica": accepted,
                "copyRecord": copy_record,
            }
        )
        failure["postState"] = copy.deepcopy(failure["preState"])
    proof = federated_replica_proof.build_federated_replica_proof(
        validated_at=NOW + timedelta(seconds=14),
        destination_fleet_identity=fixture["identityB"],
        peer_trust_record=peer,
        ingress_grant=fixture["grant"],
        receiver_transfer=receiver_transfer,
        sender_transfer=sender_transfer,
        object_set_declaration=declaration,
        source_receipt=fixture["receipt"],
        remote_receipt_bytes=fixture["receiptBytes"],
        remote_commit_bytes=fixture["commitBytes"],
        replica_attestation=fixture["attestation"],
        accepted_replica_record=accepted,
        federated_copy_record=copy_record,
        local_durability_before=copy.deepcopy(policy["replication"]),
        local_durability_after=copy.deepcopy(policy["replication"]),
        federated_durability_status=status,
        failure_observations=failures,
        wire_contracts={
            "objectSet": "object-set-v1",
            "receiptVersion": 4,
            "commitVersion": 4,
            "fastCdc": "fastcdc-v3",
            "randomizedAge": True,
        },
    )
    return {"proof": proof}


@pytest.fixture
def replica_proof_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _proof_fixture(tmp_settings, monkeypatch)


def _mutated_errors(proof: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> list[str]:
    candidate = copy.deepcopy(proof)
    mutate(candidate)
    candidate["proofDigest"] = federated_replica_proof.proof_digest(candidate)
    return federated_replica_proof.validate_federated_replica_proof(candidate)


def _failure(proof: dict[str, Any], claim: str) -> dict[str, Any]:
    return next(item for item in proof["failureObservations"] if item["claim"] == claim)


def test_federated_replica_proof_recomputes_grant_storage_attestation_and_durability(
    replica_proof_fixture: dict[str, Any],
) -> None:
    proof = replica_proof_fixture["proof"]
    assert proof["schema"] == "federated-replica-proof-v1"
    assert federated_replica_proof.validate_federated_replica_proof(proof) == []
    for check_name in federated_replica_proof.FEDERATED_REPLICA_PROOF_CHECKS:
        expected_validator = (
            evidence_proof.validate_federated_wire_compatibility_proof
            if check_name in evidence_proof._FEDERATED_REPLICA_WIRE_CHECKS
            else evidence_proof.validate_typed_federated_replica_proof
        )
        assert evidence_proof.VALIDATORS[check_name] is expected_validator
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []


def test_federated_replica_proof_rejects_storage_durability_and_failure_tamper(
    replica_proof_fixture: dict[str, Any],
) -> None:
    proof = replica_proof_fixture["proof"]

    receipt_tamper = copy.deepcopy(proof)
    receipt_tamper["remoteReceiptBytesBase64"] = "bm90LXJlY2VpcHQ="
    receipt_tamper["proofDigest"] = federated_replica_proof.proof_digest(receipt_tamper)
    assert "remote-storage-documents-invalid" in federated_replica_proof.validate_federated_replica_proof(receipt_tamper)

    local_regression = copy.deepcopy(proof)
    local_regression["localDurabilityAfter"]["minCommittedCopies"] = 1
    local_regression["proofDigest"] = federated_replica_proof.proof_digest(local_regression)
    assert "local-durability-regressed" in federated_replica_proof.validate_federated_replica_proof(local_regression)

    self_reported = copy.deepcopy(proof)
    self_reported["failureObservations"][0]["input"] = {}
    self_reported["proofDigest"] = federated_replica_proof.proof_digest(self_reported)
    assert "expired-grant-evidence-invalid" in federated_replica_proof.validate_federated_replica_proof(self_reported)

    digest_tamper = copy.deepcopy(proof)
    digest_tamper["proofDigest"] = "sha256:" + ("f" * 64)
    assert "proof-digest-mismatch" in federated_replica_proof.validate_federated_replica_proof(digest_tamper)


def test_federated_replica_proof_scalar_guards(replica_proof_fixture: dict[str, Any]) -> None:
    proof = replica_proof_fixture["proof"]
    with pytest.raises(ValueError, match="timestamp-invalid"):
        federated_replica_proof._utc_iso(NOW.replace(tzinfo=None))

    for value in (None, "not-a-time", "2026-09-01T08:00:00", "2026-09-01T10:00:00+02:00"):
        errors: list[str] = []
        assert federated_replica_proof._parse_timestamp(value, "field", errors) is None
        assert errors == ["invalid-timestamp:field"]

    dict_errors: list[str] = []
    assert federated_replica_proof._as_dict(None, "field", dict_errors) == {}
    assert dict_errors == ["field-must-be-object"]
    digest_errors: list[str] = []
    assert federated_replica_proof._typed_digest("invalid", "field", digest_errors) == ""
    assert digest_errors == ["invalid-sha256:field"]
    for value, expected in (
        (None, "invalid-base64:field"),
        ("%%%", "invalid-base64:field"),
        ("", "empty-bytes:field"),
    ):
        decode_errors: list[str] = []
        assert federated_replica_proof._decode_document(value, "field", decode_errors) == b""
        assert expected in decode_errors

    assert federated_replica_proof.validate_federated_replica_proof([]) == ["federated-replica-proof-must-be-object"]
    assert federated_replica_proof.validate_federated_replica_proof({"value": float("nan")}) == [
        "federated-replica-proof-canonical-payload-invalid"
    ]
    assert evidence_proof.validate_typed_federated_replica_proof(proof, "unsupported") == [
        "unsupported-federated-replica-proof-check:unsupported"
    ]
    assert evidence_proof.validate_federated_wire_compatibility_proof(proof, "unsupported") == [
        "unsupported-federated-wire-check:unsupported"
    ]
    assert evidence_proof.validate_federated_wire_compatibility_proof(
        {"objectSetVersion": "object-set-v1"}, "objectSetV1WireFormatUnchanged"
    ) == []
    assert evidence_proof.validate_federated_wire_compatibility_proof(
        {"cdcVersion": "fastcdc-v2"}, "fastCdcV3Unchanged"
    ) == ["frozen-wire-value-mismatch:cdcVersion"]
    assert "missing-field:targetId" in evidence_proof.validate_federated_wire_compatibility_proof(
        {"legacy": True}, "receiptV4Unchanged"
    )

    with pytest.raises(ValueError, match="invalid federated replica proof"):
        federated_replica_proof.build_federated_replica_proof(
            validated_at=NOW + timedelta(seconds=14),
            destination_fleet_identity=proof["destinationFleetIdentity"],
            peer_trust_record=proof["peerTrustRecord"],
            ingress_grant=proof["ingressGrant"],
            receiver_transfer=proof["receiverTransfer"],
            sender_transfer=proof["senderTransfer"],
            object_set_declaration=proof["objectSetDeclaration"],
            source_receipt=proof["sourceReceipt"],
            remote_receipt_bytes=base64.b64decode(proof["remoteReceiptBytesBase64"]),
            remote_commit_bytes=base64.b64decode(proof["remoteCommitBytesBase64"]),
            replica_attestation=proof["replicaAttestation"],
            accepted_replica_record=proof["acceptedReplicaRecord"],
            federated_copy_record=proof["federatedCopyRecord"],
            local_durability_before=proof["localDurabilityBefore"],
            local_durability_after=proof["localDurabilityAfter"],
            federated_durability_status=proof["federatedDurabilityStatus"],
            failure_observations=proof["failureObservations"],
            wire_contracts={},
        )


def test_federated_replica_proof_binding_tamper_matrix(replica_proof_fixture: dict[str, Any]) -> None:
    proof = replica_proof_fixture["proof"]
    digest_f = "sha256:" + ("f" * 64)
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("extra", True), "federated-replica-proof-fields-invalid"),
        (lambda item: item.__setitem__("schema", "self-reported-pass"), "federated-replica-proof-schema-invalid"),
        (lambda item: item.__setitem__("agePrivateIdentity", "AGE-SECRET-KEY-1LEAKED"), "federated-replica-proof-contains-secret"),
        (lambda item: item.__setitem__("destinationFleetIdentity", {}), "destination-identity-invalid"),
        (lambda item: item.__setitem__("peerTrustRecord", None), "peer-trust-record-must-be-object"),
        (lambda item: item["peerTrustRecord"].__setitem__("extra", True), "peer-trust-record-fields-invalid"),
        (lambda item: item["peerTrustRecord"].__setitem__("schema", "invalid"), "peer-trust-record-schema-invalid"),
        (lambda item: item["peerTrustRecord"].__setitem__("state", "SUSPENDED"), "peer-trust-not-active"),
        (lambda item: item["peerTrustRecord"].__setitem__("rootFingerprint", digest_f), "peer-trust-root-binding-invalid"),
        (lambda item: item["peerTrustRecord"].__setitem__("pinnedMetadata", None), "pinned-metadata-must-be-object"),
        (lambda item: item["peerTrustRecord"].__setitem__("metadataDigest", digest_f), "pinned-metadata-digest-mismatch"),
        (lambda item: item["peerTrustRecord"].__setitem__("pinnedBy", ""), "peer-trust-operator-pin-missing"),
        (lambda item: item["peerTrustRecord"].__setitem__("revision", 0), "peer-trust-revision-invalid"),
        (lambda item: item["peerTrustRecord"].__setitem__("revokedAt", "2026-09-01T08:00:00Z"), "peer-trust-revoked"),
        (
            lambda item: item["senderTransfer"].__setitem__("sourceFleetId", "fleet-c"),
            "transfer-role-binding-mismatch:sourceFleetId",
        ),
        (lambda item: item["receiverTransfer"].__setitem__("transferId", digest_f), "transfer-identity-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("sourceFleetId", "INVALID"), "transfer-identity-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("objectSetDigest", "invalid"), "invalid-sha256:receiverTransfer.objectSetDigest"),
        (lambda item: item["receiverTransfer"].__setitem__("extra", True), "receiver-transfer-fields-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("schema", "invalid"), "receiver-transfer-schema-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("localFleetId", "fleet-a"), "receiver-transfer-local-fleet-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("identityDigest", digest_f), "receiver-transfer-identity-digest-mismatch"),
        (lambda item: item["receiverTransfer"].__setitem__("statePayloadDigest", digest_f), "receiver-transfer-state-digest-mismatch"),
        (lambda item: item["receiverTransfer"].__setitem__("stateDetails", {"secretKey": "nope"}), "receiver-transfer-record-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("updatedAt", "2026-08-31T08:00:00Z"), "receiver-transfer-time-order-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("revision", 0), "receiver-transfer-revision-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("role", "SENDER"), "receiver-transfer-role-invalid"),
        (lambda item: item["senderTransfer"].__setitem__("role", "RECEIVER"), "sender-transfer-role-invalid"),
        (lambda item: item["receiverTransfer"].__setitem__("state", "TRANSFERRING"), "receiver-transfer-not-committed"),
        (lambda item: item["senderTransfer"].__setitem__("state", "LOCAL_RECORDED"), "sender-transfer-not-succeeded"),
        (lambda item: item["ingressGrant"].__setitem__("signerCertificate", None), "ingress-grant-certificate-must-be-object"),
        (
            lambda item: item["ingressGrant"].__setitem__(
                "signature", ("A" if item["ingressGrant"]["signature"][0] != "A" else "B") + item["ingressGrant"]["signature"][1:]
            ),
            "ingress-grant-signature-invalid",
        ),
        (lambda item: item["ingressGrant"].__setitem__("sourceFleetId", "fleet-c"), "ingress-grant-invalid"),
        (lambda item: item["objectSetDeclaration"].__setitem__("storageProtocol", "object-set-v2"), "object-set-declaration-invalid"),
        (lambda item: item["replicaAttestation"].__setitem__("signerCertificate", None), "replica-attestation-certificate-must-be-object"),
        (
            lambda item: item["replicaAttestation"].__setitem__(
                "signature",
                ("A" if item["replicaAttestation"]["signature"][0] != "A" else "B") + item["replicaAttestation"]["signature"][1:],
            ),
            "replica-attestation-invalid",
        ),
        (lambda item: item["replicaAttestation"].__setitem__("failureDomain", "Mars"), "replica-attestation-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("transferId", digest_f), "accepted-replica-record-binding-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("extra", True), "accepted-replica-record-fields-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("revision", 0), "accepted-replica-record-revision-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("acceptedAt", "2026-09-01T08:00:15Z"), "accepted-replica-record-time-order-invalid"),
        (lambda item: item["federatedCopyRecord"].__setitem__("extra", True), "federated-copy-record-fields-invalid"),
        (lambda item: item["federatedCopyRecord"].__setitem__("recordDigest", digest_f), "federated-copy-record-digest-mismatch"),
        (lambda item: item["federatedCopyRecord"].__setitem__("recordedAt", "2026-09-01T08:00:15Z"), "federated-copy-record-time-order-invalid"),
        (lambda item: item["federatedCopyRecord"].__setitem__("revision", 0), "federated-copy-record-revision-invalid"),
        (lambda item: item["federatedCopyRecord"].__setitem__("backupId", "backup-conflict"), "federated-copy-transfer-binding-mismatch:backupId"),
        (lambda item: item["federatedCopyRecord"].__setitem__("localDurabilityCredit", True), "federated-copy-semantic-binding-invalid"),
        (lambda item: item["localDurabilityBefore"].__setitem__("minCommittedCopies", False), "local-durability-objective-invalid:minCommittedCopies"),
        (lambda item: item["federatedDurabilityStatus"].__setitem__("localDurabilityCredit", 1), "federated-durability-status-invalid"),
        (lambda item: item["wireContracts"].__setitem__("commitVersion", 5), "frozen-wire-contract-mismatch"),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert any(error == expected or error.startswith(expected + ":") for error in errors), (expected, errors)


def test_federated_replica_proof_failure_observation_tamper_matrix(replica_proof_fixture: dict[str, Any]) -> None:
    proof = replica_proof_fixture["proof"]
    digest_f = "sha256:" + ("f" * 64)
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("failureObservations", None), "failure-observations-must-be-list"),
        (lambda item: item["failureObservations"].pop(), "failure-observation-inventory-mismatch"),
        (lambda item: item["failureObservations"][0].__setitem__("extra", True), "failure-fields-invalid:expiredIngressGrantCannotWrite"),
        (lambda item: item["failureObservations"][0].__setitem__("code", "SELF_REPORTED_PASS"), "failure-code-mismatch:expiredIngressGrantCannotWrite"),
        (lambda item: item["failureObservations"][0].__setitem__("preStateDigest", "invalid"), "invalid-sha256:expiredIngressGrantCannotWrite.preStateDigest"),
        (lambda item: item["failureObservations"][0].__setitem__("postStateDigest", digest_f), "failure-mutated-state:expiredIngressGrantCannotWrite"),
        (
            lambda item: item["failureObservations"][0]["preState"].__setitem__("unexpectedMutation", True),
            "failure-pre-state-digest-mismatch:expiredIngressGrantCannotWrite",
        ),
        (lambda item: _failure(item, "expiredIngressGrantCannotWrite").__setitem__("input", {}), "expired-grant-evidence-invalid"),
        (
            lambda item: _failure(item, "ingressGrantCannotEscapeObjectPrefix")["input"].__setitem__(
                "objectKey", item["ingressGrant"]["allowedObjectPrefix"] + "inside.age"
            ),
            "prefix-escape-evidence-invalid",
        ),
        (lambda item: _failure(item, "ingressGrantCannotExceedMaxBytes")["input"].__setitem__("byteCount", 1), "max-bytes-evidence-invalid"),
        (
            lambda item: _failure(item, "sameTransferIdDifferentDigestFailsClosed")["input"].__setitem__(
                "conflictingObjectSetDigest", item["receiverTransfer"]["objectSetDigest"]
            ),
            "transfer-conflict-evidence-invalid",
        ),
        (lambda item: _failure(item, "replayedIngressGrantFailsClosed")["input"].__setitem__("replayedGrantId", "grant-wrong"), "grant-replay-evidence-invalid"),
        (lambda item: _failure(item, "tamperedReplicaAttestationFailsClosed").__setitem__("input", {}), "tampered-attestation-evidence-invalid"),
        (
            lambda item: _failure(item, "tamperedReplicaAttestationFailsClosed")["input"].__setitem__(
                "attestation", copy.deepcopy(item["replicaAttestation"])
            ),
            "tampered-attestation-evidence-invalid",
        ),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert expected in errors, (expected, errors)
