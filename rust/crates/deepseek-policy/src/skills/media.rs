//! `_media_context` — the media tail of a Skill run's context.
//!
//! The oracle is `deepseek_infra/infra/skills/runner.py:_media_context` plus the two
//! readers it calls, `deepseek_infra/infra/media/library.py`'s `get_media` /
//! `list_segments`, and the record and segment normalisation in
//! `deepseek_infra/infra/media/schema.py`.
//!
//! **What is ported, and what is deliberately not.** The consumer reads exactly ten
//! things — a record's `projectId`, `title`, `type`, `mediaId` and `status`, and a
//! segment's `type`, `text`, `index`, `segmentId` and `citation` — so those are the
//! fields this port produces. Two further classes of value are still *evaluated* even
//! though their results are discarded, because their normalisation is what decides
//! whether the oracle keeps a row at all: a record's `path` and a segment's `framePath`
//! raise on an absolute or escaping path (the row is dropped), and a segment's `index`
//! and `page` go through `int()`. Everything else `normalize_media_record` returns —
//! `mimeType`, `source`, `metadata`, `createdAt`, `updatedAt`, and a segment's
//! `confidence`, `page`, `timeRange` and `framePath` — is **not** produced here.
//! Nothing in this consumer reads it, and finishing it belongs with the media slice.
//! This is the line `projects::delete_project` draws for the RAG and media purge it
//! cannot run yet: say plainly what was not done rather than approximate it.
//!
//! **One divergence, recorded rather than modelled.** Python catches only `AppError`
//! around `normalize_segment`, so a raw `ValueError` from `int(...)` on a segment's
//! `index` or `page` escapes `_media_context` and aborts the whole Skill run with a
//! 500. This port answers `invalid_payload` (400) for those two cases while an ordinary
//! validation error still drops only the segment. `save_segments` normalises `index`
//! through the same `int()`, so a store the application wrote can never contain a
//! non-numeric one.

use super::{Result, error};
use crate::core_utils::{python_int_opt, python_truthy, text_or_empty};
use crate::python_json::value_str;
use crate::workspace_schema::{
    normalize_source_ref, normalize_title, redact_sensitive_text, validate_project_id,
    validate_workspace_id,
};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// `MEDIA_CONTEXT_MAX_CHARS`.
const MAX_CONTEXT_CHARS: usize = 24_000;
/// `MEDIA_SEGMENT_MAX_CHARS`.
const MAX_SEGMENT_CHARS: usize = 1_600;
/// `MEDIA_CONTEXT_MAX_MEDIA`.
const MAX_MEDIA: usize = 12;
/// `MEDIA_CONTEXT_MAX_SEGMENTS_PER_MEDIA`.
const MAX_SEGMENTS_PER_MEDIA: usize = 12;
/// `MAX_SEGMENT_TEXT_CHARS`.
const MAX_SEGMENT_TEXT_CHARS: usize = 120_000;

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

/// Whether a segment keeps its place in the list or takes the run down with it.
enum SegmentError {
    /// The oracle raises its own `AppError`, which `list_segments` catches: drop it.
    Skipped,
    /// The oracle raises a raw `ValueError`, which nothing catches: fail the call.
    Aborted(crate::app_error::AppError),
}

/// `str.strip()`.
///
/// Rust's `is_whitespace` is the Unicode `White_Space` property; Python's `str.strip()`
/// also removes `\x1c`-`\x1f`, which that property excludes. The difference is
/// invisible on ordinary text and byte-visible on the file/group/record/unit
/// separators, which is why this exists rather than a bare `trim()`.
fn py_strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `PurePosixPath(name).suffix`.
///
/// Python's rule is `i = name.rfind(".")` then `name[i:]` only when `0 < i < len - 1`,
/// which is what makes `".pdf"` and `"x."` have no suffix.
fn posix_suffix(name: &str) -> &str {
    let name = name.rsplit('/').next().unwrap_or("");
    match name.rfind('.') {
        Some(index) if index > 0 && index < name.len() - 1 => &name[index..],
        _ => "",
    }
}

/// `normalize_mime_type`.
fn normalize_mime_type(value: Option<&Value>) -> String {
    py_strip(text_or_empty(value).split(';').next().unwrap_or("")).to_lowercase()
}

