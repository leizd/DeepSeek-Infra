use deepseek_worker::{AuthorityRequestContext, verify_authority_request_document};
use serde::Deserialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

#[derive(Debug, Deserialize)]
struct Fixture {
    canonical_request: String,
    request_digest: String,
    signer_public_key: String,
    signer_key_id: String,
    now: String,
    cases: Vec<Case>,
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
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap()
}

fn context<'a>(fixture: &'a Fixture, overrides: &'a Value) -> AuthorityRequestContext<'a> {
    let now = overrides
        .get("now")
        .and_then(Value::as_str)
        .unwrap_or(fixture.now.as_str());
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
    AuthorityRequestContext {
        now,
        signer_public_key: fixture.signer_public_key.as_str(),
        signer_key_id: overrides
            .get("signer_key_id")
            .and_then(Value::as_str)
            .unwrap_or(fixture.signer_key_id.as_str()),
        expected_domain: overrides
            .get("expected_domain")
            .and_then(Value::as_str)
            .unwrap_or("action"),
        expected_operation: "install-epoch",
        expected_runtime: overrides
            .get("expected_runtime")
            .and_then(Value::as_str)
            .unwrap_or("go"),
        expected_mode: overrides
            .get("expected_mode")
            .and_then(Value::as_str)
            .unwrap_or("shadow"),
        expected_fleet_id: overrides
            .get("expected_fleet_id")
            .and_then(Value::as_str)
            .unwrap_or("fleet-a"),
        expected_environment: overrides
            .get("expected_environment")
            .and_then(Value::as_str)
            .unwrap_or("test"),
        expected_role: overrides
            .get("expected_role")
            .and_then(Value::as_str)
            .unwrap_or("control-plane"),
        current_fencing_token: overrides
            .get("current_fencing_token")
            .and_then(Value::as_i64)
            .unwrap_or(4),
        live_epoch: overrides
            .get("live_epoch")
            .and_then(Value::as_i64)
            .unwrap_or(3),
        seen_request_ids,
        seen_nonces,
        max_future_skew_seconds: 30,
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
fn rust_accepts_the_frozen_authority_request() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let verified = verify_authority_request_document(
        fixture.canonical_request.as_bytes(),
        &context(&fixture, &empty),
    )
    .unwrap();
    assert_eq!(
        verified.get("digest").and_then(Value::as_str),
        Some(fixture.request_digest.as_str())
    );
    assert_eq!(verified.get("mode").and_then(Value::as_str), Some("shadow"));
}

#[test]
fn rust_rejects_frozen_authority_request_failures() {
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
        let error = verify_authority_request_document(&raw, &context(&fixture, &case.context))
            .expect_err(&case.name);
        assert_eq!(error.code, case.error, "{}", case.name);
    }
}
