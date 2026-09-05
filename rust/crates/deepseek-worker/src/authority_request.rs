use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashSet};

pub const AUTHORITY_REQUEST_SCHEMA: &str = "control-authority-request-v1";
pub const MAX_AUTHORITY_REQUEST_BYTES: usize = 16 * 1024;
const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:control-authority-request-v1\x00";
const MAX_LIFETIME_SECONDS: i64 = 300;
const AUTHORITY_REQUEST_FIELDS: &[&str] = &[
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthorityRequestError {
    pub code: &'static str,
}

impl AuthorityRequestError {
    pub(crate) fn new(code: &'static str) -> Self {
        Self { code }
    }
}

impl From<deepseek_protocol::AdmitError> for AuthorityRequestError {
    fn from(error: deepseek_protocol::AdmitError) -> Self {
        Self { code: error.code() }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthorityInstallFields {
    pub action_id: String,
    pub execution_epoch: u64,
    pub request_id: String,
    pub nonce: String,
}

#[derive(Debug, Clone)]
pub struct AuthorityRequestContext<'a> {
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
    pub max_future_skew_seconds: i64,
}

pub fn verify_authority_request_document(
    raw: &[u8],
    context: &AuthorityRequestContext<'_>,
) -> Result<Value, AuthorityRequestError> {
    if raw.is_empty() {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    if raw.len() > MAX_AUTHORITY_REQUEST_BYTES {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_TOO_LARGE"));
    }
    let document: Value = serde_json::from_slice(raw)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    if canonical_json_bytes(&document)? != raw {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_CANONICAL_MISMATCH",
        ));
    }
    let object = document
        .as_object()
        .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let keys: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if keys.len() != AUTHORITY_REQUEST_FIELDS.len()
        || AUTHORITY_REQUEST_FIELDS
            .iter()
            .any(|field| !keys.contains(field))
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_FIELDS_INVALID",
        ));
    }
    reject_secrets(&document)?;
    verify_envelope(object, context)?;
    verify_signature(object, context)?;
    Ok(document)
}

pub fn signer_key_id_for_public_key(public_key: &str) -> Result<String, AuthorityRequestError> {
    let raw = decode_fixed(public_key, 32)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNER_MISMATCH"))?;
    Ok(format!("ctrl-signer-{}", &sha256_hex(&raw)[..16]))
}

