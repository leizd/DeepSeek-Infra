use crate::authority_request::{AuthorityRequestError, canonical_json_bytes};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};

pub const STORAGE_OPERATION_GRANT_SCHEMA: &str = "control-storage-operation-grant-v1";
pub const MAX_STORAGE_OPERATION_GRANT_BYTES: usize = 16 * 1024;
const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:control-storage-operation-grant-v1\x00";
const MAX_LIFETIME_SECONDS: i64 = 300;
const GRANT_FIELDS: &[&str] = &[
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
const PAYLOAD_FIELDS: &[&str] = &[
    "bucket",
    "claimRevision",
    "conditionType",
    "expectedEtag",
    "expectedLength",
    "mutationType",
    "objectDigest",
    "objectKey",
    "prefix",
    "provider",
    "targetIdentity",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageOperationGrantError {
    pub code: &'static str,
}

impl StorageOperationGrantError {
    pub(crate) fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl From<AuthorityRequestError> for StorageOperationGrantError {
    fn from(error: AuthorityRequestError) -> Self {
        Self { code: error.code }
    }
}

#[derive(Debug, Clone)]
pub struct StorageOperationGrantContext<'a> {
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageOperationCommand<'a> {
    pub action_id: &'a str,
    pub execution_epoch: u64,
    pub operation_id: &'a str,
    pub mutation_type: &'a str,
    pub provider: &'a str,
    pub target_identity: &'a str,
    pub bucket: &'a str,
    pub prefix: &'a str,
    pub object_key: &'a str,
    pub object_digest: &'a str,
    pub expected_length: u64,
    pub condition_type: &'a str,
    pub expected_etag: &'a str,
    pub claim_revision: i64,
}

pub fn verify_storage_operation_grant(
    raw: &[u8],
    context: &StorageOperationGrantContext<'_>,
) -> Result<Value, StorageOperationGrantError> {
    if raw.is_empty() {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    if raw.len() > MAX_STORAGE_OPERATION_GRANT_BYTES {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_TOO_LARGE",
        ));
    }
    let document: Value = serde_json::from_slice(raw)
        .map_err(|_| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    let canonical = canonical_json_bytes(&document)
        .map_err(|_| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    if canonical != raw {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_CANONICAL_MISMATCH",
        ));
    }
    let object = document
        .as_object()
        .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    let keys: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if keys.len() != GRANT_FIELDS.len() || GRANT_FIELDS.iter().any(|field| !keys.contains(field)) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_FIELDS_INVALID",
        ));
    }
    reject_secrets(&document)?;
    verify_envelope(object, context)?;
    verify_signature(object, context)?;
    Ok(document)
}

pub fn bind_storage_operation_grant(
    document: &Value,
    command: &StorageOperationCommand<'_>,
) -> Result<(), StorageOperationGrantError> {
    let object = document
        .as_object()
        .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    let payload = object
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    let epoch = integer_field(object, "executionEpoch")?;
    if epoch < 1
        || epoch as u64 != command.execution_epoch
        || string_field(object, "actionId") != Some(command.action_id)
        || string_field(object, "operationId") != Some(command.operation_id)
        || string_field(payload, "mutationType") != Some(command.mutation_type)
        || string_field(payload, "provider") != Some(command.provider)
        || string_field(payload, "targetIdentity") != Some(command.target_identity)
        || string_field(payload, "bucket") != Some(command.bucket)
        || string_field(payload, "prefix") != Some(command.prefix)
        || string_field(payload, "objectKey") != Some(command.object_key)
        || string_field(payload, "objectDigest") != Some(command.object_digest)
        || string_field(payload, "conditionType") != Some(command.condition_type)
        || string_field(payload, "expectedEtag") != Some(command.expected_etag)
        || integer_field(payload, "expectedLength")? as u64 != command.expected_length
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_COMMAND_MISMATCH",
        ));
    }
    if command.claim_revision != 0
        && integer_field(payload, "claimRevision")? != command.claim_revision
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_COMMAND_MISMATCH",
        ));
    }
    Ok(())
}

