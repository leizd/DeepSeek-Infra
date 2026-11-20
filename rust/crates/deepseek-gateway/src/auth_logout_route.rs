//! `POST /api/auth/logout`.
//!
//! The original handler only authenticates and returns `{"ok": true}` with an
//! already-expired `auth_token` cookie. Non-POST verbs keep their original
//! routing: GET/HEAD reach the `/api/{path:path}` catch-all's flat 404, OPTIONS
//! reaches the global 204, and the remaining verbs get FastAPI's 405.

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use serde_json::json;

use crate::route_support;

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
        return route_support::route_not_found();
    }
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    route_support::method_not_allowed()
}

fn logout_response() -> Response {
    let mut response = Json(json!({"ok": true})).into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_static(EXPIRED_AUTH_COOKIE),
    );
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