pub fn authority_request_install_fields(
    document: &Value,
) -> Result<AuthorityInstallFields, AuthorityRequestError> {
    let object = document
        .as_object()
        .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let action_id = string_field(object, "actionId").unwrap_or("").to_string();
    if !valid_control_id(&action_id) {
        return Err(AuthorityRequestError::new("EMPTY_ACTION_ID"));
    }
    let epoch = integer_field(object, "executionEpoch")?;
    if epoch < 1 {
        return Err(AuthorityRequestError::new("ZERO_EXECUTION_EPOCH"));
    }
    let request_id = string_field(object, "requestId").unwrap_or("").to_string();
    let nonce = string_field(object, "nonce").unwrap_or("").to_string();
    if !valid_hex64(&request_id) || !valid_hex64(&nonce) {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    Ok(AuthorityInstallFields {
        action_id,
        execution_epoch: epoch as u64,
        request_id,
        nonce,
    })
}

pub fn utc_z_now() -> Result<String, AuthorityRequestError> {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?
        .as_secs();
    format_utc_z(seconds as i64)
}

pub(crate) fn parse_utc_z_str(value: &str) -> Result<i64, AuthorityRequestError> {
    parse_utc_z(value)
}

pub(crate) fn valid_fleet_id_str(value: &str) -> bool {
    valid_fleet_id(value)
}

fn format_utc_z(seconds: i64) -> Result<String, AuthorityRequestError> {
    if seconds < 0 {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    let days = seconds / 86400;
    let remainder = (seconds % 86400) as u32;
    let (year, month, day) = civil_from_days(days);
    let hour = remainder / 3600;
    let minute = (remainder % 3600) / 60;
    let second = remainder % 60;
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let day_of_era = z - era * 146097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    let year = if month <= 2 { year + 1 } else { year };
    (year, month as u32, day as u32)
}

fn verify_envelope(
    document: &Map<String, Value>,
    context: &AuthorityRequestContext<'_>,
) -> Result<(), AuthorityRequestError> {
    if string_field(document, "schema") != Some(AUTHORITY_REQUEST_SCHEMA)
        || document.get("schemaVersion") != Some(&Value::from(1))
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_SCHEMA_INVALID",
        ));
    }
    if string_field(document, "operation") != Some(context.expected_operation)
        || string_field(document, "operation") != Some("install-epoch")
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_OPERATION_INVALID",
        ));
    }
    if string_field(document, "domain") != Some(context.expected_domain) {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_DOMAIN_MISMATCH",
        ));
    }
    if string_field(document, "runtime") != Some("go")
        || string_field(document, "runtime") != Some(context.expected_runtime)
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_RUNTIME_MISMATCH",
        ));
    }
    if string_field(document, "mode") != Some("shadow")
        || string_field(document, "mode") != Some(context.expected_mode)
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_MODE_MISMATCH",
        ));
    }
    let fleet_id = string_field(document, "fleetId").unwrap_or("");
    if !valid_fleet_id(fleet_id) || fleet_id != context.expected_fleet_id {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_FLEET_MISMATCH",
        ));
    }
    if string_field(document, "environment") != Some(context.expected_environment)
        || context.expected_environment.is_empty()
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH",
        ));
    }
    if string_field(document, "role") != Some("control-plane")
        || string_field(document, "role") != Some(context.expected_role)
    {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_ROLE_MISMATCH",
        ));
    }
    let action_id = string_field(document, "actionId").unwrap_or("");
    if !valid_control_id(action_id) {
        return Err(AuthorityRequestError::new("EMPTY_ACTION_ID"));
    }
    let epoch = integer_field(document, "executionEpoch")?;
    if epoch < 1 {
        return Err(AuthorityRequestError::new("ZERO_EXECUTION_EPOCH"));
    }
    if integer_field(document, "revision")? < 1 {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    let fencing_token = integer_field(document, "fencingToken")?;
    if fencing_token < 1 || fencing_token != context.current_fencing_token {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_STALE_FENCING_TOKEN",
        ));
    }
    if epoch <= context.live_epoch {
        return Err(AuthorityRequestError::new("STALE_EXECUTION_EPOCH"));
    }
    let request_id = string_field(document, "requestId").unwrap_or("");
    let nonce = string_field(document, "nonce").unwrap_or("");
    if !valid_hex64(request_id) || !valid_hex64(nonce) {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    if context.seen_request_ids.contains(request_id) {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_REPLAY"));
    }
    if context.seen_nonces.contains(nonce) {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_NONCE_REUSE"));
    }
    let payload = document
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    if !payload.is_empty() {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    let payload_digest = typed_digest(&Value::Object(Map::new()))?;
    if string_field(document, "payloadDigest") != Some(payload_digest.as_str()) {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_PAYLOAD_DIGEST_MISMATCH",
        ));
    }
    let digest = authority_request_digest(document)?;
    if string_field(document, "digest") != Some(digest.as_str()) {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_DIGEST_MISMATCH",
        ));
    }
    let issued_at = parse_utc_z(string_field(document, "issuedAt").unwrap_or(""))?;
    let expires_at = parse_utc_z(string_field(document, "expiresAt").unwrap_or(""))?;
    let now = parse_utc_z(context.now)?;
    if expires_at <= issued_at || expires_at - issued_at > MAX_LIFETIME_SECONDS {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    if expires_at <= now {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_EXPIRED"));
    }
    let skew = context.max_future_skew_seconds.max(0);
    if issued_at.saturating_sub(now) > skew {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_FUTURE_SKEW"));
    }
    Ok(())
}

