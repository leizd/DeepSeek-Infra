// Actual original Python _load_store/list_media goldens; no legacy-native oracle.
fn python_original_document_case(name: &str) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_python_document_v1.json"
    ))
    .unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 23);
    let case = cases.iter().find(|case| case["name"] == name).unwrap();
    assert_python_original_case(case);
}

fn assert_python_original_case(case: &Value) {
    let name = case["name"].as_str().unwrap();
    let text = case["document"].as_str().unwrap();
    let worker = Records::from_reader(text.as_bytes())
        .map_err(|error| error.to_string())
        .and_then(|records| {
            let mut items = Vec::new();
            records
                .visit_source(|value| {
                    items.push(value);
                    Ok::<_, Infallible>(())
                })
                .map_err(|error| match error {
                    VisitError::Storage(status) => status.to_string(),
                    VisitError::Visitor(impossible) => match impossible {},
                })?;
            Ok(Value::Array(items))
        });
    let gateway = Records::from_raw_reader(text.as_bytes())
        .map_err(|error| format!("{error:?}"))
        .and_then(|records| {
            let mut items = Vec::new();
            records
                .visit_source::<crate::app_error::AppError>(|value| {
                    let Ok(stored) = crate::media_schema::normalize_media_record(&value) else {
                        return Ok(());
                    };
                    items.push(crate::media_schema::normalize_media_record(&stored)?);
                    Ok(())
                })
                .map_err(|error| match error {
                    VisitError::Storage(status) => status.to_string(),
                    VisitError::Visitor(error) => error.to_string(),
                })?;
            items.sort_by_key(|item| {
                std::cmp::Reverse(item["updatedAt"].as_str().unwrap().to_owned())
            });
            Ok(Value::Array(items))
        });
    // Evaluate both readers before asserting, including when one already differs.
    assert_eq!(
        (worker, gateway),
        (
            Ok(case["storedRows"].clone()),
            Ok(case["publicRows"].clone())
        ),
        "original Python case {name}"
    );
}

#[test]
fn python_original_discarded_number_large_exponent() {
    python_original_document_case("discarded_number_large_exponent");
}

#[test]
fn python_original_discarded_number_negative_large_exponent() {
    python_original_document_case("discarded_number_negative_large_exponent");
}

#[test]
fn python_original_discarded_number_tiny_exponent() {
    python_original_document_case("discarded_number_tiny_exponent");
}

#[test]
fn python_original_discarded_number_nan() {
    python_original_document_case("discarded_number_nan");
}

#[test]
fn python_original_discarded_number_infinity() {
    python_original_document_case("discarded_number_infinity");
}

#[test]
fn python_original_discarded_number_negative_infinity() {
    python_original_document_case("discarded_number_negative_infinity");
}

#[test]
fn python_original_discarded_array_nan() {
    python_original_document_case("discarded_array_nan");
}

#[test]
fn python_original_discarded_array_infinity() {
    python_original_document_case("discarded_array_infinity");
}

#[test]
fn python_original_discarded_array_negative_infinity() {
    python_original_document_case("discarded_array_negative_infinity");
}

#[test]
fn python_original_ordinary_root_tag_9() {
    python_original_document_case("ordinary_root_tag_9");
}

#[test]
fn python_original_ordinary_discarded_tag_10() {
    python_original_document_case("ordinary_discarded_tag_10");
}

#[test]
fn python_original_ordinary_root_tag_11() {
    python_original_document_case("ordinary_root_tag_11");
}

#[test]
fn python_original_ordinary_discarded_tag_12() {
    python_original_document_case("ordinary_discarded_tag_12");
}

#[test]
fn python_original_ordinary_root_tag_13() {
    python_original_document_case("ordinary_root_tag_13");
}

#[test]
fn python_original_ordinary_discarded_tag_14() {
    python_original_document_case("ordinary_discarded_tag_14");
}

#[test]
fn python_original_ordinary_retained_record_tag_15() {
    python_original_document_case("ordinary_retained_record_tag_15");
}

#[test]
fn python_original_ordinary_retained_record_tag_16() {
    python_original_document_case("ordinary_retained_record_tag_16");
}

#[test]
fn python_original_ordinary_tag_media_object_is_not_an_array() {
    python_original_document_case("ordinary_tag_media_object_is_not_an_array");
}

#[test]
fn python_original_discarded_depth_127() {
    python_original_document_case("discarded_depth_127");
}

#[test]
fn python_original_discarded_depth_128() {
    python_original_document_case("discarded_depth_128");
}

#[test]
fn python_original_discarded_depth_200() {
    python_original_document_case("discarded_depth_200");
}

#[test]
fn python_original_discarded_depth_500() {
    python_original_document_case("discarded_depth_500");
}

#[test]
fn python_original_retained_schema_ignored_surrogate() {
    python_original_document_case("retained_schema_ignored_surrogate");
}

fn python_original_contract_family(family: &str) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_python_contract_v1.json"
    ))
    .unwrap();
    assert_eq!(fixture["caseCount"], 328);
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 328);
    let selected: Vec<_> = cases
        .iter()
        .filter(|case| case["family"] == family)
        .collect();
    assert_eq!(
        selected.len() as u64,
        fixture["families"][family].as_u64().unwrap()
    );
    assert!(!selected.is_empty());
    for case in selected {
        assert_python_original_case(case);
    }
}
