//! Shared schema normalisation and redaction helpers for Workspace Core.
//!
//! Mirrors `deepseek_infra/infra/workspace/schema.py`. Every Workspace store
//! (projects, saved items, artifacts, conversations) normalises through this module,
//! so it is the prerequisite for all of them — and `infra/memory/schema.py` imports
//! `normalize_source_ref` from here too, which is why [`memory_schema`] delegates
//! rather than keeping its own copy.
//!
//! [`memory_schema`]: crate::memory_schema
//!
//! # What is a contract here and what is not
//!
//! The **caps** are part of the on-disk shape (`MAX_CONTENT_CHARS` 200 000,
//! `MAX_TAGS` 24, `MAX_TAG_CHARS` 40), and so is the tag de-duplication key: tags
//! collapse case-insensitively while the **first** spelling wins. The redaction
//! patterns are a security boundary — they decide what a preview may show — so they
//! are ported pattern-for-pattern rather than "improved".

use std::path::{Path, PathBuf};

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::entropy::Entropy;

/// `MAX_TITLE_CHARS`.
pub const MAX_TITLE_CHARS: usize = 160;
/// `MAX_DESCRIPTION_CHARS`.
pub const MAX_DESCRIPTION_CHARS: usize = 2_000;
/// `MAX_CONTENT_CHARS`.
pub const MAX_CONTENT_CHARS: usize = 200_000;
/// `MAX_SOURCE_REF_VALUE_CHARS` — shared with `memory_schema`.
pub const MAX_SOURCE_REF_VALUE_CHARS: usize = 2_000;
/// `MAX_TAGS`.
pub const MAX_TAGS: usize = 24;
/// `MAX_TAG_CHARS`.
pub const MAX_TAG_CHARS: usize = 40;

/// `SAVED_ITEM_TYPES`.
pub const SAVED_ITEM_TYPES: [&str; 9] = [
    "chat_snippet",
    "assistant_answer",
    "file_quote",
    "rag_citation",
    "artifact",
    "webpage",
    "media",
    "trace",
    "eval_result",
];

/// `SAVED_ITEM_PURPOSES`.
pub const SAVED_ITEM_PURPOSES: [&str; 3] = ["reference", "memory_candidate", "export_fragment"];

/// `ARTIFACT_TYPES`.
pub const ARTIFACT_TYPES: [&str; 10] = [
    "pptx", "docx", "pdf", "svg", "markdown", "md", "csv", "json", "html", "txt",
];

/// `EXPORT_FORMATS`.
pub const EXPORT_FORMATS: [&str; 5] = ["markdown", "md", "html", "json", "zip"];

fn whitespace_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\s+").expect("whitespace pattern"))
}

/// `PROJECT_ID_RE` / `WORKSPACE_ID_RE` — `[a-zA-Z0-9_-]{n,m}` anchored.
fn project_id_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\A[a-zA-Z0-9_-]{4,64}\z").expect("project id pattern"))
}

fn workspace_id_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\A[a-zA-Z0-9_-]{4,80}\z").expect("workspace id pattern"))
}

// --- time -------------------------------------------------------------------------

/// `now_ms()` — the wall clock in milliseconds.
///
/// Taken through [`Entropy`] rather than read here, so a caller can pin it and the
/// store stays testable.
pub fn now_ms(entropy: &dyn Entropy) -> i64 {
    entropy.now_millis()
}

/// `timestamp_ms_to_iso`: `int(value or 0) / 1000` rendered as
/// `...isoformat(timespec="seconds")` with `+00:00` replaced by `Z`.
///
/// Three behaviours worth stating because each is easy to get wrong:
///
/// - A value that `int()` rejects (`"abc"`) and a non-positive one both render as
///   the **empty string**, not as an epoch date.
/// - The division is a Python **float** division but `timespec="seconds"` truncates,
///   so `1500` ms renders as `00:00:01Z` — integer division gives the same answer.
/// - The result uses `Z`, not `+00:00`, which is the one place this module differs
///   from [`crate::core_utils::utc_now_iso`].
pub fn timestamp_ms_to_iso(value: Option<&Value>) -> String {
    let millis = match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or_else(|| {
            // A float truncates toward zero, like Python's `int()`.
            number.as_f64().map(|float| float as i64).unwrap_or(0)
        }),
        Some(Value::String(text)) => match python_int(text) {
            Some(parsed) => parsed,
            None => return String::new(),
        },
        Some(Value::Bool(flag)) => {
            if *flag {
                1
            } else {
                0
            }
        }
        // `None`, `null`, arrays and objects all take the `0` fallback.
        _ => 0,
    };
    if millis <= 0 {
        return String::new();
    }
    let seconds = millis.div_euclid(1000);
    crate::core_utils::utc_now_iso(seconds).replace("+00:00", "Z")
}

