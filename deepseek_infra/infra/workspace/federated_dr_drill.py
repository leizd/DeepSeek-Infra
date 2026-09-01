"""Production federated restore drill and signed semantic attestation."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_publish,
    backup_recovery_credential,
    backup_recovery_drill,
    backup_remote_restore,
    backup_target_store,
    federated_replica_attestation,
    federated_replica_commit,
    federation_custody_capability,
    federation_identity,
    federation_peer_trust,
    federation_replica_receiver,
    federation_transfer,
    federation_transfer_journal,
)

DR_DRILL_ATTESTATION_SCHEMA = "federated-dr-drill-attestation-v1"
PRODUCTION_RESTORE_PATH = "backup-recovery-drill-production-v1"
MAX_DR_ATTESTATION_LIFETIME_SECONDS = 300
DEFAULT_DR_ATTESTATION_LIFETIME_SECONDS = 300
MAX_DR_ATTESTATION_BYTES = 256 * 1024
MAX_DR_RTO_MS = 7 * 24 * 60 * 60 * 1_000

DR_DRILL_ATTESTATION_FIELDS = frozenset(
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
        "replicaAttestationDigest",
        "restoreId",
        "restorePath",
        "workspaceDigest",
        "sourceRevision",
        "startedAt",
        "completedAt",
        "rtoMs",
        "cleanupCompleted",
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
_RESTORE_ID_PATTERN = re.compile(r"^restore_[A-Za-z0-9]{1,64}$")
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SOURCE_REVISION_BYTES = 512


class FederatedDrDrillError(RuntimeError):
    """Fail-closed federated DR error with a stable machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDrDrillError("FEDERATED_DR_CANONICAL_PAYLOAD_INVALID") from exc


def _canonical_json(value: Any) -> str:
    try:
        return federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDrDrillError("FEDERATED_DR_CANONICAL_PAYLOAD_INVALID") from exc


def attestation_digest(attestation: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(attestation).encode("utf-8")).hexdigest()


