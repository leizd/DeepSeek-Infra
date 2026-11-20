use super::super::{
    Records, RetainedRecords,
    retained::{Document, MediaRecord},
};
use serde_json::{Value, json};

fn record(fields: &str) -> MediaRecord {
    let bytes = format!(
        r#"{{"mediaId":"kept","type":"image","path":"x.png","createdAt":"fixed",{fields}}}"#
    );
    Document::decode(bytes.as_bytes())
        .unwrap()
        .normalize_media()
        .unwrap()
}

#[test]
fn retained_original_http_schema_and_strict_encoding_match_all_thirty_four_cases() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_retained_http_v1.json"
    ))
    .unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 34);
    for case in cases {
        let raw = Records::from_raw_reader(case["document"].as_str().unwrap().as_bytes()).unwrap();
        let mut records = Vec::new();
        raw.visit_retained(|value| {
            if let Ok(value) = value.normalize_media() {
                records.push(value);
            }
            Ok::<_, ()>(())
        })
        .unwrap();
        assert_eq!(records.len(), 2, "{}", case["name"]);
        assert!(records[0].field_equals("mediaId", "media_control"));
        let kept = records[1].normalize_again().unwrap();
        let mut bytes = Vec::new();
        let encoded = kept.write_response(&mut bytes);
        assert_eq!(
            encoded.is_ok(),
            case["results"]["detail"]["status"] == 200,
            "{}",
            case["name"]
        );
        if encoded.is_ok() {
            // Integer text is checked independently below; these parsed Values
            // additionally cover every title, source/metadata and scalar type.
            let actual: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(
                actual, case["results"]["detail"]["body"]["media"],
                "{}",
                case["name"]
            );
        }
    }
}

#[test]
fn retained_integer_tokens_survive_without_workspace_feature_unification() {
    let value = record(
        r#""metadata":{"x":18446744073709551617,"y":1234567890123456789012345678901234567890}"#,
    );
    let mut bytes = Vec::new();
    value.write_response(&mut bytes).unwrap();
    let text = String::from_utf8(bytes).unwrap();
    assert!(text.contains("18446744073709551617"));
    assert!(text.contains("1234567890123456789012345678901234567890"));
    assert!(!text.contains("e+"));
}

