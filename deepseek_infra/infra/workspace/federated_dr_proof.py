"""Typed, independently recomputable federated DR proof (4.8.0 Gate O)."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    federated_dr_drill,
    federated_replica_attestation,
    federated_replica_proof,
    federation_custody_capability,
    federation_identity,
    federation_peer_trust,
    federation_transfer_journal,
)


FEDERATED_DR_PROOF_SCHEMA = "federated-dr-proof-v1"
FEDERATED_DR_PROOF_CHECKS = (
    "coldCustodyCannotClaimRecoveryReady",
    "recoveryCapablePeerRequiresPreprovisionedAgeIdentity",
    "agePrivateIdentityNeverCrossesFederationBoundary",
    "federatedDrDrillUsesProductionRestore",
    "federatedDrProofBindsTransferId",
    "federatedDrProofBindsBackupId",
    "federatedDrProofBindsObjectSetDigest",
    "federatedDrProofBindsRemoteReceiptAndCommit",
    "federatedDrProofRequiresCleanupSuccess",
    "federatedDrProofIsSemanticallyValidated",
)

_PROOF_FIELDS = frozenset(
    {
        "schema",
        "validatedAt",
        "sourceFleetIdentity",
        "destinationFleetIdentity",
        "peerTrustRecord",
        "senderTransfer",
        "acceptedReplicaRecord",
        "drAttestation",
        "acceptedDrRecord",
        "recoveryCapability",
        "productionRestoreResult",
        "failureObservations",
        "proofDigest",
    }
)
_DR_RECORD_FIELDS = frozenset(
    {
        "schema",
        "peerFleetId",
        "restoreId",
        "transferId",
        "sequence",
        "signerKeyId",
        "attestationDigest",
        "attestation",
        "acceptedAt",
        "revision",
    }
)
_FAILURE_CODES = {
    "coldCustodyCannotClaimRecoveryReady": "FEDERATION_PEER_COLD_CUSTODY_ONLY",
    "recoveryCapablePeerRequiresPreprovisionedAgeIdentity": "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED",
    "federatedDrProofRequiresCleanupSuccess": "FEDERATED_DR_CLEANUP_INCOMPLETE",
}
_FAILURE_FIELDS = frozenset({"claim", "code", "preState", "preStateDigest", "postState", "postStateDigest", "input"})
_FAILURE_STATE_FIELDS = frozenset({"senderTransfer", "acceptedReplica", "acceptedDrDrill"})
_PRODUCTION_RESULT_FIELDS = frozenset(
    {
        "schemaVersion",
        "restoreId",
        "result",
        "startedAt",
        "completedAt",
        "durationMs",
        "workspaceDigest",
        "sourceRevision",
        "cleanupCompleted",
        "chainLength",
        "components",
        "ciphertextBytes",
        "logicalBytes",
        "verifiedContributors",
    }
)
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return federation_identity.canonical_federation_json(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def proof_digest(proof: dict[str, Any]) -> str:
    return _digest({key: value for key, value in proof.items() if key != "proofDigest"})


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("federated-dr-proof-timestamp-invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str, errors: list[str]) -> datetime | None:
    if type(value) is not str:
        errors.append(f"invalid-timestamp:{field}")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"invalid-timestamp:{field}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"invalid-timestamp:{field}")
        return None
    normalized = parsed.astimezone(timezone.utc)
    if normalized.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        errors.append(f"invalid-timestamp:{field}")
        return None
    return normalized


def _as_dict(value: Any, field: str, errors: list[str]) -> dict[str, Any]:
    if type(value) is not dict:
        errors.append(f"{field}-must-be-object")
        return {}
    return value


def _typed_digest(value: Any, field: str, errors: list[str]) -> str:
    if type(value) is not str or _TYPED_DIGEST_PATTERN.fullmatch(value) is None:
        errors.append(f"invalid-sha256:{field}")
        return ""
    return value


def _validate_identities(
    source_value: Any,
    destination_value: Any,
    errors: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        source = federation_identity.validate_fleet_identity(source_value)
    except federation_identity.FederationIdentityError as exc:
        errors.append(f"source-identity-invalid:{exc.code}")
        source = {}
    try:
        destination = federation_identity.validate_fleet_identity(destination_value)
    except federation_identity.FederationIdentityError as exc:
        errors.append(f"destination-identity-invalid:{exc.code}")
        destination = {}
    if source and destination and (
        source.get("fleetId") == destination.get("fleetId")
        or source.get("rootFingerprint") == destination.get("rootFingerprint")
    ):
        errors.append("fleet-identities-not-distinct")
    return source, destination


def _validate_sender_transfer(
    transfer: dict[str, Any],
    *,
    source: dict[str, Any],
    destination: dict[str, Any],
    errors: list[str],
) -> None:
    federated_replica_proof._validate_transfer_record(
        transfer,
        expected_role=federation_transfer_journal.ROLE_SENDER,
        errors=errors,
    )
    state = str(transfer.get("state") or "")
    state_is_committed = state in federation_transfer_journal.TRANSFER_STATES and federation_transfer_journal.TRANSFER_STATES.index(
        state
    ) >= federation_transfer_journal.TRANSFER_STATES.index(federation_transfer_journal.STATE_REMOTE_COMMITTED)
    if (
        not state_is_committed
        or transfer.get("sourceFleetId") != source.get("fleetId")
        or transfer.get("destinationFleetId") != destination.get("fleetId")
        or transfer.get("localFleetId") != source.get("fleetId")
    ):
        errors.append("sender-transfer-binding-invalid")


def _validate_replica_record(
    record: dict[str, Any],
    *,
    identity: dict[str, Any],
    transfer: dict[str, Any],
    pinned_metadata: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> dict[str, Any]:
    attestation = _as_dict(record.get("attestation"), "accepted-replica-attestation", errors)
    if set(record) != federated_replica_proof._ACCEPTED_REPLICA_RECORD_FIELDS:
        errors.append("accepted-replica-record-fields-invalid")
    verified = attestation
    if attestation:
        verified = federated_replica_proof._validate_attestation(
            attestation,
            identity=identity,
            transfer=transfer,
            pinned_metadata=pinned_metadata,
            validated_at=validated_at,
            errors=errors,
        )
    if (
        record.get("schema") != federation_peer_trust.REPLICA_ATTESTATION_RECORD_SCHEMA
        or record.get("peerFleetId") != transfer.get("destinationFleetId")
        or record.get("transferId") != transfer.get("transferId")
        or record.get("sequence") != verified.get("sequence")
        or record.get("signerKeyId") != verified.get("signerKeyId")
        or record.get("attestationDigest") != federated_replica_attestation.attestation_digest(verified)
        or record.get("attestation") != verified
    ):
        errors.append("accepted-replica-record-binding-invalid")
    accepted_at = _parse_timestamp(record.get("acceptedAt"), "acceptedReplicaRecord.acceptedAt", errors)
    if accepted_at is not None and accepted_at > validated_at:
        errors.append("accepted-replica-record-from-future")
    revision = record.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("accepted-replica-record-revision-invalid")
    return verified


def _validate_dr_attestation(
    attestation: dict[str, Any],
    *,
    identity: dict[str, Any],
    transfer: dict[str, Any],
    replica_record: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> dict[str, Any]:
    certificate = _as_dict(attestation.get("signerCertificate"), "dr-attestation-certificate", errors)
    if set(attestation) != federated_dr_drill.DR_DRILL_ATTESTATION_FIELDS:
        errors.append("dr-attestation-fields-invalid")
    if not certificate:
        return attestation
    try:
        verified = federation_identity.verify_federation_document(
            attestation,
            certificate=certificate,
            root_identity=identity,
            expected_schema=federated_dr_drill.DR_DRILL_ATTESTATION_SCHEMA,
            now=validated_at,
            required_purpose=federation_identity.PURPOSE_DR_ATTESTATION,
        )
    except federation_identity.FederationIdentityError as exc:
        errors.append("dr-attestation-signature-invalid")
        errors.append(f"dr-attestation-signature-invalid:{exc.code}")
        return attestation
    try:
        federated_dr_drill._sender_semantics(
            verified,
            transfer=transfer,
            replica_record=replica_record,
            now=validated_at,
            max_future_skew_seconds=30,
        )
    except federated_dr_drill.FederatedDrDrillError as exc:
        errors.append(f"dr-attestation-semantics-invalid:{exc.code}")
    return verified


def _validate_dr_record(
    record: dict[str, Any],
    *,
    attestation: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    if set(record) != _DR_RECORD_FIELDS:
        errors.append("accepted-dr-record-fields-invalid")
    if (
        record.get("schema") != federation_peer_trust.DR_ATTESTATION_RECORD_SCHEMA
        or record.get("peerFleetId") != attestation.get("destinationFleetId")
        or record.get("restoreId") != attestation.get("restoreId")
        or record.get("transferId") != attestation.get("transferId")
        or record.get("sequence") != attestation.get("sequence")
        or record.get("signerKeyId") != attestation.get("signerKeyId")
        or record.get("attestationDigest") != federated_dr_drill.attestation_digest(attestation)
        or record.get("attestation") != attestation
    ):
        errors.append("accepted-dr-record-binding-invalid")
    accepted_at = _parse_timestamp(record.get("acceptedAt"), "acceptedDrRecord.acceptedAt", errors)
    if accepted_at is not None and accepted_at > validated_at:
        errors.append("accepted-dr-record-from-future")
    revision = record.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("accepted-dr-record-revision-invalid")


def _validate_capability(
    capability: dict[str, Any],
    *,
    source: dict[str, Any],
    destination: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    if set(capability) != federation_custody_capability.CUSTODY_CAPABILITY_PUBLIC_FIELDS:
        errors.append("recovery-capability-fields-invalid")
    recipient = capability.get("ageRecipient")
    recipient_valid = False
    try:
        normalized_recipient = federation_custody_capability._recipient(recipient)
    except federation_custody_capability.FederationCustodyCapabilityError:
        errors.append("recovery-capability-age-recipient-invalid")
    else:
        recipient_valid = capability.get("ageRecipientDigest") == federation_custody_capability._recipient_digest(
            normalized_recipient
        )
        if not recipient_valid:
            errors.append("recovery-capability-age-recipient-digest-mismatch")
    if (
        capability.get("schema") != federation_custody_capability.CUSTODY_CAPABILITY_SCHEMA
        or capability.get("localFleetId") != destination.get("fleetId")
        or capability.get("peerFleetId") != source.get("fleetId")
        or capability.get("peerRootFingerprint") != source.get("rootFingerprint")
        or capability.get("mode") != federation_custody_capability.RECOVERY_CAPABLE
        or capability.get("recoveryIdentityPreprovisioned") is not True
        or not recipient_valid
    ):
        errors.append("recovery-capability-invalid")
    configured_by = capability.get("configuredBy")
    if type(configured_by) is not str or not configured_by or configured_by != configured_by.strip():
        errors.append("recovery-capability-operator-invalid")
    configured_at = _parse_timestamp(capability.get("configuredAt"), "recoveryCapability.configuredAt", errors)
    updated_at = _parse_timestamp(capability.get("updatedAt"), "recoveryCapability.updatedAt", errors)
    if configured_at is not None and updated_at is not None and (
        updated_at < configured_at or updated_at > validated_at
    ):
        errors.append("recovery-capability-time-order-invalid")
    revision = capability.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("recovery-capability-revision-invalid")


def _validate_production_result(
    result: dict[str, Any],
    *,
    replica_attestation: dict[str, Any],
    dr_attestation: dict[str, Any],
    errors: list[str],
) -> None:
    if not _PRODUCTION_RESULT_FIELDS <= set(result):
        errors.append("production-restore-result-fields-invalid")
    for field in ("chainLength", "components", "verifiedContributors"):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"production-restore-metric-invalid:{field}")
    for field in ("ciphertextBytes", "logicalBytes"):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"production-restore-metric-invalid:{field}")
    try:
        evidence = federated_dr_drill._drill_evidence(
            result,
            restore_id=str(dr_attestation.get("restoreId") or ""),
            committed_at=federated_dr_drill._parse_timestamp(replica_attestation.get("committedAt")),
            signed_at=federated_dr_drill._parse_timestamp(dr_attestation.get("signedAt")),
        )
    except federated_dr_drill.FederatedDrDrillError as exc:
        errors.append(f"production-restore-evidence-invalid:{exc.code}")
        return
    if result.get("schemaVersion") != 1 or result.get("result") != "success":
        errors.append("production-restore-result-invalid")
    for field, expected in evidence.items():
        if dr_attestation.get(field) != expected:
            errors.append(f"production-restore-attestation-binding-mismatch:{field}")
    if dr_attestation.get("restorePath") != federated_dr_drill.PRODUCTION_RESTORE_PATH:
        errors.append("production-restore-path-invalid")


def _validate_failure_capability(
    capability: dict[str, Any],
    *,
    source: dict[str, Any],
    destination: dict[str, Any],
    expected_mode: str,
) -> bool:
    if set(capability) != federation_custody_capability.CUSTODY_CAPABILITY_PUBLIC_FIELDS:
        return False
    common = (
        capability.get("schema") == federation_custody_capability.CUSTODY_CAPABILITY_SCHEMA
        and capability.get("localFleetId") == destination.get("fleetId")
        and capability.get("peerFleetId") == source.get("fleetId")
        and capability.get("peerRootFingerprint") == source.get("rootFingerprint")
        and capability.get("mode") == expected_mode
    )
    return bool(
        common
        and capability.get("recoveryIdentityPreprovisioned") is False
        and capability.get("ageRecipient") is None
        and capability.get("ageRecipientDigest") is None
    )


def _validate_failures(
    value: Any,
    *,
    source: dict[str, Any],
    destination: dict[str, Any],
    expected_state: dict[str, Any],
    replica_attestation: dict[str, Any],
    dr_attestation: dict[str, Any],
    errors: list[str],
) -> None:
    if type(value) is not list:
        errors.append("failure-observations-must-be-list")
        return
    observations = {str(item.get("claim") or ""): item for item in value if type(item) is dict}
    if set(observations) != set(_FAILURE_CODES) or len(value) != len(_FAILURE_CODES):
        errors.append("failure-observation-inventory-mismatch")
    for claim, code in _FAILURE_CODES.items():
        observation = _as_dict(observations.get(claim), f"failure:{claim}", errors)
        if not observation:
            continue
        if set(observation) != _FAILURE_FIELDS:
            errors.append(f"failure-fields-invalid:{claim}")
        if observation.get("code") != code:
            errors.append(f"failure-code-mismatch:{claim}")
        pre_state = _as_dict(observation.get("preState"), f"{claim}.preState", errors)
        post_state = _as_dict(observation.get("postState"), f"{claim}.postState", errors)
        before = _typed_digest(observation.get("preStateDigest"), f"{claim}.preStateDigest", errors)
        after = _typed_digest(observation.get("postStateDigest"), f"{claim}.postStateDigest", errors)
        if set(pre_state) != _FAILURE_STATE_FIELDS or pre_state != expected_state:
            errors.append(f"failure-pre-state-binding-invalid:{claim}")
        if set(post_state) != _FAILURE_STATE_FIELDS or post_state != expected_state:
            errors.append(f"failure-post-state-binding-invalid:{claim}")
        if before and before != _digest(pre_state):
            errors.append(f"failure-pre-state-digest-mismatch:{claim}")
        if after and after != _digest(post_state):
            errors.append(f"failure-post-state-digest-mismatch:{claim}")
        if before and after and (before != after or pre_state != post_state):
            errors.append(f"failure-mutated-state:{claim}")
        evidence = _as_dict(observation.get("input"), f"failure-input:{claim}", errors)
        if claim == "coldCustodyCannotClaimRecoveryReady":
            capability = _as_dict(evidence.get("capability"), f"failure-capability:{claim}", errors)
            if not _validate_failure_capability(
                capability,
                source=source,
                destination=destination,
                expected_mode=federation_custody_capability.COLD_CUSTODY,
            ):
                errors.append("cold-custody-evidence-invalid")
        elif claim == "recoveryCapablePeerRequiresPreprovisionedAgeIdentity":
            capability = _as_dict(evidence.get("capability"), f"failure-capability:{claim}", errors)
            if not _validate_failure_capability(
                capability,
                source=source,
                destination=destination,
                expected_mode=federation_custody_capability.RECOVERY_CAPABLE,
            ):
                errors.append("missing-recovery-identity-evidence-invalid")
        else:
            result = _as_dict(evidence.get("productionRestoreResult"), "cleanup-failure-result", errors)
            try:
                federated_dr_drill._drill_evidence(
                    result,
                    restore_id=str(dr_attestation.get("restoreId") or ""),
                    committed_at=federated_dr_drill._parse_timestamp(replica_attestation.get("committedAt")),
                    signed_at=federated_dr_drill._parse_timestamp(dr_attestation.get("signedAt")),
                )
            except federated_dr_drill.FederatedDrDrillError as exc:
                if exc.code != code:
                    errors.append("cleanup-failure-code-mismatch")
            else:
                errors.append("cleanup-failure-evidence-invalid")


def build_federated_dr_proof(
    *,
    validated_at: datetime,
    source_fleet_identity: dict[str, Any],
    destination_fleet_identity: dict[str, Any],
    peer_trust_record: dict[str, Any],
    sender_transfer: dict[str, Any],
    accepted_replica_record: dict[str, Any],
    dr_attestation: dict[str, Any],
    accepted_dr_record: dict[str, Any],
    recovery_capability: dict[str, Any],
    production_restore_result: dict[str, Any],
    failure_observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": FEDERATED_DR_PROOF_SCHEMA,
        "validatedAt": _utc_iso(validated_at),
        "sourceFleetIdentity": copy.deepcopy(source_fleet_identity),
        "destinationFleetIdentity": copy.deepcopy(destination_fleet_identity),
        "peerTrustRecord": copy.deepcopy(peer_trust_record),
        "senderTransfer": copy.deepcopy(sender_transfer),
        "acceptedReplicaRecord": copy.deepcopy(accepted_replica_record),
        "drAttestation": copy.deepcopy(dr_attestation),
        "acceptedDrRecord": copy.deepcopy(accepted_dr_record),
        "recoveryCapability": copy.deepcopy(recovery_capability),
        "productionRestoreResult": copy.deepcopy(production_restore_result),
        "failureObservations": [copy.deepcopy(item) for item in failure_observations],
    }
    payload["proofDigest"] = proof_digest(payload)
    errors = validate_federated_dr_proof(payload)
    if errors:
        raise ValueError("invalid federated DR proof: " + "; ".join(errors))
    return payload


def validate_federated_dr_proof(value: Any) -> list[str]:
    """Recompute trust, restore, replica, cleanup, and Age-boundary semantics."""

    errors: list[str] = []
    try:
        normalized = federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError:
        return ["federated-dr-proof-canonical-payload-invalid"]
    if type(normalized) is not dict:
        return ["federated-dr-proof-must-be-object"]
    if set(normalized) != _PROOF_FIELDS:
        errors.append("federated-dr-proof-fields-invalid")
    if normalized.get("schema") != FEDERATED_DR_PROOF_SCHEMA:
        errors.append("federated-dr-proof-schema-invalid")
    try:
        federation_identity.assert_federation_document_secret_free(normalized)
    except federation_identity.FederationIdentityError:
        errors.append("federated-dr-proof-contains-secret")
    validated_at = _parse_timestamp(normalized.get("validatedAt"), "validatedAt", errors)
    source, destination = _validate_identities(
        normalized.get("sourceFleetIdentity"),
        normalized.get("destinationFleetIdentity"),
        errors,
    )
    _peer, pinned_metadata = federated_replica_proof._validate_peer_record(
        normalized.get("peerTrustRecord"),
        identity=destination,
        errors=errors,
    )
    transfer = _as_dict(normalized.get("senderTransfer"), "sender-transfer", errors)
    _validate_sender_transfer(transfer, source=source, destination=destination, errors=errors)
    replica_record = _as_dict(normalized.get("acceptedReplicaRecord"), "accepted-replica-record", errors)
    replica_attestation: dict[str, Any] = {}
    if validated_at is not None and destination and transfer and pinned_metadata and replica_record:
        replica_attestation = _validate_replica_record(
            replica_record,
            identity=destination,
            transfer=transfer,
            pinned_metadata=pinned_metadata,
            validated_at=validated_at,
            errors=errors,
        )
    dr_attestation = _as_dict(normalized.get("drAttestation"), "dr-attestation", errors)
    verified_dr = dr_attestation
    if validated_at is not None and destination and transfer and replica_record and dr_attestation:
        verified_dr = _validate_dr_attestation(
            dr_attestation,
            identity=destination,
            transfer=transfer,
            replica_record=replica_record,
            validated_at=validated_at,
            errors=errors,
        )
    dr_record = _as_dict(normalized.get("acceptedDrRecord"), "accepted-dr-record", errors)
    if validated_at is not None and dr_record and verified_dr:
        _validate_dr_record(dr_record, attestation=verified_dr, validated_at=validated_at, errors=errors)
    capability = _as_dict(normalized.get("recoveryCapability"), "recovery-capability", errors)
    if validated_at is not None and capability and source and destination:
        _validate_capability(
            capability,
            source=source,
            destination=destination,
            validated_at=validated_at,
            errors=errors,
        )
    production_result = _as_dict(normalized.get("productionRestoreResult"), "production-restore-result", errors)
    if production_result and replica_attestation and verified_dr:
        _validate_production_result(
            production_result,
            replica_attestation=replica_attestation,
            dr_attestation=verified_dr,
            errors=errors,
        )
    if source and destination and transfer and replica_record and dr_record and replica_attestation and verified_dr:
        _validate_failures(
            normalized.get("failureObservations"),
            source=source,
            destination=destination,
            expected_state={
                "senderTransfer": transfer,
                "acceptedReplica": replica_record,
                "acceptedDrDrill": dr_record,
            },
            replica_attestation=replica_attestation,
            dr_attestation=verified_dr,
            errors=errors,
        )
    declared_digest = _typed_digest(normalized.get("proofDigest"), "proofDigest", errors)
    if declared_digest and declared_digest != proof_digest(normalized):
        errors.append("proof-digest-mismatch")
    return list(dict.fromkeys(errors))
