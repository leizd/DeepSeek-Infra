//! The v3.0 Memory object schema and read/write policy.
//!
//! Mirrors `deepseek_infra/infra/memory/{schema,policy}.py`. This is a **projection
//! layer** over the legacy store in [`crate::memory`], not a second store: every
//! function here reads or writes `.memory/memories.json` through that module, which
//! is the one authoritative writer. What it adds is the public v3.0 shape
//! (`memoryId`/`type`/`source`/`confidence`/`expiresAt`) alongside the legacy fields
//! clients still read (`id`/`category`/`legacyScope`/`pinned`).
//!
//! # Why the two directions are not symmetric
//!
//! `public_type` and `legacy_category` are a **lossy** pair on purpose: the public
//! vocabulary has five values (`preference`, `fact`, `instruction`, `summary`,
//! `artifact_ref`) while the legacy store has four categories, and three public
//! types collapse onto `fact`. A round trip is therefore not the identity, and the
//! asymmetry is the oracle's — mirroring it is correctness, not a bug to fix.
//!
//! `public_memory` also **defaults timestamps to now** when a stored row has none
//! (`utc_now_iso()`), so reading the same malformed row twice can return different
//! `createdAt`/`updatedAt`. That is observable through the route, so the clock is a
//! parameter rather than a hidden call.

use std::collections::BTreeSet;
use std::path::Path;

use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::core_utils::{Clock, python_truthy};
use crate::memory as legacy;

/// `MAX_SOURCE_REF_VALUE_CHARS` — from `infra/workspace/schema.py`.
pub const MAX_SOURCE_REF_VALUE_CHARS: usize = 2_000;

/// `MEMORY_SCOPES` — the public scope vocabulary.
pub const MEMORY_SCOPES: [&str; 4] = ["global", "project", "skill", "automation"];

/// `MEMORY_TYPES` — the public type vocabulary.
pub const MEMORY_TYPES: [&str; 5] = [
    "preference",
    "fact",
    "instruction",
    "summary",
    "artifact_ref",
];

/// `SOURCE_KINDS` — an unrecognised kind degrades to `manual`.
pub const SOURCE_KINDS: [&str; 5] = ["chat", "saved_item", "project", "automation", "manual"];

/// `public_scope`: the storage scope collapsed to its public family.
///
/// A `seek:` storage scope has no public family, so it becomes `global` — the
/// oracle's own collapse, which is why `seek` appears in `SCOPE_KINDS` but not in
/// `MEMORY_SCOPES`.
pub fn public_scope(value: &str) -> String {
    let normalized = legacy::normalize_memory_scope(Some(&Value::String(value.to_string())));
    if normalized == "global" {
        return "global".to_string();
    }
    if normalized.starts_with("project:") {
        return "project".to_string();
    }
    if normalized.starts_with("skill:") {
        return "skill".to_string();
    }
    if normalized.starts_with("automation:") {
        return "automation".to_string();
    }
    "global".to_string()
}

/// `storage_scope`: the public scope plus an id, rendered as a storage scope.
///
/// An already-prefixed value is normalised as-is; a bare family name needs its id
/// to become a storage scope, and **without one it falls through** to
/// `normalize_memory_scope(value)`, which turns it into `global`. So
/// `storage_scope("project")` with no `project_id` is `global`, not an error.
pub fn storage_scope(scope: &str, project_id: &str, skill_id: &str, automation_id: &str) -> String {
    let value = scope.trim();
    if value.starts_with("project:")
        || value.starts_with("skill:")
        || value.starts_with("automation:")
    {
        return legacy::normalize_memory_scope(Some(&Value::String(value.to_string())));
    }
    if value == "project" && !project_id.is_empty() {
        return legacy::normalize_memory_scope(Some(&Value::String(format!(
            "project:{project_id}"
        ))));
    }
    if value == "skill" && !skill_id.is_empty() {
        return legacy::normalize_memory_scope(Some(&Value::String(format!("skill:{skill_id}"))));
    }
    if value == "automation" && !automation_id.is_empty() {
        return legacy::normalize_memory_scope(Some(&Value::String(format!(
            "automation:{automation_id}"
        ))));
    }
    legacy::normalize_memory_scope(Some(&Value::String(value.to_string())))
}

/// `public_type`: map any stored type/category onto the public vocabulary.
pub fn public_type(value: &str) -> String {
    match value.trim().to_lowercase().as_str() {
        "preference" => "preference",
        "fact" => "fact",
        // `project` and `todo` are legacy categories with no public equivalent.
        "project" => "fact",
        "todo" => "instruction",
        "instruction" => "instruction",
        "summary" => "summary",
        "artifact_ref" => "artifact_ref",
        _ => "fact",
    }
    .to_string()
}

