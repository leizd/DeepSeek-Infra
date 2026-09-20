//! The projects store, mirroring the read path of
//! `deepseek_infra/infra/data/projects.py`.
//!
//! Storage is a directory per project at `<root>/.projects/<id>/project.json`,
//! holding a project record whose six collection fields are re-normalised on every
//! read. That is why this module is much larger than the two branches it exists to
//! serve: `read_project` normalises `documents`, `skills`, `skillRuns`,
//! `savedItems` and `artifacts` before returning, so the normaliser family is on the
//! critical path even for a branch that only looks at `documents`.
//!
//! # The read path mints random ids
//!
//! `normalize_skill_run` and `normalize_saved_items` generate a fresh id when a
//! stored entry has none:
//!
//! ```python
//! "skillRunId": str(item.get("skillRunId") or item.get("runId") or f"run-{secrets.token_hex(8)}")
//! ```
//!
//! So reading the same malformed project twice yields **different** values. It is not
//! persisted — `read_project` never writes back — but it is observable through
//! [`public_project`], which is what `list_projects` returns. Measured directly:
//!
//! ```text
//! read 1 -> run-d9d3e527ae4f29df
//! read 2 -> run-5acb6344a2c0e2bf
//! ```
//!
//! This port keeps the behaviour rather than removing it — the ids belong to a
//! schema the oracle owns — and takes the source through [`Entropy`], so the parity
//! probe can pin it and see a stable value.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::app_error::AppError;
use crate::entropy::Entropy;
use crate::file_cache::{FileCache, int_field, load_cached_file, python_int};

/// `MAX_PROJECTS`.
pub const MAX_PROJECTS: usize = 40;
/// `MAX_PROJECT_DOCUMENTS`.
pub const MAX_PROJECT_DOCUMENTS: usize = 120;
/// `MAX_PROJECT_SKILL_RUNS`.
pub const MAX_PROJECT_SKILL_RUNS: usize = 200;
/// `MAX_PROJECT_SAVED_ITEMS`.
pub const MAX_PROJECT_SAVED_ITEMS: usize = 200;
/// `MAX_PROJECT_ARTIFACTS`.
pub const MAX_PROJECT_ARTIFACTS: usize = 200;

pub fn projects_dir(root: &Path) -> PathBuf {
    root.join(".projects")
}

/// `<PROJECTS_DIR>/<id>/project.json`.
pub fn project_file(root: &Path, project_id: &str) -> PathBuf {
    projects_dir(root).join(project_id).join("project.json")
}

// --- validation ------------------------------------------------------------------

/// Mirrors `validate_project_id`: `[a-zA-Z0-9_-]{4,64}` or a 400.
pub fn validate_project_id(project_id: &str) -> Result<String, AppError> {
    let safe_id = project_id.trim();
    let valid = (4..=64).contains(&safe_id.chars().count())
        && safe_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'));
    if !valid {
        return Err(AppError::invalid_payload("Invalid project id"));
    }
    Ok(safe_id.to_string())
}

/// Mirrors `normalize_skill_id_for_project` / `normalize_pack_id_for_project`:
/// `[A-Za-z0-9_:-]{3,80}`, else empty.
fn normalize_scoped_id(value: Option<&Value>) -> String {
    let text = python_str(value).trim().to_string();
    let valid = (3..=80).contains(&text.chars().count())
        && text
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | ':' | '-'));
    if valid { text } else { String::new() }
}

/// `normalize_pack_id_for_project` unwraps a `{"packId": ...}` mapping first.
fn normalize_pack_id(value: Option<&Value>) -> String {
    match value {
        Some(Value::Object(fields)) => normalize_scoped_id(fields.get("packId")),
        other => normalize_scoped_id(other),
    }
}

// --- string helpers --------------------------------------------------------------

/// `str(value or "")`.
fn python_str(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        _ => String::new(),
    }
}