/// Python's `int()` over a string, or `None` where it raises.
fn python_int(text: &str) -> Option<i64> {
    crate::core_utils::python_int_opt(Some(&Value::String(text.to_string())))
}

// --- ids --------------------------------------------------------------------------

/// `new_id(prefix)`: the prefix sanitised to `[a-z0-9_]`, then `_`, then 16 hex chars.
///
/// An empty prefix after sanitising becomes `item`, and leading/trailing underscores
/// are stripped — so `new_id("!!")` is `item_<hex>`, not `_<hex>`.
pub fn new_id(prefix: &str, entropy: &dyn Entropy) -> Result<String, AppError> {
    let sanitized: String = prefix
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_')
        .collect();
    let trimmed = sanitized.trim_matches('_');
    let safe_prefix = if trimmed.is_empty() { "item" } else { trimmed };
    Ok(format!("{safe_prefix}_{}", entropy.new_id()?))
}

/// `validate_project_id`.
pub fn validate_project_id(project_id: &str) -> Result<String, AppError> {
    let value = project_id.trim();
    if !project_id_regex().is_match(value) {
        return Err(AppError::invalid_payload("Invalid project id"));
    }
    Ok(value.to_string())
}

/// `validate_workspace_id(value, label=…)`.
pub fn validate_workspace_id(value: &str, label: &str) -> Result<String, AppError> {
    let safe = value.trim();
    if !workspace_id_regex().is_match(safe) {
        return Err(AppError::invalid_payload(format!("Invalid {label}")));
    }
    Ok(safe.to_string())
}

// --- normalisation ----------------------------------------------------------------

/// `normalize_title`: whitespace collapsed, trimmed, capped, then the default.
pub fn normalize_title(value: Option<&Value>, default: &str) -> String {
    let raw = python_str(value);
    let collapsed = whitespace_regex().replace_all(&raw, " ").to_string();
    let title: String = collapsed.trim().chars().take(MAX_TITLE_CHARS).collect();
    if title.is_empty() {
        default.to_string()
    } else {
        title
    }
}

/// `normalize_description`: CRLF normalised to LF, trimmed, capped. **No** collapse.
pub fn normalize_description(value: Option<&Value>) -> String {
    normalize_content_with_cap(value, MAX_DESCRIPTION_CHARS)
}

/// `normalize_content`: CRLF normalised to LF, trimmed, capped at 200 000.
pub fn normalize_content(value: Option<&Value>) -> String {
    normalize_content_with_cap(value, MAX_CONTENT_CHARS)
}

fn normalize_content_with_cap(value: Option<&Value>, cap: usize) -> String {
    let raw = python_str(value).replace("\r\n", "\n");
    raw.trim().chars().take(cap).collect()
}

/// `normalize_tags`: whitespace-collapsed, capped at 40 chars, de-duplicated
/// **case-insensitively** keeping the first spelling, at most 24.
///
/// A non-list input is `[]` rather than an error — the oracle's own tolerance.
pub fn normalize_tags(value: Option<&Value>) -> Vec<String> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut tags: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for item in items {
        let raw = python_str(Some(item));
        let collapsed = whitespace_regex().replace_all(&raw, " ").to_string();
        let tag: String = collapsed.trim().chars().take(MAX_TAG_CHARS).collect();
        let key = tag.to_lowercase();
        if tag.is_empty() || seen.contains(&key) {
            continue;
        }
        seen.push(key);
        tags.push(tag);
        if tags.len() >= MAX_TAGS {
            break;
        }
    }
    tags
}

/// `normalize_source_ref` — a bounded, key-sanitised recursive copy.
///
/// Lives here because `workspace/schema.py` is where the oracle defines it and
/// `memory/schema.py` imports it from there; [`crate::memory_schema`] delegates.
pub fn normalize_source_ref(value: &Value) -> Value {
    let Value::Object(fields) = value else {
        return json!({});
    };
    let mut result = Map::new();
    for (key, item) in fields {
        let safe_key: String = key
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
            .take(80)
            .collect();
        if safe_key.is_empty() {
            continue;
        }
        match item {
            Value::Object(_) => {
                let nested = normalize_source_ref(item);
                if nested.as_object().is_some_and(|object| !object.is_empty()) {
                    result.insert(safe_key, nested);
                }
            }
            Value::Array(items) => {
                let cleaned: Vec<Value> = items
                    .iter()
                    .take(20)
                    .filter(|child| !child.is_null())
                    .map(|child| {
                        Value::String(
                            python_str(Some(child))
                                .chars()
                                .take(MAX_SOURCE_REF_VALUE_CHARS)
                                .collect(),
                        )
                    })
                    .collect();
                if !cleaned.is_empty() {
                    result.insert(safe_key, Value::Array(cleaned));
                }
            }
            Value::Null | Value::Bool(_) | Value::Number(_) => {
                result.insert(safe_key, item.clone());
            }
            Value::String(_) => {
                let text: String = python_str(Some(item))
                    .chars()
                    .take(MAX_SOURCE_REF_VALUE_CHARS)
                    .collect();
                result.insert(safe_key, Value::String(text));
            }
        }
    }
    Value::Object(result)
}

