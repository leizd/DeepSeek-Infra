use super::*;
use axum::body::to_bytes;
use axum::http::Method;
use std::fs;

fn row(id: &str, stamp: &str) -> Value {
    let value = json!({"mediaId":id,"type":"image","path":"image.png","title":id,
        "createdAt":"fixed","updatedAt":stamp,"metadata":{"preserved":[1,true,"λ"]}});
    assert!(
        normalize_media_record(&value).is_ok(),
        "invalid positive fixture: {id}"
    );
    value
}

fn fixture(bytes: &[u8]) -> tempfile::NamedTempFile {
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(bytes).unwrap();
    file
}

// Frozen pre-change gateway reader; it is deliberately independent of the spool.
fn original(bytes: &[u8]) -> Result<Vec<Value>, AppError> {
    let text = std::str::from_utf8(bytes).map_err(|_| server_error())?;
    let value: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(_) => return Ok(Vec::new()),
    };
    let mut rows = Vec::new();
    for item in value
        .get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if !item.is_object() {
            continue;
        }
        if let Ok(stored) = normalize_media_record(item) {
            rows.push(normalize_media_record(&stored)?);
        }
    }
    Ok(rows)
}

#[test]
fn source_order_first_match_stable_sort_and_all_fields_match_original() {
    let input = json!({"media":[row("same","a"),row("other","z"),row("same","z"),
        {"mediaId":"bad","type":"invalid"},null,row("unicode","λ")]});
    let bytes = serde_json::to_vec(&input).unwrap();
    let file = fixture(&bytes);
    let mut expected = original(&bytes).unwrap();
    assert_eq!(
        find(file.path(), "same").unwrap(),
        Some(expected[0].clone())
    );
    expected.sort_by_key(|item| std::cmp::Reverse(media_sort_key(item)));
    let index = PublicIndex::load(file.path()).unwrap();
    let mut actual = Vec::new();
    index
        .visit_sorted(|value| {
            actual.push(value);
            Ok(())
        })
        .unwrap();
    assert_eq!(actual, expected);
    assert_eq!(index.window(2).unwrap(), (expected[..2].to_vec(), 4));
    assert_eq!(index.window(0).unwrap(), (Vec::new(), 4));
    assert_eq!(index.window(99).unwrap(), (expected, 4));
}

#[test]
fn later_second_normalization_errors_propagate_before_filter_find_and_window() {
    let bytes = serde_json::to_vec(&json!({"media":[row("first","z"),
        {"mediaId":"later","type":"image","path":"./C:/x.png"}]}))
    .unwrap();
    let file = fixture(&bytes);
    assert_eq!(original(&bytes).unwrap_err().status, 400);
    assert_eq!(find(file.path(), "first").unwrap_err().status, 400);
    assert!(matches!(PublicIndex::load(file.path()), Err(error) if error.status == 400));
    // Segment membership uses the first raw acceptance only, as before.
    assert!(contains(file.path(), "later").unwrap());
}

