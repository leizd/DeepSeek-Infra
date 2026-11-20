//! Builders and persistence for `POST /api/workspace/exports`.
//!
//! Mirrors `deepseek_infra/infra/workspace/exports.py` for all five export
//! kinds (`project`, `conversation`, `saved-items`/`saved_items`/`saved`,
//! `artifacts`/`artifact_package`, `evidence`/`trace_eval`/`trace-eval`) and
//! all four formats (`json`, `markdown`/`md`, `html`, `zip`).

use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use tempfile::NamedTempFile;
use zip::write::FileOptions;
use zip::{CompressionMethod, ZipWriter};

use crate::app_error::{AppError, codes};
use crate::core_utils::{python_int_opt, python_truthy};
use crate::entropy::Entropy;
use crate::file_cache::{FileCache, load_cached_file};
use crate::generated_files::generated_dir;
use crate::media_processing::normalize_segment;
use crate::media_schema::{normalize_media_path, normalize_media_record};
use crate::projects;
use crate::python_json::OrderedJson;
use crate::workspace_projects;
use crate::workspace_schema::{
    contains_secret, new_id, normalize_export_format, normalize_source_ref, normalize_title,
    now_ms, python_str, read_json_file, redact_sensitive_text, redact_value, resolve_runtime_path,
    safe_filename, timestamp_ms_to_iso, validate_project_id, validate_workspace_id,
    write_json_atomic,
};

pub const EXPORT_SCHEMA_VERSION: &str = "workspace-export.v1";
const STORE_NAME: &str = "exports.json";
const TEXT_PREVIEW_TYPES: [&str; 7] = ["svg", "markdown", "md", "csv", "json", "html", "txt"];
const MEDIA_TEXT_SUFFIXES: [&str; 7] = [".txt", ".md", ".html", ".htm", ".json", ".csv", ".xml"];

struct ExportSpec<'a> {
    project_id: &'a str,
    kind: &'a str,
    format: &'a str,
    filename: &'a str,
}

fn internal_error(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::INTERNAL,
        status: 500,
    }
}

fn truthy_opt(value: Option<&Value>) -> Option<&Value> {
    value.filter(|item| python_truthy(item))
}

fn truthy_str(value: Option<&Value>, fallback: &str) -> String {
    match truthy_opt(value) {
        Some(item) => python_str(Some(item)),
        None => fallback.to_string(),
    }
}

fn utc_now(entropy: &dyn Entropy) -> String {
    timestamp_ms_to_iso(Some(&json!(now_ms(entropy))))
}

fn pretty_redacted(value: &Value) -> Result<String, AppError> {
    serde_json::to_string_pretty(&redact_value(value, ""))
        .map_err(|_| internal_error("Failed to serialize export JSON"))
}

fn sorted_source_json(value: &Value) -> String {
    redact_sensitive_text(
        &OrderedJson::from_value_with_order(value, &[]).render_default_separators(),
    )
}

fn python_title_case(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut prev_cased = false;
    for ch in input.chars() {
        if ch.is_alphabetic() {
            if prev_cased {
                for lc in ch.to_lowercase() {
                    out.push(lc);
                }
            } else {
                for uc in ch.to_uppercase() {
                    out.push(uc);
                }
            }
            prev_cased = true;
        } else {
            out.push(ch);
            prev_cased = false;
        }
    }
    out
}

fn html_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(ch),
        }
    }
    out
}

pub fn markdown_to_html(markdown: &str) -> String {
    let escaped = html_escape(&redact_sensitive_text(markdown));
    format!(
        "<!doctype html><meta charset=\"utf-8\"><title>Workspace Export</title><pre>{escaped}</pre>\n"
    )
}

/// Mirrors `Path(raw).stem` and `Path(raw).suffix.lower()` on runtime relative/POSIX paths.
fn path_stem_and_lower_suffix(raw: &str) -> (String, String) {
    let last = raw.rsplit(['/', '\\']).next().unwrap_or("");
    if let Some(dot) = last.rfind('.') {
        if dot > 0 && dot + 1 < last.len() {
            return (last[..dot].to_string(), last[dot..].to_ascii_lowercase());
        }
    }
    (last.to_string(), String::new())
}

pub fn artifact_filename(artifact: &Value) -> String {
    let raw_path = truthy_opt(artifact.get("path"))
        .map(|v| python_str(Some(v)))
        .unwrap_or_default();
    let (stem, suffix) = path_stem_and_lower_suffix(&raw_path);
    let ext = if suffix.is_empty() {
        format!(".{}", truthy_str(artifact.get("type"), "artifact"))
    } else {
        suffix
    };
    let title = truthy_opt(artifact.get("title"))
        .map(|v| python_str(Some(v)))
        .filter(|s| !s.is_empty())
        .or(if stem.is_empty() { None } else { Some(stem) })
        .unwrap_or_else(|| "artifact".to_string());
    format!("{}{}", safe_filename(&title, "file"), ext)
}

fn artifact_path(root: &Path, artifact: &Value) -> Result<PathBuf, AppError> {
    let raw = python_str(truthy_opt(artifact.get("path")));
    resolve_runtime_path(
        &raw,
        &generated_dir(root),
        &projects::projects_dir(root),
        root,
    )
}

fn media_dir(root: &Path) -> PathBuf {
    root.join(".media")
}

fn media_file_path(root: &Path, media: &Value) -> Result<PathBuf, AppError> {
    let raw = normalize_media_path(media.get("path"))?;
    let base = media_dir(root);
    if raw.is_empty() {
        let mid = validate_workspace_id(&python_str(media.get("mediaId")), "media id")?;
        return Ok(base.join("objects").join(mid));
    }
    let resolved = resolve_runtime_path(&raw, &generated_dir(root), &base, root)?;
    let canonical_base = fs::canonicalize(&base).unwrap_or_else(|_| base.clone());
    let canonical_target = fs::canonicalize(&resolved).unwrap_or(resolved);
    if !canonical_target.starts_with(&canonical_base) {
        return Err(AppError::invalid_payload(
            "Media file must stay inside the media library",
        ));
    }
    Ok(canonical_target)
}