/// `legacy_category`: the inverse map, collapsing the three public-only types.
pub fn legacy_category(value: &str) -> String {
    let public = public_type(value);
    if matches!(public.as_str(), "instruction" | "summary" | "artifact_ref") {
        return "fact".to_string();
    }
    public
}

/// `normalize_source_ref` — a bounded, key-sanitised recursive copy.
///
/// Delegates to [`crate::workspace_schema`], which is where the oracle defines it and
/// from which `infra/memory/schema.py` imports it. A second copy here would be a
/// divergence waiting to happen.
pub fn normalize_source_ref(value: &Value) -> Value {
    crate::workspace_schema::normalize_source_ref(value)
}

/// `public_source`: normalise a stored source into `{"kind", "refId", ...}`.
///
/// A non-dict source is rendered as `{"kind": str(value or "manual"), "refId":
/// fallback_ref}` — so a *string* source keeps its text as the kind, which
/// `SOURCE_KINDS` then rejects back to `manual` unless it happens to name one.
pub fn public_source(value: Option<&Value>, fallback_ref: &str) -> Value {
    let source = match value {
        Some(Value::Object(_)) => normalize_source_ref(value.expect("object")),
        Some(other) if python_truthy(other) => {
            json!({"kind": python_str(other), "refId": fallback_ref})
        }
        _ => json!({"kind": "manual", "refId": fallback_ref}),
    };
    let kind = {
        // `str(source.get("kind") or source.get("type") or "manual")` — an `or` chain
        // tests **truthiness**, not presence, so an empty `kind` falls through to
        // `type` and then to `"manual"`.
        let raw = ["kind", "type"]
            .iter()
            .find_map(|key| source.get(*key).filter(|value| python_truthy(value)))
            .map(python_str)
            .unwrap_or_default();
        let lowered = raw.trim().to_lowercase();
        if lowered.is_empty() {
            "manual".to_string()
        } else {
            lowered
        }
    };
    let kind = if SOURCE_KINDS.contains(&kind.as_str()) {
        kind
    } else {
        "manual".to_string()
    };
    // Same `or` chain on the raw values: a falsy candidate (`""`, `0`, `false`,
    // `[]`, `{}`) falls through to the next key, and only then to `fallback_ref`.
    let ref_id = ["refId", "id", "savedItemId", "messageId"]
        .iter()
        .find_map(|key| source.get(*key).filter(|value| python_truthy(value)))
        .map(python_str)
        .unwrap_or_else(|| fallback_ref.to_string());
    let mut result = Map::new();
    result.insert("kind".to_string(), Value::String(kind));
    result.insert("refId".to_string(), Value::String(ref_id));
    if let Some(fields) = source.as_object() {
        for (key, item) in fields {
            // `type` and `id` are consumed into `kind`/`refId`; everything else is
            // carried through so a client keeps fields this layer does not model.
            if !result.contains_key(key) && key != "type" && key != "id" {
                result.insert(key.clone(), item.clone());
            }
        }
    }
    Value::Object(result)
}

/// `public_confidence`: `float()` with a `0.9` default, clamped to `[0, 1]`.
///
/// The clamp is Python's `max(0.0, min(1.0, confidence))`, and the argument order
/// matters for the non-finite inputs `float()` accepts:
///
/// - `min(1.0, nan)` is **`1.0`** — Python's `min` returns the first argument unless
///   the second compares strictly less, and `nan < 1.0` is `False`.
/// - `min(1.0, inf)` is `1.0`; `min(1.0, -inf)` is `-inf`, which `max(0.0, -inf)`
///   then floors to `0.0`.
///
/// So `"nan"` and `"inf"` both become `1.0`, not the default. `f64::clamp` cannot
/// express this (it propagates NaN and would return `NaN`), which is why the pair is
/// spelled out.
pub fn public_confidence(value: Option<&Value>) -> f64 {
    let confidence = match value {
        Some(Value::Number(number)) => number.as_f64().unwrap_or(0.9),
        Some(Value::String(text)) => python_float(text).unwrap_or(0.9),
        Some(Value::Bool(flag)) => {
            if *flag {
                1.0
            } else {
                0.0
            }
        }
        _ => 0.9,
    };
    // `min(1.0, confidence)`: the first argument wins unless the second is strictly
    // less, so NaN keeps `1.0`.
    let floor = if confidence < 1.0 { confidence } else { 1.0 };
    // `max(0.0, floor)`: `0.0` wins unless `floor` is strictly greater.
    if floor > 0.0 { floor } else { 0.0 }
}

