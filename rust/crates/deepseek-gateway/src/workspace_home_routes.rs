//! `GET /api/workspace/home`.
//!
//! `workspace_home.workspace_home` is a read. It does not create `.projects`,
//! `.automation`, `.media`, `.memory`, or an export directory, and it does not
//! require `DEEPSEEK_RUNTIME_MODE=python_disabled`. A per-project saved-item,
//! artifact, or skill-run failure is skipped. Invalid UTF-8 in a store that the
//! oracle does not catch is `500 {"error":"Server error","code":"internal"}`.
//!
//! `limit` is `int(query or 8)` in the route, then `int(limit or 8)` inside
//! `workspace_home`, then clamped to 1..50. `0` is falsy on that second
//! conversion, so the query `"0"` is 8. A non-integer is the route's unhandled
//! `ValueError` and never touches a store.
//!
//! `HEAD` follows Starlette: the GET status with an empty body. Any other
//! method is `405` with `Allow: GET, HEAD` and does not read a store.
//! `recent.automations` is `history.list_runs(limit=safe_limit)`, already
//! truncated, so `counts.automationRuns` is that window. `counts.automations`
//! is the full definition count.

use std::path::Path;

use axum::extract::Query;
use axum::http::{HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{SystemClock, python_int_opt, python_truthy};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::generated_files::generated_dir;
use deepseek_policy::memory_schema;
use deepseek_policy::projects::{self, MAX_PROJECT_SKILL_RUNS};
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_projects;
use serde::Deserialize;
use serde_json::{Value, json};

const HOME_RUN_CAP: i64 = 2_000;
const SKILL_RUN_PAGE: i64 = 50;

pub fn router() -> Router {
    Router::new().route("/api/workspace/home", any(dispatch))
}

#[derive(Debug, Deserialize, Default)]
struct HomeQuery {
    limit: Option<String>,
}

async fn dispatch(method: Method, Query(query): Query<HomeQuery>) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return method_not_allowed();
    }
    let limit = match home_limit(query.limit.as_deref()) {
        Ok(limit) => limit,
        Err(error) => return method_error(method, error),
    };
    match tokio::task::spawn_blocking(move || workspace_home(limit)).await {
        Ok(Ok(_body)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => method_error(method, error),
        Err(_) => method_error(method, server_error()),
    }
}

/// `int(query or 8)` then `max(1, min(int(limit or 8), 50))`.
fn home_limit(raw: Option<&str>) -> Result<i64, AppError> {
    let parsed = match raw {
        None | Some("") => 8,
        Some(text) => {
            let value =
                python_int_opt(Some(&Value::String(text.to_string()))).ok_or_else(server_error)?;
            if value == 0 { 8 } else { value }
        }
    };
    Ok(parsed.clamp(1, 50))
}

fn workspace_home(limit: i64) -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let window = usize::try_from(limit).unwrap_or(1);
    let projects = workspace_projects::list_projects(&root, &SystemEntropy)?;
    let project_ids: Vec<String> = projects.iter().map(project_id_of).collect();
    let saved_items = collect_saved(&project_ids, &root);
    let artifacts = collect_artifacts(&project_ids, &root);
    let skill_runs = collect_skill_runs(&project_ids, &root);
    let automations = crate::automation_definition_routes::load_automations()?;
    let automation_runs = automation_runs(limit)?;
    let media = crate::media_segment_routes::list_public_media()?;
    let exports = list_exports(&root)?;
    let memories = memory_schema::list_memories("", "", &root, &SystemClock);
    Ok(json!({
        "ok": true,
        "version": crate::gateway_version(),
        "modules": modules(),
        "recent": {
            "projects": take(&projects, window),
            "memories": take(&memories, window),
            "skills": take(&skill_runs, window),
            "media": take(&media, window),
            "automations": take(&automation_runs, window),
            "artifacts": take(&artifacts, window),
            "savedItems": take(&saved_items, window),
            "exports": take(&exports, window),
        },
        "counts": {
            "projects": projects.len(),
            "memories": memories.len(),
            "skills": skill_runs.len(),
            "media": media.len(),
            "automations": automations.len(),
            "automationRuns": automation_runs.len(),
            "artifacts": artifacts.len(),
            "savedItems": saved_items.len(),
            "exports": exports.len(),
        },
        "status": {
            "doctor": "ok",
            "evidence": evidence_status(&root),
            "runtime": "local",
        },
    }))
}

fn modules() -> Vec<Value> {
    [
        ("projects", "Projects"),
        ("memory", "Memory"),
        ("skills", "Skills"),
        ("media", "Media"),
        ("browser", "Browser"),
        ("automations", "Automations"),
        ("artifacts", "Artifacts"),
        ("saved_items", "Saved Items"),
        ("exports", "Exports"),
        ("settings", "Settings"),
    ]
    .into_iter()
    .map(|(id, label)| json!({"id": id, "label": label, "status": "ready"}))
    .collect()
}

fn project_id_of(project: &Value) -> String {
    for key in ["projectId", "id"] {
        if let Some(value) = project.get(key).filter(|value| python_truthy(value)) {
            return value_str(value);
        }
    }
    String::new()
}

