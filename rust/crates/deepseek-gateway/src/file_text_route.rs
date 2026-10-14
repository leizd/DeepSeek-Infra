//! `POST /api/file-text` — multipart upload of text and HTML files.
//!
//! The frontend posts `FormData` here and then reads the cached extraction through
//! `/api/file-reader` and `/api/file-source`. Text, HTML, DOCX, PPTX, XLSX,
//! selectable PDF text, EPUB chapters, and OCR for images or textless PDFs are
//! extracted when the request asks for OCR. An image with OCR off is the
//! oracle's `415 ocr_required`. A textless PDF with OCR off is `422 ocr_required`.

use std::path::PathBuf;

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::web_truthy;
use deepseek_policy::file_routes::clean_filename;
use deepseek_policy::file_upload::{
    self, MAX_SHARE_FIELD_CHARS, MAX_UPLOAD_BYTES, MAX_UPLOAD_FILE_BYTES, UploadedPart,
};
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};

const MAX_MULTIPART_FIELD_BYTES: usize = 4_096;
const MAX_MULTIPART_FILES: usize = 20;

#[derive(Clone)]
pub struct FileTextRouteState {
    pub root: PathBuf,
}

impl FileTextRouteState {
    pub fn from_env() -> Self {
        Self {
            root: std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".")),
        }
    }
}

pub async fn api_file_text(
    State(state): State<FileTextRouteState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let content_length = match declared_content_length(
        headers
            .get(header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok()),
        body.len(),
    ) {
        Ok(length) => length,
        Err(error) => return app_error(error),
    };
    let content_type = content_type.to_string();
    let control = deepseek_policy::extraction_control::FileProcessingControl::default();
    let _cancel_on_drop = control.cancel_on_drop();
    match tokio::task::spawn_blocking(move || {
        control.run(|| parse_and_extract(&state.root, &content_type, content_length, &body))
    })
    .await
    {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => app_error(error),
        Err(_) => app_error(AppError {
            message: "File extraction failed".to_string(),
            code: codes::INTERNAL,
            status: 500,
        }),
    }
}

fn parse_and_extract(
    root: &std::path::Path,
    content_type: &str,
    content_length: usize,
    body: &[u8],
) -> Result<Value, AppError> {
    let (files, ocr_enabled) = read_multipart_upload(content_type, content_length, body)?;
    file_upload::extract_upload_batch(root, &files, ocr_enabled, None)
}