/// `unique_strings`: trimmed, non-empty, de-duplicated, order-preserving.
///
/// The oracle iterates `values if isinstance(values, list) else list(values)`, so a
/// **string** argument yields its characters and a mapping yields its keys. A scalar
/// would raise `TypeError`; that is reproduced as an error rather than silently
/// treated as empty.
fn unique_strings(value: Option<&Value>) -> Result<Vec<String>, AppError> {
    let mut result: Vec<String> = Vec::new();
    let mut push = |item: String| {
        let text = item.trim().to_string();
        if !text.is_empty() && !result.contains(&text) {
            result.push(text);
        }
    };
    match value {
        // `list(None)` raises `TypeError` in the oracle, so `None` is not "empty".
        // Unreachable from the ported branches, which guard with `or []`, but the
        // shape is reproduced rather than quietly swallowed.
        None | Some(Value::Null) => return Err(not_iterable("NoneType")),
        Some(Value::Array(items)) => {
            for item in items {
                push(python_str(Some(item)));
            }
        }
        Some(Value::String(text)) => {
            for character in text.chars() {
                push(character.to_string());
            }
        }
        Some(Value::Object(fields)) => {
            for key in fields.keys() {
                push(key.clone());
            }
        }
        // `list(5)` raises `TypeError` in the oracle.
        Some(other) => return Err(not_iterable(type_name(other))),
    }
    Ok(result)
}

/// Python's `TypeError` message for a non-iterable.
///
/// The oracle raises a bare `TypeError` here, with no `AppError` code. This port's
/// error type is `AppError`, so the code it reports is `invalid_payload` — a
/// documented mapping, not an invented code pretending to be the oracle's. The
/// message matches exactly.
fn not_iterable(name: &str) -> AppError {
    AppError::invalid_payload(format!("'{name}' object is not iterable"))
}

fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(number) => {
            if number.is_i64() || number.is_u64() {
                "int"
            } else {
                "float"
            }
        }
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Mirrors `_safe_int`, including `int(str(value))`'s rejection of `"12.7"`.
///
/// Python's `int()` accepts surrounding whitespace, a sign, and single underscores
/// between digits; anything else — a float's string form, `"True"` — falls back.
fn safe_int(value: Option<&Value>, default: i64) -> i64 {
    let Some(value) = value else {
        return default.max(0);
    };
    if value.is_null() {
        return default.max(0);
    }
    let text = python_str(Some(value));
    let trimmed = text.trim();
    let (sign, digits) = match trimmed.strip_prefix('-') {
        Some(rest) => (-1i64, rest),
        None => (1i64, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let normalised = digits.replace('_', "");
    let parseable = !normalised.is_empty()
        && normalised.chars().all(|c| c.is_ascii_digit())
        && !digits.starts_with('_')
        && !digits.ends_with('_');
    if !parseable {
        return default.max(0);
    }
    match normalised.parse::<i64>() {
        Ok(parsed) => (sign * parsed).max(0),
        Err(_) => default.max(0),
    }
}

/// Python's `[:n]` on a string — code points, not bytes.
fn truncate_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

// --- normalisers -----------------------------------------------------------------

/// Mirrors `normalize_project_name`.
pub fn normalize_project_name(value: Option<&Value>) -> String {
    let raw = python_str(value).replace('\n', " ");
    let trimmed = raw.trim();
    let truncated = truncate_chars(trimmed, 60);
    if truncated.is_empty() {
        "新项目".to_string()
    } else {
        truncated
    }
}

/// Mirrors `normalize_documents`. A document without a 32-hex `fileId` and a
/// non-empty `projectId` is **dropped**, not repaired.
pub fn normalize_documents(value: Option<&Value>) -> Vec<Value> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut documents = Vec::new();
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let file_id = python_str(object.get("fileId"));
        let project_id = python_str(object.get("projectId"));
        if !is_file_id(&file_id) || project_id.is_empty() {
            continue;
        }
        documents.push(json!({
            "id": if python_str(object.get("id")).is_empty() { file_id.clone() } else { python_str(object.get("id")) },
            "name": truncate_chars(&python_str(object.get("name")).if_empty("文件"), 180),
            "type": python_str(object.get("type")),
            "size": safe_int(object.get("size"), 0),
            "kind": python_str(object.get("kind")).if_empty("text"),
            "fileId": file_id,
            "projectId": project_id,
            "sourceAvailable": truthy(object.get("sourceAvailable")),
            "preview": truncate_chars(&python_str(object.get("preview")), 1800),
            "pageCount": safe_int(object.get("pageCount"), 0),
            "charCount": safe_int(object.get("charCount"), 0),
            "chunkCount": safe_int(object.get("chunkCount"), 0),
            "chunked": truthy(object.get("chunked")),
            "createdAt": safe_int(object.get("createdAt"), 0),
        }));
    }
    documents
}

