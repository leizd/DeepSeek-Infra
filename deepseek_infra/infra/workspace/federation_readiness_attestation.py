"""Root-chained signed readiness with durable per-peer replay fencing."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    federation_identity,
    federation_peer_trust,
    resilience_federation_readiness,
)

READINESS_ATTESTATION_SCHEMA = "federation-readiness-attestation-v1"
MAX_READINESS_ATTESTATION_LIFETIME_SECONDS = 300

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SNAPSHOT_FIELDS = frozenset(
    {
        "snapshotSchema",
        "fleetId",
        "wireCompatibility",
        "availableFailureDomains",
        "forecastHeadroom",
        "costClass",
        "readiness",
        "status",
        "incompatibleWireVersions",
        "missingRequiredWireVersions",
        "generatedAt",
        "snapshotDigest",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
        "schema",
        "fleetId",
        "sequence",
        "snapshotDigest",
        "snapshot",
        "signerCertificate",
        "signedAt",
        "expiresAt",
        "signerKeyId",
        "signatureAlgorithm",
        "signature",
    }
)
_SNAPSHOT_ERROR_CODES = {
    "federationSnapshotContainsCredentials": "FEDERATION_READINESS_SNAPSHOT_CONTAINS_CREDENTIALS",
    "federationSnapshotSchemaInvalid": "FEDERATION_READINESS_SNAPSHOT_SCHEMA_INVALID",
    "federationFleetIdentityMissing": "FEDERATION_READINESS_SNAPSHOT_FLEET_INVALID",
    "federationSnapshotDigestInvalid": "FEDERATION_READINESS_SNAPSHOT_DIGEST_INVALID",
    "federationSnapshotTimestampInvalid": "FEDERATION_READINESS_SNAPSHOT_TIMESTAMP_INVALID",
    "federationSnapshotExpired": "FEDERATION_READINESS_SNAPSHOT_EXPIRED",
    "federationSnapshotFromFuture": "FEDERATION_READINESS_SNAPSHOT_FROM_FUTURE",
}


class FederationReadinessError(RuntimeError):
    """Fail-closed signed-readiness error with a stable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _normalize_json(value: Any) -> Any:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID")
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID") from exc
        return value
    if type(value) is list:
        return [_normalize_json(item) for item in value]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID") from exc
            normalized[key] = _normalize_json(item)
        return normalized
    raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID")


def _canonical_json(value: Any) -> str:
    normalized = _normalize_json(value)
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FederationReadinessError("FEDERATION_READINESS_CANONICAL_PAYLOAD_INVALID") from exc


