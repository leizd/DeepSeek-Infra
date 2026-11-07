//! `POST /api/fetch-url`.
//!
//! The original handler reads a JSON object, coerces `url` with Python's
//! `str(value or "")`, and returns `{"ok": true, "page": fetch_url(url)}`. The
//! SSRF check, pinned-address connection, redirect revalidation, cache and HTML
//! extraction live in `deepseek_policy::fetch_url` (probe-verified); this module
//! is the HTTP wiring and the original `read_json_body` contract around it.

use std::path::Path;

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::text_or_empty;
use deepseek_policy::fetch_url::{self, FetchContext};
use serde_json::{Value, json};

/// `read_json_body(request)`'s default bound in the original route.
const MAX_JSON_BODY_BYTES: i64 = 2_000_000;

pub fn router() -> Router {
    Router::new().route("/api/fetch-url", any(dispatch))
}

async fn dispatch(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    // The original route only answers POST. The `/api/{path:path}` catch-all
    // serves GET as the JSON 404, the global OPTIONS route answers 204, and the
    // remaining verbs are FastAPI's method refusal with `Allow: OPTIONS`.
    if method == Method::GET || method == Method::HEAD {
        return route_not_found();
    }
    if method == Method::OPTIONS {
        return options_response();
    }
    if method != Method::POST {
        return method_not_allowed();
    }
    let root = crate::data_routes::workspace_root();
    let joined = tokio::task::spawn_blocking(move || fetch_page(&headers, &body, &root)).await;
    match joined {
        Ok(Ok(page)) => Json(json!({"ok": true, "page": page})).into_response(),
        Ok(Err(error)) => app_error_response(error),
        Err(_) => app_error_response(unhandled_error()),
    }
}

fn fetch_page(headers: &HeaderMap, body: &[u8], root: &Path) -> Result<Value, AppError> {
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
    if content_length > MAX_JSON_BODY_BYTES {
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
    let url = text_or_empty(object.get("url"));
    let now_epoch = fetch_url::system_now();
    let dns = fetch_url::system_dns;
    let http = crate::fetch_provider::locked_http_get;
    let context = FetchContext {
        root,
        now_epoch,
        timeout_seconds: crate::fetch_provider::timeout_from_env(),
        dns: &dns,
        http: &http,
    };
    fetch_url::fetch_url(&url, Some(&context))
}

/// `parse_content_length`: Python's `int()`, refusing malformed and negative
/// values with the original `Invalid Content-Length` error.
fn parse_content_length(raw: &str) -> Result<i64, AppError> {
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

/// The original unhandled-exception envelope (`unhandled_error_handler`).
fn unhandled_error() -> AppError {
    AppError {
        message: "Server error".to_string(),
        code: codes::INTERNAL,
        status: 500,
    }
}

/// The oracle's `AppError.to_response()` envelope: `{"error", "code"}`.
fn app_error_response(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

/// The original `/api/{path:path}` catch-all's GET answer.
fn route_not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"error": "Not found", "code": codes::NOT_FOUND})),
    )
        .into_response()
}

/// The original global OPTIONS route: 204 with the two fixed allow lists.
fn options_response() -> Response {
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
fn method_not_allowed() -> Response {
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
