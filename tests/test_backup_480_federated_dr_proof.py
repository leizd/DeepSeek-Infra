from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from deepseek_infra.infra.workspace import evidence_proof, federated_dr_proof, federation_custody_capability
from tests.test_backup_480_federated_dr_drill import _issue, _verify_dr
from tests.test_backup_480_federated_replica_receiver import NOW


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _proof_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fixture, dr_attestation, _calls = _issue(tmp_settings, monkeypatch)
    _verify_dr(fixture, dr_attestation)
    sender_transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    replica_record = fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"])
    dr_record = fixture["registryA"].get_dr_attestation("fleet-b", str(dr_attestation["restoreId"]))
    peer = fixture["registryA"].get_peer("fleet-b")
    assert sender_transfer is not None and replica_record is not None and dr_record is not None and peer is not None

    state = {
        "senderTransfer": sender_transfer,
        "acceptedReplica": replica_record,
        "acceptedDrDrill": dr_record,
    }
    state_digest = _digest(state)
    recovery = fixture["custodyCapability"]
    cold = copy.deepcopy(recovery)
    cold.update(
        {
            "mode": federation_custody_capability.COLD_CUSTODY,
            "recoveryIdentityPreprovisioned": False,
            "ageRecipient": None,
            "ageRecipientDigest": None,
        }
    )
    missing_identity = copy.deepcopy(recovery)
    missing_identity.update(
        {
            "recoveryIdentityPreprovisioned": False,
            "ageRecipient": None,
            "ageRecipientDigest": None,
        }
    )
    cleanup_failed = copy.deepcopy(fixture["productionRestoreResult"])
    cleanup_failed["cleanupCompleted"] = False
    failures = [
        {
            "claim": "coldCustodyCannotClaimRecoveryReady",
            "code": "FEDERATION_PEER_COLD_CUSTODY_ONLY",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"capability": cold},
        },
        {
            "claim": "recoveryCapablePeerRequiresPreprovisionedAgeIdentity",
            "code": "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"capability": missing_identity},
        },
        {
            "claim": "federatedDrProofRequiresCleanupSuccess",
            "code": "FEDERATED_DR_CLEANUP_INCOMPLETE",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"productionRestoreResult": cleanup_failed},
        },
    ]
    proof = federated_dr_proof.build_federated_dr_proof(
        validated_at=NOW + timedelta(seconds=17),
        source_fleet_identity=fixture["identityA"],
        destination_fleet_identity=fixture["identityB"],
        peer_trust_record=peer,
        sender_transfer=sender_transfer,
        accepted_replica_record=replica_record,
        dr_attestation=dr_attestation,
        accepted_dr_record=dr_record,
        recovery_capability=recovery,
        production_restore_result=fixture["productionRestoreResult"],
        failure_observations=failures,
    )
    return {"proof": proof}


@pytest.fixture
def dr_proof_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _proof_fixture(tmp_settings, monkeypatch)


