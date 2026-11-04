use super::*;
use crate::media_records::{SourceReadError, VisitError};
use std::convert::Infallible;

const ROW: &str = r#"{"mediaId":"kept","type":"image","title":"original"}"#;

fn rows(reader: impl Read) -> Result<Vec<Value>, SourceReadError> {
    let records = Records::from_raw_reader(reader)?;
    let mut result = Vec::new();
    records
        .visit_source(|value| {
            result.push(value);
            Ok::<_, Infallible>(())
        })
        .unwrap();
    Ok(result)
}

fn legacy(bytes: &[u8]) -> Vec<Value> {
    let value: Value = serde_json::from_slice(bytes).unwrap_or(Value::Null);
    value
        .get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|value| value.is_object())
        .cloned()
        .collect()
}

#[test]
fn escaped_names_and_complete_last_key_match_the_full_document() {
    for text in [
        format!(r#"{{"med\u0069a":[{}]}}"#, ROW),
        format!(
            r#"{{"media":[{}],"media":false,"\u006dedia":[{}]}}"#,
            ROW, ROW
        ),
        format!(r#"{{"media":[{}],"media":null}}"#, ROW),
        format!(
            r#"{{"media":[null,5,1.25,[],false,{{}},{}],"ignored":{{"x":[1,true]}}}}"#,
            ROW
        ),
        format!(r#"{{"media":[{}]}} garbage"#, ROW),
    ] {
        assert_eq!(rows(text.as_bytes()).unwrap(), legacy(text.as_bytes()));
    }
}

fn unicode_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_discarded_unicode_v1.json"
    ))
    .unwrap()
}

#[test]
fn discarded_string_escapes_are_fully_validated_without_becoming_values() {
    let fixture = unicode_fixture();
    let mut checked = 0;
    for case in fixture["cases"].as_array().unwrap() {
        if case["kind"] != "ignored_value" {
            continue;
        }
        checked += 1;
        let text = case["document"].as_str().unwrap();
        assert_eq!(
            Value::Array(rows(text.as_bytes()).unwrap()),
            case["rawRows"],
            "{}",
            case["name"]
        );
    }
    assert_eq!(checked, 9);
}

