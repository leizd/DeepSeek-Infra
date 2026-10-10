"""Signed receiver custody proof and independent sender-side verification."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    backup_target_store,
    federated_replica_commit,
    federation_identity,
    federation_peer_trust,
    federation_replica_receiver,
    federation_transfer,
    federation_transfer_journal,
)

REPLICA_ATTESTATION_SCHEMA = "federated-replica-attestation-v1"
MAX_REPLICA_ATTESTATION_LIFETIME_SECONDS = 300
MAX_REPLICA_ATTESTATION_BYTES = 256 * 1024
MAX_REMOTE_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_REMOTE_COMMIT_BYTES = 128 * 1024

REPLICA_ATTESTATION_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "transferId",
        "sourceFleetId",
        "destinationFleetId",
        "backupId",
        "objectSetDigest",
        "remoteTargetId",
        "remoteReceiptDigest",
        "remoteCommitDigest",
        "failureDomain",
        "committedAt",
        "sequence",
        "signerCertificate",
        "signedAt",
        "expiresAt",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLAIN_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_DOMAIN_PATTERN = re.compile(r"^federation-peer-domain:sha256:[0-9a-f]{64}$")
_REQUIRED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})
_LINEAGE_FIELDS = (
    "snapshotKind",
    "lineageId",
    "parentBackupId",
    "baseBackupId",
    "chainDepth",
    "chunkProtocol",
)


class FederatedReplicaAttestationError(RuntimeError):
    """Fail-closed signed replica proof error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID") from exc


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID") from exc


def attestation_digest(attestation: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(attestation).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID")
    return normalized


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FLEET_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaAttestationError(code)
    return value


def _typed_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _TYPED_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaAttestationError(code)
    return value


def _plain_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _PLAIN_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederatedReplicaAttestationError(code)
    return value


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SEQUENCE_INVALID")
    return value


def _metadata(value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != _REQUIRED_METADATA_FIELDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID")
    normalized: dict[str, str] = {}
    for field in sorted(_REQUIRED_METADATA_FIELDS):
        item = value.get(field)
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or len(item) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in item)
        ):
            raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID")
        normalized[field] = item
    return normalized


