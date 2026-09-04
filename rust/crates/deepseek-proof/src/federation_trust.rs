use crate::federated_replica::{
    contains_secret, dedupe, digest, exact_fields, object_copy, parse_timestamp, positive_integer,
    string, typed_digest,
};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use deepseek_federation::{
    PURPOSE_READINESS_ATTESTATION, PURPOSE_SESSION_AUTHENTICATION, validate_fleet_identity,
    validate_online_signer_certificate, verify_federation_document,
};
use serde_json::{Map, Value};
use std::collections::{HashMap, HashSet};

pub const FEDERATION_TRUST_PROOF_SCHEMA: &str = "federation-trust-proof-v1";
pub const FEDERATION_TRUST_PROOF_CHECKS: [&str; 15] = [
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
];

const PROOF_FIELDS: [&str; 17] = [
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
];
const PEER_RECORD_FIELDS: [&str; 17] = [
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
];
const SIGNER_RECORD_FIELDS: [&str; 12] = [
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
];
const READINESS_FIELDS: [&str; 11] = [
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
];
const SNAPSHOT_FIELDS: [&str; 12] = [
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
];
const CHALLENGE_FIELDS: [&str; 12] = [
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
];
const CHALLENGE_RESPONSE_FIELDS: [&str; 13] = [
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
];
const FAILURE_FIELDS: [&str; 6] = [
    "claim",
    "code",
    "preStateDigest",
    "postStateDigest",
    "documentDigest",
    "document",
];
const FAILURE_CASES: [(&str, &str); 6] = [
    ("trustOnFirstUseIsRejected", "FEDERATION_PEER_NOT_PINNED"),
    (
        "readinessSequenceReplayIsRejected",
        "FEDERATION_READINESS_SEQUENCE_REPLAY",
    ),
    (
        "expiredReadinessAttestationIsRejected",
        "FEDERATION_READINESS_ATTESTATION_EXPIRED",
    ),
    (
        "futureReadinessAttestationBeyondSkewIsRejected",
        "FEDERATION_READINESS_ATTESTATION_FROM_FUTURE",
    ),
    (
        "challengeNonceReplayIsRejected",
        "FEDERATION_CHALLENGE_NONCE_REPLAY",
    ),
    (
        "revokedFederationSignerIsRejected",
        "FEDERATION_SIGNER_REVOKED",
    ),
];
const PINNED_METADATA_FIELDS: [&str; 4] = ["provider", "region", "jurisdiction", "siteClass"];

const PEER_RECORD_SCHEMA: &str = "federation-peer-trust-record-v1";
const SIGNER_RECORD_SCHEMA: &str = "federation-online-signer-trust-record-v1";
const READINESS_SCHEMA: &str = "federation-readiness-attestation-v1";
const READINESS_SEQUENCE_SCHEMA: &str = "federation-readiness-sequence-v1";
const SNAPSHOT_SCHEMA: &str = "federation-readiness-snapshot-v1";
const CHALLENGE_SCHEMA: &str = "federation-challenge-v1";
const CHALLENGE_RESPONSE_SCHEMA: &str = "federation-challenge-response-v1";
const MAX_READINESS_LIFETIME_SECONDS: i64 = 300;
const MAX_CHALLENGE_LIFETIME_SECONDS: i64 = 120;

pub fn federation_trust_proof_digest(proof: &Value) -> Result<String, String> {
    let fields = proof
        .as_object()
        .ok_or_else(|| "federation-trust-proof-must-be-object".to_string())?;
    let mut payload = fields.clone();
    payload.remove("proofDigest");
    digest(&Value::Object(payload))
}

