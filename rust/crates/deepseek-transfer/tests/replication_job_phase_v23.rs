use deepseek_transfer::replay_replication_phase_case;
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v23/transfer/replication_job_phase_vector.json"
    )))
    .unwrap()
}

#[test]
fn rust_replays_frozen_replication_job_phase_machine() {
    let fixture = fixture();
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture["scope"],
        "validator-parity-only-not-provider-execution-evidence"
    );
    let now = fixture["now"].as_str().unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let result = replay_replication_phase_case(case, now);
        assert_eq!(result, case["expected"], "{}", case["name"]);
    }
}
