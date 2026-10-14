//! `extract_uploaded_file` plus the cache it writes.
//!
//! Text, HTML, DOCX, PPTX, XLSX, EPUB, selectable PDF text, and OCR for an image
//! or a textless PDF follow `files.py`. A successful extraction writes the cache.
//! OCR off, an empty OCR result, and a missing OCR engine refuse before that write.

use std::cmp::Reverse;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::app_error::{AppError, codes};
use crate::attachment_context::{LOCAL_RAG_EMBEDDING_DIMENSIONS, hash_text_embedding};
use crate::file_cache::file_cache_dir;
use crate::file_routes::{FILE_SOURCE_SUFFIX, normalize_extracted_text, normalized_page_texts};

pub const MAX_UPLOAD_BYTES: usize = 220_000_000;
pub const MAX_UPLOAD_FILE_BYTES: usize = 200_000_000;
pub const FILE_CHUNK_CHARS: usize = 6_000;
pub const FILE_CHUNK_OVERLAP: usize = 400;
pub const FILE_PREVIEW_CHARS: usize = 1_800;
pub const MAX_SHARE_FIELD_CHARS: usize = 12_000;
pub const FILE_CACHE_MAX_AGE_DAYS: f64 = 14.0;
pub const FILE_CACHE_MAX_BYTES: u64 = 500_000_000;

const TEXT_EXTENSIONS: &[&str] = &[
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".log",
    ".ini",
    ".toml",
    ".env",
    ".rtf",
];
const HTML_EXTENSIONS: &[&str] = &[".html", ".htm"];
const IMAGE_EXTENSIONS: &[&str] = &[
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
];

/// One uploaded part after the multipart layer has cleaned the filename.
#[derive(Debug, Clone)]
pub struct UploadedPart {
    pub filename: String,
    pub content_type: String,
    pub data: Vec<u8>,
}

/// `file_content_id`: SHA-256 of the name, a NUL, the byte length, a NUL and the bytes,
/// truncated to 32 hex characters.
pub fn file_content_id(filename: &str, source_bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(filename.as_bytes());
    hasher.update([0]);
    hasher.update((source_bytes.len() as u64).to_be_bytes());
    hasher.update([0]);
    hasher.update(source_bytes);
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest.iter() {
        let _ = write!(hex, "{byte:02x}");
    }
    hex.truncate(32);
    hex
}

pub fn is_image_file(extension: &str, content_type: &str) -> bool {
    let normalized = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    IMAGE_EXTENSIONS.contains(&extension) || normalized.starts_with("image/")
}

/// `is_text_file`. A NUL in the first 2048 bytes keeps an unknown extension out.
pub fn is_text_file(extension: &str, content_type: &str, data: &[u8]) -> bool {
    if TEXT_EXTENSIONS.contains(&extension) {
        return true;
    }
    if content_type.starts_with("text/") {
        return true;
    }
    !data.iter().take(2048).any(|byte| *byte == 0)
}

fn extension_of(filename: &str) -> String {
    let lower = filename.to_ascii_lowercase();
    match lower.rfind('.') {
        Some(index) if index > 0 => lower[index..].to_string(),
        _ => String::new(),
    }
}

fn chars_of(text: &str) -> Vec<char> {
    text.chars().collect()
}

fn rfind_chars(haystack: &[char], needle: &str) -> Option<usize> {
    let needle: Vec<char> = needle.chars().collect();
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len())
        .rev()
        .find(|start| haystack[*start..*start + needle.len()] == needle[..])
}

fn newline_count(chars: &[char], end: usize) -> usize {
    chars
        .iter()
        .take(end)
        .filter(|character| **character == '\n')
        .count()
}

