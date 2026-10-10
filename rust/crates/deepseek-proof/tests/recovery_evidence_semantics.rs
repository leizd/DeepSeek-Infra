use deepseek_proof::{
    RECOVERY_EVIDENCE_CHECKS, parse_evidence_proof_document, validate_backup_commit_proof,
    validate_check, validate_distinct_pid_proof, validate_epoch_increase_proof,
    validate_minio_endpoints_proof, validate_pass_with_schema_only, validate_restore_proof,
    validate_sigkill_proof,
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    scope: String,
    groups: Vec<Group>,
}

#[derive(Debug, Deserialize)]
struct Group {
    validator: String,
    check_names: Vec<String>,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    evidence: Value,
    expected_errors: Vec<String>,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v13/evidence/recovery_evidence_vector.json"
    )))
    .unwrap()
}

fn validate(validator: &str, evidence: &Value) -> Vec<String> {
    match validator {
        "restore" => validate_restore_proof(evidence),
        "backup_commit" => validate_backup_commit_proof(evidence),
        "distinct_pid" => validate_distinct_pid_proof(evidence),
        "sigkill" => validate_sigkill_proof(evidence),
        "epoch_increase" => validate_epoch_increase_proof(evidence),
        "minio_endpoints" => validate_minio_endpoints_proof(evidence),
        "schema_only" => validate_pass_with_schema_only(evidence),
        _ => panic!("unknown frozen validator: {validator}"),
    }
}

#[test]
fn rust_replays_the_frozen_python_recovery_evidence_validators() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture.scope,
        "validator-parity-only-not-provider-execution-evidence"
    );

    for group in fixture.groups {
        for case in group.cases {
            assert_eq!(
                validate(&group.validator, &case.evidence),
                case.expected_errors,
                "{}/{}",
                group.validator,
                case.name
            );
        }
    }
}

#[test]
fn every_recovery_evidence_claim_uses_its_frozen_validator() {
    let fixture = fixture();
    let flattened = fixture
        .groups
        .iter()
        .flat_map(|group| group.check_names.iter().cloned())
        .collect::<Vec<_>>();
    assert_eq!(
        flattened,
        RECOVERY_EVIDENCE_CHECKS
            .iter()
            .map(|name| (*name).to_string())
            .collect::<Vec<_>>()
    );

    for group in fixture.groups {
        let valid_evidence = group
            .cases
            .iter()
            .find(|case| case.expected_errors.is_empty())
            .unwrap()
            .evidence
            .clone();
        for check_name in group.check_names {
            assert_eq!(
                validate_check(
                    &check_name,
                    &json!({"status": "PASS", "evidence": valid_evidence})
                ),
                Vec::<String>::new(),
                "{check_name}"
            );
            for case in &group.cases {
                let expected = if case.evidence.is_object() {
                    case.expected_errors.clone()
                } else {
                    vec!["evidence-must-be-object".to_string()]
                };
                assert_eq!(
                    validate_check(
                        &check_name,
                        &json!({"status": "PASS", "evidence": case.evidence})
                    ),
                    expected,
                    "{check_name}/{}",
                    case.name
                );
            }
        }
    }
}

#[test]
fn parsed_documents_preserve_python_numeric_and_text_semantics() {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v13/evidence/recovery_coercions_vector.json"
    )))
    .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let envelope = parse_evidence_proof_document(
            case["document"].as_str().unwrap().as_bytes(),
            Some("native-recovery-coercion-parity"),
        )
        .unwrap();
        let check_name = case["check_name"].as_str().unwrap();
        assert_eq!(
            serde_json::to_value(validate_check(check_name, &envelope.checks[check_name])).unwrap(),
            case["expected_errors"],
            "{}",
            case["name"]
        );
    }
}
