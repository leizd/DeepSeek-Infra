//! The memory store and its three dispatch branches, mirroring
//! `deepseek_infra/infra/data/memory.py` plus the `suggest_memory` /
//! `recall_memory` / `forget_memory` branch bodies in `tools.py`.
//!
//! Storage is one JSON array at `<root>/.memory/memories.json`, written as Python's
//! `json.dumps(..., ensure_ascii=False, indent=2)` with the record's key order
//! preserved, behind **two** locks: a process-wide mutex, a cross-process file lock
//! on `memories.lock`, and the workspace mutation gate that fences and bumps the
//! backup generation.
//!
//! # Where this is deliberately not faithful yet
//!
//! `retrieve_memories` in the oracle adds a **vector-search bonus** from
//! `local_rag.search_memories_index`. `local_rag` is 2,676 lines and belongs to the
//! RAG slice, so this port takes the bonus through an injectable
//! [`VectorHits`] provider and defaults to none.
//!
//! The oracle wraps that call in `try/except Exception` and falls back to an empty
//! map, so the default reproduces the oracle's **own degradation path** exactly. But
//! when the vector index is populated the oracle's scores include a bonus this does
//! not, which can reorder results. `recall_memory`'s ranking is therefore verified
//! only for the case where the vector index contributes nothing — that is stated
//! here, in the migration matrix, and in the docs rather than left to be discovered.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::app_error::{AppError, codes};
use crate::core_utils::{Clock, query_tokens, score_chunk};
use crate::file_lock::FileLockGuard;
use crate::mutation_gate::mutation_scope;
use crate::python_json::OrderedJson;

/// `MEMORY_MAX_ITEMS`.
pub const MEMORY_MAX_ITEMS: usize = 400;
/// `MEMORY_RETRIEVE_LIMIT`.
pub const MEMORY_RETRIEVE_LIMIT: usize = 12;

/// The record's key order, as `_save_memories_unlocked` builds it.
pub const RECORD_KEYS: [&str; 11] = [
    "id",
    "memoryId",
    "content",
    "category",
    "type",
    "scope",
    "source",
    "confidence",
    "pinned",
    "createdAt",
    "updatedAt",
];
/// The same order plus the optional `expiresAt`.
pub const RECORD_KEYS_WITH_EXPIRY: [&str; 12] = [
    "id",
    "memoryId",
    "content",
    "category",
    "type",
    "scope",
    "source",
    "confidence",
    "pinned",
    "createdAt",
    "updatedAt",
    "expiresAt",
];

/// Mirrors `_memory_lock`.
///
/// Call sites bind the whole `LockResult` rather than unwrapping it, so a poisoned
/// mutex is **deliberately** not an error: Python's `threading.RLock` has no poisoning,
/// so turning one into a failure would add a failure mode the oracle lacks.
static MEMORY_LOCK: Mutex<()> = Mutex::new(());

/// The scopes `normalize_memory_scope` accepts.
const SCOPE_KINDS: [&str; 4] = ["project", "seek", "skill", "automation"];
/// The categories `normalize_memory_category` accepts.
const CATEGORIES: [&str; 4] = ["preference", "project", "todo", "fact"];

pub fn memory_dir(root: &Path) -> PathBuf {
    root.join(".memory")
}

pub fn memory_file(root: &Path) -> PathBuf {
    memory_dir(root).join("memories.json")
}

/// `MEMORY_DIR / "memories.lock"`.
pub fn memory_lock_path(root: &Path) -> PathBuf {
    memory_dir(root).join("memories.lock")
}

// --- normalization ---------------------------------------------------------------

/// Mirrors `normalize_memory_text`: whitespace collapsed, trimmed, capped at 1200.
pub fn normalize_memory_text(value: Option<&Value>) -> String {
    let raw = python_str_or(value, "");
    let collapsed = whitespace_regex().replace_all(&raw, " ").to_string();
    collapsed.trim().chars().take(1200).collect()
}

/// Mirrors `normalize_memory_scope`.
///
/// Anything that is not `global` and does not match
/// `(project|seek|skill|automation):[A-Za-z0-9_.:-]{1,80}` becomes `global` — a
/// **silent** narrowing, which is why the round trip is asserted in tests.
pub fn normalize_memory_scope(value: Option<&Value>) -> String {
    let scope = python_str_or(value, "global");
    let scope = scope.trim();
    if scope.is_empty() {
        return "global".to_string();
    }
    if scope == "global" {
        return "global".to_string();
    }
    if let Some((kind, rest)) = scope.split_once(':') {
        let valid_kind = SCOPE_KINDS.contains(&kind);
        let valid_rest = !rest.is_empty()
            && rest.chars().count() <= 80
            && rest
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
        if valid_kind && valid_rest {
            return scope.to_string();
        }
    }
    "global".to_string()
}

/// Mirrors `memory_fingerprint`: `sha256(...)[:20]`, scoped.
pub fn memory_fingerprint(content: &str, scope: &str) -> String {
    let normalized =
        normalize_memory_text(Some(&Value::String(content.to_string()))).to_lowercase();
    let scope = normalize_memory_scope(Some(&Value::String(scope.to_string())));
    let source = if scope == "global" {
        normalized
    } else {
        format!("{scope}\u{0}{normalized}")
    };
    let digest = Sha256::digest(source.as_bytes());
    let hex = crate::core_utils::encode_lower_hex(&digest);
    hex[..20].to_string()
}

/// Mirrors `is_sensitive_memory`.
pub fn is_sensitive_memory(content: &str) -> bool {
    sensitive_regex().is_match(content)
}

/// Mirrors `infer_memory_category`, including the check order.
pub fn infer_memory_category(content: &str) -> String {
    let text = content.to_lowercase();
    if preference_regex().is_match(&text) {
        return "preference".to_string();
    }
    if project_regex().is_match(&text) {
        return "project".to_string();
    }
    if todo_regex().is_match(&text) {
        return "todo".to_string();
    }
    "fact".to_string()
}

