"""Receiver-issued, short-lived, scope-bound federation ingress grants."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from deepseek_infra.infra.workspace import federation_challenge, federation_identity, federation_peer_trust, federation_transfer

INGRESS_GRANT_SCHEMA = "federation-ingress-grant-v1"
MAX_INGRESS_GRANT_LIFETIME_SECONDS = 300
DEFAULT_INGRESS_GRANT_LIFETIME_SECONDS = 60
MAX_INGRESS_GRANT_BYTES = (1 << 63) - 1

_FLEET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_GRANT_ID_PATTERN = re.compile(r"^grant-[0-9a-f]{32}$")
_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "grantId",
        "sourceFleetId",
        "destinationFleetId",
        "transferId",
        "policyId",
        "backupId",
        "objectSetDigest",
        "allowedObjectPrefix",
        "maxBytes",
        "issuedAt",
        "expiresAt",
        "nonce",
        "sessionNonceDigest",
        "signerCertificate",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)


class FederationIngressGrantError(RuntimeError):
    """Fail-closed ingress authorization error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationIngressGrantError("FEDERATION_INGRESS_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    try:
        canonical = federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationIngressGrantError("FEDERATION_INGRESS_CANONICAL_PAYLOAD_INVALID") from exc
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def grant_digest(grant: dict[str, Any]) -> str:
    return _digest(grant)


def _fleet_id(value: Any) -> str:
    if type(value) is not str or _FLEET_ID_PATTERN.fullmatch(value) is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_FLEET_ID_INVALID")
    return value


def _grant_id(value: Any) -> str:
    if type(value) is not str or _GRANT_ID_PATTERN.fullmatch(value) is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_ID_INVALID")
    return value


def _control_id(value: Any, *, code: str) -> str:
    if type(value) is not str or _CONTROL_ID_PATTERN.fullmatch(value) is None:
        raise FederationIngressGrantError(code)
    return value


def _sha256_digest(value: Any, *, code: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
        raise FederationIngressGrantError(code)
    return value


def _object_prefix(value: Any) -> str:
    if type(value) is not str or not value.startswith("federation/") or not value.endswith("/"):
        raise FederationIngressGrantError("FEDERATION_INGRESS_OBJECT_PREFIX_INVALID")
    lowered = value.casefold()
    if (
        "\\" in value
        or "//" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(encoded in lowered for encoded in ("%2e", "%2f", "%5c"))
        or any(segment in {".", ".."} for segment in value.split("/") if segment)
    ):
        raise FederationIngressGrantError("FEDERATION_INGRESS_OBJECT_PREFIX_INVALID")
    return value


def _max_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 < value <= MAX_INGRESS_GRANT_BYTES):
        raise FederationIngressGrantError("FEDERATION_INGRESS_MAX_BYTES_INVALID")
    return value


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TIMESTAMP_INVALID")
    return normalized


def _validate_window(issued_at: datetime, expires_at: datetime) -> None:
    lifetime = (expires_at - issued_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_INGRESS_GRANT_LIFETIME_SECONDS:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_LIFETIME_INVALID")


def _require_local_signer(
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    signer: federation_identity.OnlineFleetSigner,
    *,
    at: datetime,
    document_expires_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_identity = peer_registry.local_identity
    certificate = signer.certificate
    if (
        certificate.get("fleetId") != local_identity.get("fleetId")
        or certificate.get("rootKeyId") != local_identity.get("rootKeyId")
        or certificate.get("rootFingerprint") != local_identity.get("rootFingerprint")
    ):
        raise FederationIngressGrantError("FEDERATION_INGRESS_LOCAL_SIGNER_MISMATCH")
    errors = federation_identity.validate_online_signer_certificate(
        certificate,
        local_identity,
        now=at,
        required_purpose=federation_identity.PURPOSE_INGRESS_GRANT,
    )
    if errors:
        raise FederationIngressGrantError(errors[0])
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if document_expires_at > certificate_expires_at:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SIGNER_WINDOW_INVALID")
    return local_identity, certificate


def _verify_peer_grant(
    grant: dict[str, Any],
    *,
    certificate: dict[str, Any],
    destination_fleet_id: str,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    now: datetime,
) -> dict[str, Any]:
    peer = peer_registry.get_peer(destination_fleet_id)
    if peer is None:
        raise FederationIngressGrantError("FEDERATION_PEER_NOT_PINNED")
    root_identity = peer.get("fleetIdentity")
    if type(root_identity) is not dict:
        raise FederationIngressGrantError("FEDERATION_PEER_IDENTITY_INVALID")
    try:
        peer_registry.authorize_online_signer(
            destination_fleet_id,
            certificate,
            purpose=federation_identity.PURPOSE_INGRESS_GRANT,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=now,
        )
        return federation_identity.verify_federation_document(
            grant,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=INGRESS_GRANT_SCHEMA,
            now=now,
            required_purpose=federation_identity.PURPOSE_INGRESS_GRANT,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationIngressGrantError(exc.code) from exc


def _grant_semantics(
    grant: dict[str, Any],
    *,
    expected_source_fleet_id: str,
    expected_destination_fleet_id: str,
    expected_transfer_id: str,
    expected_policy_id: str,
    expected_backup_id: str,
    expected_object_set_digest: str,
    now: datetime,
    max_future_skew_seconds: int,
) -> tuple[datetime, datetime, str]:
    if set(grant) != _FIELDS:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_FIELDS_INVALID")
    if grant.get("schema") != INGRESS_GRANT_SCHEMA:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_SCHEMA_INVALID")
    _grant_id(grant.get("grantId"))
    source = _fleet_id(grant.get("sourceFleetId"))
    destination = _fleet_id(grant.get("destinationFleetId"))
    if source != expected_source_fleet_id:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SOURCE_FLEET_MISMATCH")
    if grant.get("fleetId") != destination or destination != expected_destination_fleet_id:
        raise FederationIngressGrantError("FEDERATION_INGRESS_DESTINATION_FLEET_MISMATCH")
    if source == destination:
        raise FederationIngressGrantError("FEDERATION_INGRESS_REFLECTION_REJECTED")
    transfer = _sha256_digest(grant.get("transferId"), code="FEDERATION_INGRESS_TRANSFER_ID_INVALID")
    if transfer != expected_transfer_id:
        raise FederationIngressGrantError("FEDERATION_INGRESS_TRANSFER_ID_MISMATCH")
    policy = _control_id(grant.get("policyId"), code="FEDERATION_INGRESS_POLICY_ID_INVALID")
    if policy != expected_policy_id:
        raise FederationIngressGrantError("FEDERATION_INGRESS_POLICY_ID_MISMATCH")
    backup = _control_id(grant.get("backupId"), code="FEDERATION_INGRESS_BACKUP_ID_INVALID")
    if backup != expected_backup_id:
        raise FederationIngressGrantError("FEDERATION_INGRESS_BACKUP_ID_MISMATCH")
    object_set = _sha256_digest(
        grant.get("objectSetDigest"),
        code="FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID",
    )
    if object_set != expected_object_set_digest:
        raise FederationIngressGrantError("FEDERATION_INGRESS_OBJECT_SET_DIGEST_MISMATCH")
    try:
        derived_transfer = federation_transfer.derive_transfer_id(
            source_fleet_id=source,
            destination_fleet_id=destination,
            backup_id=backup,
            object_set_digest=object_set,
        )
    except federation_transfer.FederatedTransferError as exc:
        raise FederationIngressGrantError(exc.code) from exc
    if transfer != derived_transfer:
        raise FederationIngressGrantError("FEDERATION_TRANSFER_ID_INVALID")
    _object_prefix(grant.get("allowedObjectPrefix"))
    _max_bytes(grant.get("maxBytes"))
    try:
        federation_challenge.nonce_digest(str(grant.get("nonce")))
    except federation_challenge.FederationChallengeError as exc:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_NONCE_INVALID") from exc
    session_nonce_digest = _sha256_digest(
        grant.get("sessionNonceDigest"),
        code="FEDERATION_INGRESS_SESSION_NONCE_DIGEST_INVALID",
    )
    issued_at = _parse_timestamp(grant.get("issuedAt"))
    expires_at = _parse_timestamp(grant.get("expiresAt"))
    _validate_window(issued_at, expires_at)
    if (issued_at - now).total_seconds() > max_future_skew_seconds:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_FROM_FUTURE")
    if now >= expires_at:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_EXPIRED")
    certificate = grant.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID")
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if issued_at < certificate_not_before or expires_at > certificate_expires_at:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SIGNER_WINDOW_INVALID")
    return issued_at, expires_at, session_nonce_digest


def issue_ingress_grant(
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    receiver_signer: federation_identity.OnlineFleetSigner,
    source_fleet_id: str,
    session_nonce: str,
    transfer_id: str,
    policy_id: str,
    backup_id: str,
    object_set_digest: str,
    allowed_object_prefix: str,
    max_bytes: int,
    now: datetime,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    current = _parse_timestamp(_utc_iso(now))
    effective_expires_at = expires_at or current + timedelta(seconds=DEFAULT_INGRESS_GRANT_LIFETIME_SECONDS)
    normalized_expires_at = _parse_timestamp(_utc_iso(effective_expires_at))
    _validate_window(current, normalized_expires_at)
    source = _fleet_id(source_fleet_id)
    transfer = _sha256_digest(transfer_id, code="FEDERATION_INGRESS_TRANSFER_ID_INVALID")
    policy = _control_id(policy_id, code="FEDERATION_INGRESS_POLICY_ID_INVALID")
    backup = _control_id(backup_id, code="FEDERATION_INGRESS_BACKUP_ID_INVALID")
    object_set = _sha256_digest(
        object_set_digest,
        code="FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID",
    )
    prefix = _object_prefix(allowed_object_prefix)
    byte_limit = _max_bytes(max_bytes)
    try:
        session_nonce_digest = federation_challenge.nonce_digest(session_nonce)
        peer_registry.require_active_peer(source)
    except (federation_challenge.FederationChallengeError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationIngressGrantError(exc.code) from exc
    local_identity, certificate = _require_local_signer(
        peer_registry,
        receiver_signer,
        at=current,
        document_expires_at=normalized_expires_at,
    )
    destination = str(local_identity["fleetId"])
    if source == destination:
        raise FederationIngressGrantError("FEDERATION_INGRESS_REFLECTION_REJECTED")
    try:
        derived_transfer = federation_transfer.derive_transfer_id(
            source_fleet_id=source,
            destination_fleet_id=destination,
            backup_id=backup,
            object_set_digest=object_set,
        )
    except federation_transfer.FederatedTransferError as exc:
        raise FederationIngressGrantError(exc.code) from exc
    if transfer != derived_transfer:
        raise FederationIngressGrantError("FEDERATION_TRANSFER_ID_INVALID")
    session = peer_registry.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_INBOUND,
        session_nonce_digest,
    )
    if session is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
    session_expires_at = _parse_timestamp(session.get("expiresAt"))
    if (
        session.get("state") != federation_peer_trust.CHALLENGE_STATE_RESPONDED
        or session.get("peerFleetId") != source
        or session.get("sourceFleetId") != source
        or session.get("destinationFleetId") != destination
        or session.get("sessionPurpose") != federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY
        or current >= session_expires_at
        or normalized_expires_at > session_expires_at
    ):
        raise FederationIngressGrantError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
    if peer_registry.get_ingress_grant_by_session_nonce(session_nonce_digest) is not None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SESSION_REPLAY")
    payload = {
        "schema": INGRESS_GRANT_SCHEMA,
        "fleetId": destination,
        "grantId": "grant-" + secrets.token_hex(16),
        "sourceFleetId": source,
        "destinationFleetId": destination,
        "transferId": transfer,
        "policyId": policy,
        "backupId": backup,
        "objectSetDigest": object_set,
        "allowedObjectPrefix": prefix,
        "maxBytes": byte_limit,
        "issuedAt": _utc_iso(current),
        "expiresAt": _utc_iso(normalized_expires_at),
        "nonce": federation_challenge.generate_nonce(),
        "sessionNonceDigest": session_nonce_digest,
        "signerCertificate": certificate,
    }
    try:
        grant = federation_identity.sign_federation_document(
            receiver_signer,
            payload,
            purpose=federation_identity.PURPOSE_INGRESS_GRANT,
        )
        peer_registry.record_ingress_grant(grant, recorded_at=current)
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationIngressGrantError(exc.code) from exc
    return grant


def verify_ingress_grant(
    grant: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    expected_source_fleet_id: str,
    expected_destination_fleet_id: str,
    expected_transfer_id: str,
    expected_policy_id: str,
    expected_backup_id: str,
    expected_object_set_digest: str,
    now: datetime,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    current = _parse_timestamp(_utc_iso(now))
    normalized = _normalize(grant)
    if type(normalized) is not dict:
        raise FederationIngressGrantError("FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID")
    destination = _fleet_id(expected_destination_fleet_id)
    certificate = normalized.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID")
    verified = _verify_peer_grant(
        normalized,
        certificate=certificate,
        destination_fleet_id=destination,
        peer_registry=peer_registry,
        now=current,
    )
    _, expires_at, session_nonce_digest = _grant_semantics(
        verified,
        expected_source_fleet_id=_fleet_id(expected_source_fleet_id),
        expected_destination_fleet_id=destination,
        expected_transfer_id=_sha256_digest(
            expected_transfer_id,
            code="FEDERATION_INGRESS_TRANSFER_ID_INVALID",
        ),
        expected_policy_id=_control_id(
            expected_policy_id,
            code="FEDERATION_INGRESS_POLICY_ID_INVALID",
        ),
        expected_backup_id=_control_id(
            expected_backup_id,
            code="FEDERATION_INGRESS_BACKUP_ID_INVALID",
        ),
        expected_object_set_digest=_sha256_digest(
            expected_object_set_digest,
            code="FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID",
        ),
        now=current,
        max_future_skew_seconds=max(0, int(max_future_skew_seconds)),
    )
    session = peer_registry.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_OUTBOUND,
        session_nonce_digest,
    )
    if session is None:
        raise FederationIngressGrantError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
    session_expires_at = _parse_timestamp(session.get("expiresAt"))
    if (
        session.get("state")
        not in {
            federation_peer_trust.CHALLENGE_STATE_PENDING,
            federation_peer_trust.CHALLENGE_STATE_CONSUMED,
        }
        or
        session.get("peerFleetId") != destination
        or session.get("sourceFleetId") != expected_source_fleet_id
        or session.get("destinationFleetId") != destination
        or session.get("sessionPurpose") != federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY
        or current >= session_expires_at
        or expires_at > session_expires_at
    ):
        raise FederationIngressGrantError("FEDERATION_INGRESS_SESSION_NOT_AUTHENTICATED")
    return verified


def authorize_ingress_write(
    grant: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    source_fleet_id: str,
    write_id: str,
    object_key: str,
    byte_count: int,
    now: datetime,
) -> dict[str, Any]:
    try:
        return peer_registry.reserve_ingress_write(
            grant,
            source_fleet_id=source_fleet_id,
            write_id=write_id,
            object_key=object_key,
            byte_count=byte_count,
            reserved_at=now,
        )
    except federation_peer_trust.FederationTrustError as exc:
        raise FederationIngressGrantError(exc.code) from exc
