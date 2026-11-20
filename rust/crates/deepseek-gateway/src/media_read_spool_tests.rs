use super::*;

fn ordinary(value: MediaRecord) -> Value {
    let mut bytes = Vec::new();
    value.write_response(&mut bytes).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

fn ordinary_window(
    result: Result<(Vec<MediaRecord>, usize), AppError>,
) -> Result<(Vec<Value>, usize), AppError> {
    result.map(|(values, count)| (values.into_iter().map(ordinary).collect(), count))
}

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
        find(file.path(), "same").unwrap().map(ordinary),
        Some(expected[0].clone())
    );
    expected.sort_by_key(|item| std::cmp::Reverse(media_sort_key(item)));
    let index = PublicIndex::load(file.path()).unwrap();
    let mut actual = Vec::new();
    index
        .visit_sorted(|value| {
            actual.push(ordinary(value));
            Ok(())
        })
        .unwrap();
    assert_eq!(actual, expected);
    assert_eq!(
        ordinary_window(index.window(2)).unwrap(),
        (expected[..2].to_vec(), 4)
    );
    assert_eq!(ordinary_window(index.window(0)).unwrap(), (Vec::new(), 4));
    assert_eq!(ordinary_window(index.window(99)).unwrap(), (expected, 4));
}

#[tokio::test]
async fn second_normalization_follows_original_selection_and_complete_windows() {
    let golden: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_selected_normalization_v1.json"
    ))
    .unwrap();
    assert_eq!(golden["actualOriginalLoopbackRequests"], 13);
    let variants = golden["variants"].as_array().unwrap();
    assert_eq!(variants.len(), 2);
    for variant in variants {
        let bytes = variant["document"].as_str().unwrap().as_bytes();
        let file = fixture(bytes);
        let expected = &variant["results"];
        // This old native helper still describes the unfiltered list's error.
        assert_eq!(original(bytes).unwrap_err().status, 400);
        assert_eq!(
            find(file.path(), "first").unwrap().map(ordinary),
            Some(expected["selectedFirst"]["body"]["media"].clone())
        );
        assert_eq!(find(file.path(), "later").unwrap_err().status, 400);
        let index = PublicIndex::load(file.path()).unwrap();
        assert_eq!(index.collection(|_| true).err().unwrap().status, 400);
        for limit in [0, 1, 99] {
            assert_eq!(index.window(limit).unwrap_err().status, 400);
        }
        let response = index
            .collection(|value| value.field_equals("type", "pdf"))
            .unwrap()
            .into_response();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), 2 * 1024 * 1024)
            .await
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap(),
            expected["emptyFilter"]["body"]
        );
        if let Some(project) = expected.get("selectedProject") {
            let response = index
                .collection(|value| value.field_equals("projectId", "proj_first"))
                .unwrap()
                .into_response();
            assert_eq!(response.status(), StatusCode::OK);
            let body = to_bytes(response.into_body(), 2 * 1024 * 1024)
                .await
                .unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&body).unwrap(),
                project["body"]
            );
        }
        // Component membership has no public validation; segment routes use get_media.
        assert!(contains(file.path(), "later").unwrap());
        assert!(fs::read(file.path()).unwrap() == bytes);
    }
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
                    ordinary_window(index.window(99)).unwrap(),
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
        ordinary_window(PublicIndex::load(&missing).unwrap().window(1)).unwrap(),
        (vec![], 0)
    );
    assert!(!missing.exists());
    assert_eq!(
        ordinary_window(PublicIndex::load(directory.path()).unwrap().window(1)).unwrap(),
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
        .collection(|value| value.field_equals("mediaId", "media_alpha"))
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
    use futures_util::StreamExt;

    let mut wide = row("media_alpha", "1");
    wide["metadata"]["wide"] = json!("x".repeat(120_000));
    let bytes = serde_json::to_vec(&json!({"media":[wide]})).unwrap();
    let file = fixture(&bytes);
    for mode in [
        "truncated",
        "same-length",
        "trailing",
        "trailing-full-buffer",
    ] {
        let output = if mode == "trailing-full-buffer" {
            // The extra byte must be detected by the read after Content-Length.
            let payload = serde_json::to_vec(&"x".repeat(2 * BUFFER - 2)).unwrap();
            assert_eq!(payload.len(), 2 * BUFFER);
            CompletedJson::build(|output| output.write_all(&payload).map_err(|_| server_error()))
                .unwrap()
        } else {
            PublicIndex::load(file.path())
                .unwrap()
                .collection(|_| true)
                .unwrap()
        };
        let temporary_path = output.file.path().to_path_buf();
        let mut changed = fs::read(&temporary_path).unwrap();
        let expected_length = changed.len();
        assert!(expected_length > BUFFER);
        match mode {
            "truncated" => changed = b"changed".to_vec(),
            "same-length" => changed[0] ^= 1,
            "trailing" | "trailing-full-buffer" => changed.push(b'!'),
            _ => unreachable!(),
        }
        fs::write(&temporary_path, changed).unwrap();
        let (body, producer) = output.stream();
        let mut stream = Box::pin(body.into_data_stream());
        let mut delivered = 0usize;
        let mut failed = false;
        while let Some(part) = stream.next().await {
            match part {
                Ok(bytes) => delivered += bytes.len(),
                Err(_) => {
                    failed = true;
                    break;
                }
            }
        }
        assert!(
            failed && delivered < expected_length,
            "{mode}: {delivered}/{expected_length}"
        );
        producer.await.unwrap();
        assert!(!temporary_path.exists());
    }
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
            ordinary_window(index.window(99)).unwrap(),
            (expected.clone(), expected.len()),
            "{}",
            case["name"]
        );
        assert_eq!(
            find(file.path(), "media_kept").unwrap().map(ordinary),
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
