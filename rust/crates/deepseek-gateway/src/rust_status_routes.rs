//! `GET /api/rust/status`.
//!
//! `rust_status()` from `RustRegistry`. Nothing is written. A disabled gateway
//! reports `healthy: false` and an empty url without opening a socket. An
//! enabled gateway checks `GET /healthz` the way `http.client` does: direct
//! connection, no proxy, no redirect, two-second timeout, true only for HTTP 200.

use std::time::Duration;

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use serde_json::{Value, json};

const DEFAULT_GATEWAY_URL: &str = "http://127.0.0.1:8787";

pub fn router() -> Router {
    Router::new().route("/api/rust/status", any(dispatch))
}

async fn dispatch(method: Method, _headers: HeaderMap, _body: Bytes) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return status_response(method).await;
    }
    method_not_allowed()
}

async fn status_response(method: Method) -> Response {
    let joined = tokio::task::spawn_blocking(build_body).await;
    match joined {
        Ok(_body) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => Json(body).into_response(),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => error_response(server_error()),
    }
}

fn build_body() -> Value {
    let gateway_enabled = rust_env_bool("DEEPSEEK_RUST_GATEWAY", false);
    let gateway_url = rust_gateway_url();
    let healthy = if gateway_enabled {
        check_rust_gateway_health(&gateway_url)
    } else {
        false
    };
    json!({
        "ok": true,
        "rust": {
            "enabled": {
                "gateway": gateway_enabled,
                "mcp": rust_env_bool("DEEPSEEK_RUST_MCP", false),
                "policy": rust_env_bool("DEEPSEEK_RUST_POLICY", false),
                "rag": rust_env_bool("DEEPSEEK_RUST_RAG", false),
            },
            "components": {
                "gateway": {
                    "enabled": gateway_enabled,
                    "url": if gateway_enabled { gateway_url } else { String::new() },
                    "healthy": healthy,
                },
            },
        },
    })
}

/// `rust_core.config._env_bool`. A whitespace-only value is false, not the default.
/// An empty or missing value uses the default.
fn rust_env_bool(name: &str, default: bool) -> bool {
    let Ok(value) = std::env::var(name) else {
        return default;
    };
    if value.is_empty() {
        return default;
    }
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn rust_gateway_url() -> String {
    let raw = std::env::var("DEEPSEEK_RUST_GATEWAY_URL").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        DEFAULT_GATEWAY_URL.to_string()
    } else {
        trimmed.to_string()
    }
}

fn check_rust_gateway_health(url: &str) -> bool {
    let Ok(parsed) = reqwest::Url::parse(url) else {
        return false;
    };
    let scheme = parsed.scheme();
    if scheme != "http" && scheme != "https" {
        return false;
    }
    let host = match parsed.host_str() {
        Some(host) if !host.is_empty() => host.to_string(),
        _ => "127.0.0.1".to_string(),
    };
    let port = parsed
        .port()
        .unwrap_or(if scheme == "https" { 443 } else { 80 });
    let bracketed = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    let target = format!("{scheme}://{bracketed}:{port}/healthz");
    let Ok(client) = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs_f64(2.0))
        .connect_timeout(Duration::from_secs_f64(2.0))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
    else {
        return false;
    };
    match client.get(target).send() {
        Ok(response) => response.status().as_u16() == 200,
        Err(_) => false,
    }
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
    response
}

fn server_error() -> AppError {
    AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    }
}

fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::{rust_env_bool, rust_gateway_url};

    #[test]
    fn blank_gateway_url_falls_back_without_connecting() {
        let previous = std::env::var("DEEPSEEK_RUST_GATEWAY_URL").ok();
        unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY_URL", "   ") };
        assert_eq!(rust_gateway_url(), "http://127.0.0.1:8787");
        unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY_URL", " http://127.0.0.1:9/x ") };
        assert_eq!(rust_gateway_url(), "http://127.0.0.1:9/x");
        match previous {
            Some(value) => unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY_URL", value) },
            None => unsafe { std::env::remove_var("DEEPSEEK_RUST_GATEWAY_URL") },
        }
    }

    #[test]
    fn whitespace_flag_is_false_not_the_default() {
        let previous = std::env::var("DEEPSEEK_RUST_GATEWAY").ok();
        unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY", "   ") };
        assert!(!rust_env_bool("DEEPSEEK_RUST_GATEWAY", true));
        unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY", "") };
        assert!(rust_env_bool("DEEPSEEK_RUST_GATEWAY", true));
        match previous {
            Some(value) => unsafe { std::env::set_var("DEEPSEEK_RUST_GATEWAY", value) },
            None => unsafe { std::env::remove_var("DEEPSEEK_RUST_GATEWAY") },
        }
    }
}
