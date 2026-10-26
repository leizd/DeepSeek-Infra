//! Shared pure media normalization. The gateway reads; the worker alone writes.
use crate::app_error::AppError;
use crate::core_utils::{python_truthy, text_or_empty};
use crate::python_json::value_str;
use crate::workspace_schema::{
    normalize_source_ref, normalize_title, validate_project_id, validate_workspace_id,
};
use serde_json::{Map, Value, json};

pub const MEDIA_TYPES: [&str; 6] = ["image", "pdf", "audio", "video", "webpage", "screenshot"];

pub const MEDIA_STATUSES: [&str; 4] = ["pending", "processing", "ready", "failed"];

pub const MAX_SEGMENT_TEXT_CHARS: usize = 120_000;

pub const AUDIO_SUFFIXES: [&str; 6] = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"];

pub const VIDEO_SUFFIXES: [&str; 5] = [".mp4", ".mov", ".webm", ".mkv", ".avi"];

pub fn media_type_from_mime(content_type: &str, filename: &str) -> String {
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

pub fn normalize_mime(value: Option<&Value>) -> String {
    text_or_empty(value)
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

pub fn normalize_media_path(value: Option<&Value>) -> Result<String, AppError> {
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

pub fn posix_suffix(filename: &str) -> String {
    let name = filename.rsplit('/').next().unwrap_or(filename);
    match name.rfind('.') {
        Some(index) if index > 0 && index + 1 < name.len() => name[index..].to_ascii_lowercase(),
        _ => String::new(),
    }
}

pub fn python_truthy_opt(value: Option<&Value>) -> bool {
    value.is_some_and(python_truthy)
}

pub fn normalize_media_record(value: &Value) -> Result<Value, AppError> {
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
