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
use std::path::Path;

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path as AxumPath, Query};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{python_float_opt, python_int_opt, python_truthy, text_or_empty};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::media_schema::{
    MAX_SEGMENT_TEXT_CHARS, MEDIA_STATUSES, MEDIA_TYPES, media_type_from_mime,
    normalize_media_path, normalize_media_record, normalize_mime, python_truthy_opt,
};
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_schema::{
    new_id, normalize_source_ref, redact_sensitive_text, validate_project_id, validate_workspace_id,
};
use serde_json::{Value, json};

const SEGMENT_TYPES: [&str; 6] = [
    "ocr_text",
    "caption",
    "transcript",
    "frame",
    "page_text",
    "webpage_text",
];

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

async fn read_response(
    method: Method,
    work: impl FnOnce() -> Result<Value, AppError> + Send + 'static,
) -> Response {
    let result = match tokio::task::spawn_blocking(work).await {
        Ok(result) => result,
        Err(_) => Err(server_error()),
    };
    match result {
        Ok(_) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => Json(body).into_response(),
        Err(error) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Err(error) => error_response(error),
    }
}

fn read_media_collection(uri: &Uri) -> Result<Value, AppError> {
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
    let mut items = list_public_media()?;
    items.retain(|item| {
        (project.is_empty() || item["projectId"].as_str() == Some(project.as_str()))
            && (media_type.is_empty() || item["type"].as_str() == Some(media_type.as_str()))
            && (status.is_empty() || item["status"].as_str() == Some(status.as_str()))
    });
    Ok(json!({"ok": true, "media": items}))
}

fn read_media_item(media_id: &str) -> Result<Value, AppError> {
    let safe_id = validate_workspace_id(media_id, "media id")?;
    let media = load_public_media_in_source_order()?
        .into_iter()
        .find(|item| item["mediaId"].as_str() == Some(safe_id.as_str()))
        .ok_or_else(|| AppError::not_found("Media not found"))?;
    Ok(json!({"ok": true, "media": media}))
}

fn read_segments(media_id: &str) -> Result<Value, AppError> {
    let safe_id = validate_workspace_id(media_id, "media id")?;
    if !library_contains(&safe_id)? {
        return Err(AppError::not_found("Media not found"));
    }
    Ok(json!({"ok": true, "segments": load_segments(&safe_id)?}))
}

fn library_contains(safe_id: &str) -> Result<bool, AppError> {
    let Some(value) = read_optional_json(&library_path())? else {
        return Ok(false);
    };
    let Some(items) = value.get("media").and_then(Value::as_array) else {
        return Ok(false);
    };
    Ok(items
        .iter()
        .any(|item| item.is_object() && accepted_media_id(item).as_deref() == Some(safe_id)))
}

fn load_segments(safe_id: &str) -> Result<Vec<Value>, AppError> {
    let Some(value) = read_optional_json(&segments_path(safe_id))? else {
        return Ok(Vec::new());
    };
    let Some(items) = value.get("segments").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut segments = Vec::new();
    for (index, item) in items.iter().enumerate() {
        if !item.is_object() {
            continue;
        }
        if let Some(segment) = normalize_segment(item, safe_id, index)? {
            segments.push(segment);
        }
    }
    Ok(segments)
}

