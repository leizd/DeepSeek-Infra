use deepseek_proof::{
    AUTONOMOUS_STORAGE_BYTES_CHECKS, validate_autonomous_storage_bytes_proof, validate_check,
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    valid_evidence: Value,
    non_object_errors: Vec<String>,
    mutation_cases: Vec<MutationCase>,
}

#[derive(Debug, Deserialize)]
struct MutationCase {
    name: String,
    pointer: String,
    value: Value,
    expected_errors: Vec<String>,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v9/evidence/autonomous_storage_bytes_vector.json"
    )))
    .unwrap()
}

#[test]
fn storage_byte_evidence_requires_the_complete_provider_binding() {
    let errors = validate_autonomous_storage_bytes_proof(&json!({}));
    assert!(errors.contains(&"missing-field:receiptBytesBase64".to_string()));
    assert!(errors.contains(&"missing-field:providerCommitObject".to_string()));
    assert_eq!(AUTONOMOUS_STORAGE_BYTES_CHECKS.len(), 7);
}

#[test]
fn rust_replays_the_frozen_python_storage_byte_evidence() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        validate_autonomous_storage_bytes_proof(&fixture.valid_evidence),
        Vec::<String>::new()
    );
    assert_eq!(
        validate_autonomous_storage_bytes_proof(&json!([])),
        fixture.non_object_errors
    );
    for mutation in fixture.mutation_cases {
        let mut evidence = fixture.valid_evidence.clone();
        *evidence.pointer_mut(&mutation.pointer).unwrap() = mutation.value;
        assert_eq!(
            validate_autonomous_storage_bytes_proof(&evidence),
            mutation.expected_errors,
            "{}",
            mutation.name
        );
    }
}

#[test]
fn every_storage_byte_claim_uses_the_typed_validator() {
    let fixture = fixture();
    for check_name in AUTONOMOUS_STORAGE_BYTES_CHECKS {
        assert_eq!(
            validate_check(
                check_name,
                &json!({"status": "PASS", "evidence": fixture.valid_evidence})
            ),
            Vec::<String>::new(),
            "{check_name}"
        );
    }
}
