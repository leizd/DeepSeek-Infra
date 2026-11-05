// Original socket observations; this unit exercises the shared GET renderers.
// Full authenticated process/socket qualification remains a separate requirement.
use sha2::Digest as _;

// Compare exact decimal tokens without changing the production serde features.
// Prefix every string/key and number differently so data cannot impersonate a tag.
fn retained_decimal(token: &[u8]) -> Result<String, ()> {
    let text = std::str::from_utf8(token).map_err(|_| ())?;
    let (negative, text) = match text.strip_prefix('-') {
        Some(text) => (true, text),
        None => (false, text),
    };
    let (mantissa, mut exponent) = match text.find(['e', 'E']) {
        Some(at) => (&text[..at], text[at + 1..].parse::<i64>().map_err(|_| ())?),
        None => (text, 0),
    };
    let (integer, fraction) = match mantissa.split_once('.') {
        Some((integer, fraction)) if !fraction.is_empty() => (integer, fraction),
        Some(_) => return Err(()),
        None => (mantissa, ""),
    };
    if integer.is_empty()
        || (integer.len() > 1 && integer.starts_with('0'))
        || !integer
            .bytes()
            .chain(fraction.bytes())
            .all(|byte| byte.is_ascii_digit())
    {
        return Err(());
    }
    exponent = exponent
        .checked_sub(i64::try_from(fraction.len()).map_err(|_| ())?)
        .ok_or(())?;
    let mut digits = format!("{integer}{fraction}")
        .trim_start_matches('0')
        .to_string();
    if digits.is_empty() {
        return Ok("0".to_string());
    }
    while digits.ends_with('0') {
        digits.pop();
        exponent = exponent.checked_add(1).ok_or(())?;
    }
    Ok(format!(
        "{}{digits}e{exponent}",
        if negative { "-" } else { "" }
    ))
}

fn retained_exact_json(bytes: &[u8]) -> Result<Value, ()> {
    let mut output = Vec::new();
    let mut at = 0;
    while let Some(&byte) = bytes.get(at) {
        if byte == b'"' {
            let start = at;
            at += 1;
            loop {
                match bytes.get(at).ok_or(())? {
                    b'\\' => at += 2,
                    b'"' => {
                        at += 1;
                        break;
                    }
                    _ => at += 1,
                }
            }
            output.extend_from_slice(b"\"\\u0001s");
            output.extend_from_slice(bytes.get(start + 1..at).ok_or(())?);
        } else if matches!(byte, b'-' | b'0'..=b'9') {
            let start = at;
            while bytes
                .get(at)
                .is_some_and(|byte| matches!(byte, b'0'..=b'9' | b'-' | b'+' | b'e' | b'E' | b'.'))
            {
                at += 1;
            }
            let number = retained_decimal(&bytes[start..at])?;
            serde_json::to_writer(&mut output, &format!("\u{1}n{number}")).map_err(|_| ())?;
        } else {
            output.push(byte);
            at += 1;
        }
    }
    serde_json::from_slice(&output).map_err(|_| ())
}

fn retained_native_response(path: &std::path::Path, role: &str) -> axum::response::Response {
    let result = match role {
        "list" | "filteredList" => PublicIndex::load(path)
            .and_then(|index| {
                index.collection(|value| role == "list" || value.field_equals("type", "pdf"))
            })
            .map(IntoResponse::into_response),
        "detail" | "independentDetail" => {
            let id = if role == "detail" {
                "media_kept"
            } else {
                "media_control"
            };
            find(path, id)
                .and_then(|row| row.ok_or_else(|| AppError::not_found("Media not found")))
                .and_then(|row| CompletedJson::item(&row))
                .map(IntoResponse::into_response)
        }
        _ => panic!("unknown original retained HTTP role"),
    };
    result.unwrap_or_else(super::super::error_response)
}

async fn assert_retained_original_http(name: &str) {
    let golden: Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/native_media_retained_http_v1.json"
    ))
    .unwrap();
    let cases = golden["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 34);
    let case = cases.iter().find(|case| case["name"] == name).unwrap();
    let bytes = case["document"].as_str().unwrap().as_bytes();
    assert_eq!(
        format!("{:x}", sha2::Sha256::digest(bytes)),
        case["documentSha256"]
    );
    let file = fixture(bytes);
    let mut mismatches = Vec::new();
    for role in ["list", "detail", "independentDetail", "filteredList"] {
        let expected = &case["results"][role];
        let response = retained_native_response(file.path(), role);
        let status = response.status().as_u16();
        let body = to_bytes(response.into_body(), 2 * 1024 * 1024)
            .await
            .unwrap();
        let actual = retained_exact_json(&body).unwrap();
        let expected_bytes = expected["bodyJson"].as_str().unwrap().as_bytes();
        if u64::from(status) != expected["status"].as_u64().unwrap()
            || actual != retained_exact_json(expected_bytes).unwrap()
        {
            mismatches.push(format!(
                "{role}: status {status}/{}, body sha256 {:x}/{:x}",
                expected["status"],
                sha2::Sha256::digest(&body),
                sha2::Sha256::digest(expected_bytes)
            ));
        }
        assert!(
            fs::read(file.path()).unwrap() == bytes,
            "read changed source: {name}/{role}"
        );
    }
    assert!(
        mismatches.is_empty(),
        "original retained HTTP mismatch {name}: {mismatches:?}"
    );
}

