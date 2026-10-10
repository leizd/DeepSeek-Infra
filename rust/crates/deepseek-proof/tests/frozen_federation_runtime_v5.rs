use deepseek_proof::{
    FEDERATION_RUNTIME_PROOF_CHECKS, federation_runtime_proof_digest, validate_check,
    validate_federation_runtime_proof,
};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    valid_proof: Value,
    invalid_cases: Vec<InvalidCase>,
}

#[derive(Debug, Deserialize)]
struct InvalidCase {
    path: String,
    replacement: Value,
    expected_errors: Vec<String>,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v5/evidence/federation_runtime_proof_vector.json"
    )))
    .unwrap()
}

#[test]
fn rust_replays_the_python_4_8_0_federation_runtime_proof() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert!(validate_federation_runtime_proof(&fixture.valid_proof).is_empty());
    assert_eq!(
        federation_runtime_proof_digest(&fixture.valid_proof).unwrap(),
        fixture.valid_proof["proofDigest"]
    );
    for check_name in FEDERATION_RUNTIME_PROOF_CHECKS {
        assert!(
            validate_check(
                check_name,
                &serde_json::json!({"status": "PASS", "evidence": &fixture.valid_proof}),
            )
            .is_empty()
        );
    }
}

#[test]
fn runtime_proof_tampering_fails_with_the_frozen_errors() {
    let fixture = fixture();
    for invalid in fixture.invalid_cases {
        let mut proof = fixture.valid_proof.clone();
        *proof.pointer_mut(&invalid.path).unwrap() = invalid.replacement;
        assert_eq!(
            validate_federation_runtime_proof(&proof),
            invalid.expected_errors,
            "unexpected errors for {}",
            invalid.path
        );
    }
}
