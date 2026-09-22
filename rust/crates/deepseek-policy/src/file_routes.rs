//! The file preview/source helpers `web/routes/files.py` and `web/server.py` need.
//!
//! What the routes cannot compute themselves, and each one is a small rule that has to
//! match the oracle exactly:
//!
//! - [`clean_filename`] (`core/utils.py`) — the basename of a path, stripped of
//!   quotes, capped at 180 characters.
//! - [`content_disposition_header`] (`web/http_utils.py`) — the RFC 5987 two-part
//!   header: an ASCII-only `filename=` plus a percent-encoded `filename*=UTF-8''…`.
//! - [`original_file_media_type`] (`web/server.py`) — the media type served for an
//!   uploaded file's *original* bytes, which is deliberately narrower than a
//!   general-purpose guesser.
//! - [`cached_file_source`] (`infra/rag/files.py`) — the index plus the path of the
//!   original upload.
//! - [`file_reader_window`] and [`file_chunk`] — the paginated reader the frontend
//!   scrolls a long extraction with.
//!
//! # The percent-encoding is Python's `quote`, not a URL encoder
//!
//! `urllib.parse.quote` leaves `A-Za-z0-9_.-~` and, **by default, `/`** unescaped.
//! A URL-oriented encoder would escape the slash, and the header would differ. The
//! `SAFE` set below is the one Python uses for this call.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::file_cache::{FileCache, file_cache_dir, load_cached_file, project_file_cache_dir};

/// `FILE_SOURCE_SUFFIX`.
pub const FILE_SOURCE_SUFFIX: &str = ".source";
/// `FILE_READER_DEFAULT_CHUNKS`.
pub const FILE_READER_DEFAULT_CHUNKS: usize = 6;
/// `FILE_READER_MAX_CHUNKS`.
pub const FILE_READER_MAX_CHUNKS: usize = 12;
/// `FILE_PAGE_TEXT_CHARS`.
pub const FILE_PAGE_TEXT_CHARS: usize = 40_000;

/// Mirrors `clean_filename`.
///
/// `re.split(r"[\\/]", value.strip().strip('"'))[-1][:180] or "uploaded-file"`. The
/// order matters: strip whitespace, strip surrounding double quotes, split on either
/// separator, take the last piece, cap at 180 characters — and only then fall back, so
/// a name that is entirely separators or quotes becomes `uploaded-file`.
pub fn clean_filename(value: &str) -> String {
    let trimmed = value.trim().trim_matches('"');
    let base = trimmed.rsplit(['\\', '/']).next().unwrap_or("");
    let capped: String = base.chars().take(180).collect();
    if capped.is_empty() {
        "uploaded-file".to_string()
    } else {
        capped
    }
}

/// The characters `urllib.parse.quote` leaves alone by default.
fn quote_safe(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~' | b'/')
}

/// `urllib.parse.quote(value)` — UTF-8 bytes, with `/` kept.
fn python_quote(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if quote_safe(*byte) {
            out.push(*byte as char);
        } else {
            out.push('%');
            out.push_str(&format!("{byte:02X}"));
        }
    }
    out
}

/// Mirrors `content_disposition_header`.
///
/// The ASCII fallback is built by **dropping** every non-ASCII character
/// (`encode("ascii", errors="ignore")`), then removing any remaining double quotes so
/// the header cannot be broken out of. A name that is entirely non-ASCII therefore
/// falls back to the literal `document`, which is what the oracle sends.
pub fn content_disposition_header(disposition: &str, filename: &str) -> String {
    let safe_name = clean_filename(filename);
    let mut ascii_name: String = safe_name.chars().filter(char::is_ascii).collect();
    ascii_name.retain(|character| character != '"');
    if ascii_name.is_empty() {
        ascii_name = "document".to_string();
    }
    format!(
        "{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{}",
        python_quote(&safe_name)
    )
}

/// Mirrors `original_file_media_type`.
///
/// The order is the rule: a PDF wins outright, an image keeps its own type unless it is
/// SVG (which is served as a download, not rendered), a `text/*` that is not HTML gets
/// the UTF-8 charset appended, a recognised text-ish `kind` becomes `text/plain`, the
/// three OOXML types pass through, and everything else is `application/octet-stream`.
pub fn original_file_media_type(cached: &Value) -> String {
    let kind = crate::python_json::value_str(cached.get("kind").unwrap_or(&Value::Null))
        .trim()
        .to_ascii_lowercase();
    let raw_type = crate::python_json::value_str(cached.get("type").unwrap_or(&Value::Null))
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if kind == "pdf" || raw_type == "application/pdf" {
        return "application/pdf".to_string();
    }
    if kind == "image" && raw_type.starts_with("image/") && raw_type != "image/svg+xml" {
        return raw_type;
    }
    if raw_type.starts_with("text/") && raw_type != "text/html" {
        return format!("{raw_type}; charset=utf-8");
    }
    if [
        "txt", "text", "md", "csv", "json", "xml", "log", "py", "js", "ts", "css",
    ]
    .contains(&kind.as_str())
    {
        return "text/plain; charset=utf-8".to_string();
    }
    if [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]
    .contains(&raw_type.as_str())
    {
        return raw_type;
    }
    "application/octet-stream".to_string()
}