/// Python's `x or []` — the guard every `unique_strings` call site uses.
///
/// Without it a missing field would reach `list(None)` and raise, which is what the
/// oracle's `or []` exists to prevent. The direct-call raise is still reproduced by
/// [`unique_strings`] itself.
fn or_empty(value: Option<&Value>) -> Value {
    match value {
        Some(item) if truthy(Some(item)) => item.clone(),
        _ => Value::Array(Vec::new()),
    }
}

/// A small `str` extension for the oracle's `str(x or fallback)` idiom.
trait IfEmpty {
    fn if_empty(self, fallback: &str) -> String;
}

impl IfEmpty for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() {
            fallback.to_string()
        } else {
            self
        }
    }
}

fn is_file_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

/// Python truthiness, shared with the file-cache read path.
pub fn is_truthy(value: &Value) -> bool {
    crate::core_utils::python_truthy(value)
}

/// Python truthiness for an optional value.
fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(crate::core_utils::python_truthy)
}

/// `_safe_int`, exposed because the parity probe compares it directly.
pub fn safe_int_probe(value: Option<&Value>, default: i64) -> i64 {
    safe_int(value, default)
}

/// `unique_strings`, exposed because the parity probe compares it directly —
/// including its `TypeError`-shaped rejection of a scalar.
pub fn unique_strings_probe(value: Option<&Value>) -> Result<Vec<String>, AppError> {
    unique_strings(value)
}

/// Mirrors `normalize_project_pack_versions`: a mapping row or a bare id, de-duped
/// by `packId`, keeping the first occurrence.
pub fn normalize_project_pack_versions(value: Option<&Value>) -> Vec<Value> {
    let rows: Vec<Value> = match value {
        Some(Value::Array(items)) => items.clone(),
        _ => Vec::new(),
    };
    let mut result: Vec<(String, String, String)> = Vec::new();
    for item in &rows {
        let (pack_id, version, installed_at) = match item.as_object() {
            Some(object) => (
                normalize_pack_id(object.get("packId")),
                truncate_chars(python_str(object.get("version")).trim(), 40),
                truncate_chars(
                    python_str(
                        object
                            .get("installedAt")
                            .or_else(|| object.get("updatedAt")),
                    )
                    .trim(),
                    80,
                ),
            ),
            None => (normalize_pack_id(Some(item)), String::new(), String::new()),
        };
        if pack_id.is_empty() || result.iter().any(|(existing, _, _)| *existing == pack_id) {
            continue;
        }
        result.push((pack_id, version, installed_at));
    }
    result
        .into_iter()
        .map(|(pack_id, version, installed_at)| {
            json!({"packId": pack_id, "version": version, "installedAt": installed_at})
        })
        .collect()
}

