//! `GET /api/file-source` — the original uploaded bytes of a cached file.
//!
//! The frontend's file viewer asks for this when the user opens the original rather
//! than the extracted text. It fell through to the Go `/api/*` catch-all, so it was a
//! `503` on the native edge.
//!
//! # Everything security-relevant is in `deepseek-policy`
//!
//! `cached_file_source` validates the file id (`[0-9a-f]{32}`, the same rule the
//! generated-file download uses), resolves the project-scoped or global cache
//! directory, and reports a missing source as `410 file_index_expired` rather than
//! `404` — the index exists, the bytes are gone, and the two are different repairs for
//! the user. `content_disposition_header` and `original_file_media_type` are the
//! oracle's own rules. This module reads the bytes and writes the four headers.
//!
//! # The `download` parameter is a string parse
//!
//! `truthy(query_params.get("download", ""))` is the **web layer's** helper
//! (`1/true/yes/on`), not Python truthiness, so `download=false` is inline. That
//! distinction was measured while porting the download route and is applied here.

use std::path::PathBuf;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::web_truthy;
use deepseek_policy::file_cache::FileCache;
use deepseek_policy::file_routes::{
    cached_file_source, content_disposition_header, original_file_media_type,
};
use serde::Deserialize;
use serde_json::{Value, json};

/// The query the frontend sends.
#[derive(Debug, Default, Deserialize)]
pub struct FileSourceQuery {
    #[serde(default, rename = "fileId")]
    file_id: String,
    #[serde(default, rename = "projectId")]
    project_id: Option<String>,
    #[serde(default)]
    download: Option<String>,
}

/// The workspace root plus the per-process file index cache.
///
/// The cache is process-wide rather than per request because the oracle's is: the
/// oracle's `FileCache` lives for the server's lifetime, and its entries are keyed by
/// `file_id:mtime` so a rewritten index is re-read.
#[derive(Clone)]
pub struct FileSourceRouteState {
    pub root: PathBuf,
    pub cache: Arc<FileCache>,
}

impl FileSourceRouteState {
    pub fn from_env() -> Self {
        Self {
            root: std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".")),
            cache: Arc::new(FileCache::new()),
        }
    }
}

/// `GET /api/file-source?fileId=…&projectId=…&download=…`.
pub async fn api_file_source(
    State(state): State<FileSourceRouteState>,
    Query(query): Query<FileSourceQuery>,
) -> Response {
    let project_id = query
        .project_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let (cached, path) =
        match cached_file_source(&state.root, &query.file_id, project_id, &state.cache) {
            Ok(pair) => pair,
            Err(error) => return app_error(error),
        };
    let data = match std::fs::read(&path) {
        Ok(data) => data,
        // The source resolved and then could not be read: the oracle's
        // `path.read_bytes()` raises and the route reports the same 410 the resolution
        // would have.
        Err(_) => {
            return app_error(AppError {
                message: "Original uploaded file has expired or is missing".to_string(),
                code: deepseek_policy::app_error::codes::FILE_INDEX_EXPIRED,
                status: 410,
            });
        }
    };
    let media_type = original_file_media_type(&cached);
    // `clean_filename(str(cached.get("name") or "document"))` — the `or` runs first, so
    // a missing, null or empty name is the literal `document`, not `str(None)`.
    let raw_name = deepseek_policy::core_utils::text_or_empty(cached.get("name"));
    let filename = deepseek_policy::file_routes::clean_filename(if raw_name.is_empty() {
        "document"
    } else {
        &raw_name
    });
    let download_value = query
        .download
        .as_deref()
        .map(|value| Value::String(value.to_string()));
    let disposition = if web_truthy(download_value.as_ref()) {
        "attachment"
    } else {
        "inline"
    };

    let mut headers = HeaderMap::new();
    headers.insert(
        header::HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    if let Ok(value) = HeaderValue::from_str(&content_disposition_header(disposition, &filename)) {
        headers.insert(header::CONTENT_DISPOSITION, value);
    }
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if let Ok(value) = HeaderValue::from_str(&media_type) {
        headers.insert(header::CONTENT_TYPE, value);
    }
    (StatusCode::OK, headers, Body::from(data)).into_response()
}

/// The oracle's `AppError` envelope: `{"error": message, "code": code}`.
fn app_error(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        axum::Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