/// `public_memory`: the v3.0 object plus the legacy fields clients still read.
pub fn public_memory(item: &Value, clock: &dyn Clock) -> Value {
    let now = clock.now_iso();
    let memory_id = {
        let raw = item
            .get("memoryId")
            .or_else(|| item.get("id"))
            .map(python_str)
            .unwrap_or_default();
        raw
    };
    let content = legacy::normalize_memory_text(item.get("content"));
    let stored_scope = item
        .get("scope")
        .map(python_str)
        .unwrap_or_else(|| "global".to_string());
    let raw_type = item
        .get("type")
        .or_else(|| item.get("category"))
        .map(python_str)
        .unwrap_or_else(|| "fact".to_string());
    let created_at = item
        .get("createdAt")
        .map(python_str)
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| now.clone());
    let updated_at = item
        .get("updatedAt")
        .map(python_str)
        .filter(|text| !text.is_empty())
        .or_else(|| {
            item.get("createdAt")
                .map(python_str)
                .filter(|text| !text.is_empty())
        })
        .unwrap_or_else(|| now.clone());
    let expires_at = item
        .get("expiresAt")
        .filter(|value| python_truthy(value))
        .map(python_str);

    let mut payload = Map::new();
    payload.insert("memoryId".to_string(), Value::String(memory_id.clone()));
    payload.insert(
        "scope".to_string(),
        Value::String(public_scope(&stored_scope)),
    );
    payload.insert("type".to_string(), Value::String(public_type(&raw_type)));
    payload.insert("content".to_string(), Value::String(content));
    payload.insert(
        "source".to_string(),
        public_source(
            item.get("source"),
            &item.get("id").map(python_str).unwrap_or_default(),
        ),
    );
    payload.insert(
        "confidence".to_string(),
        json!(public_confidence(item.get("confidence"))),
    );
    payload.insert("createdAt".to_string(), Value::String(created_at));
    payload.insert("updatedAt".to_string(), Value::String(updated_at));
    payload.insert(
        "expiresAt".to_string(),
        match expires_at {
            Some(text) => Value::String(text),
            None => Value::Null,
        },
    );
    payload.insert("id".to_string(), Value::String(memory_id));
    payload.insert(
        "category".to_string(),
        Value::String(public_type(&raw_type)),
    );
    payload.insert(
        "legacyScope".to_string(),
        Value::String(legacy::normalize_memory_scope(item.get("scope"))),
    );
    payload.insert(
        "pinned".to_string(),
        Value::Bool(item.get("pinned").is_some_and(python_truthy)),
    );
    Value::Object(payload)
}

// --- policy ----------------------------------------------------------------------

/// `assert_memory_safe` — the sensitive-content refusal.
pub fn assert_memory_safe(content: &str) -> Result<(), AppError> {
    if legacy::is_sensitive_memory(content) {
        return Err(AppError {
            message: "Memory content contains sensitive data and was not saved".to_string(),
            code: codes::SENSITIVE_CONTENT,
            status: 400,
        });
    }
    Ok(())
}

/// `readable_scopes` — `global` plus one storage scope per supplied id.
pub fn readable_scopes(project_id: &str, skill_id: &str, automation_id: &str) -> Vec<String> {
    let mut scopes = vec!["global".to_string()];
    if !project_id.is_empty() {
        scopes.push(storage_scope("project", project_id, "", ""));
    }
    if !skill_id.is_empty() {
        scopes.push(storage_scope("skill", "", skill_id, ""));
    }
    if !automation_id.is_empty() {
        scopes.push(storage_scope("automation", "", "", automation_id));
    }
    scopes
}

/// `skill_can_read_memory`: an explicit `read: false` denies; a `project` scope
/// needs a project id.
pub fn skill_can_read_memory(skill: &Value, project_id: &str) -> bool {
    let policy = skill.get("memoryPolicy");
    let policy = policy.and_then(Value::as_object);
    let read = policy.and_then(|fields| fields.get("read"));
    // `if policy.get("read") is False` — only the literal `false` denies, so a
    // missing key (or any other falsy value) allows.
    if matches!(read, Some(Value::Bool(false))) {
        return false;
    }
    let scope = policy
        .and_then(|fields| fields.get("scope"))
        .map(python_str)
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| "global".to_string());
    scope != "project" || !project_id.is_empty()
}

// --- the store operations ---------------------------------------------------------

/// `store.list_memories` — the public projection, optionally filtered by scope.
pub fn list_memories(scope: &str, project_id: &str, root: &Path, clock: &dyn Clock) -> Vec<Value> {
    // `storage_scope(...) if scope or project_id else ""` — an empty pair means
    // "no filter", and `""` is falsy so the filter is skipped entirely.
    let storage = if !scope.is_empty() || !project_id.is_empty() {
        storage_scope(scope, project_id, "", "")
    } else {
        String::new()
    };
    legacy::load_memories(root)
        .into_iter()
        .filter(|item| {
            if storage.is_empty() {
                return true;
            }
            legacy::normalize_memory_scope(item.get("scope")) == storage
        })
        .map(|item| public_memory(&item, clock))
        .collect()
}