/// Mirrors `normalize_project_skills`.
pub fn normalize_project_skills(value: Option<&Value>) -> Result<Value, AppError> {
    let empty = Map::new();
    let data = match value {
        Some(Value::Object(fields)) => fields,
        _ => &empty,
    };
    let raw_enabled_packs = data.get("enabledPacks").cloned().unwrap_or(Value::Null);
    let enabled_packs: Vec<Value> = match &raw_enabled_packs {
        Value::Null => Vec::new(),
        Value::Array(items) => items.clone(),
        // `for item in <truthy scalar>` raises `TypeError` in the oracle.
        other => return Err(not_iterable(type_name(other))),
    };

    let raw_pack_versions = data.get("enabledPackVersions");
    let pack_versions = normalize_project_pack_versions(Some(
        raw_pack_versions.unwrap_or(&Value::Array(enabled_packs.clone())),
    ));

    let mut explicit: Vec<String> = Vec::new();
    for item in &enabled_packs {
        let id = normalize_pack_id(Some(item));
        if !id.is_empty() && !explicit.contains(&id) {
            explicit.push(id);
        }
    }
    let mut packs = explicit;
    for row in &pack_versions {
        let id = python_str(row.get("packId"));
        if !id.is_empty() && !packs.contains(&id) {
            packs.push(id);
        }
    }

    let mut enabled: Vec<String> = Vec::new();
    let enabled_source = or_empty(data.get("enabledSkills"));
    for item in unique_strings(Some(&enabled_source))? {
        push_skill(&mut enabled, item);
    }
    let mut recent: Vec<String> = Vec::new();
    let recent_source = or_empty(data.get("recentSkills"));
    for item in unique_strings(Some(&recent_source))? {
        push_skill(&mut recent, item);
    }
    let default = normalize_scoped_id(data.get("defaultSkill"));

    packs.truncate(40);
    let pack_versions: Vec<Value> = pack_versions.into_iter().take(40).collect();
    enabled.truncate(40);
    recent.truncate(20);
    Ok(json!({
        "enabledPacks": packs,
        "enabledPackVersions": pack_versions,
        "enabledSkills": enabled,
        "defaultSkill": if enabled.contains(&default) { default } else { String::new() },
        "recentSkills": recent,
    }))
}

/// `unique_strings(normalize_skill_id_for_project(item) for item in ...)` — the
/// normalisation happens *before* the de-duplication, so empties are dropped first.
fn push_skill(target: &mut Vec<String>, item: String) {
    let id = normalize_scoped_id(Some(&Value::String(item)));
    if !id.is_empty() && !target.contains(&id) {
        target.push(id);
    }
}

/// Mirrors `normalize_skill_runs`.
pub fn normalize_skill_runs(
    value: Option<&Value>,
    entropy: &dyn Entropy,
) -> Result<Vec<Value>, AppError> {
    let Some(Value::Array(items)) = value else {
        return Ok(Vec::new());
    };
    let mut runs = Vec::new();
    for item in items {
        if let Some(object) = item.as_object() {
            runs.push(normalize_skill_run(object, entropy)?);
        }
    }
    runs.truncate(MAX_PROJECT_SKILL_RUNS);
    Ok(runs)
}

/// Mirrors `normalize_skill_run` — thirty fields, and a **generated id** when the
/// stored run has neither `skillRunId` nor `runId`.
pub fn normalize_skill_run(
    item: &Map<String, Value>,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let mut artifact_ids: Vec<String> = Vec::new();
    let artifact_source = or_empty(item.get("artifactIds"));
    for value in unique_strings(Some(&artifact_source))? {
        if artifact_ids.len() < 40 {
            artifact_ids.push(value);
        }
    }
    let mut saved_item_ids: Vec<String> = Vec::new();
    let saved_source = or_empty(item.get("savedItemIds"));
    for value in unique_strings(Some(&saved_source))? {
        if saved_item_ids.len() < 40 {
            saved_item_ids.push(value);
        }
    }

    let stored_id = {
        let explicit = python_str(item.get("skillRunId"));
        if explicit.is_empty() {
            python_str(item.get("runId"))
        } else {
            explicit
        }
    };
    let skill_run_id = if stored_id.is_empty() {
        format!("run-{}", entropy.new_id()?)
    } else {
        stored_id
    };

    let input = match item.get("input") {
        Some(Value::Object(fields)) => Value::Object(fields.clone()),
        _ => json!({}),
    };

    Ok(json!({
        "skillRunId": truncate_chars(&skill_run_id, 80),
        "skillId": normalize_scoped_id(item.get("skillId")),
        "skillVersion": truncate_chars(&python_str(item.get("skillVersion")), 40),
        "packId": normalize_pack_id(item.get("packId")),
        "status": truncate_chars(&python_str(item.get("status")).if_empty("completed"), 40),
        "projectId": truncate_chars(&python_str(item.get("projectId")), 80),
        "input": input,
        "inputSummary": truncate_chars(&python_str(item.get("inputSummary")), 600),
        "outputSummary": truncate_chars(&python_str(item.get("outputSummary")), 1200),
        "artifactIds": artifact_ids,
        "savedItemIds": saved_item_ids,
        "artifactCount": safe_int(item.get("artifactCount"), artifact_ids_len(item)),
        "savedItemCount": safe_int(item.get("savedItemCount"), saved_item_ids_len(item)),
        "traceId": truncate_chars(&python_str(item.get("traceId")), 80),
        "startedAt": python_str(item.get("startedAt")),
        "completedAt": python_str(item.get("completedAt")),
        "latencyMs": safe_int(item.get("latencyMs"), 0),
        "offline": truthy(item.get("offline")),
        "model": truncate_chars(&python_str(item.get("model")), 120),
        "errorReason": truncate_chars(&python_str(item.get("errorReason")), 1200),
        "failureCategory": truncate_chars(&python_str(item.get("failureCategory")), 80),
        "diagnosticSuggestion": truncate_chars(&python_str(item.get("diagnosticSuggestion")), 240),
        "runSecurityLevel": truncate_chars(&python_str(item.get("runSecurityLevel")), 40),
        "securityReviewId": truncate_chars(&python_str(item.get("securityReviewId")), 120),
        "trustedAtRun": truthy(item.get("trustedAtRun")),
        "toolGrantHashAtRun": truncate_chars(&python_str(item.get("toolGrantHashAtRun")), 100),
        "blockedReason": truncate_chars(&python_str(item.get("blockedReason")), 500),
        "approvalRequired": truthy(item.get("approvalRequired")),
    }))
}

