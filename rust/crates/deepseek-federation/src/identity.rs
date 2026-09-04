use crate::attestation::{
    AttestationError, CurrentSignerAuthorization, REPLICA_ATTESTATION_SCHEMA, ReplicaAttestation,
};
use crate::canonical::{
    assert_secret_free, canonical_bytes, decode_fixed, object, parse_timestamp, positive_u64_field,
    sha256_hex, string_field, typed_sha256, validate_fleet_id,
};
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};

const FLEET_IDENTITY_SCHEMA: &str = "fleet-identity-v1";
const ONLINE_SIGNER_CERTIFICATE_SCHEMA: &str = "federation-online-signer-certificate-v1";
const SIGNATURE_ALGORITHM: &str = "Ed25519";
pub const PURPOSE_DR_ATTESTATION: &str = "DR_ATTESTATION";
pub const PURPOSE_INGRESS_GRANT: &str = "INGRESS_GRANT";
pub const PURPOSE_REPLICA_ATTESTATION: &str = "REPLICA_ATTESTATION";
const CERTIFICATE_DOMAIN: &[u8] = b"deepseek-infra:federation-online-signer-certificate-v1\0";
const DOCUMENT_DOMAIN_PREFIX: &[u8] = b"deepseek-infra:federation-document\0";
const ONLINE_SIGNER_PURPOSES: &[&str] = &[
    "DR_ATTESTATION",
    "EVIDENCE",
    "INGRESS_GRANT",
    "READINESS_ATTESTATION",
    "REPLICA_ATTESTATION",
    "SESSION_AUTHENTICATION",
];

pub(crate) struct VerifiedSigner {
    pub signer_key_id: String,
    pub not_before: i64,
    pub expires_at: i64,
}

struct VerifiedRoot {
    fleet_id: String,
    root_key_id: String,
    root_fingerprint: String,
    verifying_key: VerifyingKey,
}

pub(crate) fn verify_attestation_signature(
    attestation: &ReplicaAttestation,
    root_identity: &Value,
    authorization: &CurrentSignerAuthorization,
    now: i64,
) -> Result<VerifiedSigner, AttestationError> {
    assert_secret_free(root_identity)?;
    assert_secret_free(&attestation.signer_certificate)?;
    let root = verify_root_identity(root_identity)?;
    let (signer, verifying_key, certificate_digest) = verify_certificate(
        &attestation.signer_certificate,
        &root,
        now,
        PURPOSE_REPLICA_ATTESTATION,
    )?;
    verify_authorization(authorization, &signer, &certificate_digest)?;

    if attestation.schema != REPLICA_ATTESTATION_SCHEMA {
        return Err(error("FEDERATION_DOCUMENT_SCHEMA_INVALID"));
    }
    if attestation.signer_key_id != signer.signer_key_id {
        return Err(error("FEDERATION_DOCUMENT_SIGNER_MISMATCH"));
    }
    if attestation.signature_algorithm != SIGNATURE_ALGORITHM {
        return Err(error("FEDERATION_DOCUMENT_ALGORITHM_INVALID"));
    }
    if attestation.fleet_id != root.fleet_id {
        return Err(error("FEDERATION_DOCUMENT_FLEET_MISMATCH"));
    }

    let mut payload = serde_json::to_value(attestation)
        .map_err(|_| error("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID"))?;
    let payload_map = payload
        .as_object_mut()
        .ok_or_else(|| error("FEDERATION_REPLICA_ATTESTATION_INVALID"))?;
    payload_map.remove("signature");

    let mut certificate_context = Map::new();
    certificate_context.insert("fleetId".to_string(), Value::String(root.fleet_id));
    certificate_context.insert("rootKeyId".to_string(), Value::String(root.root_key_id));
    certificate_context.insert(
        "rootFingerprint".to_string(),
        Value::String(root.root_fingerprint),
    );
    certificate_context.insert(
        "signerKeyId".to_string(),
        Value::String(signer.signer_key_id.clone()),
    );
    certificate_context.insert(
        "certificateDigest".to_string(),
        Value::String(certificate_digest),
    );
    let mut document = Map::new();
    document.insert(
        "schema".to_string(),
        Value::String(attestation.schema.clone()),
    );
    document.insert(
        "certificateContext".to_string(),
        Value::Object(certificate_context),
    );
    document.insert("document".to_string(), payload);
    let mut message = DOCUMENT_DOMAIN_PREFIX.to_vec();
    message.extend(canonical_bytes(&Value::Object(document))?);

    let signature_bytes = decode_fixed::<64>(&attestation.signature)
        .ok_or_else(|| error("FEDERATION_DOCUMENT_SIGNATURE_INVALID"))?;
    let signature = Signature::from_bytes(&signature_bytes);
    verifying_key
        .verify_strict(&message, &signature)
        .map_err(|_| error("FEDERATION_DOCUMENT_SIGNATURE_INVALID"))?;
    Ok(signer)
}