pub fn validate_federation_trust_proof(value: &Value) -> Vec<String> {
    let Some(proof) = value.as_object() else {
        return vec!["federation-trust-proof-must-be-object".to_string()];
    };
    let mut errors = Vec::new();
    if !exact_fields(proof, &PROOF_FIELDS) {
        errors.push("federation-trust-proof-fields-invalid".to_string());
    }
    if string(proof, "schema") != FEDERATION_TRUST_PROOF_SCHEMA {
        errors.push("federation-trust-proof-schema-invalid".to_string());
    }
    if contains_secret(value) {
        errors.push("federation-proof-contains-secret".to_string());
    }
    let validated_at_text = string(proof, "validatedAt").to_string();
    let validated_at = parse_timestamp(&validated_at_text);
    if validated_at.is_none() {
        errors.push("invalid-timestamp:validatedAt".to_string());
    }

    let source_identity =
        validate_identity(proof.get("sourceFleetIdentity"), "source", &mut errors);
    let destination_identity = validate_identity(
        proof.get("destinationFleetIdentity"),
        "destination",
        &mut errors,
    );
    if !source_identity.is_empty() && !destination_identity.is_empty() {
        if source_identity.get("fleetId") == destination_identity.get("fleetId") {
            errors.push("fleet-identities-not-distinct".to_string());
        }
        if source_identity.get("rootKeyId") == destination_identity.get("rootKeyId") {
            errors.push("federation-root-keys-not-distinct".to_string());
        }
        if source_identity.get("rootFingerprint") == destination_identity.get("rootFingerprint") {
            errors.push("federation-root-fingerprints-not-distinct".to_string());
        }
    }
    validate_peer_record(
        proof.get("sourcePeerTrust"),
        &destination_identity,
        "source-peer-trust",
        &mut errors,
    );
    validate_peer_record(
        proof.get("destinationPeerTrust"),
        &source_identity,
        "destination-peer-trust",
        &mut errors,
    );

    let mut source_signer = Map::new();
    let mut destination_signer = Map::new();
    let mut revoked_signer = Map::new();
    if let Some(validated_at) = validated_at {
        source_signer = validate_signer_record(
            proof.get("sourceSignerTrust"),
            &source_identity,
            "source-signer-trust",
            &validated_at_text,
            &[PURPOSE_SESSION_AUTHENTICATION],
            false,
            &mut errors,
        );
        destination_signer = validate_signer_record(
            proof.get("destinationSignerTrust"),
            &destination_identity,
            "destination-signer-trust",
            &validated_at_text,
            &[
                PURPOSE_READINESS_ATTESTATION,
                PURPOSE_SESSION_AUTHENTICATION,
            ],
            false,
            &mut errors,
        );
        revoked_signer = validate_signer_record(
            proof.get("revokedDestinationSignerTrust"),
            &destination_identity,
            "revoked-destination-signer-trust",
            &validated_at_text,
            &[PURPOSE_READINESS_ATTESTATION],
            true,
            &mut errors,
        );
        if !destination_signer.is_empty() && !revoked_signer.is_empty() {
            let active_sequence = integer(destination_signer.get("sequence"));
            let revoked_sequence = integer(revoked_signer.get("sequence"));
            if active_sequence
                .zip(revoked_sequence)
                .is_none_or(|(active, revoked)| active <= revoked)
            {
                errors.push("signer-rotation-sequence-not-increased".to_string());
            }
            if destination_signer.get("signerKeyId") == revoked_signer.get("signerKeyId") {
                errors.push("rotated-signer-key-not-distinct".to_string());
            }
        }
        validate_readiness(
            proof.get("readinessAttestation"),
            proof.get("readinessHighWater"),
            &destination_identity,
            &destination_signer,
            &validated_at_text,
            validated_at,
            &mut errors,
        );
        validate_challenge(
            proof.get("challenge"),
            proof.get("challengeResponse"),
            &source_identity,
            &destination_identity,
            &source_signer,
            &destination_signer,
            &validated_at_text,
            validated_at,
            &mut errors,
        );
    }

    let age_recipients = object_copy(proof.get("ageRecipients"), "age-recipients", &mut errors);
    let authority_digests = object_copy(
        proof.get("authorityIdentityDigests"),
        "authority-identity-digests",
        &mut errors,
    );
    let fleet_ids = HashSet::from([
        string(&source_identity, "fleetId").to_string(),
        string(&destination_identity, "fleetId").to_string(),
    ]);
    if source_identity.is_empty()
        || destination_identity.is_empty()
        || age_recipients.keys().cloned().collect::<HashSet<_>>() != fleet_ids
    {
        errors.push("age-recipient-inventory-mismatch".to_string());
    }
    if source_identity.is_empty()
        || destination_identity.is_empty()
        || authority_digests.keys().cloned().collect::<HashSet<_>>() != fleet_ids
    {
        errors.push("authority-identity-inventory-mismatch".to_string());
    }
    let federation_identifiers = HashSet::from([
        string(&source_identity, "rootKeyId").to_string(),
        string(&source_identity, "rootFingerprint").to_string(),
        string(&source_identity, "rootPublicKey").to_string(),
        string(&destination_identity, "rootKeyId").to_string(),
        string(&destination_identity, "rootFingerprint").to_string(),
        string(&destination_identity, "rootPublicKey").to_string(),
        string(&source_signer, "signerKeyId").to_string(),
        string(&destination_signer, "signerKeyId").to_string(),
    ]);
    for (fleet_id, recipient) in &age_recipients {
        if recipient.as_str().is_none_or(|recipient| {
            !recipient.starts_with("age1") || federation_identifiers.contains(recipient)
        }) {
            errors.push(format!("age-recipient-invalid:{fleet_id}"));
        }
    }
    for (fleet_id, identity_digest) in &authority_digests {
        let identity_digest = typed_digest(
            Some(identity_digest),
            &format!("authorityIdentityDigests.{fleet_id}"),
            &mut errors,
        );
        if !identity_digest.is_empty() && federation_identifiers.contains(&identity_digest) {
            errors.push(format!("authority-identity-not-distinct:{fleet_id}"));
        }
    }
    let distinct_authorities = authority_digests
        .values()
        .map(Value::to_string)
        .collect::<HashSet<_>>();
    if distinct_authorities.len() != authority_digests.len() {
        errors.push("authority-identities-not-distinct".to_string());
    }

    let readiness_high_water = object_copy(
        proof.get("readinessHighWater"),
        "readiness-high-water",
        &mut errors,
    );
    let challenge = object_copy(proof.get("challenge"), "challenge", &mut errors);
    if let Some(validated_at) = validated_at {
        validate_failure_observations(
            proof.get("failureObservations"),
            &source_identity,
            &destination_identity,
            &revoked_signer,
            &readiness_high_water,
            &challenge,
            validated_at,
            &mut errors,
        );
    }
    let declared = typed_digest(proof.get("proofDigest"), "proofDigest", &mut errors);
    if !declared.is_empty()
        && federation_trust_proof_digest(value).is_ok_and(|computed| computed != declared)
    {
        errors.push("proof-digest-mismatch".to_string());
    }
    dedupe(errors)
}