/// `_safe_int(item.get("artifactCount"), default=len(artifact_ids))`.
fn artifact_ids_len(item: &Map<String, Value>) -> i64 {
    let source = or_empty(item.get("artifactIds"));
    match unique_strings(Some(&source)) {
        Ok(values) => values.len().min(40) as i64,
        Err(_) => 0,
    }
}

fn saved_item_ids_len(item: &Map<String, Value>) -> i64 {
    let source = or_empty(item.get("savedItemIds"));
    match unique_strings(Some(&source)) {
        Ok(values) => values.len().min(40) as i64,
        Err(_) => 0,
    }
}

/// Mirrors `normalize_saved_items` — also a generated id when one is missing.
pub fn normalize_saved_items(
    value: Option<&Value>,
    entropy: &dyn Entropy,
) -> Result<Vec<Value>, AppError> {
    let Some(Value::Array(items)) = value else {
        return Ok(Vec::new());
    };
    let mut saved: Vec<Value> = Vec::new();
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let stored = python_str(object.get("id"));
        let id = if stored.is_empty() {
            format!("saved-{}", entropy.new_id()?)
        } else {
            stored
        };
        saved.push(json!({
            "id": truncate_chars(&id, 80),
            "title": truncate_chars(&python_str(object.get("title")).if_empty("Saved item"), 160),
            "kind": truncate_chars(&python_str(object.get("kind")).if_empty("note"), 80),
            "content": truncate_chars(&python_str(object.get("content")), 80_000),
            "source": match object.get("source") {
                Some(Value::Object(fields)) => Value::Object(fields.clone()),
                _ => json!({}),
            },
            "createdAt": safe_int(object.get("createdAt"), 0),
        }));
    }
    saved.truncate(MAX_PROJECT_SAVED_ITEMS);
    Ok(saved)
}

/// Mirrors `normalize_project_artifacts`.
pub fn normalize_project_artifacts(value: Option<&Value>) -> Vec<Value> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut artifacts: Vec<Value> = Vec::new();
    for item in items {
        if let Some(object) = item.as_object() {
            artifacts.push(normalize_project_artifact(object));
        }
    }
    artifacts.truncate(MAX_PROJECT_ARTIFACTS);
    artifacts
}

fn normalize_project_artifact(item: &Map<String, Value>) -> Value {
    json!({
        "artifactId": truncate_chars(&python_str(item.get("artifactId")), 80),
        "fileId": truncate_chars(&python_str(item.get("fileId")), 80),
        "filename": truncate_chars(&python_str(item.get("filename")), 180),
        "downloadUrl": truncate_chars(&python_str(item.get("downloadUrl")), 500),
        "type": truncate_chars(&python_str(item.get("type")), 40),
        "source": match item.get("source") {
            Some(Value::Object(fields)) => Value::Object(fields.clone()),
            _ => json!({}),
        },
        "createdAt": python_str(item.get("createdAt")),
    })
}

