//! Read-only Authority retention policy: `GET /api/workspace/authority/retention/policy` (S2b).
//!
//! The original reads `<root>/.backup-authority-retention/policy.json` and
//! normalizes it on every request; a missing, unreadable or unparseable file
//! answers the defaults. This port reproduces that normalization exactly,
//! including the two asymmetric branches:
//!
//! * a *falsy* stored JSON value (`[]`, `""`, `0`, `null`, `false`) reaches
//!   `normalize_retention_policy(payload or {})` as `{}` and answers the
//!   defaults, while
//! * a *truthy* non-object (`[1, 2]`, `"text"`, `7`) is the documented
//!   `400 Retention policy must be a JSON object`.
//!
//! A stored object whose fields do not validate is the original's `400` with the
//! field-specific message, so a hand-edited policy is never silently ignored.
//! Nothing is created: the directory, the policy file and the default file all
//! stay absent when they were absent.

use std::path::PathBuf;

use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Map, Value, json};

use crate::route_support;

const RETENTION_POLICY_VERSION: i64 = 1;
const DEFAULT_MINIMUM_GENERATIONS: i64 = 100;
const DEFAULT_MINIMUM_AGE_DAYS: i64 = 30;
const DEFAULT_CHECKPOINT_INTERVAL: i64 = 10_000;
const DEFAULT_KEEP_MUTATION_CLASSES: [&str; 4] = [
    "authority-bootstrap",
    "target-change",
    "security-event",
    "formal-truth-failure",
];

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/authority/retention/policy",
        any(policy_response),
    )
}

async fn policy_response(
    method: Method,
    headers: HeaderMap,
    uri: OriginalUri,
    body: Bytes,
) -> Response {
    match method {
        Method::GET | Method::HEAD => {
            let joined = tokio::task::spawn_blocking(policy_body).await;
            match joined {
                Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
                Ok(Ok(body)) => Json(body).into_response(),
                Ok(Err(error)) => route_support::app_error_response(error),
                Err(_) if method == Method::HEAD => {
                    StatusCode::INTERNAL_SERVER_ERROR.into_response()
                }
                Err(_) => route_support::app_error_response(route_support::unhandled_error()),
            }
        }
        // `POST` on this exact path is the original's policy *mutation*
        // (`put_authority_retention_policy`), which this slice does not
        // implement. Forwarding keeps the pre-existing fail-closed behaviour
        // instead of impersonating a write with the catch-all 404.
        Method::POST => crate::control_proxy::proxy_api_to_go(method, headers, uri, body).await,
        Method::OPTIONS => route_support::options_response(),
        _ => route_support::method_not_allowed(),
    }
}

fn policy_body() -> Result<Value, AppError> {
    normalize_retention_policy(&stored_policy()?)
}

fn policy_path() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-authority-retention")
        .join("policy.json")
}

/// `json.loads(path.read_text())` with the original's two fallbacks: an `OSError`
/// or a `JSONDecodeError` answers the defaults.
fn stored_policy() -> Result<Value, AppError> {
    let path = policy_path();
    if !path.is_file() {
        return Ok(Value::Null);
    }
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Ok(Value::Null);
    };
    Ok(serde_json::from_str(&text).unwrap_or(Value::Null))
}