/// `normalize_saved_type` — an unrecognised type **raises** rather than defaulting.
pub fn normalize_saved_type(value: Option<&Value>) -> Result<String, AppError> {
    let item_type = python_str(value).trim().to_lowercase();
    if !SAVED_ITEM_TYPES.contains(&item_type.as_str()) {
        return Err(AppError::invalid_payload("Unsupported saved item type"));
    }
    Ok(item_type)
}

/// `normalize_saved_purpose` — an unrecognised purpose degrades to `reference`.
pub fn normalize_saved_purpose(value: Option<&Value>) -> String {
    let purpose = python_str(value).trim().to_lowercase();
    if SAVED_ITEM_PURPOSES.contains(&purpose.as_str()) {
        purpose
    } else {
        "reference".to_string()
    }
}

/// `normalize_artifact_type`, including the `.md` → `markdown` alias and the
/// **path-suffix fallback** when the type is absent.
///
/// The suffix fallback is the part worth stating: `normalize_artifact_type("", path=
/// "a/b.md")` is `markdown`, while `normalize_artifact_type("")` with no path raises.
pub fn normalize_artifact_type(value: Option<&Value>, path: &str) -> Result<String, AppError> {
    let raw = python_str(value).trim().to_lowercase();
    let mut artifact_type = raw.trim_start_matches('.').to_string();
    if artifact_type == "md" {
        artifact_type = "markdown".to_string();
    }
    if artifact_type.is_empty() && !path.is_empty() {
        let suffix = Path::new(path)
            .extension()
            .map(|extension| extension.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        artifact_type = if suffix == "md" {
            "markdown".to_string()
        } else {
            suffix
        };
    }
    if !ARTIFACT_TYPES.contains(&artifact_type.as_str()) {
        return Err(AppError::invalid_payload("Unsupported artifact type"));
    }
    Ok(artifact_type)
}

/// `normalize_export_format` — an unrecognised format raises.
pub fn normalize_export_format(value: Option<&Value>) -> Result<String, AppError> {
    let raw = match value {
        None => "zip".to_string(),
        Some(Value::Null) => "zip".to_string(),
        Some(other) => python_str(Some(other)),
    };
    let mut export_format = raw.trim().to_lowercase();
    export_format = export_format.trim_start_matches('.').to_string();
    if export_format.is_empty() {
        export_format = "zip".to_string();
    }
    if export_format == "md" {
        export_format = "markdown".to_string();
    }
    if !EXPORT_FORMATS.contains(&export_format.as_str()) {
        return Err(AppError::invalid_payload("Unsupported export format"));
    }
    Ok(export_format)
}

// --- file helpers -----------------------------------------------------------------

/// `read_json_file`: a missing, unreadable, malformed or non-object file all yield
/// the default — never an error.
pub fn read_json_file(path: &Path, default: Value) -> Value {
    let fallback = || {
        if default.is_object() {
            default.clone()
        } else {
            json!({})
        }
    };
    let Ok(raw) = std::fs::read_to_string(path) else {
        return fallback();
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return fallback();
    };
    if value.is_object() { value } else { fallback() }
}

/// `write_json_atomic`: a fenced write with an `indent=2` body **and a trailing
/// newline**, through a `<name>.tmp` sibling.
///
/// The trailing newline is the oracle's (`json.dumps(...) + "\n"`), and the temp name
/// is `with_suffix(suffix + ".tmp")` — which *appends* here, so `project.json`
/// becomes `project.json.tmp`, unlike the reminders store's replaced suffix.
pub fn write_json_atomic(root: &Path, path: &Path, payload: &Value) -> Result<(), AppError> {
    let _scope = crate::mutation_gate::mutation_scope(None, root).map_err(|error| AppError {
        message: error.message,
        code: codes::INVALID_REQUEST,
        status: error.status.unwrap_or(500),
    })?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    }
    let mut rendered =
        crate::python_json::OrderedJson::from_value_with_order(payload, &[]).render_indent_2();
    rendered.push('\n');
    let mut temp = path.as_os_str().to_os_string();
    temp.push(".tmp");
    let temp = PathBuf::from(temp);
    std::fs::write(&temp, rendered.as_bytes())
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    std::fs::rename(&temp, path).map_err(|error| AppError::invalid_payload(error.to_string()))?;
    Ok(())
}

