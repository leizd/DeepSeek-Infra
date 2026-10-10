use deepseek_proof::{
    LEGACY_PLANNING_PROOF_CHECKS, PREDICTIVE_PROOF_CHECKS, validate_check,
    validate_legacy_planning_check,
};
use serde_json::{Value, json};

#[test]
fn rust_replays_unpromoted_planning_without_shadowing_typed_proofs() {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v18/evidence/legacy_planning_evidence_vector.json"
    )))
    .unwrap();
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    let mut names = Vec::new();
    for group in fixture["groups"].as_array().unwrap() {
        let check = group["check_name"].as_str().unwrap();
        names.push(check);
        assert!(!PREDICTIVE_PROOF_CHECKS.contains(&check));
        for case in group["cases"].as_array().unwrap() {
            let mut evidence = if let Some(raw) = case["evidence_json"].as_str() {
                serde_json::from_str(raw).unwrap()
            } else {
                case.get("evidence")
                    .unwrap_or(&group["base_evidence"])
                    .clone()
            };
            for operation in case["operations"].as_array().unwrap() {
                let target = evidence.as_object_mut().unwrap();
                let field = operation["field"].as_str().unwrap();
                match operation["op"].as_str().unwrap() {
                    "replace" => {
                        target.insert(field.to_string(), operation["value"].clone());
                    }
                    "remove" => {
                        target.remove(field).unwrap();
                    }
                    _ => panic!("unknown frozen operation"),
                }
            }
            if evidence.is_object() {
                assert_eq!(
                    json!(validate_legacy_planning_check(check, &evidence)),
                    case["expected_errors"],
                    "{check}/{}",
                    case["name"]
                );
            }
            assert_eq!(
                json!(validate_check(
                    check,
                    &json!({"status":"PASS","evidence":evidence})
                )),
                case["expected_errors"],
                "{check}/{}",
                case["name"]
            );
        }
    }
    assert_eq!(names, LEGACY_PLANNING_PROOF_CHECKS);
    assert_eq!(
        validate_legacy_planning_check("unknown", &json!({})),
        ["unsupported-check:unknown"]
    );
    assert_eq!(
        validate_legacy_planning_check(LEGACY_PLANNING_PROOF_CHECKS[0], &Value::Null),
        ["not-a-dict"]
    );
}