/// `normalize_retention_policy(payload)`: `data = payload or {}`, then field by
/// field validation with the original messages and status.
///
/// The truthiness test applies **only** to the whole payload. Each field uses
/// `data.get(name, default)`, so a *present* `null` is validated (and usually
/// rejected) rather than replaced by the default, and a present `0`/`[]` keeps
/// its value.
fn normalize_retention_policy(payload: &Value) -> Result<Value, AppError> {
    let data: &Map<String, Value> = if python_truthy(payload) {
        match payload.as_object() {
            Some(data) => data,
            None => {
                return Err(invalid_payload("Retention policy must be a JSON object"));
            }
        }
    } else {
        // `payload or {}` replaces every falsy payload with the empty object.
        static EMPTY: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);
        &EMPTY
    };

    let version = match data.get("retentionPolicyVersion") {
        Some(value) => value.clone(),
        None => json!(RETENTION_POLICY_VERSION),
    };
    // `version != RETENTION_POLICY_VERSION` with Python equality, where
    // `1.0 == 1` and `True == 1` but `"1"`, `None`, `False` and `[]` differ.
    if !python_equals_int(&version, RETENTION_POLICY_VERSION) {
        return Err(invalid_payload(&format!(
            "Unsupported retention policy version: {}",
            value_str(&version)
        )));
    }

    let minimum_generations = python_int_field(
        data.get("minimumGenerations"),
        DEFAULT_MINIMUM_GENERATIONS,
        1,
        "minimumGenerations must be an integer >= 1",
    )?;
    let minimum_age_days = python_int_field(
        data.get("minimumAgeDays"),
        DEFAULT_MINIMUM_AGE_DAYS,
        0,
        "minimumAgeDays must be an integer >= 0",
    )?;
    let checkpoint_interval = python_int_field(
        data.get("checkpointInterval"),
        DEFAULT_CHECKPOINT_INTERVAL,
        1,
        "checkpointInterval must be an integer >= 1",
    )?;

    let keep_classes = match data.get("keepMutationClasses") {
        Some(value) => {
            // The original accepts a list or tuple of *any* values and then
            // `str(item).strip()`s each one, so a non-string member becomes its
            // Python text (`None`, `True`, `['x']`, `{'k': 'v'}`).
            let Some(items) = value.as_array() else {
                return Err(invalid_payload(
                    "keepMutationClasses must be a list of strings",
                ));
            };
            let mut cleaned: Vec<String> = items
                .iter()
                .map(|item| python_strip(&value_str(item)).to_string())
                .filter(|item| !item.is_empty())
                .collect();
            cleaned.sort();
            cleaned.dedup();
            cleaned
        }
        None => DEFAULT_KEEP_MUTATION_CLASSES
            .iter()
            .map(|name| name.to_string())
            .collect(),
    };

    let mut body = Map::new();
    body.insert(
        "retentionPolicyVersion".to_string(),
        json!(RETENTION_POLICY_VERSION),
    );
    body.insert("minimumGenerations".to_string(), json!(minimum_generations));
    body.insert("minimumAgeDays".to_string(), json!(minimum_age_days));
    body.insert("keepMutationClasses".to_string(), json!(keep_classes));
    body.insert("checkpointInterval".to_string(), json!(checkpoint_interval));
    Ok(Value::Object(body))
}

/// `data.get(key, default)` then the original's `isinstance(value, int)` check:
/// booleans are `int` in Python but are rejected explicitly, floats and strings
/// are rejected, and the bounds are inclusive on the low side only.
///
/// A missing key uses the default; a present value is always validated.
fn python_int_field(
    value: Option<&Value>,
    default: i64,
    minimum: i64,
    message: &str,
) -> Result<i64, AppError> {
    let Some(value) = value else {
        return Ok(default);
    };
    let Some(number) = python_exact_int(value) else {
        return Err(invalid_payload(message));
    };
    if number < minimum {
        return Err(invalid_payload(message));
    }
    Ok(number)
}

/// `isinstance(value, int) and not isinstance(value, bool)` as an `i64`.
fn python_exact_int(value: &Value) -> Option<i64> {
    let Value::Number(number) = value else {
        return None;
    };
    number.as_i64()
}

/// Python's `value == expected` for the retention policy version, where a float
/// with an integral value and the boolean `True` both equal `1`.
fn python_equals_int(value: &Value, expected: i64) -> bool {
    match value {
        Value::Bool(flag) => i64::from(*flag) == expected,
        Value::Number(number) => number
            .as_i64()
            .map(|value| value == expected)
            .or_else(|| number.as_f64().map(|value| value == expected as f64))
            .unwrap_or(false),
        _ => false,
    }
}

fn invalid_payload(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

/// `str.strip()`: Unicode whitespace plus the four information separators
/// CPython also treats as whitespace.
fn python_strip(text: &str) -> &str {
    text.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
    })
}

#[cfg(test)]
mod tests {
    use super::{normalize_retention_policy, python_exact_int};
    use serde_json::json;

    fn defaults() -> serde_json::Value {
        json!({
            "retentionPolicyVersion": 1,
            "minimumGenerations": 100,
            "minimumAgeDays": 30,
            "keepMutationClasses": [
                "authority-bootstrap",
                "formal-truth-failure",
                "security-event",
                "target-change"
            ],
            "checkpointInterval": 10000,
        })
    }

