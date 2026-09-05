use deepseek_proof::{WAVE_CRASH_PROOF_CHECKS, validate_check, validate_wave_crash_recovery_proof};
use serde_json::{Value, json};

#[test]
fn rust_replays_wave_crash_and_rejects_naive_lease_without_panicking() {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v19/evidence/wave_crash_evidence_vector.json"
    )))
    .unwrap();
    assert_eq!(fixture["check_names"], json!(WAVE_CRASH_PROOF_CHECKS));
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture["scope"],
        "validator-parity-only-not-provider-execution-evidence"
    );
    for case in fixture["cases"].as_array().unwrap() {
        let mut evidence = case
            .get("evidence")
            .unwrap_or(&fixture["base_evidence"])
            .clone();
        for operation in case["operations"].as_array().unwrap() {
            let (parent, field) = operation["pointer"]
                .as_str()
                .unwrap()
                .rsplit_once('/')
                .unwrap();
            let target = evidence.pointer_mut(parent).unwrap();
            match (operation["op"].as_str().unwrap(), target) {
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
        if evidence.is_object() {
            assert_eq!(
                json!(validate_wave_crash_recovery_proof(&evidence)),
                case["expected_errors"],
                "{}",
                case["name"]
            );
        }
        for check_name in WAVE_CRASH_PROOF_CHECKS {
            assert_eq!(
                json!(validate_check(
                    check_name,
                    &json!({"status":"PASS","evidence":evidence})
                )),
                case["expected_errors"],
                "{check_name}/{}",
                case["name"]
            );
        }
    }
}