/// `store.add_memory` — the public write, layered over the legacy upsert.
///
/// The oracle writes **twice**: `upsert_memory` persists, then `save_memories(
/// _merge_item(item))` persists the public fields it just added. Reproduced as-is,
/// because the second write is what stores `type`/`source`/`confidence`/`expiresAt`.
#[allow(clippy::too_many_arguments)]
pub fn add_memory(
    content: &str,
    scope: &str,
    memory_type: &str,
    project_id: &str,
    skill_id: &str,
    automation_id: &str,
    source: Option<&Value>,
    confidence: f64,
    expires_at: &str,
    pinned: bool,
    root: &Path,
    clock: &dyn Clock,
) -> Result<Value, AppError> {
    assert_memory_safe(content)?;
    let source_data = public_source(source.or(Some(&json!({"kind": "manual", "refId": ""}))), "");
    let storage = storage_scope(scope, project_id, skill_id, automation_id);
    let mut item = legacy::upsert_memory(
        content,
        Some(&Value::String(legacy_category(memory_type))),
        &storage,
        source_data
            .get("kind")
            .map(python_str)
            .unwrap_or_else(|| "manual".to_string())
            .as_str(),
        pinned,
        None,
        root,
        clock,
    )?;
    let object = item
        .as_object_mut()
        .expect("an upserted memory is an object");
    object.insert("type".to_string(), Value::String(public_type(memory_type)));
    object.insert("source".to_string(), source_data);
    object.insert("confidence".to_string(), json!(confidence));
    if !expires_at.is_empty() {
        object.insert(
            "expiresAt".to_string(),
            Value::String(expires_at.to_string()),
        );
    }
    let merged = merge_item(&item, root, clock)?;
    Ok(public_memory(&merged, clock))
}

/// `store._merge_item`: replace the matching id in place, else insert at the front.
///
/// Returns the merged item so the caller reports what was **persisted** rather than
/// what it built — the two differ when the store repairs fields on write.
fn merge_item(item: &Value, root: &Path, clock: &dyn Clock) -> Result<Value, AppError> {
    let items = legacy::load_memories(root);
    let memory_id = item.get("id").map(python_str).unwrap_or_default();
    let mut merged: Vec<Value> = Vec::with_capacity(items.len() + 1);
    let mut replaced = false;
    for existing in items {
        if existing.get("id").map(python_str).unwrap_or_default() == memory_id {
            merged.push(item.clone());
            replaced = true;
        } else {
            merged.push(existing);
        }
    }
    if !replaced {
        merged.insert(0, item.clone());
    }
    legacy::save_memories(root, &merged, clock)?;
    Ok(item.clone())
}

/// `store.edit_memory` — a field-wise patch under the store's own locks.
///
/// The oracle reaches into `legacy_memory`'s private lock and unlocked read/write so
/// the read-modify-write is **one** critical section. This port does the same by
/// holding the process lock across the whole patch; the file lock and the mutation
/// fence are taken by [`legacy::save_memories`] on the way out.
pub fn edit_memory(
    memory_id: &str,
    updates: &Map<String, Value>,
    root: &Path,
    clock: &dyn Clock,
) -> Result<Value, AppError> {
    let safe_id = memory_id.trim();
    if safe_id.is_empty() {
        return Err(AppError::invalid_payload("Memory id is required"));
    }
    let _guard = legacy::memory_process_lock();
    // The **unlocked** pair: this thread already holds the process mutex, and Rust's
    // `Mutex` is not reentrant where Python's `RLock` is.
    let mut items = legacy::load_unlocked_for_caller(root);
    for index in 0..items.len() {
        let current = items[index].clone();
        let current_id = current
            .get("id")
            .or_else(|| current.get("memoryId"))
            .map(python_str)
            .unwrap_or_default();
        if current_id != safe_id {
            continue;
        }
        let mut updated = current.as_object().cloned().unwrap_or_default();
        if updates.contains_key("content") {
            let content = legacy::normalize_memory_text(updates.get("content"));
            assert_memory_safe(&content)?;
            updated.insert("content".to_string(), Value::String(content));
        }
        if updates.contains_key("type") || updates.contains_key("category") {
            let raw = updates
                .get("type")
                .or_else(|| updates.get("category"))
                .map(python_str)
                .filter(|text| !text.is_empty())
                .unwrap_or_else(|| "fact".to_string());
            let memory_type = public_type(&raw);
            updated.insert("type".to_string(), Value::String(memory_type.clone()));
            updated.insert(
                "category".to_string(),
                Value::String(legacy_category(&memory_type)),
            );
        }
        if updates.contains_key("scope") {
            let raw = updates
                .get("scope")
                .map(python_str)
                .filter(|text| !text.is_empty())
                .unwrap_or_else(|| "global".to_string());
            updated.insert(
                "scope".to_string(),
                Value::String(storage_scope(
                    &raw,
                    &updates.get("projectId").map(python_str).unwrap_or_default(),
                    &updates.get("skillId").map(python_str).unwrap_or_default(),
                    &updates
                        .get("automationId")
                        .map(python_str)
                        .unwrap_or_default(),
                )),
            );
        }
        if updates.contains_key("source") {
            updated.insert(
                "source".to_string(),
                public_source(updates.get("source"), ""),
            );
        }
        if updates.contains_key("confidence") {
            // Copied verbatim, not clamped: the oracle assigns `updates.get(...)`
            // directly, so a patch can store a value the read path then clamps.
            updated.insert(
                "confidence".to_string(),
                updates.get("confidence").cloned().unwrap_or(Value::Null),
            );
        }
        if updates.contains_key("expiresAt") {
            updated.insert(
                "expiresAt".to_string(),
                Value::String(updates.get("expiresAt").map(python_str).unwrap_or_default()),
            );
        }
        if updates.contains_key("pinned") {
            updated.insert(
                "pinned".to_string(),
                Value::Bool(updates.get("pinned").is_some_and(python_truthy)),
            );
        }
        updated.insert("updatedAt".to_string(), Value::String(clock.now_iso()));
        let patched = Value::Object(updated);
        items[index] = patched.clone();
        legacy::save_unlocked_for_caller(root, &items, clock)?;
        return Ok(public_memory(&patched, clock));
    }
    Err(AppError::not_found("Memory not found"))
}