fn list_media_for_project(root: &Path, safe_project_id: &str) -> Result<Vec<Value>, AppError> {
    let library_path = media_dir(root).join("library.json");
    let data = read_json_file(&library_path, json!({ "media": [] }));
    let Some(rows) = data.get("media").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut items = Vec::new();
    for raw in rows {
        if !raw.is_object() {
            continue;
        }
        let Ok(normalized) = normalize_media_record(raw) else {
            continue;
        };
        if normalized.get("projectId").and_then(Value::as_str) == Some(safe_project_id) {
            items.push(normalized);
        }
    }
    items.sort_by(|left, right| {
        let lt = truthy_str(left.get("updatedAt"), "");
        let rt = truthy_str(right.get("updatedAt"), "");
        rt.cmp(&lt)
    });
    Ok(items)
}

fn list_segments_for_media(root: &Path, media_id: &str, entropy: &dyn Entropy) -> Vec<Value> {
    let Ok(safe_id) = validate_workspace_id(media_id, "media id") else {
        return Vec::new();
    };
    let path = media_dir(root)
        .join("segments")
        .join(format!("{safe_id}.json"));
    if !path.is_file() {
        return Vec::new();
    }
    let data = read_json_file(&path, json!({ "segments": [] }));
    let Some(rows) = data.get("segments").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for (idx, item) in rows.iter().enumerate() {
        if !item.is_object() {
            continue;
        }
        if let Ok(seg) = normalize_segment(item, &safe_id, idx, entropy) {
            out.push(seg);
        }
    }
    out
}

pub fn project_bundle(
    root: &Path,
    project_id: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let project = workspace_projects::get_project(&safe_project_id, root, entropy)?;
    let saved_items = workspace_projects::list_saved_items(&safe_project_id, root, "", &[])?;
    let artifacts = workspace_projects::list_artifacts(&safe_project_id, root)?;
    let media_items = list_media_for_project(root, &safe_project_id)?;
    let conversations = project
        .get("conversations")
        .cloned()
        .unwrap_or_else(|| json!([]));
    Ok(json!({
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "project",
        "metadata": project,
        "conversations": conversations,
        "savedItems": saved_items,
        "artifacts": artifacts,
        "media": media_items,
        "exportedAt": utc_now(entropy),
    }))
}

pub fn conversation_markdown(conversation: &Value) -> String {
    let title = redact_sensitive_text(&truthy_str(conversation.get("title"), "Conversation"));
    let conv_id = truthy_opt(conversation.get("conversationId"))
        .or_else(|| truthy_opt(conversation.get("id")))
        .map(|v| python_str(Some(v)))
        .unwrap_or_default();
    let mut lines = vec![
        format!("# {title}"),
        String::new(),
        format!("- Conversation ID: `{conv_id}`"),
        String::new(),
    ];
    if let Some(source_ref) = conversation.get("sourceRef").and_then(Value::as_object) {
        if !source_ref.is_empty() {
            if let Ok(rendered) = pretty_redacted(&Value::Object(source_ref.clone())) {
                lines.extend([
                    "## Source".to_string(),
                    String::new(),
                    "```json".to_string(),
                    rendered,
                    "```".to_string(),
                    String::new(),
                ]);
            }
        }
    }
    if let Some(messages) = conversation.get("messages").and_then(Value::as_array) {
        for message in messages {
            if !message.is_object() {
                continue;
            }
            let raw_role = truthy_str(message.get("role"), "message");
            let role = python_title_case(raw_role.trim());
            let content = redact_sensitive_text(&truthy_str(message.get("content"), ""));
            lines.extend([
                format!("## {role}"),
                String::new(),
                if content.is_empty() {
                    "_No content_".to_string()
                } else {
                    content
                },
                String::new(),
            ]);
            if let Some(msg_source) = message.get("sourceRef").and_then(Value::as_object) {
                if !msg_source.is_empty() {
                    if let Ok(rendered) = pretty_redacted(&Value::Object(msg_source.clone())) {
                        lines.extend([
                            "```json".to_string(),
                            rendered,
                            "```".to_string(),
                            String::new(),
                        ]);
                    }
                }
            }
        }
    }
    format!("{}\n", lines.join("\n").trim())
}

pub fn saved_items_markdown(items: &[Value]) -> String {
    let mut lines = Vec::new();
    for item in items {
        let title = redact_sensitive_text(&truthy_str(item.get("title"), "Saved item"));
        let item_type = truthy_str(item.get("type"), "");
        let saved_id = truthy_str(item.get("savedId"), "");
        lines.extend([
            format!("### {title}"),
            String::new(),
            format!("- Type: `{item_type}`"),
            format!("- Saved ID: `{saved_id}`"),
        ]);
        if let Some(tags) = item.get("tags").and_then(Value::as_array) {
            if !tags.is_empty() {
                let formatted: Vec<String> = tags
                    .iter()
                    .map(|tag| format!("`{}`", redact_sensitive_text(&python_str(Some(tag)))))
                    .collect();
                lines.push(format!("- Tags: {}", formatted.join(", ")));
            }
        }
        if let Some(source_ref) = item.get("sourceRef").and_then(Value::as_object) {
            if !source_ref.is_empty() {
                let json_str = sorted_source_json(&Value::Object(source_ref.clone()));
                lines.push(format!("- Source: `{json_str}`"));
            }
        }
        let content = redact_sensitive_text(&truthy_str(item.get("content"), ""));
        lines.extend([String::new(), content, String::new()]);
    }
    if lines.is_empty() {
        String::new()
    } else {
        format!("{}\n", lines.join("\n").trim())
    }
}