fn validate_identity(
    value: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let null = Value::Null;
    let value = value.unwrap_or(&null);
    match validate_fleet_identity(value) {
        Ok(()) => value.as_object().cloned().unwrap_or_default(),
        Err(error) => {
            errors.push(format!("{label}-identity-invalid:{}", error.code()));
            Map::new()
        }
    }
}

fn validate_peer_record(
    value: Option<&Value>,
    identity: &Map<String, Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let record = object_copy(value, label, errors);
    if record.is_empty() {
        return record;
    }
    if !exact_fields(&record, &PEER_RECORD_FIELDS) {
        errors.push(format!("{label}-fields-invalid"));
    }
    if string(&record, "schema") != PEER_RECORD_SCHEMA {
        errors.push(format!("{label}-schema-invalid"));
    }
    if string(&record, "state") != "ACTIVE" {
        errors.push(format!("{label}-not-active"));
    }
    if record.get("peerFleetId") != identity.get("fleetId") {
        errors.push(format!("{label}-fleet-mismatch"));
    }
    if record.get("fleetIdentity") != Some(&Value::Object(identity.clone())) {
        errors.push(format!("{label}-identity-binding-mismatch"));
    }
    if record.get("rootKeyId") != identity.get("rootKeyId") {
        errors.push(format!("{label}-root-key-mismatch"));
    }
    if record.get("rootFingerprint") != identity.get("rootFingerprint") {
        errors.push(format!("{label}-root-fingerprint-mismatch"));
    }
    let metadata = record.get("pinnedMetadata").and_then(Value::as_object);
    if metadata.is_none_or(|metadata| {
        !exact_fields(metadata, &PINNED_METADATA_FIELDS)
            || metadata
                .values()
                .any(|value| value.as_str().is_none_or(str::is_empty))
    }) {
        errors.push(format!("{label}-metadata-invalid"));
    } else if record.get("metadataDigest").and_then(Value::as_str)
        != metadata
            .and_then(|metadata| digest(&Value::Object(metadata.clone())).ok())
            .as_deref()
    {
        errors.push(format!("{label}-metadata-digest-mismatch"));
    }
    if !truthy(record.get("pinnedBy"))
        || !truthy(record.get("verifiedAt"))
        || !truthy(record.get("activatedAt"))
    {
        errors.push(format!("{label}-operator-pin-incomplete"));
    }
    if record
        .get("revokedAt")
        .is_some_and(|value| !value.is_null())
    {
        errors.push(format!("{label}-revoked"));
    }
    record
}