#[test]
fn retained_http_comparison_preserves_integers_and_scalar_types() {
    assert_eq!(
        retained_exact_json(br#"{"x":1.00,"s":"1"}"#),
        retained_exact_json(br#"{"s":"1","x":1e0}"#)
    );
    for (left, right) in [
        ("18446744073709551617", "1.8446744073709552e19"),
        (
            "1234567890123456789012345678901234567890",
            "1.2345678901234568e39",
        ),
        (r#""18446744073709551617""#, "18446744073709551617"),
        (r#""\u0001n1e0""#, "1"),
        (r#"{"$serde_json::private::Number":"1"}"#, "1"),
    ] {
        assert_ne!(
            retained_exact_json(left.as_bytes()).unwrap(),
            retained_exact_json(right.as_bytes()).unwrap()
        );
    }
    for invalid in [b"01".as_slice(), b"[1e]", b"NaN", b"{\"x\":1,}"] {
        assert!(retained_exact_json(invalid).is_err());
    }
}

#[tokio::test]
async fn retained_http_metadata_number_0() {
    assert_retained_original_http("metadata_number_0").await;
}

#[tokio::test]
async fn retained_http_metadata_number_1() {
    assert_retained_original_http("metadata_number_1").await;
}

#[tokio::test]
async fn retained_http_metadata_number_2() {
    assert_retained_original_http("metadata_number_2").await;
}

#[tokio::test]
async fn retained_http_metadata_number_3() {
    assert_retained_original_http("metadata_number_3").await;
}

#[tokio::test]
async fn retained_http_metadata_number_4() {
    assert_retained_original_http("metadata_number_4").await;
}

#[tokio::test]
async fn retained_http_metadata_number_5() {
    assert_retained_original_http("metadata_number_5").await;
}

#[tokio::test]
async fn retained_http_metadata_number_6() {
    assert_retained_original_http("metadata_number_6").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_7() {
    assert_retained_original_http("metadata_surrogate_7").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_8() {
    assert_retained_original_http("metadata_surrogate_8").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_9() {
    assert_retained_original_http("metadata_surrogate_9").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_10() {
    assert_retained_original_http("metadata_surrogate_10").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_11() {
    assert_retained_original_http("metadata_surrogate_11").await;
}

#[tokio::test]
async fn retained_http_metadata_sanitized_away_depth_500() {
    assert_retained_original_http("metadata_sanitized_away_depth_500").await;
}

#[tokio::test]
async fn retained_http_metadata_sanitized_away_depth_2048() {
    assert_retained_original_http("metadata_sanitized_away_depth_2048").await;
}

#[tokio::test]
async fn retained_http_source_number_14() {
    assert_retained_original_http("source_number_14").await;
}

#[tokio::test]
async fn retained_http_source_number_15() {
    assert_retained_original_http("source_number_15").await;
}

#[tokio::test]
async fn retained_http_source_number_16() {
    assert_retained_original_http("source_number_16").await;
}

#[tokio::test]
async fn retained_http_source_number_17() {
    assert_retained_original_http("source_number_17").await;
}

#[tokio::test]
async fn retained_http_source_number_18() {
    assert_retained_original_http("source_number_18").await;
}

#[tokio::test]
async fn retained_http_source_number_19() {
    assert_retained_original_http("source_number_19").await;
}

#[tokio::test]
async fn retained_http_source_number_20() {
    assert_retained_original_http("source_number_20").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_21() {
    assert_retained_original_http("source_surrogate_21").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_22() {
    assert_retained_original_http("source_surrogate_22").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_23() {
    assert_retained_original_http("source_surrogate_23").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_24() {
    assert_retained_original_http("source_surrogate_24").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_25() {
    assert_retained_original_http("source_surrogate_25").await;
}

#[tokio::test]
async fn retained_http_source_sanitized_away_depth_500() {
    assert_retained_original_http("source_sanitized_away_depth_500").await;
}

#[tokio::test]
async fn retained_http_source_sanitized_away_depth_2048() {
    assert_retained_original_http("source_sanitized_away_depth_2048").await;
}

#[tokio::test]
async fn retained_http_title_number_28() {
    assert_retained_original_http("title_number_28").await;
}

#[tokio::test]
async fn retained_http_title_number_29() {
    assert_retained_original_http("title_number_29").await;
}

#[tokio::test]
async fn retained_http_title_number_30() {
    assert_retained_original_http("title_number_30").await;
}

#[tokio::test]
async fn retained_http_title_number_31() {
    assert_retained_original_http("title_number_31").await;
}

#[tokio::test]
async fn retained_http_metadata_surrogate_after_original_business_cap() {
    assert_retained_original_http("metadata_surrogate_after_original_business_cap").await;
}

#[tokio::test]
async fn retained_http_source_surrogate_in_dropped_key() {
    assert_retained_original_http("source_surrogate_in_dropped_key").await;
}
