//! `POST /api/file-reader` and `POST /api/file-chunk` — the paginated file reader.
//!
//! The frontend's file viewer scrolls a long extraction one window at a time
//! (`file-reader`) and can jump to a single chunk (`file-chunk`). Both fell through to
//! the Go `/api/*` catch-all, so both were `503` on the native edge.
//!
//! The windowing rules live in `deepseek_policy::file_routes` (`file_reader_window`,
//! `file_chunk`, `reader_positive_int`), where they are unit-tested against the oracle's
//! own edge cases: the 1-based display indices, the 12-chunk cap, the clamp of a start
//! past the end, the empty-list shape, and the malformed-entry skip. This module parses
//! the request body and reports the two `AppError` envelopes.
//!
//! # The two error envelopes are the oracle's
//!
//! `/api/file-reader` raises `400 invalid_payload` for a non-numeric `chunkStart` or
//! `chunkCount` (message `Invalid reader start` / `Invalid reader count`).
//! `/api/file-chunk` raises `400 invalid_payload` (`Invalid chunk index`) for a
//! non-numeric index and `404 not_found` (`Chunk not found`) past the end. Both are
//! reported as `{"error": message, "code": code}`, which is `AppError.to_response()`.

use std::path::PathBuf;
use std::sync::Arc;

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::AppError;
use deepseek_policy::file_cache::FileCache;
use deepseek_policy::file_routes::{file_chunk, file_reader_window};
use serde_json::{Value, json};

/// The workspace root plus the per-process file index cache, shared with
/// `/api/file-source`.
#[derive(Clone)]
pub struct FileReaderRouteState {
    pub root: PathBuf,
    pub cache: Arc<FileCache>,
}

impl FileReaderRouteState {
    pub fn from_env() -> Self {
        Self {
            root: std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".")),
            cache: Arc::new(FileCache::new()),
        }
    }
}

/// `str(payload.get(key) or "") or None` — the project id, with the oracle's two-step
/// fallback: an empty string becomes `None`, which selects the global cache directory.
fn project_id(payload: &Value) -> Option<String> {
    let raw = deepseek_policy::core_utils::text_or_empty(payload.get("projectId"));
    if raw.is_empty() { None } else { Some(raw) }
}

fn string_field(payload: &Value, key: &str) -> String {
    deepseek_policy::core_utils::text_or_empty(payload.get(key))
}

/// `POST /api/file-reader`.
pub async fn api_file_reader(
    State(state): State<FileReaderRouteState>,
    Json(payload): Json<Value>,
) -> Response {
    let project = project_id(&payload);
    // `payload.get("chunkStart") or 1` / `or 6`: a falsy value takes the default, which
    // is why the raw value is passed through rather than defaulted here.
    let chunk_start = payload
        .get("chunkStart")
        .filter(|value| truthy(value))
        .cloned();
    let chunk_count = payload
        .get("chunkCount")
        .filter(|value| truthy(value))
        .cloned();
    match file_reader_window(
        &state.root,
        &string_field(&payload, "fileId"),
        project.as_deref(),
        chunk_start.as_ref(),
        chunk_count.as_ref(),
        &state.cache,
    ) {
        Ok(value) => Json(value).into_response(),
        Err(error) => app_error(error),
    }
}

/// `POST /api/file-chunk`.
pub async fn api_file_chunk(
    State(state): State<FileReaderRouteState>,
    Json(payload): Json<Value>,
) -> Response {
    let project = project_id(&payload);
    match file_chunk(
        &state.root,
        &string_field(&payload, "fileId"),
        project.as_deref(),
        payload.get("chunkIndex"),
        &state.cache,
    ) {
        Ok(value) => Json(value).into_response(),
        Err(error) => app_error(error),
    }
}

/// `value or default` — Python truthiness, because the oracle's `or` is what selects
/// the default here.
fn truthy(value: &Value) -> bool {
    deepseek_policy::core_utils::python_truthy(value)
}

/// The oracle's `AppError` envelope: `{"error": message, "code": code}`.
fn app_error(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