    #[test]
    fn empty_and_falsy_payloads_answer_the_defaults() {
        assert_eq!(
            normalize_retention_policy(&json!(null)).unwrap(),
            defaults()
        );
        assert_eq!(normalize_retention_policy(&json!({})).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!([])).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!("")).unwrap(), defaults());
        assert_eq!(normalize_retention_policy(&json!(0)).unwrap(), defaults());
        assert_eq!(
            normalize_retention_policy(&json!(false)).unwrap(),
            defaults()
        );
    }

    #[test]
    fn truthy_non_objects_are_the_documented_400() {
        for payload in [json!([1, 2]), json!("text"), json!(7), json!(true)] {
            let error = normalize_retention_policy(&payload).unwrap_err();
            assert_eq!(error.message, "Retention policy must be a JSON object");
            assert_eq!(error.status, 400);
            assert_eq!(error.code, "invalid_payload");
        }
    }

    #[test]
    fn field_validation_matches_the_original_messages() {
        let cases = [
            (
                json!({"retentionPolicyVersion": 2}),
                "Unsupported retention policy version: 2",
            ),
            (
                json!({"retentionPolicyVersion": "1"}),
                "Unsupported retention policy version: 1",
            ),
            (
                json!({"retentionPolicyVersion": null}),
                "Unsupported retention policy version: None",
            ),
            (
                json!({"retentionPolicyVersion": false}),
                "Unsupported retention policy version: False",
            ),
            (
                json!({"retentionPolicyVersion": 0}),
                "Unsupported retention policy version: 0",
            ),
            (
                json!({"retentionPolicyVersion": []}),
                "Unsupported retention policy version: []",
            ),
            (
                json!({"minimumGenerations": true}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": 0}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": 1.0}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": "5"}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumGenerations": null}),
                "minimumGenerations must be an integer >= 1",
            ),
            (
                json!({"minimumAgeDays": -1}),
                "minimumAgeDays must be an integer >= 0",
            ),
            (
                json!({"minimumAgeDays": false}),
                "minimumAgeDays must be an integer >= 0",
            ),
            (
                json!({"checkpointInterval": 0}),
                "checkpointInterval must be an integer >= 1",
            ),
            (
                json!({"keepMutationClasses": "x"}),
                "keepMutationClasses must be a list of strings",
            ),
            (
                json!({"keepMutationClasses": null}),
                "keepMutationClasses must be a list of strings",
            ),
            (
                json!({"keepMutationClasses": 3}),
                "keepMutationClasses must be a list of strings",
            ),
        ];
        for (payload, message) in cases {
            let error = normalize_retention_policy(&payload).unwrap_err();
            assert_eq!(error.message, message, "{payload}");
            assert_eq!(error.status, 400);
        }
    }

    #[test]
    fn version_equality_accepts_python_numeric_equality() {
        // `1.0 == 1` and `True == 1` in Python; `"1"` and `null` do not.
        for payload in [
            json!({"retentionPolicyVersion": 1}),
            json!({"retentionPolicyVersion": 1.0}),
            json!({"retentionPolicyVersion": true}),
        ] {
            assert!(
                normalize_retention_policy(&payload).is_ok(),
                "{payload} should be accepted"
            );
        }
    }

    #[test]
    fn keep_classes_are_stripped_sorted_and_deduplicated() {
        let normalized = normalize_retention_policy(&json!({
            "keepMutationClasses": [" b ", "a", "a", "", null, 1],
        }))
        .unwrap();
        assert_eq!(
            normalized["keepMutationClasses"],
            json!(["1", "None", "a", "b"])
        );
        // A present empty list is honoured (`data.get` found it); it is not the
        // same as omitting the key.
        let empty = normalize_retention_policy(&json!({"keepMutationClasses": []})).unwrap();
        assert_eq!(empty["keepMutationClasses"], json!([]));
        let omitted = normalize_retention_policy(&json!({})).unwrap();
        assert_eq!(
            omitted["keepMutationClasses"],
            defaults()["keepMutationClasses"]
        );
    }

    #[test]
    fn non_string_members_become_python_text() {
        let normalized = normalize_retention_policy(&json!({
            "keepMutationClasses": [true, [["x"]], {"k": "v"}],
        }))
        .unwrap();
        assert_eq!(
            normalized["keepMutationClasses"],
            json!(["True", "['x']", "{'k': 'v'}"])
        );
    }

    #[test]
    fn booleans_are_not_integers() {
        assert_eq!(python_exact_int(&json!(true)), None);
        assert_eq!(python_exact_int(&json!(false)), None);
        assert_eq!(python_exact_int(&json!(7)), Some(7));
        assert_eq!(python_exact_int(&json!(7.0)), None);
        assert_eq!(python_exact_int(&json!("7")), None);
    }
}