#[test]
fn retained_floats_parse_before_stringification_and_preserve_negative_zero() {
    let value = record(
        r#""source":{"x":[NaN,Infinity,-Infinity,1e-4000,-1e-4000,1e400,1.0,-0]},"title":NaN"#,
    );
    let actual = value.test_value();
    assert_eq!(actual["title"], "nan");
    assert_eq!(
        actual["source"]["x"],
        json!(["nan", "inf", "-inf", "0.0", "-0.0", "inf", "1.0", "0"])
    );
    let rounded =
        record(r#""title":112171935477118.125,"updatedAt":1.00000762939453125"#).test_value();
    assert_eq!(rounded["title"], "112171935477118.12");
    assert_eq!(rounded["updatedAt"], "1.0000076293945312");
    let long_integer_mantissa = format!("1{}e-655360", "0".repeat(655_360));
    let long_fractional_mantissa = format!("0.{}1e655360", "0".repeat(655_359));
    let balanced = record(&format!(
        r#""source":{{"x":[{long_integer_mantissa},{long_fractional_mantissa}]}}"#
    ));
    assert_eq!(balanced.test_value()["source"]["x"], json!(["1.0", "1.0"]));
}

#[test]
fn retained_source_and_metadata_container_stringification_preserves_repr_order() {
    let value = record(
        r#""metadata":{"x":[["\ud800"],[{"z":1,"a":2}]]},"source":{"x":[NaN,["\ud800"],{"z":1,"a":2},null]}"#,
    );
    let actual = value.test_value();
    assert_eq!(
        actual["metadata"]["x"],
        json!(["['\\ud800']", "[{'z': 1, 'a': 2}]"])
    );
    assert_eq!(
        actual["source"]["x"],
        json!(["nan", "['\\ud800']", "{'z': 1, 'a': 2}"])
    );
}

#[test]
fn retained_repr_uses_the_cloud_oracles_unicode_15_printability() {
    let value = record(r#""source":{"x":[["\ud83e\udee9","\u00a0","😀","\u200d"]]}"#);
    assert_eq!(
        value.test_value()["source"]["x"],
        json!(["['\\U0001fae9', '\\xa0', '😀', '\\u200d']"])
    );
    let mime = record(r#""mimeType":"IMAGE/AΣ AΣB İ \u1c89""#).test_value();
    assert_eq!(mime["mimeType"], "image/aς aσb i\u{307} \u{1c89}");
}

#[test]
fn retained_surrogates_only_fail_at_the_selected_strict_output_boundary() {
    for fields in [
        r#""title":"\ud800""#,
        r#""path":"\ud800.png""#,
        r#""updatedAt":"\udfff""#,
        r#""metadata":{"x":NaN}"#,
        r#""source":{"x":"\ud800"}"#,
    ] {
        let value = record(fields).normalize_again().unwrap();
        let mut spool = RetainedRecords::empty().unwrap();
        spool.append(&value).unwrap();
        let restored = spool.get(0).unwrap().unwrap();
        assert!(restored.write_response(&mut Vec::new()).is_err());
        assert_eq!(restored.id(), "kept");
        assert_eq!(restored.sort_key(), value.sort_key());
    }
}

#[test]
fn retained_sort_keys_follow_python_scalars_with_surrogate_pairs_and_stable_equals() {
    let non_bmp = record(r#""updatedAt":"\ud800\udc00""#);
    let private_bmp = record(r#""updatedAt":"\ue000""#);
    let unpaired = record(r#""updatedAt":"\ud800""#);
    assert!(non_bmp.sort_key() > private_bmp.sort_key());
    assert!(private_bmp.sort_key() > unpaired.sort_key());
    assert_eq!(non_bmp.sort_key(), record("\"updatedAt\":\"𐀀\"").sort_key());
}

#[test]
fn retained_normalization_keeps_business_caps_without_cutting_a_surrogate_pair() {
    let long = format!("{}\\ud800", "λ".repeat(120_000));
    let value = record(&format!(
        r#""metadata":{{"x":"{long}"}},"source":{{"x":{}}}"#,
        serde_json::to_string(&"😀".repeat(2_001)).unwrap()
    ));
    let actual = value.test_value();
    assert_eq!(
        actual["metadata"]["x"].as_str().unwrap().chars().count(),
        120_000
    );
    assert_eq!(
        actual["source"]["x"].as_str().unwrap().chars().count(),
        2_000
    );
}

#[test]
fn retained_sanitized_keys_duplicates_and_empty_children_follow_original_order() {
    let value = record(
        r#""metadata":{"a!":1,"a?":2,"a:":{},"a.":[],"!":{"x":NaN},"\ud800":NaN},"source":{"x":[null],"y":false}"#,
    );
    let actual = value.test_value();
    assert_eq!(actual["metadata"], json!({"a":2,"a.":[]}));
    assert_eq!(actual["source"], json!({"y":false}));
}

#[test]
fn retained_document_validates_tail_and_utf8_and_uses_dictionary_winners() {
    for invalid in [
        b"{\"x\":1} false".as_slice(),
        b"[1,]",
        b"{\"x\":01}",
        b"\"\xff\"",
    ] {
        assert!(Document::decode(invalid).is_err());
    }
    let value = record(
        r#""title":"\ud800","title":"ok","metadata":{"x":NaN,"x":1,"\ud83d\ude00":1,"😀":2}"#,
    );
    assert_eq!(value.test_value()["title"], "ok");
    assert_eq!(value.test_value()["metadata"], json!({"x":1}));
}

#[test]
fn retained_clock_and_second_path_validation_keep_original_phase_order() {
    let input = Document::decode(br#"{"mediaId":"kept","type":"image","path":"../bad"}"#).unwrap();
    let mut calls = 0;
    assert!(
        super::super::retained_schema::normalize_with_clock(&input, || {
            calls += 1;
            "stamp".to_string()
        })
        .is_err()
    );
    assert_eq!(calls, 1);
    let first = record(r#""path":"./C:/x.png""#);
    assert_eq!(first.test_value()["path"], "C:/x.png");
    assert_eq!(first.normalize_again().unwrap_err().status, 400);
}

#[test]
fn retained_deep_normalization_and_drop_use_heap_frames() {
    let depth = 2_048;
    let text = format!(
        r#"{{"mediaId":"kept","type":"image","createdAt":"fixed","metadata":{}1{}}}"#,
        "{\"x\":".repeat(depth),
        "}".repeat(depth)
    );
    let value = Document::decode(text.as_bytes())
        .unwrap()
        .normalize_media()
        .unwrap();
    let mut output = Vec::new();
    value.write_response(&mut output).unwrap();
    assert!(output.len() > depth * 6);
    drop(value);
}