#[test]
fn complete_document_last_key_utf8_and_missing_sources_preserve_old_results() {
    let first = serde_json::to_string(&row("first", "z")).unwrap();
    let cases = [
        format!(r#"{{"media":[{}],"media":null}}"#, first).into_bytes(),
        format!(r#"{{"media":[{}]}} trailing"#, first).into_bytes(),
        format!(r#"{{"media":[],"m\u0065dia":[{}]}}"#, first).into_bytes(),
        b"!\xff".to_vec(),
    ];
    for bytes in cases {
        let file = fixture(&bytes);
        match original(&bytes) {
            Ok(expected) => {
                let index = PublicIndex::load(file.path()).unwrap();
                assert_eq!(
                    index.window(99).unwrap(),
                    (expected.clone(), expected.len())
                );
            }
            Err(error) => assert!(
                matches!(PublicIndex::load(file.path()), Err(actual) if actual.status == error.status)
            ),
        }
    }
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("absent.json");
    assert_eq!(
        PublicIndex::load(&missing).unwrap().window(1).unwrap(),
        (vec![], 0)
    );
    assert!(!missing.exists());
    assert_eq!(
        PublicIndex::load(directory.path())
            .unwrap()
            .window(1)
            .unwrap(),
        (vec![], 0)
    );
}

#[test]
fn raw_membership_is_not_short_circuited_before_full_json_validation() {
    let good = serde_json::to_string(&row("first", "z")).unwrap();
    for bytes in [
        format!(r#"{{"media":[{}]}}junk"#, good),
        format!(r#"{{"media":[{}],"media":[]}}"#, good),
    ] {
        let file = fixture(bytes.as_bytes());
        assert!(!contains(file.path(), "first").unwrap());
    }
}

#[tokio::test]
async fn streamed_collection_matches_original_bytes_filters_headers_and_head() {
    let bytes =
        serde_json::to_vec(&json!({"media":[row("media_alpha","1"),row("media_beta","2")]}))
            .unwrap();
    let file = fixture(&bytes);
    let mut rows = original(&bytes).unwrap();
    assert_eq!(
        rows.len(),
        2,
        "both positive collection fixtures must be retained"
    );
    rows.sort_by_key(|item| std::cmp::Reverse(media_sort_key(item)));
    let index = PublicIndex::load(file.path()).unwrap();
    let response = index.collection(|_| true).unwrap().into_response();
    let expected = serde_json::to_vec(&json!({"ok":true,"media":rows})).unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    assert_eq!(
        response.headers()[header::CONTENT_LENGTH],
        expected.len().to_string()
    );
    assert_eq!(
        to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .as_ref(),
        expected
    );
    let response = index
        .collection(|value| value["mediaId"] == "media_alpha")
        .unwrap()
        .into_response();
    let value: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), usize::MAX).await.unwrap()).unwrap();
    assert_eq!(value["media"][0]["mediaId"], "media_alpha");
    assert_eq!(value["media"].as_array().unwrap().len(), 1);
    let prepared = index.collection(|_| true).unwrap();
    let response = super::super::read_response(Method::HEAD, move || Ok(prepared)).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert!(response.headers().get(header::CONTENT_TYPE).is_none());
    assert!(
        to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn dropping_a_stream_releases_its_private_file_without_modifying_the_library() {
    let bytes = serde_json::to_vec(&json!({"media":[row("media_alpha","1")]})).unwrap();
    let file = fixture(&bytes);
    let output = PublicIndex::load(file.path())
        .unwrap()
        .collection(|_| true)
        .unwrap();
    let temporary_path = output.file.path().to_path_buf();
    let (body, producer) = output.stream();
    drop(body);
    producer.await.unwrap();
    assert!(!temporary_path.exists());
    assert_eq!(fs::read(file.path()).unwrap(), bytes);
}

#[tokio::test]
async fn changed_response_spool_fails_instead_of_claiming_a_complete_body() {
    let bytes = serde_json::to_vec(&json!({"media":[row("media_alpha","1")]})).unwrap();
    let file = fixture(&bytes);
    let output = PublicIndex::load(file.path())
        .unwrap()
        .collection(|_| true)
        .unwrap();
    let temporary_path = output.file.path().to_path_buf();
    fs::write(&temporary_path, b"changed").unwrap();
    let (body, producer) = output.stream();
    assert!(to_bytes(body, usize::MAX).await.is_err());
    producer.await.unwrap();
    assert!(!temporary_path.exists());
    assert_eq!(fs::read(file.path()).unwrap(), bytes);
}

#[tokio::test]
async fn discarded_unicode_collection_detail_membership_and_window_match_python() {
    let golden: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_discarded_unicode_v1.json"
    ))
    .unwrap();
    let cases = golden["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 21);
    for case in cases {
        let bytes = case["document"].as_str().unwrap().as_bytes();
        let file = fixture(bytes);
        let expected = case["publicRows"].as_array().unwrap();
        let index = PublicIndex::load(file.path()).unwrap();
        assert_eq!(
            index.window(99).unwrap(),
            (expected.clone(), expected.len()),
            "{}",
            case["name"]
        );
        assert_eq!(
            find(file.path(), "media_kept").unwrap(),
            expected.first().cloned(),
            "{}",
            case["name"]
        );
        assert_eq!(
            contains(file.path(), "media_kept").unwrap(),
            !expected.is_empty(),
            "{}",
            case["name"]
        );
        let response = index.collection(|_| true).unwrap().into_response();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let actual: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            actual,
            json!({"ok":true,"media":expected}),
            "{}",
            case["name"]
        );
        assert_eq!(fs::read(file.path()).unwrap(), bytes);
    }
}

include!("media_retained_http_tests.rs");
