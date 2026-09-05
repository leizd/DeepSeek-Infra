//! Recovery-copy Receipt/Commit authentication status machine.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{python_int, python_int_or, python_str_or_empty, python_text, python_truthy};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub fn replay_recovery_authenticate_case(case: &Value) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "authenticate" => authenticate_case(case),
        "authenticate-committed" => authenticate_committed_case(case),
        "authenticate-parent" => authenticate_parent_case(case),
        "commit-hash" => match object_opt(case.get("commit")) {
            Some(commit) => json!({ "commitHash": commit_hash(&commit) }),
            None => json!({ "status": "error", "error": "missing-commit" }),
        },
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn authenticate_case(case: &Value) -> Value {
    let (status, receipt, commit) = authenticate_bytes(
        decode_hex(case.get("raw_receipt")),
        decode_hex(case.get("raw_commit")),
        &python_text(case.get("policy_id")),
        &python_text(case.get("backup_id")),
        case.get("expected_object_set_digest"),
    );
    json!({ "status": status, "receipt": receipt, "commit": commit })
}

fn authenticate_committed_case(case: &Value) -> Value {
    let base = authenticate_case(case);
    if base.get("status") != Some(&json!("authenticated")) {
        return base;
    }
    let receipt = base.get("receipt").cloned().unwrap_or(Value::Null);
    let Some(commit) = object_opt(base.get("commit")) else {
        return base;
    };
    if python_truthy(commit.get("commitHash").unwrap_or(&Value::Null))
        && python_text(commit.get("commitHash")) != commit_hash(&commit)
    {
        return json!({ "status": "corrupt", "receipt": receipt, "commit": commit });
    }
    if let Some(expected) = case.get("expected_previous_commit_hash") {
        if python_str_or_empty(commit.get("previousCommitHash")) != python_text(Some(expected)) {
            return json!({ "status": "conflicting", "receipt": receipt, "commit": commit });
        }
    }
    if let Some(expected) = case.get("expected_target_generation") {
        let actual = match python_int_or(commit.get("targetGeneration"), 0) {
            Ok(value) => value,
            Err(name) => {
                return json!({
                    "status": "error",
                    "oracle_exception": name,
                    "receipt": receipt,
                    "commit": commit,
                });
            }
        };
        let expected = match python_int(expected) {
            Ok(value) => value,
            Err(name) => {
                return json!({
                    "status": "error",
                    "oracle_exception": name,
                    "receipt": receipt,
                    "commit": commit,
                });
            }
        };
        if actual != expected {
            return json!({ "status": "conflicting", "receipt": receipt, "commit": commit });
        }
    }
    json!({ "status": "authenticated", "receipt": receipt, "commit": commit })
}

fn authenticate_parent_case(case: &Value) -> Value {
    let (status, receipt, commit) = authenticate_bytes(
        decode_hex(case.get("raw_receipt")),
        decode_hex(case.get("raw_commit")),
        &python_text(case.get("policy_id")),
        &python_text(case.get("expected_parent_backup_id")),
        case.get("expected_object_set_digest"),
    );
    if status != "authenticated" {
        return json!({ "ok": false, "reason": format!("parent-copy-status-{status}") });
    }
    let receipt = receipt.unwrap_or(Value::Null);
    let commit = commit.unwrap_or(Value::Null);
    let commit_map = commit.as_object();
    let receipt_map = receipt.as_object();
    if let Some(expected) = case.get("expected_receipt_digest") {
        let got = python_str_or_empty(commit_map.and_then(|map| map.get("receiptDigest")));
        if got != python_text(Some(expected)) {
            return json!({ "ok": false, "reason": "parent-receipt-digest-mismatch" });
        }
    }
    if let Some(expected) = case.get("expected_commit_hash") {
        let got = python_str_or_empty(commit_map.and_then(|map| map.get("commitHash")));
        if got != python_text(Some(expected)) {
            return json!({ "ok": false, "reason": "parent-commit-hash-mismatch" });
        }
    }
    if let Some(expected) = case.get("expected_lineage_id") {
        let lineage = or_chain(
            commit_map.and_then(|map| map.get("lineageId")),
            receipt_map.and_then(|map| map.get("lineageId")),
        );
        if lineage.as_ref().is_some_and(python_truthy)
            && python_text(lineage.as_ref()) != python_text(Some(expected))
        {
            return json!({ "ok": false, "reason": "parent-lineage-mismatch" });
        }
    }
    if let Some(expected) = case.get("expected_object_set_digest") {
        let sources = [
            commit_map.and_then(|map| map.get("objectSetDigest")),
            receipt_map.and_then(|map| map.get("objectSetDigest")),
            commit_map.and_then(|map| map.get("objectDigest")),
            receipt_map.and_then(|map| map.get("objectDigest")),
        ];
        let mut value = None;
        for source in sources {
            value = or_chain(value.as_ref(), source);
        }
        if python_str_or_empty(value.as_ref()) != python_text(Some(expected)) {
            return json!({ "ok": false, "reason": "parent-object-set-digest-mismatch" });
        }
    }
    json!({ "ok": true, "reason": "authenticated" })
}