def failure_domain_from_metadata(metadata: dict[str, str]) -> str:
    """Derive credit from the exact operator-pinned metadata, never peer prose."""

    normalized = _metadata(metadata)
    return "federation-peer-domain:sha256:" + hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def _validate_window(signed_at: datetime, expires_at: datetime) -> None:
    lifetime = (expires_at - signed_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_REPLICA_ATTESTATION_LIFETIME_SECONDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID")


def _storage_document_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _parse_storage_document(raw: bytes, *, maximum_bytes: int, invalid_code: str, encoding_code: str) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise FederatedReplicaAttestationError(invalid_code)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FederatedReplicaAttestationError(invalid_code) from exc
    normalized = _normalize(parsed)
    if type(normalized) is not dict:
        raise FederatedReplicaAttestationError(invalid_code)
    if raw != _storage_document_bytes(normalized):
        raise FederatedReplicaAttestationError(encoding_code)
    return normalized


def _source_receipt_semantics(source_receipt: dict[str, Any], transfer: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(source_receipt)
    if type(normalized) is not dict or set(normalized) != federated_replica_commit.RECEIPT_V4_FIELDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_SOURCE_RECEIPT_V4_INVALID")
    storage_object_set_digest = _plain_digest(
        normalized.get("objectSetDigest"),
        code="FEDERATION_REPLICA_SOURCE_OBJECT_SET_DIGEST_INVALID",
    )
    control_digest = _plain_digest(
        normalized.get("controlObjectDigest"),
        code="FEDERATION_REPLICA_SOURCE_CONTROL_OBJECT_DIGEST_INVALID",
    )
    if (
        normalized.get("schemaVersion") != backup_publish.RECEIPT_SCHEMA_VERSION
        or normalized.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or normalized.get("backupId") != transfer["backupId"]
        or normalized.get("policyId") != transfer["policyId"]
        or normalized.get("creationVerified") is not True
        or "sha256:" + storage_object_set_digest != transfer["objectSetDigest"]
        or type(normalized.get("size")) is not int
        or isinstance(normalized.get("size"), bool)
        or int(normalized["size"]) <= 0
    ):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_SOURCE_RECEIPT_BINDING_INVALID")
    try:
        objects = backup_object_set.committed_object_inventory(normalized)
    except AppError as exc:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_SOURCE_OBJECT_INVENTORY_INVALID") from exc
    if sum(int(item["size"]) for item in objects) != int(normalized["size"]) or control_digest not in {
        str(item["digest"]) for item in objects
    }:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_SOURCE_OBJECT_INVENTORY_INVALID")
    return normalized


def _validate_remote_receipt(
    receipt: dict[str, Any],
    *,
    source_receipt: dict[str, Any],
    transfer: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    if set(receipt) != federated_replica_commit.RECEIPT_V4_FIELDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_REMOTE_RECEIPT_V4_INVALID")
    expected_run_id = federated_replica_commit.federated_run_id(str(transfer["transferId"]))
    expected_slot = federated_replica_commit.federated_schedule_slot(str(transfer["transferId"]))
    if receipt.get("targetId") != attestation["remoteTargetId"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_MISMATCH")
    if (
        receipt.get("schemaVersion") != backup_publish.RECEIPT_SCHEMA_VERSION
        or receipt.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or receipt.get("backupId") != transfer["backupId"]
        or receipt.get("policyId") != transfer["policyId"]
        or receipt.get("runId") != expected_run_id
        or receipt.get("scheduleSlot") != expected_slot
        or receipt.get("creationVerified") is not True
        or receipt.get("pinned") is not False
        or receipt.get("size") != source_receipt.get("size")
        or receipt.get("objectSetDigest") != source_receipt.get("objectSetDigest")
        or receipt.get("controlObjectDigest") != source_receipt.get("controlObjectDigest")
        or receipt.get("objects") != source_receipt.get("objects")
        or any(receipt.get(field) != source_receipt.get(field) for field in _LINEAGE_FIELDS)
    ):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_REMOTE_RECEIPT_BINDING_INVALID")
    created_at = _parse_timestamp(receipt.get("createdAt"))
    if created_at > _parse_timestamp(attestation.get("committedAt")):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_REMOTE_RECEIPT_TIMESTAMP_INVALID")


def _validate_remote_commit(
    commit: dict[str, Any],
    *,
    receipt_bytes: bytes,
    source_receipt: dict[str, Any],
    transfer: dict[str, Any],
) -> None:
    if set(commit) != federated_replica_commit.COMMIT_V4_FIELDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_REMOTE_COMMIT_V4_INVALID")
    transfer_id = str(transfer["transferId"])
    schedule_slot = federated_replica_commit.federated_schedule_slot(transfer_id)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    if (
        commit.get("schemaVersion") != backup_publish.COMMIT_SCHEMA_VERSION
        or commit.get("policyId") != transfer["policyId"]
        or commit.get("scheduleSlot") != schedule_slot
        or commit.get("slotDigest") != backup_target_store.commit_slot_digest(schedule_slot)
        or commit.get("runId") != federated_replica_commit.federated_run_id(transfer_id)
        or commit.get("backupId") != transfer["backupId"]
        or commit.get("storageProtocol") != backup_object_set.OBJECT_SET_V1
        or commit.get("objectSetDigest") != source_receipt.get("objectSetDigest")
        or commit.get("controlObjectDigest") != source_receipt.get("controlObjectDigest")
        or commit.get("receiptDigest") != receipt_digest
        or isinstance(commit.get("fencingToken"), bool)
        or not isinstance(commit.get("fencingToken"), int)
        or int(commit["fencingToken"]) < 1
        or isinstance(commit.get("targetGeneration"), bool)
        or not isinstance(commit.get("targetGeneration"), int)
        or int(commit["targetGeneration"]) < 1
        or _PLAIN_DIGEST_PATTERN.fullmatch(str(commit.get("previousCommitHash") or "")) is None
        or _PLAIN_DIGEST_PATTERN.fullmatch(str(commit.get("commitHash") or "")) is None
        or not backup_publish.commit_marker_valid(commit)
    ):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_REMOTE_COMMIT_BINDING_INVALID")


def _validate_remote_documents(
    *,
    source_receipt: dict[str, Any],
    remote_receipt_bytes: bytes,
    remote_commit_bytes: bytes,
    transfer: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    receipt = _parse_storage_document(
        remote_receipt_bytes,
        maximum_bytes=MAX_REMOTE_RECEIPT_BYTES,
        invalid_code="FEDERATION_REPLICA_REMOTE_RECEIPT_INVALID",
        encoding_code="FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID",
    )
    commit = _parse_storage_document(
        remote_commit_bytes,
        maximum_bytes=MAX_REMOTE_COMMIT_BYTES,
        invalid_code="FEDERATION_REPLICA_REMOTE_COMMIT_INVALID",
        encoding_code="FEDERATION_REPLICA_REMOTE_COMMIT_ENCODING_INVALID",
    )
    if "sha256:" + hashlib.sha256(remote_receipt_bytes).hexdigest() != attestation["remoteReceiptDigest"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_MISMATCH")
    if "sha256:" + hashlib.sha256(remote_commit_bytes).hexdigest() != attestation["remoteCommitDigest"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_MISMATCH")
    normalized_source = _source_receipt_semantics(source_receipt, transfer)
    _validate_remote_receipt(
        receipt,
        source_receipt=normalized_source,
        transfer=transfer,
        attestation=attestation,
    )
    _validate_remote_commit(
        commit,
        receipt_bytes=remote_receipt_bytes,
        source_receipt=normalized_source,
        transfer=transfer,
    )


def _require_local_signer(
    signer: federation_identity.OnlineFleetSigner,
    local_identity: dict[str, Any],
    *,
    signed_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    certificate = signer.certificate
    if (
        signer.fleet_id != local_identity.get("fleetId")
        or certificate.get("rootKeyId") != local_identity.get("rootKeyId")
        or certificate.get("rootFingerprint") != local_identity.get("rootFingerprint")
    ):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_LOCAL_SIGNER_MISMATCH")
    errors = federation_identity.validate_online_signer_certificate(
        certificate,
        local_identity,
        now=signed_at,
        required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    if errors:
        raise FederatedReplicaAttestationError(errors[0])
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if signed_at < certificate_not_before or expires_at > certificate_expires_at:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID")
    return certificate


def issue_replica_attestation(
    *,
    signer: federation_identity.OnlineFleetSigner,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    transfer_id: str,
    remote_target_id: str,
    failure_domain_metadata: dict[str, str],
    sequence: int,
    signed_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Re-read the durable provider effect, then sign its exact document digests."""

    normalized_sequence = _sequence(sequence)
    normalized_signed_at = _parse_timestamp(_utc_iso(signed_at))
    normalized_expires_at = _parse_timestamp(_utc_iso(expires_at))
    _validate_window(normalized_signed_at, normalized_expires_at)
    certificate = _require_local_signer(
        signer,
        receiver.transfer_journal.local_identity,
        signed_at=normalized_signed_at,
        expires_at=normalized_expires_at,
    )
    try:
        committed = federated_replica_commit.inspect_committed_federated_replica(
            receiver=receiver,
            transfer_id=transfer_id,
            target_id=_control_id(
                remote_target_id,
                code="FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_INVALID",
            ),
        )
        transfer = receiver.transfer_journal.get_transfer(transfer_id)
        events = receiver.transfer_journal.list_transfer_events(transfer_id)
    except (
        federated_replica_commit.FederatedReplicaCommitError,
        federation_transfer_journal.FederatedTransferJournalError,
    ) as exc:
        raise FederatedReplicaAttestationError(exc.code) from exc
    assert transfer is not None
    committed_event = next(
        event for event in events if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    )
    committed_at = _parse_timestamp(committed_event["occurredAt"])
    if committed_at > normalized_signed_at:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID")
    receipt_bytes = _storage_document_bytes(committed.receipt)
    commit_bytes = _storage_document_bytes(committed.commit)
    payload = {
        "schema": REPLICA_ATTESTATION_SCHEMA,
        "fleetId": signer.fleet_id,
        "transferId": transfer["transferId"],
        "sourceFleetId": transfer["sourceFleetId"],
        "destinationFleetId": transfer["destinationFleetId"],
        "backupId": transfer["backupId"],
        "objectSetDigest": transfer["objectSetDigest"],
        "remoteTargetId": committed.target_id,
        "remoteReceiptDigest": "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
        "remoteCommitDigest": "sha256:" + hashlib.sha256(commit_bytes).hexdigest(),
        "failureDomain": failure_domain_from_metadata(failure_domain_metadata),
        "committedAt": _utc_iso(committed_at),
        "sequence": normalized_sequence,
        "signerCertificate": certificate,
        "signedAt": _utc_iso(normalized_signed_at),
        "expiresAt": _utc_iso(normalized_expires_at),
    }
    try:
        attestation = federation_identity.sign_federation_document(
            signer,
            payload,
            purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
    except federation_identity.FederationIdentityError as exc:
        raise FederatedReplicaAttestationError(exc.code) from exc
    return attestation


def _verify_signature_and_trust(
    attestation: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    destination_fleet_id: str,
    now: datetime,
) -> dict[str, Any]:
    certificate = attestation.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SIGNER_CERTIFICATE_INVALID")
    peer = peer_registry.get_peer(destination_fleet_id)
    if peer is None:
        raise FederatedReplicaAttestationError("FEDERATION_PEER_NOT_PINNED")
    root_identity = peer.get("fleetIdentity")
    if type(root_identity) is not dict:
        raise FederatedReplicaAttestationError("FEDERATION_PEER_IDENTITY_INVALID")
    try:
        peer_registry.authorize_online_signer(
            destination_fleet_id,
            certificate,
            purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=now,
        )
        return federation_identity.verify_federation_document(
            attestation,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=REPLICA_ATTESTATION_SCHEMA,
            now=now,
            required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederatedReplicaAttestationError(exc.code) from exc


def _attestation_semantics(
    attestation: dict[str, Any],
    *,
    transfer: dict[str, Any],
    pinned_metadata: dict[str, str],
    now: datetime,
    max_future_skew_seconds: int,
) -> tuple[int, datetime]:
    source = _fleet_id(attestation.get("sourceFleetId"))
    destination = _fleet_id(attestation.get("destinationFleetId"))
    if source != transfer["sourceFleetId"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SOURCE_FLEET_MISMATCH")
    if destination != transfer["destinationFleetId"] or attestation.get("fleetId") != destination:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_DESTINATION_FLEET_MISMATCH")
    transfer_id = _typed_digest(
        attestation.get("transferId"),
        code="FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_INVALID",
    )
    if transfer_id != transfer["transferId"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_MISMATCH")
    backup_id = _control_id(
        attestation.get("backupId"),
        code="FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_INVALID",
    )
    if backup_id != transfer["backupId"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_MISMATCH")
    object_set_digest = _typed_digest(
        attestation.get("objectSetDigest"),
        code="FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_INVALID",
    )
    if object_set_digest != transfer["objectSetDigest"]:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_MISMATCH")
    derived_transfer_id = federation_transfer.derive_transfer_id(
        source_fleet_id=source,
        destination_fleet_id=destination,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )
    if transfer_id != derived_transfer_id:
        raise FederatedReplicaAttestationError("FEDERATION_TRANSFER_ID_INVALID")
    _control_id(
        attestation.get("remoteTargetId"),
        code="FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_INVALID",
    )
    _typed_digest(
        attestation.get("remoteReceiptDigest"),
        code="FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_INVALID",
    )
    _typed_digest(
        attestation.get("remoteCommitDigest"),
        code="FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_INVALID",
    )
    failure_domain = attestation.get("failureDomain")
    if type(failure_domain) is not str or _FAILURE_DOMAIN_PATTERN.fullmatch(failure_domain) is None:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_INVALID")
    if failure_domain != failure_domain_from_metadata(pinned_metadata):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_MISMATCH")
    sequence = _sequence(attestation.get("sequence"))
    signed_at = _parse_timestamp(attestation.get("signedAt"))
    expires_at = _parse_timestamp(attestation.get("expiresAt"))
    committed_at = _parse_timestamp(attestation.get("committedAt"))
    _validate_window(signed_at, expires_at)
    if now >= expires_at:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_EXPIRED")
    if (signed_at - now).total_seconds() > max_future_skew_seconds:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FROM_FUTURE")
    if committed_at > signed_at:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID")
    certificate = attestation.get("signerCertificate")
    assert isinstance(certificate, dict)
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if signed_at < certificate_not_before or expires_at > certificate_expires_at:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID")
    return sequence, committed_at


def _sender_remote_commit_details(attestation: dict[str, Any]) -> dict[str, Any]:
    return {
        "targetId": attestation["remoteTargetId"],
        "objectSetDigest": attestation["objectSetDigest"],
        "remoteReceiptDigest": attestation["remoteReceiptDigest"],
        "remoteCommitDigest": attestation["remoteCommitDigest"],
        "attestationDigest": attestation_digest(attestation),
        "attestationSequence": attestation["sequence"],
        "signerKeyId": attestation["signerKeyId"],
        "failureDomain": attestation["failureDomain"],
        "committedAt": attestation["committedAt"],
    }


def _require_sender_state(
    journal: federation_transfer_journal.FederatedTransferJournal,
    transfer: dict[str, Any],
    expected_details: dict[str, Any],
) -> None:
    state = str(transfer["state"])
    minimum = federation_transfer_journal.TRANSFER_STATES.index(
        federation_transfer_journal.STATE_REMOTE_VERIFYING
    )
    if federation_transfer_journal.TRANSFER_STATES.index(state) < minimum:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SENDER_STATE_INVALID")
    if state == federation_transfer_journal.STATE_REMOTE_VERIFYING:
        return
    committed_events = [
        event
        for event in journal.list_transfer_events(str(transfer["transferId"]))
        if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    ]
    if len(committed_events) != 1 or committed_events[0]["stateDetails"] != expected_details:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_LOCAL_RECORD_CONFLICT")


def verify_and_record_replica_attestation(
    attestation: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    source_receipt: dict[str, Any],
    remote_receipt_bytes: bytes,
    remote_commit_bytes: bytes,
    now: datetime,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Verify trust and storage documents before recording sender custody state."""

    current = _parse_timestamp(_utc_iso(now))
    normalized = _normalize(attestation)
    if type(normalized) is not dict:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_INVALID")
    if len(_canonical_json(normalized).encode("utf-8")) > MAX_REPLICA_ATTESTATION_BYTES:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_TOO_LARGE")
    if set(normalized) != REPLICA_ATTESTATION_FIELDS:
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_FIELDS_INVALID")
    transfer_id = _typed_digest(
        normalized.get("transferId"),
        code="FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_INVALID",
    )
    transfer = sender_journal.get_transfer(transfer_id)
    if transfer is None:
        raise FederatedReplicaAttestationError("FEDERATION_TRANSFER_NOT_FOUND")
    local_identity = sender_journal.local_identity
    if (
        transfer.get("role") != federation_transfer_journal.ROLE_SENDER
        or transfer.get("localFleetId") != local_identity.get("fleetId")
        or transfer.get("sourceFleetId") != local_identity.get("fleetId")
        or peer_registry.local_identity != local_identity
    ):
        raise FederatedReplicaAttestationError("FEDERATION_REPLICA_ATTESTATION_SENDER_IDENTITY_MISMATCH")
    destination = str(transfer["destinationFleetId"])
    verified = _verify_signature_and_trust(
        normalized,
        peer_registry=peer_registry,
        destination_fleet_id=destination,
        now=current,
    )
    try:
        peer = peer_registry.require_active_peer(destination)
    except federation_peer_trust.FederationTrustError as exc:
        raise FederatedReplicaAttestationError(exc.code) from exc
    pinned_metadata = peer.get("pinnedMetadata")
    if type(pinned_metadata) is not dict:
        raise FederatedReplicaAttestationError("FEDERATION_PEER_METADATA_INVALID")
    sequence, _committed_at = _attestation_semantics(
        verified,
        transfer=transfer,
        pinned_metadata=pinned_metadata,
        now=current,
        max_future_skew_seconds=max(0, int(max_future_skew_seconds)),
    )
    _validate_remote_documents(
        source_receipt=source_receipt,
        remote_receipt_bytes=remote_receipt_bytes,
        remote_commit_bytes=remote_commit_bytes,
        transfer=transfer,
        attestation=verified,
    )
    expected_details = _sender_remote_commit_details(verified)
    _require_sender_state(sender_journal, transfer, expected_details)
    digest = attestation_digest(verified)
    try:
        peer_registry.record_replica_attestation(
            destination,
            signer_key_id=str(verified["signerKeyId"]),
            transfer_id=transfer_id,
            sequence=sequence,
            attestation_digest=digest,
            attestation=verified,
            accepted_at=current,
        )
        if transfer["state"] in {
            federation_transfer_journal.STATE_REMOTE_VERIFYING,
            federation_transfer_journal.STATE_REMOTE_COMMITTED,
        }:
            sender_journal.advance_transfer(
                transfer_id,
                expected_revision=int(transfer["revision"]),
                next_state=federation_transfer_journal.STATE_REMOTE_COMMITTED,
                details=expected_details,
                now=current,
            )
    except (federation_peer_trust.FederationTrustError, federation_transfer_journal.FederatedTransferJournalError) as exc:
        raise FederatedReplicaAttestationError(exc.code) from exc
    return copy.deepcopy(verified)
