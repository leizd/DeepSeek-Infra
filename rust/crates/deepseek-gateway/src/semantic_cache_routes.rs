//! Read-only `GET /api/semantic-cache/status` (`semantic_cache.status()`).
//!
//! Mirrors Python without ever creating `.semantic-cache` or `cache.sqlite3`:
//! when the database is absent or `SEMANTIC_CACHE_ENABLED=0`, counts remain 0.
//! Embedding provider and ONNX fallback errors mirror `local_rag.EmbeddingPipeline`.

use std::path::PathBuf;

use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::AppError;
use serde_json::{Value, json};

use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::edge_status_routes::python_module_available;
use crate::route_support;
use crate::scheduler_routes::{env_bool, env_float_clamped, env_int_clamped, python_path_str};

const DEFAULT_EMBEDDING_DIMENSIONS: i64 = 128;

pub fn router() -> Router {
    Router::new().route("/api/semantic-cache/status", any(dispatch))
}

async fn dispatch(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(build_body).await;
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => axum::http::StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => {
            axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn cache_version_env() -> String {
    let raw = std::env::var("SEMANTIC_CACHE_VERSION").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        "1".to_string()
    } else {
        trimmed.to_string()
    }
}

fn embedding_pipeline_info() -> (String, i64, String) {
    let requested = std::env::var("LOCAL_RAG_EMBEDDING_PROVIDER")
        .unwrap_or_else(|_| "hash".to_string())
        .trim()
        .to_ascii_lowercase();
    let dimensions = env_int_clamped(
        "LOCAL_RAG_EMBEDDING_DIMENSIONS",
        DEFAULT_EMBEDDING_DIMENSIONS,
        16,
        4_096,
    );
    if requested != "onnx" {
        return ("hash".to_string(), dimensions, String::new());
    }
    let model_path = std::env::var("LOCAL_RAG_ONNX_MODEL_PATH")
        .unwrap_or_default()
        .trim()
        .to_string();
    if model_path.is_empty() || !std::path::Path::new(&model_path).exists() {
        return (
            "hash".to_string(),
            dimensions,
            "LOCAL_RAG_ONNX_MODEL_PATH is not configured or does not exist".to_string(),
        );
    }
    let tokenizer_path = std::env::var("LOCAL_RAG_TOKENIZER_PATH")
        .unwrap_or_default()
        .trim()
        .to_string();
    if tokenizer_path.is_empty() || !std::path::Path::new(&tokenizer_path).exists() {
        return (
            "hash".to_string(),
            dimensions,
            "LOCAL_RAG_TOKENIZER_PATH is not configured or does not exist".to_string(),
        );
    }
    for (import_name, pip_name) in [
        ("numpy", "numpy"),
        ("onnxruntime", "onnxruntime"),
        ("tokenizers", "tokenizers"),
    ] {
        if !python_module_available(import_name) {
            return (
                "hash".to_string(),
                dimensions,
                format!("optional embedding dependency is missing: {pip_name}"),
            );
        }
    }
    ("onnx".to_string(), dimensions, String::new())
}

pub(crate) fn semantic_cache_status_value(root: &std::path::Path) -> Value {
    let enabled = env_bool("SEMANTIC_CACHE_ENABLED", true);
    let threshold = env_float_clamped("SEMANTIC_CACHE_THRESHOLD", 0.95, 0.5, 0.999);
    let ttl_seconds = env_int_clamped(
        "SEMANTIC_CACHE_TTL_SECONDS",
        7 * 24 * 60 * 60,
        60,
        31_536_000,
    );
    let max_items = env_int_clamped("SEMANTIC_CACHE_MAX_ITEMS", 1_000, 10, 50_000);
    let min_quality = env_float_clamped("SEMANTIC_CACHE_MIN_QUALITY", 0.3, 0.0, 1.0);
    let cache_attachments = env_bool("SEMANTIC_CACHE_ATTACHMENTS", true);
    let db_path: PathBuf = root.join(".semantic-cache").join("cache.sqlite3");

    let mut items: i64 = 0;
    let mut hits: i64 = 0;
    let mut cache_err = String::new();
    if enabled && db_path.is_file() {
        match open_readonly_sqlite(&db_path) {
            Ok(Some(conn)) => {
                match conn.query_row(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(hit_count), 0) AS h FROM semantic_cache_items",
                    [],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
                ) {
                    Ok((c, h)) => {
                        items = c;
                        hits = h;
                    }
                    Err(err) => {
                        // A freshly created DB without table yet mirrors sqlite error capture cleanly
                        let msg = err.to_string();
                        if !msg.contains("no such table") {
                            cache_err = format!("semantic cache status failed: {msg}");
                        }
                    }
                }
            }
            Ok(None) => {}
            Err(_) => {
                cache_err = "semantic cache status failed: unable to open database".to_string();
            }
        }
    }

    let (provider, dimensions, pipeline_err) = embedding_pipeline_info();
    let cache_version = format!("{}:{}:{}", cache_version_env(), provider, dimensions);
    let last_error = if !cache_err.is_empty() {
        cache_err
    } else {
        pipeline_err
    };

    json!({
        "enabled": enabled,
        "databasePath": python_path_str(&db_path),
        "similarityThreshold": threshold,
        "ttlSeconds": ttl_seconds,
        "maxItems": max_items,
        "items": items,
        "hits": hits,
        "embeddingProvider": provider,
        "embeddingDimensions": dimensions,
        "cacheVersion": cache_version,
        "minQualityScore": min_quality,
        "cacheAttachments": cache_attachments,
        "lastError": last_error,
    })
}

fn build_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    Ok(json!({
        "ok": true,
        "semanticCache": semantic_cache_status_value(&root),
    }))
}
