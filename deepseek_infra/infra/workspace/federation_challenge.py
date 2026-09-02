"""Signed bilateral Fleet challenge/response with durable single-use nonces."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import federation_identity, federation_peer_trust

CHALLENGE_SCHEMA = "federation-challenge-v1"
CHALLENGE_RESPONSE_SCHEMA = "federation-challenge-response-v1"
FEDERATION_SESSION_SCHEMA = "federation-session-v1"
MAX_CHALLENGE_LIFETIME_SECONDS = 120
NONCE_BYTES = 32

SESSION_PURPOSE_REMOTE_CUSTODY = "REMOTE_CUSTODY"
SESSION_PURPOSE_FEDERATED_DR = "FEDERATED_DR"
SESSION_PURPOSE_READINESS_EXCHANGE = "READINESS_EXCHANGE"
SESSION_PURPOSES = frozenset(
    {
        SESSION_PURPOSE_REMOTE_CUSTODY,
        SESSION_PURPOSE_FEDERATED_DR,
        SESSION_PURPOSE_READINESS_EXCHANGE,
    }
)

_CHALLENGE_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "sourceFleetId",
        "destinationFleetId",
        "nonce",
        "sessionPurpose",
        "signerCertificate",
        "issuedAt",
        "expiresAt",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)
_RESPONSE_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "sourceFleetId",
        "destinationFleetId",
        "nonce",
        "challengeDigest",
        "sessionPurpose",
        "signerCertificate",
        "respondedAt",
        "expiresAt",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)


class FederationChallengeError(RuntimeError):
    """Fail-closed session-establishment error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize(value: Any) -> Any:
    try:
        return federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationChallengeError("FEDERATION_CHALLENGE_CANONICAL_PAYLOAD_INVALID") from exc


def _digest(value: Any) -> str:
    try:
        canonical = federation_identity.canonical_federation_json(value)
    except federation_identity.FederationIdentityError as exc:
        raise FederationChallengeError("FEDERATION_CHALLENGE_CANONICAL_PAYLOAD_INVALID") from exc
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def challenge_digest(challenge: dict[str, Any]) -> str:
    return _digest(challenge)


def response_digest(response: dict[str, Any]) -> str:
    return _digest(response)


def generate_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(NONCE_BYTES)).decode("ascii").rstrip("=")