#[allow(clippy::too_many_arguments)]
fn validate_signer_record(
    value: Option<&Value>,
    identity: &Map<String, Value>,
    label: &str,
    validated_at: &str,
    required_purposes: &[&str],
    revoked: bool,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let record = object_copy(value, label, errors);
    if record.is_empty() {
        return record;
    }
    if !exact_fields(&record, &SIGNER_RECORD_FIELDS) {
        errors.push(format!("{label}-fields-invalid"));
    }
    if string(&record, "schema") != SIGNER_RECORD_SCHEMA {
        errors.push(format!("{label}-schema-invalid"));
    }
    if record.get("peerFleetId") != identity.get("fleetId") {
        errors.push(format!("{label}-fleet-mismatch"));
    }
    let certificate = object_copy(
        record.get("certificate"),
        &format!("{label}-certificate"),
        errors,
    );
    if !certificate.is_empty() {
        if record.get("signerKeyId") != certificate.get("signerKeyId") {
            errors.push(format!("{label}-key-binding-mismatch"));
        }
        if record.get("sequence") != certificate.get("sequence") {
            errors.push(format!("{label}-sequence-binding-mismatch"));
        }
        if record.get("certificateDigest").and_then(Value::as_str)
            != digest(&Value::Object(certificate.clone())).ok().as_deref()
        {
            errors.push(format!("{label}-certificate-digest-mismatch"));
        }
        let mut certificate_time = validated_at.to_string();
        if revoked
            && labeled_timestamp(
                record.get("acceptedAt"),
                &format!("{label}.acceptedAt"),
                errors,
            )
            .is_some()
        {
            certificate_time = string(&record, "acceptedAt").to_string();
        }
        for code in validate_online_signer_certificate(
            &Value::Object(certificate.clone()),
            &Value::Object(identity.clone()),
            &certificate_time,
            None,
        ) {
            errors.push(format!("{label}-certificate-invalid:{code}"));
        }
        let purposes = certificate.get("purposes").and_then(Value::as_array);
        if purposes.is_none_or(|purposes| {
            required_purposes.iter().any(|required| {
                !purposes
                    .iter()
                    .any(|purpose| purpose.as_str() == Some(*required))
            })
        }) {
            errors.push(format!("{label}-purpose-missing"));
        }
    }
    if revoked {
        let revoked_at = labeled_timestamp(
            record.get("revokedAt"),
            &format!("{label}.revokedAt"),
            errors,
        );
        let validated = parse_timestamp(validated_at).unwrap_or_default();
        if revoked_at.is_some_and(|revoked_at| revoked_at > validated) {
            errors.push(format!("{label}-revocation-from-future"));
        }
        if !truthy(record.get("revokedBy")) || !truthy(record.get("revocationReason")) {
            errors.push(format!("{label}-revocation-incomplete"));
        }
    } else if record
        .get("revokedAt")
        .is_some_and(|value| !value.is_null())
    {
        errors.push(format!("{label}-unexpectedly-revoked"));
    }
    record
}

fn readiness_signature(
    value: Option<&Value>,
    identity: &Map<String, Value>,
    label: &str,
    verification_time: &str,
    verification_epoch: i64,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let readiness = object_copy(value, label, errors);
    if readiness.is_empty() {
        return readiness;
    }
    if !exact_fields(&readiness, &READINESS_FIELDS)
        || string(&readiness, "schema") != READINESS_SCHEMA
    {
        errors.push(format!("{label}-fields-invalid"));
        return readiness;
    }
    let certificate = object_copy(
        readiness.get("signerCertificate"),
        &format!("{label}-certificate"),
        errors,
    );
    if certificate.is_empty() {
        return readiness;
    }
    let verified = verify_federation_document(
        &Value::Object(readiness.clone()),
        &certificate,
        &Value::Object(identity.clone()),
        READINESS_SCHEMA,
        verification_time,
        PURPOSE_READINESS_ATTESTATION,
    );
    let Ok(verified) = verified else {
        errors.push(format!("{label}-signature-invalid"));
        return readiness;
    };
    let verified = verified.as_object().cloned().unwrap_or(readiness);
    match validate_snapshot(
        verified.get("snapshot"),
        string(identity, "fleetId"),
        verification_epoch,
    ) {
        Some(snapshot) => {
            if verified.get("snapshotDigest") != snapshot.get("snapshotDigest") {
                errors.push(format!("{label}-snapshot-digest-mismatch"));
            }
        }
        None => errors.push(format!("{label}-snapshot-invalid")),
    }
    verified
}

