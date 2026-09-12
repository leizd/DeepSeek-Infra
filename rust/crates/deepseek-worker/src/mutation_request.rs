use crate::authority_request::{AuthorityRequestError, canonical_json_bytes};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};

pub const MUTATION_REQUEST_SCHEMA: &str = "control-mutation-request-v1";
pub const MAX_MUTATION_REQUEST_BYTES: usize = 16 * 1024;
const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:control-mutation-request-v1\x00";
const MAX_LIFETIME_SECONDS: i64 = 300;
const MUTATION_REQUEST_FIELDS: &[&str] = &[
    "actionId",
    "digest",
    "domain",
    "environment",
    "executionEpoch",
    "expiresAt",
    "fencingToken",
    "fleetId",
    "issuedAt",
    "mode",
    "nonce",
    "operation",
    "operationId",
    "payload",
    "payloadDigest",
    "requestId",
    "revision",
    "role",
    "runtime",
    "schema",
    "schemaVersion",
    "signature",
    "signatureAlgorithm",
    "signerKeyId",
];
const PAYLOAD_FIELDS: &[&str] = &["intent", "recordId", "revision", "state"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MutationRequestError {
    pub code: &'static str,
}

impl MutationRequestError {
    fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl From<AuthorityRequestError> for MutationRequestError {
    fn from(error: AuthorityRequestError) -> Self {
        Self { code: error.code }
    }
}

#[derive(Debug, Clone)]
pub struct MutationRequestContext<'a> {
    pub now: &'a str,
    pub signer_public_key: &'a str,
    pub signer_key_id: &'a str,
    pub expected_domain: &'a str,
    pub expected_operation: &'a str,
    pub expected_runtime: &'a str,
    pub expected_mode: &'a str,
    pub expected_fleet_id: &'a str,
    pub expected_environment: &'a str,
    pub expected_role: &'a str,
    pub current_fencing_token: i64,
    pub live_epoch: i64,
    pub seen_request_ids: HashSet<String>,
    pub seen_nonces: HashSet<String>,
    pub seen_operation_digests: HashMap<String, String>,
    pub max_future_skew_seconds: i64,
}

pub fn verify_mutation_request_document(
    raw: &[u8],
    context: &MutationRequestContext<'_>,
) -> Result<Value, MutationRequestError> {
    if raw.is_empty() {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if raw.len() > MAX_MUTATION_REQUEST_BYTES {
        return Err(MutationRequestError::new("MUTATION_REQUEST_TOO_LARGE"));
    }
    let document: Value = serde_json::from_slice(raw)
        .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?;
    let canonical = canonical_json_bytes(&document)
        .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?;
    if canonical != raw {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_CANONICAL_MISMATCH",
        ));
    }
    let object = document
        .as_object()
        .ok_or_else(|| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?;
    let keys: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if keys.len() != MUTATION_REQUEST_FIELDS.len()
        || MUTATION_REQUEST_FIELDS
            .iter()
            .any(|field| !keys.contains(field))
    {
        return Err(MutationRequestError::new("MUTATION_REQUEST_FIELDS_INVALID"));
    }
    reject_secrets(&document)?;
    verify_envelope(object, context)?;
    verify_signature(object, context)?;
    Ok(document)
}

fn verify_envelope(
    document: &Map<String, Value>,
    context: &MutationRequestContext<'_>,
) -> Result<(), MutationRequestError> {
    if string_field(document, "schema") != Some(MUTATION_REQUEST_SCHEMA)
        || document.get("schemaVersion") != Some(&Value::from(1))
    {
        return Err(MutationRequestError::new("MUTATION_REQUEST_SCHEMA_INVALID"));
    }
    if string_field(document, "operation") != Some(context.expected_operation)
        || string_field(document, "operation") != Some("propose-mutation")
    {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_OPERATION_INVALID",
        ));
    }
    if string_field(document, "domain") != Some(context.expected_domain) {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_DOMAIN_MISMATCH",
        ));
    }
    if string_field(document, "runtime") != Some("go")
        || string_field(document, "runtime") != Some(context.expected_runtime)
    {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_RUNTIME_MISMATCH",
        ));
    }
    if string_field(document, "mode") != Some("shadow")
        || string_field(document, "mode") != Some(context.expected_mode)
    {
        return Err(MutationRequestError::new("MUTATION_REQUEST_MODE_MISMATCH"));
    }
    let fleet_id = string_field(document, "fleetId").unwrap_or("");
    if !valid_fleet_id(fleet_id) || fleet_id != context.expected_fleet_id {
        return Err(MutationRequestError::new("MUTATION_REQUEST_FLEET_MISMATCH"));
    }
    if string_field(document, "environment") != Some(context.expected_environment)
        || context.expected_environment.is_empty()
    {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_ENVIRONMENT_MISMATCH",
        ));
    }
    if string_field(document, "role") != Some("control-plane")
        || string_field(document, "role") != Some(context.expected_role)
    {
        return Err(MutationRequestError::new("MUTATION_REQUEST_ROLE_MISMATCH"));
    }
    let action_id = string_field(document, "actionId").unwrap_or("");
    if !valid_control_id(action_id) {
        return Err(MutationRequestError::new("EMPTY_ACTION_ID"));
    }
    let epoch = integer_field(document, "executionEpoch")?;
    if epoch < 1 {
        return Err(MutationRequestError::new("ZERO_EXECUTION_EPOCH"));
    }
    if integer_field(document, "revision")? < 1 {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    let fencing_token = integer_field(document, "fencingToken")?;
    if fencing_token < 1 || fencing_token != context.current_fencing_token {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_STALE_FENCING_TOKEN",
        ));
    }
    if epoch <= context.live_epoch {
        return Err(MutationRequestError::new("STALE_EXECUTION_EPOCH"));
    }
    let request_id = string_field(document, "requestId").unwrap_or("");
    let nonce = string_field(document, "nonce").unwrap_or("");
    let operation_id = string_field(document, "operationId").unwrap_or("");
    if !valid_hex64(request_id) || !valid_hex64(nonce) || !valid_hex64(operation_id) {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if context.seen_request_ids.contains(request_id) {
        return Err(MutationRequestError::new("MUTATION_REQUEST_REPLAY"));
    }
    if context.seen_nonces.contains(nonce) {
        return Err(MutationRequestError::new("MUTATION_REQUEST_NONCE_REUSE"));
    }
    let payload = document
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?;
    let payload_keys: BTreeSet<&str> = payload.keys().map(String::as_str).collect();
    if payload_keys.len() != PAYLOAD_FIELDS.len()
        || PAYLOAD_FIELDS
            .iter()
            .any(|field| !payload_keys.contains(field))
    {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if string_field(payload, "intent") != Some("shadow-compare") {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if !valid_control_id(string_field(payload, "recordId").unwrap_or("")) {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if integer_field(payload, "revision")? < 1 {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if string_field(payload, "state").unwrap_or("").is_empty() {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    let payload_digest = typed_digest(&Value::Object(payload.clone()))?;
    if string_field(document, "payloadDigest") != Some(payload_digest.as_str()) {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_PAYLOAD_DIGEST_MISMATCH",
        ));
    }
    if let Some(seen) = context.seen_operation_digests.get(operation_id) {
        if seen != payload_digest.as_str() {
            return Err(MutationRequestError::new(
                "MUTATION_REQUEST_REPLAY_CONFLICT",
            ));
        }
    }
    let digest = mutation_digest(document)?;
    if string_field(document, "digest") != Some(digest.as_str()) {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_DIGEST_MISMATCH",
        ));
    }
    let issued_at = parse_utc_z(string_field(document, "issuedAt").unwrap_or(""))?;
    let expires_at = parse_utc_z(string_field(document, "expiresAt").unwrap_or(""))?;
    let now = parse_utc_z(context.now)?;
    if expires_at <= issued_at || expires_at - issued_at > MAX_LIFETIME_SECONDS {
        return Err(MutationRequestError::new("MUTATION_REQUEST_INVALID"));
    }
    if expires_at <= now {
        return Err(MutationRequestError::new("MUTATION_REQUEST_EXPIRED"));
    }
    let skew = context.max_future_skew_seconds.max(0);
    if issued_at.saturating_sub(now) > skew {
        return Err(MutationRequestError::new("MUTATION_REQUEST_FUTURE_SKEW"));
    }
    Ok(())
}

fn verify_signature(
    document: &Map<String, Value>,
    context: &MutationRequestContext<'_>,
) -> Result<(), MutationRequestError> {
    if string_field(document, "signatureAlgorithm") != Some("Ed25519") {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_SIGNATURE_INVALID",
        ));
    }
    let signer_key_id = string_field(document, "signerKeyId").unwrap_or("");
    if signer_key_id != context.signer_key_id || !valid_signer_key_id(signer_key_id) {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_SIGNER_MISMATCH",
        ));
    }
    let signature = decode_fixed(string_field(document, "signature").unwrap_or(""), 64)?;
    let public_key = decode_fixed(context.signer_public_key, 32)?;
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    let mut message = SIGNATURE_DOMAIN.to_vec();
    message.extend(
        canonical_json_bytes(&Value::Object(unsigned))
            .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?,
    );
    let verifying_key = VerifyingKey::from_bytes(
        public_key
            .as_slice()
            .try_into()
            .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_SIGNATURE_INVALID"))?,
    )
    .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_SIGNATURE_INVALID"))?;
    let signature = Signature::from_bytes(
        signature
            .as_slice()
            .try_into()
            .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_SIGNATURE_INVALID"))?,
    );
    verifying_key
        .verify_strict(&message, &signature)
        .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_SIGNATURE_INVALID"))?;
    Ok(())
}

