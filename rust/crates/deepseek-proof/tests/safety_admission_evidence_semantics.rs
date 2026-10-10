use deepseek_proof::{
    SAFETY_ADMISSION_PROOF_CHECKS, validate_check, validate_safety_admission_check,
};
use serde_json::{Value, json};

#[test]
fn rust_replays_safety_floors_and_atomic_admission_with_exact_numeric_equality() {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v16/evidence/safety_admission_evidence_vector.json"
    )))
    .unwrap();
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    let mut names = Vec::new();
    for group in fixture["groups"].as_array().unwrap() {
        names.extend(
            group["check_names"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap()),
        );
        for case in group["cases"].as_array().unwrap() {
            let mut evidence = if let Some(raw) = case["evidence_json"].as_str() {
                serde_json::from_str(raw).unwrap()
            } else {
                case.get("evidence")
                    .unwrap_or(&group["base_evidence"])
                    .clone()
            };
            for operation in case["operations"].as_array().unwrap() {
                let (parent, field) = operation["pointer"]
                    .as_str()
                    .unwrap()
                    .rsplit_once('/')
                    .unwrap();
                match (
                    operation["op"].as_str().unwrap(),
                    evidence.pointer_mut(parent).unwrap(),
                ) {
                    ("replace", Value::Object(fields)) => {
                        fields.insert(field.to_string(), operation["value"].clone());
                    }
                    ("replace", Value::Array(items)) => {
                        items[field.parse::<usize>().unwrap()] = operation["value"].clone();
                    }
                    ("remove", Value::Object(fields)) => {
                        fields.remove(field).unwrap();
                    }
                    _ => panic!("unknown frozen operation"),
                }
            }
            for check in group["check_names"].as_array().unwrap() {
                let check = check.as_str().unwrap();
                let expected = case["per_check_errors"]
                    .get(check)
                    .unwrap_or(&case["expected_errors"]);
                if evidence.is_object() {
                    assert_eq!(
                        json!(validate_safety_admission_check(check, &evidence)),
                        *expected,
                        "{check}/{}",
                        case["name"]
                    );
                }
                assert_eq!(
                    json!(validate_check(
                        check,
                        &json!({"status":"PASS","evidence":evidence})
                    )),
                    *expected,
                    "{check}/{}",
                    case["name"]
                );
            }
        }
    }
    assert_eq!(names, SAFETY_ADMISSION_PROOF_CHECKS);
    assert_eq!(
        validate_safety_admission_check("unknown", &json!({})),
        ["unsupported-check:unknown"]
    );
    assert_eq!(
        validate_safety_admission_check(SAFETY_ADMISSION_PROOF_CHECKS[0], &Value::Null),
        ["not-a-dict"]
    );
    assert_eq!(
        validate_safety_admission_check(SAFETY_ADMISSION_PROOF_CHECKS[3], &Value::Null),
        ["not-a-dict"]
    );
}