fn authenticate_bytes(
    raw_receipt: Option<Vec<u8>>,
    raw_commit: Option<Vec<u8>>,
    policy_id: &str,
    backup_id: &str,
    expected_object_set_digest: Option<&Value>,
) -> (String, Option<Value>, Option<Value>) {
    if raw_receipt.is_none() && raw_commit.is_none() {
        return ("missing".into(), None, None);
    }
    let (Some(raw_receipt), Some(raw_commit)) = (raw_receipt, raw_commit) else {
        return ("corrupt".into(), None, None);
    };
    let Ok(receipt_text) = std::str::from_utf8(&raw_receipt) else {
        return ("corrupt".into(), None, None);
    };
    let Ok(commit_text) = std::str::from_utf8(&raw_commit) else {
        return ("corrupt".into(), None, None);
    };
    let receipt: Value = match serde_json::from_str(receipt_text) {
        Ok(value) => value,
        Err(_) => return ("corrupt".into(), None, None),
    };
    let commit: Value = match serde_json::from_str(commit_text) {
        Ok(value) => value,
        Err(_) => return ("corrupt".into(), None, None),
    };
    let (Some(receipt_map), Some(commit_map)) = (receipt.as_object(), commit.as_object()) else {
        return ("corrupt".into(), None, None);
    };
    let calc = sha256_hex(&raw_receipt);
    if python_text(commit_map.get("receiptDigest")) != calc {
        return ("corrupt".into(), Some(receipt), Some(commit));
    }
    let schema_ver = match python_int_or(commit_map.get("schemaVersion"), 0) {
        Ok(value) => value,
        Err(_) => return ("corrupt".into(), Some(receipt), Some(commit)),
    };
    if ![1, 2, 3, 4].contains(&schema_ver) {
        return ("corrupt".into(), Some(receipt), Some(commit));
    }
    if python_text(commit_map.get("policyId")) != policy_id
        || python_text(commit_map.get("backupId")) != backup_id
    {
        return ("conflicting".into(), Some(receipt), Some(commit));
    }
    if python_text(receipt_map.get("policyId")) != policy_id
        || python_text(receipt_map.get("backupId")) != backup_id
    {
        return ("conflicting".into(), Some(receipt), Some(commit));
    }
    let r_osd = or_chain(
        receipt_map.get("objectSetDigest"),
        receipt_map.get("objectDigest"),
    );
    let c_osd = or_chain(
        commit_map.get("objectSetDigest"),
        commit_map.get("objectDigest"),
    );
    if r_osd.as_ref().is_none_or(|value| !python_truthy(value)) {
        return ("corrupt".into(), Some(receipt), Some(commit));
    }
    if let Some(c_osd) = c_osd.as_ref() {
        if r_osd.as_ref() != Some(c_osd) {
            return ("corrupt".into(), Some(receipt), Some(commit));
        }
    }
    if let Some(expected) = expected_object_set_digest {
        if python_text(r_osd.as_ref()) != python_text(Some(expected)) {
            return ("conflicting".into(), Some(receipt), Some(commit));
        }
    }
    ("authenticated".into(), Some(receipt), Some(commit))
}

fn commit_hash(commit: &Map<String, Value>) -> String {
    let mut body = commit.clone();
    body.remove("commitHash");
    let bytes = serde_json::to_vec(&Value::Object(body)).unwrap_or_default();
    sha256_hex(&bytes)
}

fn or_chain(left: Option<&Value>, right: Option<&Value>) -> Option<Value> {
    match left {
        Some(value) if python_truthy(value) => Some(value.clone()),
        _ => right.cloned(),
    }
}

fn object_opt(value: Option<&Value>) -> Option<Map<String, Value>> {
    match value {
        Some(Value::Object(map)) => Some(map.clone()),
        _ => None,
    }
}

fn decode_hex(value: Option<&Value>) -> Option<Vec<u8>> {
    let Value::String(text) = value? else {
        return None;
    };
    if text.len() % 2 != 0 {
        return Some(Vec::new());
    }
    let mut bytes = Vec::with_capacity(text.len() / 2);
    let chars: Vec<char> = text.chars().collect();
    for chunk in chars.chunks(2) {
        let Ok(byte) = u8::from_str_radix(&chunk.iter().collect::<String>(), 16) else {
            return Some(Vec::new());
        };
        bytes.push(byte);
    }
    Some(bytes)
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = std::fmt::Write::write_fmt(&mut encoded, format_args!("{byte:02x}"));
    }
    encoded
}