fn collect_saved(project_ids: &[String], root: &Path) -> Vec<Value> {
    let mut items = Vec::new();
    for project_id in project_ids {
        if project_id.is_empty() {
            continue;
        }
        match workspace_projects::list_saved_items(project_id, root, "", &[] as &[String]) {
            Ok(found) => items.extend(found),
            Err(_) => continue,
        }
    }
    sort_by_timestamp(&mut items);
    items
}

fn collect_artifacts(project_ids: &[String], root: &Path) -> Vec<Value> {
    let mut items = Vec::new();
    for project_id in project_ids {
        if project_id.is_empty() {
            continue;
        }
        match workspace_projects::list_artifacts(project_id, root) {
            Ok(found) => items.extend(found),
            Err(_) => continue,
        }
    }
    sort_by_timestamp(&mut items);
    items
}

/// `list_project_skill_runs(project_id, limit=50)`, then the home timestamp sort.
fn collect_skill_runs(project_ids: &[String], root: &Path) -> Vec<Value> {
    let page = skill_run_page();
    let mut runs = Vec::new();
    for project_id in project_ids {
        if project_id.is_empty() {
            continue;
        }
        let Ok(project) = projects::require_project(project_id, root, &SystemEntropy) else {
            continue;
        };
        let Ok(mut loaded) =
            projects::normalize_skill_runs(project.get("skillRuns"), &SystemEntropy)
        else {
            continue;
        };
        loaded.truncate(page);
        runs.extend(loaded);
    }
    sort_by_timestamp(&mut runs);
    runs
}

fn skill_run_page() -> usize {
    SKILL_RUN_PAGE.clamp(1, MAX_PROJECT_SKILL_RUNS as i64) as usize
}

/// `history.list_runs(limit=safe_limit)`: sort by `startedAtMs` descending and
/// slice. Home never passes `0`, which would mean "no cap".
fn automation_runs(limit: i64) -> Result<Vec<Value>, AppError> {
    let mut runs = crate::automation_run_routes::load_runs()?;
    runs.sort_by_key(|run| std::cmp::Reverse(started_at_ms(run)));
    let cap = limit.clamp(0, HOME_RUN_CAP);
    if cap > 0 {
        runs.truncate(usize::try_from(cap).unwrap_or(0));
    }
    Ok(runs)
}

fn started_at_ms(run: &Value) -> i64 {
    match run.get("startedAtMs") {
        Some(value) if python_truthy(value) => value.as_i64().unwrap_or(0),
        _ => 0,
    }
}

fn list_exports(root: &Path) -> Result<Vec<Value>, AppError> {
    let mut items = export_rows(
        &generated_dir(root)
            .join("workspace-exports")
            .join("exports.json"),
    )?;
    for project in projects::list_projects(root, &SystemEntropy)? {
        let project_id = project_id_of(&project);
        if project_id.is_empty() {
            continue;
        }
        let path = root
            .join(".projects")
            .join(&project_id)
            .join("exports")
            .join("exports.json");
        items.extend(export_rows(&path)?);
    }
    items.sort_by_key(|item| std::cmp::Reverse(export_created(item)));
    Ok(items)
}

fn export_rows(path: &Path) -> Result<Vec<Value>, AppError> {
    let data = read_export_object(path)?;
    let Some(rows) = data.get("exports").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    Ok(rows
        .iter()
        .filter(|item| item.is_object())
        .cloned()
        .collect())
}

/// `read_json_file` for an export store. `OSError` and `JSONDecodeError` use
/// the default. `UnicodeDecodeError` is not caught. The file is not created.
fn read_export_object(path: &Path) -> Result<Value, AppError> {
    if !path.exists() {
        return Ok(json!({"exports": []}));
    }
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(json!({"exports": []})),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    match serde_json::from_str::<Value>(text) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Ok(json!({"exports": []})),
    }
}

fn export_created(item: &Value) -> String {
    match item.get("createdAt") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

/// `config.ROOT` is `Path(DEEPSEEK_INFRA_ROOT).resolve()` when that variable
/// is set. The evidence file is `<ROOT>/docs/evidence/ga-v<APP_VERSION>.json`
/// and is not created.
fn evidence_status(root: &Path) -> Value {
    let base = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let path = base
        .join("docs")
        .join("evidence")
        .join(format!("ga-v{}.json", crate::gateway_version()));
    let present = path.exists();
    json!({
        "path": posix_path(&path),
        "present": present,
        "status": if present { "present" } else { "missing" },
    })
}

fn posix_path(path: &Path) -> String {
    let text = path.to_string_lossy();
    let stripped = text
        .strip_prefix(r"\\?\")
        .or_else(|| text.strip_prefix("//?/"))
        .unwrap_or(text.as_ref());
    stripped.replace('\\', "/")
}

fn sort_by_timestamp(items: &mut [Value]) {
    items.sort_by_key(|item| std::cmp::Reverse(sort_timestamp(item)));
}

fn sort_timestamp(item: &Value) -> String {
    for key in ["updatedAt", "createdAt", "finishedAt", "startedAt"] {
        if let Some(value) = item.get(key).filter(|value| python_truthy(value)) {
            return value_str(value);
        }
    }
    String::new()
}

fn take(items: &[Value], limit: usize) -> Vec<Value> {
    items.iter().take(limit).cloned().collect()
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
    response
}

fn method_error(method: Method, error: AppError) -> Response {
    if method == Method::HEAD {
        return StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response();
    }
    error_response(error)
}

fn server_error() -> AppError {
    AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    }
}

fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
