use deepseek_proof::{
    FEDERATION_TRUST_PROOF_CHECKS, federation_trust_proof_digest, validate_check,
    validate_federation_trust_proof,
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    check_names: Vec<String>,
    valid_proof: Value,
    non_object_errors: Vec<String>,
    empty_object_errors: Vec<String>,
    mutation_cases: Vec<MutationCase>,
}

#[derive(Debug, Deserialize)]
struct MutationCase {
    name: String,
    operations: Vec<MutationOperation>,
    rebind_proof: bool,
    expected_errors: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct MutationOperation {
    op: String,
    pointer: String,
    value: Value,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v12/evidence/federation_trust_proof_vector.json"
    )))
    .unwrap()
}

fn apply_operation(value: &mut Value, operation: &MutationOperation) {
    let (parent_pointer, leaf) = operation.pointer.rsplit_once('/').unwrap();
    let parent = if parent_pointer.is_empty() {
        value
    } else {
        value.pointer_mut(parent_pointer).unwrap()
    };
    match (operation.op.as_str(), parent) {
        ("add" | "replace", Value::Object(fields)) => {
            fields.insert(leaf.to_string(), operation.value.clone());
        }
        ("add" | "replace", Value::Array(items)) => {
            items[leaf.parse::<usize>().unwrap()] = operation.value.clone();
        }
        ("remove", Value::Object(fields)) => {
            fields.remove(leaf).unwrap();
        }
        ("remove", Value::Array(items)) => {
            items.remove(leaf.parse::<usize>().unwrap());
        }
        _ => panic!("unsupported frozen mutation operation"),
    }
}

#[test]
fn federation_trust_proofs_fail_closed_on_empty_payloads() {
    assert!(!validate_federation_trust_proof(&json!({})).is_empty());
    assert_eq!(FEDERATION_TRUST_PROOF_CHECKS.len(), 15);
}

#[test]
fn rust_replays_the_frozen_python_federation_trust_proof() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture.check_names,
        FEDERATION_TRUST_PROOF_CHECKS
            .iter()
            .map(|name| (*name).to_string())
            .collect::<Vec<_>>()
    );
    assert_eq!(
        validate_federation_trust_proof(&fixture.valid_proof),
        Vec::<String>::new()
    );
    assert_eq!(
        validate_federation_trust_proof(&json!([])),
        fixture.non_object_errors
    );
    assert_eq!(
        validate_federation_trust_proof(&json!({})),
        fixture.empty_object_errors
    );
    for mutation in fixture.mutation_cases {
        let mut proof = fixture.valid_proof.clone();
        for operation in &mutation.operations {
            apply_operation(&mut proof, operation);
        }
        if mutation.rebind_proof {
            proof["proofDigest"] = Value::String(federation_trust_proof_digest(&proof).unwrap());
        }
        assert_eq!(
            validate_federation_trust_proof(&proof),
            mutation.expected_errors,
            "{}",
            mutation.name
        );
    }
}

#[test]
fn every_federation_trust_claim_uses_the_typed_validator() {
    let fixture = fixture();
    for check_name in FEDERATION_TRUST_PROOF_CHECKS {
        assert_eq!(
            validate_check(
                check_name,
                &json!({"status": "PASS", "evidence": fixture.valid_proof})
            ),
            Vec::<String>::new(),
            "{check_name}"
        );
    }
}
