use deepseek_proof::{
    CONTROL_STORAGE_EVIDENCE_CHECKS, validate_check, validate_control_storage_evidence_check,
};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
struct Fixture {
    schema_version: u32,
    source_commit: String,
    groups: Vec<Group>,
}

#[derive(Deserialize)]
struct Group {
    check_names: Vec<String>,
    base_evidence: Value,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    evidence: Option<Value>,
    operations: Vec<Operation>,
    expected_errors: Vec<String>,
}

#[derive(Deserialize)]
struct Operation {
    op: String,
    pointer: String,
    value: Value,
}

#[test]
fn rust_replays_all_control_and_storage_evidence_claims() {
    let fixture: Fixture = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v14/evidence/control_storage_evidence_vector.json"
    )))
    .unwrap();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    let observed_names: Vec<_> = fixture
        .groups
        .iter()
        .flat_map(|group| &group.check_names)
        .cloned()
        .collect();
    assert_eq!(observed_names, CONTROL_STORAGE_EVIDENCE_CHECKS);
    for group in fixture.groups {
        for case in group.cases {
            let mut evidence = case.evidence.unwrap_or_else(|| group.base_evidence.clone());
            for operation in case.operations {
                let (parent, field) = operation.pointer.rsplit_once('/').unwrap();
                let target = evidence
                    .pointer_mut(parent)
                    .unwrap()
                    .as_object_mut()
                    .unwrap();
                match operation.op.as_str() {
                    "replace" => {
                        target.insert(field.to_string(), operation.value);
                    }
                    "remove" => {
                        target.remove(field).unwrap();
                    }
                    _ => panic!("unknown frozen operation"),
                }
            }
            for check_name in &group.check_names {
                if evidence.is_object() {
                    assert_eq!(
                        validate_control_storage_evidence_check(check_name, &evidence),
                        case.expected_errors,
                        "{check_name}/{}",
                        case.name
                    );
                }
                assert_eq!(
                    validate_check(check_name, &json!({"status":"PASS","evidence":evidence})),
                    case.expected_errors,
                    "{check_name}/{}",
                    case.name
                );
            }
        }
    }
}

#[test]
fn direct_control_evidence_validation_rejects_unbound_inputs() {
    for name in CONTROL_STORAGE_EVIDENCE_CHECKS {
        assert_eq!(
            validate_control_storage_evidence_check(name, &Value::Null),
            ["not-a-dict"]
        );
    }
    assert_eq!(
        validate_control_storage_evidence_check("unknown", &json!({})),
        ["unsupported-check:unknown"]
    );
}