// --- the store -------------------------------------------------------------------

/// Mirrors `read_project`: `None` for a missing or malformed record, and every
/// collection field re-normalised on the way out.
pub fn read_project(
    project_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Option<Value>, AppError> {
    let safe_id = validate_project_id(project_id)?;
    let path = project_file(root, &safe_id);
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return Ok(None);
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Ok(None);
    };
    let Value::Object(mut fields) = value else {
        return Ok(None);
    };

    fields.insert("id".to_string(), Value::String(safe_id));
    fields.insert(
        "name".to_string(),
        Value::String(normalize_project_name(fields.get("name"))),
    );
    fields.insert(
        "documents".to_string(),
        Value::Array(normalize_documents(fields.get("documents"))),
    );
    fields.insert(
        "skills".to_string(),
        normalize_project_skills(fields.get("skills"))?,
    );
    fields.insert(
        "skillRuns".to_string(),
        Value::Array(normalize_skill_runs(fields.get("skillRuns"), entropy)?),
    );
    fields.insert(
        "savedItems".to_string(),
        Value::Array(normalize_saved_items(fields.get("savedItems"), entropy)?),
    );
    fields.insert(
        "artifacts".to_string(),
        Value::Array(normalize_project_artifacts(fields.get("artifacts"))),
    );
    Ok(Some(Value::Object(fields)))
}

/// Mirrors `require_project`.
pub fn require_project(
    project_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    read_project(project_id, root, entropy)?.ok_or_else(|| AppError::not_found("Project not found"))
}

/// Mirrors `list_projects`: directory names, `public_project`, sorted by
/// `updatedAt` descending.
pub fn list_projects(root: &Path, entropy: &dyn Entropy) -> Result<Vec<Value>, AppError> {
    let directory = projects_dir(root);
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut projects = Vec::new();
    let mut entries: Vec<PathBuf> = std::fs::read_dir(&directory)
        .map_err(|error| AppError::invalid_payload(error.to_string()))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    // `iterdir()` order is filesystem-dependent; sorting makes the port deterministic
    // where the oracle is not, and the final sort by `updatedAt` makes it irrelevant.
    entries.sort();
    for path in entries {
        let Some(name) = path
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
        else {
            continue;
        };
        // A directory whose name is not a valid project id is skipped, not an error.
        if validate_project_id(&name).is_err() {
            continue;
        }
        if let Some(project) = read_project(&name, root, entropy)? {
            projects.push(public_project(&project, entropy)?);
        }
    }
    projects.sort_by_key(|item| std::cmp::Reverse(safe_int(item.get("updatedAt"), 0)));
    Ok(projects)
}

/// Mirrors `public_project`, including the `[:20]` slices on the three large
/// collections.
pub fn public_project(project: &Value, entropy: &dyn Entropy) -> Result<Value, AppError> {
    let empty = Map::new();
    let fields = project.as_object().unwrap_or(&empty);
    let twenty =
        |values: Vec<Value>| -> Value { Value::Array(values.into_iter().take(20).collect()) };
    Ok(json!({
        "id": python_str(fields.get("id")),
        "name": normalize_project_name(fields.get("name")),
        "documents": normalize_documents(fields.get("documents")),
        "skills": normalize_project_skills(fields.get("skills"))?,
        "skillRuns": twenty(normalize_skill_runs(fields.get("skillRuns"), entropy)?),
        "savedItems": twenty(normalize_saved_items(fields.get("savedItems"), entropy)?),
        "artifacts": twenty(normalize_project_artifacts(fields.get("artifacts"))),
        "createdAt": safe_int(fields.get("createdAt"), 0),
        "updatedAt": safe_int(fields.get("updatedAt"), 0),
    }))
}

// --- the branches ----------------------------------------------------------------