/// `store.delete_memory` — the legacy id delete.
pub fn delete_memory(memory_id: &str, root: &Path, clock: &dyn Clock) -> Result<i64, AppError> {
    legacy::delete_memory_by_id(memory_id, root, clock)
}

/// `search.search_memories` — scope-filtered retrieval, then the public projection.
///
/// `limit` is applied **after** the projection, and `max(0, int(limit))` means a
/// negative limit yields an empty list rather than everything.
pub fn search_memories(
    query: &str,
    project_id: &str,
    skill_id: &str,
    automation_id: &str,
    limit: Option<i64>,
    root: &Path,
    clock: &dyn Clock,
) -> Vec<Value> {
    let scopes = readable_scopes(project_id, skill_id, automation_id);
    let hits = legacy::retrieve_memories(query, Some(&scopes), root, None);
    let result: Vec<Value> = hits.iter().map(|item| public_memory(item, clock)).collect();
    match limit {
        Some(limit) => {
            let capped = limit.max(0) as usize;
            result.into_iter().take(capped).collect()
        }
        None => result,
    }
}

/// `search.memory_context_for_skill` — the formatted context a Skill reads.
pub fn memory_context_for_skill(
    skill: &Value,
    query: &str,
    project_id: &str,
    root: &Path,
    clock: &dyn Clock,
) -> String {
    let policy = skill.get("memoryPolicy").and_then(Value::as_object);
    // `if not policy.get("read"): return ""` — any falsy value returns early, which
    // is the *opposite* of `skill_can_read_memory`'s literal-`False` test.
    let reads = policy
        .and_then(|fields| fields.get("read"))
        .is_some_and(python_truthy);
    if !reads {
        return String::new();
    }
    let mut scopes = vec!["global".to_string()];
    let scope = policy
        .and_then(|fields| fields.get("scope"))
        .map(python_str)
        .unwrap_or_default();
    if scope == "project" && !project_id.is_empty() {
        scopes.push(format!("project:{project_id}"));
    }
    let hits = legacy::retrieve_memories(query, Some(&scopes), root, None);
    let _ = clock;
    legacy::format_memory_context(&hits)
}

/// `store.delete_memories_by_query` with the route's scope list.
pub fn delete_memories_by_query(
    query: &str,
    scopes: &[String],
    root: &Path,
    clock: &dyn Clock,
) -> Result<i64, AppError> {
    legacy::delete_memories_by_query(query, Some(scopes), root, clock)
}

/// `store.clear_memories` — the count is the pre-clear length.
pub fn clear_memories(root: &Path, clock: &dyn Clock) -> Result<i64, AppError> {
    legacy::clear_memories(root, clock)
}

/// `detect_memory_conflicts`, with the route's category normalisation applied first.
///
/// The oracle's `deps.detect_memory_conflicts(content, category, scope)` passes the
/// **already-normalised** public category through; the legacy function re-normalises
/// it, so `public_type` runs before the call and the legacy normaliser sees a
/// category it accepts unchanged.
pub fn detect_conflicts(content: &str, category: &str, scope: &str, root: &Path) -> Vec<Value> {
    let public = public_type(category);
    legacy::detect_memory_conflicts(
        root,
        content,
        Some(&Value::String(legacy_category(&public))),
        scope,
    )
}