/// `media_type_from_mime`. An empty answer means "no guess", not "no type".
fn media_type_from_mime(mime_type: &str, filename: &str) -> &'static str {
    let content_type = py_strip(mime_type.split(';').next().unwrap_or("")).to_lowercase();
    let suffix = posix_suffix(filename).to_lowercase();
    let suffix = suffix.as_str();
    if content_type.starts_with("image/") {
        return "image";
    }
    if content_type == "application/pdf" || suffix == ".pdf" {
        return "pdf";
    }
    if content_type.starts_with("audio/")
        || [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"].contains(&suffix)
    {
        return "audio";
    }
    if content_type.starts_with("video/")
        || [".mp4", ".mov", ".webm", ".mkv", ".avi"].contains(&suffix)
    {
        return "video";
    }
    if ["text/html", "application/xhtml+xml"].contains(&content_type.as_str())
        || [".html", ".htm"].contains(&suffix)
    {
        return "webpage";
    }
    ""
}

/// `normalize_media_type` — a type that is neither declared nor inferable **raises**.
fn normalize_media_type(value: Option<&Value>, mime_type: &str, filename: &str) -> Result<String> {
    let candidate = py_strip(&text_or_empty(value)).to_lowercase();
    if MEDIA_TYPES.contains(&candidate.as_str()) {
        return Ok(candidate);
    }
    let guessed = media_type_from_mime(mime_type, filename);
    if guessed.is_empty() {
        return Err(error("Unsupported media type", 400));
    }
    Ok(guessed.to_string())
}

/// `normalize_status`.
fn normalize_status(value: Option<&Value>, default: &str) -> Result<String> {
    let raw = text_or_empty(value);
    let candidate = if raw.is_empty() { default } else { &raw };
    let status = py_strip(candidate).to_lowercase();
    if !MEDIA_STATUSES.contains(&status.as_str()) {
        return Err(error("Unsupported media status", 400));
    }
    Ok(status)
}

/// `normalize_media_path`.
fn normalize_media_path(value: Option<&Value>) -> Result<String> {
    let replaced = text_or_empty(value).replace('\\', "/");
    let raw = py_strip(&replaced);
    if raw.is_empty() {
        return Ok(String::new());
    }
    let drive = {
        let bytes = raw.as_bytes();
        bytes.len() > 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'/'
    };
    if raw.starts_with('/') || drive {
        return Err(error(
            "Media path must be relative to the media library",
            400,
        ));
    }
    let parts: Vec<&str> = raw
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    if parts.iter().any(|part| *part == "..") {
        return Err(error("Media path must not escape the media library", 400));
    }
    Ok(parts.join("/"))
}

fn media_dir(root: &Path) -> PathBuf {
    root.join(".media")
}

fn store_path(root: &Path) -> PathBuf {
    media_dir(root).join("library.json")
}

fn segments_path(root: &Path, media_id: &str) -> Result<PathBuf> {
    let safe = validate_workspace_id(media_id, "media id")?;
    Ok(media_dir(root)
        .join("segments")
        .join(format!("{safe}.json")))
}

/// The fields of `normalize_media_record` that this consumer reads, plus the ones whose
/// normalisation can raise. `Err` is the oracle's `except AppError: continue`.
fn context_record(row: &Value) -> Result<Value> {
    let media_id = validate_workspace_id(&text_or_empty(row.get("mediaId")), "media id")?;
    let raw_project = py_strip(&text_or_empty(row.get("projectId"))).to_string();
    let project_id = if raw_project.is_empty() {
        raw_project
    } else {
        validate_project_id(&raw_project)?
    };
    let mime_type = normalize_mime_type(row.get("mimeType"));
    let media_type = normalize_media_type(
        row.get("type"),
        &mime_type,
        &text_or_empty(row.get("title")),
    )?;
    let title = normalize_title(row.get("title"), "Untitled media");
    // Evaluated for its ability to raise, which is what drops the row.
    normalize_media_path(row.get("path"))?;
    let status = normalize_status(row.get("status"), "pending")?;
    Ok(json!({
        "mediaId": media_id,
        "projectId": project_id,
        "type": media_type,
        "title": title,
        "status": status,
    }))
}

/// `_load_store`: normalise every row, drop the ones that raise.
fn load_store(root: &Path) -> Vec<Value> {
    let data = super::registry::read_json(&store_path(root));
    if !data.is_object() {
        return Vec::new();
    }
    data.get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|row| row.is_object())
        .filter_map(|row| context_record(row).ok())
        .collect()
}

/// `get_media`: the first normalised row carrying this id.
fn find_media(root: &Path, media_id: &str) -> Option<Value> {
    load_store(root)
        .into_iter()
        .find(|row| row.get("mediaId").and_then(Value::as_str) == Some(media_id))
}