/// Mirrors `normalize_memory_category`: an unknown value falls back to inference,
/// it is not an error.
pub fn normalize_memory_category(value: Option<&Value>, content: &str) -> String {
    let category = python_str_or(value, "").trim().to_lowercase();
    if CATEGORIES.contains(&category.as_str()) {
        category
    } else {
        infer_memory_category(content)
    }
}

/// Mirrors `memory_conflict_key`, including the ordered preference checks.
pub fn memory_conflict_key(content: &str, category: &str) -> String {
    let text = normalize_memory_text(Some(&Value::String(content.to_string()))).to_lowercase();
    if category == "preference" {
        for (pattern, key) in [
            (
                r"(vue|react|angular|svelte|前端框架|frontend framework)",
                "preference:frontend-framework",
            ),
            (
                r"(简洁|短回答|详细|一步一步|条理|concise|brief|detailed|step by step)",
                "preference:answer-style",
            ),
            (
                r"(中文|英文|英语|chinese|english|language)",
                "preference:language",
            ),
            (r"(称呼|叫我|名字|call me|name)", "preference:addressing"),
            (r"(深色|浅色|主题|dark|light|theme)", "preference:theme"),
        ] {
            if compiled(pattern).is_match(&text) {
                return key.to_string();
            }
        }
    }
    if category == "project" {
        let pattern = compiled(
            r"(项目|project|repo|仓库|app)[：:\s-]*([A-Za-z0-9_\-\u{4e00}-\u{9fff}]{2,40})",
        );
        if let Some(captures) = pattern.captures(&text) {
            if let Some(name) = captures.get(2) {
                return format!("project:{}", name.as_str());
            }
        }
    }
    String::new()
}

// --- the store -------------------------------------------------------------------

/// Mirrors `load_memories`: read, sort by `(pinned, updatedAt || createdAt)`
/// descending, then truncate to `MEMORY_MAX_ITEMS`.
pub fn load_memories(root: &Path) -> Vec<Value> {
    let _guard = MEMORY_LOCK.lock();
    load_unlocked(root)
}

fn load_unlocked(root: &Path) -> Vec<Value> {
    let Ok(raw) = std::fs::read_to_string(memory_file(root)) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    let Value::Array(items) = value else {
        return Vec::new();
    };
    let mut memories: Vec<Value> = items.into_iter().filter(Value::is_object).collect();
    // A stable descending sort on `(pinned, timestamp)`, matching the oracle's
    // `sort(..., reverse=True)` — `Reverse` keeps it stable, so ties keep file order.
    memories.sort_by_key(|item| std::cmp::Reverse(memory_sort_key(item)));
    memories.truncate(MEMORY_MAX_ITEMS);
    memories
}