def replica_attestation_digest(attestation: dict[str, Any]) -> str:
    return federated_replica_attestation.attestation_digest(attestation)


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederatedDrDrillError("FEDERATED_DR_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederatedDrDrillError("FEDERATED_DR_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederatedDrDrillError("FEDERATED_DR_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederatedDrDrillError("FEDERATED_DR_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederatedDrDrillError("FEDERATED_DR_TIMESTAMP_INVALID")
    return normalized


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederatedDrDrillError("FEDERATED_DR_FLEET_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederatedDrDrillError(code)
    return value


def _restore_id(value: Any) -> str:
    if type(value) is not str or _RESTORE_ID_PATTERN.fullmatch(value) is None:
        raise FederatedDrDrillError("FEDERATED_DR_RESTORE_ID_INVALID")
    return value


def _typed_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _TYPED_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederatedDrDrillError(code)
    return value


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_SEQUENCE_INVALID")
    return value


def _rto(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_DR_RTO_MS:
        raise FederatedDrDrillError("FEDERATED_DR_RTO_INVALID")
    return value


def _source_revision(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_SOURCE_REVISION_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise FederatedDrDrillError("FEDERATED_DR_SOURCE_REVISION_INVALID")
    return value


def _validate_window(signed_at: datetime, expires_at: datetime) -> None:
    lifetime = (expires_at - signed_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_DR_ATTESTATION_LIFETIME_SECONDS:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_LIFETIME_INVALID")


def _inspect_durable_remote_documents(
    *,
    transfer: dict[str, Any],
    remote_target_id: str,
    replica_attestation: dict[str, Any],
) -> tuple[bytes, bytes]:
    """Re-read Receipt v4 and Commit v4 without relying on transfer staging."""

    try:
        target = backup_publish.resolve_target(remote_target_id, write_intent=False)
        if (
            not isinstance(target, backup_publish.ResolvedTarget)
            or target.target_id != remote_target_id
            or target.kind != "s3"
            or target.root is not None
            or target.store is None
        ):
            raise FederatedDrDrillError("FEDERATED_DR_PROVIDER_TARGET_REQUIRED")
        store = target.require_store()
        receipt_bytes = store.get_bytes(backup_target_store.receipt_key(str(transfer["backupId"])))
        if receipt_bytes is None:
            raise FederatedDrDrillError("FEDERATED_DR_REMOTE_RECEIPT_MISSING")
        commit_bytes: bytes | None = None
        schedule_slot = federated_replica_commit.federated_schedule_slot(str(transfer["transferId"]))
        for key in backup_target_store.commit_marker_keys(str(transfer["policyId"]), schedule_slot):
            commit_bytes = store.get_bytes(key)
            if commit_bytes is not None:
                break
        if commit_bytes is None:
            raise FederatedDrDrillError("FEDERATED_DR_REMOTE_COMMIT_MISSING")
    except FederatedDrDrillError:
        raise
    except Exception as exc:
        raise FederatedDrDrillError("FEDERATED_DR_REMOTE_TARGET_UNAVAILABLE") from exc
    try:
        source_receipt = federated_replica_attestation._parse_storage_document(
            receipt_bytes,
            maximum_bytes=federated_replica_attestation.MAX_REMOTE_RECEIPT_BYTES,
            invalid_code="FEDERATED_DR_REMOTE_RECEIPT_INVALID",
            encoding_code="FEDERATED_DR_REMOTE_RECEIPT_ENCODING_INVALID",
        )
        federated_replica_attestation._validate_remote_documents(
            source_receipt=source_receipt,
            remote_receipt_bytes=receipt_bytes,
            remote_commit_bytes=commit_bytes,
            transfer=transfer,
            attestation=replica_attestation,
        )
    except federated_replica_attestation.FederatedReplicaAttestationError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    return receipt_bytes, commit_bytes


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
        raise FederatedDrDrillError("FEDERATED_DR_LOCAL_SIGNER_MISMATCH")
    errors = federation_identity.validate_online_signer_certificate(
        certificate,
        local_identity,
        now=signed_at,
        required_purpose=federation_identity.PURPOSE_DR_ATTESTATION,
    )
    if errors:
        raise FederatedDrDrillError(errors[0])
    not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expiry = _parse_timestamp(certificate.get("expiresAt"))
    if signed_at < not_before or expires_at > certificate_expiry:
        raise FederatedDrDrillError("FEDERATED_DR_SIGNER_WINDOW_INVALID")
    return certificate


def _require_local_registry_identity(
    *,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    custody_registry: federation_custody_capability.FederationCustodyCapabilityRegistry,
) -> dict[str, Any]:
    local_identity = receiver.transfer_journal.local_identity
    if peer_registry.local_identity != local_identity or custody_registry.local_identity != local_identity:
        raise FederatedDrDrillError("FEDERATED_DR_LOCAL_IDENTITY_MISMATCH")
    return local_identity


def _cleanup_restore_session(restore_id: str | None) -> None:
    if restore_id is None:
        return
    try:
        backup_crypto.clear_secret(restore_id)
        session = backup_remote_restore.read_restore_session(restore_id)
        if session is None:
            return
        backup_remote_restore._release_session_holds(session)
    except FederatedDrDrillError:
        raise
    except Exception as exc:
        raise FederatedDrDrillError("FEDERATED_DR_CLEANUP_INCOMPLETE") from exc


def _local_replica_binding(
    replica_attestation: dict[str, Any],
    *,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    transfer_id: str,
    remote_target_id: str,
    local_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = _normalize(replica_attestation)
    if type(normalized) is not dict or set(normalized) != federated_replica_attestation.REPLICA_ATTESTATION_FIELDS:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_INVALID")
    certificate = normalized.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_INVALID")
    replica_signed_at = _parse_timestamp(normalized.get("signedAt"))
    try:
        verified = federation_identity.verify_federation_document(
            normalized,
            certificate=certificate,
            root_identity=local_identity,
            expected_schema=federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA,
            now=replica_signed_at,
            required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
        transfer = receiver.transfer_journal.get_transfer(transfer_id)
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    if transfer is None:
        raise FederatedDrDrillError("FEDERATION_TRANSFER_NOT_FOUND")
    source_fleet_id = _fleet_id(verified.get("sourceFleetId"))
    destination_fleet_id = _fleet_id(verified.get("destinationFleetId"))
    replica_transfer_id = _typed_digest(
        verified.get("transferId"),
        code="FEDERATED_DR_REPLICA_TRANSFER_ID_INVALID",
    )
    backup_id = _control_id(verified.get("backupId"), code="FEDERATED_DR_REPLICA_BACKUP_ID_INVALID")
    object_set_digest = _typed_digest(
        verified.get("objectSetDigest"),
        code="FEDERATED_DR_REPLICA_OBJECT_SET_DIGEST_INVALID",
    )
    if replica_transfer_id != federation_transfer.derive_transfer_id(
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    ):
        raise FederatedDrDrillError("FEDERATION_TRANSFER_ID_INVALID")
    replica_expires_at = _parse_timestamp(verified.get("expiresAt"))
    committed_at = _parse_timestamp(verified.get("committedAt"))
    try:
        federated_replica_attestation._validate_window(replica_signed_at, replica_expires_at)
    except federated_replica_attestation.FederatedReplicaAttestationError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    if committed_at > replica_signed_at:
        raise FederatedDrDrillError("FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID")
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if replica_signed_at < certificate_not_before or replica_expires_at > certificate_expires_at:
        raise FederatedDrDrillError("FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID")
    if (
        verified.get("fleetId") != local_identity.get("fleetId")
        or verified.get("destinationFleetId") != local_identity.get("fleetId")
        or verified.get("sourceFleetId") != transfer.get("sourceFleetId")
        or verified.get("transferId") != transfer.get("transferId")
        or verified.get("backupId") != transfer.get("backupId")
        or verified.get("objectSetDigest") != transfer.get("objectSetDigest")
        or verified.get("remoteTargetId") != remote_target_id
    ):
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_BINDING_INVALID")
    _sequence(verified.get("sequence"))
    return transfer, verified


def _drill_evidence(
    result: Any,
    *,
    restore_id: str,
    committed_at: datetime,
    signed_at: datetime,
) -> dict[str, Any]:
    normalized = _normalize(result)
    if type(normalized) is not dict or normalized.get("result") != "success":
        raise FederatedDrDrillError("FEDERATED_DR_PRODUCTION_RESTORE_FAILED")
    if normalized.get("restoreId") != restore_id:
        raise FederatedDrDrillError("FEDERATED_DR_RESTORE_ID_MISMATCH")
    if normalized.get("cleanupCompleted") is not True:
        raise FederatedDrDrillError("FEDERATED_DR_CLEANUP_INCOMPLETE")
    workspace_digest = _typed_digest(
        normalized.get("workspaceDigest"),
        code="FEDERATED_DR_WORKSPACE_DIGEST_INVALID",
    )
    source_revision = _source_revision(normalized.get("sourceRevision"))
    started_at = _parse_timestamp(normalized.get("startedAt"))
    completed_at = _parse_timestamp(normalized.get("completedAt"))
    rto_ms = _rto(normalized.get("durationMs"))
    elapsed_ms = int((completed_at - started_at).total_seconds() * 1_000)
    if started_at < committed_at or completed_at < started_at or completed_at > signed_at:
        raise FederatedDrDrillError("FEDERATED_DR_TIME_BINDING_INVALID")
    if abs(rto_ms - elapsed_ms) > 1_000:
        raise FederatedDrDrillError("FEDERATED_DR_RTO_TIME_MISMATCH")
    return {
        "restoreId": restore_id,
        "workspaceDigest": workspace_digest,
        "sourceRevision": source_revision,
        "startedAt": _utc_iso(started_at),
        "completedAt": _utc_iso(completed_at),
        "rtoMs": rto_ms,
        "cleanupCompleted": True,
    }


def run_federated_dr_drill(
    *,
    signer: federation_identity.OnlineFleetSigner,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    custody_registry: federation_custody_capability.FederationCustodyCapabilityRegistry,
    replica_attestation: dict[str, Any],
    transfer_id: str,
    remote_target_id: str,
    sequence: int,
    signed_at: datetime | None = None,
    expires_at: datetime | None = None,
    client: Any | None = None,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Restore a committed federated replica through the production drill path."""

    normalized_transfer_id = _typed_digest(transfer_id, code="FEDERATED_DR_TRANSFER_ID_INVALID")
    normalized_target = _control_id(remote_target_id, code="FEDERATED_DR_REMOTE_TARGET_INVALID")
    normalized_sequence = _sequence(sequence)
    preflight_signed_at = _parse_timestamp(_utc_iso(signed_at if signed_at is not None else clock()))
    preflight_expires_at = (
        _parse_timestamp(_utc_iso(expires_at))
        if expires_at is not None
        else preflight_signed_at + timedelta(seconds=DEFAULT_DR_ATTESTATION_LIFETIME_SECONDS)
    )
    _validate_window(preflight_signed_at, preflight_expires_at)
    local_identity = _require_local_registry_identity(
        receiver=receiver,
        peer_registry=peer_registry,
        custody_registry=custody_registry,
    )
    _require_local_signer(
        signer,
        local_identity,
        signed_at=preflight_signed_at,
        expires_at=preflight_expires_at,
    )
    transfer, verified_replica = _local_replica_binding(
        replica_attestation,
        receiver=receiver,
        transfer_id=normalized_transfer_id,
        remote_target_id=normalized_target,
        local_identity=local_identity,
    )
    source_fleet_id = _fleet_id(transfer.get("sourceFleetId"))
    destination_fleet_id = _fleet_id(transfer.get("destinationFleetId"))
    if destination_fleet_id != local_identity.get("fleetId") or transfer.get("role") != federation_transfer_journal.ROLE_RECEIVER:
        raise FederatedDrDrillError("FEDERATED_DR_RECEIVER_IDENTITY_MISMATCH")
    backup_id = _control_id(transfer.get("backupId"), code="FEDERATED_DR_BACKUP_ID_INVALID")
    object_set_digest = _typed_digest(
        transfer.get("objectSetDigest"),
        code="FEDERATED_DR_OBJECT_SET_DIGEST_INVALID",
    )
    try:
        peer_registry.require_active_peer(source_fleet_id)
    except federation_peer_trust.FederationTrustError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    _inspect_durable_remote_documents(
        transfer=transfer,
        remote_target_id=normalized_target,
        replica_attestation=verified_replica,
    )
    slot_secret: bytearray | None = None
    restore_id: str | None = None
    try:
        with custody_registry.open_recovery_identity(peer_registry, source_fleet_id) as identity:
            slot_secret = bytearray(identity)
        created = backup_remote_restore.create_restore_from_target(
            target_id=normalized_target,
            backup_id=backup_id,
            client=client,
        )
        restore_id = _restore_id(created.get("restoreId") if isinstance(created, dict) else None)
        owned_secret = slot_secret
        assert owned_secret is not None
        slot_secret = None
        try:
            backup_crypto.put_secret_bytes(restore_id, "age-identity", owned_secret)
        except BaseException:
            backup_recovery_credential.zeroize(owned_secret)
            raise
        result = backup_recovery_drill.run_recovery_drill(restore_id, client=client)
    except federation_custody_capability.FederationCustodyCapabilityError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    except FederatedDrDrillError:
        raise
    except AppError as exc:
        raise FederatedDrDrillError("FEDERATED_DR_PRODUCTION_RESTORE_FAILED") from exc
    except Exception as exc:
        raise FederatedDrDrillError("FEDERATED_DR_PRODUCTION_RESTORE_FAILED") from exc
    finally:
        backup_recovery_credential.zeroize(slot_secret)
        _cleanup_restore_session(restore_id)
    assert restore_id is not None
    _inspect_durable_remote_documents(
        transfer=transfer,
        remote_target_id=normalized_target,
        replica_attestation=verified_replica,
    )
    try:
        peer_registry.require_active_peer(source_fleet_id)
    except federation_peer_trust.FederationTrustError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    normalized_signed_at = preflight_signed_at if signed_at is not None else _parse_timestamp(_utc_iso(clock()))
    normalized_expires_at = (
        _parse_timestamp(_utc_iso(expires_at))
        if expires_at is not None
        else normalized_signed_at + timedelta(seconds=DEFAULT_DR_ATTESTATION_LIFETIME_SECONDS)
    )
    _validate_window(normalized_signed_at, normalized_expires_at)
    certificate = _require_local_signer(
        signer,
        local_identity,
        signed_at=normalized_signed_at,
        expires_at=normalized_expires_at,
    )
    committed_at = _parse_timestamp(verified_replica.get("committedAt"))
    evidence = _drill_evidence(
        result,
        restore_id=restore_id,
        committed_at=committed_at,
        signed_at=normalized_signed_at,
    )
    payload = {
        "schema": DR_DRILL_ATTESTATION_SCHEMA,
        "fleetId": signer.fleet_id,
        "transferId": normalized_transfer_id,
        "sourceFleetId": source_fleet_id,
        "destinationFleetId": destination_fleet_id,
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
        "remoteTargetId": normalized_target,
        "remoteReceiptDigest": verified_replica["remoteReceiptDigest"],
        "remoteCommitDigest": verified_replica["remoteCommitDigest"],
        "replicaAttestationDigest": replica_attestation_digest(verified_replica),
        **evidence,
        "restorePath": PRODUCTION_RESTORE_PATH,
        "sequence": normalized_sequence,
        "signerCertificate": certificate,
        "signedAt": _utc_iso(normalized_signed_at),
        "expiresAt": _utc_iso(normalized_expires_at),
    }
    try:
        return federation_identity.sign_federation_document(
            signer,
            payload,
            purpose=federation_identity.PURPOSE_DR_ATTESTATION,
        )
    except federation_identity.FederationIdentityError as exc:
        raise FederatedDrDrillError(exc.code) from exc


def _verify_signature_and_trust(
    attestation: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    destination_fleet_id: str,
    now: datetime,
) -> dict[str, Any]:
    certificate = attestation.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederatedDrDrillError("FEDERATED_DR_SIGNER_CERTIFICATE_INVALID")
    peer = peer_registry.get_peer(destination_fleet_id)
    if peer is None:
        raise FederatedDrDrillError("FEDERATION_PEER_NOT_PINNED")
    root_identity = peer.get("fleetIdentity")
    if type(root_identity) is not dict:
        raise FederatedDrDrillError("FEDERATION_PEER_IDENTITY_INVALID")
    try:
        peer_registry.authorize_online_signer(
            destination_fleet_id,
            certificate,
            purpose=federation_identity.PURPOSE_DR_ATTESTATION,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=now,
        )
        return federation_identity.verify_federation_document(
            attestation,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=DR_DRILL_ATTESTATION_SCHEMA,
            now=now,
            required_purpose=federation_identity.PURPOSE_DR_ATTESTATION,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederatedDrDrillError(exc.code) from exc


def _sender_semantics(
    attestation: dict[str, Any],
    *,
    transfer: dict[str, Any],
    replica_record: dict[str, Any],
    now: datetime,
    max_future_skew_seconds: int,
) -> int:
    source = _fleet_id(attestation.get("sourceFleetId"))
    destination = _fleet_id(attestation.get("destinationFleetId"))
    if source != transfer.get("sourceFleetId"):
        raise FederatedDrDrillError("FEDERATED_DR_SOURCE_FLEET_MISMATCH")
    if destination != transfer.get("destinationFleetId") or attestation.get("fleetId") != destination:
        raise FederatedDrDrillError("FEDERATED_DR_DESTINATION_FLEET_MISMATCH")
    transfer_id = _typed_digest(attestation.get("transferId"), code="FEDERATED_DR_TRANSFER_ID_INVALID")
    if transfer_id != transfer.get("transferId"):
        raise FederatedDrDrillError("FEDERATED_DR_TRANSFER_ID_MISMATCH")
    backup_id = _control_id(attestation.get("backupId"), code="FEDERATED_DR_BACKUP_ID_INVALID")
    if backup_id != transfer.get("backupId"):
        raise FederatedDrDrillError("FEDERATED_DR_BACKUP_ID_MISMATCH")
    object_set_digest = _typed_digest(
        attestation.get("objectSetDigest"),
        code="FEDERATED_DR_OBJECT_SET_DIGEST_INVALID",
    )
    if object_set_digest != transfer.get("objectSetDigest"):
        raise FederatedDrDrillError("FEDERATED_DR_OBJECT_SET_DIGEST_MISMATCH")
    if transfer_id != federation_transfer.derive_transfer_id(
        source_fleet_id=source,
        destination_fleet_id=destination,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    ):
        raise FederatedDrDrillError("FEDERATION_TRANSFER_ID_INVALID")
    replica = replica_record.get("attestation")
    if type(replica) is not dict:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_NOT_ACCEPTED")
    expected_replica_digest = replica_record.get("attestationDigest")
    try:
        observed_replica_digest = replica_attestation_digest(replica)
    except federated_replica_attestation.FederatedReplicaAttestationError as exc:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID") from exc
    if (
        replica_record.get("peerFleetId") != destination
        or replica_record.get("transferId") != transfer_id
        or expected_replica_digest != observed_replica_digest
        or replica.get("fleetId") != destination
        or replica.get("sourceFleetId") != source
        or replica.get("destinationFleetId") != destination
        or replica.get("transferId") != transfer_id
        or replica.get("backupId") != backup_id
        or replica.get("objectSetDigest") != object_set_digest
    ):
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID")
    if attestation.get("replicaAttestationDigest") != expected_replica_digest:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_MISMATCH")
    if attestation.get("remoteTargetId") != replica.get("remoteTargetId"):
        raise FederatedDrDrillError("FEDERATED_DR_REMOTE_TARGET_MISMATCH")
    if attestation.get("remoteReceiptDigest") != replica.get("remoteReceiptDigest"):
        raise FederatedDrDrillError("FEDERATED_DR_REMOTE_RECEIPT_MISMATCH")
    if attestation.get("remoteCommitDigest") != replica.get("remoteCommitDigest"):
        raise FederatedDrDrillError("FEDERATED_DR_REMOTE_COMMIT_MISMATCH")
    for field, code in (
        ("remoteReceiptDigest", "FEDERATED_DR_REMOTE_RECEIPT_DIGEST_INVALID"),
        ("remoteCommitDigest", "FEDERATED_DR_REMOTE_COMMIT_DIGEST_INVALID"),
        ("replicaAttestationDigest", "FEDERATED_DR_REPLICA_ATTESTATION_DIGEST_INVALID"),
    ):
        _typed_digest(attestation.get(field), code=code)
    _control_id(attestation.get("remoteTargetId"), code="FEDERATED_DR_REMOTE_TARGET_INVALID")
    _restore_id(attestation.get("restoreId"))
    if attestation.get("restorePath") != PRODUCTION_RESTORE_PATH:
        raise FederatedDrDrillError("FEDERATED_DR_RESTORE_PATH_INVALID")
    _typed_digest(attestation.get("workspaceDigest"), code="FEDERATED_DR_WORKSPACE_DIGEST_INVALID")
    _source_revision(attestation.get("sourceRevision"))
    if attestation.get("cleanupCompleted") is not True:
        raise FederatedDrDrillError("FEDERATED_DR_CLEANUP_INCOMPLETE")
    rto_ms = _rto(attestation.get("rtoMs"))
    started_at = _parse_timestamp(attestation.get("startedAt"))
    completed_at = _parse_timestamp(attestation.get("completedAt"))
    signed_at = _parse_timestamp(attestation.get("signedAt"))
    expires_at = _parse_timestamp(attestation.get("expiresAt"))
    committed_at = _parse_timestamp(replica.get("committedAt"))
    _validate_window(signed_at, expires_at)
    if now >= expires_at:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_EXPIRED")
    if (signed_at - now).total_seconds() > max_future_skew_seconds:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_FROM_FUTURE")
    if started_at < committed_at or completed_at < started_at or signed_at < completed_at:
        raise FederatedDrDrillError("FEDERATED_DR_TIME_BINDING_INVALID")
    elapsed_ms = int((completed_at - started_at).total_seconds() * 1_000)
    if abs(rto_ms - elapsed_ms) > 1_000:
        raise FederatedDrDrillError("FEDERATED_DR_RTO_TIME_MISMATCH")
    certificate = attestation.get("signerCertificate")
    assert isinstance(certificate, dict)
    if signed_at < _parse_timestamp(certificate.get("notBefore")) or expires_at > _parse_timestamp(certificate.get("expiresAt")):
        raise FederatedDrDrillError("FEDERATED_DR_SIGNER_WINDOW_INVALID")
    return _sequence(attestation.get("sequence"))


def verify_and_record_dr_drill_attestation(
    attestation: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    sender_journal: federation_transfer_journal.FederatedTransferJournal,
    now: datetime,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Verify signed DR semantics and bind them to an accepted remote replica."""

    current = _parse_timestamp(_utc_iso(now))
    normalized = _normalize(attestation)
    if type(normalized) is not dict:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_INVALID")
    if len(_canonical_json(normalized).encode("utf-8")) > MAX_DR_ATTESTATION_BYTES:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_TOO_LARGE")
    if set(normalized) != DR_DRILL_ATTESTATION_FIELDS:
        raise FederatedDrDrillError("FEDERATED_DR_ATTESTATION_FIELDS_INVALID")
    transfer_id = _typed_digest(normalized.get("transferId"), code="FEDERATED_DR_TRANSFER_ID_INVALID")
    try:
        transfer = sender_journal.get_transfer(transfer_id)
    except federation_transfer_journal.FederatedTransferJournalError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    if transfer is None:
        raise FederatedDrDrillError("FEDERATION_TRANSFER_NOT_FOUND")
    local_identity = sender_journal.local_identity
    if (
        transfer.get("role") != federation_transfer_journal.ROLE_SENDER
        or transfer.get("localFleetId") != local_identity.get("fleetId")
        or transfer.get("sourceFleetId") != local_identity.get("fleetId")
        or peer_registry.local_identity != local_identity
    ):
        raise FederatedDrDrillError("FEDERATED_DR_SENDER_IDENTITY_MISMATCH")
    minimum_state = federation_transfer_journal.TRANSFER_STATES.index(
        federation_transfer_journal.STATE_REMOTE_COMMITTED
    )
    if federation_transfer_journal.TRANSFER_STATES.index(str(transfer.get("state"))) < minimum_state:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_NOT_COMMITTED")
    destination = str(transfer["destinationFleetId"])
    verified = _verify_signature_and_trust(
        normalized,
        peer_registry=peer_registry,
        destination_fleet_id=destination,
        now=current,
    )
    replica_record = peer_registry.get_replica_attestation(destination, transfer_id)
    if replica_record is None:
        raise FederatedDrDrillError("FEDERATED_DR_REPLICA_ATTESTATION_NOT_ACCEPTED")
    sequence = _sender_semantics(
        verified,
        transfer=transfer,
        replica_record=replica_record,
        now=current,
        max_future_skew_seconds=max(0, int(max_future_skew_seconds)),
    )
    digest = attestation_digest(verified)
    try:
        peer_registry.record_dr_attestation(
            destination,
            signer_key_id=str(verified["signerKeyId"]),
            transfer_id=transfer_id,
            restore_id=str(verified["restoreId"]),
            sequence=sequence,
            attestation_digest=digest,
            attestation=verified,
            accepted_at=current,
        )
    except federation_peer_trust.FederationTrustError as exc:
        raise FederatedDrDrillError(exc.code) from exc
    return copy.deepcopy(verified)