pub fn artifacts_markdown(artifacts: &[Value]) -> String {
    let mut lines = Vec::new();
    for artifact in artifacts {
        let filename = artifact_filename(artifact);
        let raw_title = truthy_str(artifact.get("title"), &filename);
        let title = redact_sensitive_text(&raw_title);
        let art_type = truthy_str(artifact.get("type"), "");
        let version = artifact
            .get("version")
            .filter(|v| python_truthy(v))
            .and_then(|v| python_int_opt(Some(v)))
            .unwrap_or(1);
        let source = artifact
            .get("source")
            .filter(|v| python_truthy(v))
            .cloned()
            .unwrap_or_else(|| json!({}));
        let source_str = sorted_source_json(&source);
        lines.push(format!(
            "- [{title}](artifacts/{filename}) `{art_type}` v{version} source=`{source_str}`"
        ));
    }
    if lines.is_empty() {
        String::new()
    } else {
        format!("{}\n", lines.join("\n"))
    }
}

fn media_markdown(media_items: &[Value], root: &Path, entropy: &dyn Entropy) -> String {
    let mut lines = Vec::new();
    for media in media_items {
        let mut citation_hint = String::new();
        let media_id = truthy_str(media.get("mediaId"), "");
        for segment in list_segments_for_media(root, &media_id, entropy) {
            if let Some(citation) = segment.get("citation").and_then(Value::as_object) {
                if let Some(md) = citation.get("markdown").filter(|v| python_truthy(v)) {
                    citation_hint = format!(" citation=`{}`", python_str(Some(md)));
                    break;
                }
            }
        }
        let title = redact_sensitive_text(&truthy_str(media.get("title"), "Media"));
        let media_type = truthy_str(media.get("type"), "");
        let status = truthy_str(media.get("status"), "");
        lines.push(format!(
            "- {title} `{media_type}` id=`{media_id}` status=`{status}`{citation_hint}"
        ));
    }
    if lines.is_empty() {
        String::new()
    } else {
        format!("{}\n", lines.join("\n"))
    }
}

fn project_markdown(bundle: &Value, root: &Path, entropy: &dyn Entropy) -> String {
    let metadata = bundle
        .get("metadata")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let title = normalize_title(metadata.get("name"), "Project");
    let pid = truthy_opt(metadata.get("projectId"))
        .or_else(|| truthy_opt(metadata.get("id")))
        .map(|v| python_str(Some(v)))
        .unwrap_or_default();
    let exported_at = truthy_opt(bundle.get("exportedAt"))
        .map(|v| python_str(Some(v)))
        .unwrap_or_else(|| utc_now(entropy));
    let mut lines = vec![
        format!("# {}", redact_sensitive_text(&title)),
        String::new(),
        format!("- Project ID: `{pid}`"),
        format!("- Exported: `{exported_at}`"),
        String::new(),
    ];
    let description = truthy_str(metadata.get("description"), "");
    if !description.trim().is_empty() {
        lines.extend([
            "## Description".to_string(),
            String::new(),
            redact_sensitive_text(description.trim()),
            String::new(),
        ]);
    }
    if let Some(conversations) = bundle.get("conversations").and_then(Value::as_array) {
        if !conversations.is_empty() {
            lines.extend(["## Conversations".to_string(), String::new()]);
            for conversation in conversations {
                if !conversation.is_object() {
                    continue;
                }
                let conv_title =
                    redact_sensitive_text(&truthy_str(conversation.get("title"), "Conversation"));
                let conv_id = truthy_opt(conversation.get("conversationId"))
                    .or_else(|| truthy_opt(conversation.get("id")))
                    .map(|v| python_str(Some(v)))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "conversation".to_string());
                let conv_file = safe_filename(&conv_id, "file");
                lines.push(format!(
                    "- [{conv_title}](conversations/conversation-{conv_file}.md)"
                ));
            }
            lines.push(String::new());
        }
    }
    if let Some(saved_items) = bundle.get("savedItems").and_then(Value::as_array) {
        if !saved_items.is_empty() {
            lines.extend([
                "## Saved Items".to_string(),
                String::new(),
                saved_items_markdown(saved_items),
                String::new(),
            ]);
        }
    }
    if let Some(artifacts) = bundle.get("artifacts").and_then(Value::as_array) {
        if !artifacts.is_empty() {
            lines.extend([
                "## Artifacts".to_string(),
                String::new(),
                artifacts_markdown(artifacts),
                String::new(),
            ]);
        }
    }
    if let Some(media_items) = bundle.get("media").and_then(Value::as_array) {
        if !media_items.is_empty() {
            lines.extend([
                "## Media".to_string(),
                String::new(),
                media_markdown(media_items, root, entropy),
                String::new(),
            ]);
        }
    }
    format!("{}\n", lines.join("\n").trim())
}

fn zip_opts() -> FileOptions {
    FileOptions::default().compression_method(CompressionMethod::Deflated)
}

fn zip_write_bytes<W: Write + std::io::Seek>(
    archive: &mut ZipWriter<W>,
    name: &str,
    data: &[u8],
) -> Result<(), AppError> {
    archive
        .start_file(name, zip_opts())
        .map_err(|_| internal_error("Failed to write zip entry header"))?;
    archive
        .write_all(data)
        .map_err(|_| internal_error("Failed to write zip entry bytes"))
}

fn write_project_files_to_zip<W: Write + std::io::Seek>(
    archive: &mut ZipWriter<W>,
    root: &Path,
    project_id: &str,
    metadata: &Value,
) -> Result<(), AppError> {
    let Some(files) = truthy_opt(metadata.get("files"))
        .or_else(|| truthy_opt(metadata.get("documents")))
        .and_then(Value::as_array)
    else {
        return Ok(());
    };
    let cache = FileCache::new();
    for document in files {
        if !document.is_object() {
            continue;
        }
        let file_id = truthy_str(document.get("fileId"), "");
        if file_id.is_empty() {
            continue;
        }
        let Ok(cached) = load_cached_file(root, &file_id, Some(project_id), &cache) else {
            continue;
        };
        let mut text = if let Some(chunks) = cached.get("chunks").and_then(Value::as_array) {
            chunks
                .iter()
                .filter(|chunk| chunk.is_object())
                .map(|chunk| truthy_str(chunk.get("text"), ""))
                .collect::<Vec<String>>()
                .join("\n\n")
        } else {
            String::new()
        };
        if text.is_empty() {
            text = truthy_str(cached.get("preview"), "");
        }
        let raw_name = truthy_str(cached.get("name"), &file_id);
        let filename = format!("{}.txt", safe_filename(&raw_name, "source-file"));
        let entry_name = format!("files/source-files/{filename}");
        let redacted = redact_sensitive_text(&text);
        zip_write_bytes(archive, &entry_name, redacted.as_bytes())?;
    }
    Ok(())
}

