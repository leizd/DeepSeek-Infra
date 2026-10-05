//! `GET /api/edge/status`.
//!
//! `edge_inference_status()` with an empty payload, so every field comes from
//! the `EDGE_*` settings. Nothing is written. `dependencyAvailable` follows
//! `importlib.util.find_spec` for `llama_cpp` and `mlc_llm` by searching the
//! path entries Python would search. This route does not start an interpreter.
//! `loaded` stays false because this process does not load a model.

use std::path::{Path, PathBuf};

use axum::body::Bytes;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use serde_json::{Value, json};

use crate::scheduler_routes::{
    env_bool, env_float_clamped, env_int_clamped, env_int_min, python_path_str, python_resolve,
};

const PROVIDERS: [&str; 3] = ["llama_cpp", "mlc", "fake"];

pub fn router() -> Router {
    Router::new().route("/api/edge/status", any(dispatch))
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
    let provider = configured_provider();
    let model_path = configured_model_path(&provider);
    let enabled = env_bool("EDGE_INFERENCE_ENABLED", false);
    let provider_supported = PROVIDERS.contains(&provider.as_str());
    let dependency_available = dependency_available(&provider);
    let model_path_configured = !model_path.is_empty() || provider == "fake";
    let model_path_suffix_supported = suffix_supported(&provider, &model_path);
    let model_path_exists = path_exists(&provider, &model_path);
    let available = enabled
        && provider_supported
        && dependency_available
        && model_path_configured
        && model_path_exists;
    let edge = json!({
        "enabled": enabled,
        "provider": provider,
        "providerSupported": provider_supported,
        "available": available,
        "dependencyAvailable": dependency_available,
        "modelPathConfigured": model_path_configured,
        "modelPathExists": model_path_exists,
        "modelPathSuffixSupported": model_path_suffix_supported,
        "modelPath": model_path,
        "modelName": configured_model_name(),
        "loaded": false,
        "quantization": infer_quantization(&model_path),
        "nCtx": env_int_clamped("EDGE_N_CTX", 4096, 512, 262_144),
        "nThreads": env_int_min("EDGE_N_THREADS", 0, 0),
        "nGpuLayers": env_int_min("EDGE_N_GPU_LAYERS", 0, 0),
        "maxTokens": env_int_clamped("EDGE_MAX_TOKENS", 1024, 16, 16_384),
        "temperature": env_float_clamped("EDGE_TEMPERATURE", 0.7, 0.0, 2.0),
        "topP": env_float_clamped("EDGE_TOP_P", 0.95, 0.05, 1.0),
        "allowModelPathOverride": env_bool("EDGE_ALLOW_MODEL_PATH_OVERRIDE", false),
    });
    let mut edge = edge;
    if let Value::Object(fields) = &mut edge {
        fields.insert(
            "suggestions".to_string(),
            Value::Array(suggestions(fields).into_iter().map(Value::String).collect()),
        );
    }
    json!({"ok": true, "edgeInference": edge})
}

/// Settings coerce the provider before `normalize_provider`. `dry_run` is not
/// in that set, so the route reports `llama_cpp`, not `fake`.
fn configured_provider() -> String {
    let inference = std::env::var("EDGE_INFERENCE_PROVIDER").unwrap_or_default();
    let fallback = std::env::var("EDGE_PROVIDER").unwrap_or_default();
    let selected = if !inference.trim().is_empty() {
        inference
    } else {
        fallback
    };
    let raw = selected.trim().to_ascii_lowercase();
    let chosen = if PROVIDERS.contains(&raw.as_str()) {
        raw
    } else {
        "llama_cpp".to_string()
    };
    normalize_provider(&chosen)
}

fn normalize_provider(value: &str) -> String {
    let raw = value.trim().to_ascii_lowercase().replace('-', "_");
    if matches!(raw.as_str(), "llama" | "llamacpp" | "llama_cpp" | "gguf") {
        return "llama_cpp".to_string();
    }
    if matches!(raw.as_str(), "mlc" | "mlc_llm" | "mlcllm") {
        return "mlc".to_string();
    }
    if matches!(raw.as_str(), "fake" | "test" | "dry_run" | "dryrun") {
        return "fake".to_string();
    }
    raw
}

fn configured_model_path(provider: &str) -> String {
    let trimmed = std::env::var("EDGE_MODEL_PATH").unwrap_or_default();
    let trimmed = trimmed.trim();
    if provider == "llama_cpp" && !trimmed.is_empty() {
        return python_path_str(&python_resolve(Path::new(trimmed)));
    }
    trimmed.to_string()
}

fn configured_model_name() -> String {
    let raw = std::env::var("EDGE_MODEL_NAME").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        "deepseek-r1-distill-local".to_string()
    } else {
        trimmed.to_string()
    }
}

fn dependency_available(provider: &str) -> bool {
    match provider {
        "llama_cpp" => python_module_available("llama_cpp"),
        "mlc" => python_module_available("mlc_llm"),
        "fake" => true,
        _ => false,
    }
}

fn suffix_supported(provider: &str, model_path: &str) -> bool {
    if provider == "llama_cpp" && !model_path.is_empty() {
        return gguf_suffix(model_path);
    }
    true
}

fn path_exists(provider: &str, model_path: &str) -> bool {
    if provider == "fake" {
        return true;
    }
    if model_path.is_empty() {
        return false;
    }
    if provider == "llama_cpp" {
        return Path::new(model_path).is_file() && gguf_suffix(model_path);
    }
    provider == "mlc"
}