fn mutation_digest(document: &Map<String, Value>) -> Result<String, MutationRequestError> {
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    unsigned.remove("digest");
    typed_digest(&Value::Object(unsigned))
}

fn typed_digest(value: &Value) -> Result<String, MutationRequestError> {
    Ok(format!(
        "sha256:{}",
        sha256_hex(
            &canonical_json_bytes(value)
                .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_INVALID"))?
        )
    ))
}

fn reject_secrets(value: &Value) -> Result<(), MutationRequestError> {
    match value {
        Value::Object(map) => {
            for (key, nested) in map {
                let normalized: String = key
                    .chars()
                    .filter(|character| character.is_ascii_alphanumeric())
                    .map(|character| character.to_ascii_lowercase())
                    .collect();
                if !matches!(
                    normalized.as_str(),
                    "fencingtoken" | "signature" | "signaturealgorithm" | "signerkeyid"
                ) && [
                    "password",
                    "passwd",
                    "privatekey",
                    "ageidentity",
                    "apikey",
                    "accesskey",
                    "secretkey",
                    "token",
                    "credential",
                    "oauth",
                    "bearer",
                    "secret",
                ]
                .iter()
                .any(|fragment| normalized.contains(fragment))
                {
                    return Err(MutationRequestError::new(
                        "MUTATION_REQUEST_SECRET_DETECTED",
                    ));
                }
                reject_secrets(nested)?;
            }
        }
        Value::Array(items) => {
            for nested in items {
                reject_secrets(nested)?;
            }
        }
        Value::String(text) => {
            let lower = text.to_ascii_lowercase();
            if lower.contains("age-secret-key-")
                || lower.contains("-----begin") && lower.contains("private key")
            {
                return Err(MutationRequestError::new(
                    "MUTATION_REQUEST_SECRET_DETECTED",
                ));
            }
        }
        _ => {}
    }
    Ok(())
}