#[test]
fn discarded_numeric_domain_uses_the_callers_existing_json_features() {
    for number in [
        "0",
        "-0",
        "18446744073709551616",
        "1e400",
        "-1e400",
        "0e9999999999999999999",
        "1e-99999999999999999999",
        "1.7976931348623157e308",
        "1.7976931348623159e308",
        "01",
        "-01",
        "1e+",
        "1.",
        "+1",
        "NaN",
        "Infinity",
    ] {
        let text = format!(r#"{{"ignored":{},"media":[{}]}}"#, number, ROW);
        assert_eq!(
            rows(text.as_bytes()).unwrap(),
            legacy(text.as_bytes()),
            "{number}"
        );
    }
}

#[test]
fn full_document_depth_is_preserved_for_discarded_and_retained_values() {
    for depth in 0..134 {
        let nested = format!("{}0{}", "[".repeat(depth), "]".repeat(depth));
        for text in [
            format!(r#"{{"ignored":{},"media":[{}]}}"#, nested, ROW),
            format!(
                r#"{{"media":[{{"mediaId":"kept","metadata":{{"nested":{}}}}}]}}"#,
                nested
            ),
        ] {
            assert_eq!(
                rows(text.as_bytes()).unwrap(),
                legacy(text.as_bytes()),
                "depth {depth}"
            );
        }
    }
}

#[test]
fn enormous_ignored_keys_and_values_are_consumed_from_a_generated_reader() {
    let input = io::Cursor::new(b"{\"")
        .chain(io::repeat(b'k').take(2 * 1024 * 1024))
        .chain(io::Cursor::new(b"\":\""))
        .chain(io::repeat(b'v').take(2 * 1024 * 1024))
        .chain(io::Cursor::new(
            format!(r#"","media":[{}]}}"#, ROW).into_bytes(),
        ));
    let result = rows(input).unwrap();
    assert_eq!(result, vec![serde_json::from_str::<Value>(ROW).unwrap()]);
}

struct Broken;
impl Read for Broken {
    fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
        Err(io::Error::other("original source failed"))
    }
}

#[test]
fn early_syntax_does_not_hide_utf8_or_a_later_original_source_failure() {
    assert!(matches!(
        rows(&b"!\xff"[..]),
        Err(SourceReadError::InvalidUtf8)
    ));
    assert!(matches!(
        rows(io::Cursor::new(b"!\xff").chain(Broken)),
        Err(SourceReadError::SourceIo)
    ));
    assert!(matches!(
        rows(io::Cursor::new(b"!").chain(Broken)),
        Err(SourceReadError::SourceIo)
    ));
    let incomplete =
        io::Cursor::new(format!(r#"{{"media":[{}]}}"#, ROW).into_bytes()).chain(Broken);
    assert!(matches!(rows(incomplete), Err(SourceReadError::SourceIo)));
    let mut state = Utf8::default();
    for byte in "λ🦀".as_bytes() {
        state.accept(&[*byte]);
    }
    assert!(!state.invalid && state.length == 0);
}

#[test]
fn raw_mode_preserves_per_row_skip_then_propagate_and_does_not_return_early() {
    let text = format!(
        r#"{{"media":[{},{{"mediaId":"later","type":"image","path":"./C:/x.png"}}]}}"#,
        ROW
    );
    let records = Records::from_raw_reader(text.as_bytes()).unwrap();
    let mut selected = None;
    let result = records.visit_source::<crate::app_error::AppError>(|value| {
        let Ok(stored) = crate::media_schema::normalize_media_record(&value) else {
            return Ok(());
        };
        let public = crate::media_schema::normalize_media_record(&stored)?;
        if selected.is_none() {
            selected = Some(public);
        }
        Ok(())
    });
    assert!(selected.is_some());
    assert!(matches!(result, Err(VisitError::Visitor(error)) if error.status == 400));
}

#[test]
fn valid_and_invalid_token_combinations_match_full_value_parsing() {
    let values = [
        "null",
        "true",
        "false",
        "12",
        "-3.5e2",
        "[]",
        "{}",
        r#""λ""#,
        r#"{"nested":[1,{"name":"value"}]}"#,
        "[1,]",
        r#"{"name":0,}"#,
    ];
    for value in values {
        for tail in ["", " ", "\n\r\t", "[]", "false", ","] {
            let text = format!(r#"{{"ignored":{},"media":[{}]}}{}"#, value, ROW, tail);
            assert_eq!(rows(text.as_bytes()).unwrap(), legacy(text.as_bytes()));
        }
    }
}

struct InterruptedOnce<R> {
    source: R,
    calls: usize,
    at: usize,
}
impl<R: Read> Read for InterruptedOnce<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let call = self.calls;
        self.calls += 1;
        if call == self.at {
            return Err(io::ErrorKind::Interrupted.into());
        }
        let size = bytes.len().min(5);
        self.source.read(&mut bytes[..size])
    }
}

#[test]
fn interrupted_reads_before_and_inside_a_record_are_retried_without_poisoning() {
    let text = format!(r#"{{"media":[{}]}}"#, ROW);
    for at in [0, 3, 7] {
        let source = InterruptedOnce {
            source: io::Cursor::new(text.as_bytes()),
            calls: 0,
            at,
        };
        assert_eq!(rows(source).unwrap(), legacy(text.as_bytes()));
        let source = InterruptedOnce {
            source: io::Cursor::new(text.as_bytes()),
            calls: 0,
            at,
        };
        assert_eq!(Records::from_reader(source).unwrap().len().unwrap(), 1);
    }
}

#[test]
fn first_key_number_and_raw_value_wrappers_match_the_existing_value_decoder() {
    let number = "$serde_json::private::Number";
    let raw = "$serde_json::private::RawValue";
    let encoded_document = serde_json::to_string(&format!(r#"{{"media":[{}]}}"#, ROW)).unwrap();
    let cases = [
        format!(r#"{{"ignored":{{"{}":0}},"media":[{}]}}"#, number, ROW),
        format!(
            r#"{{"ignored":{{"{}":"1e400"}},"media":[{}]}}"#,
            number, ROW
        ),
        format!(
            r#"{{"ignored":{{"{}":"not a number"}},"media":[{}]}}"#,
            number, ROW
        ),
        format!(
            r#"{{"ignored":{{"first":0,"{}":0}},"media":[{}]}}"#,
            number, ROW
        ),
        format!(r#"{{"{}":"1","media":[{}]}}"#, number, ROW),
        format!(
            r#"{{"ignored":{{"\u0024serde_json::private::Number":0}},"media":[{}]}}"#,
            ROW
        ),
        format!(
            r#"{{"ignored":[{{"{}":false}}],"media":[{}]}}"#,
            number, ROW
        ),
        format!(r#"{{"{}":{}}}"#, raw, encoded_document),
        format!(r#"{{"{}":{},"media":[{}]}}"#, raw, encoded_document, ROW),
        format!(r#"{{"ignored":{{"{}":"[1,]"}},"media":[{}]}}"#, raw, ROW),
        format!(r#"{{"ignored":{{"{}":"[1,2]"}},"media":[{}]}}"#, raw, ROW),
        format!(r#"{{"media":[{{"{}":"0"}},{}]}}"#, number, ROW),
    ];
    for text in cases {
        assert_eq!(
            rows(text.as_bytes()).unwrap(),
            legacy(text.as_bytes()),
            "{text}"
        );
    }
    for value in [
        r#"{"$serde_json::private::Number":"0"}"#,
        r#"{"$serde_json::private::RawValue":"0"}"#,
    ] {
        let text = format!(r#"{{"media":[{}]}}"#, value);
        let mut old_calls = 0;
        let old = Records::parse_mode(
            text.as_bytes(),
            Records::empty().unwrap(),
            Records::empty().unwrap(),
            |value| {
                old_calls += 1;
                crate::media_schema::normalize_media_record(value)
            },
            false,
            false,
        )
        .unwrap();
        let mut calls = 0;
        let actual = Records::parse(
            text.as_bytes(),
            Records::empty().unwrap(),
            Records::empty().unwrap(),
            |value| {
                calls += 1;
                crate::media_schema::normalize_media_record(value)
            },
        )
        .unwrap();
        assert_eq!(calls, old_calls, "{text}");
        assert_eq!(actual.len().unwrap(), old.len().unwrap(), "{text}");
    }
}

#[test]
fn wrapped_media_arrays_and_inner_depth_match_full_value_decoding() {
    let inner = format!(r#"[{}]"#, ROW);
    let wrapped = |text: &str| {
        format!(
            r#"{{"$serde_json::private::RawValue":{}}}"#,
            serde_json::to_string(text).unwrap()
        )
    };
    for value in [
        wrapped(&inner),
        wrapped(&wrapped(&inner)),
        wrapped("null"),
        wrapped("[1,]"),
        wrapped(r#"{"wrong":[]}"#),
    ] {
        let text = format!(r#"{{"media":{}}}"#, value);
        assert_eq!(
            rows(text.as_bytes()).unwrap(),
            legacy(text.as_bytes()),
            "{text}"
        );
    }
    for depth in [124, 125, 126, 127, 128] {
        let inner = format!("{}0{}", "[".repeat(depth), "]".repeat(depth));
        let text = format!(r#"{{"media":{}}}"#, wrapped(&inner));
        assert_eq!(
            rows(text.as_bytes()).unwrap(),
            legacy(text.as_bytes()),
            "depth={depth}"
        );
    }
}

#[test]
fn discarded_surrogate_escapes_match_original_python_media_reader() {
    let fixture = unicode_fixture();
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 21);
    for case in cases {
        let text = case["document"].as_str().unwrap();
        assert_eq!(
            Value::Array(rows(text.as_bytes()).unwrap()),
            case["rawRows"],
            "gateway {}",
            case["name"]
        );
        let actual = Records::from_reader(text.as_bytes()).unwrap();
        let mut normalized = Vec::new();
        actual
            .visit_source(|value| {
                normalized.push(value);
                Ok::<_, Infallible>(())
            })
            .unwrap();
        assert_eq!(
            Value::Array(normalized),
            case["storedRows"],
            "worker {}",
            case["name"]
        );
        // Recovery consumes the generated normalized image, never discarded source strings.
        let mut image = br#"{"media":"#.to_vec();
        actual.write_retained_array(&mut image).unwrap();
        image.extend_from_slice(br#","nativePublication":{"phase":"PREPARED"}}"#);
        let recovered = Records::from_image(image.as_slice()).unwrap();
        assert_eq!(
            recovered.len().unwrap(),
            actual.len().unwrap(),
            "recovery {}",
            case["name"]
        );
        for index in 0..actual.len().unwrap() {
            assert_eq!(recovered.get(index).unwrap(), actual.get(index).unwrap());
        }
    }
}
