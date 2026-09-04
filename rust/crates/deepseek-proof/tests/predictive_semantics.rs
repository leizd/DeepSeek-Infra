use deepseek_proof::{
    PREDICTIVE_PROOF_CHECKS, predictive_planning_proof_digest, validate_check,
    validate_predictive_planning_proof,
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    valid_proof: Value,
    invalid_mutations: Vec<InvalidMutation>,
}

#[derive(Debug, Deserialize)]
struct InvalidMutation {
    name: String,
    pointer: String,
    value: Value,
    rebind_proof: bool,
    expected_errors: Vec<String>,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v8/evidence/predictive_planning_proof_vector.json"
    )))
    .unwrap()
}

#[test]
fn predictive_proofs_fail_closed_before_semantic_parity_lands() {
    let errors = validate_predictive_planning_proof(&json!({}));
    assert!(errors.contains(&"predictive-proof-schema-mismatch".to_string()));
    assert!(errors.contains(&"predictive-proof-digest-mismatch".to_string()));
    assert_eq!(PREDICTIVE_PROOF_CHECKS.len(), 24);
}

#[test]
fn rust_replays_the_frozen_python_predictive_proof() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        validate_predictive_planning_proof(&fixture.valid_proof),
        Vec::<String>::new()
    );

    for mutation in fixture.invalid_mutations {
        let mut proof = fixture.valid_proof.clone();
        *proof.pointer_mut(&mutation.pointer).unwrap() = mutation.value;
        if mutation.rebind_proof {
            let digest = predictive_planning_proof_digest(&proof).unwrap();
            proof["proofDigest"] = Value::String(digest);
        }
        assert_eq!(
            validate_predictive_planning_proof(&proof),
            mutation.expected_errors,
            "{}",
            mutation.name
        );
    }
}

#[test]
fn every_promoted_predictive_claim_uses_the_typed_validator() {
    let fixture = fixture();
    for check_name in PREDICTIVE_PROOF_CHECKS {
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

#[test]
fn hostile_integer_extremes_cannot_panic_the_validator() {
    let mut proof = fixture().valid_proof;
    proof["forecastBacktests"][0]["predictedP50FreeBytes"] = json!(i64::MIN);
    proof["forecastBacktests"][0]["predictedP90FreeBytes"] = json!(0);
    proof["forecastBacktests"][0]["actualFreeBytes"] = json!(i64::MAX);
    let digest = predictive_planning_proof_digest(&proof).unwrap();
    proof["proofDigest"] = Value::String(digest);

    assert!(!validate_predictive_planning_proof(&proof).is_empty());
}