/// Mirrors `project_document_for_tool`.
///
/// The `preview` cap is **500** here, not the store's 1800 — the tool sees less than
/// the record holds.
pub fn project_document_for_tool(document: &Value) -> Value {
    let empty = Map::new();
    let fields = document.as_object().unwrap_or(&empty);
    let count = |key: &str| int_field(document, key, 0).unwrap_or(0);
    json!({
        "name": python_str(fields.get("name")),
        "fileId": python_str(fields.get("fileId")),
        "projectId": python_str(fields.get("projectId")),
        "kind": python_str(fields.get("kind")).if_empty("text"),
        "pageCount": count("pageCount"),
        "charCount": count("charCount"),
        "chunkCount": count("chunkCount"),
        "preview": truncate_chars(&python_str(fields.get("preview")), 500),
    })
}

/// Mirrors `list_project_files_tool`.
///
/// Note the two different caps: at most `MAX_PROJECTS` projects, and within each at
/// most `MAX_PROJECT_DOCUMENTS` documents. `count` is the sum of the **emitted**
/// files, so it is the count after both caps.
pub fn list_project_files(
    arguments: &Map<String, Value>,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let safe_project_id = python_str(arguments.get("projectId")).trim().to_string();
    let projects: Vec<Value> = if safe_project_id.is_empty() {
        list_projects(root, entropy)?
    } else {
        match read_project(&safe_project_id, root, entropy)? {
            Some(project) => vec![project],
            // An invalid id already raised a 400 inside `read_project`.
            None => return Err(AppError::not_found("Project not found")),
        }
    };

    let mut payload: Vec<Value> = Vec::new();
    for project in projects.iter().take(MAX_PROJECTS) {
        let empty = Map::new();
        let fields = project.as_object().unwrap_or(&empty);
        let raw_documents = match fields.get("documents") {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        };
        let mut documents: Vec<Value> = Vec::new();
        for document in raw_documents.iter().take(MAX_PROJECT_DOCUMENTS) {
            if document.is_object() {
                documents.push(project_document_for_tool(document));
            }
        }
        payload.push(json!({
            "id": python_str(fields.get("id")),
            "name": python_str(fields.get("name")),
            "files": documents,
        }));
    }
    let count: usize = payload
        .iter()
        .map(|project| {
            project
                .get("files")
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0)
        })
        .sum();
    Ok(json!({"projects": payload, "count": count}))
}

/// Mirrors `read_file_chunk_tool`.
pub fn read_file_chunk(
    arguments: &Map<String, Value>,
    root: &Path,
    cache: &FileCache,
) -> Result<Value, AppError> {
    let file_id = python_str(arguments.get("fileId"));
    let project_id = python_str(arguments.get("projectId")).trim().to_string();
    let scoped = if project_id.is_empty() {
        None
    } else {
        Some(project_id.as_str())
    };
    let cached = load_cached_file(root, &file_id, scoped, cache)?;

    let chunks: Vec<Value> = match cached.get("chunks") {
        Some(Value::Array(items)) => items.clone(),
        _ => Vec::new(),
    };
    // `max(1, int(chunk_index or 1)) - 1`: zero and absent both mean the first chunk.
    let requested = match arguments.get("chunkIndex") {
        Some(value) if is_truthy(value) => python_int(Some(value))?,
        _ => 1,
    };
    let index = requested.max(1) - 1;
    if index < 0 || index as usize >= chunks.len() {
        return Err(AppError::not_found("Chunk not found"));
    }
    let chunk = &chunks[index as usize];
    if !chunk.is_object() {
        return Err(AppError::not_found("Chunk not found"));
    }

    // `json!` does not accept a block expression as a value, so the two fallbacks
    // are computed first.
    let stored_file_id = python_str(cached.get("id"));
    let reported_file_id = if stored_file_id.is_empty() {
        file_id
    } else {
        stored_file_id
    };
    let stored_project_id = python_str(cached.get("projectId"));
    let reported_project_id = if stored_project_id.is_empty() {
        project_id
    } else {
        stored_project_id
    };

    Ok(json!({
        "file": {
            "name": python_str(cached.get("name")),
            "kind": python_str(cached.get("kind")).if_empty("text"),
            "fileId": reported_file_id,
            "projectId": reported_project_id,
            "chunkCount": chunks.len(),
        },
        "chunk": {
            "index": index + 1,
            "lineStart": int_field(chunk, "lineStart", 0)?,
            "lineEnd": int_field(chunk, "lineEnd", 0)?,
            "text": truncate_chars(&python_str(chunk.get("text")), 6000),
        },
    }))
}

