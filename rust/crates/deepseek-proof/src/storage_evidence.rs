use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::fmt::Write;

pub const AUTONOMOUS_STORAGE_BYTES_CHECKS: &[&str] = &[
    "destinationReceiptAuthenticated",
    "destinationCommitAuthenticated",
    "autonomousProofUsesActualReceiptBytes",
    "autonomousProofUsesActualCommitBytes",
    "receiptSha256MatchesCommitReceiptDigest",
    "proofObjectSetDigestMatchesCommit",
    "proofObjectKeysExistOnExpectedMinioEndpoint",
];

const REQUIRED_FIELDS: &[&str] = &[
    "targetId",
    "endpoint",
    "bucket",
    "backupId",
    "policyId",
    "actionId",
    "receiptKey",
    "commitKey",
    "receiptBytesBase64",
    "commitBytesBase64",
    "rawReceiptSha256",
    "rawCommitSha256",
    "commitReceiptDigest",
    "objectSetDigest",
    "providerReceiptObject",
    "providerCommitObject",
];

pub fn validate_autonomous_storage_bytes_proof(value: &Value) -> Vec<String> {
    let Some(evidence) = value.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = required_field_errors(evidence);
    for field in [
        "rawReceiptSha256",
        "rawCommitSha256",
        "commitReceiptDigest",
        "objectSetDigest",
    ] {
        if !missing(evidence.get(field)) && !is_plain_sha256(evidence.get(field)) {
            errors.push(format!("invalid-sha256:{field}"));
        }
    }

    let raw_receipt = decode_document_bytes(
        evidence.get("receiptBytesBase64"),
        "receiptBytesBase64",
        &mut errors,
    );
    let raw_commit = decode_document_bytes(
        evidence.get("commitBytesBase64"),
        "commitBytesBase64",
        &mut errors,
    );
    let receipt_sha256 = raw_receipt.as_deref().map(sha256_hex).unwrap_or_default();
    let commit_sha256 = raw_commit.as_deref().map(sha256_hex).unwrap_or_default();

    if !receipt_sha256.is_empty()
        && receipt_sha256 != text_or_empty(evidence.get("rawReceiptSha256"))
    {
        errors.push("raw-receipt-sha256-mismatch".to_string());
    }
    if !commit_sha256.is_empty() && commit_sha256 != text_or_empty(evidence.get("rawCommitSha256"))
    {
        errors.push("raw-commit-sha256-mismatch".to_string());
    }
    if !receipt_sha256.is_empty()
        && receipt_sha256 != text_or_empty(evidence.get("commitReceiptDigest"))
    {
        errors.push("receipt-digest-binding-mismatch".to_string());
    }

    let receipt = parse_document(raw_receipt.as_deref(), "receipt", &mut errors);
    let commit = parse_document(raw_commit.as_deref(), "commit", &mut errors);
    if let Some(ref receipt) = receipt {
        if receipt.get("schemaVersion").and_then(Value::as_u64) != Some(4) {
            errors.push("receipt-schema-not-v4".to_string());
        }
    }
    if let Some(ref commit) = commit {
        if commit.get("schemaVersion").and_then(Value::as_u64) != Some(4) {
            errors.push("commit-schema-not-v4".to_string());
        }
    }

    let backup_id = text_or_empty(evidence.get("backupId"));
    let policy_id = text_or_empty(evidence.get("policyId"));
    let object_set_digest = text_or_empty(evidence.get("objectSetDigest"));
    if receipt
        .as_ref()
        .is_some_and(|receipt| text_or_empty(receipt.get("backupId")) != backup_id)
    {
        errors.push("receipt-backup-id-mismatch".to_string());
    }
    if commit
        .as_ref()
        .is_some_and(|commit| text_or_empty(commit.get("backupId")) != backup_id)
    {
        errors.push("commit-backup-id-mismatch".to_string());
    }
    if commit
        .as_ref()
        .is_some_and(|commit| text_or_empty(commit.get("policyId")) != policy_id)
    {
        errors.push("commit-policy-id-mismatch".to_string());
    }
    if commit.as_ref().is_some_and(|commit| {
        text_or_empty(commit.get("receiptDigest"))
            != text_or_empty(evidence.get("commitReceiptDigest"))
    }) {
        errors.push("commit-receipt-digest-mismatch".to_string());
    }
    if receipt
        .as_ref()
        .is_some_and(|receipt| text_or_empty(receipt.get("objectSetDigest")) != object_set_digest)
    {
        errors.push("receipt-object-set-digest-mismatch".to_string());
    }
    if commit
        .as_ref()
        .is_some_and(|commit| text_or_empty(commit.get("objectSetDigest")) != object_set_digest)
    {
        errors.push("commit-object-set-digest-mismatch".to_string());
    }

    if !backup_id.is_empty()
        && text_or_empty(evidence.get("receiptKey")) != format!("receipts/{backup_id}.json")
    {
        errors.push("receipt-key-mismatch".to_string());
    }
    if !backup_id.is_empty()
        && !policy_id.is_empty()
        && text_or_empty(evidence.get("commitKey"))
            != format!("commits/{policy_id}/{backup_id}.json")
    {
        errors.push("commit-key-mismatch".to_string());
    }
    validate_provider_object(
        evidence.get("providerReceiptObject"),
        "providerReceiptObject",
        evidence.get("receiptKey"),
        raw_receipt.as_deref().unwrap_or_default(),
        &receipt_sha256,
        &mut errors,
    );
    validate_provider_object(
        evidence.get("providerCommitObject"),
        "providerCommitObject",
        evidence.get("commitKey"),
        raw_commit.as_deref().unwrap_or_default(),
        &commit_sha256,
        &mut errors,
    );
    errors
}

