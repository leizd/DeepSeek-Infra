//! `GET /api/mcp`.
//!
//! `mcp_status()` from the original MCP hub: a read-only description of the
//! local Tool Hub and the outbound MCP client configuration. Nothing is written
//! and no socket is opened; every value is derived from the same environment
//! knobs the oracle reads (`MCP_ENABLED`, `MCP_CAPABILITY`, the expose flags and
//! the `MCP_CLIENT_*` family), with the oracle's own precedence and bounds.

use std::collections::HashMap;
use std::env;

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use deepseek_policy::tool_catalog;
use serde_json::{Value, json};

const MCP_PROTOCOL_VERSION: &str = "2025-06-18";
const KNOWN_CAPABILITIES: [&str; 6] = [
    "full",
    "researcher",
    "browser_reader",
    "coder",
    "reasoner",
    "critic",
];

pub fn router() -> Router {
    Router::new().route("/api/mcp", any(dispatch))
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
    let enabled = env_bool("MCP_ENABLED", true);
    let capability = hub_capability();
    let tool_count = if enabled {
        tool_catalog::mcp_tools(&capability).len()
    } else {
        0
    };
    let timeout_seconds = env_int_clamped("MCP_CLIENT_TIMEOUT_SECONDS", 30, 1, 600);
    let (servers, timeouts) = parse_client_servers();
    let mut server_values = Vec::with_capacity(servers.len());
    for (name, url) in servers {
        let timeout = timeouts.get(&name).copied().unwrap_or(timeout_seconds);
        server_values.push(json!({
            "name": name,
            "url": url,
            "timeoutSeconds": timeout,
        }));
    }
    json!({
        "ok": true,
        "mcp": {
            "enabled": enabled,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "endpoint": "/mcp",
            "capability": capability,
            "toolCount": tool_count,
            "exposeResources": env_bool("MCP_EXPOSE_RESOURCES", true),
            "exposePrompts": env_bool("MCP_EXPOSE_PROMPTS", true),
            "client": {
                "enabled": env_bool("MCP_CLIENT_ENABLED", false),
                "servers": server_values,
                "timeoutSeconds": timeout_seconds,
                "maxRetries": env_int_clamped("MCP_CLIENT_MAX_RETRIES", 1, 0, 5),
                "retryBackoffSeconds": env_float_clamped(
                    "MCP_CLIENT_RETRY_BACKOFF_SECONDS",
                    0.25,
                    0.0,
                    10.0,
                ),
                "circuitBreakerFailures": env_int_clamped(
                    "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES",
                    3,
                    1,
                    20,
                ),
                "circuitBreakerResetSeconds": env_int_clamped(
                    "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS",
                    60,
                    1,
                    3600,
                ),
            },
        },
    })
}

/// `hub_capability()`: an unknown profile falls back to `full`.
fn hub_capability() -> String {
    let raw = env::var("MCP_CAPABILITY").unwrap_or_default();
    let trimmed = python_strip(&raw);
    if trimmed.is_empty() {
        return "full".to_string();
    }
    if KNOWN_CAPABILITIES.contains(&trimmed) {
        trimmed.to_string()
    } else {
        "full".to_string()
    }
}