fn string_field<'a>(document: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    document.get(key).and_then(Value::as_str)
}

fn integer_field(document: &Map<String, Value>, key: &str) -> Result<i64, MutationRequestError> {
    document
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| MutationRequestError::new("MUTATION_REQUEST_INVALID"))
}

fn decode_fixed(value: &str, size: usize) -> Result<Vec<u8>, MutationRequestError> {
    let raw = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_SIGNATURE_INVALID"))?;
    if raw.len() != size {
        return Err(MutationRequestError::new(
            "MUTATION_REQUEST_SIGNATURE_INVALID",
        ));
    }
    Ok(raw)
}

fn sha256_hex(value: &[u8]) -> String {
    let mut out = String::with_capacity(64);
    for byte in Sha256::digest(value) {
        use std::fmt::Write;
        let _ = write!(out, "{byte:02x}");
    }
    out
}

fn parse_utc_z(value: &str) -> Result<i64, MutationRequestError> {
    crate::authority_request::parse_utc_z_str(value)
        .map_err(|_| MutationRequestError::new("MUTATION_REQUEST_INVALID"))
}

fn valid_fleet_id(value: &str) -> bool {
    crate::authority_request::valid_fleet_id_str(value)
}

fn valid_hex64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_control_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn valid_signer_key_id(value: &str) -> bool {
    value.starts_with("ctrl-signer-")
        && value.len() == 28
        && value.as_bytes()[12..]
            .iter()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}