/// `exists` then read. A missing path, `OSError` (including a directory), and
/// invalid JSON are the oracle's empty default. Invalid UTF-8 is not caught.
fn read_optional_json(path: &Path) -> Result<Option<Value>, AppError> {
    if !path.exists() {
        return Ok(None);
    }
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(None),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    match serde_json::from_str(text) {
        Ok(value) => Ok(Some(value)),
        Err(_) => Ok(None),
    }
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

/// The raise points of `normalize_media_record`, in order. The public record
/// is discarded by the route, so only acceptance is reproduced.
fn accepted_media_id(value: &Value) -> Option<String> {
    let media_id = validate_workspace_id(&text_or_empty(value.get("mediaId")), "media id").ok()?;
    let project = text_or_empty(value.get("projectId"));
    let project = project.trim();
    if !project.is_empty() && validate_project_id(project).is_err() {
        return None;
    }
    let mime = normalize_mime(value.get("mimeType"));
    let title = text_or_empty(value.get("title"));
    if media_type_of(value.get("type"), &mime, &title).is_err() {
        return None;
    }
    if normalize_media_path(value.get("path")).is_err() {
        return None;
    }
    if normalize_status(value.get("status")).is_err() {
        return None;
    }
    Some(media_id)
}

fn media_type_of(value: Option<&Value>, mime: &str, title: &str) -> Result<(), AppError> {
    let candidate = text_or_empty(value).trim().to_lowercase();
    if MEDIA_TYPES.contains(&candidate.as_str()) || !media_type_from_mime(mime, title).is_empty() {
        return Ok(());
    }
    Err(AppError::invalid_payload("Unsupported media type"))
}

fn normalize_status(value: Option<&Value>) -> Result<(), AppError> {
    let status = if python_truthy_opt(value) {
        text_or_empty(value)
    } else {
        "pending".to_string()
    };
    let status = status.trim().to_lowercase();
    if MEDIA_STATUSES.contains(&status.as_str()) {
        Ok(())
    } else {
        Err(AppError::invalid_payload("Unsupported media status"))
    }
}

/// `PurePosixPath.suffix.lower()`, where a leading or trailing dot is empty.

/// `Ok(None)` is the oracle's `except AppError: continue`. `Err` is an
/// uncaught `ValueError`/`TypeError` from `int()`, which fails the request.
fn normalize_segment(
    value: &Value,
    media_id: &str,
    fallback_index: usize,
) -> Result<Option<Value>, AppError> {
    let segment_type = str_or_default(value.get("type"), "page_text");
    let segment_type = segment_type.trim().to_lowercase();
    if !SEGMENT_TYPES.contains(&segment_type.as_str()) {
        return Ok(None);
    }
    let raw_id = text_or_empty(value.get("segmentId"));
    let raw_id = raw_id.trim();
    let segment_id = if raw_id.is_empty() {
        new_id("seg", &SystemEntropy).map_err(|_| server_error())?
    } else {
        match validate_workspace_id(raw_id, "segment id") {
            Ok(segment_id) => segment_id,
            Err(_) => return Ok(None),
        }
    };
    let text: String = redact_sensitive_text(&text_or_empty(value.get("text")))
        .chars()
        .take(MAX_SEGMENT_TEXT_CHARS)
        .collect();
    let index = match value.get("index") {
        None | Some(Value::Null) => i64::try_from(fallback_index).unwrap_or(i64::MAX),
        Some(raw) => python_int_opt(Some(raw)).ok_or_else(server_error)?,
    };
    let mut segment = Map::new();
    segment.insert("segmentId".to_string(), json!(segment_id));
    segment.insert("mediaId".to_string(), json!(media_id));
    segment.insert("type".to_string(), json!(segment_type));
    segment.insert("text".to_string(), json!(text));
    segment.insert(
        "confidence".to_string(),
        json!(normalize_confidence(value.get("confidence"))),
    );
    segment.insert("index".to_string(), json!(index));
    if let Some(page) = value.get("page").filter(|page| !page.is_null()) {
        segment.insert("page".to_string(), json!(required_page(page)?));
    }
    let time_range = normalize_time_range(value.get("timeRange"));
    if !time_range.is_empty() {
        segment.insert("timeRange".to_string(), json!(time_range));
    }
    match normalize_media_path(value.get("framePath")) {
        Ok(path) if !path.is_empty() => {
            segment.insert("framePath".to_string(), json!(path));
        }
        Ok(_) => {}
        Err(_) => return Ok(None),
    }
    if let Some(citation) = value
        .get("citation")
        .filter(|citation| citation.is_object())
    {
        segment.insert(
            "citation".to_string(),
            deepseek_policy::workspace_schema::normalize_source_ref(citation),
        );
    }
    Ok(Some(Value::Object(segment)))
}

fn normalize_confidence(value: Option<&Value>) -> f64 {
    match value {
        None | Some(Value::Null) => 1.0,
        Some(Value::String(text)) if text.is_empty() => 1.0,
        Some(other) => match python_float_opt(other) {
            Some(number) if number.is_finite() => round_digits(number.clamp(0.0, 1.0), 4),
            _ => 1.0,
        },
    }
}

fn normalize_time_range(value: Option<&Value>) -> Vec<f64> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    if items.len() < 2 {
        return Vec::new();
    }
    let (Some(start), Some(end)) = (python_float_opt(&items[0]), python_float_opt(&items[1]))
    else {
        return Vec::new();
    };
    if !start.is_finite() || !end.is_finite() {
        return Vec::new();
    }
    let start = start.max(0.0);
    let end = end.max(start);
    vec![round_digits(start, 3), round_digits(end, 3)]
}

fn required_page(value: &Value) -> Result<i64, AppError> {
    let parsed = if python_truthy(value) {
        python_int_opt(Some(value)).ok_or_else(server_error)?
    } else {
        1
    };
    Ok(parsed.max(1))
}

fn round_digits(value: f64, digits: i32) -> f64 {
    let factor = 10f64.powi(digits);
    (value * factor).round() / factor
}

fn str_or_default(value: Option<&Value>, default: &str) -> String {
    if python_truthy_opt(value) {
        text_or_empty(value)
    } else {
        default.to_string()
    }
}

/// `library.list_media()` with no filters: normalise each row, skip `AppError`,
/// and sort by `str(updatedAt)` descending. A missing library is `[]` and is
/// not created. Invalid UTF-8 is the oracle's uncaught `UnicodeDecodeError`.
pub(crate) fn list_public_media() -> Result<Vec<Value>, AppError> {
    let mut items = load_public_media_in_source_order()?;
    items.sort_by_key(|item| std::cmp::Reverse(media_sort_key(item)));
    Ok(items)
}

fn load_public_media_in_source_order() -> Result<Vec<Value>, AppError> {
    let path = library_path();
    if !path.exists() {
        return Ok(Vec::new());
    }
    let bytes = match std::fs::read(&path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(Vec::new()),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    let value: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(_) => return Ok(Vec::new()),
    };
    if !value.is_object() {
        return Ok(Vec::new());
    }
    let rows = value.get("media").and_then(Value::as_array);
    let mut items = Vec::new();
    for item in rows.into_iter().flatten() {
        if !item.is_object() {
            continue;
        }
        // `_load_store` catches `AppError` and continues. `public_media` is a
        // second `normalize_media_record` and is not caught.
        let Ok(stored) = normalize_media_record(item) else {
            continue;
        };
        items.push(normalize_media_record(&stored)?);
    }
    Ok(items)
}

fn media_sort_key(row: &Value) -> String {
    match row.get("updatedAt") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

/// `schema.normalize_media_record`. The segment route only needed acceptance;
/// home and library readers return the public record.

/// `mimetypes.guess_type` for the suffixes the library actually stores.
/// An unregistered suffix is the oracle's `application/octet-stream` default.

/// `schema.normalize_metadata`. Lists are capped at 200, not the source-ref cap.

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