/// `settings.mcp.client_servers` and `settings.mcp.client_server_timeouts`.
///
/// The original parses `MCP_CLIENT_SERVERS` twice: once for the ordered
/// `(name, url)` list and once for the per-server timeout overrides. Invalid
/// items are skipped by both readers; a duplicate name keeps the list entry but
/// the timeout map's last value, matching Python's dict assignment order.
fn parse_client_servers() -> (Vec<(String, String)>, HashMap<String, i64>) {
    let mut servers = Vec::new();
    let mut timeouts = HashMap::new();
    let raw = env::var("MCP_CLIENT_SERVERS").unwrap_or_default();
    let stripped = python_strip(&raw);
    if stripped.is_empty() {
        return (servers, timeouts);
    }
    let Ok(parsed) = serde_json::from_str::<Value>(stripped) else {
        return (servers, timeouts);
    };
    let Some(items) = parsed.as_array() else {
        return (servers, timeouts);
    };
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let name = python_str_or(object.get("name"));
        let url = python_str_or(object.get("url"));
        if name.is_empty() || !(url.starts_with("http://") || url.starts_with("https://")) {
            continue;
        }
        servers.push((name.clone(), url));
        let raw_timeout = object
            .get("timeoutSeconds")
            .or_else(|| object.get("timeout_seconds"));
        let Some(raw_timeout) = raw_timeout else {
            continue;
        };
        let Some(timeout) = python_int_of(raw_timeout) else {
            continue;
        };
        timeouts.insert(name, timeout.clamp(1, 600));
    }
    (servers, timeouts)
}

/// `str(value or "")`, then the reader's `.strip()`.
fn python_str_or(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => python_strip(&value_str(found)).to_string(),
        _ => String::new(),
    }
}

/// `int(str(value))` for the timeout field. Floats and booleans are rejected the
/// way Python's `int()` rejects their `str()` forms.
fn python_int_of(value: &Value) -> Option<i64> {
    python_int(&value_str(value))
}

fn python_int(text: &str) -> Option<i64> {
    let trimmed = python_strip(text);
    if trimmed.is_empty() {
        return None;
    }
    let cleaned: String = trimmed
        .chars()
        .filter(|character| *character != '_')
        .collect();
    cleaned.parse::<i64>().ok()
}

fn env_bool(name: &str, default: bool) -> bool {
    let Ok(raw) = env::var(name) else {
        return default;
    };
    if python_strip(&raw).is_empty() {
        return default;
    }
    matches!(
        python_strip(&raw).to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn env_int_clamped(name: &str, default: i64, minimum: i64, maximum: i64) -> i64 {
    let Ok(raw) = env::var(name) else {
        return default;
    };
    let trimmed = python_strip(&raw);
    if trimmed.is_empty() {
        return default;
    }
    python_int(trimmed)
        .unwrap_or(default)
        .clamp(minimum, maximum)
}

fn env_float_clamped(name: &str, default: f64, minimum: f64, maximum: f64) -> f64 {
    let Ok(raw) = env::var(name) else {
        return default;
    };
    let trimmed = python_strip(&raw);
    if trimmed.is_empty() {
        return default;
    }
    python_float(trimmed)
        .unwrap_or(default)
        .clamp(minimum, maximum)
}

fn python_float(text: &str) -> Option<f64> {
    let trimmed = python_strip(text);
    if trimmed.is_empty() {
        return None;
    }
    let cleaned: String = trimmed
        .chars()
        .filter(|character| *character != '_')
        .collect();
    cleaned.parse::<f64>().ok()
}

/// `str.strip()`: Unicode whitespace plus `\x1c`-`\x1f`, which Rust's
/// `char::is_whitespace` excludes but Python's `str.isspace` includes.
fn python_strip(value: &str) -> &str {
    fn is_whitespace(character: char) -> bool {
        character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
    }
    value.trim_matches(is_whitespace)
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
    use super::{python_int, python_strip};

    #[test]
    fn python_strip_covers_the_extra_control_whitespace() {
        assert_eq!(python_strip(" \u{1c}\tvalue \u{1f} "), "value");
        assert_eq!(python_strip("\u{1c}\u{1d}\u{1e}\u{1f}"), "");
        assert_eq!(python_strip(" value "), "value");
    }

    #[test]
    fn python_int_accepts_python_prefixed_and_underscored_integers() {
        assert_eq!(python_int(" 10 "), Some(10));
        assert_eq!(python_int("+10"), Some(10));
        assert_eq!(python_int("-10"), Some(-10));
        assert_eq!(python_int("1_0"), Some(10));
        assert_eq!(python_int("10.0"), None);
        assert_eq!(python_int("True"), None);
        assert_eq!(python_int(""), None);
    }
}