fn gguf_suffix(model_path: &str) -> bool {
    Path::new(model_path)
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("gguf"))
}

fn suggestions(fields: &serde_json::Map<String, Value>) -> Vec<String> {
    let enabled = fields
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let provider_supported = fields
        .get("providerSupported")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let dependency = fields
        .get("dependencyAvailable")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let configured = fields
        .get("modelPathConfigured")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let suffix_ok = fields
        .get("modelPathSuffixSupported")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let exists = fields
        .get("modelPathExists")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let provider = fields
        .get("provider")
        .and_then(Value::as_str)
        .unwrap_or("llama_cpp");
    let mut lines = Vec::new();
    if !enabled {
        lines.push("Set EDGE_INFERENCE_ENABLED=1 to enable local model routing.".to_string());
    }
    if !provider_supported {
        lines.push(
            "Set EDGE_PROVIDER or EDGE_INFERENCE_PROVIDER to llama_cpp, mlc, or fake.".to_string(),
        );
    }
    if enabled && provider_supported && !dependency {
        let requirement = if provider == "llama_cpp" {
            "llama-cpp-python"
        } else {
            "mlc-llm"
        };
        lines.push(format!(
            "Install {requirement} or choose another edge provider."
        ));
    }
    if enabled && !configured {
        lines.push("Set EDGE_MODEL_PATH to a local model file.".to_string());
    }
    if enabled && !suffix_ok {
        lines.push("Use a .gguf model file with the llama_cpp provider.".to_string());
    }
    if enabled && configured && !exists {
        lines.push("Check EDGE_MODEL_PATH exists and is readable.".to_string());
    }
    lines
}

/// `(?:^|[-_.])(Q[234568](?:_[A-Z0-9]+)*)(?:[-_.]|$)`, case-insensitive.
fn infer_quantization(model_path: &str) -> String {
    let name = Path::new(model_path)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    let bytes = name.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index].eq_ignore_ascii_case(&b'q') {
            let before_ok = index == 0 || matches!(bytes[index - 1], b'-' | b'_' | b'.');
            if before_ok {
                if let Some(end) = quant_token_end(&bytes[index..]) {
                    let after = index + end;
                    let after_ok =
                        after == bytes.len() || matches!(bytes[after], b'-' | b'_' | b'.');
                    if after_ok {
                        return name[index..after].to_ascii_uppercase();
                    }
                }
            }
        }
        index += 1;
    }
    String::new()
}

fn quant_token_end(bytes: &[u8]) -> Option<usize> {
    if bytes.len() < 2 || !bytes[0].eq_ignore_ascii_case(&b'q') {
        return None;
    }
    if !matches!(
        bytes[1].to_ascii_uppercase(),
        b'2' | b'3' | b'4' | b'5' | b'6' | b'8'
    ) {
        return None;
    }
    let mut end = 2;
    while end < bytes.len() && bytes[end] == b'_' {
        let mut cursor = end + 1;
        if cursor >= bytes.len() || !bytes[cursor].is_ascii_alphanumeric() {
            break;
        }
        while cursor < bytes.len() && bytes[cursor].is_ascii_alphanumeric() {
            cursor += 1;
        }
        end = cursor;
    }
    Some(end)
}

/// `importlib.util.find_spec(name) is not None` for a top-level module.
/// Searches the working directory, `PYTHONPATH`, and `site-packages` next to
/// `python` executables on `PATH`. It does not execute Python.
fn python_module_available(name: &str) -> bool {
    module_search_dirs()
        .iter()
        .any(|dir| module_in_dir(dir, name))
}

fn module_search_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        dirs.push(cwd);
    }
    if let Ok(raw) = std::env::var("PYTHONPATH") {
        let separator = if cfg!(windows) { ';' } else { ':' };
        for part in raw.split(separator) {
            let part = part.trim();
            if !part.is_empty() {
                dirs.push(PathBuf::from(part));
            }
        }
    }
    dirs.extend(site_package_dirs());
    dirs
}

fn site_package_dirs() -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Some(path) = std::env::var_os("PATH") else {
        return found;
    };
    for dir in std::env::split_paths(&path) {
        for exe_name in ["python.exe", "python3.exe", "python", "python3"] {
            let exe = dir.join(exe_name);
            if !exe.is_file() {
                continue;
            }
            let Some(parent) = exe.parent() else {
                continue;
            };
            found.push(parent.join("Lib").join("site-packages"));
            found.push(parent.join("lib").join("site-packages"));
            if let Some(root) = parent.parent() {
                found.push(root.join("Lib").join("site-packages"));
                found.push(root.join("lib").join("site-packages"));
            }
        }
    }
    found
}

fn module_in_dir(dir: &Path, name: &str) -> bool {
    if !dir.is_dir() {
        return false;
    }
    if dir.join(name).is_dir() || dir.join(format!("{name}.py")).is_file() {
        return true;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    let prefix = format!("{}.", name.to_ascii_lowercase());
    for entry in entries.flatten() {
        let Some(text) = entry.file_name().to_str().map(str::to_ascii_lowercase) else {
            continue;
        };
        if text.starts_with(&prefix) && (text.ends_with(".pyd") || text.ends_with(".so")) {
            return true;
        }
    }
    false
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