/// `safe_filename`: non-word runs become `-`, then trim `.`/`-`, cap at 80.
///
/// `\w` is Unicode-aware on both sides, so CJK survives.
pub fn safe_filename(value: &str, default: &str) -> String {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"[^\w.-]+").expect("safe filename pattern"));
    let name = re.replace_all(value, "-").to_string();
    let name = name.trim_matches(|c| c == '.' || c == '-');
    let capped: String = name.chars().take(80).collect();
    if capped.is_empty() {
        default.to_string()
    } else {
        capped
    }
}

/// `runtime_relative_path` — an absolute path is rebased onto a runtime root; a
/// relative one is normalised to POSIX and refused if it escapes.
///
/// `roots` is `(directory, name)` in the oracle's precedence order:
/// `GENERATED_DIR`, `PROJECTS_DIR`, `ROOT`. `ROOT` is the special case — it yields the
/// path *without* a leading directory name.
pub fn runtime_relative_path(
    value: &str,
    generated: &Path,
    projects: &Path,
    root: &Path,
) -> Result<String, AppError> {
    let raw = value.trim();
    if raw.is_empty() {
        return Err(AppError::invalid_payload("Artifact path is required"));
    }
    let candidate = Path::new(raw);
    if candidate.is_absolute() {
        // The oracle resolves symlinks (`candidate.resolve()`), so a path that only
        // *looks* contained through a symlink is judged on its target.
        let resolved = candidate
            .canonicalize()
            .unwrap_or_else(|_| candidate.to_path_buf());
        for (base, include_name) in [(generated, true), (projects, true), (root, false)] {
            let base_resolved = base.canonicalize().unwrap_or_else(|_| base.to_path_buf());
            if let Ok(relative) = resolved.strip_prefix(&base_resolved) {
                let relative = relative.to_string_lossy().replace('\\', "/");
                if include_name {
                    let name = base
                        .file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .unwrap_or_default();
                    return Ok(format!("{name}/{relative}"));
                }
                return Ok(relative);
            }
        }
        return Err(AppError::invalid_payload(
            "Artifact path must stay inside the workspace runtime root",
        ));
    }
    let normalized = raw.replace('\\', "/");
    // `PurePosixPath(...).parts` keeps the **root marker** as its first element, so a
    // leading `/` survives the `{"", "."}` filter and `as_posix()` re-emits it. This
    // is reachable on Windows, where `/etc/passwd` is *not* absolute (no drive
    // letter), so it takes this branch and the oracle returns `"/etc/passwd"`.
    // Dropping the marker — the obvious reading — silently changes the answer.
    let mut parts: Vec<String> = Vec::new();
    let mut rest = normalized.as_str();
    if let Some(after_two) = rest.strip_prefix("//") {
        if after_two.starts_with('/') {
            // Three or more slashes collapse to a single root.
            parts.push("/".to_string());
            rest = after_two.trim_start_matches('/');
        } else {
            // Exactly two leading slashes are their own root, per POSIX.
            parts.push("//".to_string());
            rest = after_two;
        }
    } else if let Some(after_one) = rest.strip_prefix('/') {
        parts.push("/".to_string());
        rest = after_one;
    }
    for part in rest.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        parts.push(part.to_string());
    }
    // `..` is **kept** by `parts` and then caught by the explicit check below, so both
    // a leading `..` and a nested one are refused.
    if parts.is_empty() || parts.iter().any(|part| part == "..") {
        return Err(AppError::invalid_payload(
            "Artifact path must not escape the workspace",
        ));
    }
    // `PurePosixPath(*parts).as_posix()`.
    let rendered = match parts.first().map(String::as_str) {
        Some("/") => format!("/{}", parts[1..].join("/")),
        Some("//") => format!("//{}", parts[1..].join("/")),
        _ => parts.join("/"),
    };
    Ok(rendered)
}