fn write_artifacts_to_zip<W: Write + std::io::Seek>(
    archive: &mut ZipWriter<W>,
    root: &Path,
    artifacts: &[Value],
) -> Result<(), AppError> {
    for artifact in artifacts {
        if !artifact.is_object() {
            continue;
        }
        let Ok(path) = artifact_path(root, artifact) else {
            continue;
        };
        if !path.is_file() {
            continue;
        }
        let Ok(data) = fs::read(&path) else {
            continue;
        };
        let filename = artifact_filename(artifact);
        let artifact_type = truthy_str(artifact.get("type"), "").to_lowercase();
        let entry_name = format!("artifacts/{filename}");
        if TEXT_PREVIEW_TYPES.contains(&artifact_type.as_str()) || contains_secret(&data) {
            let redacted = redact_sensitive_text(&String::from_utf8_lossy(&data));
            zip_write_bytes(archive, &entry_name, redacted.as_bytes())?;
        } else {
            zip_write_bytes(archive, &entry_name, &data)?;
        }
    }
    Ok(())
}

fn write_media_to_zip<W: Write + std::io::Seek>(
    archive: &mut ZipWriter<W>,
    root: &Path,
    media_items: &[Value],
    entropy: &dyn Entropy,
) -> Result<(), AppError> {
    let mut redacted_items = Vec::new();
    for item in media_items {
        if item.is_object() {
            redacted_items.push(redact_value(item, ""));
        }
    }
    let media_json = pretty_redacted(&json!({ "items": redacted_items }))?;
    zip_write_bytes(archive, "media/media.json", media_json.as_bytes())?;
    for media in media_items {
        if !media.is_object() {
            continue;
        }
        let media_id = truthy_str(media.get("mediaId"), "");
        if media_id.is_empty() {
            continue;
        }
        let segments = list_segments_for_media(root, &media_id, entropy);
        let seg_json = pretty_redacted(&json!({ "segments": segments }))?;
        let seg_entry = format!("media/segments/{}.json", safe_filename(&media_id, "media"));
        zip_write_bytes(archive, &seg_entry, seg_json.as_bytes())?;
        let Ok(path) = media_file_path(root, media) else {
            continue;
        };
        if !path.is_file() {
            continue;
        }
        let Ok(data) = fs::read(&path) else {
            continue;
        };
        let path_name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("media")
            .to_string();
        let raw_title = truthy_str(media.get("title"), &path_name);
        let mut filename = safe_filename(&raw_title, "media");
        let (_, suffix) = path_stem_and_lower_suffix(&path_name);
        if !suffix.is_empty() && !filename.to_lowercase().ends_with(&suffix) {
            filename.push_str(&suffix);
        }
        let source_entry = format!("media/source/{filename}");
        if contains_secret(&data) || MEDIA_TEXT_SUFFIXES.contains(&suffix.as_str()) {
            let redacted = redact_sensitive_text(&String::from_utf8_lossy(&data));
            zip_write_bytes(archive, &source_entry, redacted.as_bytes())?;
        } else {
            zip_write_bytes(archive, &source_entry, &data)?;
        }
    }
    Ok(())
}

fn write_trace_items_to_zip<W: Write + std::io::Seek>(
    archive: &mut ZipWriter<W>,
    items: &[Value],
) -> Result<(), AppError> {
    for item in items {
        if !item.is_object() || item.get("type").and_then(Value::as_str) != Some("trace") {
            continue;
        }
        let name = safe_filename(&truthy_str(item.get("savedId"), "trace"), "trace");
        let entry = format!("traces/trace-{name}.json");
        let body = pretty_redacted(item)?;
        zip_write_bytes(archive, &entry, body.as_bytes())?;
    }
    Ok(())
}

pub fn export_dir(root: &Path, project_id: &str) -> Result<PathBuf, AppError> {
    if project_id.is_empty() {
        Ok(generated_dir(root).join("workspace-exports"))
    } else {
        let safe_id = validate_project_id(project_id)?;
        Ok(projects::projects_dir(root).join(safe_id).join("exports"))
    }
}

fn export_includes(root: &Path, project_id: &str, kind: &str, entropy: &dyn Entropy) -> Value {
    let mut includes = Map::new();
    if project_id.is_empty() {
        return Value::Object(includes);
    }
    includes.insert(
        "projectId".to_string(),
        Value::String(project_id.to_string()),
    );
    let Ok(bundle) = project_bundle(root, project_id, entropy) else {
        return Value::Object(includes);
    };
    let collect_ids = |field: &str, keys: &[&str]| -> Vec<Value> {
        bundle
            .get(field)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter(|item| item.is_object())
            .map(|item| {
                let picked = keys
                    .iter()
                    .find_map(|key| truthy_opt(item.get(*key)))
                    .map(|val| python_str(Some(val)))
                    .unwrap_or_default();
                Value::String(picked)
            })
            .collect()
    };
    match kind {
        "project" => {
            includes.insert(
                "conversationIds".to_string(),
                Value::Array(collect_ids("conversations", &["conversationId", "id"])),
            );
            includes.insert(
                "savedItemIds".to_string(),
                Value::Array(collect_ids("savedItems", &["savedId"])),
            );
            includes.insert(
                "artifactIds".to_string(),
                Value::Array(collect_ids("artifacts", &["artifactId"])),
            );
            includes.insert(
                "mediaIds".to_string(),
                Value::Array(collect_ids("media", &["mediaId"])),
            );
        }
        "saved_items" => {
            includes.insert(
                "savedItemIds".to_string(),
                Value::Array(collect_ids("savedItems", &["savedId"])),
            );
        }
        "artifacts" => {
            includes.insert(
                "artifactIds".to_string(),
                Value::Array(collect_ids("artifacts", &["artifactId"])),
            );
        }
        _ => {}
    }
    Value::Object(includes)
}

