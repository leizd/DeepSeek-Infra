use deepseek_transfer::replay_transfer_qos_case;
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v28/transfer/transfer_qos_vector.json"
    )))
    .unwrap()
}

#[test]
fn rust_replays_frozen_transfer_qos() {
    let fixture = fixture();
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture["scope"],
        "validator-parity-only-not-provider-execution-evidence"
    );
    for case in fixture["cases"].as_array().unwrap() {
        let result = replay_transfer_qos_case(case);
        assert_eq!(result, case["expected"], "{}", case["name"]);
    }
}