/// `resolve_runtime_path` — the inverse of [`runtime_relative_path`].
pub fn resolve_runtime_path(
    value: &str,
    generated: &Path,
    projects: &Path,
    root: &Path,
) -> Result<PathBuf, AppError> {
    let rel = runtime_relative_path(value, generated, projects, root)?;
    let generated_name = generated
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    let projects_name = projects
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    if rel == generated_name || rel.starts_with(&format!("{generated_name}/")) {
        let tail = rel
            .strip_prefix(&generated_name)
            .unwrap_or("")
            .trim_start_matches('/');
        return Ok(generated.join(tail));
    }
    if rel == projects_name || rel.starts_with(&format!("{projects_name}/")) {
        let tail = rel
            .strip_prefix(&projects_name)
            .unwrap_or("")
            .trim_start_matches('/');
        return Ok(projects.join(tail));
    }
    Ok(root.join(rel))
}

// --- redaction --------------------------------------------------------------------

fn bearer_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}").expect("bearer pattern"))
}

fn secret_token_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"\b(sk-[a-zA-Z0-9][a-zA-Z0-9_-]{8,})\b").expect("secret token pattern")
    })
}

fn query_secret_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"(?i)([?&](?:token|api_key|apikey|auth_token|access_token|refresh_token)=)([^&\s"'<>]+)"#)
            .expect("query secret pattern")
    })
}

fn secret_value_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r#"(?i)\b(api[-_ ]?key|authorization|auth[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*([^,\s"'<>]{4,})"#,
        )
        .expect("secret value pattern")
    })
}

fn sensitive_key_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)(api[_-]?key|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|secret|cookie)$")
            .expect("sensitive key pattern")
    })
}

/// `redact_sensitive_text` — the four patterns, **in the oracle's order**.
///
/// The order matters: a bearer token is redacted before the generic
/// `key: value` rule runs, so `Authorization: Bearer abc…` becomes
/// `Authorization: Bearer [redacted]` rather than the whole value being replaced.
pub fn redact_sensitive_text(value: &str) -> String {
    let text = bearer_regex()
        .replace_all(value, "${1}[redacted]")
        .to_string();
    let text = secret_token_regex()
        .replace_all(&text, "[redacted-secret]")
        .to_string();
    let text = query_secret_regex()
        .replace_all(&text, "${1}[redacted]")
        .to_string();
    secret_value_regex()
        .replace_all(&text, |captures: &regex::Captures<'_>| {
            format!("{}=[redacted]", &captures[1])
        })
        .to_string()
}

/// `redact_value` — recursive, and a **key** match redacts the whole subtree.
pub fn redact_value(value: &Value, key: &str) -> Value {
    if !key.is_empty() && sensitive_key_regex().is_match(key) {
        return Value::String("[redacted]".to_string());
    }
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(item_key, item_value)| {
                    (item_key.clone(), redact_value(item_value, item_key))
                })
                .collect(),
        ),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| redact_value(item, key)).collect())
        }
        Value::String(text) => Value::String(redact_sensitive_text(text)),
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
    }
}

/// `contains_secret` — the gate a preview passes before it may be shown.
pub fn contains_secret(value: &[u8]) -> bool {
    let text = String::from_utf8_lossy(value);
    if secret_token_regex().is_match(&text) || bearer_regex().is_match(&text) {
        return true;
    }
    secret_value_regex().is_match(&text) || query_secret_regex().is_match(&text)
}