fn write_export_with<F>(
    root: &Path,
    spec: ExportSpec<'_>,
    entropy: &dyn Entropy,
    writer: F,
) -> Result<Value, AppError>
where
    F: FnOnce(&Path) -> Result<(), AppError>,
{
    let safe_project_id = if spec.project_id.is_empty() {
        String::new()
    } else {
        validate_project_id(spec.project_id)?
    };
    let export_id = new_id("export", entropy)?;
    let dir = export_dir(root, &safe_project_id)?;
    fs::create_dir_all(&dir).map_err(|_| internal_error("Failed to create export directory"))?;
    let target = dir.join(format!(
        "{export_id}-{}",
        safe_filename(spec.filename, "workspace-export")
    ));
    let temp = NamedTempFile::new_in(&dir)
        .map_err(|_| internal_error("Failed to allocate export file"))?;
    let temp_path = temp.path().to_path_buf();
    writer(&temp_path)?;
    temp.as_file()
        .sync_all()
        .map_err(|_| internal_error("Failed to flush export file"))?;
    temp.persist(&target)
        .map_err(|_| internal_error("Failed to finalize export file"))?;
    let size = fs::metadata(&target)
        .map_err(|_| internal_error("Failed to stat export file"))?
        .len();
    let download_suffix = if safe_project_id.is_empty() {
        String::new()
    } else {
        format!("?projectId={safe_project_id}")
    };
    let export = json!({
        "exportId": export_id,
        "projectId": safe_project_id,
        "kind": spec.kind,
        "format": spec.format,
        "filename": spec.filename,
        "path": target.to_string_lossy().to_string(),
        "size": size,
        "includes": export_includes(root, &safe_project_id, spec.kind, entropy),
        "createdAt": utc_now(entropy),
        "downloadUrl": format!("/api/workspace/exports/{export_id}/download{download_suffix}"),
    });
    let store_path = dir.join(STORE_NAME);
    let existing = read_json_file(&store_path, json!({ "exports": [] }));
    let mut exports: Vec<Value> = existing
        .get("exports")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| item.is_object())
        .cloned()
        .collect();
    exports.push(export.clone());
    if exports.len() > 100 {
        let keep_from = exports.len() - 100;
        exports.drain(..keep_from);
    }
    if let Err(err) = write_json_atomic(root, &store_path, &json!({ "exports": exports })) {
        let _ = fs::remove_file(&target);
        return Err(err);
    }
    Ok(json!({ "ok": true, "export": export }))
}

fn write_text_file(path: &Path, content: &[u8]) -> Result<(), AppError> {
    let mut file = File::create(path).map_err(|_| internal_error("Failed to write export"))?;
    file.write_all(content)
        .map_err(|_| internal_error("Failed to write export"))?;
    file.sync_all()
        .map_err(|_| internal_error("Failed to sync export"))
}

fn export_project(
    root: &Path,
    project_id: &str,
    export_format: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let project = workspace_projects::get_project(&safe_project_id, root, entropy)?;
    let fmt = normalize_export_format(Some(&Value::String(export_format.to_string())))?;
    let proj_slug = safe_filename(&truthy_str(project.get("name"), "project"), "project");
    if fmt == "zip" {
        let filename = format!("{proj_slug}-project-export.zip");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "project",
                format: "zip",
                filename: &filename,
            },
            entropy,
            |path| {
                let bundle = project_bundle(root, &safe_project_id, entropy)?;
                let metadata = bundle.get("metadata").cloned().unwrap_or_else(|| json!({}));
                let saved_items = bundle
                    .get("savedItems")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let artifacts = bundle
                    .get("artifacts")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let media_items = bundle
                    .get("media")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let conversations = bundle
                    .get("conversations")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let file = File::create(path)
                    .map_err(|_| internal_error("Failed to create zip export"))?;
                let mut archive = ZipWriter::new(BufWriter::new(file));
                zip_write_bytes(
                    &mut archive,
                    "metadata.json",
                    pretty_redacted(&metadata)?.as_bytes(),
                )?;
                zip_write_bytes(
                    &mut archive,
                    "saved-items/saved-items.json",
                    pretty_redacted(&json!({ "items": saved_items }))?.as_bytes(),
                )?;
                zip_write_bytes(
                    &mut archive,
                    "project.md",
                    project_markdown(&bundle, root, entropy).as_bytes(),
                )?;
                for conversation in &conversations {
                    if !conversation.is_object() {
                        continue;
                    }
                    let cid = truthy_opt(conversation.get("conversationId"))
                        .or_else(|| truthy_opt(conversation.get("id")))
                        .map(|v| python_str(Some(v)))
                        .filter(|s| !s.is_empty())
                        .unwrap_or_else(|| "conversation".to_string());
                    let entry = format!(
                        "conversations/conversation-{}.md",
                        safe_filename(&cid, "conversation")
                    );
                    zip_write_bytes(
                        &mut archive,
                        &entry,
                        conversation_markdown(conversation).as_bytes(),
                    )?;
                }
                write_project_files_to_zip(&mut archive, root, &safe_project_id, &metadata)?;
                write_artifacts_to_zip(&mut archive, root, &artifacts)?;
                write_media_to_zip(&mut archive, root, &media_items, entropy)?;
                write_trace_items_to_zip(&mut archive, &saved_items)?;
                archive
                    .finish()
                    .map_err(|_| internal_error("Failed to close zip archive"))?;
                Ok(())
            },
        );
    }
    let bundle = project_bundle(root, &safe_project_id, entropy)?;
    if fmt == "json" {
        let content = pretty_redacted(&bundle)?;
        let filename = format!("{proj_slug}-project-export.json");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "project",
                format: "json",
                filename: &filename,
            },
            entropy,
            |path| write_text_file(path, content.as_bytes()),
        );
    }
    let markdown = project_markdown(&bundle, root, entropy);
    if fmt == "html" {
        let html = markdown_to_html(&markdown);
        let filename = format!("{proj_slug}-project-export.html");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "project",
                format: "html",
                filename: &filename,
            },
            entropy,
            |path| write_text_file(path, html.as_bytes()),
        );
    }
    let filename = format!("{proj_slug}-project-export.md");
    write_export_with(
        root,
        ExportSpec {
            project_id: &safe_project_id,
            kind: "project",
            format: "markdown",
            filename: &filename,
        },
        entropy,
        |path| write_text_file(path, markdown.as_bytes()),
    )
}

