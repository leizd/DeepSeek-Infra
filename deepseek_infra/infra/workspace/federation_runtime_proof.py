"""Semantic runtime proof for the real two-Fleet, four-MinIO process topology."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from deepseek_infra.infra.workspace import federated_dr_drill, federation_identity, federation_transfer_journal

FEDERATION_RUNTIME_PROOF_SCHEMA = "federation-runtime-e2e-proof-v1"
FEDERATION_RUNTIME_PROOF_CHECKS = (
    "realTwoFleetFourMinioReplicationE2E",
    "realReceiverProcessSigkillResumesTransfer",
    "realReceiverRestartDoesNotDuplicateCommit",
    "revokedPeerCannotStartTransfer",
    "fleetProcessesUseDistinctStorageCredentials",
)
_PROOF_FIELDS = frozenset(
    {
        "schema",
        "validatedAt",
        "fleetProcesses",
        "storagePrincipalIsolation",
        "minioTopology",
        "transferRecovery",
        "failClosed",
        "dr",
        "proofDigest",
    }
)


def _digest(value: Any) -> str:
    canonical = federation_identity.canonical_federation_json(value)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def proof_digest(proof: dict[str, Any]) -> str:
    return _digest({key: value for key, value in proof.items() if key != "proofDigest"})


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("federation-runtime-proof-timestamp-invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _document(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if type(value) is not dict:
        errors.append(f"{label}-must-be-object")
        return {}
    return value


def _typed_digest(value: Any) -> bool:
    return type(value) is str and len(value) == 71 and value.startswith("sha256:") and all(
        character in "0123456789abcdef" for character in value[7:]
    )


def build_federation_runtime_proof(
    *,
    validated_at: datetime,
    fleet_processes: dict[str, Any],
    storage_principal_isolation: dict[str, Any],
    minio_topology: dict[str, Any],
    transfer_recovery: dict[str, Any],
    fail_closed: dict[str, Any],
    dr: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema": FEDERATION_RUNTIME_PROOF_SCHEMA,
        "validatedAt": _utc_iso(validated_at),
        "fleetProcesses": copy.deepcopy(fleet_processes),
        "storagePrincipalIsolation": copy.deepcopy(storage_principal_isolation),
        "minioTopology": copy.deepcopy(minio_topology),
        "transferRecovery": copy.deepcopy(transfer_recovery),
        "failClosed": copy.deepcopy(fail_closed),
        "dr": copy.deepcopy(dr),
    }
    payload["proofDigest"] = proof_digest(payload)
    errors = validate_federation_runtime_proof(payload)
    if errors:
        raise ValueError("invalid federation runtime proof: " + "; ".join(errors))
    return payload


def validate_federation_runtime_proof(value: Any) -> list[str]:
    errors: list[str] = []
    try:
        normalized = federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError:
        return ["federation-runtime-proof-canonical-payload-invalid"]
    if type(normalized) is not dict:
        return ["federation-runtime-proof-must-be-object"]
    if set(normalized) != _PROOF_FIELDS:
        errors.append("federation-runtime-proof-fields-invalid")
    if normalized.get("schema") != FEDERATION_RUNTIME_PROOF_SCHEMA:
        errors.append("federation-runtime-proof-schema-invalid")
    try:
        federation_identity.assert_federation_document_secret_free(normalized)
    except federation_identity.FederationIdentityError:
        errors.append("federation-runtime-proof-contains-secret")
    try:
        timestamp = datetime.fromisoformat(str(normalized.get("validatedAt") or "").replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        errors.append("federation-runtime-proof-timestamp-invalid")

    processes = _document(normalized.get("fleetProcesses"), "fleet-processes", errors)
    if set(processes) != {"source", "receiverBefore", "receiverAfter", "receiverKillReturnCode"}:
        errors.append("fleet-process-fields-invalid")
    source = _document(processes.get("source"), "source-process", errors)
    before = _document(processes.get("receiverBefore"), "receiver-before-process", errors)
    after = _document(processes.get("receiverAfter"), "receiver-after-process", errors)
    for label, process in (("source", source), ("receiver-before", before), ("receiver-after", after)):
        if set(process) != {"fleetId", "pid", "rootFingerprint"}:
            errors.append(f"{label}-process-fields-invalid")
        if isinstance(process.get("pid"), bool) or not isinstance(process.get("pid"), int) or int(process.get("pid") or 0) <= 0:
            errors.append(f"{label}-pid-invalid")
        if not _typed_digest(process.get("rootFingerprint")):
            errors.append(f"{label}-root-fingerprint-invalid")
    pids = [source.get("pid"), before.get("pid"), after.get("pid")]
    if len(set(pids)) != 3:
        errors.append("fleet-processes-not-independent")
    if source.get("fleetId") != "fleet-a" or before.get("fleetId") != "fleet-b" or after.get("fleetId") != "fleet-b":
        errors.append("fleet-process-identity-invalid")
    if source.get("rootFingerprint") == before.get("rootFingerprint") or before.get("rootFingerprint") != after.get("rootFingerprint"):
        errors.append("fleet-root-sovereignty-invalid")
    kill_code = processes.get("receiverKillReturnCode")
    if isinstance(kill_code, bool) or not isinstance(kill_code, int) or kill_code == 0:
        errors.append("receiver-sigkill-exit-invalid")

    storage_isolation = _document(normalized.get("storagePrincipalIsolation"), "storage-principal-isolation", errors)
    if set(storage_isolation) != {
        "sourcePrincipalDigest",
        "receiverPrincipalDigest",
        "sourceToReceiverDeniedCode",
        "receiverToSourceDeniedCode",
    }:
        errors.append("storage-principal-isolation-fields-invalid")
    source_principal = storage_isolation.get("sourcePrincipalDigest")
    receiver_principal = storage_isolation.get("receiverPrincipalDigest")
    denied_codes = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}
    if (
        not _typed_digest(source_principal)
        or not _typed_digest(receiver_principal)
        or source_principal == receiver_principal
        or storage_isolation.get("sourceToReceiverDeniedCode") not in denied_codes
        or storage_isolation.get("receiverToSourceDeniedCode") not in denied_codes
    ):
        errors.append("cross-fleet-storage-principal-isolation-invalid")

    topology = _document(normalized.get("minioTopology"), "minio-topology", errors)
    if set(topology) != {"endpoints", "containers", "targetBindings"}:
        errors.append("minio-topology-fields-invalid")
    endpoints = topology.get("endpoints")
    containers = topology.get("containers")
    bindings = topology.get("targetBindings")
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != 4
        or not all(type(endpoint) is str for endpoint in endpoints)
        or len(set(endpoints)) != 4
    ):
        errors.append("four-minio-endpoints-invalid")
        endpoints = []
    for endpoint in endpoints:
        parsed = urlparse(str(endpoint))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
            errors.append("minio-endpoint-invalid")
    if (
        not isinstance(containers, list)
        or len(containers) != 4
        or not all(type(container) is str and container for container in containers)
        or len(set(containers)) != 4
    ):
        errors.append("four-minio-containers-invalid")
    if not isinstance(bindings, list) or len(bindings) != 4:
        errors.append("four-minio-target-bindings-invalid")
        bindings = []
    roles: set[str] = set()
    target_ids: set[str] = set()
    binding_endpoints: set[str] = set()
    role_fleets: dict[str, str] = {}
    for binding_value in bindings:
        binding = _document(binding_value, "minio-target-binding", errors)
        if set(binding) != {"fleetId", "role", "targetId", "endpoint", "providerObjectCount"}:
            errors.append("minio-target-binding-fields-invalid")
            continue
        role = binding.get("role")
        fleet_id = binding.get("fleetId")
        target_id = binding.get("targetId")
        endpoint = binding.get("endpoint")
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(fleet_id, str)
            or not fleet_id
            or not isinstance(target_id, str)
            or not target_id
            or not isinstance(endpoint, str)
            or not endpoint
        ):
            errors.append("minio-target-binding-value-invalid")
            continue
        roles.add(role)
        target_ids.add(target_id)
        binding_endpoints.add(endpoint)
        role_fleets[role] = fleet_id
        count = binding.get("providerObjectCount")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            errors.append("minio-provider-object-count-invalid")
    if (
        roles != {"A1", "A2", "B1", "B2"}
        or role_fleets != {"A1": "fleet-a", "A2": "fleet-a", "B1": "fleet-b", "B2": "fleet-b"}
        or len(target_ids) != 4
        or binding_endpoints != set(endpoints)
    ):
        errors.append("four-minio-role-binding-invalid")

    recovery = _document(normalized.get("transferRecovery"), "transfer-recovery", errors)
    expected_recovery_fields = {
        "transferId",
        "senderTransferId",
        "receiverTransferId",
        "interruptedComponentDigest",
        "interruptedBytesSent",
        "interruptedComponentBytes",
        "reconcileStatus",
        "reconcileState",
        "senderFinalState",
        "remoteCommittedEvents",
        "commitEffectDigest",
        "repeatedCommitEffectDigest",
        "localInventoryBeforeDigest",
        "localInventoryAfterDigest",
    }
    if set(recovery) != expected_recovery_fields:
        errors.append("transfer-recovery-fields-invalid")
    transfer_id = recovery.get("transferId")
    if (
        not _typed_digest(transfer_id)
        or recovery.get("senderTransferId") != transfer_id
        or recovery.get("receiverTransferId") != transfer_id
    ):
        errors.append("runtime-transfer-binding-invalid")
    component_digest = recovery.get("interruptedComponentDigest")
    if type(component_digest) is not str or len(component_digest) != 64 or not all(c in "0123456789abcdef" for c in component_digest):
        errors.append("interrupted-component-digest-invalid")
    sent = recovery.get("interruptedBytesSent")
    total = recovery.get("interruptedComponentBytes")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (sent, total)) or not (0 < int(sent or 0) < int(total or 0)):
        errors.append("interrupted-transfer-bytes-invalid")
    if recovery.get("reconcileStatus") != "RESUME" or recovery.get("reconcileState") == federation_transfer_journal.STATE_REMOTE_COMMITTED:
        errors.append("receiver-reconcile-state-invalid")
    if recovery.get("senderFinalState") != federation_transfer_journal.STATE_SUCCEEDED:
        errors.append("sender-terminal-state-invalid")
    events = recovery.get("remoteCommittedEvents")
    if not isinstance(events, list) or len(events) != 1:
        errors.append("remote-commit-event-count-invalid")
    else:
        event = _document(events[0], "remote-commit-event", errors)
        if event.get("transferId") != transfer_id or event.get("nextState") != federation_transfer_journal.STATE_REMOTE_COMMITTED:
            errors.append("remote-commit-event-binding-invalid")
    for field in (
        "commitEffectDigest",
        "repeatedCommitEffectDigest",
        "localInventoryBeforeDigest",
        "localInventoryAfterDigest",
    ):
        if not _typed_digest(recovery.get(field)):
            errors.append(f"{field}-invalid")
    if recovery.get("commitEffectDigest") != recovery.get("repeatedCommitEffectDigest"):
        errors.append("remote-commit-effect-duplicated")
    if recovery.get("localInventoryBeforeDigest") != recovery.get("localInventoryAfterDigest"):
        errors.append("local-inventory-regressed")

    fail_closed = _document(normalized.get("failClosed"), "fail-closed", errors)
    expected_failures = {
        "replayedIngressGrant": "FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY",
        "tamperedReplicaAttestation": "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
        "revokedPeer": "FEDERATION_PEER_REVOKED",
    }
    if fail_closed != expected_failures:
        errors.append("runtime-fail-closed-evidence-invalid")

    dr = _document(normalized.get("dr"), "runtime-dr", errors)
    if set(dr) != {"schema", "transferId", "restorePath", "cleanupCompleted", "workspaceDigest"}:
        errors.append("runtime-dr-fields-invalid")
    if (
        dr.get("schema") != "federated-dr-drill-attestation-v1"
        or dr.get("transferId") != transfer_id
        or dr.get("restorePath") != federated_dr_drill.PRODUCTION_RESTORE_PATH
        or dr.get("cleanupCompleted") is not True
        or not _typed_digest(dr.get("workspaceDigest"))
    ):
        errors.append("runtime-dr-evidence-invalid")
    declared_digest = normalized.get("proofDigest")
    if not _typed_digest(declared_digest) or declared_digest != proof_digest(normalized):
        errors.append("federation-runtime-proof-digest-mismatch")
    return list(dict.fromkeys(errors))