/// Python's `str(value)` for the fields this module reads.
///
/// `str(None)` is `"None"`, and the callers that want the empty string spell
/// `value or ""` themselves — so this must **not** collapse `null`.
pub fn python_str(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => crate::python_json::value_str(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::SystemEntropy;

    #[test]
    fn timestamp_ms_to_iso_truncates_and_uses_z() {
        assert_eq!(timestamp_ms_to_iso(Some(&json!(0))), "");
        assert_eq!(timestamp_ms_to_iso(Some(&json!(-1))), "");
        assert_eq!(timestamp_ms_to_iso(None), "");
        assert_eq!(timestamp_ms_to_iso(Some(&json!("abc"))), "");
        assert_eq!(
            timestamp_ms_to_iso(Some(&json!(1_789_815_737_000_i64))),
            "2026-09-19T11:02:17Z"
        );
        // Sub-second milliseconds truncate rather than rounding.
        assert_eq!(
            timestamp_ms_to_iso(Some(&json!(1_500))),
            "1970-01-01T00:00:01Z"
        );
        assert_eq!(
            timestamp_ms_to_iso(Some(&json!("1789815737000"))),
            "2026-09-19T11:02:17Z"
        );
    }

    #[test]
    fn new_id_sanitizes_the_prefix_and_falls_back_to_item() {
        let id = new_id("save", &SystemEntropy).unwrap();
        assert!(id.starts_with("save_"), "id: {id}");
        assert_eq!(id.len(), "save_".len() + 16);
        // A prefix that sanitises to nothing becomes `item`.
        let id = new_id("!!!", &SystemEntropy).unwrap();
        assert!(id.starts_with("item_"), "id: {id}");
        // Case is lowered and separators stripped.
        let id = new_id("My-ID", &SystemEntropy).unwrap();
        assert!(id.starts_with("myid_"), "id: {id}");
    }

    #[test]
    fn validate_ids_match_the_anchored_patterns() {
        assert!(validate_project_id("proj-abc123").is_ok());
        assert!(validate_project_id("abc").is_err(), "3 chars is too short");
        assert!(validate_project_id("").is_err());
        assert!(validate_project_id("has space").is_err());
        assert!(validate_project_id("a".repeat(64).as_str()).is_ok());
        assert!(validate_project_id("a".repeat(65).as_str()).is_err());

        assert!(validate_workspace_id("save_abc", "saved item id").is_ok());
        assert!(validate_workspace_id("abc", "saved item id").is_err());
        let error = validate_workspace_id("no!", "artifact id").unwrap_err();
        assert_eq!(error.message, "Invalid artifact id");
    }

    #[test]
    fn normalize_title_collapses_and_defaults() {
        assert_eq!(normalize_title(Some(&json!("  a   b  ")), "X"), "a b");
        assert_eq!(normalize_title(Some(&json!("")), "X"), "X");
        assert_eq!(normalize_title(None, "X"), "X");
        assert_eq!(normalize_title(Some(&json!("   ")), "X"), "X");
        // The cap applies *before* the default check, so a long title survives.
        let long = normalize_title(Some(&json!("z".repeat(200))), "X");
        assert_eq!(long.chars().count(), MAX_TITLE_CHARS);
    }

    #[test]
    fn normalize_content_keeps_newlines_but_normalize_title_does_not() {
        assert_eq!(
            normalize_content(Some(&json!("  a\r\nb  "))),
            "a\nb",
            "CRLF becomes LF and the newline survives"
        );
        assert_eq!(
            normalize_title(Some(&json!("a\r\nb")), "X"),
            "a b",
            "whitespace collapses in a title"
        );
        assert_eq!(normalize_description(Some(&json!("  a\r\nb  "))), "a\nb");
    }

    #[test]
    fn normalize_tags_dedupes_case_insensitively_keeping_the_first_spelling() {
        let tags = normalize_tags(Some(&json!(["Rust", "rust", " RUST ", "Go"])));
        assert_eq!(tags, vec!["Rust".to_string(), "Go".to_string()]);
        // A non-list is empty rather than an error.
        assert!(normalize_tags(Some(&json!("rust"))).is_empty());
        assert!(normalize_tags(None).is_empty());
        // An empty tag is skipped.
        assert_eq!(normalize_tags(Some(&json!(["", "  ", "ok"]))), vec!["ok"]);
        // The cap is 24.
        let many: Vec<String> = (0..40).map(|index| format!("t{index}")).collect();
        assert_eq!(normalize_tags(Some(&json!(many))).len(), MAX_TAGS);
    }

    #[test]
    fn normalize_source_ref_is_bounded_and_drops_emptied_nesting() {
        let normalised = normalize_source_ref(&json!({
            "bad key!": "x",
            "nested": {"ok": 1, "drop me!": 2},
            "emptyNested": {"!!!": 1},
            "list": [1, null, "two"],
            "emptyList": [null],
            "nothing": null,
        }));
        assert_eq!(normalised["badkey"], "x");
        assert_eq!(normalised["nested"]["dropme"], 2);
        assert!(normalised.get("emptyNested").is_none());
        assert_eq!(normalised["list"], json!(["1", "two"]));
        assert!(normalised.get("emptyList").is_none());
        assert_eq!(normalised["nothing"], Value::Null);
        assert_eq!(normalize_source_ref(&json!("x")), json!({}));
    }

    #[test]
    fn saved_type_raises_where_purpose_degrades() {
        assert_eq!(
            normalize_saved_type(Some(&json!("chat_snippet"))).unwrap(),
            "chat_snippet"
        );
        assert_eq!(
            normalize_saved_type(Some(&json!(" Chat_Snippet "))).unwrap(),
            "chat_snippet"
        );
        assert!(normalize_saved_type(Some(&json!("nonsense"))).is_err());
        assert!(normalize_saved_type(None).is_err());

        assert_eq!(
            normalize_saved_purpose(Some(&json!("export_fragment"))),
            "export_fragment"
        );
        assert_eq!(
            normalize_saved_purpose(Some(&json!("nonsense"))),
            "reference"
        );
        assert_eq!(normalize_saved_purpose(None), "reference");
    }

    #[test]
    fn artifact_type_aliases_and_path_fallback() {
        assert_eq!(
            normalize_artifact_type(Some(&json!("md")), "").unwrap(),
            "markdown"
        );
        assert_eq!(
            normalize_artifact_type(Some(&json!(".svg")), "").unwrap(),
            "svg"
        );
        // The path-suffix fallback only applies when the type is absent.
        assert_eq!(normalize_artifact_type(None, "a/b.md").unwrap(), "markdown");
        assert_eq!(normalize_artifact_type(None, "a/b.svg").unwrap(), "svg");
        // No type and no path raises.
        assert!(normalize_artifact_type(None, "").is_err());
        assert!(normalize_artifact_type(Some(&json!("nonsense")), "a.md").is_err());
    }

    #[test]
    fn export_format_defaults_to_zip_and_raises_otherwise() {
        assert_eq!(normalize_export_format(None).unwrap(), "zip");
        assert_eq!(normalize_export_format(Some(&json!(""))).unwrap(), "zip");
        assert_eq!(
            normalize_export_format(Some(&json!("md"))).unwrap(),
            "markdown"
        );
        assert_eq!(
            normalize_export_format(Some(&json!(".JSON"))).unwrap(),
            "json"
        );
        assert!(normalize_export_format(Some(&json!("nonsense"))).is_err());
    }

    #[test]
    fn runtime_relative_path_refuses_escapes_and_rebases_absolute_paths() {
        let root = Path::new("/srv/ws");
        let generated = Path::new("/srv/ws/.generated");
        let projects = Path::new("/srv/ws/.projects");

        assert_eq!(
            runtime_relative_path("a/b.md", generated, projects, root).unwrap(),
            "a/b.md"
        );
        // A relative escape is refused, including a nested one.
        assert!(runtime_relative_path("../etc/passwd", generated, projects, root).is_err());
        assert!(runtime_relative_path("a/../../etc", generated, projects, root).is_err());
        assert!(runtime_relative_path("", generated, projects, root).is_err());
        assert!(runtime_relative_path(".", generated, projects, root).is_err());
        // Backslashes normalise to POSIX.
        assert_eq!(
            runtime_relative_path("a\\b.md", generated, projects, root).unwrap(),
            "a/b.md"
        );

        // **A platform split, measured on both sides.** `/etc/passwd` is absolute on
        // POSIX and *not* absolute on Windows (no drive letter), so Python's
        // `Path("/etc/passwd").is_absolute()` is `False` there and the oracle returns
        // `"/etc/passwd"` — **with** the leading slash, because `PurePosixPath.parts`
        // keeps the root marker. Rust's `Path::is_absolute()` draws the same line, so
        // the port agrees; asserting one answer here would encode a single platform.
        let unix_style = runtime_relative_path("/etc/passwd", generated, projects, root).unwrap();
        if cfg!(windows) {
            assert_eq!(unix_style, "/etc/passwd", "the root marker survives");
        } else {
            // On POSIX it *is* absolute, and outside every root, so it is refused.
            assert!(runtime_relative_path("/etc/passwd", generated, projects, root).is_err());
        }

        // A genuinely absolute path outside every root is refused on **both**
        // platforms, which is the security property that matters.
        let outside = if cfg!(windows) {
            "C:\\Windows\\System32\\drivers\\etc\\hosts"
        } else {
            "/etc/hosts"
        };
        let error = runtime_relative_path(outside, generated, projects, root).unwrap_err();
        assert_eq!(
            error.message,
            "Artifact path must stay inside the workspace runtime root"
        );

        // The root marker's exact spelling: one slash, and exactly two is its own root.
        assert_eq!(
            runtime_relative_path("//a", generated, projects, root).unwrap(),
            "//a"
        );
        assert_eq!(
            runtime_relative_path("///a", generated, projects, root).unwrap(),
            "/a"
        );
        // `a/../b` is refused rather than collapsed.
        assert!(runtime_relative_path("a/../b", generated, projects, root).is_err());
    }

    #[test]
    fn resolve_runtime_path_reprefixes_the_directory_name() {
        let root = Path::new("/srv/ws");
        let generated = Path::new("/srv/ws/.generated");
        let projects = Path::new("/srv/ws/.projects");
        assert_eq!(
            resolve_runtime_path(".generated/a.svg", generated, projects, root).unwrap(),
            PathBuf::from("/srv/ws/.generated/a.svg")
        );
        assert_eq!(
            resolve_runtime_path(".projects/p1/x.json", generated, projects, root).unwrap(),
            PathBuf::from("/srv/ws/.projects/p1/x.json")
        );
        assert_eq!(
            resolve_runtime_path("other/x", generated, projects, root).unwrap(),
            PathBuf::from("/srv/ws/other/x")
        );
    }

    #[test]
    fn safe_filename_keeps_cjk_and_caps_at_80() {
        assert_eq!(safe_filename("my file!.txt", "item"), "my-file-.txt");
        assert_eq!(safe_filename("...", "item"), "item");
        assert_eq!(safe_filename("", "item"), "item");
        assert_eq!(safe_filename("中文文档", "item"), "中文文档");
        assert_eq!(safe_filename(&"a".repeat(200), "item").chars().count(), 80);
    }

    #[test]
    fn redaction_covers_the_four_patterns_in_order() {
        // **Measured**, not assumed. The bearer pattern runs first but the generic
        // `key: value` pattern runs *last* and then re-matches the already-redacted
        // text: `Authorization: Bearer [redacted]` still contains `Authorization:`
        // followed by a 4+ character value, so it becomes `Authorization=[redacted]
        // [redacted]`. That looks wrong and is the oracle's actual output.
        assert_eq!(
            redact_sensitive_text("Authorization: Bearer abcdefghijklmnop"),
            "Authorization=[redacted] [redacted]"
        );
        assert_eq!(
            redact_sensitive_text("key sk-abcdefghijklmno here"),
            "key [redacted-secret] here"
        );
        // The trailing `&x=1` is consumed by the last pattern, whose value class
        // excludes `,`, whitespace and quotes but **not** `&`. Measured.
        assert_eq!(
            redact_sensitive_text("see ?api_key=supersecret&x=1"),
            "see ?api_key=[redacted]"
        );
        assert_eq!(
            redact_sensitive_text("api_key=supersecret"),
            "api_key=[redacted]"
        );
        // A short value is below the `{4,}` floor and is left alone.
        assert_eq!(redact_sensitive_text("password=abc"), "password=abc");
    }

    #[test]
    fn redact_value_redacts_a_sensitive_key_subtree() {
        let redacted = redact_value(
            &json!({"apiKey": {"nested": "secret"}, "name": "ok", "list": ["sk-abcdefghijklmno"]}),
            "",
        );
        assert_eq!(redacted["apiKey"], "[redacted]");
        assert_eq!(redacted["name"], "ok");
        assert_eq!(redacted["list"][0], "[redacted-secret]");
    }

    #[test]
    fn contains_secret_is_the_preview_gate() {
        assert!(contains_secret(b"sk-abcdefghijklmno"));
        assert!(contains_secret(b"Bearer abcdefghijklmnop"));
        assert!(contains_secret(b"password=supersecret"));
        assert!(contains_secret(b"?token=abcdefgh"));
        assert!(!contains_secret(b"just a normal document"));
        // Invalid UTF-8 is decoded lossily rather than panicking.
        assert!(!contains_secret(&[0xff, 0xfe, 0x00]));
    }

    #[test]
    fn read_json_file_degrades_to_the_default() {
        let root = std::env::temp_dir().join(format!("ws-schema-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let missing = root.join("nope.json");
        assert_eq!(
            read_json_file(&missing, json!({"items": []})),
            json!({"items": []})
        );

        let malformed = root.join("bad.json");
        std::fs::write(&malformed, "not json").unwrap();
        assert_eq!(
            read_json_file(&malformed, json!({"items": []})),
            json!({"items": []})
        );

        let scalar = root.join("scalar.json");
        std::fs::write(&scalar, "[]").unwrap();
        assert_eq!(
            read_json_file(&scalar, json!({"items": []})),
            json!({"items": []})
        );

        let good = root.join("good.json");
        std::fs::write(&good, r#"{"items":[1]}"#).unwrap();
        assert_eq!(read_json_file(&good, json!({})), json!({"items": [1]}));

        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn write_json_atomic_uses_the_appended_tmp_suffix_and_a_trailing_newline() {
        let root = std::env::temp_dir().join(format!("ws-schema-write-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let target = root.join("project.json");
        write_json_atomic(&root, &target, &json!({"id": "p1"})).unwrap();
        let text = std::fs::read_to_string(&target).unwrap();
        // The suffix is *appended*, so the temp file is `project.json.tmp`.
        assert!(text.ends_with("}\n"), "trailing newline: {text:?}");
        assert!(
            !root.join("project.json.tmp").exists(),
            "temp must be renamed away"
        );
        assert_eq!(text, "{\n  \"id\": \"p1\"\n}\n");

        // The write is fenced: one scope bumps the generation twice.
        let generation = std::fs::read_to_string(root.join(".workspace-generation")).unwrap();
        assert_eq!(generation.trim(), "2");

        let _ = std::fs::remove_dir_all(&root);
    }
}