/// Mirrors `cached_file_source`: the index plus the path of the original upload.
///
/// A missing source file is `410 file_index_expired`, not `404`: the index exists but
/// the bytes are gone, which is a different repair for the user than an unknown id.
pub fn cached_file_source(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    cache: &FileCache,
) -> Result<(Value, PathBuf), AppError> {
    let cached = load_cached_file(root, file_id, project_id, cache)?;
    let directory = match project_id.filter(|id| !id.is_empty()) {
        Some(id) => project_file_cache_dir(root, id)?,
        None => file_cache_dir(root),
    };
    let source_path = directory.join(format!("{file_id}{FILE_SOURCE_SUFFIX}"));
    if !source_path.exists() {
        return Err(AppError {
            message: "Original uploaded file has expired or is missing".to_string(),
            code: codes::FILE_INDEX_EXPIRED,
            status: 410,
        });
    }
    Ok((cached, source_path))
}

/// `_cached_chunk_list`: the `chunks` array, or empty when it is not a list.
fn cached_chunk_list(cached: &Value) -> &[Value] {
    cached
        .get("chunks")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

/// `int(cached.get(key) or 0)` — the `or` runs first, so a null or falsy value is `0`,
/// and a string that is not an integer is `0` rather than an error (the oracle's
/// `int(...)` would raise, but every producer writes a number).
fn int_field_or_zero(cached: &Value, key: &str) -> i64 {
    let raw = match cached.get(key) {
        Some(value) if crate::core_utils::python_truthy(value) => value.clone(),
        _ => return 0,
    };
    match &raw {
        Value::Number(number) => number.as_i64().unwrap_or(0),
        Value::String(text) => text.trim().parse::<i64>().unwrap_or(0),
        Value::Bool(flag) => i64::from(*flag),
        _ => 0,
    }
}

/// `cached.get(key) or <fallback>` — the `or` runs before any stringification.
fn text_field_or(cached: &Value, key: &str, fallback: &str) -> String {
    let raw = crate::core_utils::text_or_empty(cached.get(key));
    if raw.is_empty() {
        fallback.to_string()
    } else {
        raw
    }
}

/// `_reader_positive_int`: `None` and `""` take the default; anything else must parse
/// and is floored at 1.
///
/// The order matters — `value in (None, "")` is checked *before* `int(value)`, so a
/// missing field is the default while a non-numeric string is a `400`.
pub fn reader_positive_int(
    value: Option<&Value>,
    message: &str,
    default: usize,
) -> Result<usize, AppError> {
    let Some(value) = value else {
        return Ok(default);
    };
    if value.is_null() {
        return Ok(default);
    }
    if let Value::String(text) = value {
        if text.is_empty() {
            return Ok(default);
        }
    }
    let parsed = match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    };
    let Some(parsed) = parsed else {
        return Err(AppError {
            message: message.to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    Ok((parsed.trunc() as i64).max(1) as usize)
}

/// `_reader_file_payload`.
pub fn reader_file_payload(
    cached: &Value,
    file_id: &str,
    project_id: Option<&str>,
    total_chunks: usize,
) -> Value {
    // `int(cached.get("chunkCount") or total_chunks)` — the *fallback* is the window's
    // own count, not zero.
    let chunk_count = match cached.get("chunkCount") {
        Some(value) if crate::core_utils::python_truthy(value) => {
            int_field_or_zero(cached, "chunkCount")
        }
        _ => total_chunks as i64,
    };
    json!({
        "name": text_field_or(cached, "name", "文件"),
        "kind": text_field_or(cached, "kind", "text"),
        "type": text_field_or(cached, "type", ""),
        "size": int_field_or_zero(cached, "size"),
        "charCount": int_field_or_zero(cached, "charCount"),
        "chunkCount": chunk_count,
        "pageCount": int_field_or_zero(cached, "pageCount"),
        "fileId": file_id,
        "projectId": project_id.unwrap_or(""),
        "sourceAvailable": cached
            .get("sourceAvailable")
            .is_some_and(crate::core_utils::python_truthy),
    })
}

/// `_reader_chunk_payload`.
pub fn reader_chunk_payload(chunk: &Value, fallback_index: usize) -> Value {
    // `int(raw_index) + 1 if raw_index is not None else fallback_index + 1`, with a
    // parse failure falling back to the window position.
    let display_index = match chunk.get("index") {
        None | Some(Value::Null) => fallback_index as i64 + 1,
        Some(value) => match value {
            Value::Number(number) => number.as_i64().map(|parsed| parsed + 1),
            Value::String(text) => text.trim().parse::<i64>().ok().map(|parsed| parsed + 1),
            Value::Bool(flag) => Some(i64::from(*flag) + 1),
            _ => None,
        }
        .unwrap_or(fallback_index as i64 + 1),
    };
    json!({
        "index": display_index,
        "start": int_field_or_zero(chunk, "start"),
        "end": int_field_or_zero(chunk, "end"),
        "lineStart": int_field_or_zero(chunk, "lineStart"),
        "lineEnd": int_field_or_zero(chunk, "lineEnd"),
        "text": text_field_or(chunk, "text", ""),
    })
}

/// Mirrors `file_reader_window`: one window of the extracted chunks.
///
/// The window is `[start, start + count)` over the cached chunk list, and the reported
/// `chunkStart`/`chunkEnd` are **1-based** and count only the chunks that were
/// payloads — a non-object entry in the list is skipped by the comprehension *and* by
/// the end index, so `chunkEnd - chunkStart + 1` can be less than `chunkCount` when the
/// list is malformed. An empty list is its own shape: start/end/count are all `0`.
pub fn file_reader_window(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    chunk_start: Option<&Value>,
    chunk_count: Option<&Value>,
    cache: &FileCache,
) -> Result<Value, AppError> {
    let cached = load_cached_file(root, file_id, project_id, cache)?;
    let requested_start = reader_positive_int(chunk_start, "Invalid reader start", 1)?;
    let requested_count = FILE_READER_MAX_CHUNKS.min(reader_positive_int(
        chunk_count,
        "Invalid reader count",
        FILE_READER_DEFAULT_CHUNKS,
    )?);
    let chunks = cached_chunk_list(&cached);
    let total_chunks = chunks.len();
    if total_chunks == 0 {
        return Ok(json!({
            "ok": true,
            "file": reader_file_payload(&cached, file_id, project_id, total_chunks),
            "window": {
                "chunkStart": 0,
                "chunkEnd": 0,
                "chunkCount": 0,
                "totalChunks": 0,
                "hasPrevious": false,
                "hasNext": false,
            },
            "chunks": [],
        }));
    }
    let start_index = requested_start.saturating_sub(1).min(total_chunks - 1);
    let end = (start_index + requested_count).min(total_chunks);
    let mut normalized: Vec<Value> = Vec::new();
    for (offset, chunk) in chunks[start_index..end].iter().enumerate() {
        if chunk.is_object() {
            normalized.push(reader_chunk_payload(chunk, start_index + offset));
        }
    }
    let end_index = start_index + normalized.len();
    Ok(json!({
        "ok": true,
        "file": reader_file_payload(&cached, file_id, project_id, total_chunks),
        "window": {
            "chunkStart": start_index + 1,
            "chunkEnd": end_index,
            "chunkCount": normalized.len(),
            "totalChunks": total_chunks,
            "hasPrevious": start_index > 0,
            "hasNext": end_index < total_chunks,
        },
        "chunks": normalized,
    }))
}

/// Mirrors `web/server.py`'s `/api/file-chunk` body: one chunk, 1-based, with the
/// file's own name and kind beside it.
///
/// The index is `max(0, int(payload.get("chunkIndex") or 0) - 1)`, so `0` and a
/// missing field both mean the first chunk and a negative value is clamped to it. A
/// non-numeric value is a `400`, and an index past the end is a `404`.
pub fn file_chunk(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    chunk_index: Option<&Value>,
    cache: &FileCache,
) -> Result<Value, AppError> {
    let raw = chunk_index
        .filter(|value| crate::core_utils::python_truthy(value))
        .cloned()
        .unwrap_or_else(|| json!(0));
    let parsed = match &raw {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.trim().parse::<i64>().ok(),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    };
    let Some(parsed) = parsed else {
        return Err(AppError {
            message: "Invalid chunk index".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    let index = (parsed - 1).max(0) as usize;

    let cached = load_cached_file(root, file_id, project_id, cache)?;
    let chunks = cached_chunk_list(&cached);
    let Some(chunk) = chunks.get(index).filter(|chunk| chunk.is_object()) else {
        return Err(AppError {
            message: "Chunk not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    };
    Ok(json!({
        "file": {
            "name": cached.get("name").cloned().unwrap_or(Value::Null),
            "kind": cached.get("kind").cloned().unwrap_or(Value::Null),
            "fileId": file_id,
            "projectId": project_id.unwrap_or(""),
        },
        "chunk": chunk,
    }))
}

/// Mirrors `normalize_extracted_text`: CRLF and CR to LF, NUL dropped, trailing
/// whitespace stripped from every line, then the whole string trimmed.
pub fn normalize_extracted_text(value: &str) -> String {
    let text = value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\u{0}', "");
    let lines: Vec<&str> = text.split('\n').map(str::trim_end).collect();
    lines.join("\n").trim().to_string()
}

/// `normalized_page_texts`: the usable page entries, each capped at
/// [`FILE_PAGE_TEXT_CHARS`].
///
/// A non-object entry is skipped, a page number that is not a positive integer is
/// skipped, and a page whose text normalizes to nothing is skipped — so an entry that
/// survives is guaranteed to have both a page and text.
pub fn normalized_page_texts(value: Option<&Value>) -> Vec<Value> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut pages: Vec<Value> = Vec::new();
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let page = match object.get("page") {
            Some(value) if crate::core_utils::python_truthy(value) => {
                match crate::python_json::value_str(value).trim().parse::<i64>() {
                    Ok(parsed) => parsed,
                    Err(_) => continue,
                }
            }
            _ => 0,
        };
        let text = normalize_extracted_text(&crate::core_utils::text_or_empty(object.get("text")));
        if page <= 0 || text.is_empty() {
            continue;
        }
        let capped: String = text.chars().take(FILE_PAGE_TEXT_CHARS).collect();
        pages.push(json!({"page": page, "text": capped}));
    }
    pages
}

/// `page_text_for_index`: the text of one page, or empty.
pub fn page_text_for_index(page_texts: &[Value], requested_page: i64) -> String {
    for item in page_texts {
        let page = match item.get("page") {
            Some(value) if crate::core_utils::python_truthy(value) => {
                crate::python_json::value_str(value)
                    .trim()
                    .parse::<i64>()
                    .unwrap_or(0)
            }
            _ => 0,
        };
        if page == requested_page {
            return crate::core_utils::text_or_empty(item.get("text"));
        }
    }
    String::new()
}

/// `page_text_from_cached_chunks`: the fallback when a file has no page texts — the
/// joined chunk text, split evenly across the page count.
///
/// The split is by **characters** (`len(text)` is a character count in Python), and the
/// last page takes everything that is left, which is why a page count that does not
/// divide the text evenly still ends on the final character.
pub fn page_text_from_cached_chunks(
    cached: &Value,
    requested_page: i64,
    page_count: i64,
) -> String {
    let text = cached_chunk_list(cached)
        .iter()
        .filter(|chunk| chunk.is_object())
        .map(|chunk| crate::core_utils::text_or_empty(chunk.get("text")))
        .collect::<Vec<String>>()
        .join("\n\n");
    let text = text.trim().to_string();
    if text.is_empty() {
        return String::new();
    }
    if page_count <= 1 {
        return text;
    }
    let characters: Vec<char> = text.chars().collect();
    let length = characters.len() as i64;
    let per_page = (length / page_count).max(1);
    let start = (requested_page - 1) * per_page;
    let end = if requested_page >= page_count {
        length
    } else {
        length.min(requested_page * per_page)
    };
    let start = start.clamp(0, length) as usize;
    let end = end.clamp(0, length) as usize;
    if start >= end {
        return String::new();
    }
    characters[start..end]
        .iter()
        .collect::<String>()
        .trim()
        .to_string()
}

/// Mirrors `file_page_text`: one page of a PDF/image extraction.
///
/// The page count is raised to cover the highest page a `pageTexts` entry names, and
/// floored at 1 — so a file with no pages still reports one, and the requested page is
/// clamped into that range rather than refused.
pub fn file_page_text(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    page: Option<&Value>,
    cache: &FileCache,
) -> Result<Value, AppError> {
    let cached = load_cached_file(root, file_id, project_id, cache)?;
    let requested_page = reader_positive_int(page, "Invalid page", 1)? as i64;
    let mut page_count = int_field_or_zero(&cached, "pageCount");
    let page_texts = normalized_page_texts(cached.get("pageTexts"));
    if !page_texts.is_empty() {
        let highest = page_texts
            .iter()
            .map(|item| {
                item.get("page")
                    .map(|value| {
                        crate::python_json::value_str(value)
                            .trim()
                            .parse::<i64>()
                            .unwrap_or(0)
                    })
                    .unwrap_or(0)
            })
            .max()
            .unwrap_or(0);
        page_count = page_count.max(highest);
    }
    let page_count = page_count.max(1);
    let requested_page = requested_page.min(page_count);
    let mut page_text = page_text_for_index(&page_texts, requested_page);
    if page_text.is_empty() {
        page_text = page_text_from_cached_chunks(&cached, requested_page, page_count);
    }
    let capped: String = page_text.chars().take(FILE_PAGE_TEXT_CHARS).collect();
    Ok(json!({
        "ok": true,
        "file": reader_file_payload(&cached, file_id, project_id, cached_chunk_list(&cached).len()),
        "page": {
            "index": requested_page,
            "pageCount": page_count,
            "text": capped,
            "hasText": !page_text.trim().is_empty(),
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn clean_filename_takes_the_basename_and_caps_it() {
        assert_eq!(clean_filename("report.pdf"), "report.pdf");
        assert_eq!(clean_filename(r"C:\Users\me\report.pdf"), "report.pdf");
        assert_eq!(clean_filename("/tmp/a/b/report.pdf"), "report.pdf");
        assert_eq!(clean_filename("  report.pdf  "), "report.pdf");
        assert_eq!(clean_filename("\"report.pdf\""), "report.pdf");
        // The fallback runs *after* the cap, so a name of separators is empty.
        assert_eq!(clean_filename("///"), "uploaded-file");
        assert_eq!(clean_filename(""), "uploaded-file");
        assert_eq!(clean_filename("\"\""), "uploaded-file");
        // 180 characters, and the 181st is dropped.
        assert_eq!(clean_filename(&"a".repeat(200)).chars().count(), 180);
    }

    #[test]
    fn the_disposition_header_is_the_oracles_two_part_form() {
        assert_eq!(
            content_disposition_header("inline", "report.pdf"),
            "inline; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        );
        assert_eq!(
            content_disposition_header("attachment", "my report.pdf"),
            "attachment; filename=\"my report.pdf\"; filename*=UTF-8''my%20report.pdf"
        );
        // A CJK name drops out of the ASCII part entirely, leaving the extension —
        // measured against the oracle, which sends `.pdf` rather than the `document`
        // fallback, because `errors="ignore"` drops the non-ASCII *characters* and the
        // `.pdf` that follows them survives.
        assert_eq!(
            content_disposition_header("inline", "报告.pdf"),
            "inline; filename=\".pdf\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"
        );
        // A name whose ASCII part is empty *does* fall back.
        assert_eq!(
            content_disposition_header("inline", "报告"),
            "inline; filename=\"document\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A"
        );
        // The name is cleaned first, so a path's directory part never reaches either
        // form of the header.
        assert_eq!(
            content_disposition_header("inline", "a/b.txt"),
            "inline; filename=\"b.txt\"; filename*=UTF-8''b.txt"
        );
        // A quote inside the name cannot break out of the header.
        assert_eq!(
            content_disposition_header("inline", "a\"b.txt"),
            "inline; filename=\"ab.txt\"; filename*=UTF-8''a%22b.txt"
        );
    }

    #[test]
    fn the_media_type_follows_the_oracles_order() {
        // A PDF kind wins over a wrong `type`.
        assert_eq!(
            original_file_media_type(&json!({"kind": "pdf", "type": "text/plain"})),
            "application/pdf"
        );
        assert_eq!(
            original_file_media_type(&json!({"type": "application/pdf"})),
            "application/pdf"
        );
        // An image keeps its own type, except SVG.
        assert_eq!(
            original_file_media_type(&json!({"kind": "image", "type": "image/png"})),
            "image/png"
        );
        assert_eq!(
            original_file_media_type(&json!({"kind": "image", "type": "image/svg+xml"})),
            "application/octet-stream"
        );
        // `text/*` gets the charset, except HTML.
        assert_eq!(
            original_file_media_type(&json!({"type": "text/markdown"})),
            "text/markdown; charset=utf-8"
        );
        assert_eq!(
            original_file_media_type(&json!({"type": "text/html"})),
            "application/octet-stream"
        );
        // A recognised text-ish kind becomes `text/plain`.
        assert_eq!(
            original_file_media_type(&json!({"kind": "csv"})),
            "text/plain; charset=utf-8"
        );
        // The three OOXML types pass through; anything else is octet-stream.
        assert_eq!(
            original_file_media_type(&json!({
                "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            })),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        );
        assert_eq!(
            original_file_media_type(&json!({"kind": "zip", "type": "application/zip"})),
            "application/octet-stream"
        );
        assert_eq!(
            original_file_media_type(&json!({})),
            "application/octet-stream"
        );
        // The `type` is split at the first `;` before it is compared.
        assert_eq!(
            original_file_media_type(&json!({"type": "text/plain; charset=utf-16"})),
            "text/plain; charset=utf-8"
        );
    }

    #[test]
    fn the_reader_window_is_one_based_and_clamped() {
        let root = std::env::temp_dir().join(format!("file-reader-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let directory = file_cache_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file_id = "0123456789abcdef0123456789abcdef";
        let chunks: Vec<Value> = (0..5)
            .map(|index| json!({"index": index, "text": format!("chunk {index}")}))
            .collect();
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({"name": "a.txt", "kind": "txt", "size": 42, "chunks": chunks}).to_string(),
        )
        .unwrap();
        let cache = FileCache::new();

        // The default window is the first six chunks, which is all five here.
        let window = file_reader_window(&root, file_id, None, None, None, &cache).unwrap();
        assert_eq!(window["window"]["chunkStart"], 1);
        assert_eq!(window["window"]["chunkEnd"], 5);
        assert_eq!(window["window"]["chunkCount"], 5);
        assert_eq!(window["window"]["totalChunks"], 5);
        assert_eq!(window["window"]["hasPrevious"], false);
        assert_eq!(window["window"]["hasNext"], false);
        assert_eq!(window["file"]["name"], "a.txt");
        assert_eq!(window["file"]["chunkCount"], 5);
        assert_eq!(window["file"]["sourceAvailable"], false);

        // A start past the end clamps to the last chunk, not to an empty window.
        let window = file_reader_window(
            &root,
            file_id,
            None,
            Some(&json!(99)),
            Some(&json!(2)),
            &cache,
        )
        .unwrap();
        assert_eq!(window["window"]["chunkStart"], 5);
        assert_eq!(window["window"]["chunkEnd"], 5);
        assert_eq!(window["window"]["hasPrevious"], true);
        assert_eq!(window["window"]["hasNext"], false);

        // A count above the cap is clamped to 12, and a start of 0 floors to 1.
        let window = file_reader_window(
            &root,
            file_id,
            None,
            Some(&json!(0)),
            Some(&json!(99)),
            &cache,
        )
        .unwrap();
        assert_eq!(window["window"]["chunkStart"], 1);
        assert_eq!(window["window"]["chunkCount"], 5);

        // A non-numeric start is a 400; `""` and null take the default.
        let error = file_reader_window(&root, file_id, None, Some(&json!("x")), None, &cache)
            .expect_err("a non-numeric start must be refused");
        assert_eq!(error.code, codes::INVALID_PAYLOAD);
        assert_eq!(error.message, "Invalid reader start");
        for default in [Value::Null, json!("")] {
            assert!(file_reader_window(&root, file_id, None, Some(&default), None, &cache).is_ok());
        }

        // An empty chunk list is its own shape: every window field is zero.
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({"name": "empty.txt", "chunks": []}).to_string(),
        )
        .unwrap();
        let cache = FileCache::new();
        let window = file_reader_window(&root, file_id, None, None, None, &cache).unwrap();
        assert_eq!(window["window"]["chunkStart"], 0);
        assert_eq!(window["window"]["chunkEnd"], 0);
        assert_eq!(window["window"]["totalChunks"], 0);
        assert_eq!(window["chunks"], json!([]));
        // The file payload's `kind` falls back, and `chunkCount` falls back to the
        // window's own total.
        assert_eq!(window["file"]["kind"], "text");
        assert_eq!(window["file"]["name"], "empty.txt");
        assert_eq!(window["file"]["chunkCount"], 0);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_malformed_chunk_list_is_skipped_by_the_window() {
        let root = std::env::temp_dir().join(format!("file-reader-bad-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let directory = file_cache_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file_id = "0123456789abcdef0123456789abcdef";
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({"name": "a.txt", "chunks": [
                {"index": 0, "text": "first"},
                "not an object",
                {"text": "third", "index": "2"},
            ]})
            .to_string(),
        )
        .unwrap();
        let cache = FileCache::new();
        let window = file_reader_window(&root, file_id, None, None, None, &cache).unwrap();
        // Three entries, two of which are payloads: the count is 2 and the end index
        // counts only the payloads, which is what the oracle's comprehension does.
        assert_eq!(window["window"]["totalChunks"], 3);
        assert_eq!(window["window"]["chunkCount"], 2);
        assert_eq!(window["window"]["chunkEnd"], 2);
        assert_eq!(window["chunks"][0]["index"], 1);
        assert_eq!(window["chunks"][0]["text"], "first");
        // The third chunk's own `index: "2"` is parsed and displayed as 3.
        assert_eq!(window["chunks"][1]["index"], 3);
        assert_eq!(window["chunks"][1]["text"], "third");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn one_chunk_is_one_based_and_bounds_checked() {
        let root = std::env::temp_dir().join(format!("file-chunk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let directory = file_cache_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file_id = "0123456789abcdef0123456789abcdef";
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({"name": "a.txt", "kind": "txt", "chunks": [
                {"index": 0, "text": "first"},
                {"index": 1, "text": "second"},
            ]})
            .to_string(),
        )
        .unwrap();
        let cache = FileCache::new();

        // `chunkIndex` is 1-based in the request and the answer echoes the raw chunk.
        let chunk = file_chunk(&root, file_id, None, Some(&json!(2)), &cache).unwrap();
        assert_eq!(chunk["chunk"]["text"], "second");
        assert_eq!(chunk["file"]["name"], "a.txt");
        assert_eq!(chunk["file"]["kind"], "txt");
        assert_eq!(chunk["file"]["fileId"], file_id);
        assert_eq!(chunk["file"]["projectId"], "");

        // Missing, null and 0 all mean the first chunk; a negative index clamps to it.
        for index in [
            None,
            Some(json!(Value::Null)),
            Some(json!(0)),
            Some(json!(-5)),
        ] {
            let chunk = file_chunk(&root, file_id, None, index.as_ref(), &cache).unwrap();
            assert_eq!(chunk["chunk"]["text"], "first", "{index:?}");
        }
        // Past the end is a 404; a non-numeric index is a 400.
        let error = file_chunk(&root, file_id, None, Some(&json!(3)), &cache).unwrap_err();
        assert_eq!(error.code, codes::NOT_FOUND);
        assert_eq!(error.message, "Chunk not found");
        let error = file_chunk(&root, file_id, None, Some(&json!("x")), &cache).unwrap_err();
        assert_eq!(error.code, codes::INVALID_PAYLOAD);
        assert_eq!(error.message, "Invalid chunk index");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn text_normalization_follows_the_oracles_four_steps() {
        assert_eq!(normalize_extracted_text("a\r\nb\rc"), "a\nb\nc");
        assert_eq!(normalize_extracted_text("a\u{0}b"), "ab");
        // Trailing whitespace is stripped per line, then the whole string is trimmed —
        // which also strips the *leading* whitespace of the first line.
        assert_eq!(normalize_extracted_text("  a  \n  b  \n"), "a\n  b");
        assert_eq!(normalize_extracted_text("\n\n  \n"), "");
        assert_eq!(normalize_extracted_text(""), "");
        // Interior runs are *not* collapsed.
        assert_eq!(normalize_extracted_text("a    b"), "a    b");
    }

    #[test]
    fn page_texts_skip_unusable_entries() {
        let pages = normalized_page_texts(Some(&json!([
            {"page": 1, "text": "one"},
            {"page": 0, "text": "zero"},
            {"page": -1, "text": "negative"},
            {"page": 2, "text": "   "},
            {"page": 3},
            "not an object",
            {"page": "4", "text": "four"},
            {"page": "x", "text": "bad"},
            {"text": "no page"},
        ])));
        assert_eq!(pages.len(), 2);
        assert_eq!(pages[0], json!({"page": 1, "text": "one"}));
        assert_eq!(pages[1], json!({"page": 4, "text": "four"}));
        // Seven of the nine entries are dropped, and this list was measured against the
        // oracle rather than reasoned about: `{"page": 3}` has no text, `{"page": 2,
        // "text": "   "}` normalises to empty, and the malformed page values and the
        // non-object entry never become pages. Two survive, and there is no third.
        assert!(normalized_page_texts(None).is_empty());
        assert!(normalized_page_texts(Some(&json!("not a list"))).is_empty());
    }

    #[test]
    fn a_page_lookup_finds_the_page_or_answers_empty() {
        let pages = vec![
            json!({"page": 1, "text": "one"}),
            json!({"page": 3, "text": "three"}),
        ];
        assert_eq!(page_text_for_index(&pages, 1), "one");
        assert_eq!(page_text_for_index(&pages, 3), "three");
        assert_eq!(page_text_for_index(&pages, 2), "");
        assert_eq!(page_text_for_index(&[], 1), "");
    }

    #[test]
    fn the_chunk_fallback_splits_by_characters_and_the_last_page_takes_the_rest() {
        let cached = json!({"chunks": [
            {"text": "aaaa"}, {"text": "bbbb"}, {"text": "cccc"},
        ]});
        // The join is "\n\n", so the text is "aaaa\n\nbbbb\n\ncccc" (16 characters).
        let text = page_text_from_cached_chunks(&cached, 1, 4);
        assert_eq!(text, "aaaa");
        // A page count that does not divide evenly: the last page takes the remainder.
        let last = page_text_from_cached_chunks(&cached, 4, 4);
        assert!(last.ends_with("cccc"), "{last:?}");
        // One page is the whole text, trimmed.
        assert_eq!(
            page_text_from_cached_chunks(&cached, 1, 1),
            "aaaa\n\nbbbb\n\ncccc"
        );
        // No text at all is empty.
        assert_eq!(
            page_text_from_cached_chunks(&json!({"chunks": []}), 1, 2),
            ""
        );
        // Non-object entries are skipped before the join.
        assert_eq!(
            page_text_from_cached_chunks(&json!({"chunks": ["x", {"text": "only"}]}), 1, 1),
            "only"
        );
    }

    #[test]
    fn file_page_text_clamps_the_page_and_raises_the_count() {
        let root = std::env::temp_dir().join(format!("file-page-text-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let directory = file_cache_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file_id = "0123456789abcdef0123456789abcdef";
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({
                "name": "a.pdf", "kind": "pdf", "type": "application/pdf",
                "pageCount": 2,
                "pageTexts": [
                    {"page": 1, "text": "page one"},
                    {"page": 5, "text": "page five"},
                ],
                "chunks": [{"index": 0, "text": "chunk text"}],
            })
            .to_string(),
        )
        .unwrap();
        let cache = FileCache::new();

        // The count is raised to cover page 5, so page 3 is reachable and empty, and
        // its text falls back to the chunk split.
        let page = file_page_text(&root, file_id, None, Some(&json!(1)), &cache).unwrap();
        assert_eq!(page["page"]["index"], 1);
        assert_eq!(page["page"]["pageCount"], 5);
        assert_eq!(page["page"]["text"], "page one");
        assert_eq!(page["page"]["hasText"], true);
        assert_eq!(page["file"]["name"], "a.pdf");
        assert_eq!(page["file"]["chunkCount"], 1);

        let page = file_page_text(&root, file_id, None, Some(&json!(3)), &cache).unwrap();
        assert_eq!(page["page"]["index"], 3);
        assert_eq!(page["page"]["hasText"], true);
        // Not `"chunk text"`. The oracle splits the chunk text across the page count it
        // was raised to: `per_page = len("chunk text") // 5 = 2`, so page 3 is
        // `text[4:6]` = `"k "`, stripped to `"k"`. Measured by running the oracle, not
        // read off the code -- the honest answer for a page whose text is a slice of
        // one chunk is that the slice is what the API returns.
        assert_eq!(page["page"]["text"], "k");

        // A page past the end clamps to the last page; 0 and null take the default.
        let page = file_page_text(&root, file_id, None, Some(&json!(99)), &cache).unwrap();
        assert_eq!(page["page"]["index"], 5);
        for default in [Value::Null, json!(0), json!("")] {
            let page = file_page_text(&root, file_id, None, Some(&default), &cache).unwrap();
            assert_eq!(page["page"]["index"], 1, "{default:?}");
        }
        // A non-numeric page is the oracle's 400.
        let error = file_page_text(&root, file_id, None, Some(&json!("x")), &cache)
            .expect_err("a non-numeric page must be refused");
        assert_eq!(error.code, codes::INVALID_PAYLOAD);
        assert_eq!(error.message, "Invalid page");

        // A file with no pages at all still reports one.
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            json!({"name": "b.txt", "chunks": []}).to_string(),
        )
        .unwrap();
        let cache = FileCache::new();
        let page = file_page_text(&root, file_id, None, None, &cache).unwrap();
        assert_eq!(page["page"]["pageCount"], 1);
        assert_eq!(page["page"]["index"], 1);
        assert_eq!(page["page"]["text"], "");
        assert_eq!(page["page"]["hasText"], false);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_missing_source_file_is_a_410_not_a_404() {
        let root = std::env::temp_dir().join(format!("file-source-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let directory = file_cache_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file_id = "0123456789abcdef0123456789abcdef";
        std::fs::write(
            directory.join(format!("{file_id}.json")),
            r#"{"name":"report.pdf","kind":"pdf"}"#,
        )
        .unwrap();
        let cache = FileCache::new();

        // The index exists but the source does not: 410.
        let error = cached_file_source(&root, file_id, None, &cache)
            .expect_err("a missing source must fail");
        assert_eq!(error.code, codes::FILE_INDEX_EXPIRED);
        assert_eq!(error.status, 410);
        assert_eq!(
            error.message,
            "Original uploaded file has expired or is missing"
        );

        // With the source present the pair comes back.
        std::fs::write(
            directory.join(format!("{file_id}{FILE_SOURCE_SUFFIX}")),
            b"%PDF-1.4",
        )
        .unwrap();
        let (cached, path) = cached_file_source(&root, file_id, None, &cache).expect("source");
        assert_eq!(cached["name"], "report.pdf");
        assert_eq!(std::fs::read(&path).unwrap(), b"%PDF-1.4");

        // A malformed id is a 400, which `load_cached_file` raises before any path.
        let error = cached_file_source(&root, "../escape", None, &cache).expect_err("bad id");
        assert_eq!(error.code, codes::INVALID_PAYLOAD);
        let _ = std::fs::remove_dir_all(&root);
    }
}
