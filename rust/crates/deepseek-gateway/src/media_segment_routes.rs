//! `GET /api/media/{media_id}/segments`.
//!
//! `library.get_media` then `library.list_segments`. Both read `.media` and
//! neither creates it. A missing `library.json` is an empty library, so a
//! well-formed id is `404 Media not found`. Invalid records are skipped.
//! A missing segments file is `{"ok": true, "segments": []}`.
//!
//! This path has no other Python method. `HEAD` follows Starlette and returns
//! the GET status with an empty body. Any other method is `405` with
//! FastAPI's `{"detail": "Method Not Allowed"}` and does not touch the store.
//! `GET /api/media` and `GET /api/media/{media_id}` stay on the Go proxy.

use std::path::Path;

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path as AxumPath};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{python_float_opt, python_int_opt, python_truthy, text_or_empty};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_schema::{
    new_id, normalize_source_ref, normalize_title, redact_sensitive_text, validate_project_id,
    validate_workspace_id,
};
use serde_json::{Map, Value, json};

const MEDIA_TYPES: [&str; 6] = ["image", "pdf", "audio", "video", "webpage", "screenshot"];
const MEDIA_STATUSES: [&str; 4] = ["pending", "processing", "ready", "failed"];
const SEGMENT_TYPES: [&str; 6] = [
    "ocr_text",
    "caption",
    "transcript",
    "frame",
    "page_text",
    "webpage_text",
];
const MAX_SEGMENT_TEXT_CHARS: usize = 120_000;
const AUDIO_SUFFIXES: [&str; 6] = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"];
const VIDEO_SUFFIXES: [&str; 5] = [".mp4", ".mov", ".webm", ".mkv", ".avi"];

