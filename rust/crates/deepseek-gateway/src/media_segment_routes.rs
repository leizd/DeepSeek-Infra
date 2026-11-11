//! Read-only media collection, detail, and segment routes.
//!
//! `library.get_media` then `library.list_segments`. Both read `.media` and
//! neither creates it. A missing `library.json` is an empty library, so a
//! well-formed id is `404 Media not found`. Invalid records are skipped.
//! A missing segments file is `{"ok": true, "segments": []}`.
//!
//! The segment path has no other Python method. `HEAD` returns
//! the GET status with an empty body. Any other method is `405` with
//! FastAPI's `{"detail": "Method Not Allowed"}` and does not touch the store.
//! Collection/item mutations retain their existing Go control proxy until
//! their native write paths are implemented. These readers never create stores.

use std::collections::BTreeMap;

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path as AxumPath, Query};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
#[cfg(test)]
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::media_records::{retained::MediaRecord, write_segment_response};
#[cfg(test)]
use deepseek_policy::media_schema::normalize_media_record;
use deepseek_policy::media_schema::{declared_media_type, media_status};
#[cfg(test)]
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_schema::{validate_project_id, validate_workspace_id};
use serde_json::{Value, json};

#[path = "media_read_spool.rs"]
pub(crate) mod media_read_spool;

pub fn router() -> Router {
    Router::new()
        .route("/api/media", any(collection_dispatch))
        .route("/api/media/:media_id", any(item_dispatch))
        .route("/api/media/:media_id/segments", any(dispatch))
}

async fn collection_dispatch(
    method: Method,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return read_response(method, move || read_media_collection(&uri)).await;
    }
    crate::control_proxy::proxy_api_to_go(method, headers, OriginalUri(uri), body).await
}

async fn item_dispatch(
    method: Method,
    media_id: Result<AxumPath<String>, axum::extract::rejection::PathRejection>,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        let media_id = match media_id {
            Ok(AxumPath(media_id)) => media_id,
            Err(rejection) => return rejection.into_response(),
        };
        return read_response(method, move || read_media_item(&media_id)).await;
    }
    crate::control_proxy::proxy_api_to_go(method, headers, OriginalUri(uri), body).await
}

async fn dispatch(
    method: Method,
    AxumPath(media_id): AxumPath<String>,
    _headers: HeaderMap,
    _uri: OriginalUri,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return segments_response(method, media_id).await;
    }
    method_not_allowed()
}

async fn segments_response(method: Method, media_id: String) -> Response {
    read_response(method, move || read_segments(&media_id)).await
}

async fn read_response<T: IntoResponse + Send + 'static>(
    method: Method,
    work: impl FnOnce() -> Result<T, AppError> + Send + 'static,
) -> Response {
    let result = match tokio::task::spawn_blocking(work).await {
        Ok(result) => result,
        Err(_) => Err(server_error()),
    };
    match result {
        Ok(_) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => body.into_response(),
        Err(error) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Err(error) => error_response(error),
    }
}

fn read_media_collection(uri: &Uri) -> Result<media_read_spool::CompletedJson, AppError> {
    let Query(filters) = Query::<BTreeMap<String, String>>::try_from_uri(uri)
        .map_err(|_| AppError::invalid_payload("Invalid media filters"))?;
    let raw_project = filters.get("projectId").map(String::as_str).unwrap_or("");
    let project = if raw_project.is_empty() {
        String::new()
    } else {
        validate_project_id(raw_project)?
    };
    let raw_type = filters.get("type").map(String::as_str).unwrap_or("");
    let media_type = if raw_type.is_empty() {
        String::new()
    } else {
        declared_media_type(Some(&Value::String(raw_type.to_string())), "", "")?
    };
    let raw_status = filters.get("status").map(String::as_str).unwrap_or("");
    let status = if raw_status.is_empty() {
        String::new()
    } else {
        media_status(Some(&Value::String(raw_status.to_string())))?
    };
    media_read_spool::PublicIndex::load(&library_path())?.collection(|item| {
        (project.is_empty() || item.field_equals("projectId", &project))
            && (media_type.is_empty() || item.field_equals("type", &media_type))
            && (status.is_empty() || item.field_equals("status", &status))
    })
}

fn lookup_media_item(media_id: &str) -> Result<MediaRecord, AppError> {
    let safe_id = validate_workspace_id(media_id, "media id")?;
    let media = media_read_spool::find(&library_path(), &safe_id)?
        .ok_or_else(|| AppError::not_found("Media not found"))?;
    Ok(media)
}

fn read_media_item(media_id: &str) -> Result<media_read_spool::CompletedJson, AppError> {
    media_read_spool::CompletedJson::item(&lookup_media_item(media_id)?)
}

fn read_segments(media_id: &str) -> Result<media_read_spool::CompletedJson, AppError> {
    let safe_id = validate_workspace_id(media_id, "media id")?;
    // The original route calls library.get_media before reading any segments.
    lookup_media_item(&safe_id)?;
    media_read_spool::CompletedJson::build(|output| {
        let source = std::fs::File::open(segments_path(&safe_id)).ok();
        write_segment_response(source, &safe_id, output)
    })
}

fn library_path() -> std::path::PathBuf {
    crate::data_routes::workspace_root()
        .join(".media")
        .join("library.json")
}

fn segments_path(media_id: &str) -> std::path::PathBuf {
    crate::data_routes::workspace_root()
        .join(".media")
        .join("segments")
        .join(format!("{media_id}.json"))
}

/// `library.list_media()` with no filters: normalise each row, skip `AppError`,
/// and sort by `str(updatedAt)` descending before selecting the home window. A missing library is `[]` and is
/// not created. Invalid UTF-8 is the oracle's uncaught `UnicodeDecodeError`.
pub(crate) fn media_window(limit: usize) -> Result<(Vec<MediaRecord>, usize), AppError> {
    media_read_spool::PublicIndex::load(&library_path())?.window(limit)
}

#[cfg(test)]
fn media_sort_key(row: &Value) -> String {
    match row.get("updatedAt") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
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