fn export_conversation(
    root: &Path,
    conversation: &Value,
    project_id: &str,
    export_format: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = if project_id.is_empty() {
        String::new()
    } else {
        validate_project_id(project_id)?
    };
    let normalized = workspace_projects::normalize_conversation(conversation, entropy);
    let fmt = normalize_export_format(Some(&Value::String(export_format.to_string())))?;
    let base = safe_filename(
        &truthy_str(normalized.get("title"), "conversation"),
        "conversation",
    );
    if fmt == "json" {
        let content = pretty_redacted(&json!({
            "schemaVersion": EXPORT_SCHEMA_VERSION,
            "conversation": normalized,
        }))?;
        let filename = format!("{base}.json");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "conversation",
                format: "json",
                filename: &filename,
            },
            entropy,
            |path| write_text_file(path, content.as_bytes()),
        );
    }
    let markdown = conversation_markdown(&normalized);
    if fmt == "html" {
        let html = markdown_to_html(&markdown);
        let filename = format!("{base}.html");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "conversation",
                format: "html",
                filename: &filename,
            },
            entropy,
            |path| write_text_file(path, html.as_bytes()),
        );
    }
    if fmt == "zip" {
        let conv_id = normalized
            .get("conversationId")
            .cloned()
            .unwrap_or_else(|| json!(""));
        let filename = format!("{base}.zip");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "conversation",
                format: "zip",
                filename: &filename,
            },
            entropy,
            |path| {
                let file = File::create(path)
                    .map_err(|_| internal_error("Failed to create zip export"))?;
                let mut archive = ZipWriter::new(BufWriter::new(file));
                let meta = pretty_redacted(&json!({
                    "schemaVersion": EXPORT_SCHEMA_VERSION,
                    "kind": "conversation",
                    "conversationId": conv_id,
                }))?;
                zip_write_bytes(&mut archive, "metadata.json", meta.as_bytes())?;
                zip_write_bytes(
                    &mut archive,
                    &format!("conversations/{base}.md"),
                    markdown.as_bytes(),
                )?;
                archive
                    .finish()
                    .map_err(|_| internal_error("Failed to close zip archive"))?;
                Ok(())
            },
        );
    }
    let filename = format!("{base}.md");
    write_export_with(
        root,
        ExportSpec {
            project_id: &safe_project_id,
            kind: "conversation",
            format: "markdown",
            filename: &filename,
        },
        entropy,
        |path| write_text_file(path, markdown.as_bytes()),
    )
}

fn export_saved_items(
    root: &Path,
    project_id: &str,
    saved_ids: &[String],
    export_format: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let fmt = normalize_export_format(Some(&Value::String(export_format.to_string())))?;
    let ids: HashSet<&str> = saved_ids.iter().map(String::as_str).collect();
    let mut items = workspace_projects::list_saved_items(&safe_project_id, root, "", &[])?;
    if !ids.is_empty() {
        items.retain(|item| {
            let sid = item.get("savedId").and_then(Value::as_str).unwrap_or("");
            ids.contains(sid)
        });
    }
    if fmt == "zip" {
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "saved_items",
                format: "zip",
                filename: "saved-items.zip",
            },
            entropy,
            |path| {
                let file = File::create(path)
                    .map_err(|_| internal_error("Failed to create zip export"))?;
                let mut archive = ZipWriter::new(BufWriter::new(file));
                zip_write_bytes(
                    &mut archive,
                    "saved-items/saved-items.json",
                    pretty_redacted(&json!({ "items": items }))?.as_bytes(),
                )?;
                zip_write_bytes(
                    &mut archive,
                    "saved-items/saved-items.md",
                    saved_items_markdown(&items).as_bytes(),
                )?;
                archive
                    .finish()
                    .map_err(|_| internal_error("Failed to close zip archive"))?;
                Ok(())
            },
        );
    }
    if fmt == "html" {
        let html = markdown_to_html(&saved_items_markdown(&items));
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "saved_items",
                format: "html",
                filename: "saved-items.html",
            },
            entropy,
            |path| write_text_file(path, html.as_bytes()),
        );
    }
    if fmt == "markdown" {
        let md = saved_items_markdown(&items);
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "saved_items",
                format: "markdown",
                filename: "saved-items.md",
            },
            entropy,
            |path| write_text_file(path, md.as_bytes()),
        );
    }
    let content = pretty_redacted(&json!({
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "items": items,
    }))?;
    write_export_with(
        root,
        ExportSpec {
            project_id: &safe_project_id,
            kind: "saved_items",
            format: "json",
            filename: "saved-items.json",
        },
        entropy,
        |path| write_text_file(path, content.as_bytes()),
    )
}

