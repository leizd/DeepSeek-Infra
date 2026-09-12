use deepseek_transfer::reconcile_multipart_checkpoint;
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v21/transfer/multipart_checkpoint_vector.json"
    )))
    .unwrap()
}

fn decode_chunks(values: &[Value]) -> Vec<Vec<u8>> {
    values
        .iter()
        .map(|value| hex::decode(value.as_str().unwrap()))
        .collect()
}

mod hex {
    pub fn decode(value: &str) -> Vec<u8> {
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).unwrap())
            .collect()
    }
}

#[test]
fn rust_replays_frozen_multipart_checkpoints() {
    let fixture = fixture();
    assert_eq!(
        fixture["source_commit"],
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        fixture["scope"],
        "validator-parity-only-not-provider-execution-evidence"
    );
    let upload_id = fixture["upload_id"].as_str().unwrap();
    let now = fixture["now"].as_str().unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let chunks = decode_chunks(case["source_chunks"].as_array().unwrap());
        let result = reconcile_multipart_checkpoint(
            case["local_parts"].as_array().unwrap(),
            case["remote_parts"].as_array().unwrap(),
            &chunks,
            case["next_offset"].as_i64().unwrap(),
            upload_id,
            now,
            case["upload_missing"].as_bool().unwrap_or(false),
        );
        assert_eq!(result.outcome, case["expected_outcome"], "{}", case["name"]);
        assert_eq!(
            result.error.as_deref(),
            case["expected_error"].as_str(),
            "{}",
            case["name"]
        );
        assert_eq!(
            Value::Object(result.progress),
            case["expected_progress"],
            "{}",
            case["name"]
        );
    }
}
