//! Shared HTTP fallbacks for migrated public routes.
//!
//! Several original top-level routes share the same FastAPI plumbing: the
//! `read_json_body` contract, the `/api/{path:path}` catch-all's flat 404, the
//! global OPTIONS 204 with both access-control allow lists, FastAPI's 405 for
//! non-matching verbs, and the `AppError.to_response()` / unhandled-500
//! envelopes. Keeping them in one place stops the migrated routes from growing
//! divergent copies of the same oracle behavior.

use axum::Json;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use serde_json::{Map, Value, json};

/// `parse_content_length`: Python's `int()`, refusing malformed and negative
/// values with the original `Invalid Content-Length` error.
pub(crate) fn parse_content_length(raw: &str) -> Result<i64, AppError> {
    let trimmed = raw.trim();
    let cleaned: String = trimmed
        .chars()
        .filter(|character| *character != '_')
        .collect();
    let Ok(value) = cleaned.parse::<i64>() else {
        return Err(AppError {
            message: "Invalid Content-Length".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    if value < 0 {
        return Err(AppError {
            message: "Invalid Content-Length".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(value)
}

/// `read_json_body(request, max_bytes)`: the original declared-length checks,
/// UTF-8 decode, JSON parse and object requirement.
pub(crate) fn read_json_object(
    headers: &HeaderMap,
    body: &[u8],
    max_bytes: i64,
) -> Result<Map<String, Value>, AppError> {
    let content_length = parse_content_length(
        headers
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("0"),
    )?;
    if content_length <= 0 {
        return Err(AppError {
            message: "Request body is empty".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    if content_length > max_bytes {
        return Err(AppError {
            message: "Request body is too large".to_string(),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let text = std::str::from_utf8(body).map_err(|_| unhandled_error())?;
    let payload: Value = serde_json::from_str(text).map_err(|error| AppError {
        message: format!("Invalid JSON: {error}"),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    })?;
    let Some(object) = payload.as_object() else {
        return Err(AppError {
            message: "Request body must be a JSON object".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    Ok(object.clone())
}

/// `unhandled_error_handler`: `{"error":"Server error","code":"internal"}`, 500.
pub(crate) fn unhandled_error() -> AppError {
    AppError {
        message: "Server error".to_string(),
        code: codes::INTERNAL,
        status: 500,
    }
}

/// The oracle's `AppError.to_response()` envelope: `{"error", "code"}`.
pub(crate) fn app_error_response(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

/// The `/api/{path:path}` catch-all's GET answer.
pub(crate) fn route_not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"error": "Not found", "code": codes::NOT_FOUND})),
    )
        .into_response()
}

/// The global OPTIONS route: 204 with the two fixed allow lists.
pub(crate) fn options_response() -> Response {
    let mut response = StatusCode::NO_CONTENT.into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static("GET, POST, PATCH, DELETE, OPTIONS"),
    );
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_HEADERS,
        HeaderValue::from_static(
            "Content-Type, Authorization, X-Backup-Filename, X-Workspace-Restore-Id",
        ),
    );
    response
}

/// FastAPI's method refusal when only the global OPTIONS route matches.
pub(crate) fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("OPTIONS"));
    response
}

#[cfg(test)]
mod tests {
    use super::parse_content_length;

    #[test]
    fn content_length_accepts_python_integer_forms_and_refuses_others() {
        assert_eq!(parse_content_length("0").expect("zero"), 0);
        assert_eq!(parse_content_length(" 12 ").expect("padded"), 12);
        assert_eq!(parse_content_length("+12").expect("signed"), 12);
        assert_eq!(parse_content_length("1_2").expect("underscored"), 12);
        assert!(parse_content_length("").is_err());
        assert!(parse_content_length("-1").is_err());
        assert!(parse_content_length("12.0").is_err());
        assert!(parse_content_length("abc").is_err());
    }
}