fn memory_sort_key(item: &Value) -> (bool, String) {
    let pinned = item.get("pinned").and_then(Value::as_bool).unwrap_or(false);
    let updated = item
        .get("updatedAt")
        .or_else(|| item.get("createdAt"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    (pinned, updated)
}

/// Mirrors `save_memories`: the process lock, the file lock, and the gate.
pub fn save_memories(root: &Path, memories: &[Value], clock: &dyn Clock) -> Result<(), AppError> {
    let _process = MEMORY_LOCK.lock();
    let _file = FileLockGuard::acquire(&memory_lock_path(root), true)
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    save_unlocked(root, memories, clock)
}

/// Mirrors `_save_memories_unlocked`, which doubles as the migration.
///
/// Every field is coerced here, so a record written by an older shape is repaired
/// on the next write rather than at read time. Two details matter for identity:
/// `id` falls back to a **content-addressed** fingerprint, and `confidence` is
/// clamped to `[0, 1]` defaulting to `0.9`.
fn save_unlocked(root: &Path, memories: &[Value], clock: &dyn Clock) -> Result<(), AppError> {
    // The oracle's `_save_memories_unlocked` opens the gate itself, so *every* save
    // is fenced — the callers only hold the process and file locks. Putting the gate
    // in the delete path instead would leave the migration save ungated.
    let _scope = mutation_scope(None, root).map_err(|error| AppError {
        message: error.message,
        code: codes::INVALID_REQUEST,
        status: error.status.unwrap_or(500),
    })?;
    let directory = memory_dir(root);
    std::fs::create_dir_all(&directory).map_err(|e| AppError::invalid_payload(e.to_string()))?;

    let mut cleaned: Vec<OrderedJson> = Vec::new();
    for item in memories {
        let Some(object) = item.as_object() else {
            continue;
        };
        let content = normalize_memory_text(object.get("content"));
        if content.is_empty() {
            continue;
        }
        let scope = normalize_memory_scope(
            object
                .get("scope")
                .or(Some(&Value::String("global".into()))),
        );
        let memory_id = match object.get("memoryId").and_then(Value::as_str) {
            Some(id) if !id.is_empty() => id.to_string(),
            _ => match object.get("id").and_then(Value::as_str) {
                Some(id) if !id.is_empty() => id.to_string(),
                _ => memory_fingerprint(&content, &scope),
            },
        };
        // `raw_source = item.get("source") or "manual"`, then
        // `raw_source if isinstance(raw_source, dict) else str(raw_source or "manual")`.
        // The truthiness test matters: `0` and `false` fall back to "manual" while
        // a non-zero number and `true` are stringified ("5", "True").
        let raw_source = match object.get("source") {
            Some(value) if python_truthy(value) => value.clone(),
            _ => Value::String("manual".to_string()),
        };
        let source = match raw_source {
            Value::Object(_) => raw_source,
            other if python_truthy(&other) => Value::String(crate::python_json::value_str(&other)),
            _ => Value::String("manual".to_string()),
        };
        let confidence = match object.get("confidence") {
            Some(Value::Number(number)) => number.as_f64().unwrap_or(0.9),
            Some(Value::String(text)) => text.parse::<f64>().unwrap_or(0.9),
            _ => 0.9,
        };
        let confidence = confidence.clamp(0.0, 1.0);
        let memory_type = {
            let raw = object
                .get("type")
                .or_else(|| object.get("category"))
                .and_then(Value::as_str)
                .unwrap_or("fact")
                .trim()
                .to_lowercase();
            if raw.is_empty() {
                "fact".to_string()
            } else {
                raw
            }
        };
        let category = match object.get("category").and_then(Value::as_str) {
            Some(text) if !text.is_empty() => text.to_string(),
            _ => memory_type.clone(),
        };
        let now = clock.now_iso();
        let created_at = string_field(object.get("createdAt")).unwrap_or_else(|| now.clone());
        let updated_at = string_field(object.get("updatedAt")).unwrap_or(now);

        let mut fields: Vec<(String, OrderedJson)> = vec![
            (
                "id".into(),
                OrderedJson::Scalar(Value::String(memory_id.clone())),
            ),
            (
                "memoryId".into(),
                OrderedJson::Scalar(Value::String(memory_id)),
            ),
            (
                "content".into(),
                OrderedJson::Scalar(Value::String(content)),
            ),
            (
                "category".into(),
                OrderedJson::Scalar(Value::String(category)),
            ),
            (
                "type".into(),
                OrderedJson::Scalar(Value::String(memory_type)),
            ),
            ("scope".into(), OrderedJson::Scalar(Value::String(scope))),
            ("source".into(), OrderedJson::Scalar(source)),
            ("confidence".into(), OrderedJson::Scalar(json!(confidence))),
            (
                "pinned".into(),
                OrderedJson::Scalar(Value::Bool(
                    object
                        .get("pinned")
                        .and_then(Value::as_bool)
                        .unwrap_or(false),
                )),
            ),
            (
                "createdAt".into(),
                OrderedJson::Scalar(Value::String(created_at)),
            ),
            (
                "updatedAt".into(),
                OrderedJson::Scalar(Value::String(updated_at)),
            ),
        ];
        let expires_at = object
            .get("expiresAt")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if !expires_at.is_empty() {
            fields.push((
                "expiresAt".into(),
                OrderedJson::Scalar(Value::String(expires_at)),
            ));
        }
        cleaned.push(OrderedJson::Object(fields));
    }
    cleaned.truncate(MEMORY_MAX_ITEMS);
    let rendered = OrderedJson::List(cleaned).render_indent_2();

    // The oracle writes `<name>.<pid>.tmp` for the memory store, unlike reminders.
    let temporary = directory.join(format!("{}.{}.tmp", "memories.json", std::process::id()));
    std::fs::write(&temporary, rendered.as_bytes())
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    std::fs::rename(&temporary, memory_file(root))
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;

    // The oracle then best-effort syncs the RAG index and swallows any failure.
    // That sync is not ported; see the module docs.
    Ok(())
}

/// Python truthiness for the JSON shapes a field can hold.
///
/// `0`, `false`, `""`, `[]`, `{}` and `None` are falsy; everything else is truthy.
/// The oracle relies on this in `or` chains, where `0` and `false` differ from a
/// non-zero number and `true`.
fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|float| float != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

fn string_field(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(text)) if !text.is_empty() => Some(text.clone()),
        Some(Value::Number(number)) => Some(number.to_string()),
        _ => None,
    }
}

// --- conflicts and suggestions ---------------------------------------------------

/// Mirrors `build_memory_suggestion`.
pub fn build_memory_suggestion(
    content: &str,
    category: Option<&Value>,
    scope: &str,
    root: &Path,
) -> Result<Value, AppError> {
    let content = normalize_memory_text(Some(&Value::String(content.to_string())));
    if content.is_empty() {
        return Err(AppError::invalid_payload(
            "Memory suggestion content is empty",
        ));
    }
    if is_sensitive_memory(&content) {
        return Err(AppError {
            message: "Memory suggestion contains sensitive content".to_string(),
            code: codes::SENSITIVE_CONTENT,
            status: 400,
        });
    }
    let category = normalize_memory_category(category, &content);
    let scope = normalize_memory_scope(Some(&Value::String(scope.to_string())));
    let conflicts = conflicts_in(root, &content, &category, &scope);
    Ok(json!({
        "content": content,
        "category": category,
        "scope": scope,
        "conflicts": conflicts,
    }))
}

/// Mirrors `detect_memory_conflicts`: same scope, same category and same conflict
/// key, capped at five. The oracle reads the store itself; this takes the root.
pub fn detect_memory_conflicts(
    root: &Path,
    content: &str,
    category: Option<&Value>,
    scope: &str,
) -> Vec<Value> {
    let content = normalize_memory_text(Some(&Value::String(content.to_string())));
    let scope = normalize_memory_scope(Some(&Value::String(scope.to_string())));
    let category = normalize_memory_category(category, &content);
    conflicts_in(root, &content, &category, &scope)
}

fn conflicts_in(root: &Path, content: &str, category: &str, scope: &str) -> Vec<Value> {
    let key = memory_conflict_key(content, category);
    if key.is_empty() {
        return Vec::new();
    }
    let mut conflicts: Vec<Value> = Vec::new();
    for item in load_memories(root) {
        let Some(object) = item.as_object() else {
            continue;
        };
        let item_scope = normalize_memory_scope(
            object
                .get("scope")
                .or(Some(&Value::String("global".to_string()))),
        );
        if item_scope != scope {
            continue;
        }
        let existing_content = object
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let existing_category =
            normalize_memory_category(object.get("category"), &existing_content);
        if existing_category != category {
            continue;
        }
        if memory_conflict_key(&existing_content, &existing_category) != key {
            continue;
        }
        if normalize_memory_text(Some(&Value::String(existing_content.clone()))).to_lowercase()
            == content.to_lowercase()
        {
            continue;
        }
        conflicts.push(json!({
            "id": object.get("id").and_then(Value::as_str).unwrap_or(""),
            "content": existing_content,
            "category": existing_category,
            "scope": scope,
            "reason": "same_memory_domain",
        }));
        if conflicts.len() >= 5 {
            break;
        }
    }
    conflicts
}

