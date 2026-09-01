from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from deepseek_infra.infra.workspace import evidence_proof, federation_runtime_proof

UTC = timezone.utc


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _valid_proof() -> dict[str, Any]:
    transfer_id = _digest("a")
    endpoints = [f"http://127.0.0.1:{9000 + index}" for index in range(4)]
    return federation_runtime_proof.build_federation_runtime_proof(
        validated_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        fleet_processes={
            "source": {"fleetId": "fleet-a", "pid": 101, "rootFingerprint": _digest("1")},
            "receiverBefore": {"fleetId": "fleet-b", "pid": 202, "rootFingerprint": _digest("2")},
            "receiverAfter": {"fleetId": "fleet-b", "pid": 303, "rootFingerprint": _digest("2")},
            "receiverKillReturnCode": 1,
        },
        storage_principal_isolation={
            "sourcePrincipalDigest": _digest("3"),
            "receiverPrincipalDigest": _digest("4"),
            "sourceToReceiverDeniedCode": "InvalidAccessKeyId",
            "receiverToSourceDeniedCode": "InvalidAccessKeyId",
        },
        minio_topology={
            "endpoints": endpoints,
            "containers": ["minio-a", "minio-b", "minio-c", "minio-d"],
            "targetBindings": [
                {
                    "fleetId": "fleet-a" if role.startswith("A") else "fleet-b",
                    "role": role,
                    "targetId": f"target-{role.lower()}",
                    "endpoint": endpoint,
                    "providerObjectCount": index + 1,
                }
                for index, (role, endpoint) in enumerate(zip(("A1", "A2", "B1", "B2"), endpoints, strict=True))
            ],
        },
        transfer_recovery={
            "transferId": transfer_id,
            "senderTransferId": transfer_id,
            "receiverTransferId": transfer_id,
            "interruptedComponentDigest": "b" * 64,
            "interruptedBytesSent": 1024,
            "interruptedComponentBytes": 4096,
            "reconcileStatus": "RESUME",
            "reconcileState": "TRANSFERRING",
            "senderFinalState": "SUCCEEDED",
            "remoteCommittedEvents": [{"transferId": transfer_id, "nextState": "REMOTE_COMMITTED"}],
            "commitEffectDigest": _digest("c"),
            "repeatedCommitEffectDigest": _digest("c"),
            "localInventoryBeforeDigest": _digest("d"),
            "localInventoryAfterDigest": _digest("d"),
        },
        fail_closed={
            "replayedIngressGrant": "FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY",
            "tamperedReplicaAttestation": "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
            "revokedPeer": "FEDERATION_PEER_REVOKED",
        },
        dr={
            "schema": "federated-dr-drill-attestation-v1",
            "transferId": transfer_id,
            "restorePath": "backup-recovery-drill-production-v1",
            "cleanupCompleted": True,
            "workspaceDigest": _digest("e"),
        },
    )


def _mutated(proof: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    changed = copy.deepcopy(proof)
    mutate(changed)
    changed["proofDigest"] = federation_runtime_proof.proof_digest(changed)
    return changed


def test_runtime_proof_is_semantic_and_all_claims_use_its_validator() -> None:
    proof = _valid_proof()

    assert federation_runtime_proof.validate_federation_runtime_proof(proof) == []
    for check_name in federation_runtime_proof.FEDERATION_RUNTIME_PROOF_CHECKS:
        assert evidence_proof.VALIDATORS[check_name] is evidence_proof.validate_typed_federation_runtime_proof
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda proof: proof["fleetProcesses"]["receiverAfter"].update(pid=202), "fleet-processes-not-independent"),
        (
            lambda proof: proof["storagePrincipalIsolation"].update(
                receiverPrincipalDigest=proof["storagePrincipalIsolation"]["sourcePrincipalDigest"]
            ),
            "cross-fleet-storage-principal-isolation-invalid",
        ),
        (lambda proof: proof["transferRecovery"].update(receiverTransferId=_digest("f")), "runtime-transfer-binding-invalid"),
        (lambda proof: proof["transferRecovery"].update(interruptedBytesSent=4096), "interrupted-transfer-bytes-invalid"),
        (
            lambda proof: proof["transferRecovery"].update(
                remoteCommittedEvents=proof["transferRecovery"]["remoteCommittedEvents"] * 2
            ),
            "remote-commit-event-count-invalid",
        ),
        (lambda proof: proof["transferRecovery"].update(repeatedCommitEffectDigest=_digest("f")), "remote-commit-effect-duplicated"),
        (lambda proof: proof["transferRecovery"].update(localInventoryAfterDigest=_digest("f")), "local-inventory-regressed"),
        (lambda proof: proof["failClosed"].update(revokedPeer="HTTP_200"), "runtime-fail-closed-evidence-invalid"),
        (lambda proof: proof["dr"].update(cleanupCompleted=False), "runtime-dr-evidence-invalid"),
    ],
)
def test_runtime_proof_rejects_self_declared_or_unbound_evidence(
    mutate: Callable[[dict[str, Any]], None],
    expected_error: str,
) -> None:
    errors = federation_runtime_proof.validate_federation_runtime_proof(_mutated(_valid_proof(), mutate))

    assert expected_error in errors