/// Python's `float()`, or `None` where it raises.
///
/// `int`/`float` accept a leading sign, surrounding whitespace, `inf`/`infinity`/
/// `nan` (any case) and underscores between digits; they reject `""`, `"1__0"` and
/// any other text.
pub fn python_float(text: &str) -> Option<f64> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    let lowered = trimmed.to_ascii_lowercase();
    let (sign, body) = match lowered.strip_prefix('-') {
        Some(rest) => (-1.0, rest),
        None => (1.0, lowered.strip_prefix('+').unwrap_or(&lowered)),
    };
    if matches!(body, "inf" | "infinity") {
        return Some(sign * f64::INFINITY);
    }
    if body == "nan" {
        return Some(f64::NAN);
    }
    // Underscores are legal only *between* digits, so a leading/trailing or doubled
    // one is a rejection rather than something to strip.
    if trimmed.contains('_') {
        let bytes: Vec<char> = trimmed.chars().collect();
        for (index, character) in bytes.iter().enumerate() {
            if *character != '_' {
                continue;
            }
            let previous = index.checked_sub(1).and_then(|i| bytes.get(i));
            let next = bytes.get(index + 1);
            let digits_around = previous.is_some_and(|c| c.is_ascii_digit())
                && next.is_some_and(|c| c.is_ascii_digit());
            if !digits_around {
                return None;
            }
        }
        return trimmed.replace('_', "").parse::<f64>().ok();
    }
    trimmed.parse::<f64>().ok()
}

/// `str(value)` for the fields this layer reads.
pub fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        other => crate::python_json::value_str(other),
    }
}

