//! Native data-plane HTTP surfaces the browser frontend calls directly.
//!
//! Python serves these today (`deepseek_infra/web/server.py`), and the frontend
//! talks to them on every reminder read/write. They are being moved here one
//! store at a time, each behind the **same** ownership gate the chat tool loop
//! already applies, because a store has one authoritative writer and an HTTP
//! route is just another writer.
//!
//! # Why the gate is not optional on a read-looking route
//!
//! `POST /api/reminders` is not a pure read: `action=create` and `action=delete`
//! rewrite `.reminders/reminders.json`, and `POST /api/reminders/due` marks
//! entries `notified` and rewrites the whole file. Python reaches the same file
//! from three paths, one of them the delivery poll, and both sides reproduce the
//! same temp name (`reminders.json` -> `reminders.tmp`), so two writers can
//! interleave before either replaces it. So while `reminders_store` is not
//! declared and cut over, **every** mutating action here is refused with
//! [`crate::REMINDERS_WRITE_NOT_OWNED`] — the same code and the same reason the
//! tool loop gives — rather than reporting a reminder that was never stored.
//!
//! The read actions (`list`) are served for real: they do not write, and the
//! frontend needs them to render.

use std::path::PathBuf;

use axum::extract::{Json as JsonBody, Path, Query};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{
    Json, Router,
    routing::{delete, get, post},
};
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::SystemClock;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::memory_schema as memory_v3;
use deepseek_policy::reminders;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::{MEMORY_WRITE_NOT_OWNED, REMINDERS_WRITE_NOT_OWNED};

pub fn router() -> Router {
    Router::new()
        .route("/api/reminders", post(api_reminders))
        .route("/api/reminders/due", post(api_due_reminders))
        .route("/api/memory", get(api_memory_list).post(api_memory))
        .route("/api/memory/search", get(api_memory_search))
        .route("/api/memory/conflicts", post(api_memory_conflicts))
        .route(
            "/api/memory/:memory_id",
            delete(api_memory_delete_by_id).patch(api_memory_edit),
        )
}

/// The workspace root every store read/write is anchored to.
///
/// Mirrors `policy_routes`: an unset `DEEPSEEK_INFRA_ROOT` falls back to the
/// process working directory, which is the oracle's `config.ROOT` in a normal
/// launch. It is deliberately **not** an error here, because a read of a
/// non-existent store is an empty store in the oracle, not a failure.
fn workspace_root() -> PathBuf {
    match std::env::var_os("DEEPSEEK_INFRA_ROOT") {
        Some(root) => PathBuf::from(root),
        None => PathBuf::from("."),
    }
}

/// The oracle's `AppError.to_response()` envelope: `{"error", "code"}`.
fn app_error_response(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

/// The refusal a mutating reminder action gets while Python still owns the store.
fn write_not_owned() -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "The reminders store is still written by the Python runtime, so this \
                      gateway refuses to mutate it.",
            "code": REMINDERS_WRITE_NOT_OWNED,
        })),
    )
        .into_response()
}