fn verify_signature(
    document: &Map<String, Value>,
    context: &AuthorityRequestContext<'_>,
) -> Result<(), AuthorityRequestError> {
    if string_field(document, "signatureAlgorithm") != Some("Ed25519") {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_SIGNATURE_INVALID",
        ));
    }
    let signer_key_id = string_field(document, "signerKeyId").unwrap_or("");
    if signer_key_id != context.signer_key_id || !valid_signer_key_id(signer_key_id) {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_SIGNER_MISMATCH",
        ));
    }
    let signature = decode_fixed(string_field(document, "signature").unwrap_or(""), 64)?;
    let public_key = decode_fixed(context.signer_public_key, 32)?;
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    let mut message = SIGNATURE_DOMAIN.to_vec();
    message.extend(canonical_json_bytes(&Value::Object(unsigned))?);
    let verifying_key = VerifyingKey::from_bytes(
        public_key
            .as_slice()
            .try_into()
            .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNATURE_INVALID"))?,
    )
    .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNATURE_INVALID"))?;
    let signature = Signature::from_bytes(
        signature
            .as_slice()
            .try_into()
            .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNATURE_INVALID"))?,
    );
    verifying_key
        .verify_strict(&message, &signature)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNATURE_INVALID"))?;
    Ok(())
}

fn authority_request_digest(
    document: &Map<String, Value>,
) -> Result<String, AuthorityRequestError> {
    let mut unsigned = document.clone();
    unsigned.remove("signature");
    unsigned.remove("digest");
    typed_digest(&Value::Object(unsigned))
}

fn typed_digest(value: &Value) -> Result<String, AuthorityRequestError> {
    Ok(format!(
        "sha256:{}",
        sha256_hex(&canonical_json_bytes(value)?)
    ))
}

pub(crate) fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>, AuthorityRequestError> {
    serde_json::to_vec(&sorted(value)?)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))
}

fn sorted(value: &Value) -> Result<Value, AuthorityRequestError> {
    match value {
        Value::Object(map) => {
            let mut ordered = Map::new();
            let mut keys: Vec<_> = map.keys().cloned().collect();
            keys.sort();
            for key in keys {
                ordered.insert(key.clone(), sorted(&map[&key])?);
            }
            Ok(Value::Object(ordered))
        }
        Value::Array(items) => Ok(Value::Array(
            items.iter().map(sorted).collect::<Result<Vec<_>, _>>()?,
        )),
        other => Ok(other.clone()),
    }
}

fn reject_secrets(value: &Value) -> Result<(), AuthorityRequestError> {
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
                    return Err(AuthorityRequestError::new(
                        "AUTHORITY_REQUEST_SECRET_DETECTED",
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
                return Err(AuthorityRequestError::new(
                    "AUTHORITY_REQUEST_SECRET_DETECTED",
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

fn integer_field(document: &Map<String, Value>, key: &str) -> Result<i64, AuthorityRequestError> {
    document
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))
}

fn decode_fixed(value: &str, size: usize) -> Result<Vec<u8>, AuthorityRequestError> {
    let raw = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNATURE_INVALID"))?;
    if raw.len() != size {
        return Err(AuthorityRequestError::new(
            "AUTHORITY_REQUEST_SIGNATURE_INVALID",
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

fn valid_fleet_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes[1..].iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
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

fn valid_hex64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_signer_key_id(value: &str) -> bool {
    value.len() == "ctrl-signer-".len() + 16
        && value.starts_with("ctrl-signer-")
        && value["ctrl-signer-".len()..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn parse_utc_z(value: &str) -> Result<i64, AuthorityRequestError> {
    if value.len() != 20 || !value.ends_with('Z') {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    let year: i64 = value[0..4]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let month: u32 = value[5..7]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let day: u32 = value[8..10]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let hour: u32 = value[11..13]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let minute: u32 = value[14..16]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    let second: u32 = value[17..19]
        .parse()
        .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
    if value.as_bytes()[4] != b'-'
        || value.as_bytes()[7] != b'-'
        || value.as_bytes()[10] != b'T'
        || value.as_bytes()[13] != b':'
        || value.as_bytes()[16] != b':'
        || !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    Ok(days_from_civil(year, month, day) * 86400 + i64::from(hour * 3600 + minute * 60 + second))
}

fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let mut year = year;
    let mut month = month as i64;
    if month <= 2 {
        year -= 1;
        month += 9;
    } else {
        month -= 3;
    }
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year = (153 * month + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146097 + day_of_era - 719468
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_format_round_trips_frozen_timestamp() {
        let seconds = parse_utc_z("2026-09-04T00:00:40Z").unwrap();
        assert_eq!(format_utc_z(seconds).unwrap(), "2026-09-04T00:00:40Z");
        assert_eq!(
            signer_key_id_for_public_key("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo").unwrap(),
            "ctrl-signer-21fe31dfa154a261"
        );
    }
}
