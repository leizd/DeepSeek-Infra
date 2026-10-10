//! `POST /api/file-reader`, `POST /api/file-chunk` and `POST /api/file-page-text`.
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
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::AppError;
use deepseek_policy::file_cache::FileCache;
use deepseek_policy::file_routes::{
    file_chunk, file_page_search, file_page_text, file_reader_window,
};
use serde::Deserialize;
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

/// `POST /api/file-page-text`.
///
/// `page=payload.get("page") or 1`: a falsy page takes page 1, and the policy function
/// still refuses a non-numeric page with `400 Invalid page`. The handler only reads
/// the cache; a refusal and a success both leave the index bytes untouched.
pub async fn api_file_page_text(
    State(state): State<FileReaderRouteState>,
    Json(payload): Json<Value>,
) -> Response {
    let project = project_id(&payload);
    let page = payload.get("page").filter(|value| truthy(value)).cloned();
    match file_page_text(
        &state.root,
        &string_field(&payload, "fileId"),
        project.as_deref(),
        page.as_ref(),
        &state.cache,
    ) {
        Ok(value) => Json(value).into_response(),
        Err(error) => app_error(error),
    }
}

/// `GET /api/file-page-search?fileId=…&projectId=…&query=…`.
///
/// `projectId` uses `get(...) or None`: an empty string selects the global cache, and
/// a whitespace-only id is kept so the id-shape check can refuse it. The query is
/// passed through; a blank one is `400 Search query is required` from the policy
/// function, before any other read side effect.
#[derive(Debug, Default, Deserialize)]
pub struct FilePageSearchQuery {
    #[serde(default, rename = "fileId")]
    file_id: String,
    #[serde(default, rename = "projectId")]
    project_id: Option<String>,
    #[serde(default)]
    query: String,
}

/// `GET /api/file-page-image?fileId=…&page=…&scale=…`.
///
/// A PDF page is rendered to PNG and cached beside the source. A non-PDF, a bad
/// page, or a bad scale is refused before that file is created.
#[derive(Debug, Default, Deserialize)]
pub struct FilePageImageQuery {
    #[serde(default, rename = "fileId")]
    file_id: String,
    #[serde(default, rename = "projectId")]
    project_id: Option<String>,
    #[serde(default)]
    page: String,
    #[serde(default)]
    scale: String,
}

pub async fn api_file_page_image(
    State(state): State<FileReaderRouteState>,
    Query(query): Query<FilePageImageQuery>,
) -> Response {
    let project = query.project_id.filter(|value| !value.is_empty());
    let page = Value::String(if query.page.is_empty() {
        "1".to_string()
    } else {
        query.page
    });
    let scale = Value::String(query.scale);
    let control = deepseek_policy::extraction_control::FileProcessingControl::default();
    let _cancel_on_drop = control.cancel_on_drop();
    let rendered = tokio::task::spawn_blocking(move || {
        control.run(|| {
            deepseek_policy::pdf_page::file_page_image(
                &state.root,
                &query.file_id,
                project.as_deref(),
                Some(&page),
                Some(&scale),
                &state.cache,
            )
        })
    })
    .await;
    match rendered {
        Ok(Ok(image)) => (
            StatusCode::OK,
            [
                (axum::http::header::CONTENT_TYPE, "image/png".to_string()),
                (
                    axum::http::header::HeaderName::from_static("x-file-page"),
                    image.page.to_string(),
                ),
                (
                    axum::http::header::HeaderName::from_static("x-file-page-count"),
                    image.page_count.to_string(),
                ),
                (axum::http::header::CONTENT_DISPOSITION, image.disposition),
                (axum::http::header::CACHE_CONTROL, "no-store".to_string()),
            ],
            image.png,
        )
            .into_response(),
        Ok(Err(error)) => app_error(error),
        Err(_) => app_error(AppError {
            message: "PDF page rendering failed".to_string(),
            code: deepseek_policy::app_error::codes::INTERNAL,
            status: 500,
        }),
    }
}

/// `GET /api/file-page-layout?fileId=…&page=…`.
///
/// Word boxes for one PDF page. The handler does not write a cache file.
#[derive(Debug, Default, Deserialize)]
pub struct FilePageLayoutQuery {
    #[serde(default, rename = "fileId")]
    file_id: String,
    #[serde(default, rename = "projectId")]
    project_id: Option<String>,
    #[serde(default)]
    page: String,
}

pub async fn api_file_page_layout(
    State(state): State<FileReaderRouteState>,
    Query(query): Query<FilePageLayoutQuery>,
) -> Response {
    let project = query.project_id.filter(|value| !value.is_empty());
    let page = Value::String(if query.page.is_empty() {
        "1".to_string()
    } else {
        query.page
    });
    match tokio::task::spawn_blocking(move || {
        deepseek_policy::pdf_page::file_page_layout(
            &state.root,
            &query.file_id,
            project.as_deref(),
            Some(&page),
            &state.cache,
        )
    })
    .await
    {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => app_error(error),
        Err(_) => app_error(AppError {
            message: "PDF page layout failed".to_string(),
            code: deepseek_policy::app_error::codes::INTERNAL,
            status: 500,
        }),
    }
}

pub async fn api_file_page_search(
    State(state): State<FileReaderRouteState>,
    Query(query): Query<FilePageSearchQuery>,
) -> Response {
    let project = query.project_id.filter(|value| !value.is_empty());
    let query_value = Value::String(query.query);
    match file_page_search(
        &state.root,
        &query.file_id,
        project.as_deref(),
        Some(&query_value),
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