def _mutated_errors(proof: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> list[str]:
    candidate = copy.deepcopy(proof)
    mutate(candidate)
    candidate["proofDigest"] = federated_dr_proof.proof_digest(candidate)
    return federated_dr_proof.validate_federated_dr_proof(candidate)


def _failure(proof: dict[str, Any], claim: str) -> dict[str, Any]:
    return next(item for item in proof["failureObservations"] if item["claim"] == claim)


def test_federated_dr_proof_recomputes_restore_signature_replica_and_cleanup(
    dr_proof_fixture: dict[str, Any],
) -> None:
    proof = dr_proof_fixture["proof"]

    assert proof["schema"] == "federated-dr-proof-v1"
    assert federated_dr_proof.validate_federated_dr_proof(proof) == []
    for check_name in federated_dr_proof.FEDERATED_DR_PROOF_CHECKS:
        assert evidence_proof.VALIDATORS[check_name] is evidence_proof.validate_typed_federated_dr_proof
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []


def test_federated_dr_proof_rejects_restore_capability_record_and_failure_tamper(
    dr_proof_fixture: dict[str, Any],
) -> None:
    proof = dr_proof_fixture["proof"]

    cleanup = copy.deepcopy(proof)
    cleanup["productionRestoreResult"]["cleanupCompleted"] = False
    cleanup["proofDigest"] = federated_dr_proof.proof_digest(cleanup)
    assert "production-restore-evidence-invalid:FEDERATED_DR_CLEANUP_INCOMPLETE" in federated_dr_proof.validate_federated_dr_proof(cleanup)

    attestation = copy.deepcopy(proof)
    attestation["drAttestation"]["remoteCommitDigest"] = "sha256:" + ("f" * 64)
    attestation["proofDigest"] = federated_dr_proof.proof_digest(attestation)
    assert "dr-attestation-signature-invalid" in federated_dr_proof.validate_federated_dr_proof(attestation)

    capability = copy.deepcopy(proof)
    capability["recoveryCapability"]["recoveryIdentityPreprovisioned"] = False
    capability["proofDigest"] = federated_dr_proof.proof_digest(capability)
    assert "recovery-capability-invalid" in federated_dr_proof.validate_federated_dr_proof(capability)

    accepted = copy.deepcopy(proof)
    accepted["acceptedDrRecord"]["attestationDigest"] = "sha256:" + ("f" * 64)
    accepted["proofDigest"] = federated_dr_proof.proof_digest(accepted)
    assert "accepted-dr-record-binding-invalid" in federated_dr_proof.validate_federated_dr_proof(accepted)

    state = copy.deepcopy(proof)
    state["failureObservations"][0]["preState"]["unexpectedMutation"] = True
    state["proofDigest"] = federated_dr_proof.proof_digest(state)
    assert "failure-pre-state-digest-mismatch:coldCustodyCannotClaimRecoveryReady" in federated_dr_proof.validate_federated_dr_proof(state)

    digest = copy.deepcopy(proof)
    digest["proofDigest"] = "sha256:" + ("f" * 64)
    assert "proof-digest-mismatch" in federated_dr_proof.validate_federated_dr_proof(digest)


def test_federated_dr_proof_scalar_and_builder_guards(dr_proof_fixture: dict[str, Any]) -> None:
    proof = dr_proof_fixture["proof"]
    with pytest.raises(ValueError, match="timestamp-invalid"):
        federated_dr_proof._utc_iso(NOW.replace(tzinfo=None))

    for value in (None, "not-a-time", "2026-09-01T08:00:00", "2026-09-01T10:00:00+02:00"):
        errors: list[str] = []
        assert federated_dr_proof._parse_timestamp(value, "field", errors) is None
        assert errors == ["invalid-timestamp:field"]
    object_errors: list[str] = []
    assert federated_dr_proof._as_dict(None, "field", object_errors) == {}
    assert object_errors == ["field-must-be-object"]
    digest_errors: list[str] = []
    assert federated_dr_proof._typed_digest("invalid", "field", digest_errors) == ""
    assert digest_errors == ["invalid-sha256:field"]

    assert federated_dr_proof.validate_federated_dr_proof([]) == ["federated-dr-proof-must-be-object"]
    assert federated_dr_proof.validate_federated_dr_proof({"value": float("nan")}) == [
        "federated-dr-proof-canonical-payload-invalid"
    ]
    assert evidence_proof.validate_typed_federated_dr_proof(proof, "unsupported") == [
        "unsupported-federated-dr-proof-check:unsupported"
    ]
    with pytest.raises(ValueError, match="invalid federated DR proof"):
        federated_dr_proof.build_federated_dr_proof(
            validated_at=NOW + timedelta(seconds=17),
            source_fleet_identity=proof["sourceFleetIdentity"],
            destination_fleet_identity=proof["destinationFleetIdentity"],
            peer_trust_record=proof["peerTrustRecord"],
            sender_transfer=proof["senderTransfer"],
            accepted_replica_record=proof["acceptedReplicaRecord"],
            dr_attestation=proof["drAttestation"],
            accepted_dr_record=proof["acceptedDrRecord"],
            recovery_capability=proof["recoveryCapability"],
            production_restore_result={"result": "self-reported-success"},
            failure_observations=proof["failureObservations"],
        )


def test_federated_dr_proof_binding_tamper_matrix(dr_proof_fixture: dict[str, Any]) -> None:
    proof = dr_proof_fixture["proof"]
    digest_f = "sha256:" + ("f" * 64)
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("extra", True), "federated-dr-proof-fields-invalid"),
        (lambda item: item.__setitem__("schema", "self-reported-pass"), "federated-dr-proof-schema-invalid"),
        (lambda item: item.__setitem__("agePrivateIdentity", "AGE-SECRET-KEY-1LEAKED"), "federated-dr-proof-contains-secret"),
        (lambda item: item.__setitem__("sourceFleetIdentity", {}), "source-identity-invalid"),
        (lambda item: item.__setitem__("destinationFleetIdentity", {}), "destination-identity-invalid"),
        (lambda item: item.__setitem__("destinationFleetIdentity", copy.deepcopy(item["sourceFleetIdentity"])), "fleet-identities-not-distinct"),
        (lambda item: item["senderTransfer"].__setitem__("state", "TRANSFERRING"), "sender-transfer-binding-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("extra", True), "accepted-replica-record-fields-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("transferId", digest_f), "accepted-replica-record-binding-invalid"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("acceptedAt", "2026-09-01T08:00:18Z"), "accepted-replica-record-from-future"),
        (lambda item: item["acceptedReplicaRecord"].__setitem__("revision", 0), "accepted-replica-record-revision-invalid"),
        (lambda item: item["drAttestation"].__setitem__("extra", True), "dr-attestation-fields-invalid"),
        (lambda item: item["drAttestation"].__setitem__("signerCertificate", None), "dr-attestation-certificate-must-be-object"),
        (
            lambda item: item["drAttestation"].__setitem__("workspaceDigest", digest_f),
            "dr-attestation-signature-invalid",
        ),
        (lambda item: item["acceptedDrRecord"].__setitem__("extra", True), "accepted-dr-record-fields-invalid"),
        (lambda item: item["acceptedDrRecord"].__setitem__("restoreId", "restore_wrong"), "accepted-dr-record-binding-invalid"),
        (lambda item: item["acceptedDrRecord"].__setitem__("acceptedAt", "2026-09-01T08:00:18Z"), "accepted-dr-record-from-future"),
        (lambda item: item["acceptedDrRecord"].__setitem__("revision", 0), "accepted-dr-record-revision-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("extra", True), "recovery-capability-fields-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("ageRecipient", "invalid"), "recovery-capability-age-recipient-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("ageRecipientDigest", digest_f), "recovery-capability-age-recipient-digest-mismatch"),
        (lambda item: item["recoveryCapability"].__setitem__("peerFleetId", "fleet-c"), "recovery-capability-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("configuredBy", ""), "recovery-capability-operator-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("updatedAt", "2026-09-01T08:00:18Z"), "recovery-capability-time-order-invalid"),
        (lambda item: item["recoveryCapability"].__setitem__("revision", 0), "recovery-capability-revision-invalid"),
        (lambda item: item["productionRestoreResult"].pop("components"), "production-restore-result-fields-invalid"),
        (lambda item: item["productionRestoreResult"].__setitem__("components", 0), "production-restore-metric-invalid:components"),
        (lambda item: item["productionRestoreResult"].__setitem__("logicalBytes", -1), "production-restore-metric-invalid:logicalBytes"),
        (lambda item: item["productionRestoreResult"].__setitem__("schemaVersion", 2), "production-restore-result-invalid"),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert any(error == expected or error.startswith(expected + ":") for error in errors), (expected, errors)


def test_federated_dr_proof_failure_observation_tamper_matrix(dr_proof_fixture: dict[str, Any]) -> None:
    proof = dr_proof_fixture["proof"]
    digest_f = "sha256:" + ("f" * 64)
    cases: tuple[tuple[Callable[[dict[str, Any]], None], str], ...] = (
        (lambda item: item.__setitem__("failureObservations", None), "failure-observations-must-be-list"),
        (lambda item: item["failureObservations"].pop(), "failure-observation-inventory-mismatch"),
        (lambda item: item["failureObservations"][0].__setitem__("extra", True), "failure-fields-invalid:coldCustodyCannotClaimRecoveryReady"),
        (lambda item: item["failureObservations"][0].__setitem__("code", "SELF_REPORTED_PASS"), "failure-code-mismatch:coldCustodyCannotClaimRecoveryReady"),
        (lambda item: item["failureObservations"][0].__setitem__("preState", {}), "failure-pre-state-binding-invalid:coldCustodyCannotClaimRecoveryReady"),
        (lambda item: item["failureObservations"][0].__setitem__("postState", {}), "failure-post-state-binding-invalid:coldCustodyCannotClaimRecoveryReady"),
        (lambda item: item["failureObservations"][0].__setitem__("preStateDigest", digest_f), "failure-pre-state-digest-mismatch:coldCustodyCannotClaimRecoveryReady"),
        (lambda item: item["failureObservations"][0].__setitem__("postStateDigest", digest_f), "failure-post-state-digest-mismatch:coldCustodyCannotClaimRecoveryReady"),
        (
            lambda item: _failure(item, "coldCustodyCannotClaimRecoveryReady")["input"]["capability"].__setitem__(
                "mode", federation_custody_capability.RECOVERY_CAPABLE
            ),
            "cold-custody-evidence-invalid",
        ),
        (
            lambda item: _failure(item, "recoveryCapablePeerRequiresPreprovisionedAgeIdentity")["input"]["capability"].__setitem__(
                "recoveryIdentityPreprovisioned", True
            ),
            "missing-recovery-identity-evidence-invalid",
        ),
        (
            lambda item: _failure(item, "federatedDrProofRequiresCleanupSuccess")["input"]["productionRestoreResult"].__setitem__(
                "result", "failed"
            ),
            "cleanup-failure-code-mismatch",
        ),
        (
            lambda item: _failure(item, "federatedDrProofRequiresCleanupSuccess")["input"]["productionRestoreResult"].__setitem__(
                "cleanupCompleted", True
            ),
            "cleanup-failure-evidence-invalid",
        ),
    )
    for mutate, expected in cases:
        errors = _mutated_errors(proof, mutate)
        assert expected in errors, (expected, errors)