/// `normalize_segment`, restricted to what survives into the context.
fn context_segment(
    row: &Value,
    media_id: &str,
    fallback_index: i64,
) -> std::result::Result<Value, SegmentError> {
    let raw_type = text_or_empty(row.get("type"));
    let candidate = if raw_type.is_empty() {
        "page_text"
    } else {
        &raw_type
    };
    let segment_type = py_strip(candidate).to_lowercase();
    if !SEGMENT_TYPES.contains(&segment_type.as_str()) {
        return Err(SegmentError::Skipped);
    }
    let raw_id = py_strip(&text_or_empty(row.get("segmentId"))).to_string();
    let segment_id = if raw_id.is_empty() {
        crate::workspace_schema::new_id("seg", &crate::entropy::SystemEntropy)
            .map_err(SegmentError::Aborted)?
    } else {
        validate_workspace_id(&raw_id, "segment id").map_err(|_| SegmentError::Skipped)?
    };
    let redacted = redact_sensitive_text(&text_or_empty(row.get("text")));
    let text: String = redacted.chars().take(MAX_SEGMENT_TEXT_CHARS).collect();
    let index = match row.get("index") {
        Some(value) if !value.is_null() => python_int_opt(Some(value)).ok_or_else(|| {
            SegmentError::Aborted(error("Media segment index must be an integer", 400))
        })?,
        _ => fallback_index,
    };
    if row.get("page").is_some_and(|value| !value.is_null()) {
        let given = row.get("page").expect("checked above");
        let one = Value::from(1);
        let effective = if python_truthy(given) { given } else { &one };
        python_int_opt(Some(effective)).ok_or_else(|| {
            SegmentError::Aborted(error("Media segment page must be an integer", 400))
        })?;
    }
    // Evaluated for its ability to raise, which is what drops the segment.
    if normalize_media_path(row.get("framePath")).is_err() {
        return Err(SegmentError::Skipped);
    }
    let mut segment = Map::new();
    segment.insert("segmentId".into(), json!(segment_id));
    segment.insert(
        "mediaId".into(),
        json!(validate_workspace_id(media_id, "media id").map_err(|_| SegmentError::Skipped)?),
    );
    segment.insert("type".into(), json!(segment_type));
    segment.insert("text".into(), json!(text));
    segment.insert("index".into(), json!(index));
    if let Some(citation) = row.get("citation").filter(|value| value.is_object()) {
        segment.insert("citation".into(), normalize_source_ref(citation));
    }
    Ok(Value::Object(segment))
}

/// `list_segments`.
fn list_segments(root: &Path, media_id: &str) -> Result<Vec<Value>> {
    let path = segments_path(root, media_id)?;
    let data = super::registry::read_json(&path);
    if !data.is_object() {
        return Ok(Vec::new());
    }
    let rows = data.get("segments").and_then(Value::as_array);
    let mut result = Vec::new();
    for (position, row) in rows.into_iter().flatten().enumerate() {
        if !row.is_object() {
            continue;
        }
        match context_segment(row, media_id, position as i64) {
            Ok(segment) => result.push(segment),
            Err(SegmentError::Skipped) => continue,
            Err(SegmentError::Aborted(failure)) => return Err(failure),
        }
    }
    Ok(result)
}

/// `re.findall(r"[a-z0-9_\u4e00-\u9fff]{2,}", text)`.
fn terms_regex() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(r"[a-z0-9_\u{4e00}-\u{9fff}]{2,}").expect("media context term pattern")
    })
}

/// `_media_context_terms`: the query's own words, lowercased, two characters or more.
fn query_terms(input: &Value) -> BTreeSet<String> {
    let mut values = Vec::new();
    for key in ["task", "query", "question", "prompt", "goal"] {
        if let Value::String(text) = input.get(key).unwrap_or(&Value::Null) {
            values.push(text.clone());
        }
    }
    let haystack = values.join(" ").to_lowercase();
    terms_regex()
        .find_iter(&haystack)
        .map(|found| found.as_str().to_string())
        .collect()
}