pub fn validate_fleet_identity(identity: &Value) -> Result<(), AttestationError> {
    if !identity.is_object() {
        return Err(error("FEDERATION_ROOT_IDENTITY_INVALID"));
    }
    verify_root_identity(identity).map(|_| ())
}

pub fn verify_federation_document(
    document: &Value,
    certificate: &Map<String, Value>,
    root_identity: &Value,
    expected_schema: &str,
    now: &str,
    required_purpose: &str,
) -> Result<Value, AttestationError> {
    let root = verify_root_identity(root_identity)?;
    let now = parse_timestamp(now)
        .ok_or_else(|| error("FEDERATION_CERTIFICATE_VALIDATION_TIME_INVALID"))?;
    let (signer, verifying_key, certificate_digest) =
        verify_certificate(certificate, &root, now, required_purpose)?;
    assert_secret_free(document)?;
    let fields = object(document).ok_or_else(|| error("FEDERATION_DOCUMENT_INVALID"))?;
    let schema = string_field(fields, "schema")
        .filter(|schema| *schema == expected_schema)
        .ok_or_else(|| error("FEDERATION_DOCUMENT_SCHEMA_INVALID"))?;
    if string_field(fields, "signerKeyId") != Some(signer.signer_key_id.as_str()) {
        return Err(error("FEDERATION_DOCUMENT_SIGNER_MISMATCH"));
    }
    if string_field(fields, "signatureAlgorithm") != Some(SIGNATURE_ALGORITHM) {
        return Err(error("FEDERATION_DOCUMENT_ALGORITHM_INVALID"));
    }
    let fleet_id = string_field(fields, "fleetId")
        .filter(|fleet_id| !fleet_id.is_empty())
        .ok_or_else(|| error("FEDERATION_DOCUMENT_FLEET_ID_REQUIRED"))?;
    if fleet_id != root.fleet_id {
        return Err(error("FEDERATION_DOCUMENT_FLEET_MISMATCH"));
    }

    let mut payload = fields.clone();
    let signature = payload
        .remove("signature")
        .and_then(|value| value.as_str().map(ToOwned::to_owned))
        .and_then(|value| decode_fixed::<64>(&value))
        .ok_or_else(|| error("FEDERATION_DOCUMENT_SIGNATURE_INVALID"))?;
    let mut certificate_context = Map::new();
    certificate_context.insert("fleetId".to_string(), Value::String(root.fleet_id));
    certificate_context.insert("rootKeyId".to_string(), Value::String(root.root_key_id));
    certificate_context.insert(
        "rootFingerprint".to_string(),
        Value::String(root.root_fingerprint),
    );
    certificate_context.insert(
        "signerKeyId".to_string(),
        Value::String(signer.signer_key_id),
    );
    certificate_context.insert(
        "certificateDigest".to_string(),
        Value::String(certificate_digest),
    );
    let message_document = serde_json::json!({
        "schema": schema,
        "certificateContext": certificate_context,
        "document": payload,
    });
    let mut message = DOCUMENT_DOMAIN_PREFIX.to_vec();
    message.extend(canonical_bytes(&message_document)?);
    verifying_key
        .verify_strict(&message, &Signature::from_bytes(&signature))
        .map_err(|_| error("FEDERATION_DOCUMENT_SIGNATURE_INVALID"))?;
    Ok(document.clone())
}

fn verify_root_identity(identity: &Value) -> Result<VerifiedRoot, AttestationError> {
    let identity = object(identity).ok_or_else(|| error("FEDERATION_PEER_IDENTITY_INVALID"))?;
    if string_field(identity, "schema") != Some(FLEET_IDENTITY_SCHEMA) {
        return Err(error("FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID"));
    }
    let fleet_id = string_field(identity, "fleetId")
        .filter(|value| validate_fleet_id(value))
        .ok_or_else(|| error("FEDERATION_FLEET_ID_INVALID"))?;
    if string_field(identity, "signatureAlgorithm") != Some(SIGNATURE_ALGORITHM) {
        return Err(error("FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID"));
    }
    let root_public = string_field(identity, "rootPublicKey")
        .and_then(decode_fixed::<32>)
        .ok_or_else(|| error("FEDERATION_ROOT_PUBLIC_KEY_INVALID"))?;
    let expected_key_id = format!("fed-root-{}", &sha256_hex(&root_public)[..24]);
    let root_key_id = string_field(identity, "rootKeyId")
        .filter(|value| *value == expected_key_id)
        .ok_or_else(|| error("FEDERATION_ROOT_KEY_ID_INVALID"))?;
    let expected_fingerprint = typed_sha256(&root_public);
    let root_fingerprint = string_field(identity, "rootFingerprint")
        .filter(|value| *value == expected_fingerprint)
        .ok_or_else(|| error("FEDERATION_ROOT_FINGERPRINT_INVALID"))?;
    let created_at = string_field(identity, "createdAt")
        .and_then(parse_timestamp)
        .ok_or_else(|| error("FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID"))?;
    let _ = created_at;
    let verifying_key = VerifyingKey::from_bytes(&root_public)
        .map_err(|_| error("FEDERATION_ROOT_PUBLIC_KEY_INVALID"))?;
    Ok(VerifiedRoot {
        fleet_id: fleet_id.to_string(),
        root_key_id: root_key_id.to_string(),
        root_fingerprint: root_fingerprint.to_string(),
        verifying_key,
    })
}