// --- the write path ---------------------------------------------------------------

/// Mirrors `write_project`: `<PROJECTS_DIR>/<id>/project.json`, through a
/// `project.tmp` sibling, with the trailing newline `json.dumps(...) + "\n"` implies.
///
/// Note the temp name here **replaces** nothing: it is the literal `project.tmp`, not
/// `project.json.tmp` — a third spelling alongside the reminders store's replaced
/// suffix and the workspace schema's appended one. Reproduced rather than unified,
/// because the bytes on disk are the contract.
pub fn write_project(
    root: &Path,
    project: &Value,
    entropy: &dyn Entropy,
) -> Result<(), AppError> {
    let safe_id = validate_project_id(&python_str(project.get("id")))?;
    let directory = projects_dir(root).join(&safe_id);
    std::fs::create_dir_all(&directory)
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    let path = directory.join("project.json");
    let temporary = directory.join("project.tmp");
    let mut rendered =
        crate::python_json::OrderedJson::from_value_with_order(project, &PROJECT_RECORD_KEYS)
            .render_indent_2();
    rendered.push('\n');
    std::fs::write(&temporary, rendered.as_bytes())
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    std::fs::rename(&temporary, &path).map_err(|error| AppError::invalid_payload(error.to_string()))?;
    let _ = entropy;
    Ok(())
}

/// The key order a project record is written in — the oracle's dict insertion order
/// from `create_project`.
const PROJECT_RECORD_KEYS: [&str; 9] = [
    "id",
    "name",
    "documents",
    "skills",
    "skillRuns",
    "savedItems",
    "artifacts",
    "createdAt",
    "updatedAt",
];

/// Mirrors `create_project`: the 40-project cap, the id minted as
/// `proj-{secrets.token_hex(6)}` (12 hex characters, not 16), and the seven initial
/// fields.
pub fn create_project(
    name: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    if list_projects(root, entropy)?.len() >= MAX_PROJECTS {
        return Err(AppError {
            message: "Too many projects".to_string(),
            code: crate::app_error::codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let now = entropy.now_millis();
    // `secrets.token_hex(6)` is 12 characters; `new_id` gives 16, so the id is built
    // from the first 12 of one draw rather than reusing the helper.
    let hex = entropy.new_id()?;
    let project = json!({
        "id": format!("proj-{}", hex.chars().take(12).collect::<String>()),
        "name": normalize_project_name(Some(&Value::String(name.to_string()))),
        "documents": [],
        "skills": {
            "enabledPacks": [],
            "enabledPackVersions": [],
            "enabledSkills": [],
            "defaultSkill": "",
            "recentSkills": [],
        },
        "skillRuns": [],
        "savedItems": [],
        "artifacts": [],
        "createdAt": now,
        "updatedAt": now,
    });
    write_project(root, &project, entropy)?;
    public_project(&project, entropy)
}

/// Mirrors `delete_project`: `0` when the directory is absent, otherwise the whole
/// subtree removed.
///
/// The oracle also purges the project's RAG rows and media library entries first,
/// each inside a swallowing `try/except`. Those two subsystems are not ported, and
/// **this port does not pretend they were cleaned** — it removes the directory and
/// returns, which is what the oracle does when both blocks raise. Wiring the purge
/// belongs with the RAG and media slices.
pub fn delete_project(project_id: &str, root: &Path) -> Result<i64, AppError> {
    let safe_id = validate_project_id(project_id)?;
    let path = projects_dir(root).join(&safe_id);
    if !path.exists() {
        return Ok(0);
    }
    std::fs::remove_dir_all(&path).map_err(|error| AppError::invalid_payload(error.to_string()))?;
    Ok(1)
}