/// `_media_segment_rank`: the best-scoring segment first, then a citation, then order.
fn segment_rank(segment: &Value, terms: &BTreeSet<String>) -> (i64, i64, i64) {
    let citation = segment
        .get("citation")
        .filter(|value| value.as_object().is_some_and(|fields| !fields.is_empty()));
    let field = |key: &str| citation.and_then(|value| value.get(key)).map(value_str);
    let haystack = format!(
        "{} {} {} {}",
        text_or_empty(segment.get("text")),
        field("label").unwrap_or_default(),
        field("markdown").unwrap_or_default(),
        field("uri").unwrap_or_default(),
    )
    .to_lowercase();
    let score = terms
        .iter()
        .filter(|term| haystack.contains(term.as_str()))
        .count() as i64;
    let bonus = if citation.is_some() { 1 } else { 0 };
    let index = python_int_opt(segment.get("index")).unwrap_or_default();
    (-score, -bonus, index)
}

/// `_append_context_line` — false means the budget is spent.
fn push_line(lines: &mut Vec<String>, line: &str, used: usize) -> bool {
    if used + line.chars().count() + 1 > MAX_CONTEXT_CHARS {
        return false;
    }
    lines.push(line.to_string());
    true
}

/// The `mediaIds` / `mediaId` pair, stripped, emptied, capped at twelve.
fn requested_ids(input: &Value) -> Vec<String> {
    let single = |value: Option<&Value>| match value {
        Some(found) if python_truthy(found) => vec![found.clone()],
        _ => Vec::new(),
    };
    let raw = match input.get("mediaIds") {
        Some(Value::Array(items)) => items.clone(),
        _ => single(input.get("mediaId")),
    };
    raw.iter()
        .map(|item| py_strip(&text_or_empty(Some(item))).to_string())
        .filter(|media_id| !media_id.is_empty())
        .take(MAX_MEDIA)
        .collect()
}

/// `_media_context`.
pub fn context(
    registry: &super::registry::Registry,
    input: &Value,
    project_id: &str,
) -> Result<String> {
    let media_ids = requested_ids(input);
    if media_ids.is_empty() {
        return Ok(String::new());
    }
    let terms = query_terms(input);
    let mut lines = vec!["[Media context]".to_string()];
    let mut used = lines[0].chars().count();
    for (offset, media_id) in media_ids.iter().enumerate() {
        let position = offset + 1;
        let Some(record) = find_media(&registry.root, media_id) else {
            let line = format!("- M{position}: warning: mediaId={media_id} was not found");
            if !push_line(&mut lines, &line, used) {
                break;
            }
            used += line.chars().count() + 1;
            continue;
        };
        let owner = text_or_empty(record.get("projectId"));
        if !project_id.is_empty() && !owner.is_empty() && owner != project_id {
            let line = format!(
                "- M{position}: warning: mediaId={media_id} belongs to a different project"
            );
            if !push_line(&mut lines, &line, used) {
                break;
            }
            used += line.chars().count() + 1;
            continue;
        }
        let header = format!(
            "- M{position}: {} ({}, mediaId={}, status={})",
            text_or_empty(record.get("title")),
            text_or_empty(record.get("type")),
            text_or_empty(record.get("mediaId")),
            text_or_empty(record.get("status")),
        );
        if !push_line(&mut lines, &header, used) {
            break;
        }
        used += header.chars().count() + 1;
        let mut ranked = list_segments(&registry.root, media_id)?;
        ranked.sort_by_key(|segment| segment_rank(segment, &terms));
        for segment in ranked.iter().take(MAX_SEGMENTS_PER_MEDIA) {
            let raw = py_strip(&text_or_empty(segment.get("text"))).to_string();
            let text = if raw.chars().count() > MAX_SEGMENT_CHARS {
                let head: String = raw.chars().take(MAX_SEGMENT_CHARS).collect();
                format!("{}\n[truncated]", head.trim_end())
            } else {
                raw
            };
            let locator = {
                let citation = segment
                    .get("citation")
                    .filter(|value| value.as_object().is_some_and(|fields| !fields.is_empty()));
                let field = |key: &str| {
                    citation
                        .and_then(|value| value.get(key))
                        .filter(|value| python_truthy(value))
                        .map(value_str)
                };
                field("markdown")
                    .or_else(|| field("uri"))
                    .or_else(|| {
                        segment
                            .get("segmentId")
                            .filter(|value| python_truthy(value))
                            .map(value_str)
                    })
                    .unwrap_or_default()
            };
            let block = format!(
                "  segment {} {}:\n{text}",
                text_or_empty(segment.get("type")),
                locator,
            );
            if used + block.chars().count() + 1 > MAX_CONTEXT_CHARS {
                let _ = push_line(&mut lines, "  [media context truncated]", used);
                return Ok(lines.join("\n"));
            }
            used += block.chars().count() + 1;
            lines.push(block);
        }
    }
    Ok(lines.join("\n"))
}
