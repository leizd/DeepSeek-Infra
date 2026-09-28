//! Replays the frozen control-mutation-request-v2 (compat v32) corpus.
//!
//! v1 is covered by `frozen_mutation_request_v17.rs`; this file proves the v2
//! revision, including the record body the apply operation commits to, and that
//! the two revisions cannot be confused with each other.

use deepseek_worker::{
    MutationRequestContext, verify_mutation_request_document, verify_mutation_request_v2_document,
};
use serde::Deserialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

#[derive(Debug, Deserialize)]
struct Fixture {
    canonical_request: String,
    request_digest: String,
    signer_public_key: String,
    signer_key_id: String,
    now: String,
    max_future_skew_seconds: i64,
    context: FixtureContext,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct FixtureContext {
    current_fencing_token: i64,
    expected_domain: String,
    expected_environment: String,
    expected_fleet_id: String,
    expected_mode: String,
    expected_operation: String,
    expected_role: String,
    expected_runtime: String,
    live_epoch: i64,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    error: String,
    #[serde(default)]
    raw: Option<String>,
    #[serde(default)]
    replace: Value,
    #[serde(default)]
    add: Value,
    #[serde(default)]
    drop: Option<String>,
    #[serde(default)]
    recompute_digest: bool,
    #[serde(default)]
    noncanonical: bool,
    #[serde(default)]
    context: Value,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v32/control/mutation_request_v2_vector.json"
    )))
    .unwrap()
}

fn text<'a>(overrides: &'a Value, key: &str, fallback: &'a str) -> &'a str {
    overrides
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(fallback)
}

fn integer(overrides: &Value, key: &str, fallback: i64) -> i64 {
    overrides
        .get(key)
        .and_then(Value::as_i64)
        .unwrap_or(fallback)
}

fn context<'a>(fixture: &'a Fixture, overrides: &'a Value) -> MutationRequestContext<'a> {
    let frozen = &fixture.context;
    let mut seen_request_ids = HashSet::new();
    if let Some(values) = overrides.get("seen_request_ids").and_then(Value::as_array) {
        for value in values {
            seen_request_ids.insert(value.as_str().unwrap().to_string());
        }
    }
    let mut seen_nonces = HashSet::new();
    if let Some(values) = overrides.get("seen_nonces").and_then(Value::as_array) {
        for value in values {
            seen_nonces.insert(value.as_str().unwrap().to_string());
        }
    }
    let mut seen_operation_digests = HashMap::new();
    if let Some(values) = overrides
        .get("seen_operation_digests")
        .and_then(Value::as_object)
    {
        for (key, value) in values {
            seen_operation_digests.insert(key.clone(), value.as_str().unwrap().to_string());
        }
    }
    MutationRequestContext {
        now: text(overrides, "now", fixture.now.as_str()),
        signer_public_key: fixture.signer_public_key.as_str(),
        signer_key_id: text(overrides, "signer_key_id", fixture.signer_key_id.as_str()),
        expected_domain: text(
            overrides,
            "expected_domain",
            frozen.expected_domain.as_str(),
        ),
        expected_operation: text(
            overrides,
            "expected_operation",
            frozen.expected_operation.as_str(),
        ),
        expected_runtime: text(
            overrides,
            "expected_runtime",
            frozen.expected_runtime.as_str(),
        ),
        expected_mode: text(overrides, "expected_mode", frozen.expected_mode.as_str()),
        expected_fleet_id: text(
            overrides,
            "expected_fleet_id",
            frozen.expected_fleet_id.as_str(),
        ),
        expected_environment: text(
            overrides,
            "expected_environment",
            frozen.expected_environment.as_str(),
        ),
        expected_role: text(overrides, "expected_role", frozen.expected_role.as_str()),
        current_fencing_token: integer(
            overrides,
            "current_fencing_token",
            frozen.current_fencing_token,
        ),
        live_epoch: integer(overrides, "live_epoch", frozen.live_epoch),
        seen_request_ids,
        seen_nonces,
        seen_operation_digests,
        max_future_skew_seconds: fixture.max_future_skew_seconds,
    }
}

fn canonical_bytes(value: &Value) -> Vec<u8> {
    serde_json::to_vec(&sorted(value)).unwrap()
}

fn sorted(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut ordered = Map::new();
            let mut keys: Vec<_> = map.keys().cloned().collect();
            keys.sort();
            for key in keys {
                ordered.insert(key.clone(), sorted(&map[&key]));
            }
            Value::Object(ordered)
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

#[test]
fn rust_accepts_the_frozen_apply_mutation_request() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let verified = verify_mutation_request_v2_document(
        fixture.canonical_request.as_bytes(),
        &context(&fixture, &empty),
    )
    .unwrap();
    assert_eq!(
        verified.get("digest").and_then(Value::as_str),
        Some(fixture.request_digest.as_str())
    );
    assert_eq!(
        verified.get("schema").and_then(Value::as_str),
        Some("control-mutation-request-v2")
    );
    assert_eq!(
        verified.get("operation").and_then(Value::as_str),
        Some("apply-mutation")
    );
    let body = verified
        .get("payload")
        .and_then(|payload| payload.get("recordPayload"))
        .and_then(Value::as_object)
        .unwrap();
    assert_eq!(
        body.get("name").and_then(Value::as_str),
        Some("approved policy")
    );
    assert_eq!(fixture.cases.len(), 34, "frozen v2 case count");
}

#[test]
fn rust_rejects_frozen_apply_mutation_failures() {
    let fixture = fixture();
    for case in &fixture.cases {
        let raw = if let Some(raw) = &case.raw {
            raw.as_bytes().to_vec()
        } else {
            let mut document: Value = serde_json::from_str(&fixture.canonical_request).unwrap();
            let object = document.as_object_mut().unwrap();
            if let Some(replace) = case.replace.as_object() {
                for (key, value) in replace {
                    object.insert(key.clone(), value.clone());
                }
            }
            if case.recompute_digest {
                let mut unsigned = object.clone();
                unsigned.remove("signature");
                unsigned.remove("digest");
                let mut digest = String::from("sha256:");
                for byte in Sha256::digest(canonical_bytes(&Value::Object(unsigned))) {
                    use std::fmt::Write;
                    let _ = write!(digest, "{byte:02x}");
                }
                object.insert("digest".to_string(), Value::String(digest));
            }
            if let Some(drop) = &case.drop {
                object.remove(drop);
            }
            if let Some(add) = case.add.as_object() {
                for (key, value) in add {
                    object.insert(key.clone(), value.clone());
                }
            }
            if case.noncanonical {
                serde_json::to_vec_pretty(&document).unwrap()
            } else {
                canonical_bytes(&document)
            }
        };
        let error = verify_mutation_request_v2_document(&raw, &context(&fixture, &case.context))
            .expect_err(&case.name);
        assert_eq!(error.code, case.error, "{}", case.name);
    }
}

/// The two revisions must not be interchangeable: the v1 verifier refuses a v2
/// document (and the `v1-signature-domain` case above proves the reverse, since a
/// v2 document signed under the v1 domain must not verify).
#[test]
fn rust_keeps_the_mutation_request_revisions_disjoint() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let error = verify_mutation_request_document(
        fixture.canonical_request.as_bytes(),
        &context(&fixture, &empty),
    )
    .expect_err("v1 verifier must refuse a v2 document");
    assert_eq!(error.code, "MUTATION_REQUEST_SCHEMA_INVALID");
}
