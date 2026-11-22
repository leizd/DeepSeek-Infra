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
    if parts.contains(&"..") {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::skills::registry::Registry;
    use std::sync::atomic::{AtomicI64, Ordering};

    static COUNTER: AtomicI64 = AtomicI64::new(0);

    fn temp_root(label: &str) -> PathBuf {
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "skills-media-test-{label}-{}-{unique}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp root");
        root
    }

    fn write_json(path: &Path, value: &Value) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, serde_json::to_string(value).expect("encode json"))
            .expect("write json");
    }

    fn write_store(root: &Path, rows: Value) {
        write_json(&store_path(root), &json!({ "media": rows }));
    }

    fn write_segments(root: &Path, media_id: &str, rows: Value) {
        let path = segments_path(root, media_id).expect("segments path");
        write_json(&path, &json!({ "segments": rows }));
    }

    fn record(media_id: &str) -> Value {
        json!({
            "mediaId": media_id,
            "projectId": "proj-1",
            "type": "pdf",
            "title": "Doc",
            "status": "ready",
            "path": "a/b.pdf",
        })
    }

    fn segment(segment_id: &str, text: &str, index: i64) -> Value {
        json!({
            "segmentId": segment_id,
            "type": "page_text",
            "text": text,
            "index": index,
        })
    }

    /// `SegmentError` carries no `Debug`, so the three outcomes are matched by hand
    /// rather than through `unwrap`.
    fn segment_ok(row: &Value, media_id: &str, index: i64) -> Value {
        match context_segment(row, media_id, index) {
            Ok(value) => value,
            Err(SegmentError::Skipped) => panic!("segment was skipped: {row}"),
            Err(SegmentError::Aborted(failure)) => panic!("segment aborted: {}", failure.message),
        }
    }

    fn segment_skipped(row: &Value, media_id: &str, index: i64) -> bool {
        matches!(
            context_segment(row, media_id, index),
            Err(SegmentError::Skipped)
        )
    }

    fn segment_abort(
        row: &Value,
        media_id: &str,
        index: i64,
    ) -> Option<crate::app_error::AppError> {
        match context_segment(row, media_id, index) {
            Err(SegmentError::Aborted(failure)) => Some(failure),
            _ => None,
        }
    }

    // --- py_strip, posix_suffix, normalize_mime_type ---------------------------

    #[test]
    fn strip_also_removes_the_four_separators_unicode_whitespace_excludes() {
        assert_eq!(py_strip("  x  "), "x");
        assert_eq!(py_strip("\u{1c}x\u{1f}"), "x");
        assert_eq!(py_strip("\u{1d}\u{1e}"), "");
        assert_eq!(py_strip("a\u{1c}b"), "a\u{1c}b");
    }

    #[test]
    fn a_suffix_needs_a_dot_that_is_neither_leading_nor_trailing() {
        assert_eq!(posix_suffix("a.pdf"), ".pdf");
        assert_eq!(posix_suffix("a.b.pdf"), ".pdf");
        assert_eq!(posix_suffix("dir/a.pdf"), ".pdf");
        assert_eq!(posix_suffix(".pdf"), "");
        assert_eq!(posix_suffix("x."), "");
        assert_eq!(posix_suffix("noext"), "");
        assert_eq!(posix_suffix(""), "");
    }

    #[test]
    fn a_mime_type_loses_its_parameters_and_its_case() {
        assert_eq!(
            normalize_mime_type(Some(&json!("TEXT/HTML; charset=utf-8"))),
            "text/html"
        );
        assert_eq!(
            normalize_mime_type(Some(&json!("  Application/PDF  "))),
            "application/pdf"
        );
        assert_eq!(normalize_mime_type(None), "");
        assert_eq!(normalize_mime_type(Some(&json!(7))), "7");
    }

    // --- media_type_from_mime, normalize_media_type ----------------------------

    #[test]
    fn every_media_type_is_reachable_from_mime_or_suffix() {
        assert_eq!(media_type_from_mime("image/png", ""), "image");
        assert_eq!(
            media_type_from_mime("IMAGE/PNG; charset=utf-8", ""),
            "image"
        );
        assert_eq!(media_type_from_mime("application/pdf", ""), "pdf");
        assert_eq!(media_type_from_mime("", "a.pdf"), "pdf");
        assert_eq!(media_type_from_mime("audio/mpeg", ""), "audio");
        assert_eq!(media_type_from_mime("video/mp4", ""), "video");
        assert_eq!(media_type_from_mime("text/html", ""), "webpage");
        assert_eq!(media_type_from_mime("application/xhtml+xml", ""), "webpage");
        assert_eq!(media_type_from_mime("application/zip", "a.bin"), "");
    }

    #[test]
    fn each_audio_and_video_and_webpage_suffix_is_recognised() {
        for suffix in [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"] {
            assert_eq!(media_type_from_mime("", &format!("a{suffix}")), "audio");
        }
        for suffix in [".mp4", ".mov", ".webm", ".mkv", ".avi"] {
            assert_eq!(media_type_from_mime("", &format!("a{suffix}")), "video");
        }
        for suffix in [".html", ".htm"] {
            assert_eq!(media_type_from_mime("", &format!("a{suffix}")), "webpage");
        }
        assert_eq!(media_type_from_mime("", "A.PDF"), "pdf");
    }

    #[test]
    fn a_declared_type_wins_and_an_uninferable_one_raises() {
        assert_eq!(
            normalize_media_type(Some(&json!(" AUDIO ")), "", "").expect("declared"),
            "audio"
        );
        assert_eq!(
            normalize_media_type(Some(&json!("bogus")), "image/png", "").expect("guessed"),
            "image"
        );
        let failure = normalize_media_type(Some(&json!("bogus")), "", "a.bin").unwrap_err();
        assert_eq!(failure.status, 400);
        assert_eq!(failure.message, "Unsupported media type");
    }

    // --- normalize_status, normalize_media_path --------------------------------

    #[test]
    fn a_status_falls_back_to_the_default_only_when_empty() {
        assert_eq!(
            normalize_status(None, "pending").expect("default"),
            "pending"
        );
        assert_eq!(
            normalize_status(Some(&json!("")), "ready").expect("empty"),
            "ready"
        );
        assert_eq!(
            normalize_status(Some(&json!("READY")), "pending").expect("case"),
            "ready"
        );
        let failure = normalize_status(Some(&json!("nope")), "pending").unwrap_err();
        assert_eq!(failure.status, 400);
        assert_eq!(failure.message, "Unsupported media status");
    }

    #[test]
    fn a_media_path_is_relative_collapsed_and_backslash_normalised() {
        assert_eq!(normalize_media_path(None).expect("absent"), "");
        assert_eq!(normalize_media_path(Some(&json!("  "))).expect("blank"), "");
        assert_eq!(
            normalize_media_path(Some(&json!("a\\b.pdf"))).expect("slash"),
            "a/b.pdf"
        );
        assert_eq!(
            normalize_media_path(Some(&json!("./a//b/"))).expect("collapse"),
            "a/b"
        );
        assert_eq!(
            normalize_media_path(Some(&json!("  a/b  "))).expect("strip"),
            "a/b"
        );
    }

    #[test]
    fn an_absolute_driven_or_escaping_media_path_raises() {
        for value in ["/abs/a.pdf", "C:/a.pdf", "a/../b.pdf", "..", "a/.."] {
            let failure = normalize_media_path(Some(&json!(value))).unwrap_err();
            assert_eq!(failure.status, 400, "{value}");
        }
        assert_eq!(
            normalize_media_path(Some(&json!("/abs")))
                .unwrap_err()
                .message,
            "Media path must be relative to the media library"
        );
        assert_eq!(
            normalize_media_path(Some(&json!("a/../b")))
                .unwrap_err()
                .message,
            "Media path must not escape the media library"
        );
    }

    // --- store layout ----------------------------------------------------------

    #[test]
    fn the_store_lives_under_dot_media_and_segments_are_named_by_id() {
        let root = Path::new("root");
        assert_eq!(media_dir(root), Path::new("root/.media"));
        assert_eq!(store_path(root), Path::new("root/.media/library.json"));
        assert_eq!(
            segments_path(root, "media1").expect("path"),
            Path::new("root/.media/segments/media1.json")
        );
        assert!(segments_path(root, "x").is_err());
    }

    // --- context_record, load_store, find_media --------------------------------

    #[test]
    fn a_record_keeps_exactly_the_five_fields_the_consumer_reads() {
        let row = context_record(&record("media1")).expect("record");
        assert_eq!(
            row,
            json!({
                "mediaId": "media1",
                "projectId": "proj-1",
                "type": "pdf",
                "title": "Doc",
                "status": "ready",
            })
        );
    }

    #[test]
    fn an_empty_project_id_is_kept_but_an_invalid_one_drops_the_row() {
        let mut row = record("media1");
        row["projectId"] = json!("  ");
        assert_eq!(context_record(&row).expect("empty")["projectId"], "");
        row["projectId"] = json!("p1");
        assert!(context_record(&row).is_err());
    }

    #[test]
    fn a_record_raises_on_a_bad_id_type_path_or_status() {
        let mut row = record("media1");
        row["mediaId"] = json!("x");
        assert!(context_record(&row).is_err());
        let mut row = record("media1");
        row["type"] = json!("bogus");
        assert!(context_record(&row).is_err());
        let mut row = record("media1");
        row["path"] = json!("/abs");
        assert!(context_record(&row).is_err());
        let mut row = record("media1");
        row["status"] = json!("nope");
        assert!(context_record(&row).is_err());
    }

    #[test]
    fn a_title_falls_back_and_a_type_is_inferred_from_the_title_suffix() {
        let mut row = record("media1");
        row["title"] = json!("   ");
        row["type"] = json!("");
        row["mimeType"] = json!("image/png");
        let kept = context_record(&row).expect("record");
        assert_eq!(kept["title"], "Untitled media");
        assert_eq!(kept["type"], "image");
    }

    #[test]
    fn the_store_drops_rows_that_raise_and_rows_that_are_not_objects() {
        let root = temp_root("store");
        write_store(
            &root,
            json!([record("media1"), 7, "text", record("x"), record("media2")]),
        );
        let rows = load_store(&root);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["mediaId"], "media1");
        assert_eq!(rows[1]["mediaId"], "media2");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_missing_or_non_object_store_is_empty() {
        let root = temp_root("empty-store");
        assert!(load_store(&root).is_empty());
        write_json(&store_path(&root), &json!([]));
        assert!(load_store(&root).is_empty());
        write_json(&store_path(&root), &json!({ "media": "not-a-list" }));
        assert!(load_store(&root).is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn find_media_returns_the_first_normalised_row_with_that_id() {
        let root = temp_root("find");
        write_store(&root, json!([record("media1"), record("media2")]));
        assert_eq!(
            find_media(&root, "media2").expect("found")["mediaId"],
            "media2"
        );
        assert!(find_media(&root, "media9").is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    // --- context_segment -------------------------------------------------------

    #[test]
    fn a_segment_defaults_its_type_and_generates_a_missing_id() {
        let row = json!({ "text": "alpha" });
        let kept = segment_ok(&row, "media1", 4);
        assert_eq!(kept["type"], "page_text");
        assert_eq!(kept["index"], 4);
        assert_eq!(kept["mediaId"], "media1");
        assert_eq!(kept["text"], "alpha");
        let generated = kept["segmentId"].as_str().expect("id");
        assert!(generated.starts_with("seg_"), "{generated}");
        assert!(kept.get("citation").is_none());
    }

    #[test]
    fn an_unknown_type_bad_id_or_escaping_frame_drops_only_the_segment() {
        assert!(segment_skipped(&json!({ "type": "bogus" }), "media1", 0));
        assert!(segment_skipped(&json!({ "segmentId": "x" }), "media1", 0));
        assert!(segment_skipped(
            &json!({ "framePath": "/abs" }),
            "media1",
            0
        ));
        assert!(segment_skipped(&segment("seg-0001", "alpha", 0), "x", 0));
    }

    #[test]
    fn an_index_is_coerced_the_way_int_does_and_a_bad_one_aborts() {
        assert_eq!(
            segment_ok(&json!({ "index": "3" }), "media1", 9)["index"],
            3
        );
        assert_eq!(
            segment_ok(&json!({ "index": 2.7 }), "media1", 9)["index"],
            2
        );
        assert_eq!(
            segment_ok(&json!({ "index": true }), "media1", 9)["index"],
            1
        );
        assert_eq!(
            segment_ok(&json!({ "index": null }), "media1", 9)["index"],
            9
        );
        let failure = segment_abort(&json!({ "index": "abc" }), "media1", 0).expect("abort");
        assert_eq!(failure.status, 400);
        assert_eq!(failure.message, "Media segment index must be an integer");
    }

    #[test]
    fn a_falsy_page_becomes_one_and_a_non_numeric_page_aborts() {
        assert_eq!(segment_ok(&json!({ "page": 0 }), "media1", 0)["index"], 0);
        assert_eq!(
            segment_ok(&json!({ "page": null }), "media1", 0)["index"],
            0
        );
        assert_eq!(segment_ok(&json!({ "page": 3 }), "media1", 0)["index"], 0);
        let failure = segment_abort(&json!({ "page": "abc" }), "media1", 0).expect("abort");
        assert_eq!(failure.status, 400);
        assert_eq!(failure.message, "Media segment page must be an integer");
    }

    #[test]
    fn a_segment_text_is_redacted_and_an_object_citation_is_normalised() {
        let row = json!({
            "segmentId": "seg-0001",
            "text": "password=abc123",
            "citation": { "label": "L", "markdown": "[^M1]" },
        });
        let kept = segment_ok(&row, "media1", 0);
        assert!(kept["text"].as_str().expect("text").contains("[redacted]"));
        assert_eq!(kept["citation"]["markdown"], "[^M1]");
        let plain = segment_ok(&json!({ "citation": "not-an-object" }), "media1", 0);
        assert!(plain.get("citation").is_none());
    }

    // --- list_segments ---------------------------------------------------------

    #[test]
    fn segments_are_listed_in_order_and_non_object_rows_are_ignored() {
        let root = temp_root("segments");
        write_segments(
            &root,
            "media1",
            json!([
                segment("seg-0001", "alpha", 0),
                5,
                segment("seg-0002", "beta", 1)
            ]),
        );
        let rows = list_segments(&root, "media1").expect("list");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["text"], "alpha");
        assert_eq!(rows[1]["text"], "beta");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_missing_or_non_object_segment_file_is_an_empty_list() {
        let root = temp_root("no-segments");
        assert!(list_segments(&root, "media1").expect("missing").is_empty());
        let path = segments_path(&root, "media1").expect("path");
        write_json(&path, &json!("not-an-object"));
        assert!(list_segments(&root, "media1").expect("scalar").is_empty());
        assert!(list_segments(&root, "x").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_skipped_segment_is_passed_over_but_an_aborted_one_fails_the_list() {
        let root = temp_root("segment-errors");
        write_segments(
            &root,
            "media1",
            json!([json!({ "type": "bogus" }), segment("seg-0001", "alpha", 1)]),
        );
        assert_eq!(list_segments(&root, "media1").expect("skip").len(), 1);
        write_segments(
            &root,
            "media1",
            json!([segment("seg-0001", "alpha", 0), json!({ "index": "abc" })]),
        );
        let failure = list_segments(&root, "media1").unwrap_err();
        assert_eq!(failure.message, "Media segment index must be an integer");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_segment_without_an_index_takes_its_position_in_the_file() {
        let root = temp_root("fallback-index");
        write_segments(
            &root,
            "media1",
            json!([json!({ "text": "alpha" }), json!({ "text": "beta" })]),
        );
        let rows = list_segments(&root, "media1").expect("list");
        assert_eq!(rows[0]["index"], 0);
        assert_eq!(rows[1]["index"], 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    // --- query_terms, segment_rank, push_line, requested_ids -------------------

    #[test]
    fn query_terms_come_from_five_keys_lowercased_and_at_least_two_chars() {
        let terms = query_terms(&json!({
            "task": "Alpha beta",
            "query": "gamma",
            "question": "a",
            "prompt": "中文",
            "goal": "delta_1",
        }));
        assert_eq!(
            terms.into_iter().collect::<Vec<_>>(),
            ["alpha", "beta", "delta_1", "gamma", "中文"]
        );
        assert!(query_terms(&json!({ "task": 7 })).is_empty());
        assert!(query_terms(&json!({})).is_empty());
    }

    #[test]
    fn a_rank_prefers_more_matched_terms_then_a_citation_then_a_lower_index() {
        let terms: BTreeSet<String> = ["alpha".to_string(), "beta".to_string()]
            .into_iter()
            .collect();
        let both = segment_ok(&segment("seg-0001", "alpha beta", 5), "media1", 5);
        let one = segment_ok(&segment("seg-0002", "alpha", 0), "media1", 0);
        let none = segment_ok(&segment("seg-0003", "nothing", 0), "media1", 0);
        assert!(segment_rank(&both, &terms) < segment_rank(&one, &terms));
        assert!(segment_rank(&one, &terms) < segment_rank(&none, &terms));

        let cited = segment_ok(
            &json!({ "segmentId": "seg-0004", "text": "nothing", "index": 9,
                     "citation": { "label": "L" } }),
            "media1",
            9,
        );
        assert!(segment_rank(&cited, &terms) < segment_rank(&none, &terms));
        let later = segment_ok(&segment("seg-0005", "nothing", 7), "media1", 7);
        assert!(segment_rank(&none, &terms) < segment_rank(&later, &terms));
    }

    #[test]
    fn a_citation_label_markdown_and_uri_all_feed_the_rank_haystack() {
        let terms: BTreeSet<String> = ["alpha".to_string()].into_iter().collect();
        let row = json!({
            "segmentId": "seg-0001", "text": "nothing", "index": 0,
            "citation": { "label": "alpha", "markdown": "x", "uri": "y" },
        });
        let cited = segment_ok(&row, "media1", 0);
        assert_eq!(segment_rank(&cited, &terms).0, -1);
    }

    #[test]
    fn a_line_fits_only_when_it_and_its_newline_stay_inside_the_budget() {
        let mut lines = Vec::new();
        assert!(push_line(&mut lines, "abcd", MAX_CONTEXT_CHARS - 5));
        assert_eq!(lines.len(), 1);
        assert!(!push_line(&mut lines, "abcde", MAX_CONTEXT_CHARS - 5));
        assert_eq!(lines.len(), 1);
        assert!(!push_line(&mut lines, "x", MAX_CONTEXT_CHARS));
    }

    #[test]
    fn requested_ids_prefer_the_list_strip_blanks_and_cap_at_twelve() {
        assert_eq!(
            requested_ids(&json!({ "mediaIds": [" a ", "b"] })),
            ["a", "b"]
        );
        assert_eq!(requested_ids(&json!({ "mediaId": " m1 " })), ["m1"]);
        assert!(requested_ids(&json!({ "mediaIds": [], "mediaId": "m1" })).is_empty());
        assert!(requested_ids(&json!({ "mediaId": "" })).is_empty());
        assert!(requested_ids(&json!({ "mediaId": null })).is_empty());
        assert!(requested_ids(&json!({ "mediaId": 0 })).is_empty());
        assert!(requested_ids(&json!({})).is_empty());
        let many: Vec<Value> = (0..15).map(|index| json!(format!("m{index}"))).collect();
        assert_eq!(requested_ids(&json!({ "mediaIds": many })).len(), MAX_MEDIA);
    }

    // --- context ---------------------------------------------------------------

    #[test]
    fn no_requested_media_produces_no_context_at_all() {
        let root = temp_root("no-ids");
        let registry = Registry::new(&root);
        assert_eq!(context(&registry, &json!({}), "").expect("empty"), "");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_found_record_renders_its_header_and_its_segments() {
        let root = temp_root("render");
        write_store(&root, json!([record("media1")]));
        write_segments(&root, "media1", json!([segment("seg-0001", "alpha", 0)]));
        let registry = Registry::new(&root);
        let text = context(&registry, &json!({ "mediaId": "media1" }), "").expect("context");
        assert_eq!(
            text,
            "[Media context]\n- M1: Doc (pdf, mediaId=media1, status=ready)\n  segment page_text seg-0001:\nalpha"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_missing_record_and_a_foreign_project_each_warn_in_place() {
        let root = temp_root("warnings");
        write_store(&root, json!([record("media1")]));
        let registry = Registry::new(&root);
        let text = context(
            &registry,
            &json!({ "mediaIds": ["media9", "media1"] }),
            "proj-2",
        )
        .expect("context");
        assert!(text.contains("- M1: warning: mediaId=media9 was not found"));
        assert!(text.contains("- M2: warning: mediaId=media1 belongs to a different project"));
        let same = context(&registry, &json!({ "mediaId": "media1" }), "proj-1").expect("same");
        assert!(same.contains("- M1: Doc (pdf, mediaId=media1, status=ready)"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn segments_are_emitted_best_rank_first_and_at_most_twelve() {
        let root = temp_root("ranking");
        write_store(&root, json!([record("media1")]));
        let mut rows: Vec<Value> = (0..14)
            .map(|position| segment(&format!("seg-{position:04}"), "nothing", position))
            .collect();
        rows.push(segment("seg-best", "alpha beta", 99));
        write_segments(&root, "media1", json!(rows));
        let registry = Registry::new(&root);
        let text = context(
            &registry,
            &json!({ "mediaId": "media1", "task": "alpha beta" }),
            "",
        )
        .expect("context");
        let best = text.find("seg-best").expect("best segment present");
        let first_other = text.find("seg-0000").expect("other segment present");
        assert!(
            best < first_other,
            "the best-scoring segment must come first"
        );
        assert_eq!(text.matches("  segment ").count(), MAX_SEGMENTS_PER_MEDIA);
        assert!(!text.contains("seg-0012"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_locator_prefers_citation_markdown_then_uri_then_the_segment_id() {
        let root = temp_root("locator");
        write_store(&root, json!([record("media1")]));
        write_segments(
            &root,
            "media1",
            json!([
                json!({ "segmentId": "seg-0001", "text": "a", "index": 0,
                        "citation": { "markdown": "[^M1]", "uri": "media://1" } }),
                json!({ "segmentId": "seg-0002", "text": "b", "index": 1,
                        "citation": { "uri": "media://2" } }),
                segment("seg-0003", "c", 2),
            ]),
        );
        let registry = Registry::new(&root);
        let text = context(&registry, &json!({ "mediaId": "media1" }), "").expect("context");
        assert!(text.contains("  segment page_text [^M1]:\na"));
        assert!(text.contains("  segment page_text media://2:\nb"));
        assert!(text.contains("  segment page_text seg-0003:\nc"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn an_overlong_segment_text_is_cut_and_marked_truncated() {
        let root = temp_root("segment-truncate");
        write_store(&root, json!([record("media1")]));
        let long = "x".repeat(MAX_SEGMENT_CHARS + 400);
        write_segments(&root, "media1", json!([segment("seg-0001", &long, 0)]));
        let registry = Registry::new(&root);
        let text = context(&registry, &json!({ "mediaId": "media1" }), "").expect("context");
        assert!(text.ends_with("[truncated]"));
        let body = text.split(":\n").nth(1).expect("segment body");
        assert_eq!(
            body.chars().count(),
            MAX_SEGMENT_CHARS + 1 + "[truncated]".chars().count()
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_context_budget_stops_the_tail_and_says_so() {
        let root = temp_root("budget");
        let long = "x".repeat(MAX_SEGMENT_CHARS);
        let rows: Vec<Value> = (0..MAX_SEGMENTS_PER_MEDIA)
            .map(|position| segment(&format!("seg-{position:04}"), &long, position as i64))
            .collect();
        write_store(&root, json!([record("media1"), record("media2")]));
        write_segments(&root, "media1", json!(rows));
        write_segments(&root, "media2", json!(rows));
        let registry = Registry::new(&root);
        let text =
            context(&registry, &json!({ "mediaIds": ["media1", "media2"] }), "").expect("context");
        assert!(text.contains("[media context truncated]"));
        assert!(text.chars().count() <= MAX_CONTEXT_CHARS);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn an_aborted_segment_index_fails_the_whole_context_call() {
        let root = temp_root("context-abort");
        write_store(&root, json!([record("media1")]));
        write_segments(&root, "media1", json!([json!({ "index": "abc" })]));
        let registry = Registry::new(&root);
        let failure = context(&registry, &json!({ "mediaId": "media1" }), "").unwrap_err();
        assert_eq!(failure.status, 400);
        assert_eq!(failure.message, "Media segment index must be an integer");
        let _ = std::fs::remove_dir_all(&root);
    }
}