/// The public scope families as a set, for tests and callers that validate input.
pub fn scope_families() -> BTreeSet<&'static str> {
    MEMORY_SCOPES.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core_utils::FixedClock;

    fn clock() -> FixedClock {
        FixedClock {
            epoch_seconds: 1_700_000_000,
        }
    }

    #[test]
    fn the_public_and_legacy_type_maps_are_lossy_in_the_oracles_direction() {
        assert_eq!(public_type("todo"), "instruction");
        assert_eq!(public_type("project"), "fact");
        // The collapse: three public types share one legacy category.
        assert_eq!(legacy_category("instruction"), "fact");
        assert_eq!(legacy_category("summary"), "fact");
        assert_eq!(legacy_category("artifact_ref"), "fact");
        assert_eq!(legacy_category("preference"), "preference");
        // An unknown value is `fact` on both sides.
        assert_eq!(public_type("nonsense"), "fact");
    }

    #[test]
    fn storage_scope_needs_an_id_and_silently_falls_back_to_global() {
        assert_eq!(storage_scope("project", "p1", "", ""), "project:p1");
        // No id: the bare family name normalises to `global`, not to an error.
        assert_eq!(storage_scope("project", "", "", ""), "global");
        // An already-prefixed value passes through the storage normaliser.
        assert_eq!(storage_scope("skill:s1", "", "", ""), "skill:s1");
        // A malformed id is narrowed to `global` by the storage normaliser.
        assert_eq!(storage_scope("project", "bad id", "", ""), "global");
    }

    #[test]
    fn public_scope_collapses_seek_to_global() {
        assert_eq!(public_scope("project:p1"), "project");
        assert_eq!(public_scope("skill:s1"), "skill");
        assert_eq!(public_scope("automation:a1"), "automation");
        // `seek` is a storage scope with no public family.
        assert_eq!(public_scope("seek:x"), "global");
        assert_eq!(public_scope("global"), "global");
    }

    #[test]
    fn public_confidence_defaults_and_clamps() {
        assert_eq!(public_confidence(None), 0.9);
        assert_eq!(public_confidence(Some(&json!("nonsense"))), 0.9);
        assert_eq!(public_confidence(Some(&json!(2.0))), 1.0);
        assert_eq!(public_confidence(Some(&json!(-1.0))), 0.0);
        assert_eq!(public_confidence(Some(&json!(0.5))), 0.5);
        assert_eq!(public_confidence(Some(&json!("0.25"))), 0.25);
        assert_eq!(public_confidence(Some(&json!(true))), 1.0);
        // Measured, not inferred: Python's `min(1.0, nan)` is `1.0` because `nan < 1.0`
        // is `False`, so the *first* argument survives — the same for `inf`.
        assert_eq!(public_confidence(Some(&json!("nan"))), 1.0);
        assert_eq!(public_confidence(Some(&json!("inf"))), 1.0);
        assert_eq!(public_confidence(Some(&json!("-inf"))), 0.0);
    }

    #[test]
    fn python_float_matches_the_accepted_forms() {
        assert_eq!(python_float("1.5"), Some(1.5));
        assert_eq!(python_float("  2  "), Some(2.0));
        assert_eq!(python_float("-3"), Some(-3.0));
        assert_eq!(python_float("1_0.5"), Some(10.5));
        assert_eq!(python_float(""), None);
        assert_eq!(python_float("abc"), None);
        // A doubled or edge underscore is a rejection.
        assert_eq!(python_float("1__0"), None);
        assert_eq!(python_float("_10"), None);
        assert_eq!(python_float("10_"), None);
        assert!(python_float("inf").unwrap().is_infinite());
        assert!(python_float("nan").unwrap().is_nan());
    }

    #[test]
    fn source_ref_is_bounded_and_sanitised() {
        let normalised = normalize_source_ref(&json!({
            "kind": "chat",
            "bad key!": "x",
            "nested": {"ok": 1, "drop me!": 2},
            "emptyNested": {"!!!": 1},
            "list": [1, null, "two"],
            "emptyList": [null],
            "nothing": null,
            "flag": true,
        }));
        assert_eq!(normalised["kind"], "chat");
        assert_eq!(normalised["badkey"], "x");
        assert_eq!(normalised["nested"]["ok"], 1);
        // `"drop me!"` is **sanitised** to `dropme`, not dropped — only a key that
        // empties entirely disappears. Measured against the oracle, which keeps it.
        assert_eq!(normalised["nested"]["dropme"], 2);
        // A nested object that empties *is* dropped, not kept as `{}`.
        assert!(normalised.get("emptyNested").is_none());
        assert_eq!(normalised["list"], json!(["1", "two"]));
        assert!(normalised.get("emptyList").is_none());
        assert_eq!(normalised["nothing"], Value::Null);
        assert_eq!(normalised["flag"], true);
        // Non-dict input is `{}`.
        assert_eq!(normalize_source_ref(&json!("x")), json!({}));
    }

    #[test]
    fn public_source_degrades_an_unknown_kind_to_manual() {
        assert_eq!(
            public_source(Some(&json!({"kind": "chat"})), "")["kind"],
            "chat"
        );
        assert_eq!(
            public_source(Some(&json!({"kind": "nonsense"})), "")["kind"],
            "manual"
        );
        assert_eq!(public_source(None, "ref1")["refId"], "ref1");
        // A non-dict truthy source keeps its text as the kind, which then degrades.
        assert_eq!(public_source(Some(&json!("chat")), "")["kind"], "chat");
        assert_eq!(public_source(Some(&json!(5)), "")["kind"], "manual");
        // `type`/`id` are consumed into `kind`/`refId` and not carried through.
        let source = public_source(Some(&json!({"type": "chat", "id": "m1"})), "");
        assert_eq!(source["kind"], "chat");
        assert_eq!(source["refId"], "m1");
        assert!(source.get("type").is_none());
    }

    #[test]
    fn public_memory_defaults_missing_timestamps_to_now() {
        let item = json!({"id": "m1", "content": "hello"});
        let public = public_memory(&item, &clock());
        assert_eq!(public["memoryId"], "m1");
        assert_eq!(public["id"], "m1");
        assert_eq!(public["scope"], "global");
        assert_eq!(public["type"], "fact");
        assert_eq!(public["category"], "fact");
        assert_eq!(public["legacyScope"], "global");
        assert_eq!(public["pinned"], false);
        assert_eq!(public["confidence"], 0.9);
        // The clock supplies both, so they agree when neither is stored.
        assert_eq!(public["createdAt"], public["updatedAt"]);
        assert!(public["expiresAt"].is_null());
    }

    #[test]
    fn skill_can_read_memory_and_the_context_helper_disagree_on_falsy() {
        // `skill_can_read_memory` denies only on the literal `false`...
        assert!(skill_can_read_memory(&json!({}), "p1"));
        assert!(skill_can_read_memory(&json!({"memoryPolicy": {}}), "p1"));
        assert!(!skill_can_read_memory(
            &json!({"memoryPolicy": {"read": false}}),
            "p1"
        ));
        // ...and a `project` scope needs an id.
        assert!(!skill_can_read_memory(
            &json!({"memoryPolicy": {"read": true, "scope": "project"}}),
            ""
        ));
        assert!(skill_can_read_memory(
            &json!({"memoryPolicy": {"read": true, "scope": "project"}}),
            "p1"
        ));
    }

    #[test]
    fn add_memory_persists_the_public_fields_it_adds() {
        let root = &temp_root("add");
        let item = add_memory(
            "likes dark mode",
            "global",
            "preference",
            "",
            "",
            "",
            Some(&json!({"kind": "chat", "refId": "c1"})),
            0.7,
            "2030-01-01T00:00:00+00:00",
            true,
            root,
            &clock(),
        )
        .unwrap();
        assert_eq!(item["type"], "preference");
        assert_eq!(item["confidence"], 0.7);
        assert_eq!(item["source"]["kind"], "chat");
        assert_eq!(item["pinned"], true);

        // The stored row carries the public fields, because the oracle saves twice.
        let stored = legacy::load_memories(root);
        assert_eq!(stored.len(), 1);
        assert_eq!(stored[0]["type"], "preference");
        assert_eq!(stored[0]["source"]["kind"], "chat");
        assert_eq!(stored[0]["confidence"], 0.7);
        assert_eq!(stored[0]["expiresAt"], "2030-01-01T00:00:00+00:00");
    }

    #[test]
    fn add_memory_refuses_sensitive_content_before_writing() {
        let root = &temp_root("sensitive");
        let error = add_memory(
            "my api key is sk-abcdefghijklmnopqrstuvwxyz",
            "global",
            "fact",
            "",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap_err();
        assert_eq!(error.code, codes::SENSITIVE_CONTENT);
        assert!(legacy::load_memories(root).is_empty());
    }

    #[test]
    fn edit_memory_patches_fields_and_reports_not_found() {
        let root = &temp_root("edit");
        let created = add_memory(
            "original",
            "global",
            "fact",
            "",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap();
        let id = created["id"].as_str().unwrap().to_string();

        let mut updates = Map::new();
        updates.insert("content".to_string(), json!("updated"));
        updates.insert("type".to_string(), json!("instruction"));
        updates.insert("pinned".to_string(), json!(true));
        let edited = edit_memory(&id, &updates, root, &clock()).unwrap();
        assert_eq!(edited["content"], "updated");
        assert_eq!(edited["type"], "instruction");
        // `edit_memory` writes `type` **and** `category` from the *public* type, so
        // the stored category is `instruction`, not the legacy `fact` a fresh
        // `add_memory` would store. Measured against the oracle, not inferred.
        assert_eq!(edited["category"], "instruction");
        assert_eq!(edited["pinned"], true);

        let missing = edit_memory("nope", &Map::new(), root, &clock()).unwrap_err();
        assert_eq!(missing.code, codes::NOT_FOUND);
        assert_eq!(missing.status, 404);

        let blank = edit_memory("  ", &Map::new(), root, &clock()).unwrap_err();
        assert_eq!(blank.code, codes::INVALID_PAYLOAD);
    }

    #[test]
    fn list_memories_filters_only_when_a_scope_is_supplied() {
        let root = &temp_root("list");
        add_memory(
            "g",
            "global",
            "fact",
            "",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap();
        add_memory(
            "p",
            "project",
            "fact",
            "p1",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap();

        // No filter: both.
        assert_eq!(list_memories("", "", root, &clock()).len(), 2);
        // A project filter: only the project row.
        let scoped = list_memories("project", "p1", root, &clock());
        assert_eq!(scoped.len(), 1);
        assert_eq!(scoped[0]["content"], "p");
        assert_eq!(scoped[0]["scope"], "project");
    }

    #[test]
    fn search_memories_applies_the_limit_after_the_projection() {
        let root = &temp_root("search");
        add_memory(
            "rust ownership notes",
            "global",
            "fact",
            "",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap();
        let hits = search_memories("rust ownership", "", "", "", Some(10), root, &clock());
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0]["type"], "fact");
        // A zero limit yields nothing; the oracle's `max(0, int(limit))`.
        assert!(search_memories("rust", "", "", "", Some(0), root, &clock()).is_empty());
    }

    #[test]
    fn memory_context_for_skill_returns_empty_without_read_permission() {
        let root = &temp_root("skill-context");
        add_memory(
            "skill fact",
            "global",
            "fact",
            "",
            "",
            "",
            None,
            0.9,
            "",
            false,
            root,
            &clock(),
        )
        .unwrap();
        assert_eq!(
            memory_context_for_skill(&json!({}), "skill", "", root, &clock()),
            ""
        );
        assert_eq!(
            memory_context_for_skill(
                &json!({"memoryPolicy": {"read": false}}),
                "skill",
                "",
                root,
                &clock()
            ),
            ""
        );
        let context = memory_context_for_skill(
            &json!({"memoryPolicy": {"read": true}}),
            "skill fact",
            "",
            root,
            &clock(),
        );
        assert!(context.contains("skill fact"), "context: {context}");
    }

    /// A per-test scratch root, in the crate's own style — `deepseek-policy` has no
    /// `tempfile` dev-dependency, and adding one for tests only would put a new crate
    /// version in the lockfile for no production gain.
    fn temp_root(label: &str) -> std::path::PathBuf {
        static COUNTER: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(0);
        let unique = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "memory-schema-{label}-{}-{unique}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp root");
        root
    }
}