// --- retrieval -------------------------------------------------------------------

/// The vector-search bonus the oracle gets from
/// `local_rag.search_memories_index`. Not ported; see the module docs.
pub type VectorHits = dyn Fn(&str, &[String]) -> HashMap<String, i64>;

/// Mirrors `is_memory_broad_query`.
pub fn is_memory_broad_query(query: &str) -> bool {
    broad_regex().is_match(query)
}

/// Mirrors `retrieve_memories`, with the vector bonus injected.
pub fn retrieve_memories(
    query: &str,
    scopes: Option<&[String]>,
    root: &Path,
    vector_hits: Option<&VectorHits>,
) -> Vec<Value> {
    let memories = load_memories(root);
    if memories.is_empty() {
        return Vec::new();
    }

    let tokens = query_tokens(query);
    let broad = is_memory_broad_query(query);
    let allowed: Vec<String> = match scopes {
        Some(scopes) if !scopes.is_empty() => {
            let mut normalized: Vec<String> = scopes
                .iter()
                .map(|scope| normalize_memory_scope(Some(&Value::String(scope.clone()))))
                .collect();
            normalized.sort();
            normalized.dedup();
            normalized
        }
        _ => vec!["global".to_string()],
    };

    let hits: HashMap<String, i64> = match vector_hits {
        Some(provider) => provider(query, &allowed),
        None => HashMap::new(),
    };

    let mut scored: Vec<(i64, String, Value)> = Vec::new();
    for item in memories {
        let Some(object) = item.as_object() else {
            continue;
        };
        let item_scope = normalize_memory_scope(
            object
                .get("scope")
                .or(Some(&Value::String("global".to_string()))),
        );
        if !allowed.contains(&item_scope) {
            continue;
        }
        let content = object
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let category = object
            .get("category")
            .and_then(Value::as_str)
            .unwrap_or("fact")
            .to_string();
        let updated_at = object
            .get("updatedAt")
            .or_else(|| object.get("createdAt"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        let mut score = 0i64;
        if object
            .get("pinned")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            score += 100;
        }
        if category == "preference" {
            score += 8;
        }
        if category == "project" {
            score += 3;
        }
        if broad {
            score += 10;
        }
        score += score_chunk(&content, &tokens);
        let memory_id = object.get("id").and_then(Value::as_str).unwrap_or("");
        if let Some(bonus) = hits.get(memory_id) {
            score += 1.max(bonus / 10);
        }

        if score > 0 {
            scored.push((score, updated_at, item));
        }
    }

    // Descending on `(score, updatedAt)`, stable so ties keep load order.
    scored.sort_by(|left, right| (right.0, &right.1).cmp(&(left.0, &left.1)));
    scored
        .into_iter()
        .take(MEMORY_RETRIEVE_LIMIT)
        .map(|(_, _, item)| item)
        .collect()
}

/// Mirrors `delete_memories_by_query`.
pub fn delete_memories_by_query(
    query: &str,
    scopes: Option<&[String]>,
    root: &Path,
    clock: &dyn Clock,
) -> Result<i64, AppError> {
    let query = normalize_memory_text(Some(&Value::String(query.to_string())));
    if query.is_empty() {
        return Ok(0);
    }
    let allowed: Option<Vec<String>> = scopes.map(|scopes| {
        let mut normalized: Vec<String> = scopes
            .iter()
            .map(|scope| normalize_memory_scope(Some(&Value::String(scope.clone()))))
            .collect();
        normalized.sort();
        normalized.dedup();
        normalized
    });

    let _process = MEMORY_LOCK.lock();
    let _file = FileLockGuard::acquire(&memory_lock_path(root), true)
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    let memories = load_unlocked(root);
    let lowered_query = query.to_lowercase();
    let mut kept: Vec<Value> = Vec::new();
    let mut deleted = 0i64;
    for item in memories {
        let content = item
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        let item_scope = normalize_memory_scope(
            item.get("scope")
                .or(Some(&Value::String("global".to_string()))),
        );
        let in_scope = match &allowed {
            None => true,
            Some(scopes) => scopes.contains(&item_scope),
        };
        if in_scope && content.contains(&lowered_query) {
            deleted += 1;
            continue;
        }
        kept.push(item);
    }
    if deleted > 0 {
        save_unlocked(root, &kept, clock)?;
    }
    Ok(deleted)
}

// --- the branches ----------------------------------------------------------------

/// Mirrors `memory_tool_scopes`.
///
/// Note the three-way split on the *raw* `scope` argument's truthiness: an empty
/// scope inherits the request's default scopes, while an explicitly-supplied
/// `global` narrows to `["global"]` and does **not** inherit.
pub fn memory_tool_scopes(scope: &str, default_scope: &str) -> Vec<String> {
    let requested = normalize_memory_scope(Some(&Value::String(scope.to_string())));
    if !scope.is_empty() {
        if requested != "global" {
            return vec![requested];
        }
        return vec!["global".to_string()];
    }
    let current = normalize_memory_scope(Some(&Value::String(default_scope.to_string())));
    if current != "global" {
        vec!["global".to_string(), current]
    } else {
        vec!["global".to_string()]
    }
}

/// Mirrors the `suggest_memory` branch.
///
/// The optional `memory_suggestion_callback` fires only on success, matching the
/// oracle's ordering (build, then notify).
pub fn suggest_memory(
    arguments: &Map<String, Value>,
    default_scope: &str,
    root: &Path,
    on_suggestion: Option<&dyn Fn(&Value)>,
) -> Result<Value, AppError> {
    let scope_argument = arguments.get("scope");
    let scope = match scope_argument {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        _ => default_scope.to_string(),
    };
    let scope = normalize_memory_scope(Some(&Value::String(scope)));
    let content = python_str_or(arguments.get("content"), "");
    let category = python_str_or(arguments.get("category"), "");
    let result = build_memory_suggestion(&content, Some(&Value::String(category)), &scope, root)?;
    if let Some(callback) = on_suggestion {
        callback(&result);
    }
    Ok(result)
}

/// Mirrors the `recall_memory` branch.
pub fn recall_memory(
    arguments: &Map<String, Value>,
    default_scope: &str,
    root: &Path,
    vector_hits: Option<&VectorHits>,
) -> Value {
    let cleaned = {
        let raw = python_str_or(arguments.get("query"), "");
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            "memory".to_string()
        } else {
            trimmed.to_string()
        }
    };
    let scopes = memory_tool_scopes(&python_str_or(arguments.get("scope"), ""), default_scope);
    let memories = retrieve_memories(&cleaned, Some(&scopes), root, vector_hits);
    let projected: Vec<Value> = memories
        .iter()
        .map(|item| {
            json!({
                "id": item.get("id").and_then(Value::as_str).unwrap_or(""),
                "content": item.get("content").and_then(Value::as_str).unwrap_or(""),
                "category": item.get("category").and_then(Value::as_str).unwrap_or("fact"),
                "scope": normalize_memory_scope(
                    item.get("scope").or(Some(&Value::String("global".to_string())))
                ),
                "updatedAt": item
                    .get("updatedAt")
                    .or_else(|| item.get("createdAt"))
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            })
        })
        .collect();
    json!({"query": cleaned, "scopes": scopes, "memories": projected})
}

