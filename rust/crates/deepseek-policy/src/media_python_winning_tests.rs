// Actual original reader goldens; no native output in the oracle.
fn winning_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_winning_fields_v1.json"
    ))
    .unwrap()
}
fn original_winning_case(name: &str) {
    let fixture = winning_fixture();
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 10);
    assert_python_original_case(cases.iter().find(|case| case["name"] == name).unwrap());
}

#[test]
fn original_winning_last_title_replaces_surrogate() {
    original_winning_case("last_title_replaces_surrogate");
}

#[test]
fn original_winning_last_metadata_replaces_nonfinite() {
    original_winning_case("last_metadata_replaces_nonfinite");
}

#[test]
fn original_winning_last_metadata_key_replaces_surrogate() {
    original_winning_case("last_metadata_key_replaces_surrogate");
}

#[test]
fn original_winning_mime_raw_path_0() {
    original_winning_case("mime_raw_path_0");
}

#[test]
fn original_winning_mime_raw_path_1() {
    original_winning_case("mime_raw_path_1");
}

#[test]
fn original_winning_mime_raw_path_2() {
    original_winning_case("mime_raw_path_2");
}

#[test]
fn original_winning_mime_raw_path_3() {
    original_winning_case("mime_raw_path_3");
}

#[test]
fn original_winning_last_media_replaces_first_nan() {
    original_winning_case("last_media_replaces_first_nan");
}

#[test]
fn original_winning_last_media_replaces_first_surrogate() {
    original_winning_case("last_media_replaces_first_surrogate");
}

#[test]
fn original_winning_last_media_replaces_first_deep() {
    original_winning_case("last_media_replaces_first_deep");
}

#[test]
fn original_winning_clock_before_invalid_path() {
    use std::cell::Cell;
    let fixture = winning_fixture();
    let case = &fixture["clock"];
    let text = case["document"].as_str().unwrap();
    let count = Cell::new(0_usize);
    let mut clock = || {
        let ordinal = count.get();
        count.set(ordinal + 1);
        ["clock-first", "clock-second", "clock-third"][ordinal].to_owned()
    };
    let records = Records::parse(
        text.as_bytes(),
        RawRecords::empty().unwrap(),
        Records::empty().unwrap(),
        |value| super::super::retained_schema::normalize_with_clock(value, &mut clock),
    )
    .unwrap();
    let mut worker = Vec::new();
    records
        .visit_source(|value| {
            worker.push(value);
            Ok::<_, Infallible>(())
        })
        .unwrap();
    let worker_calls = count.get();
    count.set(0);
    let raw = Records::from_raw_reader(text.as_bytes()).unwrap();
    let mut gateway = Vec::new();
    raw.visit_source::<crate::app_error::AppError>(|value| {
        let Ok(stored) = crate::media_schema::normalize_media_record_with_clock(&value, &mut clock)
        else {
            return Ok(());
        };
        gateway.push(crate::media_schema::normalize_media_record_with_clock(
            &stored, &mut clock,
        )?);
        Ok(())
    })
    .unwrap();
    assert_eq!(
        (
            worker_calls,
            Value::Array(worker),
            count.get(),
            Value::Array(gateway)
        ),
        (2, case["rows"].clone(), 2, case["rows"].clone())
    );
}