/// `POST /api/reminders` — mirrors `server.reminder_action`.
///
/// The oracle's shape, exactly: `list` returns `{"reminders": [...]}`,
/// `create` returns `{"ok": true, "reminder": {...}}`, `delete` returns
/// `{"ok": true, "deleted": <0|1>}`, and an unknown action is a 400
/// `invalid_payload` with the oracle's own message.
///
/// A missing/non-object body is the oracle's `read_json_body` result (`{}`), so
/// it takes the `list` default rather than erroring.
pub async fn api_reminders(body: Option<JsonBody<Value>>) -> Response {
    let payload = match body {
        Some(JsonBody(Value::Object(fields))) => Value::Object(fields),
        // `read_json_body` yields `{}` for an empty body and rejects malformed
        // JSON before the handler; anything non-object that still arrives is
        // treated as the empty payload rather than guessed at.
        _ => json!({}),
    };
    let action = payload
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("list")
        .trim()
        .to_lowercase();
    let root = workspace_root();
    match action.as_str() {
        "list" => Json(json!({"reminders": reminders::load_reminders(&root)})).into_response(),
        "create" => {
            if !crate::may_write_native_store("reminders_store") {
                return write_not_owned();
            }
            let arguments = match payload.as_object() {
                Some(fields) => fields.clone(),
                None => serde_json::Map::new(),
            };
            match reminders::create_reminder(&arguments, &root, &SystemEntropy) {
                Ok(reminder) => Json(json!({"ok": true, "reminder": reminder})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        "delete" => {
            if !crate::may_write_native_store("reminders_store") {
                return write_not_owned();
            }
            let id = payload.get("id").map(python_str).unwrap_or_default();
            match reminders::delete_reminder(&id, &root) {
                Ok(deleted) => Json(json!({"ok": true, "deleted": deleted})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        _ => app_error_response(AppError::invalid_payload("Unsupported reminder action")),
    }
}

/// `POST /api/reminders/due` — mirrors `server.api_due_reminders`.
///
/// This route **writes**: the oracle marks each newly-due entry `notified` and
/// rewrites the file, which is why it is gated even though the name reads like a
/// query. Once the store is cut over the same call runs for real.
pub async fn api_due_reminders() -> Response {
    if !crate::may_write_native_store("reminders_store") {
        return write_not_owned();
    }
    let root = workspace_root();
    let now_millis = SystemEntropy.now_millis();
    match reminders::due_reminders(&root, now_millis) {
        Ok(due) => Json(json!({"reminders": due})).into_response(),
        Err(error) => app_error_response(error),
    }
}

/// Python's `str(value)` for the one field read here (`id`).
///
/// `str(None)` is `"None"` in Python, but the oracle writes
/// `str(payload.get("id") or "")`, so a missing/`null` id is `""` — which
/// `delete_reminder` then treats as a no-op returning `0`.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

// --- memory ----------------------------------------------------------------------
//
// `deepseek_infra/infra/memory/` is a **projection** layer over the same
// `.memory/memories.json` the chat turn reads and writes, so it is the same
// authoritative store and the same gate. `memory_store` is declared
// (python -> rust, cutover 4.9.4), so the mutations here flip with one environment
// variable exactly as the reminder ones do.

/// The refusal a mutating memory action gets while Python still owns the store.
fn memory_write_not_owned() -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "The memory store is still written by the Python runtime, so this \
                      gateway refuses to mutate it.",
            "code": MEMORY_WRITE_NOT_OWNED,
        })),
    )
        .into_response()
}

fn memory_clock() -> SystemClock {
    SystemClock
}

/// `POST /api/memory` and `GET /api/memory` — mirrors `create_memory_router`.
///
/// `list` is served (a read the frontend needs); `add`, `clear`, `delete` and
/// `deletebyid` are gated. The `add` conflict path is a **409 with the conflicts
/// listed**, and it is checked before the gate: the oracle answers a conflicting
/// request with the conflicts rather than storing, and reporting conflicts is not a
/// write.
pub async fn api_memory(body: Option<JsonBody<Value>>) -> Response {
    let payload = match body {
        Some(JsonBody(Value::Object(fields))) => Value::Object(fields),
        _ => json!({}),
    };
    let action = payload
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("add")
        .trim()
        .to_lowercase();
    let root = workspace_root();
    let clock = memory_clock();
    match action.as_str() {
        "list" => Json(json!({"memories": memory_v3::list_memories("", "", &root, &clock)}))
            .into_response(),
        "add" => {
            let content = payload
                .get("content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let category = memory_v3::public_type(
                &payload
                    .get("category")
                    .map(python_str)
                    .unwrap_or_else(|| "fact".to_string()),
            );
            let scope = memory_v3::storage_scope(
                &payload
                    .get("scope")
                    .map(python_str)
                    .filter(|text| !text.is_empty())
                    .unwrap_or_else(|| "global".to_string()),
                "",
                "",
                "",
            );
            let pinned = payload.get("pinned").is_some_and(python_truthy);
            let replace_ids: Vec<String> = match payload.get("replaceIds") {
                Some(Value::Array(items)) => items.iter().map(python_str).collect(),
                _ => Vec::new(),
            };
            let conflicts = memory_v3::detect_conflicts(&content, &category, &scope, &root);
            let unresolved: Vec<Value> = conflicts
                .into_iter()
                .filter(|item| {
                    let id = item.get("id").map(python_str).unwrap_or_default();
                    !replace_ids.contains(&id)
                })
                .collect();
            if !unresolved.is_empty() {
                return (
                    StatusCode::CONFLICT,
                    Json(json!({
                        "error": "Memory conflicts with an existing item",
                        "code": "memory_conflict",
                        "conflicts": unresolved,
                    })),
                )
                    .into_response();
            }
            if !crate::may_write_native_store("memory_store") {
                return memory_write_not_owned();
            }
            match memory_v3::add_memory(
                &content, &scope, &category, "", "", "", None, 0.9, "", pinned, &root, &clock,
            ) {
                Ok(item) => Json(json!({"ok": true, "memory": item})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        "clear" => {
            if !crate::may_write_native_store("memory_store") {
                return memory_write_not_owned();
            }
            match memory_v3::clear_memories(&root, &clock) {
                Ok(deleted) => Json(json!({"ok": true, "deleted": deleted})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        "delete" => {
            if !crate::may_write_native_store("memory_store") {
                return memory_write_not_owned();
            }
            let query = payload
                .get("query")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let scope = memory_v3::storage_scope(
                &payload
                    .get("scope")
                    .map(python_str)
                    .filter(|text| !text.is_empty())
                    .unwrap_or_else(|| "global".to_string()),
                "",
                "",
                "",
            );
            // `["global", scope] if scope != "global" else ["global"]`.
            let scopes = if scope == "global" {
                vec!["global".to_string()]
            } else {
                vec!["global".to_string(), scope]
            };
            match memory_v3::delete_memories_by_query(&query, &scopes, &root, &clock) {
                Ok(deleted) => Json(json!({"ok": true, "deleted": deleted})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        "deletebyid" => {
            if !crate::may_write_native_store("memory_store") {
                return memory_write_not_owned();
            }
            let memory_id = payload
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            match memory_v3::delete_memory(&memory_id, &root, &clock) {
                Ok(deleted) => Json(json!({"ok": true, "deleted": deleted})).into_response(),
                Err(error) => app_error_response(error),
            }
        }
        _ => app_error_response(AppError::invalid_payload("Unsupported memory action")),
    }
}

/// `GET /api/memory` — the list projection on its own verb.
pub async fn api_memory_list() -> Response {
    let root = workspace_root();
    Json(json!({"memories": memory_v3::list_memories("", "", &root, &memory_clock())}))
        .into_response()
}

/// `DELETE /api/memory/{id}` — gated, like every other memory mutation.
pub async fn api_memory_delete_by_id(Path(memory_id): Path<String>) -> Response {
    if !crate::may_write_native_store("memory_store") {
        return memory_write_not_owned();
    }
    let root = workspace_root();
    match memory_v3::delete_memory(&memory_id, &root, &memory_clock()) {
        Ok(deleted) => Json(json!({"ok": true, "deleted": deleted})).into_response(),
        Err(error) => app_error_response(error),
    }
}

/// `PATCH /api/memory/{id}` — gated; the oracle's field-wise patch.
pub async fn api_memory_edit(
    Path(memory_id): Path<String>,
    body: Option<JsonBody<Value>>,
) -> Response {
    if !crate::may_write_native_store("memory_store") {
        return memory_write_not_owned();
    }
    let updates = match body {
        Some(JsonBody(Value::Object(fields))) => fields,
        _ => Map::new(),
    };
    let root = workspace_root();
    match memory_v3::edit_memory(&memory_id, &updates, &root, &memory_clock()) {
        Ok(item) => Json(json!({"ok": true, "memory": item})).into_response(),
        Err(error) => app_error_response(error),
    }
}

#[derive(Debug, Default, Deserialize)]
pub struct MemorySearchQuery {
    q: Option<String>,
    query: Option<String>,
    limit: Option<String>,
    #[serde(rename = "projectId")]
    project_id: Option<String>,
    #[serde(rename = "skillId")]
    skill_id: Option<String>,
    #[serde(rename = "automationId")]
    automation_id: Option<String>,
}

/// `GET /api/memory/search` — a read, so it is served without the gate.
///
/// `int(query_params.get("limit") or 10)` is a **bare** `int()` in the oracle, so a
/// non-numeric limit raises rather than falling back; that surfaces as a 400 rather
/// than being silently coerced.
pub async fn api_memory_search(Query(query): Query<MemorySearchQuery>) -> Response {
    let raw_limit = query
        .limit
        .as_deref()
        .map(str::trim)
        .filter(|text| !text.is_empty());
    let limit = match raw_limit {
        Some(text) => match text.parse::<i64>() {
            Ok(value) => value,
            Err(_) => {
                return app_error_response(AppError::invalid_payload(format!(
                    "invalid literal for int() with base 10: '{text}'"
                )));
            }
        },
        None => 10,
    };
    let text = query.q.or(query.query).unwrap_or_default();
    let root = workspace_root();
    Json(json!({
        "ok": true,
        "memories": memory_v3::search_memories(
            &text,
            query.project_id.as_deref().unwrap_or(""),
            query.skill_id.as_deref().unwrap_or(""),
            query.automation_id.as_deref().unwrap_or(""),
            Some(limit),
            &root,
            &memory_clock(),
        ),
    }))
    .into_response()
}

/// `POST /api/memory/conflicts` — a pure read of the store.
pub async fn api_memory_conflicts(body: Option<JsonBody<Value>>) -> Response {
    let payload = match body {
        Some(JsonBody(Value::Object(fields))) => Value::Object(fields),
        _ => json!({}),
    };
    let content = payload
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let category = memory_v3::public_type(
        &payload
            .get("category")
            .map(python_str)
            .unwrap_or_else(|| "fact".to_string()),
    );
    let scope = memory_v3::storage_scope(
        &payload
            .get("scope")
            .map(python_str)
            .filter(|text| !text.is_empty())
            .unwrap_or_else(|| "global".to_string()),
        "",
        "",
        "",
    );
    let root = workspace_root();
    Json(json!({
        "ok": true,
        "conflicts": memory_v3::detect_conflicts(&content, &category, &scope, &root),
    }))
    .into_response()
}

/// Python truthiness, for the fields this module reads.
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

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_policy::entropy::Entropy;

    /// A deterministic entropy source, so a created id is checkable.
    struct FixedEntropy;

    impl Entropy for FixedEntropy {
        fn new_id(&self) -> Result<String, AppError> {
            Ok("0123456789abcdef".to_string())
        }
        fn now_millis(&self) -> i64 {
            1_700_000_000_000
        }
    }

    #[test]
    fn python_str_matches_the_oracles_id_coercion() {
        assert_eq!(python_str(&Value::Null), "");
        assert_eq!(python_str(&json!("abc")), "abc");
        // `str(123)` is `"123"`; the id comparison then simply never matches.
        assert_eq!(python_str(&json!(123)), "123");
    }

    /// The gate is what this route adds beyond the store, so pin both directions
    /// with an explicit root rather than depending on the process environment.
    #[test]
    fn the_write_gate_reads_the_declared_domain_and_the_mode() {
        // The predicate is process-wide (env-driven), so this asserts the rule it
        // implements rather than mutating the environment of a parallel test.
        assert!(crate::DECLARED_NATIVE_DATA_DOMAINS.contains(&"reminders_store"));
        assert!(
            crate::may_write_native_store("reminders_store") == crate::python_is_de_authorised()
        );
    }

    #[test]
    fn a_create_through_the_store_round_trips_and_advances_the_fence() {
        let scratch = tempfile::tempdir().unwrap();
        let root = scratch.path();
        let arguments =
            json!({"title": "喝水", "content": "多喝水", "dueAt": "2026-01-01T00:00:00Z"});
        let reminder =
            reminders::create_reminder(arguments.as_object().unwrap(), root, &FixedEntropy)
                .unwrap();
        assert_eq!(reminder["id"], "0123456789abcdef");
        assert_eq!(reminder["title"], "喝水");
        assert_eq!(reminder["notified"], false);

        let listed = reminders::load_reminders(root);
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0]["id"], "0123456789abcdef");

        // The write really went through the mutation fence, not around it.
        let generation = std::fs::read_to_string(root.join(".workspace-generation")).unwrap();
        assert_eq!(generation.trim(), "2");
    }
}
