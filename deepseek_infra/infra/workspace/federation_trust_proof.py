"""Typed, independently recomputable federation trust proof (4.8.0 Gate O)."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    federation_challenge,
    federation_identity,
    federation_peer_trust,
    federation_readiness_attestation,
)


FEDERATION_TRUST_PROOF_SCHEMA = "federation-trust-proof-v1"
FEDERATION_TRUST_PROOF_CHECKS = (
    "fleetIdentityUsesDedicatedFederationSigningKey",
    "federationKeyIsDistinctFromAgeIdentity",
    "federationKeyIsDistinctFromAuthorityIdentity",
    "peerTrustRequiresPinnedRoot",
    "trustOnFirstUseIsRejected",
    "rotatedOnlineSignerRequiresPinnedRootCertificate",
    "revokedFederationSignerIsRejected",
    "federationReadinessSignatureIsVerified",
    "readinessAttestationBindsFullCanonicalPayload",
    "readinessSequenceReplayIsRejected",
    "expiredReadinessAttestationIsRejected",
    "futureReadinessAttestationBeyondSkewIsRejected",
    "challengeResponseBindsBothFleetIds",
    "challengeNonceReplayIsRejected",
    "federationTrustProofIsSemanticallyValidated",
)

_PROOF_FIELDS = frozenset(
    {
        "schema",
        "validatedAt",
        "sourceFleetIdentity",
        "destinationFleetIdentity",
        "sourcePeerTrust",
        "destinationPeerTrust",
        "sourceSignerTrust",
        "destinationSignerTrust",
        "revokedDestinationSignerTrust",
        "readinessAttestation",
        "readinessHighWater",
        "challenge",
        "challengeResponse",
        "ageRecipients",
        "authorityIdentityDigests",
        "failureObservations",
        "proofDigest",
    }
)
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
_SIGNER_RECORD_FIELDS = frozenset(
    {
        "schema",
        "peerFleetId",
        "signerKeyId",
        "sequence",
        "certificateDigest",
        "certificate",
        "acceptedBy",
        "acceptedAt",
        "revokedAt",
        "revokedBy",
        "revocationReason",
        "revision",
    }
)
_READINESS_FIELDS = frozenset(
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
_CHALLENGE_RESPONSE_FIELDS = frozenset(
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
_FAILURE_FIELDS = frozenset(
    {
        "claim",
        "code",
        "preStateDigest",
        "postStateDigest",
        "documentDigest",
        "document",
    }
)
_FAILURE_CODES = {
    "trustOnFirstUseIsRejected": "FEDERATION_PEER_NOT_PINNED",
    "readinessSequenceReplayIsRejected": "FEDERATION_READINESS_SEQUENCE_REPLAY",
    "expiredReadinessAttestationIsRejected": "FEDERATION_READINESS_ATTESTATION_EXPIRED",
    "futureReadinessAttestationBeyondSkewIsRejected": "FEDERATION_READINESS_ATTESTATION_FROM_FUTURE",
    "challengeNonceReplayIsRejected": "FEDERATION_CHALLENGE_NONCE_REPLAY",
    "revokedFederationSignerIsRejected": "FEDERATION_SIGNER_REVOKED",
}
_PINNED_METADATA_FIELDS = frozenset({"provider", "region", "jurisdiction", "siteClass"})
_TYPED_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return federation_identity.canonical_federation_json(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def proof_digest(proof: dict[str, Any]) -> str:
    return _digest({key: value for key, value in proof.items() if key != "proofDigest"})


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("federation-trust-proof-timestamp-invalid")
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


def _validate_identity(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        return federation_identity.validate_fleet_identity(value)
    except federation_identity.FederationIdentityError as exc:
        errors.append(f"{label}-identity-invalid:{exc.code}")
        return {}


def _validate_peer_record(record: Any, expected_identity: dict[str, Any], label: str, errors: list[str]) -> dict[str, Any]:
    normalized = _as_dict(record, label, errors)
    if not normalized:
        return {}
    if set(normalized) != _PEER_RECORD_FIELDS:
        errors.append(f"{label}-fields-invalid")
    if normalized.get("schema") != federation_peer_trust.PEER_TRUST_RECORD_SCHEMA:
        errors.append(f"{label}-schema-invalid")
    if normalized.get("state") != federation_peer_trust.STATE_ACTIVE:
        errors.append(f"{label}-not-active")
    if normalized.get("peerFleetId") != expected_identity.get("fleetId"):
        errors.append(f"{label}-fleet-mismatch")
    if normalized.get("fleetIdentity") != expected_identity:
        errors.append(f"{label}-identity-binding-mismatch")
    if normalized.get("rootKeyId") != expected_identity.get("rootKeyId"):
        errors.append(f"{label}-root-key-mismatch")
    if normalized.get("rootFingerprint") != expected_identity.get("rootFingerprint"):
        errors.append(f"{label}-root-fingerprint-mismatch")
    metadata = normalized.get("pinnedMetadata")
    if type(metadata) is not dict or set(metadata) != _PINNED_METADATA_FIELDS or any(type(item) is not str or not item for item in metadata.values()):
        errors.append(f"{label}-metadata-invalid")
    elif normalized.get("metadataDigest") != _digest(metadata):
        errors.append(f"{label}-metadata-digest-mismatch")
    if not normalized.get("pinnedBy") or not normalized.get("verifiedAt") or not normalized.get("activatedAt"):
        errors.append(f"{label}-operator-pin-incomplete")
    if normalized.get("revokedAt") is not None:
        errors.append(f"{label}-revoked")
    return normalized


def _validate_signer_record(
    record: Any,
    identity: dict[str, Any],
    label: str,
    errors: list[str],
    *,
    validation_time: datetime,
    required_purposes: Sequence[str],
    revoked: bool,
) -> dict[str, Any]:
    normalized = _as_dict(record, label, errors)
    if not normalized:
        return {}
    if set(normalized) != _SIGNER_RECORD_FIELDS:
        errors.append(f"{label}-fields-invalid")
    if normalized.get("schema") != federation_peer_trust.ONLINE_SIGNER_TRUST_RECORD_SCHEMA:
        errors.append(f"{label}-schema-invalid")
    if normalized.get("peerFleetId") != identity.get("fleetId"):
        errors.append(f"{label}-fleet-mismatch")
    certificate = _as_dict(normalized.get("certificate"), f"{label}-certificate", errors)
    if certificate:
        if normalized.get("signerKeyId") != certificate.get("signerKeyId"):
            errors.append(f"{label}-key-binding-mismatch")
        if normalized.get("sequence") != certificate.get("sequence"):
            errors.append(f"{label}-sequence-binding-mismatch")
        if normalized.get("certificateDigest") != _digest(certificate):
            errors.append(f"{label}-certificate-digest-mismatch")
        certificate_time = validation_time
        if revoked:
            accepted_at = _parse_timestamp(normalized.get("acceptedAt"), f"{label}.acceptedAt", errors)
            if accepted_at is not None:
                certificate_time = accepted_at
        certificate_errors = federation_identity.validate_online_signer_certificate(
            certificate,
            identity,
            now=certificate_time,
        )
        for code in certificate_errors:
            errors.append(f"{label}-certificate-invalid:{code}")
        purposes = certificate.get("purposes")
        if type(purposes) is not list or not set(required_purposes) <= set(purposes):
            errors.append(f"{label}-purpose-missing")
    revoked_at = normalized.get("revokedAt")
    if revoked:
        parsed_revoked = _parse_timestamp(revoked_at, f"{label}.revokedAt", errors)
        if parsed_revoked is not None and parsed_revoked > validation_time:
            errors.append(f"{label}-revocation-from-future")
        if not normalized.get("revokedBy") or not normalized.get("revocationReason"):
            errors.append(f"{label}-revocation-incomplete")
    elif revoked_at is not None:
        errors.append(f"{label}-unexpectedly-revoked")
    return normalized


def _readiness_signature(
    document: Any,
    identity: dict[str, Any],
    errors: list[str],
    *,
    label: str,
    verification_time: datetime,
) -> dict[str, Any]:
    readiness = _as_dict(document, label, errors)
    if not readiness:
        return {}
    if set(readiness) != _READINESS_FIELDS or readiness.get("schema") != federation_readiness_attestation.READINESS_ATTESTATION_SCHEMA:
        errors.append(f"{label}-fields-invalid")
        return readiness
    certificate = _as_dict(readiness.get("signerCertificate"), f"{label}-certificate", errors)
    if not certificate:
        return readiness
    try:
        verified = federation_identity.verify_federation_document(
            readiness,
            certificate=certificate,
            root_identity=identity,
            expected_schema=federation_readiness_attestation.READINESS_ATTESTATION_SCHEMA,
            now=verification_time,
            required_purpose=federation_identity.PURPOSE_READINESS_ATTESTATION,
        )
    except federation_identity.FederationIdentityError:
        errors.append(f"{label}-signature-invalid")
        return readiness
    snapshot = verified.get("snapshot")
    try:
        validated_snapshot = federation_readiness_attestation._validate_snapshot(
            snapshot,
            expected_fleet_id=str(identity.get("fleetId") or ""),
            now=verification_time,
            max_snapshot_age_seconds=300,
            max_future_skew_seconds=30,
        )
        if verified.get("snapshotDigest") != validated_snapshot.get("snapshotDigest"):
            errors.append(f"{label}-snapshot-digest-mismatch")
    except federation_readiness_attestation.FederationReadinessError:
        errors.append(f"{label}-snapshot-invalid")
    return verified


def _validate_readiness(
    readiness: Any,
    high_water: Any,
    identity: dict[str, Any],
    signer_record: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    verified = _readiness_signature(
        readiness,
        identity,
        errors,
        label="readiness",
        verification_time=validated_at,
    )
    if not verified:
        return
    raw_certificate = signer_record.get("certificate")
    certificate: dict[str, Any] = raw_certificate if type(raw_certificate) is dict else {}
    if not certificate:
        errors.append("readiness-signer-certificate-missing")
    if verified.get("signerCertificate") != certificate or verified.get("signerKeyId") != signer_record.get("signerKeyId"):
        errors.append("readiness-signer-binding-mismatch")
    if verified.get("fleetId") != identity.get("fleetId"):
        errors.append("readiness-fleet-mismatch")
    sequence = verified.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        errors.append("readiness-sequence-invalid")
    signed_at = _parse_timestamp(verified.get("signedAt"), "readiness.signedAt", errors)
    expires_at = _parse_timestamp(verified.get("expiresAt"), "readiness.expiresAt", errors)
    if signed_at is not None and expires_at is not None:
        lifetime = (expires_at - signed_at).total_seconds()
        if lifetime <= 0 or lifetime > federation_readiness_attestation.MAX_READINESS_ATTESTATION_LIFETIME_SECONDS:
            errors.append("readiness-lifetime-invalid")
        if validated_at >= expires_at:
            errors.append("readiness-expired")
        if (signed_at - validated_at).total_seconds() > 30:
            errors.append("readiness-from-future")
        certificate_not_before = _parse_timestamp(certificate.get("notBefore"), "readiness.certificate.notBefore", errors)
        certificate_expires_at = _parse_timestamp(certificate.get("expiresAt"), "readiness.certificate.expiresAt", errors)
        if (
            certificate_not_before is not None
            and certificate_expires_at is not None
            and (signed_at < certificate_not_before or expires_at > certificate_expires_at)
        ):
            errors.append("readiness-signer-window-invalid")
    record = _as_dict(high_water, "readiness-high-water", errors)
    if record and (
        record.get("schema") != federation_peer_trust.READINESS_SEQUENCE_RECORD_SCHEMA
        or record.get("peerFleetId") != identity.get("fleetId")
        or record.get("highSequence") != sequence
        or record.get("signerKeyId") != verified.get("signerKeyId")
        or record.get("attestationDigest") != federation_readiness_attestation.attestation_digest(verified)
    ):
        errors.append("readiness-high-water-binding-mismatch")


def _verify_document(
    document: dict[str, Any],
    *,
    certificate: dict[str, Any],
    identity: dict[str, Any],
    expected_schema: str,
    validation_time: datetime,
) -> bool:
    try:
        federation_identity.verify_federation_document(
            document,
            certificate=certificate,
            root_identity=identity,
            expected_schema=expected_schema,
            now=validation_time,
            required_purpose=federation_identity.PURPOSE_SESSION_AUTHENTICATION,
        )
    except federation_identity.FederationIdentityError:
        return False
    return True


def _validate_challenge(
    challenge_value: Any,
    response_value: Any,
    source_identity: dict[str, Any],
    destination_identity: dict[str, Any],
    source_signer: dict[str, Any],
    destination_signer: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    challenge = _as_dict(challenge_value, "challenge", errors)
    response = _as_dict(response_value, "challenge-response", errors)
    if not challenge or not response:
        return
    if set(challenge) != _CHALLENGE_FIELDS or challenge.get("schema") != federation_challenge.CHALLENGE_SCHEMA:
        errors.append("challenge-fields-invalid")
    if set(response) != _CHALLENGE_RESPONSE_FIELDS or response.get("schema") != federation_challenge.CHALLENGE_RESPONSE_SCHEMA:
        errors.append("challenge-response-fields-invalid")
    source_certificate = _as_dict(source_signer.get("certificate"), "source-signer-certificate", errors)
    destination_certificate = _as_dict(destination_signer.get("certificate"), "destination-signer-certificate", errors)
    if challenge.get("signerCertificate") != source_certificate or challenge.get("signerKeyId") != source_signer.get("signerKeyId"):
        errors.append("challenge-source-signer-mismatch")
    if response.get("signerCertificate") != destination_certificate or response.get("signerKeyId") != destination_signer.get("signerKeyId"):
        errors.append("challenge-destination-signer-mismatch")
    if source_certificate and not _verify_document(
        challenge,
        certificate=source_certificate,
        identity=source_identity,
        expected_schema=federation_challenge.CHALLENGE_SCHEMA,
        validation_time=validated_at,
    ):
        errors.append("challenge-signature-invalid")
    if destination_certificate and not _verify_document(
        response,
        certificate=destination_certificate,
        identity=destination_identity,
        expected_schema=federation_challenge.CHALLENGE_RESPONSE_SCHEMA,
        validation_time=validated_at,
    ):
        errors.append("challenge-response-signature-invalid")
    source = source_identity.get("fleetId")
    destination = destination_identity.get("fleetId")
    if (
        challenge.get("fleetId") != source
        or challenge.get("sourceFleetId") != source
        or challenge.get("destinationFleetId") != destination
        or response.get("fleetId") != destination
        or response.get("sourceFleetId") != source
        or response.get("destinationFleetId") != destination
    ):
        errors.append("challenge-fleet-binding-mismatch")
    try:
        federation_challenge.nonce_digest(str(challenge.get("nonce") or ""))
    except federation_challenge.FederationChallengeError:
        errors.append("challenge-nonce-invalid")
    if response.get("nonce") != challenge.get("nonce"):
        errors.append("challenge-nonce-binding-mismatch")
    if response.get("challengeDigest") != federation_challenge.challenge_digest(challenge):
        errors.append("challenge-digest-binding-mismatch")
    if response.get("sessionPurpose") != challenge.get("sessionPurpose"):
        errors.append("challenge-purpose-binding-mismatch")
    issued_at = _parse_timestamp(challenge.get("issuedAt"), "challenge.issuedAt", errors)
    responded_at = _parse_timestamp(response.get("respondedAt"), "challengeResponse.respondedAt", errors)
    expires_at = _parse_timestamp(challenge.get("expiresAt"), "challenge.expiresAt", errors)
    response_expires = _parse_timestamp(response.get("expiresAt"), "challengeResponse.expiresAt", errors)
    if issued_at is not None and responded_at is not None and expires_at is not None:
        if not issued_at <= responded_at <= validated_at < expires_at:
            errors.append("challenge-time-binding-invalid")
        lifetime = (expires_at - issued_at).total_seconds()
        if lifetime <= 0 or lifetime > federation_challenge.MAX_CHALLENGE_LIFETIME_SECONDS:
            errors.append("challenge-lifetime-invalid")
        source_not_before = _parse_timestamp(source_certificate.get("notBefore"), "challenge.certificate.notBefore", errors)
        source_expires = _parse_timestamp(source_certificate.get("expiresAt"), "challenge.certificate.expiresAt", errors)
        destination_not_before = _parse_timestamp(
            destination_certificate.get("notBefore"), "challengeResponse.certificate.notBefore", errors
        )
        destination_expires = _parse_timestamp(
            destination_certificate.get("expiresAt"), "challengeResponse.certificate.expiresAt", errors
        )
        if source_not_before is not None and source_expires is not None and (
            issued_at < source_not_before or expires_at > source_expires
        ):
            errors.append("challenge-source-signer-window-invalid")
        if destination_not_before is not None and destination_expires is not None and (
            responded_at < destination_not_before or expires_at > destination_expires
        ):
            errors.append("challenge-destination-signer-window-invalid")
    if response_expires != expires_at:
        errors.append("challenge-expiry-binding-mismatch")


def _historical_readiness(
    document: Any,
    identity: dict[str, Any],
    errors: list[str],
    *,
    label: str,
) -> tuple[dict[str, Any], datetime | None, datetime | None]:
    raw = _as_dict(document, label, errors)
    if not raw:
        return {}, None, None
    signed_at = _parse_timestamp(raw.get("signedAt"), f"{label}.signedAt", errors)
    expires_at = _parse_timestamp(raw.get("expiresAt"), f"{label}.expiresAt", errors)
    if signed_at is None:
        return raw, signed_at, expires_at
    verified = _readiness_signature(raw, identity, errors, label=label, verification_time=signed_at)
    return verified, signed_at, expires_at


def _validate_failure_observations(
    value: Any,
    *,
    source_identity: dict[str, Any],
    destination_identity: dict[str, Any],
    revoked_signer: dict[str, Any],
    readiness_high_water: dict[str, Any],
    challenge: dict[str, Any],
    validated_at: datetime,
    errors: list[str],
) -> None:
    if type(value) is not list:
        errors.append("failure-observations-must-be-list")
        return
    observations = {str(item.get("claim") or ""): item for item in value if type(item) is dict}
    if set(observations) != set(_FAILURE_CODES) or len(value) != len(_FAILURE_CODES):
        errors.append("failure-observation-inventory-mismatch")
    for claim, expected_code in _FAILURE_CODES.items():
        observation = _as_dict(observations.get(claim), f"failure:{claim}", errors)
        if not observation:
            continue
        if set(observation) != _FAILURE_FIELDS:
            errors.append(f"failure-fields-invalid:{claim}")
        if observation.get("code") != expected_code:
            errors.append(f"failure-code-mismatch:{claim}")
        before = _typed_digest(observation.get("preStateDigest"), f"{claim}.preStateDigest", errors)
        after = _typed_digest(observation.get("postStateDigest"), f"{claim}.postStateDigest", errors)
        if before and after and before != after:
            errors.append(f"failure-mutated-state:{claim}")
        document = observation.get("document")
        if type(document) is not dict or observation.get("documentDigest") != _digest(document):
            errors.append(f"failure-document-digest-mismatch:{claim}")
            continue
        if claim == "trustOnFirstUseIsRejected":
            candidate = _as_dict(document.get("fleetIdentity"), "tofu-identity", errors)
            attestation = _as_dict(document.get("attestation"), "tofu-attestation", errors)
            candidate_identity = _validate_identity(candidate, "tofu", errors)
            if (
                not candidate_identity
                or candidate_identity.get("fleetId") in {source_identity.get("fleetId"), destination_identity.get("fleetId")}
                or not attestation
            ):
                errors.append("tofu-evidence-invalid")
            elif not _historical_readiness(attestation, candidate_identity, errors, label="tofu-readiness")[0]:
                errors.append("tofu-evidence-invalid")
        elif claim == "readinessSequenceReplayIsRejected":
            replay, _signed, _expires = _historical_readiness(document, destination_identity, errors, label="replayed-readiness")
            high_sequence = readiness_high_water.get("highSequence")
            if not replay or not isinstance(high_sequence, int) or not isinstance(replay.get("sequence"), int) or replay["sequence"] >= high_sequence:
                errors.append("readiness-replay-evidence-invalid")
        elif claim == "expiredReadinessAttestationIsRejected":
            expired, _signed, expires = _historical_readiness(document, destination_identity, errors, label="expired-readiness")
            if not expired or expires is None or validated_at < expires:
                errors.append("expired-readiness-evidence-invalid")
        elif claim == "futureReadinessAttestationBeyondSkewIsRejected":
            future, signed, _expires = _historical_readiness(document, destination_identity, errors, label="future-readiness")
            if not future or signed is None or (signed - validated_at).total_seconds() <= 30:
                errors.append("future-readiness-evidence-invalid")
        elif claim == "challengeNonceReplayIsRejected":
            if document != challenge or federation_challenge.challenge_digest(document) != federation_challenge.challenge_digest(challenge):
                errors.append("challenge-replay-evidence-invalid")
        elif claim == "revokedFederationSignerIsRejected":
            revoked, _signed, _expires = _historical_readiness(document, destination_identity, errors, label="revoked-readiness")
            if (
                not revoked
                or revoked.get("signerKeyId") != revoked_signer.get("signerKeyId")
                or revoked.get("signerCertificate") != revoked_signer.get("certificate")
                or revoked_signer.get("revokedAt") is None
            ):
                errors.append("revoked-signer-evidence-invalid")


def build_federation_trust_proof(
    *,
    validated_at: datetime,
    source_fleet_identity: dict[str, Any],
    destination_fleet_identity: dict[str, Any],
    source_peer_trust: dict[str, Any] | None,
    destination_peer_trust: dict[str, Any] | None,
    source_signer_trust: dict[str, Any] | None,
    destination_signer_trust: dict[str, Any] | None,
    revoked_destination_signer_trust: dict[str, Any] | None,
    readiness_attestation: dict[str, Any],
    readiness_high_water: dict[str, Any] | None,
    challenge: dict[str, Any],
    challenge_response: dict[str, Any],
    age_recipients: dict[str, str],
    authority_identity_digests: dict[str, str],
    failure_observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": FEDERATION_TRUST_PROOF_SCHEMA,
        "validatedAt": _utc_iso(validated_at),
        "sourceFleetIdentity": copy.deepcopy(source_fleet_identity),
        "destinationFleetIdentity": copy.deepcopy(destination_fleet_identity),
        "sourcePeerTrust": copy.deepcopy(source_peer_trust),
        "destinationPeerTrust": copy.deepcopy(destination_peer_trust),
        "sourceSignerTrust": copy.deepcopy(source_signer_trust),
        "destinationSignerTrust": copy.deepcopy(destination_signer_trust),
        "revokedDestinationSignerTrust": copy.deepcopy(revoked_destination_signer_trust),
        "readinessAttestation": copy.deepcopy(readiness_attestation),
        "readinessHighWater": copy.deepcopy(readiness_high_water),
        "challenge": copy.deepcopy(challenge),
        "challengeResponse": copy.deepcopy(challenge_response),
        "ageRecipients": copy.deepcopy(age_recipients),
        "authorityIdentityDigests": copy.deepcopy(authority_identity_digests),
        "failureObservations": [copy.deepcopy(item) for item in failure_observations],
    }
    payload["proofDigest"] = proof_digest(payload)
    errors = validate_federation_trust_proof(payload)
    if errors:
        raise ValueError("invalid federation trust proof: " + "; ".join(errors))
    return payload


def validate_federation_trust_proof(value: Any) -> list[str]:
    """Recompute trust, signature, replay, rotation, and identity separation claims."""

    errors: list[str] = []
    try:
        normalized = federation_identity.normalize_federation_json(value)
    except federation_identity.FederationIdentityError:
        return ["federation-proof-canonical-payload-invalid"]
    if type(normalized) is not dict:
        return ["federation-trust-proof-must-be-object"]
    if set(normalized) != _PROOF_FIELDS:
        errors.append("federation-trust-proof-fields-invalid")
    if normalized.get("schema") != FEDERATION_TRUST_PROOF_SCHEMA:
        errors.append("federation-trust-proof-schema-invalid")
    try:
        federation_identity.assert_federation_document_secret_free(normalized)
    except federation_identity.FederationIdentityError:
        errors.append("federation-proof-contains-secret")
    validated_at = _parse_timestamp(normalized.get("validatedAt"), "validatedAt", errors)
    source_identity = _validate_identity(normalized.get("sourceFleetIdentity"), "source", errors)
    destination_identity = _validate_identity(normalized.get("destinationFleetIdentity"), "destination", errors)
    if source_identity and destination_identity:
        if source_identity.get("fleetId") == destination_identity.get("fleetId"):
            errors.append("fleet-identities-not-distinct")
        if source_identity.get("rootKeyId") == destination_identity.get("rootKeyId"):
            errors.append("federation-root-keys-not-distinct")
        if source_identity.get("rootFingerprint") == destination_identity.get("rootFingerprint"):
            errors.append("federation-root-fingerprints-not-distinct")
    _validate_peer_record(normalized.get("sourcePeerTrust"), destination_identity, "source-peer-trust", errors)
    _validate_peer_record(
        normalized.get("destinationPeerTrust"), source_identity, "destination-peer-trust", errors
    )
    source_signer: dict[str, Any] = {}
    destination_signer: dict[str, Any] = {}
    revoked_signer: dict[str, Any] = {}
    if validated_at is not None:
        source_signer = _validate_signer_record(
            normalized.get("sourceSignerTrust"),
            source_identity,
            "source-signer-trust",
            errors,
            validation_time=validated_at,
            required_purposes=(federation_identity.PURPOSE_SESSION_AUTHENTICATION,),
            revoked=False,
        )
        destination_signer = _validate_signer_record(
            normalized.get("destinationSignerTrust"),
            destination_identity,
            "destination-signer-trust",
            errors,
            validation_time=validated_at,
            required_purposes=(
                federation_identity.PURPOSE_READINESS_ATTESTATION,
                federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            ),
            revoked=False,
        )
        revoked_signer = _validate_signer_record(
            normalized.get("revokedDestinationSignerTrust"),
            destination_identity,
            "revoked-destination-signer-trust",
            errors,
            validation_time=validated_at,
            required_purposes=(federation_identity.PURPOSE_READINESS_ATTESTATION,),
            revoked=True,
        )
        if destination_signer and revoked_signer:
            active_sequence = destination_signer.get("sequence")
            revoked_sequence = revoked_signer.get("sequence")
            if not isinstance(active_sequence, int) or not isinstance(revoked_sequence, int) or active_sequence <= revoked_sequence:
                errors.append("signer-rotation-sequence-not-increased")
            if destination_signer.get("signerKeyId") == revoked_signer.get("signerKeyId"):
                errors.append("rotated-signer-key-not-distinct")
        _validate_readiness(
            normalized.get("readinessAttestation"),
            normalized.get("readinessHighWater"),
            destination_identity,
            destination_signer,
            validated_at,
            errors,
        )
        _validate_challenge(
            normalized.get("challenge"),
            normalized.get("challengeResponse"),
            source_identity,
            destination_identity,
            source_signer,
            destination_signer,
            validated_at,
            errors,
        )

    age_recipients = _as_dict(normalized.get("ageRecipients"), "age-recipients", errors)
    authority_digests = _as_dict(normalized.get("authorityIdentityDigests"), "authority-identity-digests", errors)
    fleet_ids = {str(source_identity.get("fleetId") or ""), str(destination_identity.get("fleetId") or "")}
    if not source_identity or not destination_identity or set(age_recipients) != fleet_ids:
        errors.append("age-recipient-inventory-mismatch")
    if not source_identity or not destination_identity or set(authority_digests) != fleet_ids:
        errors.append("authority-identity-inventory-mismatch")
    federation_identifiers = {
        str(source_identity.get("rootKeyId") or ""),
        str(source_identity.get("rootFingerprint") or ""),
        str(source_identity.get("rootPublicKey") or ""),
        str(destination_identity.get("rootKeyId") or ""),
        str(destination_identity.get("rootFingerprint") or ""),
        str(destination_identity.get("rootPublicKey") or ""),
        str(source_signer.get("signerKeyId") or ""),
        str(destination_signer.get("signerKeyId") or ""),
    }
    for fleet_id, recipient in age_recipients.items():
        if type(recipient) is not str or not recipient.startswith("age1") or recipient in federation_identifiers:
            errors.append(f"age-recipient-invalid:{fleet_id}")
    for fleet_id, digest in authority_digests.items():
        normalized_digest = _typed_digest(digest, f"authorityIdentityDigests.{fleet_id}", errors)
        if normalized_digest and normalized_digest in federation_identifiers:
            errors.append(f"authority-identity-not-distinct:{fleet_id}")
    if len(set(str(value) for value in authority_digests.values())) != len(authority_digests):
        errors.append("authority-identities-not-distinct")
    readiness_high_water = _as_dict(normalized.get("readinessHighWater"), "readiness-high-water", errors)
    challenge = _as_dict(normalized.get("challenge"), "challenge", errors)
    if validated_at is not None:
        _validate_failure_observations(
            normalized.get("failureObservations"),
            source_identity=source_identity,
            destination_identity=destination_identity,
            revoked_signer=revoked_signer,
            readiness_high_water=readiness_high_water,
            challenge=challenge,
            validated_at=validated_at,
            errors=errors,
        )
    declared_digest = _typed_digest(normalized.get("proofDigest"), "proofDigest", errors)
    if declared_digest and declared_digest != proof_digest(normalized):
        errors.append("proof-digest-mismatch")
    return list(dict.fromkeys(errors))