/// Mirrors the `forget_memory` branch.
pub fn forget_memory(
    arguments: &Map<String, Value>,
    default_scope: &str,
    root: &Path,
    clock: &dyn Clock,
) -> Result<Value, AppError> {
    let cleaned = python_str_or(arguments.get("query"), "");
    let cleaned = cleaned.trim();
    if cleaned.is_empty() {
        return Err(AppError::invalid_payload("forget_memory query is required"));
    }
    let scopes = memory_tool_scopes(&python_str_or(arguments.get("scope"), ""), default_scope);
    let deleted = delete_memories_by_query(cleaned, Some(&scopes), root, clock)?;
    Ok(json!({"query": cleaned, "scopes": scopes, "deleted": deleted}))
}

// --- helpers ---------------------------------------------------------------------

/// `str(value or fallback)` for the shapes a memory field can hold.
fn python_str_or(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        _ => fallback.to_string(),
    }
}

fn compiled(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static pattern must compile")
}

fn cached(pattern: &str) -> &'static Regex {
    use std::sync::OnceLock;
    static CACHE: OnceLock<std::sync::Mutex<HashMap<String, &'static Regex>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| std::sync::Mutex::new(HashMap::new()));
    let mut cache = cache.lock().expect("pattern cache");
    if let Some(found) = cache.get(pattern) {
        return found;
    }
    let leaked: &'static Regex = Box::leak(Box::new(compiled(pattern)));
    cache.insert(pattern.to_string(), leaked);
    leaked
}

fn whitespace_regex() -> &'static Regex {
    cached(r"\s+")
}

fn sensitive_regex() -> &'static Regex {
    cached(
        r"(?i)(api\s*key|apikey|token|secret|password|密码|密钥|私钥|银行卡|身份证|验证码|授权码)",
    )
}

fn preference_regex() -> &'static Regex {
    cached(r"(喜欢|不喜欢|偏好|习惯|希望|以后回答|称呼|语气|风格|prefer|preference)")
}

fn project_regex() -> &'static Regex {
    cached(r"(项目|代码|仓库|app|客户端|后端|前端|接口|文件|功能|需求|backend|frontend|python)")
}

fn todo_regex() -> &'static Regex {
    cached(r"(待办|计划|下一步|todo|任务|要做|提醒|refactor)")
}

