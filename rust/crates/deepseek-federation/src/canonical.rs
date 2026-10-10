use crate::attestation::AttestationError;
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::Serialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const SECRET_FIELD_NAMES: &[&str] = &[
    "accesskey",
    "ageidentity",
    "ageprivateidentity",
    "credential",
    "credentialref",
    "credentialreference",
    "passphrase",
    "password",
    "privatekey",
    "privatekeyenvelope",
    "secret",
    "secretkey",
    "sessiontoken",
];

const SECRET_VALUE_MARKERS: &[&str] = &[
    "age-secret-key-",
    "begin private key",
    "begin openssh private key",
];

pub(crate) fn canonical_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>, AttestationError> {
    let mut normalized = serde_json::to_value(value).map_err(|_| {
        AttestationError::new("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID")
    })?;
    normalized.sort_all_objects();
    serde_json::to_vec(&normalized).map_err(|_| {
        AttestationError::new("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID")
    })
}

pub(crate) fn sha256_hex(value: &[u8]) -> String {
    hex::encode(Sha256::digest(value))
}

pub(crate) fn typed_sha256(value: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(value))
}

pub(crate) fn is_plain_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn is_typed_sha256(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(is_plain_sha256)
}

pub(crate) fn is_failure_domain(value: &str) -> bool {
    value
        .strip_prefix("federation-peer-domain:sha256:")
        .is_some_and(is_plain_sha256)
}

pub(crate) fn validate_fleet_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

pub(crate) fn validate_control_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

pub(crate) fn parse_timestamp(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
    {
        return None;
    }
    let year = decimal(bytes, 0, 4)? as i64;
    let month = decimal(bytes, 5, 2)? as i64;
    let day = decimal(bytes, 8, 2)? as i64;
    let hour = decimal(bytes, 11, 2)? as i64;
    let minute = decimal(bytes, 14, 2)? as i64;
    let second = decimal(bytes, 17, 2)? as i64;
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let leap_year = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap_year => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=days_in_month).contains(&day) {
        return None;
    }
    let days = days_from_civil(year, month, day);
    Some(days * 86_400 + hour * 3_600 + minute * 60 + second)
}

pub(crate) fn decode_fixed<const N: usize>(value: &str) -> Option<[u8; N]> {
    let decoded = URL_SAFE_NO_PAD.decode(value).ok()?;
    decoded.try_into().ok()
}

pub(crate) fn encode_b64url(value: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(value)
}

pub(crate) fn object(value: &Value) -> Option<&Map<String, Value>> {
    value.as_object()
}

pub(crate) fn string_field<'a>(value: &'a Map<String, Value>, name: &str) -> Option<&'a str> {
    value.get(name)?.as_str()
}

pub(crate) fn positive_u64_field(value: &Map<String, Value>, name: &str) -> Option<u64> {
    value.get(name)?.as_u64().filter(|number| *number > 0)
}

pub(crate) fn assert_secret_free<T: Serialize>(value: &T) -> Result<(), AttestationError> {
    let normalized = serde_json::to_value(value)
        .map_err(|_| AttestationError::new("FEDERATION_DOCUMENT_CONTAINS_SECRET"))?;
    if contains_secret(&normalized) {
        return Err(AttestationError::new("FEDERATION_DOCUMENT_CONTAINS_SECRET"));
    }
    Ok(())
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(items) => items.iter().any(|(key, item)| {
            let normalized_key: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            SECRET_FIELD_NAMES.contains(&normalized_key.as_str()) || contains_secret(item)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        Value::String(item) => {
            let lowered = item.to_lowercase();
            SECRET_VALUE_MARKERS
                .iter()
                .any(|marker| lowered.contains(marker))
        }
        _ => false,
    }
}

fn decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    bytes
        .get(start..start + length)?
        .iter()
        .try_fold(0_u32, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(*byte - b'0'))
        })
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

mod hex {
    use std::fmt::Write;

    pub fn encode(data: impl AsRef<[u8]>) -> String {
        let mut encoded = String::with_capacity(data.as_ref().len() * 2);
        for byte in data.as_ref() {
            let _ = write!(encoded, "{byte:02x}");
        }
        encoded
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_timestamp_parser_handles_leap_years_and_epoch() {
        assert_eq!(parse_timestamp("1970-01-01T00:00:00Z"), Some(0));
        assert!(parse_timestamp("2024-02-29T23:59:59Z").is_some());
        assert_eq!(parse_timestamp("2026-02-29T00:00:00Z"), None);
        assert_eq!(parse_timestamp("2026-01-01T00:00:00+00:00"), None);
    }

    #[test]
    fn identifier_patterns_match_the_frozen_ascii_contract() {
        assert!(validate_fleet_id("fleet-a_1.example"));
        assert!(!validate_fleet_id("Fleet-A"));
        assert!(validate_control_id("target:a-1"));
        assert!(!validate_control_id("target/a"));
    }
}