def test_runtime_proof_treats_malformed_topology_as_invalid_instead_of_crashing() -> None:
    proof = _mutated(_valid_proof(), lambda value: value["minioTopology"].update(endpoints=[{}, {}, {}, {}]))

    assert "four-minio-endpoints-invalid" in federation_runtime_proof.validate_federation_runtime_proof(proof)


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda proof: proof.update(unexpected=True), "federation-runtime-proof-fields-invalid"),
        (lambda proof: proof.update(schema="federation-runtime-e2e-proof-v2"), "federation-runtime-proof-schema-invalid"),
        (lambda proof: proof["dr"].update(privateKey="redacted"), "federation-runtime-proof-contains-secret"),
        (lambda proof: proof.update(validatedAt="not-a-timestamp"), "federation-runtime-proof-timestamp-invalid"),
        (lambda proof: proof.update(fleetProcesses=[]), "fleet-processes-must-be-object"),
        (lambda proof: proof["fleetProcesses"]["source"].update(extra=True), "source-process-fields-invalid"),
        (lambda proof: proof["fleetProcesses"]["source"].update(pid=True), "source-pid-invalid"),
        (lambda proof: proof["fleetProcesses"]["source"].update(rootFingerprint="bad"), "source-root-fingerprint-invalid"),
        (lambda proof: proof["fleetProcesses"]["source"].update(fleetId="fleet-b"), "fleet-process-identity-invalid"),
        (
            lambda proof: proof["fleetProcesses"]["source"].update(
                rootFingerprint=proof["fleetProcesses"]["receiverBefore"]["rootFingerprint"]
            ),
            "fleet-root-sovereignty-invalid",
        ),
        (lambda proof: proof["fleetProcesses"].update(receiverKillReturnCode=0), "receiver-sigkill-exit-invalid"),
        (lambda proof: proof.update(storagePrincipalIsolation=None), "storage-principal-isolation-must-be-object"),
        (
            lambda proof: proof["storagePrincipalIsolation"].update(sourceToReceiverDeniedCode="HTTP_200"),
            "cross-fleet-storage-principal-isolation-invalid",
        ),
        (lambda proof: proof["minioTopology"].update(extra=True), "minio-topology-fields-invalid"),
        (
            lambda proof: proof["minioTopology"]["endpoints"].__setitem__(0, "not-a-provider-url"),
            "minio-endpoint-invalid",
        ),
        (
            lambda proof: proof["minioTopology"].update(containers=["same"] * 4),
            "four-minio-containers-invalid",
        ),
        (lambda proof: proof["minioTopology"].update(targetBindings="invalid"), "four-minio-target-bindings-invalid"),
        (
            lambda proof: proof["minioTopology"]["targetBindings"].__setitem__(0, None),
            "minio-target-binding-must-be-object",
        ),
        (
            lambda proof: proof["minioTopology"]["targetBindings"][0].update(extra=True),
            "minio-target-binding-fields-invalid",
        ),
        (
            lambda proof: proof["minioTopology"]["targetBindings"][0].update(role=""),
            "minio-target-binding-value-invalid",
        ),
        (
            lambda proof: proof["minioTopology"]["targetBindings"][0].update(providerObjectCount=0),
            "minio-provider-object-count-invalid",
        ),
        (lambda proof: proof.update(transferRecovery=None), "transfer-recovery-must-be-object"),
        (lambda proof: proof["transferRecovery"].pop("senderTransferId"), "transfer-recovery-fields-invalid"),
        (
            lambda proof: proof["transferRecovery"].update(interruptedComponentDigest="bad"),
            "interrupted-component-digest-invalid",
        ),
        (
            lambda proof: proof["transferRecovery"].update(
                reconcileStatus="COMMITTED",
                reconcileState="REMOTE_COMMITTED",
            ),
            "receiver-reconcile-state-invalid",
        ),
        (lambda proof: proof["transferRecovery"].update(senderFinalState="FAILED"), "sender-terminal-state-invalid"),
        (
            lambda proof: proof["transferRecovery"].update(remoteCommittedEvents=[None]),
            "remote-commit-event-must-be-object",
        ),
        (lambda proof: proof["transferRecovery"].update(commitEffectDigest="bad"), "commitEffectDigest-invalid"),
        (lambda proof: proof.update(failClosed={}), "runtime-fail-closed-evidence-invalid"),
        (lambda proof: proof.update(dr=None), "runtime-dr-must-be-object"),
        (lambda proof: proof["dr"].update(extra=True), "runtime-dr-fields-invalid"),
    ],
)
def test_runtime_proof_rejects_malformed_process_storage_transfer_and_dr_sections(
    mutate: Callable[[dict[str, Any]], None],
    expected_error: str,
) -> None:
    errors = federation_runtime_proof.validate_federation_runtime_proof(_mutated(_valid_proof(), mutate))

    assert expected_error in errors


def test_runtime_proof_rejects_noncanonical_top_level_and_bad_declared_digest() -> None:
    assert federation_runtime_proof.validate_federation_runtime_proof(None) == [
        "federation-runtime-proof-must-be-object"
    ]
    assert federation_runtime_proof.validate_federation_runtime_proof({"invalid": object()}) == [
        "federation-runtime-proof-canonical-payload-invalid"
    ]

    proof = _valid_proof()
    proof["proofDigest"] = _digest("f")
    assert "federation-runtime-proof-digest-mismatch" in federation_runtime_proof.validate_federation_runtime_proof(proof)


def test_runtime_proof_timestamp_requires_timezone() -> None:
    with pytest.raises(ValueError, match="federation-runtime-proof-timestamp-invalid"):
        federation_runtime_proof._utc_iso(datetime(2026, 9, 2, 12, 0))