/// The shared multipart upload reader for `/api/file-text` and `/api/project-files`.
pub(crate) fn read_multipart_upload(
    content_type: &str,
    content_length: usize,
    body: &[u8],
) -> Result<(Vec<UploadedPart>, bool), AppError> {
    if !content_type
        .to_ascii_lowercase()
        .contains("multipart/form-data")
    {
        return Err(invalid("Expected multipart/form-data"));
    }
    if content_length == 0 {
        return Err(invalid("Upload body is empty"));
    }
    if content_length > MAX_UPLOAD_BYTES {
        return Err(AppError {
            message: format!(
                "Upload body is too large. Maximum request size is {} MB.",
                format_upload_limit(MAX_UPLOAD_BYTES)
            ),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let (files, ocr_enabled, _api_key) = parse_multipart(content_type, body)?;
    Ok((files, ocr_enabled))
}

/// `Content-Length` when the header is present, otherwise the buffered body length.
pub(crate) fn declared_content_length(
    header: Option<&str>,
    body_len: usize,
) -> Result<usize, AppError> {
    match header {
        Some(value) => match value.trim().parse::<i64>() {
            Ok(parsed) if parsed >= 0 => Ok(parsed as usize),
            _ => Err(AppError {
                message: "Invalid Content-Length".to_string(),
                code: codes::INVALID_PAYLOAD,
                status: 400,
            }),
        },
        None => Ok(body_len),
    }
}

fn invalid(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn format_upload_limit(value: usize) -> u64 {
    ((value as f64) / 1_000_000.0).round().max(1.0) as u64
}

struct Part {
    headers: String,
    body: Vec<u8>,
}

fn parse_multipart(
    content_type: &str,
    body: &[u8],
) -> Result<(Vec<UploadedPart>, bool, String), AppError> {
    let boundary = boundary_parameter(content_type)
        .ok_or_else(|| invalid("Upload body is not multipart/form-data"))?;
    if boundary.is_empty() {
        return Err(invalid("Upload body is not multipart/form-data"));
    }
    let parts = split_parts(body, &boundary)?;
    let mut files = Vec::new();
    let mut ocr_enabled = false;
    let mut api_key = String::new();
    let mut saw_api_key = false;
    for part in parts {
        let disposition = header_value(&part.headers, "content-disposition").unwrap_or_default();
        let filename = filename_parameter(&disposition);
        if let Some(raw_name) = filename {
            if files.len() >= MAX_MULTIPART_FILES {
                return Err(AppError {
                    message: "Too many uploaded files".to_string(),
                    code: codes::UPLOAD_TOO_LARGE,
                    status: 413,
                });
            }
            let filename = clean_filename(&raw_name);
            if filename.is_empty() {
                continue;
            }
            if part.body.len() > MAX_UPLOAD_FILE_BYTES {
                return Err(AppError {
                    message: format!(
                        "File is too large. Maximum file size is {} MB.",
                        format_upload_limit(MAX_UPLOAD_FILE_BYTES)
                    ),
                    code: codes::UPLOAD_TOO_LARGE,
                    status: 413,
                });
            }
            let part_type = header_value(&part.headers, "content-type")
                .unwrap_or_else(|| "application/octet-stream".to_string());
            files.push(UploadedPart {
                filename,
                content_type: part_type,
                data: part.body,
            });
            continue;
        }
        if part.body.len() > MAX_MULTIPART_FIELD_BYTES {
            return Err(AppError {
                message: "Upload field is too large".to_string(),
                code: codes::UPLOAD_TOO_LARGE,
                status: 413,
            });
        }
        let name = name_parameter(&disposition);
        if name.is_empty() {
            continue;
        }
        let value = String::from_utf8_lossy(&part.body);
        let value: String = value.chars().take(MAX_SHARE_FIELD_CHARS).collect();
        if name == "ocrEnabled" {
            ocr_enabled = web_truthy(Some(&Value::String(value)));
        } else if name == "apiKey" && !saw_api_key {
            saw_api_key = true;
            api_key = value.trim().to_string();
        }
    }
    Ok((files, ocr_enabled, api_key))
}

fn boundary_parameter(content_type: &str) -> Option<String> {
    let (media, parameters) = split_content_type(content_type);
    if media != "multipart/form-data" {
        return None;
    }
    parameters.get("boundary").cloned()
}

fn split_content_type(header: &str) -> (String, std::collections::BTreeMap<String, String>) {
    let mut parts = header.split(';');
    let media = parts.next().unwrap_or("").trim().to_ascii_lowercase();
    let mut parameters = std::collections::BTreeMap::new();
    for part in parts {
        let Some((name, value)) = part.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"').to_string();
        parameters.insert(name.trim().to_ascii_lowercase(), value);
    }
    (media, parameters)
}

fn split_parts(body: &[u8], boundary: &str) -> Result<Vec<Part>, AppError> {
    let marker = format!("--{boundary}").into_bytes();
    let Some(first) = find_slice(body, &marker) else {
        return Err(invalid("Invalid multipart upload"));
    };
    let mut cursor = first + marker.len();
    let mut parts = Vec::new();
    loop {
        if body.get(cursor..cursor + 2) == Some(b"--") {
            break;
        }
        if body.get(cursor..cursor + 2) == Some(b"\r\n") {
            cursor += 2;
        }
        let Some(header_end) = find_slice(&body[cursor..], b"\r\n\r\n") else {
            return Err(invalid("Invalid multipart upload"));
        };
        let headers = String::from_utf8_lossy(&body[cursor..cursor + header_end]).to_string();
        if headers.len() > 4_096 {
            return Err(invalid("Invalid multipart upload"));
        }
        cursor += header_end + 4;
        let next = find_slice(&body[cursor..], &{
            let mut delimiter = b"\r\n--".to_vec();
            delimiter.extend(boundary.as_bytes());
            delimiter
        });
        let Some(next) = next else {
            return Err(invalid("Invalid multipart upload"));
        };
        let part_body = body[cursor..cursor + next].to_vec();
        parts.push(Part {
            headers,
            body: part_body,
        });
        cursor += next + 2;
        if cursor + marker.len() > body.len() {
            break;
        }
        cursor += marker.len();
    }
    Ok(parts)
}

fn find_slice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn header_value(headers: &str, name: &str) -> Option<String> {
    for line in headers.split("\r\n") {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        if key.trim().eq_ignore_ascii_case(name) {
            return Some(value.trim().to_string());
        }
    }
    None
}

fn filename_parameter(disposition: &str) -> Option<String> {
    if let Some(star) = parameter(disposition, "filename*") {
        let encoded = star.split("''").nth(1).unwrap_or(star.as_str());
        let decoded = percent_decode_str(encoded).decode_utf8_lossy().to_string();
        return Some(decoded);
    }
    parameter(disposition, "filename")
}

fn name_parameter(disposition: &str) -> String {
    parameter(disposition, "name").unwrap_or_default()
}

fn parameter(header: &str, name: &str) -> Option<String> {
    for piece in header.split(';') {
        let piece = piece.trim();
        let Some((key, value)) = piece.split_once('=') else {
            continue;
        };
        if !key.trim().eq_ignore_ascii_case(name) {
            continue;
        }
        let value = value.trim();
        if let Some(quoted) = value.strip_prefix('"') {
            return Some(quoted.trim_end_matches('"').to_string());
        }
        return Some(value.to_string());
    }
    None
}

fn app_error(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