#[allow(clippy::too_many_arguments)]
fn validate_readiness(
    readiness: Option<&Value>,
    high_water: Option<&Value>,
    identity: &Map<String, Value>,
    signer_record: &Map<String, Value>,
    validated_at_text: &str,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    let verified = readiness_signature(
        readiness,
        identity,
        "readiness",
        validated_at_text,
        validated_at,
        errors,
    );
    if verified.is_empty() {
        return;
    }
    let certificate = signer_record
        .get("certificate")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if certificate.is_empty() {
        errors.push("readiness-signer-certificate-missing".to_string());
    }
    if verified.get("signerCertificate") != Some(&Value::Object(certificate.clone()))
        || verified.get("signerKeyId") != signer_record.get("signerKeyId")
    {
        errors.push("readiness-signer-binding-mismatch".to_string());
    }
    if verified.get("fleetId") != identity.get("fleetId") {
        errors.push("readiness-fleet-mismatch".to_string());
    }
    if !positive_integer(verified.get("sequence")) {
        errors.push("readiness-sequence-invalid".to_string());
    }
    let signed_at = labeled_timestamp(verified.get("signedAt"), "readiness.signedAt", errors);
    let expires_at = labeled_timestamp(verified.get("expiresAt"), "readiness.expiresAt", errors);
    if let Some((signed_at, expires_at)) = signed_at.zip(expires_at) {
        let lifetime = expires_at - signed_at;
        if !(1..=MAX_READINESS_LIFETIME_SECONDS).contains(&lifetime) {
            errors.push("readiness-lifetime-invalid".to_string());
        }
        if validated_at >= expires_at {
            errors.push("readiness-expired".to_string());
        }
        if signed_at.saturating_sub(validated_at) > 30 {
            errors.push("readiness-from-future".to_string());
        }
        let not_before = labeled_timestamp(
            certificate.get("notBefore"),
            "readiness.certificate.notBefore",
            errors,
        );
        let certificate_expires = labeled_timestamp(
            certificate.get("expiresAt"),
            "readiness.certificate.expiresAt",
            errors,
        );
        if not_before
            .zip(certificate_expires)
            .is_some_and(|(not_before, certificate_expires)| {
                signed_at < not_before || expires_at > certificate_expires
            })
        {
            errors.push("readiness-signer-window-invalid".to_string());
        }
    }
    let record = object_copy(high_water, "readiness-high-water", errors);
    if !record.is_empty()
        && (string(&record, "schema") != READINESS_SEQUENCE_SCHEMA
            || record.get("peerFleetId") != identity.get("fleetId")
            || record.get("highSequence") != verified.get("sequence")
            || record.get("signerKeyId") != verified.get("signerKeyId")
            || record.get("attestationDigest").and_then(Value::as_str)
                != digest(&Value::Object(verified)).ok().as_deref())
    {
        errors.push("readiness-high-water-binding-mismatch".to_string());
    }
}