/// `chunk_text`, with the hash embedding the oracle uses when no ONNX model is loaded.
pub fn chunk_text(text: &str) -> Vec<Value> {
    if text.is_empty() {
        return Vec::new();
    }
    let chars = chars_of(text);
    let text_length = chars.len();
    let mut chunks = Vec::new();
    let mut start = 0usize;
    while start < text_length {
        let mut end = (start + FILE_CHUNK_CHARS).min(text_length);
        if end < text_length {
            let window = &chars[start..end];
            let boundary = rfind_chars(window, "\n\n")
                .into_iter()
                .chain(rfind_chars(window, "\n"))
                .max()
                .map(|offset| start + offset);
            if let Some(boundary) = boundary {
                if boundary > start + FILE_CHUNK_CHARS / 2 {
                    end = boundary;
                }
            }
        }
        let body: String = chars[start..end]
            .iter()
            .collect::<String>()
            .trim()
            .to_string();
        if !body.is_empty() {
            let vector = hash_text_embedding(&body, LOCAL_RAG_EMBEDDING_DIMENSIONS);
            chunks.push(json!({
                "index": chunks.len(),
                "start": start,
                "end": end,
                "lineStart": newline_count(&chars, start) + 1,
                "lineEnd": newline_count(&chars, end) + 1,
                "text": body,
                "vector": vector,
            }));
        }
        if end >= text_length {
            break;
        }
        start = (end - FILE_CHUNK_OVERLAP).max(start + 1);
    }
    chunks
}

fn preview_of(text: &str) -> String {
    text.chars()
        .take(FILE_PREVIEW_CHARS)
        .collect::<String>()
        .trim_end()
        .to_string()
}

struct CachedFile<'a> {
    root: &'a Path,
    file_id: &'a str,
    filename: &'a str,
    content_type: &'a str,
    kind: &'a str,
    text: &'a str,
    chunks: &'a [Value],
    source_bytes: &'a [u8],
    project_id: Option<&'a str>,
    page_count: i64,
    page_texts: &'a [Value],
}

fn write_cache(file: &CachedFile<'_>) -> Result<(), AppError> {
    let directory = match file.project_id.filter(|id| !id.is_empty()) {
        Some(id) => crate::file_cache::project_file_cache_dir(file.root, id)?,
        None => file_cache_dir(file.root),
    };
    fs::create_dir_all(&directory).map_err(|_| cache_io())?;
    let payload = json!({
        "id": file.file_id,
        "name": file.filename,
        "type": file.content_type,
        "size": file.source_bytes.len(),
        "kind": file.kind,
        "projectId": file.project_id.unwrap_or(""),
        "sourceAvailable": true,
        "pageCount": file.page_count,
        "pageTexts": file.page_texts,
        "charCount": file.text.chars().count(),
        "chunkCount": file.chunks.len(),
        "chunks": file.chunks,
    });
    let index_path = directory.join(format!("{}.json", file.file_id));
    let source_path = directory.join(format!("{}{FILE_SOURCE_SUFFIX}", file.file_id));
    replace_file(&source_path, file.source_bytes)?;
    let encoded = serde_json::to_vec(&payload).map_err(|_| cache_io())?;
    replace_file(&index_path, &encoded)?;
    Ok(())
}

fn replace_file(path: &Path, bytes: &[u8]) -> Result<(), AppError> {
    let tmp = PathBuf::from(format!("{}.tmp", path.display()));
    fs::write(&tmp, bytes).map_err(|_| cache_io())?;
    if path.exists() {
        fs::remove_file(path).map_err(|_| cache_io())?;
    }
    fs::rename(&tmp, path).map_err(|_| cache_io())?;
    Ok(())
}