fn export_artifacts(
    root: &Path,
    project_id: &str,
    artifact_ids: &[String],
    export_format: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let fmt = normalize_export_format(Some(&Value::String(export_format.to_string())))?;
    let ids: HashSet<&str> = artifact_ids.iter().map(String::as_str).collect();
    let mut artifacts = workspace_projects::list_artifacts(&safe_project_id, root)?;
    if !ids.is_empty() {
        artifacts.retain(|item| {
            let aid = item.get("artifactId").and_then(Value::as_str).unwrap_or("");
            ids.contains(aid)
        });
    }
    if fmt == "json" {
        let content = pretty_redacted(&json!({
            "schemaVersion": EXPORT_SCHEMA_VERSION,
            "artifacts": artifacts,
        }))?;
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "artifacts",
                format: "json",
                filename: "artifacts.json",
            },
            entropy,
            |path| write_text_file(path, content.as_bytes()),
        );
    }
    if fmt == "markdown" || fmt == "html" {
        let markdown = artifacts_markdown(&artifacts);
        if fmt == "html" {
            let html = markdown_to_html(&markdown);
            return write_export_with(
                root,
                ExportSpec {
                    project_id: &safe_project_id,
                    kind: "artifacts",
                    format: "html",
                    filename: "artifacts.html",
                },
                entropy,
                |path| write_text_file(path, html.as_bytes()),
            );
        }
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "artifacts",
                format: "markdown",
                filename: "artifacts.md",
            },
            entropy,
            |path| write_text_file(path, markdown.as_bytes()),
        );
    }
    write_export_with(
        root,
        ExportSpec {
            project_id: &safe_project_id,
            kind: "artifacts",
            format: "zip",
            filename: "artifacts.zip",
        },
        entropy,
        |path| {
            let file =
                File::create(path).map_err(|_| internal_error("Failed to create zip export"))?;
            let mut archive = ZipWriter::new(BufWriter::new(file));
            zip_write_bytes(
                &mut archive,
                "metadata.json",
                pretty_redacted(&json!({
                    "schemaVersion": EXPORT_SCHEMA_VERSION,
                    "kind": "artifacts",
                    "artifacts": artifacts,
                }))?
                .as_bytes(),
            )?;
            write_artifacts_to_zip(&mut archive, root, &artifacts)?;
            archive
                .finish()
                .map_err(|_| internal_error("Failed to close zip archive"))?;
            Ok(())
        },
    )
}

fn export_evidence(
    root: &Path,
    payload: &Value,
    project_id: &str,
    export_format: &str,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = if project_id.is_empty() {
        String::new()
    } else {
        validate_project_id(project_id)?
    };
    let fmt = normalize_export_format(Some(&Value::String(export_format.to_string())))?;
    let traces = payload
        .get("traces")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let evals = payload
        .get("evals")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let evidence = json!({
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "evidence",
        "traces": traces,
        "evals": evals,
        "sourceRef": normalize_source_ref(payload.get("sourceRef").unwrap_or(&Value::Null)),
        "exportedAt": utc_now(entropy),
    });
    if fmt == "zip" {
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "evidence",
                format: "zip",
                filename: "workspace-evidence.zip",
            },
            entropy,
            |path| {
                let file = File::create(path)
                    .map_err(|_| internal_error("Failed to create zip export"))?;
                let mut archive = ZipWriter::new(BufWriter::new(file));
                zip_write_bytes(
                    &mut archive,
                    "metadata.json",
                    pretty_redacted(&evidence)?.as_bytes(),
                )?;
                zip_write_bytes(
                    &mut archive,
                    "traces/traces.json",
                    pretty_redacted(evidence.get("traces").unwrap_or(&json!([])))?.as_bytes(),
                )?;
                zip_write_bytes(
                    &mut archive,
                    "evals/evals.json",
                    pretty_redacted(evidence.get("evals").unwrap_or(&json!([])))?.as_bytes(),
                )?;
                archive
                    .finish()
                    .map_err(|_| internal_error("Failed to close zip archive"))?;
                Ok(())
            },
        );
    }
    let content = pretty_redacted(&evidence)?;
    if fmt == "html" {
        let html = markdown_to_html(&format!("```json\n{content}\n```"));
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "evidence",
                format: "html",
                filename: "workspace-evidence.html",
            },
            entropy,
            |path| write_text_file(path, html.as_bytes()),
        );
    }
    if fmt == "markdown" {
        let md = format!("```json\n{content}\n```");
        return write_export_with(
            root,
            ExportSpec {
                project_id: &safe_project_id,
                kind: "evidence",
                format: "markdown",
                filename: "workspace-evidence.md",
            },
            entropy,
            |path| write_text_file(path, md.as_bytes()),
        );
    }
    write_export_with(
        root,
        ExportSpec {
            project_id: &safe_project_id,
            kind: "evidence",
            format: "json",
            filename: "workspace-evidence.json",
        },
        entropy,
        |path| write_text_file(path, content.as_bytes()),
    )
}

pub fn create_export(
    root: &Path,
    payload: &Value,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let raw_kind = truthy_opt(payload.get("kind"))
        .or_else(|| truthy_opt(payload.get("type")))
        .map(|v| python_str(Some(v)))
        .unwrap_or_else(|| "project".to_string());
    let kind = raw_kind.trim().to_lowercase();
    let format_val = truthy_opt(payload.get("format"))
        .cloned()
        .unwrap_or_else(|| json!("zip"));
    let export_format = normalize_export_format(Some(&format_val))?;
    let project_id = truthy_opt(payload.get("projectId"))
        .map(|v| python_str(Some(v)))
        .unwrap_or_default()
        .trim()
        .to_string();
    match kind.as_str() {
        "project" => export_project(root, &project_id, &export_format, entropy),
        "conversation" => {
            let conversation = payload
                .get("conversation")
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}));
            export_conversation(root, &conversation, &project_id, &export_format, entropy)
        }
        "saved_items" | "saved-items" | "saved" => {
            let saved_ids: Vec<String> = payload
                .get("savedIds")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(|v| python_str(Some(v))).collect())
                .unwrap_or_default();
            export_saved_items(root, &project_id, &saved_ids, &export_format, entropy)
        }
        "artifacts" | "artifact_package" => {
            let artifact_ids: Vec<String> = payload
                .get("artifactIds")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(|v| python_str(Some(v))).collect())
                .unwrap_or_default();
            export_artifacts(root, &project_id, &artifact_ids, &export_format, entropy)
        }
        "evidence" | "trace_eval" | "trace-eval" => {
            export_evidence(root, payload, &project_id, &export_format, entropy)
        }
        _ => Err(AppError::invalid_payload("Unsupported export kind")),
    }
}