def attestation_digest(attestation: dict[str, Any]) -> str:
    """Digest the complete signed attestation bytes used by the replay journal."""

    return "sha256:" + hashlib.sha256(_canonical_json(attestation).encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FederationReadinessError("FEDERATION_READINESS_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str:
        raise FederationReadinessError("FEDERATION_READINESS_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederationReadinessError("FEDERATION_READINESS_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederationReadinessError("FEDERATION_READINESS_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FederationReadinessError("FEDERATION_READINESS_TIMESTAMP_INVALID")
    return normalized


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FederationReadinessError("FEDERATION_READINESS_SEQUENCE_INVALID")
    return value


def _snapshot_digest(value: Any) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_DIGEST_INVALID")
    return value


def _validate_snapshot(
    snapshot: Any,
    *,
    expected_fleet_id: str,
    now: datetime,
    max_snapshot_age_seconds: int,
    max_future_skew_seconds: int,
) -> dict[str, Any]:
    if type(snapshot) is not dict:
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_INVALID")
    normalized = _normalize_json(snapshot)
    assert isinstance(normalized, dict)
    if normalized.get("snapshotSchema") != resilience_federation_readiness.FEDERATION_SNAPSHOT_SCHEMA:
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_SCHEMA_INVALID")
    if not _REQUIRED_SNAPSHOT_FIELDS.issubset(normalized):
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_FIELDS_MISSING")
    if normalized.get("fleetId") != expected_fleet_id:
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_FLEET_MISMATCH")
    semantic_error = resilience_federation_readiness._validate_snapshot(
        normalized,
        now=now,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        max_future_skew_seconds=max_future_skew_seconds,
    )
    if semantic_error is not None:
        raise FederationReadinessError(_SNAPSHOT_ERROR_CODES.get(semantic_error, "FEDERATION_READINESS_SNAPSHOT_INVALID"))
    return normalized


def _validate_window(signed_at: datetime, expires_at: datetime) -> None:
    lifetime_seconds = (expires_at - signed_at).total_seconds()
    if lifetime_seconds <= 0 or lifetime_seconds > MAX_READINESS_ATTESTATION_LIFETIME_SECONDS:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_LIFETIME_INVALID")


def issue_readiness_attestation(
    signer: federation_identity.OnlineFleetSigner,
    snapshot: dict[str, Any],
    *,
    sequence: int,
    signed_at: datetime,
    expires_at: datetime,
    max_snapshot_age_seconds: int = 300,
) -> dict[str, Any]:
    """Sign the complete canonical readiness snapshot, never a risk projection."""

    normalized_sequence = _sequence(sequence)
    signed_at_iso = _utc_iso(signed_at)
    expires_at_iso = _utc_iso(expires_at)
    normalized_signed_at = _parse_timestamp(signed_at_iso)
    normalized_expires_at = _parse_timestamp(expires_at_iso)
    _validate_window(normalized_signed_at, normalized_expires_at)
    certificate = signer.certificate
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if normalized_signed_at < certificate_not_before or normalized_expires_at > certificate_expires_at:
        raise FederationReadinessError("FEDERATION_READINESS_SIGNER_WINDOW_INVALID")
    normalized_snapshot = _validate_snapshot(
        snapshot,
        expected_fleet_id=signer.fleet_id,
        now=normalized_signed_at,
        max_snapshot_age_seconds=max(1, int(max_snapshot_age_seconds)),
        max_future_skew_seconds=0,
    )
    digest = _snapshot_digest(normalized_snapshot.get("snapshotDigest"))
    payload = {
        "schema": READINESS_ATTESTATION_SCHEMA,
        "fleetId": signer.fleet_id,
        "sequence": normalized_sequence,
        "snapshotDigest": digest,
        "snapshot": normalized_snapshot,
        "signerCertificate": certificate,
        "signedAt": signed_at_iso,
        "expiresAt": expires_at_iso,
    }
    try:
        return federation_identity.sign_federation_document(
            signer,
            payload,
            purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
        )
    except federation_identity.FederationIdentityError as exc:
        raise FederationReadinessError(exc.code) from exc


def verify_and_record_readiness_attestation(
    attestation: dict[str, Any],
    *,
    peer_registry: federation_peer_trust.PeerTrustRegistry,
    expected_peer_fleet_id: str,
    now: datetime,
    max_future_skew_seconds: int = 30,
    max_snapshot_age_seconds: int = 300,
) -> dict[str, Any]:
    """Verify trust, signature, full snapshot semantics, time, then consume sequence."""

    current_iso = _utc_iso(now)
    current = _parse_timestamp(current_iso)
    if type(attestation) is not dict:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_INVALID")
    normalized = _normalize_json(attestation)
    assert isinstance(normalized, dict)
    if set(normalized) != _ATTESTATION_FIELDS:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_FIELDS_INVALID")
    if normalized.get("schema") != READINESS_ATTESTATION_SCHEMA:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_SCHEMA_INVALID")
    if type(expected_peer_fleet_id) is not str or normalized.get("fleetId") != expected_peer_fleet_id:
        raise FederationReadinessError("FEDERATION_READINESS_FLEET_MISMATCH")
    certificate = normalized.get("signerCertificate")
    if type(certificate) is not dict:
        raise FederationReadinessError("FEDERATION_READINESS_SIGNER_CERTIFICATE_INVALID")
    peer = peer_registry.get_peer(expected_peer_fleet_id)
    if peer is None:
        raise FederationReadinessError("FEDERATION_PEER_NOT_PINNED")
    root_identity = peer.get("fleetIdentity")
    if type(root_identity) is not dict:
        raise FederationReadinessError("FEDERATION_PEER_IDENTITY_INVALID")
    try:
        peer_registry.authorize_online_signer(
            expected_peer_fleet_id,
            certificate,
            purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
            mode=federation_peer_trust.AUTHORIZATION_CURRENT,
            validation_time=current,
        )
        verified = federation_identity.verify_federation_document(
            normalized,
            certificate=certificate,
            root_identity=root_identity,
            expected_schema=READINESS_ATTESTATION_SCHEMA,
            now=current,
            required_purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
        )
    except (federation_identity.FederationIdentityError, federation_peer_trust.FederationTrustError) as exc:
        raise FederationReadinessError(exc.code) from exc

    sequence = _sequence(verified.get("sequence"))
    signed_at = _parse_timestamp(verified.get("signedAt"))
    expires_at = _parse_timestamp(verified.get("expiresAt"))
    _validate_window(signed_at, expires_at)
    certificate_not_before = _parse_timestamp(certificate.get("notBefore"))
    certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"))
    if signed_at < certificate_not_before or expires_at > certificate_expires_at:
        raise FederationReadinessError("FEDERATION_READINESS_SIGNER_WINDOW_INVALID")
    max_future_skew = max(0, int(max_future_skew_seconds))
    if (signed_at - current).total_seconds() > max_future_skew:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_FROM_FUTURE")
    if current >= expires_at:
        raise FederationReadinessError("FEDERATION_READINESS_ATTESTATION_EXPIRED")
    snapshot = _validate_snapshot(
        verified.get("snapshot"),
        expected_fleet_id=expected_peer_fleet_id,
        now=current,
        max_snapshot_age_seconds=max(1, int(max_snapshot_age_seconds)),
        max_future_skew_seconds=max_future_skew,
    )
    outer_digest = _snapshot_digest(verified.get("snapshotDigest"))
    if outer_digest != snapshot.get("snapshotDigest"):
        raise FederationReadinessError("FEDERATION_READINESS_SNAPSHOT_DIGEST_INVALID")
    try:
        peer_registry.record_readiness_sequence(
            expected_peer_fleet_id,
            signer_key_id=str(verified["signerKeyId"]),
            sequence=sequence,
            attestation_digest=attestation_digest(verified),
            accepted_at=current,
        )
    except federation_peer_trust.FederationTrustError as exc:
        raise FederationReadinessError(exc.code) from exc
    return copy.deepcopy(verified)
