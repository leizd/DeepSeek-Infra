//! `POST /api/fetch-url`.
//!
//! The original handler reads a JSON object, coerces `url` with Python's
//! `str(value or "")`, and returns `{"ok": true, "page": fetch_url(url)}`. The
//! SSRF check, pinned-address connection, redirect revalidation, cache and HTML
//! extraction live in `deepseek_policy::fetch_url` (probe-verified); this module
//! is the HTTP wiring and the original `read_json_body` contract around it.

use std::path::Path;

use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::text_or_empty;
use deepseek_policy::fetch_url::{self, FetchContext};
use serde_json::{Value, json};

use crate::route_support;

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
        return route_support::route_not_found();
    }
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method != Method::POST {
        return route_support::method_not_allowed();
    }
    let root = crate::data_routes::workspace_root();
    let joined = tokio::task::spawn_blocking(move || fetch_page(&headers, &body, &root)).await;
    match joined {
        Ok(Ok(page)) => Json(json!({"ok": true, "page": page})).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn fetch_page(headers: &HeaderMap, body: &[u8], root: &Path) -> Result<Value, AppError> {
    let object = route_support::read_json_object(headers, body, MAX_JSON_BODY_BYTES)?;
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
