"""Typed, independently recomputable federated replica proof (4.8.0 Gate O)."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    federated_durability,
    federated_replica_attestation,
    federation_identity,
    federation_ingress_grant,
    federation_peer_trust,
    federation_transfer,
    federation_transfer_journal,
)


FEDERATED_REPLICA_PROOF_SCHEMA = "federated-replica-proof-v1"
FEDERATED_REPLICA_PROOF_CHECKS = (
    "ingressGrantIsReceiverSigned",
    "ingressGrantBindsSourceFleet",
    "ingressGrantBindsDestinationFleet",
    "ingressGrantBindsBackupId",
    "ingressGrantBindsObjectSetDigest",
    "ingressGrantBindsTransferId",
    "expiredIngressGrantCannotWrite",
    "ingressGrantCannotEscapeObjectPrefix",
    "ingressGrantCannotExceedMaxBytes",
    "sameTransferIdSameDigestIsIdempotent",
    "sameTransferIdDifferentDigestFailsClosed",
    "receiverRestartResumesExistingTransfer",
    "federatedReplicaUsesExistingObjectSetV1",
    "federatedReplicaCreatesReceiptV4",
    "federatedReplicaCreatesCommitV4",
    "federatedReplicaAttestationBindsReceiptDigest",
    "federatedReplicaAttestationBindsCommitDigest",
    "federatedReplicaAttestationBindsObjectSetDigest",
    "remoteCopyRecordedOnlyAfterAttestationVerification",
    "federatedCopyDoesNotReduceLocalMinCommittedCopies",
    "federatedCopyDoesNotReduceLocalMinFailureDomains",
    "federatedCopyCannotAuthorizePrimaryPromotion",
    "federatedTransferNeverDeletesLocalReplica",
    "peerFailureDomainIsCheckedAgainstPinnedMetadata",
    "replayedIngressGrantFailsClosed",
    "tamperedReplicaAttestationFailsClosed",
    "objectSetV1WireFormatUnchanged",
    "receiptV4Unchanged",
    "commitV4Unchanged",
    "fastCdcV3Unchanged",
    "randomizedAgeUnchanged",
    "federatedReplicaProofIsSemanticallyValidated",
)

_PROOF_FIELDS = frozenset(
    {
        "schema",
        "validatedAt",
        "destinationFleetIdentity",
        "peerTrustRecord",
        "ingressGrant",
        "receiverTransfer",
        "senderTransfer",
        "objectSetDeclaration",
        "sourceReceipt",
        "remoteReceiptBytesBase64",
        "remoteCommitBytesBase64",
        "replicaAttestation",
        "acceptedReplicaRecord",
        "federatedCopyRecord",
        "localDurabilityBefore",
        "localDurabilityAfter",
        "federatedDurabilityStatus",
        "failureObservations",
        "wireContracts",
        "proofDigest",
    }
)
_FAILURE_CODES = {
    "expiredIngressGrantCannotWrite": "FEDERATION_INGRESS_GRANT_EXPIRED",
    "ingressGrantCannotEscapeObjectPrefix": "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION",
    "ingressGrantCannotExceedMaxBytes": "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED",
    "sameTransferIdDifferentDigestFailsClosed": "FEDERATION_TRANSFER_IDENTITY_CONFLICT",
    "replayedIngressGrantFailsClosed": "FEDERATION_INGRESS_GRANT_NONCE_REPLAY",
    "tamperedReplicaAttestationFailsClosed": "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
}
_FAILURE_FIELDS = frozenset({"claim", "code", "preState", "preStateDigest", "postState", "postStateDigest", "input"})
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PINNED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})
_PEER_RECORD_FIELDS = frozenset(
    {
        "schema",
        "peerFleetId",
        "rootKeyId",
        "rootFingerprint",
        "fleetIdentity",
        "pinnedMetadata",
        "metadataDigest",
        "state",
        "pinnedBy",
        "stateReason",
        "pinnedAt",
        "verifiedAt",
        "activatedAt",
        "suspendedAt",
        "revokedAt",
        "revision",
        "updatedAt",
    }
)
_TRANSFER_RECORD_FIELDS = frozenset(
    {
        "schema",
        "transferId",
        "identityDigest",
        "localFleetId",
        "role",
        "sourceFleetId",
        "destinationFleetId",
        "policyId",
        "backupId",
        "objectSetDigest",
        "state",
        "statePayloadDigest",
        "stateDetails",
        "createdAt",
        "updatedAt",
        "revision",
    }
)
_ACCEPTED_REPLICA_RECORD_FIELDS = frozenset(
    {
        "schema",
        "peerFleetId",
        "transferId",
        "sequence",
        "signerKeyId",
        "attestationDigest",
        "attestation",
        "acceptedAt",
        "revision",
    }
)


def _canonical_json(value: Any) -> str:
    return federation_identity.canonical_federation_json(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def proof_digest(proof: dict[str, Any]) -> str:
    return _digest({key: value for key, value in proof.items() if key != "proofDigest"})


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("federated-replica-proof-timestamp-invalid")
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


def _decode_document(value: Any, field: str, errors: list[str]) -> bytes:
    if type(value) is not str:
        errors.append(f"invalid-base64:{field}")
        return b""
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        errors.append(f"invalid-base64:{field}")
        return b""
    if not decoded:
        errors.append(f"empty-bytes:{field}")
    return decoded


def _validate_peer_record(
    value: Any,
    *,
    identity: dict[str, Any],
    errors: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    peer = _as_dict(value, "peer-trust-record", errors)
    if not peer:
        return {}, {}
    if set(peer) != _PEER_RECORD_FIELDS:
        errors.append("peer-trust-record-fields-invalid")
    if peer.get("schema") != federation_peer_trust.PEER_TRUST_RECORD_SCHEMA:
        errors.append("peer-trust-record-schema-invalid")
    if peer.get("state") != federation_peer_trust.STATE_ACTIVE:
        errors.append("peer-trust-not-active")
    if peer.get("peerFleetId") != identity.get("fleetId") or peer.get("fleetIdentity") != identity:
        errors.append("peer-trust-identity-binding-invalid")
    if peer.get("rootKeyId") != identity.get("rootKeyId") or peer.get("rootFingerprint") != identity.get("rootFingerprint"):
        errors.append("peer-trust-root-binding-invalid")
    metadata = _as_dict(peer.get("pinnedMetadata"), "pinned-metadata", errors)
    if (
        set(metadata) != _PINNED_METADATA_FIELDS
        or any(type(item) is not str or not item or item != item.strip() for item in metadata.values())
    ):
        errors.append("pinned-metadata-invalid")
    elif peer.get("metadataDigest") != _digest(metadata):
        errors.append("pinned-metadata-digest-mismatch")
    if type(peer.get("pinnedBy")) is not str or not peer.get("pinnedBy"):
        errors.append("peer-trust-operator-pin-missing")
    for field in ("pinnedAt", "verifiedAt", "activatedAt", "updatedAt"):
        _parse_timestamp(peer.get(field), f"peerTrustRecord.{field}", errors)
    revision = peer.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("peer-trust-revision-invalid")
    if peer.get("revokedAt") is not None:
        errors.append("peer-trust-revoked")
    return peer, metadata


def _validate_transfer_record(record: dict[str, Any], *, expected_role: str, errors: list[str]) -> None:
    label = expected_role.casefold()
    raw_state_details = record.get("stateDetails")
    state_details: dict[str, Any] = raw_state_details if type(raw_state_details) is dict else {}
    if set(record) != _TRANSFER_RECORD_FIELDS:
        errors.append(f"{label}-transfer-fields-invalid")
    if record.get("schema") != federation_transfer_journal.TRANSFER_JOURNAL_RECORD_SCHEMA:
        errors.append(f"{label}-transfer-schema-invalid")
    if record.get("role") != expected_role:
        errors.append(f"{label}-transfer-role-invalid")
    expected_local_fleet = record.get("destinationFleetId") if expected_role == federation_transfer_journal.ROLE_RECEIVER else record.get("sourceFleetId")
    if record.get("localFleetId") != expected_local_fleet:
        errors.append(f"{label}-transfer-local-fleet-invalid")
    try:
        _, identity_digest = federation_transfer_journal._identity_binding(
            transfer_id=str(record.get("transferId") or ""),
            source_fleet_id=str(record.get("sourceFleetId") or ""),
            destination_fleet_id=str(record.get("destinationFleetId") or ""),
            policy_id=str(record.get("policyId") or ""),
            backup_id=str(record.get("backupId") or ""),
            object_set_digest=str(record.get("objectSetDigest") or ""),
        )
        _, state_digest, _ = federation_transfer_journal._state_payload(
            str(record.get("transferId") or ""),
            str(record.get("state") or ""),
            state_details,
        )
    except federation_transfer_journal.FederatedTransferJournalError:
        errors.append(f"{label}-transfer-record-invalid")
    else:
        if record.get("identityDigest") != identity_digest:
            errors.append(f"{label}-transfer-identity-digest-mismatch")
        if record.get("statePayloadDigest") != state_digest:
            errors.append(f"{label}-transfer-state-digest-mismatch")
    created = _parse_timestamp(record.get("createdAt"), f"{label}Transfer.createdAt", errors)
    updated = _parse_timestamp(record.get("updatedAt"), f"{label}Transfer.updatedAt", errors)
    if created is not None and updated is not None and updated < created:
        errors.append(f"{label}-transfer-time-order-invalid")
    revision = record.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append(f"{label}-transfer-revision-invalid")


def _validate_transfer_pair(
    receiver: dict[str, Any],
    sender: dict[str, Any],
    errors: list[str],
) -> tuple[str, str, str, str, str]:
    _validate_transfer_record(receiver, expected_role=federation_transfer_journal.ROLE_RECEIVER, errors=errors)
    _validate_transfer_record(sender, expected_role=federation_transfer_journal.ROLE_SENDER, errors=errors)
    fields = ("transferId", "sourceFleetId", "destinationFleetId", "policyId", "backupId", "objectSetDigest")
    for field in fields:
        if receiver.get(field) != sender.get(field):
            errors.append(f"transfer-role-binding-mismatch:{field}")
    source = str(receiver.get("sourceFleetId") or "")
    destination = str(receiver.get("destinationFleetId") or "")
    policy = str(receiver.get("policyId") or "")
    backup = str(receiver.get("backupId") or "")
    object_set = _typed_digest(receiver.get("objectSetDigest"), "receiverTransfer.objectSetDigest", errors)
    transfer_id = _typed_digest(receiver.get("transferId"), "receiverTransfer.transferId", errors)
    if object_set and transfer_id:
        try:
            derived = federation_transfer.derive_transfer_id(
                source_fleet_id=source,
                destination_fleet_id=destination,
                backup_id=backup,
                object_set_digest=object_set,
            )
        except federation_transfer.FederatedTransferError:
            errors.append("transfer-identity-invalid")
        else:
            if derived != transfer_id:
                errors.append("transfer-identity-invalid")
    if receiver.get("state") != federation_transfer_journal.STATE_REMOTE_COMMITTED:
        errors.append("receiver-transfer-not-committed")
    if sender.get("state") != federation_transfer_journal.STATE_SUCCEEDED:
        errors.append("sender-transfer-not-succeeded")
    return transfer_id, source, destination, policy, backup


def _validate_grant(
    grant: dict[str, Any],
    *,
    identity: dict[str, Any],
    transfer: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    certificate = _as_dict(grant.get("signerCertificate"), "ingress-grant-certificate", errors)
    if certificate:
        try:
            federation_identity.verify_federation_document(
                grant,
                certificate=certificate,
                root_identity=identity,
                expected_schema=federation_ingress_grant.INGRESS_GRANT_SCHEMA,
                now=validated_at,
                required_purpose=federation_identity.PURPOSE_INGRESS_GRANT,
            )
        except federation_identity.FederationIdentityError as exc:
            errors.append(f"ingress-grant-signature-invalid:{exc.code}")
    try:
        federation_ingress_grant._grant_semantics(
            grant,
            expected_source_fleet_id=str(transfer.get("sourceFleetId") or ""),
            expected_destination_fleet_id=str(transfer.get("destinationFleetId") or ""),
            expected_transfer_id=str(transfer.get("transferId") or ""),
            expected_policy_id=str(transfer.get("policyId") or ""),
            expected_backup_id=str(transfer.get("backupId") or ""),
            expected_object_set_digest=str(transfer.get("objectSetDigest") or ""),
            now=validated_at,
            max_future_skew_seconds=30,
        )
    except federation_ingress_grant.FederationIngressGrantError as exc:
        errors.append(f"ingress-grant-invalid:{exc.code}")


def _validate_storage(
    *,
    declaration: dict[str, Any],
    source_receipt: dict[str, Any],
    receipt_bytes: bytes,
    commit_bytes: bytes,
    transfer: dict[str, Any],
    attestation: dict[str, Any],
    errors: list[str],
) -> None:
    if (
        declaration.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or declaration.get("objectSetDigest") != transfer.get("objectSetDigest")
        or declaration.get("backupId") != transfer.get("backupId")
        or declaration.get("policyId") != transfer.get("policyId")
        or not isinstance(declaration.get("objects"), list)
        or not declaration.get("objects")
    ):
        errors.append("object-set-declaration-invalid")
    try:
        federated_replica_attestation._validate_remote_documents(
            source_receipt=source_receipt,
            remote_receipt_bytes=receipt_bytes,
            remote_commit_bytes=commit_bytes,
            transfer=transfer,
            attestation=attestation,
        )
    except federated_replica_attestation.FederatedReplicaAttestationError as exc:
        errors.append("remote-storage-documents-invalid")
        errors.append(f"remote-storage-documents-invalid:{exc.code}")


def _validate_attestation(
    attestation: dict[str, Any],
    *,
    identity: dict[str, Any],
    transfer: dict[str, Any],
    pinned_metadata: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> dict[str, Any]:
    certificate = _as_dict(attestation.get("signerCertificate"), "replica-attestation-certificate", errors)
    if not certificate:
        return attestation
    try:
        verified = federation_identity.verify_federation_document(
            attestation,
            certificate=certificate,
            root_identity=identity,
            expected_schema=federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA,
            now=validated_at,
            required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
        federated_replica_attestation._attestation_semantics(
            verified,
            transfer=transfer,
            pinned_metadata=pinned_metadata,
            now=validated_at,
            max_future_skew_seconds=30,
        )
        return verified
    except (federation_identity.FederationIdentityError, federated_replica_attestation.FederatedReplicaAttestationError) as exc:
        code = getattr(exc, "code", "invalid")
        errors.append(f"replica-attestation-invalid:{code}")
        return attestation


def _validate_records(
    *,
    accepted: dict[str, Any],
    copy_record: dict[str, Any],
    attestation: dict[str, Any],
    transfer: dict[str, Any],
    pinned_metadata: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    attestation_digest = federated_replica_attestation.attestation_digest(attestation)
    if set(accepted) != _ACCEPTED_REPLICA_RECORD_FIELDS:
        errors.append("accepted-replica-record-fields-invalid")
    if (
        accepted.get("schema") != federation_peer_trust.REPLICA_ATTESTATION_RECORD_SCHEMA
        or accepted.get("peerFleetId") != transfer.get("destinationFleetId")
        or accepted.get("transferId") != transfer.get("transferId")
        or accepted.get("sequence") != attestation.get("sequence")
        or accepted.get("signerKeyId") != attestation.get("signerKeyId")
        or accepted.get("attestationDigest") != attestation_digest
        or accepted.get("attestation") != attestation
    ):
        errors.append("accepted-replica-record-binding-invalid")
    accepted_at = _parse_timestamp(accepted.get("acceptedAt"), "acceptedReplicaRecord.acceptedAt", errors)
    committed_at = _parse_timestamp(attestation.get("committedAt"), "replicaAttestation.committedAt", errors)
    if accepted_at is not None and committed_at is not None and (accepted_at < committed_at or accepted_at > validated_at):
        errors.append("accepted-replica-record-time-order-invalid")
    accepted_revision = accepted.get("revision")
    if isinstance(accepted_revision, bool) or not isinstance(accepted_revision, int) or accepted_revision < 1:
        errors.append("accepted-replica-record-revision-invalid")
    if set(copy_record) != federated_durability.FEDERATED_COPY_RECORD_FIELDS:
        errors.append("federated-copy-record-fields-invalid")
    if copy_record.get("recordDigest") != federated_durability._record_digest(copy_record):
        errors.append("federated-copy-record-digest-mismatch")
    recorded_at = _parse_timestamp(copy_record.get("recordedAt"), "federatedCopyRecord.recordedAt", errors)
    if recorded_at is not None and accepted_at is not None and (recorded_at < accepted_at or recorded_at > validated_at):
        errors.append("federated-copy-record-time-order-invalid")
    copy_revision = copy_record.get("revision")
    if isinstance(copy_revision, bool) or not isinstance(copy_revision, int) or copy_revision < 1:
        errors.append("federated-copy-record-revision-invalid")
    for field in (
        "transferId",
        "sourceFleetId",
        "destinationFleetId",
        "policyId",
        "backupId",
        "objectSetDigest",
    ):
        if copy_record.get(field) != transfer.get(field):
            errors.append(f"federated-copy-transfer-binding-mismatch:{field}")
    if (
        copy_record.get("status") != federated_durability.FEDERATED_COMMITTED
        or copy_record.get("localDurabilityCredit") is not False
        or copy_record.get("attestationDigest") != attestation_digest
        or copy_record.get("remoteTargetId") != attestation.get("remoteTargetId")
        or copy_record.get("remoteReceiptDigest") != attestation.get("remoteReceiptDigest")
        or copy_record.get("remoteCommitDigest") != attestation.get("remoteCommitDigest")
        or copy_record.get("committedAt") != attestation.get("committedAt")
        or copy_record.get("attestationSequence") != attestation.get("sequence")
        or copy_record.get("signerKeyId") != attestation.get("signerKeyId")
        or copy_record.get("attestationAcceptedAt") != accepted.get("acceptedAt")
        or copy_record.get("peerMetadata") != pinned_metadata
        or copy_record.get("failureDomain") != federated_replica_attestation.failure_domain_from_metadata(pinned_metadata)
    ):
        errors.append("federated-copy-semantic-binding-invalid")


def _validate_durability(
    before: dict[str, Any],
    after: dict[str, Any],
    status: dict[str, Any],
    transfer: dict[str, Any],
    errors: list[str],
) -> None:
    if before != after:
        errors.append("local-durability-regressed")
    for field in ("minCommittedCopies", "minFailureDomains"):
        value = before.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or after.get(field) != value:
            errors.append(f"local-durability-objective-invalid:{field}")
    if (
        status.get("schema") != federated_durability.FEDERATED_DURABILITY_STATUS_SCHEMA
        or status.get("satisfied") is not True
        or status.get("localDurabilityCredit") != 0
        or status.get("objectSetDigest") != transfer.get("objectSetDigest")
        or status.get("backupId") != transfer.get("backupId")
        or transfer.get("transferId") not in (status.get("creditedTransferIds") or [])
    ):
        errors.append("federated-durability-status-invalid")


def _validate_failures(
    value: Any,
    *,
    grant: dict[str, Any],
    transfer: dict[str, Any],
    identity: dict[str, Any],
    validated_at: datetime,
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
        if before and pre_state and before != _digest(pre_state):
            errors.append(f"failure-pre-state-digest-mismatch:{claim}")
        if after and post_state and after != _digest(post_state):
            errors.append(f"failure-post-state-digest-mismatch:{claim}")
        if before and after and (before != after or pre_state != post_state):
            errors.append(f"failure-mutated-state:{claim}")
        evidence = _as_dict(observation.get("input"), f"failure-input:{claim}", errors)
        if claim == "expiredIngressGrantCannotWrite":
            attempted = _parse_timestamp(evidence.get("attemptedAt"), "expiredGrant.attemptedAt", errors)
            expires = _parse_timestamp(grant.get("expiresAt"), "ingressGrant.expiresAt", errors)
            if evidence.get("grant") != grant or attempted is None or expires is None or attempted < expires:
                errors.append("expired-grant-evidence-invalid")
        elif claim == "ingressGrantCannotEscapeObjectPrefix":
            key = evidence.get("objectKey")
            if evidence.get("grant") != grant or type(key) is not str or key.startswith(str(grant.get("allowedObjectPrefix") or "")):
                errors.append("prefix-escape-evidence-invalid")
        elif claim == "ingressGrantCannotExceedMaxBytes":
            byte_count = evidence.get("byteCount")
            max_bytes = grant.get("maxBytes")
            if (
                evidence.get("grant") != grant
                or isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or isinstance(max_bytes, bool)
                or not isinstance(max_bytes, int)
                or byte_count <= max_bytes
            ):
                errors.append("max-bytes-evidence-invalid")
        elif claim == "sameTransferIdDifferentDigestFailsClosed":
            conflict = _typed_digest(evidence.get("conflictingObjectSetDigest"), "conflictingObjectSetDigest", errors)
            if evidence.get("transfer") != transfer or not conflict or conflict == transfer.get("objectSetDigest"):
                errors.append("transfer-conflict-evidence-invalid")
            else:
                try:
                    conflicting_transfer_id = federation_transfer.derive_transfer_id(
                        source_fleet_id=str(transfer.get("sourceFleetId") or ""),
                        destination_fleet_id=str(transfer.get("destinationFleetId") or ""),
                        backup_id=str(transfer.get("backupId") or ""),
                        object_set_digest=conflict,
                    )
                except federation_transfer.FederatedTransferError:
                    errors.append("transfer-conflict-evidence-invalid")
                else:
                    if conflicting_transfer_id == transfer.get("transferId"):
                        errors.append("transfer-conflict-evidence-invalid")
        elif claim == "replayedIngressGrantFailsClosed":
            if evidence.get("grant") != grant or evidence.get("replayedGrantId") != grant.get("grantId"):
                errors.append("grant-replay-evidence-invalid")
        elif claim == "tamperedReplicaAttestationFailsClosed":
            tampered = _as_dict(evidence.get("attestation"), "tampered-attestation", errors)
            certificate = _as_dict(tampered.get("signerCertificate"), "tampered-attestation-certificate", errors)
            if not tampered or not certificate:
                errors.append("tampered-attestation-evidence-invalid")
            else:
                try:
                    federation_identity.verify_federation_document(
                        tampered,
                        certificate=certificate,
                        root_identity=identity,
                        expected_schema=federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA,
                        now=validated_at,
                        required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
                    )
                except federation_identity.FederationIdentityError:
                    pass
                else:
                    errors.append("tampered-attestation-evidence-invalid")


def build_federated_replica_proof(
    *,
    validated_at: datetime,
    destination_fleet_identity: dict[str, Any],
    peer_trust_record: dict[str, Any],
    ingress_grant: dict[str, Any],
    receiver_transfer: dict[str, Any],
    sender_transfer: dict[str, Any],
    object_set_declaration: dict[str, Any],
    source_receipt: dict[str, Any],
    remote_receipt_bytes: bytes,
    remote_commit_bytes: bytes,
    replica_attestation: dict[str, Any],
    accepted_replica_record: dict[str, Any],
    federated_copy_record: dict[str, Any],
    local_durability_before: dict[str, Any],
    local_durability_after: dict[str, Any],
    federated_durability_status: dict[str, Any],
    failure_observations: Sequence[dict[str, Any]],
    wire_contracts: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": FEDERATED_REPLICA_PROOF_SCHEMA,
        "validatedAt": _utc_iso(validated_at),
        "destinationFleetIdentity": copy.deepcopy(destination_fleet_identity),
        "peerTrustRecord": copy.deepcopy(peer_trust_record),
        "ingressGrant": copy.deepcopy(ingress_grant),
        "receiverTransfer": copy.deepcopy(receiver_transfer),
        "senderTransfer": copy.deepcopy(sender_transfer),
        "objectSetDeclaration": copy.deepcopy(object_set_declaration),
        "sourceReceipt": copy.deepcopy(source_receipt),
        "remoteReceiptBytesBase64": base64.b64encode(remote_receipt_bytes).decode("ascii"),
        "remoteCommitBytesBase64": base64.b64encode(remote_commit_bytes).decode("ascii"),
        "replicaAttestation": copy.deepcopy(replica_attestation),
        "acceptedReplicaRecord": copy.deepcopy(accepted_replica_record),
        "federatedCopyRecord": copy.deepcopy(federated_copy_record),
        "localDurabilityBefore": copy.deepcopy(local_durability_before),
        "localDurabilityAfter": copy.deepcopy(local_durability_after),
        "federatedDurabilityStatus": copy.deepcopy(federated_durability_status),
        "failureObservations": [copy.deepcopy(item) for item in failure_observations],
        "wireContracts": copy.deepcopy(wire_contracts),
    }
    payload["proofDigest"] = proof_digest(payload)
    errors = validate_federated_replica_proof(payload)
    if errors:
        raise ValueError("invalid federated replica proof: " + "; ".join(errors))
    return payload


def validate_federated_replica_proof(value: Any) -> list[str]:
    """Recompute grant, storage wire, attestation, and durability boundaries."""

    errors: list[str] = []
    try:
        normalized = federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError:
        return ["federated-replica-proof-canonical-payload-invalid"]
    if type(normalized) is not dict:
        return ["federated-replica-proof-must-be-object"]
    if set(normalized) != _PROOF_FIELDS:
        errors.append("federated-replica-proof-fields-invalid")
    if normalized.get("schema") != FEDERATED_REPLICA_PROOF_SCHEMA:
        errors.append("federated-replica-proof-schema-invalid")
    try:
        federation_identity.assert_federation_document_secret_free(normalized)
    except federation_identity.FederationIdentityError:
        errors.append("federated-replica-proof-contains-secret")
    validated_at = _parse_timestamp(normalized.get("validatedAt"), "validatedAt", errors)
    try:
        identity = federation_identity.validate_fleet_identity(normalized.get("destinationFleetIdentity"))
    except federation_identity.FederationIdentityError as exc:
        errors.append(f"destination-identity-invalid:{exc.code}")
        identity = {}
    peer, pinned_metadata = _validate_peer_record(normalized.get("peerTrustRecord"), identity=identity, errors=errors)
    receiver = _as_dict(normalized.get("receiverTransfer"), "receiver-transfer", errors)
    sender = _as_dict(normalized.get("senderTransfer"), "sender-transfer", errors)
    _validate_transfer_pair(receiver, sender, errors)
    grant = _as_dict(normalized.get("ingressGrant"), "ingress-grant", errors)
    declaration = _as_dict(normalized.get("objectSetDeclaration"), "object-set-declaration", errors)
    source_receipt = _as_dict(normalized.get("sourceReceipt"), "source-receipt", errors)
    attestation = _as_dict(normalized.get("replicaAttestation"), "replica-attestation", errors)
    accepted = _as_dict(normalized.get("acceptedReplicaRecord"), "accepted-replica-record", errors)
    copy_record = _as_dict(normalized.get("federatedCopyRecord"), "federated-copy-record", errors)
    before = _as_dict(normalized.get("localDurabilityBefore"), "local-durability-before", errors)
    after = _as_dict(normalized.get("localDurabilityAfter"), "local-durability-after", errors)
    status = _as_dict(normalized.get("federatedDurabilityStatus"), "federated-durability-status", errors)
    receipt_bytes = _decode_document(normalized.get("remoteReceiptBytesBase64"), "remoteReceiptBytesBase64", errors)
    commit_bytes = _decode_document(normalized.get("remoteCommitBytesBase64"), "remoteCommitBytesBase64", errors)
    if validated_at is not None and identity and receiver and grant:
        _validate_grant(grant, identity=identity, transfer=receiver, validated_at=validated_at, errors=errors)
    if declaration and source_receipt and receipt_bytes and commit_bytes and sender and attestation:
        _validate_storage(
            declaration=declaration,
            source_receipt=source_receipt,
            receipt_bytes=receipt_bytes,
            commit_bytes=commit_bytes,
            transfer=sender,
            attestation=attestation,
            errors=errors,
        )
    verified_attestation = attestation
    if validated_at is not None and identity and sender and pinned_metadata and attestation:
        verified_attestation = _validate_attestation(
            attestation,
            identity=identity,
            transfer=sender,
            pinned_metadata=pinned_metadata,
            validated_at=validated_at,
            errors=errors,
        )
    if validated_at is not None and accepted and copy_record and verified_attestation and sender and pinned_metadata:
        _validate_records(
            accepted=accepted,
            copy_record=copy_record,
            attestation=verified_attestation,
            transfer=sender,
            pinned_metadata=pinned_metadata,
            validated_at=validated_at,
            errors=errors,
        )
    if before and after and status and sender:
        _validate_durability(before, after, status, sender, errors)
    wires = _as_dict(normalized.get("wireContracts"), "wire-contracts", errors)
    if wires != {
        "objectSet": backup_object_set.OBJECT_SET_V1,
        "receiptVersion": backup_publish.RECEIPT_SCHEMA_VERSION,
        "commitVersion": backup_publish.COMMIT_SCHEMA_VERSION,
        "fastCdc": "fastcdc-v3",
        "randomizedAge": True,
    }:
        errors.append("frozen-wire-contract-mismatch")
    if validated_at is not None and grant and receiver and identity:
        _validate_failures(
            normalized.get("failureObservations"),
            grant=grant,
            transfer=receiver,
            identity=identity,
            validated_at=validated_at,
            errors=errors,
        )
    declared_digest = _typed_digest(normalized.get("proofDigest"), "proofDigest", errors)
    if declared_digest and declared_digest != proof_digest(normalized):
        errors.append("proof-digest-mismatch")
    return list(dict.fromkeys(errors))