#[allow(clippy::too_many_arguments)]
fn validate_challenge(
    challenge_value: Option<&Value>,
    response_value: Option<&Value>,
    source_identity: &Map<String, Value>,
    destination_identity: &Map<String, Value>,
    source_signer: &Map<String, Value>,
    destination_signer: &Map<String, Value>,
    validated_at_text: &str,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    let challenge = object_copy(challenge_value, "challenge", errors);
    let response = object_copy(response_value, "challenge-response", errors);
    if challenge.is_empty() || response.is_empty() {
        return;
    }
    if !exact_fields(&challenge, &CHALLENGE_FIELDS)
        || string(&challenge, "schema") != CHALLENGE_SCHEMA
    {
        errors.push("challenge-fields-invalid".to_string());
    }
    if !exact_fields(&response, &CHALLENGE_RESPONSE_FIELDS)
        || string(&response, "schema") != CHALLENGE_RESPONSE_SCHEMA
    {
        errors.push("challenge-response-fields-invalid".to_string());
    }
    let source_certificate = object_copy(
        source_signer.get("certificate"),
        "source-signer-certificate",
        errors,
    );
    let destination_certificate = object_copy(
        destination_signer.get("certificate"),
        "destination-signer-certificate",
        errors,
    );
    if challenge.get("signerCertificate") != Some(&Value::Object(source_certificate.clone()))
        || challenge.get("signerKeyId") != source_signer.get("signerKeyId")
    {
        errors.push("challenge-source-signer-mismatch".to_string());
    }
    if response.get("signerCertificate") != Some(&Value::Object(destination_certificate.clone()))
        || response.get("signerKeyId") != destination_signer.get("signerKeyId")
    {
        errors.push("challenge-destination-signer-mismatch".to_string());
    }
    if !source_certificate.is_empty()
        && verify_federation_document(
            &Value::Object(challenge.clone()),
            &source_certificate,
            &Value::Object(source_identity.clone()),
            CHALLENGE_SCHEMA,
            validated_at_text,
            PURPOSE_SESSION_AUTHENTICATION,
        )
        .is_err()
    {
        errors.push("challenge-signature-invalid".to_string());
    }
    if !destination_certificate.is_empty()
        && verify_federation_document(
            &Value::Object(response.clone()),
            &destination_certificate,
            &Value::Object(destination_identity.clone()),
            CHALLENGE_RESPONSE_SCHEMA,
            validated_at_text,
            PURPOSE_SESSION_AUTHENTICATION,
        )
        .is_err()
    {
        errors.push("challenge-response-signature-invalid".to_string());
    }
    if challenge.get("fleetId") != source_identity.get("fleetId")
        || challenge.get("sourceFleetId") != source_identity.get("fleetId")
        || challenge.get("destinationFleetId") != destination_identity.get("fleetId")
        || response.get("fleetId") != destination_identity.get("fleetId")
        || response.get("sourceFleetId") != source_identity.get("fleetId")
        || response.get("destinationFleetId") != destination_identity.get("fleetId")
    {
        errors.push("challenge-fleet-binding-mismatch".to_string());
    }
    if !is_nonce(string(&challenge, "nonce")) {
        errors.push("challenge-nonce-invalid".to_string());
    }
    if response.get("nonce") != challenge.get("nonce") {
        errors.push("challenge-nonce-binding-mismatch".to_string());
    }
    if response.get("challengeDigest").and_then(Value::as_str)
        != digest(&Value::Object(challenge.clone())).ok().as_deref()
    {
        errors.push("challenge-digest-binding-mismatch".to_string());
    }
    if response.get("sessionPurpose") != challenge.get("sessionPurpose") {
        errors.push("challenge-purpose-binding-mismatch".to_string());
    }
    let issued_at = labeled_timestamp(challenge.get("issuedAt"), "challenge.issuedAt", errors);
    let responded_at = labeled_timestamp(
        response.get("respondedAt"),
        "challengeResponse.respondedAt",
        errors,
    );
    let expires_at = labeled_timestamp(challenge.get("expiresAt"), "challenge.expiresAt", errors);
    let response_expires = labeled_timestamp(
        response.get("expiresAt"),
        "challengeResponse.expiresAt",
        errors,
    );
    if let Some((issued_at, responded_at, expires_at)) = issued_at
        .zip(responded_at)
        .zip(expires_at)
        .map(|((a, b), c)| (a, b, c))
    {
        if !(issued_at <= responded_at && responded_at <= validated_at && validated_at < expires_at)
        {
            errors.push("challenge-time-binding-invalid".to_string());
        }
        let lifetime = expires_at - issued_at;
        if !(1..=MAX_CHALLENGE_LIFETIME_SECONDS).contains(&lifetime) {
            errors.push("challenge-lifetime-invalid".to_string());
        }
        let source_not_before = labeled_timestamp(
            source_certificate.get("notBefore"),
            "challenge.certificate.notBefore",
            errors,
        );
        let source_expires = labeled_timestamp(
            source_certificate.get("expiresAt"),
            "challenge.certificate.expiresAt",
            errors,
        );
        let destination_not_before = labeled_timestamp(
            destination_certificate.get("notBefore"),
            "challengeResponse.certificate.notBefore",
            errors,
        );
        let destination_expires = labeled_timestamp(
            destination_certificate.get("expiresAt"),
            "challengeResponse.certificate.expiresAt",
            errors,
        );
        if source_not_before
            .zip(source_expires)
            .is_some_and(|(not_before, signer_expires)| {
                issued_at < not_before || expires_at > signer_expires
            })
        {
            errors.push("challenge-source-signer-window-invalid".to_string());
        }
        if destination_not_before.zip(destination_expires).is_some_and(
            |(not_before, signer_expires)| responded_at < not_before || expires_at > signer_expires,
        ) {
            errors.push("challenge-destination-signer-window-invalid".to_string());
        }
    }
    if response_expires != expires_at {
        errors.push("challenge-expiry-binding-mismatch".to_string());
    }
}