fn verify_envelope(
    document: &Map<String, Value>,
    context: &StorageOperationGrantContext<'_>,
) -> Result<(), StorageOperationGrantError> {
    if string_field(document, "schema") != Some(STORAGE_OPERATION_GRANT_SCHEMA)
        || document.get("schemaVersion") != Some(&Value::from(1))
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_SCHEMA_INVALID",
        ));
    }
    if string_field(document, "operation") != Some(context.expected_operation)
        || string_field(document, "operation") != Some("execute-storage-put")
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_OPERATION_INVALID",
        ));
    }
    if string_field(document, "domain") != Some("action")
        || string_field(document, "domain") != Some(context.expected_domain)
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_DOMAIN_MISMATCH",
        ));
    }
    if string_field(document, "runtime") != Some("go")
        || string_field(document, "runtime") != Some(context.expected_runtime)
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_RUNTIME_MISMATCH",
        ));
    }
    if string_field(document, "mode") != Some("shadow")
        || string_field(document, "mode") != Some(context.expected_mode)
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_MODE_MISMATCH",
        ));
    }
    let fleet_id = string_field(document, "fleetId").unwrap_or("");
    if !valid_fleet_id(fleet_id) || fleet_id != context.expected_fleet_id {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_FLEET_MISMATCH",
        ));
    }
    if string_field(document, "environment") != Some(context.expected_environment)
        || context.expected_environment.is_empty()
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_ENVIRONMENT_MISMATCH",
        ));
    }
    if string_field(document, "role") != Some("control-plane")
        || string_field(document, "role") != Some(context.expected_role)
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_ROLE_MISMATCH",
        ));
    }
    let action_id = string_field(document, "actionId").unwrap_or("");
    if !valid_control_id(action_id) {
        return Err(StorageOperationGrantError::new("EMPTY_ACTION_ID"));
    }
    let epoch = integer_field(document, "executionEpoch")?;
    if epoch < 1 {
        return Err(StorageOperationGrantError::new("ZERO_EXECUTION_EPOCH"));
    }
    if context.live_epoch < 1 {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_AUTHORITY_MISSING",
        ));
    }
    if epoch != context.live_epoch {
        return Err(StorageOperationGrantError::new("FENCE_MISMATCH"));
    }
    if integer_field(document, "revision")? < 1 {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    let fencing_token = integer_field(document, "fencingToken")?;
    if fencing_token < 1 || fencing_token != context.current_fencing_token {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_STALE_FENCING_TOKEN",
        ));
    }
    let request_id = string_field(document, "requestId").unwrap_or("");
    let nonce = string_field(document, "nonce").unwrap_or("");
    let operation_id = string_field(document, "operationId").unwrap_or("");
    if !valid_hex64(request_id) || !valid_hex64(nonce) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    if !crate::valid_storage_operation_id(operation_id) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    if context.seen_request_ids.contains(request_id) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_REPLAY",
        ));
    }
    if context.seen_nonces.contains(nonce) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_NONCE_REUSE",
        ));
    }
    let payload = document
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
    let payload_keys: BTreeSet<&str> = payload.keys().map(String::as_str).collect();
    if payload_keys.len() != PAYLOAD_FIELDS.len()
        || PAYLOAD_FIELDS
            .iter()
            .any(|field| !payload_keys.contains(field))
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    verify_payload(payload)?;
    let payload_digest = typed_digest(&Value::Object(payload.clone()))?;
    if string_field(document, "payloadDigest") != Some(payload_digest.as_str()) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_PAYLOAD_DIGEST_MISMATCH",
        ));
    }
    if let Some(seen) = context.seen_operation_digests.get(operation_id) {
        if seen != payload_digest.as_str() {
            return Err(StorageOperationGrantError::new(
                "STORAGE_OPERATION_GRANT_REPLAY_CONFLICT",
            ));
        }
    }
    let digest = grant_digest(document)?;
    if string_field(document, "digest") != Some(digest.as_str()) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_DIGEST_MISMATCH",
        ));
    }
    let issued_at = parse_utc_z(string_field(document, "issuedAt").unwrap_or(""))?;
    let expires_at = parse_utc_z(string_field(document, "expiresAt").unwrap_or(""))?;
    let now = parse_utc_z(context.now)?;
    if expires_at <= issued_at || expires_at - issued_at > MAX_LIFETIME_SECONDS {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    if expires_at <= now {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_EXPIRED",
        ));
    }
    let skew = context.max_future_skew_seconds.max(0);
    if issued_at.saturating_sub(now) > skew {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_FUTURE_SKEW",
        ));
    }
    Ok(())
}

