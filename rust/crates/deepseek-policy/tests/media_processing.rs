use deepseek_policy::{
    app_error::AppError,
    entropy::Entropy,
    media_processing::{index_items, prepare_segments, segment_metadata},
};
use serde_json::Value;
use std::cell::Cell;

struct OriginalIds(Cell<u64>);
impl Entropy for OriginalIds {
    fn new_id(&self) -> Result<String, AppError> {
        let n = self.0.get() + 1;
        self.0.set(n);
        Ok(format!("{n:016x}"))
    }
    fn now_millis(&self) -> i64 {
        0
    }
}

#[test]
fn retained_media_segments_match_28_original_processor_cases() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../tests/native/media_segments_reference_v1.json"
    ))
    .unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 28);
    for case in cases {
        let encoded = case["sourceHex"].as_str().unwrap();
        let source: Vec<u8> = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        let result = prepare_segments(&case["media"], &source, false, &OriginalIds(Cell::new(0)));
        if case.get("error").is_some() {
            let error = result.unwrap_err();
            assert_eq!(
                error.code,
                case["error"]["code"].as_str().unwrap(),
                "{}",
                case["name"]
            );
            assert_eq!(
                error.status as u64,
                case["error"]["status"].as_u64().unwrap(),
                "{}",
                case["name"]
            );
            assert_eq!(
                error.message,
                case["error"]["message"].as_str().unwrap(),
                "{}",
                case["name"]
            );
        } else {
            let segments = result.unwrap_or_else(|e| panic!("{}: {e}", case["name"]));
            assert_eq!(
                serde_json::json!(segments),
                case["expected"]["segments"],
                "{}",
                case["name"]
            );
            let indexed = segments
                .iter()
                .filter(|s| !s["text"].as_str().unwrap().trim().is_empty())
                .count();
            assert_eq!(
                segment_metadata(&segments, indexed),
                case["expected"]["metadata"],
                "{}",
                case["name"]
            );
            let index = index_items(&case["media"], &segments, 64, 1_700_000_000_123);
            assert_eq!(
                serde_json::json!(index),
                case["expected"]["indexItems"],
                "index {}",
                case["name"]
            );
        }
    }
}
