//! `GET /api/download` — the generated-file download route.
//!
//! The tools that generate a document, deck or mindmap answer with a `downloadUrl`
//! (`/api/download?id={fileId}`), and this is what serves it. It fell through to the Go
//! `/api/*` catch-all, so every generated file was a `503` on the native edge.
//!
//! # Everything security-relevant is in `deepseek-policy`
//!
//! `resolve_generated_file` accepts only a 32-character lowercase-hex id and probes the
//! five registered extensions, which is what rules out path traversal — the id never
//! reaches a path component unchecked. `download_descriptor` picks the media type and
//! the attachment name per extension. This module only reads the bytes and writes the
//! two headers, so a change in the id rule cannot be forgotten here.
//!
//! # The headers are the oracle's
//!
//! `Content-Disposition: {inline|attachment}; filename="{name}"` and
//! `Cache-Control: no-store`. The disposition is `inline` only for an `.svg` when the
//! request asks for it (`?inline=...` truthy), because an SVG is the one generated type
//! a browser can safely render in place — the others are downloads by construction.

use std::path::PathBuf;

use axum::body::Body;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::generated_files::{download_descriptor, resolve_generated_file};
use serde::Deserialize;
use serde_json::{Value, json};

/// The query the frontend sends: the file id, and the optional `inline` request.
#[derive(Debug, Default, Deserialize)]
pub struct DownloadQuery {
    #[serde(default)]
    id: String,
    #[serde(default)]
    inline: Option<String>,
}

/// The workspace root the generated files live under.
#[derive(Clone)]
pub struct DownloadRouteState {
    pub root: PathBuf,
}

impl DownloadRouteState {
    /// `DEEPSEEK_INFRA_ROOT`, defaulting to the working directory — the same binding
    /// the data routes use, because a deployment that has not set it has no workspace
    /// and the lookup then simply finds nothing.
    pub fn from_env() -> Self {
        Self {
            root: std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".")),
        }
    }
}

/// `GET /api/download?id=...`.
pub async fn api_download(
    State(state): State<DownloadRouteState>,
    Query(query): Query<DownloadQuery>,
) -> Response {
    let Some(path) = resolve_generated_file(&state.root, &query.id) else {
        return app_error(AppError {
            message: "File does not exist or has expired".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    };
    let data = match std::fs::read(&path) {
        Ok(data) => data,
        // A file that resolved and then could not be read is the same observable
        // answer as one that is not there: the oracle's `path.read_bytes()` raises and
        // the route reports the file as gone.
        Err(_) => {
            return app_error(AppError {
                message: "File does not exist or has expired".to_string(),
                code: codes::NOT_FOUND,
                status: 404,
            });
        }
    };
    let (media_type, download_name) = download_descriptor(&path);
    let extension = path
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    // `truthy(request.query_params.get("inline", ""))` — the **web layer's** helper,
    // which is a string parse over `1/true/yes/on`, not Python truthiness. So
    // `inline=false` is *false* and the file downloads. The first version of this route
    // used `python_truthy` and its test asserted the wrong answer; both were corrected
    // against the oracle.
    let inline_value = query
        .inline
        .as_deref()
        .map(|value| Value::String(value.to_string()));
    let inline_requested = deepseek_policy::core_utils::web_truthy(inline_value.as_ref());
    let disposition = if extension == "svg" && inline_requested {
        "inline"
    } else {
        "attachment"
    };

    let mut headers = HeaderMap::new();
    if let Ok(value) =
        HeaderValue::from_str(&format!("{disposition}; filename=\"{download_name}\""))
    {
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
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::NOT_FOUND);
    (
        status,
        axum::Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