#[cfg(test)]
mod tests {
    use std::io::Read;
    use std::sync::atomic::{AtomicU64, Ordering};

    use zip::ZipArchive;

    use super::*;

    struct TestEntropy {
        seq: AtomicU64,
        millis: i64,
    }

    impl TestEntropy {
        fn new(millis: i64) -> Self {
            Self {
                seq: AtomicU64::new(1),
                millis,
            }
        }
    }

    impl Entropy for TestEntropy {
        fn new_id(&self) -> Result<String, AppError> {
            let n = self.seq.fetch_add(1, Ordering::Relaxed);
            Ok(format!("{n:016x}"))
        }

        fn now_millis(&self) -> i64 {
            self.millis
        }
    }

    #[test]
    fn project_and_sub_exports_match_oracle_shapes_and_redaction() {
        let root = tempfile::tempdir().unwrap();
        let entropy = TestEntropy::new(1_700_000_000_000);
        let proj =
            workspace_projects::create_project("Alpha <script>Project", "", root.path(), &entropy)
                .unwrap();
        let pid = proj["id"].as_str().unwrap().to_string();

        workspace_projects::upsert_project_conversation(
            &pid,
            &json!({
                "id": "conv-1",
                "title": "Conv Title",
                "messages": [{"id": "m1", "role": "user", "content": "hi sk-1234567890abcdefghij"}]
            }),
            root.path(),
            &entropy,
        )
        .unwrap();
        workspace_projects::create_saved_item(
            &pid,
            root.path(),
            workspace_projects::SavedItemInput {
                item_type: "artifact",
                title: "Saved 1",
                content: "saved sk-1234567890abcdefghij",
                source_ref: &json!({}),
                tags: &json!(["s"]),
                purpose: "reference",
            },
            &entropy,
        )
        .unwrap();
        let gen_dir = generated_dir(root.path());
        fs::create_dir_all(&gen_dir).unwrap();
        fs::write(
            gen_dir.join("note.txt"),
            b"artifact sk-1234567890abcdefghij ok",
        )
        .unwrap();
        workspace_projects::register_artifact(
            &pid,
            root.path(),
            "csv",
            "Art 1",
            "generated/note.txt",
            &json!({}),
            &entropy,
        )
        .unwrap();

        let proj_html = create_export(
            root.path(),
            &json!({"projectId": pid, "kind": "project", "format": "html"}),
            &entropy,
        )
        .unwrap();
        let ex = &proj_html["export"];
        assert_eq!(ex["kind"], "project");
        assert_eq!(ex["filename"], "Alpha-script-Project-project-export.html");
        assert!(
            ex["downloadUrl"]
                .as_str()
                .unwrap()
                .ends_with(&format!("/download?projectId={pid}"))
        );
        let html_body = fs::read_to_string(ex["path"].as_str().unwrap()).unwrap();
        assert!(html_body.contains("# Alpha &lt;script&gt;Project"));
        assert!(html_body.contains("[redacted-secret]"));
        assert!(!html_body.contains("sk-1234567890abcdefghij"));

        let saved_zip = create_export(
            root.path(),
            &json!({"projectId": pid, "kind": "saved-items", "format": "zip"}),
            &entropy,
        )
        .unwrap();
        assert_eq!(saved_zip["export"]["kind"], "saved_items");
        let zip_file = File::open(saved_zip["export"]["path"].as_str().unwrap()).unwrap();
        let mut archive = ZipArchive::new(zip_file).unwrap();
        let mut saved_md = String::new();
        archive
            .by_name("saved-items/saved-items.md")
            .unwrap()
            .read_to_string(&mut saved_md)
            .unwrap();
        assert!(saved_md.contains("### Saved 1"));
        assert!(saved_md.contains("[redacted-secret]"));

        let err = create_export(
            root.path(),
            &json!({"projectId": pid, "kind": "bogus"}),
            &entropy,
        )
        .unwrap_err();
        assert_eq!(err.status, 400);
        assert_eq!(err.message, "Unsupported export kind");
    }

    #[test]
    fn inline_conversation_and_evidence_exports_write_to_generated_dir() {
        let root = tempfile::tempdir().unwrap();
        let entropy = TestEntropy::new(1_700_000_000_000);
        let conv = create_export(
            root.path(),
            &json!({
                "kind": "conversation",
                "format": "markdown",
                "conversation": {
                    "id": "c-inline",
                    "title": "Inline",
                    "messages": [{"role": "assistant", "content": "ok"}]
                }
            }),
            &entropy,
        )
        .unwrap();
        assert_eq!(conv["export"]["projectId"], "");
        assert!(
            Path::new(conv["export"]["path"].as_str().unwrap())
                .starts_with(generated_dir(root.path()).join("workspace-exports"))
        );

        let ev = create_export(
            root.path(),
            &json!({
                "kind": "evidence",
                "format": "zip",
                "traces": [{"token": "sk-1234567890abcdefghij"}]
            }),
            &entropy,
        )
        .unwrap();
        let file = File::open(ev["export"]["path"].as_str().unwrap()).unwrap();
        let mut zip = ZipArchive::new(file).unwrap();
        let mut traces_json = String::new();
        zip.by_name("traces/traces.json")
            .unwrap()
            .read_to_string(&mut traces_json)
            .unwrap();
        assert!(traces_json.contains("[redacted-secret]"));
        assert!(!traces_json.contains("sk-1234567890abcdefghij"));
    }
}