def _nonce(value: Any) -> str:
    if type(value) is not str or not value or "=" in value:
        raise FederationChallengeError("FEDERATION_CHALLENGE_NONCE_INVALID")
    try:
        raw = base64.b64decode(value + ("=" * (-len(value) % 4)), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise FederationChallengeError("FEDERATION_CHALLENGE_NONCE_INVALID") from exc
    if len(raw) != NONCE_BYTES or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        raise FederationChallengeError("FEDERATION_CHALLENGE_NONCE_INVALID")
    return value


def nonce_digest(nonce: str) -> str:
    normalized = _nonce(nonce)
    return "sha256:" + hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _fleet_id(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FederationChallengeError("FEDERATION_CHALLENGE_FLEET_ID_INVALID")
    return value


def _purpose(value: Any) -> str:
    if type(value) is not str or value not in SESSION_PURPOSES:
        raise FederationChallengeError("FEDERATION_CHALLENGE_PURPOSE_INVALID")
    return value


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederationChallengeError("FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederationChallengeError("FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederationChallengeError("FEDERATION_CHALLENGE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederationChallengeError("FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederationChallengeError("FEDERATION_CHALLENGE_TIMESTAMP_INVALID")
    return normalized


def _validate_window(issued_at: datetime, expires_at: datetime) -> None:
    lifetime = (expires_at - issued_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_CHALLENGE_LIFETIME_SECONDS:
        raise FederationChallengeError("FEDERATION_CHALLENGE_LIFETIME_INVALID")


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
        raise FederationChallengeError("FEDERATION_CHALLENGE_LOCAL_SIGNER_MISMATCH")
    errors = federation_identity.validate_online_signer_certificate(
        certificate,
        local_identity,
        now=at,
        required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
    )
    if errors:
        raise FederationChallengeError(errors[0])
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if document_expires_at > certificate_expires_at:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SIGNER_WINDOW_INVALID")
    return local_identity, certificate


def _verify_peer_document(
    document: dict[str, Any],
    *,
    certificate: dict[str, Any],
    peer_fleet_id: str,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    expected_schema: str,
    now: datetime,
) -> dict[str, Any]:
    peer = peer_registry.get_peer(peer_fleet_id)
    if peer is None:
        raise FederationChallengeError("FEDERATION_PEER_NOT_PINNED")
    root_identity = peer.get("fleetIdentity")
    if type(root_identity) is not dict:
        raise FederationChallengeError("FEDERATION_PEER_IDENTITY_INVALID")
    try:
        peer_registry.authorize_online_signer(
            peer_fleet_id,
            certificate,
            purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=now,
        )
        return federation_identity.verify_federation_document(
            document,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=expected_schema,
            now=now,
            required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationChallengeError(exc.code) from exc


def _challenge_semantics(
    challenge: dict[str, Any],
    *,
    expected_source_fleet_id: str,
    expected_destination_fleet_id: str,
    now: datetime,
    max_future_skew_seconds: int,
) -> tuple[str, str, datetime, datetime]:
    if set(challenge) != _CHALLENGE_FIELDS:
        raise FederationChallengeError("FEDERATION_CHALLENGE_FIELDS_INVALID")
    if challenge.get("schema") != CHALLENGE_SCHEMA:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SCHEMA_INVALID")
    source = _fleet_id(challenge.get("sourceFleetId"))
    destination = _fleet_id(challenge.get("destinationFleetId"))
    if challenge.get("fleetId") != source or source != expected_source_fleet_id:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SOURCE_FLEET_MISMATCH")
    if destination != expected_destination_fleet_id:
        raise FederationChallengeError("FEDERATION_CHALLENGE_DESTINATION_FLEET_MISMATCH")
    nonce = _nonce(challenge.get("nonce"))
    purpose = _purpose(challenge.get("sessionPurpose"))
    issued_at = _parse_timestamp(challenge.get("issuedAt"))
    expires_at = _parse_timestamp(challenge.get("expiresAt"))
    _validate_window(issued_at, expires_at)
    if (issued_at - now).total_seconds() > max_future_skew_seconds:
        raise FederationChallengeError("FEDERATION_CHALLENGE_FROM_FUTURE")
    if now >= expires_at:
        raise FederationChallengeError("FEDERATION_CHALLENGE_EXPIRED")
    certificate = challenge.get("signerCertificate")
    assert isinstance(certificate, dict)
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if issued_at < certificate_not_before or expires_at > certificate_expires_at:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SIGNER_WINDOW_INVALID")
    return nonce, purpose, issued_at, expires_at


def issue_federation_challenge(
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    challenger_signer: federation_identity.OnlineFleetSigner,
    destination_fleet_id: str,
    session_purpose: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    issued_at_iso = _utc_iso(now)
    issued_at = _parse_timestamp(issued_at_iso)
    effective_expires_at = expires_at or datetime.fromtimestamp(
        issued_at.timestamp() + MAX_CHALLENGE_LIFETIME_SECONDS,
        tz=timezone.utc,
    )
    expires_at_iso = _utc_iso(effective_expires_at)
    normalized_expires_at = _parse_timestamp(expires_at_iso)
    _validate_window(issued_at, normalized_expires_at)
    destination = _fleet_id(destination_fleet_id)
    purpose = _purpose(session_purpose)
    local_identity, certificate = _require_local_signer(
        peer_registry,
        challenger_signer,
        at=issued_at,
        document_expires_at=normalized_expires_at,
    )
    source = str(local_identity["fleetId"])
    if source == destination:
        raise FederationChallengeError("FEDERATION_CHALLENGE_REFLECTION_REJECTED")
    try:
        peer_registry.require_active_peer(destination)
    except federation_peer_trust.FederationTrustError as exc:
        raise FederationChallengeError(exc.code) from exc
    payload = {
        "schema": CHALLENGE_SCHEMA,
        "fleetId": source,
        "sourceFleetId": source,
        "destinationFleetId": destination,
        "nonce": generate_nonce(),
        "sessionPurpose": purpose,
        "signerCertificate": certificate,
        "issuedAt": issued_at_iso,
        "expiresAt": expires_at_iso,
    }
    try:
        challenge = federation_identity.sign_federation_document(
            challenger_signer,
            payload,
            purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
        )
        peer_registry.record_outbound_federation_challenge(
            destination,
            nonce_digest=nonce_digest(str(challenge["nonce"])),
            challenge_digest=challenge_digest(challenge),
            challenge=challenge,
            session_purpose=purpose,
            issued_at=issued_at,
            expires_at=normalized_expires_at,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationChallengeError(exc.code) from exc
    return challenge


def respond_to_federation_challenge(
    challenge: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    responder_signer: federation_identity.OnlineFleetSigner,
    now: datetime,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    current = _parse_timestamp(_utc_iso(now))
    normalized = _normalize(challenge)
    if type(normalized) is not dict:
        raise FederationChallengeError("FEDERATION_CHALLENGE_INVALID")
    if normalized.get("schema") != CHALLENGE_SCHEMA:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SCHEMA_INVALID")
    source = _fleet_id(normalized.get("sourceFleetId"))
    local_fleet_id = str(peer_registry.local_identity["fleetId"])
    certificate = normalized.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SIGNER_CERTIFICATE_INVALID")
    verified = _verify_peer_document(
        normalized,
        certificate=certificate,
        peer_fleet_id=source,
        peer_registry=peer_registry,
        expected_schema=CHALLENGE_SCHEMA,
        now=current,
    )
    nonce, purpose, issued_at, expires_at = _challenge_semantics(
        verified,
        expected_source_fleet_id=source,
        expected_destination_fleet_id=local_fleet_id,
        now=current,
        max_future_skew_seconds=max(0, int(max_future_skew_seconds)),
    )
    _, responder_certificate = _require_local_signer(
        peer_registry,
        responder_signer,
        at=current,
        document_expires_at=expires_at,
    )
    existing = peer_registry.get_federation_challenge(
        federation_peer_trust.CHALLENGE_DIRECTION_INBOUND,
        nonce_digest(nonce),
    )
    if existing is not None:
        raise FederationChallengeError("FEDERATION_CHALLENGE_NONCE_REPLAY")
    response_payload = {
        "schema": CHALLENGE_RESPONSE_SCHEMA,
        "fleetId": local_fleet_id,
        "sourceFleetId": source,
        "destinationFleetId": local_fleet_id,
        "nonce": nonce,
        "challengeDigest": challenge_digest(verified),
        "sessionPurpose": purpose,
        "signerCertificate": responder_certificate,
        "respondedAt": _utc_iso(current),
        "expiresAt": _utc_iso(expires_at),
    }
    try:
        response = federation_identity.sign_federation_document(
            responder_signer,
            response_payload,
            purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
        )
        peer_registry.record_inbound_federation_response(
            source,
            peer_signer_key_id=str(verified["signerKeyId"]),
            nonce_digest=nonce_digest(nonce),
            challenge_digest=challenge_digest(verified),
            challenge=verified,
            response_digest=response_digest(response),
            response=response,
            session_purpose=purpose,
            issued_at=issued_at,
            expires_at=expires_at,
            responded_at=current,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationChallengeError(exc.code) from exc
    return response


def verify_federation_challenge_response(
    challenge: dict[str, Any],
    response: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    now: datetime,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    current = _parse_timestamp(_utc_iso(now))
    normalized_challenge = _normalize(challenge)
    normalized_response = _normalize(response)
    if type(normalized_challenge) is not dict or type(normalized_response) is not dict:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_INVALID")
    if normalized_response.get("schema") != CHALLENGE_RESPONSE_SCHEMA:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_SCHEMA_INVALID")
    if set(normalized_response) != _RESPONSE_FIELDS:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_FIELDS_INVALID")
    local_fleet_id = str(peer_registry.local_identity["fleetId"])
    source = _fleet_id(normalized_challenge.get("sourceFleetId"))
    destination = _fleet_id(normalized_challenge.get("destinationFleetId"))
    if source != local_fleet_id:
        raise FederationChallengeError("FEDERATION_CHALLENGE_SOURCE_FLEET_MISMATCH")
    certificate = normalized_response.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_SIGNER_CERTIFICATE_INVALID")
    verified_response = _verify_peer_document(
        normalized_response,
        certificate=certificate,
        peer_fleet_id=destination,
        peer_registry=peer_registry,
        expected_schema=CHALLENGE_RESPONSE_SCHEMA,
        now=current,
    )
    if verified_response.get("fleetId") != destination or verified_response.get("destinationFleetId") != destination:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_DESTINATION_FLEET_MISMATCH")
    if verified_response.get("sourceFleetId") != source:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_SOURCE_FLEET_MISMATCH")
    nonce = _nonce(normalized_challenge.get("nonce"))
    if verified_response.get("nonce") != nonce:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_NONCE_MISMATCH")
    purpose = _purpose(normalized_challenge.get("sessionPurpose"))
    if verified_response.get("sessionPurpose") != purpose:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_PURPOSE_MISMATCH")
    expected_challenge_digest = challenge_digest(normalized_challenge)
    if verified_response.get("challengeDigest") != expected_challenge_digest:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_BINDING_INVALID")
    challenge_issued_at = _parse_timestamp(normalized_challenge.get("issuedAt"))
    challenge_expires_at = _parse_timestamp(normalized_challenge.get("expiresAt"))
    _validate_window(challenge_issued_at, challenge_expires_at)
    responded_at = _parse_timestamp(verified_response.get("respondedAt"))
    response_expires_at = _parse_timestamp(verified_response.get("expiresAt"))
    if response_expires_at != challenge_expires_at or not (challenge_issued_at <= responded_at < challenge_expires_at):
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_WINDOW_INVALID")
    max_future_skew = max(0, int(max_future_skew_seconds))
    if (responded_at - current).total_seconds() > max_future_skew:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_FROM_FUTURE")
    if current >= response_expires_at:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_EXPIRED")
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if responded_at < certificate_not_before or response_expires_at > certificate_expires_at:
        raise FederationChallengeError("FEDERATION_CHALLENGE_RESPONSE_SIGNER_WINDOW_INVALID")
    try:
        journal = peer_registry.consume_outbound_federation_response(
            destination,
            peer_signer_key_id=str(verified_response["signerKeyId"]),
            nonce_digest=nonce_digest(nonce),
            challenge_digest=expected_challenge_digest,
            response_digest=response_digest(verified_response),
            response=verified_response,
            consumed_at=current,
        )
    except federation_peer_trust.FederationTrustError as exc:
        raise FederationChallengeError(exc.code) from exc
    return {
        "schema": FEDERATION_SESSION_SCHEMA,
        "status": "AUTHENTICATED",
        "sourceFleetId": source,
        "destinationFleetId": destination,
        "nonceDigest": nonce_digest(nonce),
        "sessionPurpose": purpose,
        "challengeDigest": expected_challenge_digest,
        "responseDigest": response_digest(verified_response),
        "authenticatedAt": _utc_iso(current),
        "expiresAt": _utc_iso(response_expires_at),
        "journalRevision": journal["revision"],
    }