fn historical_readiness(
    value: Option<&Value>,
    identity: &Map<String, Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> (Map<String, Value>, Option<i64>, Option<i64>) {
    let raw = object_copy(value, label, errors);
    if raw.is_empty() {
        return (raw, None, None);
    }
    let signed_at = labeled_timestamp(raw.get("signedAt"), &format!("{label}.signedAt"), errors);
    let expires_at = labeled_timestamp(raw.get("expiresAt"), &format!("{label}.expiresAt"), errors);
    let Some(signed_at_epoch) = signed_at else {
        return (raw, signed_at, expires_at);
    };
    let signed_at_text = string(&raw, "signedAt").to_string();
    let verified = readiness_signature(
        Some(&Value::Object(raw)),
        identity,
        label,
        &signed_at_text,
        signed_at_epoch,
        errors,
    );
    (verified, signed_at, expires_at)
}

#[allow(clippy::too_many_arguments)]
fn validate_failure_observations(
    value: Option<&Value>,
    source_identity: &Map<String, Value>,
    destination_identity: &Map<String, Value>,
    revoked_signer: &Map<String, Value>,
    readiness_high_water: &Map<String, Value>,
    challenge: &Map<String, Value>,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    let Some(items) = value.and_then(Value::as_array) else {
        errors.push("failure-observations-must-be-list".to_string());
        return;
    };
    let mut observations = HashMap::new();
    for item in items {
        if let Some(fields) = item.as_object() {
            observations.insert(string(fields, "claim").to_string(), fields.clone());
        }
    }
    if observations.len() != FAILURE_CASES.len()
        || items.len() != FAILURE_CASES.len()
        || !FAILURE_CASES
            .iter()
            .all(|(claim, _)| observations.contains_key(*claim))
    {
        errors.push("failure-observation-inventory-mismatch".to_string());
    }
    for (claim, expected_code) in FAILURE_CASES {
        let observation = match observations.get(claim) {
            Some(observation) => observation.clone(),
            None => {
                errors.push(format!("failure:{claim}-must-be-object"));
                Map::new()
            }
        };
        if observation.is_empty() {
            continue;
        }
        if !exact_fields(&observation, &FAILURE_FIELDS) {
            errors.push(format!("failure-fields-invalid:{claim}"));
        }
        if string(&observation, "code") != expected_code {
            errors.push(format!("failure-code-mismatch:{claim}"));
        }
        let before = typed_digest(
            observation.get("preStateDigest"),
            &format!("{claim}.preStateDigest"),
            errors,
        );
        let after = typed_digest(
            observation.get("postStateDigest"),
            &format!("{claim}.postStateDigest"),
            errors,
        );
        if !before.is_empty() && !after.is_empty() && before != after {
            errors.push(format!("failure-mutated-state:{claim}"));
        }
        let Some(document) = observation
            .get("document")
            .and_then(Value::as_object)
            .cloned()
        else {
            errors.push(format!("failure-document-digest-mismatch:{claim}"));
            continue;
        };
        if observation.get("documentDigest").and_then(Value::as_str)
            != digest(&Value::Object(document.clone())).ok().as_deref()
        {
            errors.push(format!("failure-document-digest-mismatch:{claim}"));
            continue;
        }
        match claim {
            "trustOnFirstUseIsRejected" => {
                let candidate = object_copy(document.get("fleetIdentity"), "tofu-identity", errors);
                let attestation =
                    object_copy(document.get("attestation"), "tofu-attestation", errors);
                let candidate_identity =
                    validate_identity(Some(&Value::Object(candidate)), "tofu", errors);
                let known = [
                    source_identity.get("fleetId"),
                    destination_identity.get("fleetId"),
                ];
                let evidence_invalid = candidate_identity.is_empty()
                    || known.contains(&candidate_identity.get("fleetId"))
                    || attestation.is_empty();
                let readiness_invalid = !evidence_invalid
                    && historical_readiness(
                        Some(&Value::Object(attestation)),
                        &candidate_identity,
                        "tofu-readiness",
                        errors,
                    )
                    .0
                    .is_empty();
                if evidence_invalid || readiness_invalid {
                    errors.push("tofu-evidence-invalid".to_string());
                }
            }
            "readinessSequenceReplayIsRejected" => {
                let replay = historical_readiness(
                    Some(&Value::Object(document)),
                    destination_identity,
                    "replayed-readiness",
                    errors,
                )
                .0;
                let high_sequence = integer(readiness_high_water.get("highSequence"));
                let replay_sequence = integer(replay.get("sequence"));
                if replay.is_empty()
                    || high_sequence
                        .zip(replay_sequence)
                        .is_none_or(|(high, replay)| replay >= high)
                {
                    errors.push("readiness-replay-evidence-invalid".to_string());
                }
            }
            "expiredReadinessAttestationIsRejected" => {
                let (expired, _, expires_at) = historical_readiness(
                    Some(&Value::Object(document)),
                    destination_identity,
                    "expired-readiness",
                    errors,
                );
                if expired.is_empty()
                    || expires_at.is_none_or(|expires_at| validated_at < expires_at)
                {
                    errors.push("expired-readiness-evidence-invalid".to_string());
                }
            }
            "futureReadinessAttestationBeyondSkewIsRejected" => {
                let (future, signed_at, _) = historical_readiness(
                    Some(&Value::Object(document)),
                    destination_identity,
                    "future-readiness",
                    errors,
                );
                if future.is_empty()
                    || signed_at
                        .is_none_or(|signed_at| signed_at.saturating_sub(validated_at) <= 30)
                {
                    errors.push("future-readiness-evidence-invalid".to_string());
                }
            }
            "challengeNonceReplayIsRejected" => {
                if document != *challenge
                    || digest(&Value::Object(document)).ok()
                        != digest(&Value::Object(challenge.clone())).ok()
                {
                    errors.push("challenge-replay-evidence-invalid".to_string());
                }
            }
            _ => {
                let revoked = historical_readiness(
                    Some(&Value::Object(document)),
                    destination_identity,
                    "revoked-readiness",
                    errors,
                )
                .0;
                if revoked.is_empty()
                    || revoked.get("signerKeyId") != revoked_signer.get("signerKeyId")
                    || revoked.get("signerCertificate") != revoked_signer.get("certificate")
                    || revoked_signer.get("revokedAt").is_none_or(Value::is_null)
                {
                    errors.push("revoked-signer-evidence-invalid".to_string());
                }
            }
        }
    }
}

fn validate_snapshot(
    value: Option<&Value>,
    expected_fleet_id: &str,
    now: i64,
) -> Option<Map<String, Value>> {
    let snapshot = value.and_then(Value::as_object)?.clone();
    if string(&snapshot, "snapshotSchema") != SNAPSHOT_SCHEMA
        || !SNAPSHOT_FIELDS
            .iter()
            .all(|field| snapshot.contains_key(*field))
        || string(&snapshot, "fleetId") != expected_fleet_id
        || contains_snapshot_forbidden(&Value::Object(snapshot.clone()))
    {
        return None;
    }
    let mut payload = snapshot.clone();
    let declared = payload.remove("snapshotDigest")?.as_str()?.to_string();
    let computed = digest(&Value::Object(payload)).ok()?;
    if computed.strip_prefix("sha256:") != Some(declared.as_str()) {
        return None;
    }
    let generated_at = parse_timestamp(string(&snapshot, "generatedAt"))?;
    let age = now - generated_at;
    if !(-30..=300).contains(&age) {
        return None;
    }
    Some(snapshot)
}

fn contains_snapshot_forbidden(value: &Value) -> bool {
    match value {
        Value::Object(fields) => fields.iter().any(|(key, value)| {
            let normalized = key.to_ascii_lowercase().replace(['_', '-'], "");
            [
                "credential",
                "secret",
                "password",
                "privatekey",
                "ageidentity",
                "identity",
                "token",
                "authorityprivate",
            ]
            .iter()
            .any(|fragment| normalized.contains(fragment))
                || contains_snapshot_forbidden(value)
        }),
        Value::Array(items) => items.iter().any(contains_snapshot_forbidden),
        _ => false,
    }
}

fn labeled_timestamp(value: Option<&Value>, label: &str, errors: &mut Vec<String>) -> Option<i64> {
    let parsed = value.and_then(Value::as_str).and_then(parse_timestamp);
    if parsed.is_none() {
        errors.push(format!("invalid-timestamp:{label}"));
    }
    parsed
}

fn integer(value: Option<&Value>) -> Option<i128> {
    value.and_then(|value| {
        value
            .as_i64()
            .map(i128::from)
            .or_else(|| value.as_u64().map(i128::from))
    })
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Number(value)) => value.as_f64().is_some_and(|value| value != 0.0),
        Some(Value::Array(values)) => !values.is_empty(),
        Some(Value::Object(values)) => !values.is_empty(),
        Some(Value::Bool(true)) => true,
    }
}

fn is_nonce(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('=')
        && URL_SAFE_NO_PAD
            .decode(value)
            .is_ok_and(|decoded| decoded.len() == 32 && URL_SAFE_NO_PAD.encode(decoded) == value)
}