fn verify_certificate(
    certificate: &Map<String, Value>,
    root: &VerifiedRoot,
    now: i64,
    required_purpose: &str,
) -> Result<(VerifiedSigner, VerifyingKey, String), AttestationError> {
    if string_field(certificate, "schema") != Some(ONLINE_SIGNER_CERTIFICATE_SCHEMA) {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_SCHEMA_INVALID"));
    }
    if string_field(certificate, "fleetId") != Some(root.fleet_id.as_str()) {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_FLEET_MISMATCH"));
    }
    if string_field(certificate, "rootKeyId") != Some(root.root_key_id.as_str())
        || string_field(certificate, "rootFingerprint") != Some(root.root_fingerprint.as_str())
    {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH"));
    }
    if string_field(certificate, "signatureAlgorithm") != Some(SIGNATURE_ALGORITHM) {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_ALGORITHM_INVALID"));
    }
    verify_purposes(certificate, required_purpose)?;
    let signer_public = string_field(certificate, "signerPublicKey")
        .and_then(decode_fixed::<32>)
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID"))?;
    let signer_key_id = string_field(certificate, "signerKeyId")
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID"))?;
    let expected_signer_key_id = format!("fed-signer-{}", &sha256_hex(&signer_public)[..24]);
    if signer_key_id != expected_signer_key_id {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID"));
    }
    if positive_u64_field(certificate, "sequence").is_none() {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID"));
    }
    let issued_at = certificate_timestamp(certificate, "issuedAt")?;
    let not_before = certificate_timestamp(certificate, "notBefore")?;
    let expires_at = certificate_timestamp(certificate, "expiresAt")?;
    if issued_at > not_before || not_before >= expires_at {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID"));
    }
    if issued_at > now {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE"));
    }
    if not_before > now {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_NOT_YET_VALID"));
    }
    if now >= expires_at {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_EXPIRED"));
    }

    let root_signature = string_field(certificate, "rootSignature")
        .and_then(decode_fixed::<64>)
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"))?;
    let mut certificate_payload = certificate.clone();
    certificate_payload.remove("rootSignature");
    let mut message = CERTIFICATE_DOMAIN.to_vec();
    message.extend(canonical_bytes(&Value::Object(certificate_payload))?);
    root.verifying_key
        .verify_strict(&message, &Signature::from_bytes(&root_signature))
        .map_err(|_| error("FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"))?;

    let certificate_digest = typed_sha256(&canonical_bytes(&Value::Object(certificate.clone()))?);
    let verifying_key = VerifyingKey::from_bytes(&signer_public)
        .map_err(|_| error("FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID"))?;
    Ok((
        VerifiedSigner {
            signer_key_id: signer_key_id.to_string(),
            not_before,
            expires_at,
        },
        verifying_key,
        certificate_digest,
    ))
}

fn verify_authorization(
    authorization: &CurrentSignerAuthorization,
    signer: &VerifiedSigner,
    certificate_digest: &str,
) -> Result<(), AttestationError> {
    if authorization.signer_key_id != signer.signer_key_id {
        return Err(error("FEDERATION_SIGNER_NOT_ACCEPTED"));
    }
    if authorization.certificate_digest != certificate_digest {
        return Err(error("FEDERATION_SIGNER_CERTIFICATE_CONFLICT"));
    }
    if !authorization.active {
        return Err(error("FEDERATION_SIGNER_REVOKED"));
    }
    Ok(())
}

fn verify_purposes(
    certificate: &Map<String, Value>,
    required_purpose: &str,
) -> Result<(), AttestationError> {
    let purposes = certificate
        .get("purposes")
        .and_then(Value::as_array)
        .filter(|purposes| !purposes.is_empty())
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID"))?;
    let mut previous: Option<&str> = None;
    if !ONLINE_SIGNER_PURPOSES.contains(&required_purpose) {
        return Err(error("FEDERATION_SIGNER_PURPOSE_INVALID"));
    }
    let mut permits_required_purpose = false;
    for purpose in purposes {
        let purpose = purpose
            .as_str()
            .filter(|purpose| ONLINE_SIGNER_PURPOSES.contains(purpose))
            .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID"))?;
        if previous.is_some_and(|item| item >= purpose) {
            return Err(error("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID"));
        }
        permits_required_purpose |= purpose == required_purpose;
        previous = Some(purpose);
    }
    if !permits_required_purpose {
        return Err(error("FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"));
    }
    Ok(())
}

fn certificate_timestamp(
    certificate: &Map<String, Value>,
    field: &str,
) -> Result<i64, AttestationError> {
    string_field(certificate, field)
        .and_then(parse_timestamp)
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID"))
}

fn error(code: &'static str) -> AttestationError {
    AttestationError::new(code)
}