fn required_field_errors(evidence: &Map<String, Value>) -> Vec<String> {
    REQUIRED_FIELDS
        .iter()
        .filter(|field| missing(evidence.get(**field)))
        .map(|field| format!("missing-field:{field}"))
        .collect()
}

fn decode_document_bytes(
    value: Option<&Value>,
    field: &str,
    errors: &mut Vec<String>,
) -> Option<Vec<u8>> {
    if missing(value) {
        return None;
    }
    let encoded = python_string(value.unwrap_or(&Value::Null));
    match STANDARD.decode(encoded.as_bytes()) {
        Ok(bytes) if bytes.is_empty() => {
            errors.push(format!("empty-bytes:{field}"));
            None
        }
        Ok(bytes) => Some(bytes),
        Err(_) => {
            errors.push(format!("invalid-base64:{field}"));
            None
        }
    }
}

fn parse_document(
    bytes: Option<&[u8]>,
    label: &str,
    errors: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let bytes = bytes?;
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(document)) => Some(document),
        Ok(_) => {
            errors.push(format!("{label}-must-be-object"));
            None
        }
        Err(_) => {
            errors.push(format!("invalid-{label}-json"));
            None
        }
    }
}

fn validate_provider_object(
    value: Option<&Value>,
    field: &str,
    expected_key: Option<&Value>,
    bytes: &[u8],
    digest: &str,
    errors: &mut Vec<String>,
) {
    let Some(provider) = value.and_then(Value::as_object) else {
        errors.push(format!("{field}-must-be-object"));
        return;
    };
    if provider.get("key") != expected_key {
        errors.push(format!("{field}-key-mismatch"));
    }
    if !python_size_equals(provider.get("size"), bytes.len()) {
        errors.push(format!("{field}-size-mismatch"));
    }
    let provider_sha256 = provider.get("sha256");
    if !missing(provider_sha256) && provider_sha256 != Some(&Value::String(digest.to_string())) {
        errors.push(format!("{field}-sha256-mismatch"));
    }
    if text_or_empty(provider.get("etag")).is_empty() {
        errors.push(format!("{field}-etag-missing"));
    }
}

fn python_size_equals(value: Option<&Value>, expected: usize) -> bool {
    match value {
        Some(Value::Bool(value)) => usize::from(*value) == expected,
        Some(Value::Number(value)) => value.as_f64() == Some(expected as f64),
        _ => false,
    }
}

fn missing(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::String(value)) => value.is_empty(),
        _ => false,
    }
}

fn is_plain_sha256(value: Option<&Value>) -> bool {
    let value = text_or_empty(value);
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null | Value::Bool(false)) => String::new(),
        Some(value) if !truthy(value) => String::new(),
        Some(value) => python_string(value),
    }
}

fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Array(_) | Value::Object(_) => serde_json::to_string(value).unwrap_or_default(),
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(false) => false,
        Value::Bool(true) => true,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_base64_and_sha256_helpers_fail_closed() {
        let mut errors = Vec::new();
        assert_eq!(
            decode_document_bytes(
                Some(&Value::String("%%%".to_string())),
                "receipt",
                &mut errors
            ),
            None
        );
        assert_eq!(errors, ["invalid-base64:receipt"]);
        assert!(is_plain_sha256(Some(&Value::String("a".repeat(64)))));
        assert!(!is_plain_sha256(Some(&Value::String("A".repeat(64)))));
    }
}
