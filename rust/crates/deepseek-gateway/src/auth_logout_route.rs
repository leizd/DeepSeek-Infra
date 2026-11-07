//! `POST /api/auth/logout`.
//!
//! The original handler only authenticates and returns `{"ok": true}` with an
//! already-expired `auth_token` cookie. Non-POST verbs keep their original
//! routing: GET/HEAD reach the `/api/{path:path}` catch-all's flat 404, OPTIONS
//! reaches the global 204, and the remaining verbs get FastAPI's 405.

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use serde_json::json;

/// `expired_auth_cookie_header()` from the Python `http_utils`: the same
/// attributes `SimpleCookie` renders for the cleared token.
const EXPIRED_AUTH_COOKIE: &str = "auth_token=\"\"; HttpOnly; Max-Age=0; Path=/; SameSite=Strict";

pub fn router() -> Router {
    Router::new().route("/api/auth/logout", any(dispatch))
}

async fn dispatch(method: Method, _headers: HeaderMap, _body: Bytes) -> Response {
    if method == Method::POST {
        return logout_response();
    }
    if method == Method::GET || method == Method::HEAD {
        return route_not_found();
    }
    if method == Method::OPTIONS {
        return options_response();
    }
    method_not_allowed()
}

fn logout_response() -> Response {
    let mut response = Json(json!({"ok": true})).into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_static(EXPIRED_AUTH_COOKIE),
    );
    response
}

/// The original `/api/{path:path}` catch-all's GET answer.
fn route_not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"error": "Not found", "code": "not_found"})),
    )
        .into_response()
}

/// The original global OPTIONS route: 204 with both fixed allow lists.
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
    use super::EXPIRED_AUTH_COOKIE;

    #[test]
    fn expired_cookie_is_the_oracle_rendering() {
        assert_eq!(
            EXPIRED_AUTH_COOKIE,
            "auth_token=\"\"; HttpOnly; Max-Age=0; Path=/; SameSite=Strict"
        );
    }
}
