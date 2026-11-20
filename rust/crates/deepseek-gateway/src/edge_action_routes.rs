//! `POST /api/edge/reload` and `POST /api/edge/route-preview`.
//!
//! Mirrors `edge.py` + `edge_inference.py` without starting a Python interpreter
//! or loading local GGUF/MLC weights into the gateway process. `reload` confirms
//! that no local backend is pinned in memory (`loaded: false`) and returns the
//! current `edgeInference` status; `route-preview` evaluates the exact routing
//! decision tree (`cloud_forced`, `local_forced`, `edge_unavailable`,
//! `unsupported_payload`, `cloud_unavailable_simple_local`, `simple_task_local`,
//! `complex_task_cloud`).

use std::sync::OnceLock;

use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{latest_user_query, python_truthy};
use deepseek_policy::edge_inference::{
    artifact_query_regex, chat_messages_from_payload, complex_query_regex, has_image_attachment,
    simple_task_regex,
};
use deepseek_policy::python_json::value_str;
use regex::Regex;
use serde_json::{Value, json};

use crate::edge_status_routes::{build_body as edge_status_body, status_for_payload};
use crate::route_support;

const MAX_JSON_BODY_BYTES: i64 = 2_000_000;
const EDGE_CLOUD_MODES: [&str; 5] = ["cloud", "remote", "off", "none", "disabled"];
const EDGE_LOCAL_MODES: [&str; 3] = ["local", "edge", "on"];
const EDGE_SETTINGS_MODES: [&str; 5] = ["auto", "local", "cloud", "edge", "off"];

pub const CURRENT_QUERY_PATTERN: &str = r"\b(latest|today|current|news|price|weather|release|version|search|browse|internet|web)\b|最新|今天|新闻|价格|天气|搜索|联网|浏览";

pub fn router() -> Router {
    Router::new()
        .route("/api/edge/reload", any(dispatch_reload))
        .route("/api/edge/route-preview", any(dispatch_route_preview))
}