fn broad_regex() -> &'static Regex {
    cached(
        r"(?i)(你记得|记忆|长期记忆|关于我|我的偏好|我的信息|你知道我什么|remember about me|memory)",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicI64, Ordering};

    static COUNTER: AtomicI64 = AtomicI64::new(0);

    /// A frozen clock so a store write is reproducible.
    fn clock() -> crate::core_utils::FixedClock {
        crate::core_utils::FixedClock {
            epoch_seconds: 1_760_000_000,
        }
    }

    fn temp_root(label: &str) -> PathBuf {
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "memory-test-{label}-{}-{unique}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp root");
        root
    }

    fn write_raw(root: &Path, raw: &str) {
        std::fs::create_dir_all(memory_dir(root)).unwrap();
        std::fs::write(memory_file(root), raw).unwrap();
    }

    // --- normalization --------------------------------------------------------

    #[test]
    fn text_normalization_collapses_and_caps() {
        assert_eq!(normalize_memory_text(Some(&json!("  a   b\tc  "))), "a b c");
        assert_eq!(normalize_memory_text(None), "");
        assert_eq!(
            normalize_memory_text(Some(&json!("x".repeat(2000))))
                .chars()
                .count(),
            1200
        );
    }

    #[test]
    fn scope_normalization_is_a_silent_narrowing() {
        assert_eq!(normalize_memory_scope(Some(&json!("global"))), "global");
        assert_eq!(
            normalize_memory_scope(Some(&json!("project:abc"))),
            "project:abc"
        );
        assert_eq!(
            normalize_memory_scope(Some(&json!("seek:SEARCH-1"))),
            "seek:SEARCH-1"
        );
        // Every rejection becomes global rather than an error.
        for bad in ["", "  ", "unknown:x", "project:", "project:x y", "global:x"] {
            assert_eq!(
                normalize_memory_scope(Some(&json!(bad))),
                "global",
                "{bad} should narrow to global"
            );
        }
        // Missing and null fall back to global.
        assert_eq!(normalize_memory_scope(None), "global");
        assert_eq!(normalize_memory_scope(Some(&json!(null))), "global");
    }

    #[test]
    fn fingerprint_is_scoped_case_insensitive_and_twenty_chars() {
        assert_eq!(
            memory_fingerprint("Hello", "global"),
            memory_fingerprint("hello", "global")
        );
        assert_ne!(
            memory_fingerprint("Hello", "global"),
            memory_fingerprint("Hello", "project:abc")
        );
        assert_eq!(memory_fingerprint("Hello", "global").len(), 20);
        // Two different scopes must not collide through the NUL separator.
        assert_ne!(
            memory_fingerprint("x", "project:a"),
            memory_fingerprint("project:a", "global")
        );
    }

    #[test]
    fn sensitive_detection_matches_the_oracles_terms() {
        for content in [
            "my api key is x",
            "APIKEY",
            "the token",
            "secret",
            "password",
            "密码",
            "密钥",
            "银行卡",
            "身份证",
            "验证码",
            "授权码",
        ] {
            assert!(is_sensitive_memory(content), "{content}");
        }
        assert!(!is_sensitive_memory("I like concise answers"));
        // Case-insensitive.
        assert!(is_sensitive_memory("PASSWORD"));
    }

    #[test]
    fn category_inference_follows_the_oracles_check_order() {
        assert_eq!(infer_memory_category("我喜欢简洁"), "preference");
        // "项目" would also match the todo pattern via 提醒, but preference wins
        // when a preference term is present.
        assert_eq!(infer_memory_category("项目 代码"), "project");
        assert_eq!(infer_memory_category("待办 计划"), "todo");
        assert_eq!(infer_memory_category("the sky is blue"), "fact");
        // A valid explicit category wins over inference.
        assert_eq!(
            normalize_memory_category(Some(&json!("FACT")), "我喜欢简洁"),
            "fact"
        );
        // An invalid one falls back to inference rather than erroring.
        assert_eq!(
            normalize_memory_category(Some(&json!("nope")), "我喜欢简洁"),
            "preference"
        );
    }

    #[test]
    fn conflict_keys_cover_the_preference_domains_in_order() {
        assert_eq!(
            memory_conflict_key("我用 React", "preference"),
            "preference:frontend-framework"
        );
        assert_eq!(
            memory_conflict_key("请一步一步来", "preference"),
            "preference:answer-style"
        );
        assert_eq!(
            memory_conflict_key("用中文回答", "preference"),
            "preference:language"
        );
        assert_eq!(
            memory_conflict_key("叫我 leizd", "preference"),
            "preference:addressing"
        );
        assert_eq!(
            memory_conflict_key("我喜欢深色主题", "preference"),
            "preference:theme"
        );
        assert_eq!(
            memory_conflict_key("项目：DeepSeek-Infra", "project"),
            "project:deepseek-infra"
        );
        // No key for an unrelated preference, and none for a non-preference.
        assert_eq!(memory_conflict_key("我喜欢猫", "preference"), "");
        assert_eq!(memory_conflict_key("事实", "fact"), "");
    }

    // --- the branches ---------------------------------------------------------

    #[test]
    fn suggest_builds_a_suggestion_and_rejects_empty_or_sensitive() {
        let root = temp_root("suggest");
        let mut arguments = Map::new();
        arguments.insert("content".to_string(), json!("  我喜欢简洁的回答  "));
        let result = suggest_memory(&arguments, "global", &root, None).unwrap();
        assert_eq!(result["content"], "我喜欢简洁的回答");
        assert_eq!(result["category"], "preference");
        assert_eq!(result["scope"], "global");
        assert_eq!(result["conflicts"], json!([]));

        let mut empty = Map::new();
        empty.insert("content".to_string(), json!("   "));
        assert_eq!(
            suggest_memory(&empty, "global", &root, None)
                .unwrap_err()
                .message,
            "Memory suggestion content is empty"
        );

        let mut sensitive = Map::new();
        sensitive.insert("content".to_string(), json!("my password is hunter2"));
        let failure = suggest_memory(&sensitive, "global", &root, None).unwrap_err();
        assert_eq!(failure.code, codes::SENSITIVE_CONTENT);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn suggest_fires_the_callback_only_on_success() {
        use std::sync::atomic::AtomicBool;
        let root = temp_root("callback");
        let called = AtomicBool::new(false);
        let callback = |_value: &Value| {
            called.store(true, Ordering::SeqCst);
        };
        let mut arguments = Map::new();
        arguments.insert("content".to_string(), json!("fact"));
        suggest_memory(&arguments, "global", &root, Some(&callback)).unwrap();
        assert!(called.load(Ordering::SeqCst));

        let called_again = AtomicBool::new(false);
        let failing = |_value: &Value| {
            called_again.store(true, Ordering::SeqCst);
        };
        let mut sensitive = Map::new();
        sensitive.insert("content".to_string(), json!("password"));
        assert!(suggest_memory(&sensitive, "global", &root, Some(&failing)).is_err());
        assert!(
            !called_again.load(Ordering::SeqCst),
            "a failure must not notify"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn scope_arguments_do_not_inherit_when_explicitly_global() {
        // No scope argument: inherit the request default.
        assert_eq!(
            memory_tool_scopes("", "project:abc"),
            vec!["global", "project:abc"]
        );
        assert_eq!(memory_tool_scopes("", "global"), vec!["global"]);
        // Explicitly global: narrow, do not inherit.
        assert_eq!(memory_tool_scopes("global", "project:abc"), vec!["global"]);
        // An explicit valid scope wins.
        assert_eq!(
            memory_tool_scopes("seek:s1", "project:abc"),
            vec!["seek:s1"]
        );
        // An invalid scope normalizes to global and therefore narrows.
        assert_eq!(memory_tool_scopes("bogus", "project:abc"), vec!["global"]);
    }

    #[test]
    fn recall_projects_the_stored_fields_and_defaults_the_query() {
        let root = temp_root("recall");
        write_raw(
            &root,
            r#"[{"id": "m1", "content": "我用 React", "category": "preference", "scope": "global",
                 "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-02T00:00:00+00:00"}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("React"));
        let result = recall_memory(&arguments, "global", &root, None);
        assert_eq!(result["query"], "React");
        assert_eq!(result["scopes"], json!(["global"]));
        let memories = result["memories"].as_array().unwrap();
        assert_eq!(memories.len(), 1);
        assert_eq!(memories[0]["id"], "m1");
        assert_eq!(memories[0]["category"], "preference");
        assert_eq!(memories[0]["updatedAt"], "2026-01-02T00:00:00+00:00");
        // The projection carries exactly these five fields.
        assert_eq!(memories[0].as_object().unwrap().len(), 5);

        // A blank query becomes the literal "memory".
        let mut blank = Map::new();
        blank.insert("query".to_string(), json!("   "));
        assert_eq!(
            recall_memory(&blank, "global", &root, None)["query"],
            "memory"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn recall_ranks_pinned_and_preference_memories_higher() {
        let root = temp_root("rank");
        write_raw(
            &root,
            r#"[{"id": "plain", "content": "the sky is blue", "category": "fact", "scope": "global"},
                {"id": "pref", "content": "the sky is blue", "category": "preference", "scope": "global"},
                {"id": "pinned", "content": "the sky is blue", "category": "fact", "scope": "global",
                 "pinned": true}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("sky"));
        let result = recall_memory(&arguments, "global", &root, None);
        let ids: Vec<&str> = result["memories"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["id"].as_str().unwrap())
            .collect();
        // pinned 100 + chunk, preference 8 + chunk, plain chunk only.
        assert_eq!(ids, vec!["pinned", "pref", "plain"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A memory with no matching token and no category bonus scores zero and is
    /// therefore not returned at all.
    #[test]
    fn recall_drops_zero_scoring_memories() {
        let root = temp_root("zero");
        write_raw(
            &root,
            r#"[{"id": "unrelated", "content": "the sky is blue", "category": "fact", "scope": "global"}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("completely-different-term"));
        let result = recall_memory(&arguments, "global", &root, None);
        assert_eq!(result["memories"], json!([]));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn recall_honours_scope_filters_and_the_vector_bonus() {
        let root = temp_root("scope-filter");
        write_raw(
            &root,
            r#"[{"id": "g1", "content": "sky", "category": "fact", "scope": "global"},
                {"id": "p1", "content": "sky", "category": "fact", "scope": "project:abc"}]"#,
        );
        // With the default global scope only the global memory is eligible.
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("sky"));
        let result = recall_memory(&arguments, "global", &root, None);
        assert_eq!(
            result["memories"][0]["id"], "g1",
            "a project-scoped memory must not leak into a global query"
        );
        assert_eq!(result["memories"].as_array().unwrap().len(), 1);

        // The injected vector bonus can raise a zero-scoring memory into the list.
        write_raw(
            &root,
            r#"[{"id": "m1", "content": "unrelated text", "category": "fact", "scope": "global"}]"#,
        );
        let provider = |_query: &str, _scopes: &[String]| {
            let mut hits = HashMap::new();
            hits.insert("m1".to_string(), 50i64);
            hits
        };
        let boosted = recall_memory(&arguments, "global", &root, Some(&provider));
        assert_eq!(boosted["memories"].as_array().unwrap().len(), 1);
        assert_eq!(boosted["memories"][0]["id"], "m1");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn forget_requires_a_query_and_reports_the_deleted_count() {
        let root = temp_root("forget");
        write_raw(
            &root,
            r#"[{"id": "a", "content": "I use React", "scope": "global"},
                {"id": "b", "content": "I use Vue", "scope": "global"},
                {"id": "c", "content": "I use React", "scope": "project:x"}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("   "));
        assert_eq!(
            forget_memory(&arguments, "global", &root, &clock())
                .unwrap_err()
                .message,
            "forget_memory query is required"
        );

        // A global default scope deletes only the global match.
        let mut react = Map::new();
        react.insert("query".to_string(), json!("react"));
        let result = forget_memory(&react, "global", &root, &clock()).unwrap();
        assert_eq!(result["deleted"], 1);
        assert_eq!(result["scopes"], json!(["global"]));
        // Case-insensitive substring matching.
        assert_eq!(load_memories(&root).len(), 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn forget_with_a_project_default_scope_also_clears_that_scope() {
        let root = temp_root("forget-project");
        write_raw(
            &root,
            r#"[{"id": "a", "content": "I use React", "scope": "global"},
                {"id": "c", "content": "I use React", "scope": "project:x"}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("React"));
        let result = forget_memory(&arguments, "project:x", &root, &clock()).unwrap();
        assert_eq!(result["scopes"], json!(["global", "project:x"]));
        assert_eq!(result["deleted"], 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    // --- the store ------------------------------------------------------------

    #[test]
    fn the_store_file_matches_pythons_layout_and_key_order() {
        let root = temp_root("layout");
        std::fs::create_dir_all(memory_dir(&root)).unwrap();
        let memories = vec![json!({
            "id": "abc",
            "content": "I like concise answers",
            "category": "preference",
            "scope": "global",
            "source": "manual",
            "confidence": 0.75,
            "pinned": false,
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-02T00:00:00+00:00",
        })];
        save_memories(&root, &memories, &clock()).unwrap();
        let written = std::fs::read_to_string(memory_file(&root)).unwrap();
        let expected = "[\n  {\n    \"id\": \"abc\",\n    \"memoryId\": \"abc\",\n    \"content\": \"I like concise answers\",\n    \"category\": \"preference\",\n    \"type\": \"preference\",\n    \"scope\": \"global\",\n    \"source\": \"manual\",\n    \"confidence\": 0.75,\n    \"pinned\": false,\n    \"createdAt\": \"2026-01-01T00:00:00+00:00\",\n    \"updatedAt\": \"2026-01-02T00:00:00+00:00\"\n  }\n]";
        assert_eq!(written, expected);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The write path is the migration: malformed records are repaired, dropped or
    /// coerced rather than rejected.
    #[test]
    fn saving_migrates_records_in_place() {
        let root = temp_root("migrate");
        std::fs::create_dir_all(memory_dir(&root)).unwrap();
        let memories = vec![
            json!("not an object"),
            json!({"content": "   "}),
            json!({"content": "survivor", "confidence": 5, "category": "PREFERENCE", "scope": "bogus"}),
            json!({"content": "bad confidence", "confidence": "nope"}),
        ];
        save_memories(&root, &memories, &clock()).unwrap();
        let stored = load_memories(&root);
        assert_eq!(stored.len(), 2, "non-objects and empty content are dropped");
        let survivor = stored
            .iter()
            .find(|item| item["content"] == "survivor")
            .expect("survivor kept");
        // `confidence` is clamped, an invalid scope narrows to global, and `type`
        // is derived from the lowercased category.
        assert_eq!(survivor["confidence"], 1.0);
        assert_eq!(survivor["scope"], "global");
        assert_eq!(survivor["type"], "preference");
        // The id is content-addressed when neither `memoryId` nor `id` is present.
        assert_eq!(survivor["id"], memory_fingerprint("survivor", "global"));
        let bad = stored
            .iter()
            .find(|item| item["content"] == "bad confidence")
            .expect("present");
        assert_eq!(bad["confidence"], 0.9);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn reads_are_silent_about_corruption() {
        let root = temp_root("tolerant");
        for raw in ["{not json", "42", "{}", "[]"] {
            write_raw(&root, raw);
            assert!(load_memories(&root).is_empty(), "{raw}");
        }
        write_raw(&root, r#"[{"id": "a"}, "x", 7, null, {"id": "b"}]"#);
        assert_eq!(load_memories(&root).len(), 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn loading_sorts_pinned_first_then_by_timestamp() {
        let root = temp_root("sort");
        write_raw(
            &root,
            r#"[{"id": "old", "createdAt": "2026-01-01T00:00:00+00:00"},
                {"id": "new", "createdAt": "2026-02-01T00:00:00+00:00"},
                {"id": "pinned-old", "createdAt": "2025-01-01T00:00:00+00:00", "pinned": true}]"#,
        );
        // Bound first: `load_memories` returns an owned `Vec`, and `ids` borrows it.
        let loaded = load_memories(&root);
        let ids: Vec<&str> = loaded
            .iter()
            .map(|item| item["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["pinned-old", "new", "old"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_delete_participates_in_the_backup_generation_protocol() {
        let root = temp_root("fence");
        write_raw(&root, r#"[{"id": "a", "content": "x", "scope": "global"}]"#);
        assert_eq!(
            delete_memories_by_query("x", None, &root, &clock()).unwrap(),
            1
        );
        let generation = std::fs::read_to_string(root.join(".workspace-generation")).unwrap();
        // One delete, one mutation scope, two bumps.
        assert_eq!(generation, "2");
        assert!(memory_lock_path(&root).exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_delete_that_matches_nothing_does_not_write() {
        let root = temp_root("no-write");
        write_raw(&root, r#"[{"id": "a", "content": "x", "scope": "global"}]"#);
        assert_eq!(
            delete_memories_by_query("absent", None, &root, &clock()).unwrap(),
            0
        );
        // No mutation scope was entered, so no generation file exists.
        assert!(!root.join(".workspace-generation").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn detect_conflicts_finds_same_domain_memories_in_scope() {
        let root = temp_root("conflicts");
        write_raw(
            &root,
            r#"[{"id": "old", "content": "我用 Vue 做前端", "category": "preference", "scope": "global"},
                {"id": "other-scope", "content": "我用 React", "category": "preference", "scope": "project:x"}]"#,
        );
        let conflicts = conflicts_in(
            &root,
            "我用 React",
            &normalize_memory_category(Some(&json!("preference")), "我用 React"),
            "global",
        );
        assert_eq!(conflicts.len(), 1);
        assert_eq!(conflicts[0]["id"], "old");
        assert_eq!(conflicts[0]["reason"], "same_memory_domain");
        // A different scope is not a conflict for a global memory.
        assert_eq!(conflicts[0]["scope"], "global");

        // An identical content is not a conflict with itself.
        let self_conflicts = conflicts_in(
            &root,
            "我用 Vue 做前端",
            &normalize_memory_category(Some(&json!("preference")), "我用 Vue 做前端"),
            "global",
        );
        assert!(self_conflicts.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn broad_queries_score_every_in_scope_memory() {
        assert!(is_memory_broad_query("你记得什么"));
        assert!(is_memory_broad_query("MEMORY"));
        assert!(!is_memory_broad_query("what is rust"));

        let root = temp_root("broad");
        write_raw(
            &root,
            r#"[{"id": "a", "content": "unrelated text", "category": "fact", "scope": "global"}]"#,
        );
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("你记得什么"));
        // A broad query scores +10, so even an unmatched memory is returned.
        let result = recall_memory(&arguments, "global", &root, None);
        assert_eq!(result["memories"].as_array().unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&root);
    }
}