fn cache_io() -> AppError {
    AppError {
        message: "Uploaded file index is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    }
}

/// Mirrors `extract_uploaded_file` for the types this crate extracts.
///
/// `ocr_enabled` is the value the route already resolved (the server default, then
/// each `ocrEnabled` field). Image uploads with OCR off use the oracle's
/// `ocr_required` refusal. A PDF with selectable text is extracted. A PDF that
/// parses but has no text uses that same OCR refusal when OCR is off. With OCR on,
/// an image or a textless PDF is recognized and cached; an empty recognition is
/// `ocr_empty` and a missing engine is `ocr_unavailable`, and neither writes a
/// cache entry. DOCX, PPTX, XLSX and EPUB are extracted.
pub fn extract_uploaded_file(
    root: &Path,
    filename: &str,
    content_type: &str,
    data: &[u8],
    ocr_enabled: bool,
    project_id: Option<&str>,
) -> Result<Value, AppError> {
    crate::extraction_control::check()?;
    let extension = extension_of(filename);
    if data.is_empty() {
        return Err(AppError {
            message: format!("Uploaded file is empty: {filename}"),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let (text, kind, page_count, page_texts) = if extension == ".docx" {
        let text = normalize_extracted_text(&crate::docx_text::extract_docx_text(data)?);
        (text, "docx".to_string(), 0, Vec::new())
    } else if extension == ".pptx" {
        let text = normalize_extracted_text(&crate::docx_text::extract_pptx_text(data)?);
        (text, "pptx".to_string(), 0, Vec::new())
    } else if extension == ".xlsx" {
        let text = normalize_extracted_text(&crate::docx_text::extract_xlsx_text(data)?);
        (text, "xlsx".to_string(), 0, Vec::new())
    } else if extension == ".pdf" {
        let extracted = crate::pdf_text::extract_pdf_document(data)?;
        if extracted.text.is_empty() {
            if !ocr_enabled {
                return Err(AppError {
                    message: "Scanned/image-only PDF requires OCR. Enable OCR and retry, or convert it to selectable text first.".to_string(),
                    code: codes::OCR_REQUIRED,
                    status: 422,
                });
            }
            let (ocr_text, page_texts) = crate::ocr::ocr_pdf_document(data)?;
            let text = normalize_extracted_text(&ocr_text);
            (
                text.clone(),
                "pdf".to_string(),
                extracted.page_count,
                page_texts,
            )
        } else {
            let text = normalize_extracted_text(&extracted.text);
            (
                text,
                "pdf".to_string(),
                extracted.page_count,
                extracted.page_texts,
            )
        }
    } else if extension == ".epub" {
        let text = normalize_extracted_text(&crate::docx_text::extract_epub_text(data)?);
        (text, "epub".to_string(), 0, Vec::new())
    } else if is_image_file(&extension, content_type) {
        if !ocr_enabled {
            return Err(AppError {
                message: "Image OCR requires OCR to be enabled. Enable OCR and retry, or set OCR_ENABLED=1.".to_string(),
                code: codes::OCR_REQUIRED,
                status: 415,
            });
        }
        let text = normalize_extracted_text(&crate::ocr::ocr_image(data)?);
        let page_texts = vec![json!({"page": 1, "text": text})];
        (text, "image".to_string(), 1, page_texts)
    } else if is_text_file(&extension, content_type, data) {
        let raw = if HTML_EXTENSIONS.contains(&extension.as_str()) {
            crate::fetch_url::extract_html_text(data)
        } else {
            crate::fetch_url::decode_text_file(data)
        };
        let text = normalize_extracted_text(&raw);
        let kind = {
            let stripped = extension.trim_start_matches('.');
            if stripped.is_empty() {
                "text"
            } else {
                stripped
            }
        };
        (text, kind.to_string(), 0, Vec::new())
    } else {
        return Err(AppError {
            message: "Unsupported file type. Use txt, md, csv, json, code files, docx, xlsx, pptx, epub, pdf, image, or text-based files.".to_string(),
            code: codes::UNSUPPORTED_FILE,
            status: 415,
        });
    };
    if text.is_empty() {
        return Err(AppError {
            message: "No readable text found in this file".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 422,
        });
    }
    let file_id = file_content_id(filename, data);
    let chunks = chunk_text(&text);
    let stored_pages = normalized_page_texts(Some(&Value::Array(page_texts.clone())));
    crate::extraction_control::check()?;
    write_cache(&CachedFile {
        root,
        file_id: &file_id,
        filename,
        content_type,
        kind: &kind,
        text: &text,
        chunks: &chunks,
        source_bytes: data,
        project_id,
        page_count,
        page_texts: &stored_pages,
    })?;
    let preview = preview_of(&text);
    Ok(json!({
        "name": filename,
        "type": content_type,
        "size": data.len(),
        "kind": kind,
        "fileId": file_id,
        "projectId": project_id.unwrap_or(""),
        "sourceAvailable": true,
        "text": preview,
        "preview": preview,
        "pageCount": page_count,
        "charCount": text.chars().count(),
        "chunkCount": chunks.len(),
        "chunked": chunks.len() > 1,
        "truncated": false,
    }))
}

/// `cleanup_file_cache`: drop indexes older than the age cap, then stop once the
/// remaining bytes would pass the size cap. Newest files are kept.
pub fn cleanup_file_cache(root: &Path) {
    let directory = file_cache_dir(root);
    let Ok(entries) = fs::read_dir(&directory) else {
        return;
    };
    let mut files: Vec<(PathBuf, std::time::SystemTime, u64)> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        let modified = metadata
            .modified()
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        files.push((path, modified, metadata.len()));
    }
    files.sort_by_key(|item| Reverse(item.1));
    let now = std::time::SystemTime::now();
    let mut total = 0u64;
    for (path, modified, size) in files {
        let source = path.with_extension("source");
        let source_size = fs::metadata(&source).map(|meta| meta.len()).unwrap_or(0);
        let age_days = now
            .duration_since(modified)
            .map(|elapsed| elapsed.as_secs_f64() / 86400.0)
            .unwrap_or(0.0);
        let entry_size = size + source_size;
        if age_days > FILE_CACHE_MAX_AGE_DAYS || total + entry_size > FILE_CACHE_MAX_BYTES {
            let _ = fs::remove_file(&path);
            let _ = fs::remove_file(&source);
            continue;
        }
        total += entry_size;
    }
}

/// The route body: extract every part, and raise the first error when nothing
/// succeeded. A mix of one success and one refusal is a 200 with `errors`.
pub fn extract_upload_batch(
    root: &Path,
    files: &[UploadedPart],
    ocr_enabled: bool,
    project_id: Option<&str>,
) -> Result<Value, AppError> {
    if files.is_empty() {
        return Err(AppError {
            message: "No file uploaded".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let mut extracted = Vec::new();
    let mut errors = Vec::new();
    for file in files {
        crate::extraction_control::check()?;
        match extract_uploaded_file(
            root,
            &file.filename,
            &file.content_type,
            &file.data,
            ocr_enabled,
            project_id,
        ) {
            Ok(value) => extracted.push(value),
            Err(error) => errors.push(json!({
                "name": file.filename,
                "error": error.message,
                "code": error.code,
                "status": error.status,
            })),
        }
    }
    if extracted.is_empty() {
        let message = errors
            .first()
            .and_then(|item| item.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("No file uploaded");
        let code = errors
            .first()
            .and_then(|item| item.get("code"))
            .and_then(Value::as_str)
            .unwrap_or(codes::INVALID_PAYLOAD);
        let status = errors
            .first()
            .and_then(|item| item.get("status"))
            .and_then(Value::as_u64)
            .unwrap_or(400) as u16;
        return Err(AppError {
            message: message.to_string(),
            code: leak_code(code),
            status,
        });
    }
    cleanup_file_cache(root);
    Ok(json!({
        "files": extracted,
        "errors": errors,
        "file": extracted.first().cloned().unwrap_or(Value::Null),
    }))
}

fn leak_code(code: &str) -> &'static str {
    match code {
        codes::INVALID_PAYLOAD => codes::INVALID_PAYLOAD,
        codes::UNSUPPORTED_FILE => codes::UNSUPPORTED_FILE,
        codes::OCR_REQUIRED => codes::OCR_REQUIRED,
        codes::OCR_EMPTY => codes::OCR_EMPTY,
        codes::OCR_UNAVAILABLE => codes::OCR_UNAVAILABLE,
        codes::UPLOAD_TOO_LARGE => codes::UPLOAD_TOO_LARGE,
        codes::INTERNAL => codes::INTERNAL,
        _ => codes::INVALID_PAYLOAD,
    }
}