async fn dispatch_reload(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_JSON_BODY_BYTES) {
        Ok(object) => object,
        Err(error) => return route_support::app_error_response(error),
    };
    let action = payload_truthy_str(payload.get("action"), "unload")
        .trim()
        .to_ascii_lowercase();
    if action != "unload" && action != "reload" {
        return route_support::app_error_response(AppError {
            message: "Unsupported edge action".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let joined = tokio::task::spawn_blocking(edge_status_body).await;
    match joined {
        Ok(body) => Json(body).into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_route_preview(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_JSON_BODY_BYTES) {
        Ok(object) => Value::Object(object),
        Err(error) => return route_support::app_error_response(error),
    };
    let joined = tokio::task::spawn_blocking(move || route_preview(&payload)).await;
    match joined {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn current_query_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(&format!("(?i){CURRENT_QUERY_PATTERN}")).expect("static pattern must compile")
    })
}

fn payload_truthy_str(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => fallback.to_string(),
    }
}

fn settings_edge_mode() -> String {
    let raw = std::env::var("EDGE_MODE")
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if EDGE_SETTINGS_MODES.contains(&raw.as_str()) {
        raw
    } else {
        "auto".to_string()
    }
}

fn edge_mode(payload: &Value) -> String {
    let raw = if payload.get("edgeMode").is_some_and(python_truthy) {
        value_str(payload.get("edgeMode").unwrap_or(&Value::Null))
    } else if payload.get("localMode").is_some_and(python_truthy) {
        value_str(payload.get("localMode").unwrap_or(&Value::Null))
    } else {
        settings_edge_mode()
    };
    let normalized = raw.trim().to_ascii_lowercase();
    if normalized.is_empty() {
        "auto".to_string()
    } else {
        normalized
    }
}

fn edge_payload_supported(payload: &Value) -> bool {
    if matches!(payload.get("agentMode"), Some(Value::Bool(true))) {
        return false;
    }
    let search_mode = payload_truthy_str(payload.get("searchMode"), "")
        .trim()
        .to_ascii_lowercase();
    if matches!(search_mode.as_str(), "on" | "force" | "true" | "1") {
        return false;
    }
    !has_image_attachment(payload)
}

fn edge_simple_enough(payload: &Value, simple_max_chars: usize) -> bool {
    let query = latest_user_query(payload);
    if query.is_empty() {
        return false;
    }
    if query.chars().count() > simple_max_chars {
        return false;
    }
    if current_query_regex().is_match(&query)
        || complex_query_regex().is_match(&query)
        || artifact_query_regex().is_match(&query)
    {
        return false;
    }
    if simple_task_regex().is_match(&query) {
        return true;
    }
    query.chars().count() <= 800 && chat_messages_from_payload(payload).len() <= 8
}

fn cloud_api_key_available(payload: &Value) -> bool {
    let key = if payload.get("apiKey").is_some_and(python_truthy) {
        value_str(payload.get("apiKey").unwrap_or(&Value::Null))
    } else {
        std::env::var("DEEPSEEK_API_KEY").unwrap_or_default()
    };
    !key.trim().is_empty()
}

fn edge_unavailable_message(status: &Value) -> String {
    if !status
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return "Edge inference is disabled. Set EDGE_INFERENCE_ENABLED=1 to enable local model routing."
            .to_string();
    }
    if !status
        .get("providerSupported")
        .and_then(Value::as_bool)
        .unwrap_or(true)
    {
        let provider = status
            .get("provider")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .unwrap_or("unknown");
        return format!(
            "Unsupported edge inference provider: {provider}. Use llama_cpp, mlc, or fake."
        );
    }
    if !status
        .get("dependencyAvailable")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        let provider = status
            .get("provider")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .unwrap_or("llama_cpp");
        let requirement = if provider == "llama_cpp" {
            "llama-cpp-python"
        } else {
            "mlc-llm"
        };
        return format!(
            "Edge inference dependency is not installed. Install {requirement} for provider {provider}."
        );
    }
    if !status
        .get("modelPathConfigured")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return "Edge model path is not configured. Set EDGE_MODEL_PATH to a local model path."
            .to_string();
    }
    if !status
        .get("modelPathSuffixSupported")
        .and_then(Value::as_bool)
        .unwrap_or(true)
    {
        return "Edge model path must point to a .gguf file for the llama_cpp provider."
            .to_string();
    }
    if !status
        .get("modelPathExists")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return "Edge model path does not exist or is not a supported GGUF file.".to_string();
    }
    "Edge inference is unavailable.".to_string()
}

pub(crate) fn route_preview(payload: &Value) -> Result<Value, AppError> {
    let (status, simple_max_chars) = status_for_payload(payload);
    let mode = edge_mode(payload);
    let provider = status
        .get("provider")
        .and_then(Value::as_str)
        .unwrap_or("llama_cpp")
        .to_string();
    let status_available = status
        .get("available")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let cloud_available = cloud_api_key_available(payload);

    let (use_edge, reason) = if EDGE_CLOUD_MODES.contains(&mode.as_str()) {
        (false, "cloud_forced")
    } else if EDGE_LOCAL_MODES.contains(&mode.as_str()) {
        if !status_available {
            return Err(AppError {
                message: edge_unavailable_message(&status),
                code: codes::INVALID_PAYLOAD,
                status: 409,
            });
        }
        (true, "local_forced")
    } else if !status_available {
        (false, "edge_unavailable")
    } else if !edge_payload_supported(payload) {
        (false, "unsupported_payload")
    } else if !cloud_available && edge_simple_enough(payload, simple_max_chars) {
        (true, "cloud_unavailable_simple_local")
    } else if edge_simple_enough(payload, simple_max_chars) {
        (true, "simple_task_local")
    } else {
        (false, "complex_task_cloud")
    };

    Ok(json!({
        "useEdge": use_edge,
        "reason": reason,
        "mode": mode,
        "provider": provider,
        "status": status,
    }))
}