fn verify_payload(payload: &Map<String, Value>) -> Result<(), StorageOperationGrantError> {
    if string_field(payload, "mutationType") != Some("PUT_CHUNK")
        || string_field(payload, "provider") != Some("s3")
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    let target = string_field(payload, "targetIdentity").unwrap_or("");
    let digest = string_field(payload, "objectDigest").unwrap_or("");
    if !valid_hex64(target) || !valid_hex64(digest) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    for key in ["bucket", "prefix", "objectKey", "expectedEtag"] {
        let value = string_field(payload, key)
            .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
        if value.len() > 1024 || value.contains('\0') {
            return Err(StorageOperationGrantError::new(
                "STORAGE_OPERATION_GRANT_INVALID",
            ));
        }
    }
    if string_field(payload, "bucket")
        .unwrap_or("")
        .trim()
        .is_empty()
        || string_field(payload, "objectKey")
            .unwrap_or("")
            .trim()
            .is_empty()
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    let condition = string_field(payload, "conditionType").unwrap_or("");
    let etag = string_field(payload, "expectedEtag").unwrap_or("");
    if condition != "CREATE_ONLY" && condition != "IF_MATCH"
        || condition == "CREATE_ONLY" && !etag.is_empty()
        || condition == "IF_MATCH"
            && (etag.len() < 2
                || !etag.starts_with('"')
                || !etag.ends_with('"')
                || etag.contains('\r')
                || etag.contains('\n'))
    {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    if integer_field(payload, "claimRevision")? < 1 {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    let length = integer_field(payload, "expectedLength")?;
    if length < 0 || length as u64 > 8 * 1024 * 1024 {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_INVALID",
        ));
    }
    Ok(())
}

fn verify_signature(
    document: &Map<String, Value>,
    context: &StorageOperationGrantContext<'_>,
) -> Result<(), StorageOperationGrantError> {
    if string_field(document, "signatureAlgorithm") != Some("Ed25519") {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_SIGNATURE_INVALID",
        ));
    }
    let signer_key_id = string_field(document, "signerKeyId").unwrap_or("");
    if signer_key_id != context.signer_key_id || !valid_signer_key_id(signer_key_id) {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_SIGNER_MISMATCH",
        ));
    }
    let signature = decode_fixed(string_field(document, "signature").unwrap_or(""), 64)?;
    let public_key = decode_fixed(context.signer_public_key, 32)?;
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    let mut message = SIGNATURE_DOMAIN.to_vec();
    message.extend(
        canonical_json_bytes(&Value::Object(unsigned))
            .map_err(|_| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?,
    );
    let verifying_key =
        VerifyingKey::from_bytes(public_key.as_slice().try_into().map_err(|_| {
            StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
        })?)
        .map_err(|_| {
            StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
        })?;
    let signature = Signature::from_bytes(signature.as_slice().try_into().map_err(|_| {
        StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
    })?);
    verifying_key
        .verify_strict(&message, &signature)
        .map_err(|_| {
            StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
        })?;
    Ok(())
}

fn grant_digest(document: &Map<String, Value>) -> Result<String, StorageOperationGrantError> {
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    unsigned.remove("digest");
    typed_digest(&Value::Object(unsigned))
}

fn typed_digest(value: &Value) -> Result<String, StorageOperationGrantError> {
    Ok(format!(
        "sha256:{}",
        sha256_hex(
            &canonical_json_bytes(value).map_err(|_| {
                StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID")
            })?
        )
    ))
}

fn reject_secrets(value: &Value) -> Result<(), StorageOperationGrantError> {
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
                    return Err(StorageOperationGrantError::new(
                        "STORAGE_OPERATION_GRANT_SECRET_DETECTED",
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
                return Err(StorageOperationGrantError::new(
                    "STORAGE_OPERATION_GRANT_SECRET_DETECTED",
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

fn integer_field(
    document: &Map<String, Value>,
    key: &str,
) -> Result<i64, StorageOperationGrantError> {
    document
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))
}

fn decode_fixed(value: &str, size: usize) -> Result<Vec<u8>, StorageOperationGrantError> {
    let raw = URL_SAFE_NO_PAD.decode(value).map_err(|_| {
        StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
    })?;
    if raw.len() != size {
        return Err(StorageOperationGrantError::new(
            "STORAGE_OPERATION_GRANT_SIGNATURE_INVALID",
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

fn parse_utc_z(value: &str) -> Result<i64, StorageOperationGrantError> {
    crate::authority_request::parse_utc_z_str(value)
        .map_err(|_| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))
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
