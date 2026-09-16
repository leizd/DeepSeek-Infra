use deepseek_worker::{
    StorageOperationCommand, StorageOperationGrantContext, bind_storage_operation_grant,
    verify_storage_operation_grant,
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
        "/../../../compat/native-runtime/v31/control/storage_operation_grant_vector.json"
    )))
    .unwrap()
}

fn context<'a>(fixture: &'a Fixture, overrides: &'a Value) -> StorageOperationGrantContext<'a> {
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
    let mut seen_operation_digests = HashMap::new();
    if let Some(values) = overrides
        .get("seen_operation_digests")
        .and_then(Value::as_object)
    {
        for (key, value) in values {
            seen_operation_digests.insert(key.clone(), value.as_str().unwrap().to_string());
        }
    }
    StorageOperationGrantContext {
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
        expected_operation: overrides
            .get("expected_operation")
            .and_then(Value::as_str)
            .unwrap_or("execute-storage-put"),
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
            .unwrap_or(4),
        seen_request_ids,
        seen_nonces,
        seen_operation_digests,
        max_future_skew_seconds: 30,
    }
}

fn canonical_bytes(value: &Value) -> Vec<u8> {
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
    serde_json::to_vec(&sorted(value)).unwrap()
}

fn frozen_command() -> StorageOperationCommand<'static> {
    StorageOperationCommand {
        action_id: "act-1",
        execution_epoch: 4,
        operation_id: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        mutation_type: "PUT_CHUNK",
        provider: "s3",
        target_identity: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        bucket: "backup-a",
        prefix: "native/",
        object_key: "objects/chunk-1",
        object_digest: "039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81",
        expected_length: 3,
        condition_type: "CREATE_ONLY",
        expected_etag: "",
        claim_revision: 2,
    }
}

#[test]
fn rust_accepts_the_frozen_storage_operation_grant() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let verified = verify_storage_operation_grant(
        fixture.canonical_request.as_bytes(),
        &context(&fixture, &empty),
    )
    .unwrap();
    assert_eq!(
        verified.get("digest").and_then(Value::as_str),
        Some(fixture.request_digest.as_str())
    );
    assert_eq!(
        verified.get("operation").and_then(Value::as_str),
        Some("execute-storage-put")
    );
    bind_storage_operation_grant(&verified, &frozen_command()).unwrap();
}

#[test]
fn rust_rejects_frozen_storage_operation_grant_failures() {
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
            let mut raw = canonical_bytes(&document);
            if case.noncanonical {
                raw = serde_json::to_vec_pretty(&document).unwrap();
            }
            raw
        };
        let error = verify_storage_operation_grant(&raw, &context(&fixture, &case.context))
            .expect_err(&case.name);
        assert_eq!(error.code, case.error, "{}", case.name);
    }
}

#[test]
fn rust_rejects_command_substitution_after_a_valid_grant() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let verified = verify_storage_operation_grant(
        fixture.canonical_request.as_bytes(),
        &context(&fixture, &empty),
    )
    .unwrap();
    let mut command = frozen_command();
    command.object_key = "objects/other";
    assert_eq!(
        bind_storage_operation_grant(&verified, &command)
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_COMMAND_MISMATCH"
    );
}

#[test]
fn worker_cached_grant_cannot_outlive_installed_epoch() {
    let fixture = fixture();
    let mut worker = deepseek_worker::Worker::new();
    worker
        .configure_authority(deepseek_worker::WorkerAuthorityConfig {
            signer_public_key: fixture.signer_public_key,
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 4,
            now: Some(fixture.now),
        })
        .unwrap();
    let fence = deepseek_protocol::ActionFence {
        action_id: "act-1".into(),
        execution_epoch: 4,
    };
    worker.install_authoritative_epoch(&fence).unwrap();
    worker
        .admit_storage_operation_grant(fixture.canonical_request.as_bytes(), &frozen_command())
        .unwrap();
    // Exact retries remain idempotent only while live authorization still holds.
    worker
        .admit_storage_operation_grant(fixture.canonical_request.as_bytes(), &frozen_command())
        .unwrap();
    worker
        .install_authoritative_epoch(&deepseek_protocol::ActionFence {
            execution_epoch: 5,
            ..fence
        })
        .unwrap();
    assert_eq!(
        worker
            .admit_storage_operation_grant(fixture.canonical_request.as_bytes(), &frozen_command())
            .unwrap_err()
            .code,
        "FENCE_MISMATCH"
    );
}

// Published RFC 8032 test key, never a deployed credential. Re-sign invalid
// payloads so the test exercises type validation, not a broken signature/digest.
fn resign_fixture(document: &mut Value) {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use ed25519_dalek::{Signer as _, SigningKey};
    let seed_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
    let mut seed = [0_u8; 32];
    for (index, pair) in seed_hex.as_bytes().chunks_exact(2).enumerate() {
        seed[index] = u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap();
    }
    document.as_object_mut().unwrap().remove("signature");
    document.as_object_mut().unwrap().remove("digest");
    document["payloadDigest"] = Value::String(format!(
        "sha256:{:x}",
        Sha256::digest(canonical_bytes(&document["payload"]))
    ));
    document["digest"] = Value::String(format!(
        "sha256:{:x}",
        Sha256::digest(canonical_bytes(document))
    ));
    let mut message = b"deepseek-infra:control-storage-operation-grant-v1\x00".to_vec();
    message.extend(canonical_bytes(document));
    document["signature"] = Value::String(
        URL_SAFE_NO_PAD.encode(SigningKey::from_bytes(&seed).sign(&message).to_bytes()),
    );
}

#[test]
fn rust_rejects_signed_non_string_storage_scope() {
    let fixture = fixture();
    let empty = Value::Object(Map::new());
    let mut original: Value = serde_json::from_str(&fixture.canonical_request).unwrap();
    resign_fixture(&mut original);
    assert_eq!(
        canonical_bytes(&original),
        fixture.canonical_request.as_bytes()
    );
    for (field, value) in [("prefix", Value::from(42)), ("expectedEtag", Value::Null)] {
        let mut document = original.clone();
        document["payload"][field] = value;
        resign_fixture(&mut document);
        assert_eq!(
            verify_storage_operation_grant(&canonical_bytes(&document), &context(&fixture, &empty))
                .unwrap_err()
                .code,
            "STORAGE_OPERATION_GRANT_INVALID"
        );
    }
}
