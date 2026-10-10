use deepseek_transfer::replay_replica_planner_case;
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v29/transfer/replica_planner_vector.json"
    )))
    .unwrap()
}

#[test]
fn rust_replays_frozen_replica_planner() {
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
        let result = replay_replica_planner_case(case);
        assert_eq!(result, case["expected"], "{}", case["name"]);
    }
}