pub fn router() -> Router {
    Router::new().route("/api/media/:media_id/segments", any(dispatch))
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
    let result = match tokio::task::spawn_blocking(move || read_segments(&media_id)).await {
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

fn media_type_from_mime(content_type: &str, filename: &str) -> String {
    let suffix = posix_suffix(filename);
    if content_type.starts_with("image/") {
        return "image".to_string();
    }
    if content_type == "application/pdf" || suffix == ".pdf" {
        return "pdf".to_string();
    }
    if content_type.starts_with("audio/") || AUDIO_SUFFIXES.contains(&suffix.as_str()) {
        return "audio".to_string();
    }
    if content_type.starts_with("video/") || VIDEO_SUFFIXES.contains(&suffix.as_str()) {
        return "video".to_string();
    }
    if content_type == "text/html"
        || content_type == "application/xhtml+xml"
        || suffix == ".html"
        || suffix == ".htm"
    {
        return "webpage".to_string();
    }
    String::new()
}

fn normalize_mime(value: Option<&Value>) -> String {
    text_or_empty(value)
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
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

fn normalize_media_path(value: Option<&Value>) -> Result<String, AppError> {
    let replaced = text_or_empty(value).replace('\\', "/");
    let raw = replaced.trim();
    if raw.is_empty() {
        return Ok(String::new());
    }
    if raw.starts_with('/') || is_drive_path(raw) {
        return Err(AppError::invalid_payload(
            "Media path must be relative to the media library",
        ));
    }
    let mut parts = Vec::new();
    for part in raw.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            return Err(AppError::invalid_payload(
                "Media path must not escape the media library",
            ));
        }
        parts.push(part);
    }
    Ok(parts.join("/"))
}

fn is_drive_path(raw: &str) -> bool {
    let mut chars = raw.chars();
    matches!(
        (chars.next(), chars.next(), chars.next()),
        (Some(letter), Some(':'), Some('/')) if letter.is_ascii_alphabetic()
    )
}

/// `PurePosixPath.suffix.lower()`, where a leading or trailing dot is empty.
fn posix_suffix(filename: &str) -> String {
    let name = filename.rsplit('/').next().unwrap_or(filename);
    match name.rfind('.') {
        Some(index) if index > 0 && index + 1 < name.len() => name[index..].to_ascii_lowercase(),
        _ => String::new(),
    }
}

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

fn python_truthy_opt(value: Option<&Value>) -> bool {
    value.is_some_and(python_truthy)
}

/// `library.list_media()` with no filters: normalise each row, skip `AppError`,
/// and sort by `str(updatedAt)` descending. A missing library is `[]` and is
/// not created. Invalid UTF-8 is the oracle's uncaught `UnicodeDecodeError`.
pub(crate) fn list_public_media() -> Result<Vec<Value>, AppError> {
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
    items.sort_by_key(|item| std::cmp::Reverse(media_sort_key(item)));
    Ok(items)
}

fn media_sort_key(row: &Value) -> String {
    match row.get("updatedAt") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

/// `schema.normalize_media_record`. The segment route only needed acceptance;
/// home returns the public record.
fn normalize_media_record(value: &Value) -> Result<Value, AppError> {
    let media_id = validate_workspace_id(&text_or_empty(value.get("mediaId")), "media id")?;
    let project_raw = text_or_empty(value.get("projectId"));
    let project_id = if project_raw.trim().is_empty() {
        String::new()
    } else {
        validate_project_id(project_raw.trim())?
    };
    let mime = normalize_mime(value.get("mimeType"));
    let title_raw = text_or_empty(value.get("title"));
    let media_type = declared_media_type(value.get("type"), &mime, &title_raw)?;
    let path = normalize_media_path(value.get("path"))?;
    let created_at = if python_truthy_opt(value.get("createdAt")) {
        value_str(value.get("createdAt").expect("truthy createdAt"))
    } else {
        utc_now_z()
    };
    let updated_at = if python_truthy_opt(value.get("updatedAt")) {
        value_str(value.get("updatedAt").expect("truthy updatedAt"))
    } else {
        created_at.clone()
    };
    let mime_type = if mime.is_empty() {
        guess_mime_type(&path)
    } else {
        mime
    };
    Ok(json!({
        "mediaId": media_id,
        "projectId": project_id,
        "type": media_type,
        "title": normalize_title(value.get("title"), "Untitled media"),
        "mimeType": mime_type,
        "path": path,
        "source": normalize_source_ref(value.get("source").unwrap_or(&Value::Null)),
        "status": media_status(value.get("status"))?,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "metadata": normalize_media_metadata(value.get("metadata")),
    }))
}

fn declared_media_type(value: Option<&Value>, mime: &str, title: &str) -> Result<String, AppError> {
    let candidate = text_or_empty(value).trim().to_lowercase();
    if MEDIA_TYPES.contains(&candidate.as_str()) {
        return Ok(candidate);
    }
    let guessed = media_type_from_mime(mime, title);
    if guessed.is_empty() {
        Err(AppError::invalid_payload("Unsupported media type"))
    } else {
        Ok(guessed)
    }
}

fn media_status(value: Option<&Value>) -> Result<String, AppError> {
    let status = if python_truthy_opt(value) {
        text_or_empty(value)
    } else {
        "pending".to_string()
    };
    let status = status.trim().to_lowercase();
    if MEDIA_STATUSES.contains(&status.as_str()) {
        Ok(status)
    } else {
        Err(AppError::invalid_payload("Unsupported media status"))
    }
}

/// `mimetypes.guess_type` for the suffixes the library actually stores.
/// An unregistered suffix is the oracle's `application/octet-stream` default.
fn guess_mime_type(filename: &str) -> String {
    match posix_suffix(filename).as_str() {
        ".png" => "image/png",
        ".jpg" | ".jpeg" => "image/jpeg",
        ".gif" => "image/gif",
        ".webp" => "image/webp",
        ".svg" => "image/svg+xml",
        ".pdf" => "application/pdf",
        ".mp3" => "audio/mpeg",
        ".wav" => "audio/wav",
        ".m4a" => "audio/mp4",
        ".aac" => "audio/aac",
        ".ogg" => "audio/ogg",
        ".flac" => "audio/flac",
        ".mp4" => "video/mp4",
        ".webm" => "video/webm",
        ".mov" => "video/quicktime",
        ".mkv" => "video/x-matroska",
        ".avi" => "video/x-msvideo",
        ".html" | ".htm" => "text/html",
        ".json" => "application/json",
        ".txt" => "text/plain",
        ".csv" => "text/csv",
        _ => "application/octet-stream",
    }
    .to_string()
}

fn utc_now_z() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    deepseek_policy::core_utils::utc_now_iso(seconds).replace("+00:00", "Z")
}

/// `schema.normalize_metadata`. Lists are capped at 200, not the source-ref cap.
fn normalize_media_metadata(value: Option<&Value>) -> Value {
    let Some(Value::Object(fields)) = value else {
        return json!({});
    };
    let mut result = Map::new();
    for (key, item) in fields {
        let safe_key: String = key
            .chars()
            .filter(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | ':' | '-')
            })
            .take(80)
            .collect();
        if safe_key.is_empty() {
            continue;
        }
        match item {
            Value::Object(_) => {
                let nested = normalize_media_metadata(Some(item));
                if nested.as_object().is_some_and(|object| !object.is_empty()) {
                    result.insert(safe_key, nested);
                }
            }
            Value::Array(items) => {
                let cleaned: Vec<Value> = items
                    .iter()
                    .take(200)
                    .map(|child| {
                        if child.is_object() {
                            normalize_media_metadata(Some(child))
                        } else {
                            compact_media_scalar(child)
                        }
                    })
                    .collect();
                result.insert(safe_key, Value::Array(cleaned));
            }
            other => {
                result.insert(safe_key, compact_media_scalar(other));
            }
        }
    }
    Value::Object(result)
}

fn compact_media_scalar(value: &Value) -> Value {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        other => {
            let text: String = value_str(other)
                .chars()
                .take(MAX_SEGMENT_TEXT_CHARS)
                .collect();
            Value::String(text)
        }
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
