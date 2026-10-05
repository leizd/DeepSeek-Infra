//! `GET /api/workspace/disaster-recovery/replication`.
//!
//! This is `backup_replication.list_jobs` with the route's `policyId`,
//! `backupId`, and `limit=100`. `.backup-replication` stays the Python worker's
//! directory. A missing directory answers `{"jobs":[]}` and is not created.
//! This handler never writes a job, a cursor, or a schema. There is no write to
//! gate: a reader that created the directory would become a second writer.

use std::path::PathBuf;

use axum::extract::Query;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{Json, Router, routing::get};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde::Deserialize;
use serde_json::{Value, json};

const LIST_LIMIT: usize = 100;

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/disaster-recovery/replication",
        get(list_jobs),
    )
}

#[derive(Debug, Deserialize, Default)]
struct ReplicationQuery {
    #[serde(rename = "policyId")]
    policy_id: Option<String>,
    #[serde(rename = "backupId")]
    backup_id: Option<String>,
}

async fn list_jobs(Query(query): Query<ReplicationQuery>) -> Response {
    // `query.get(name) or ""` then `or None`: a missing or empty value is no filter.
    // Whitespace is kept and does filter. `phase` is not a parameter of this route.
    let policy_id = blank_to_none(query.policy_id);
    let backup_id = blank_to_none(query.backup_id);
    match tokio::task::spawn_blocking(move || read_jobs(policy_id, backup_id)).await {
        Ok(Ok(jobs)) => Json(json!({"jobs": jobs})).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(server_error()),
    }
}

fn blank_to_none(value: Option<String>) -> Option<String> {
    value.filter(|text| !text.is_empty())
}

fn read_jobs(policy_id: Option<String>, backup_id: Option<String>) -> Result<Vec<Value>, AppError> {
    let dir = replication_dir();
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut names = Vec::new();
    for entry in std::fs::read_dir(&dir).map_err(|_| server_error())? {
        let entry = entry.map_err(|_| server_error())?;
        let Some(name) = entry.file_name().into_string().ok() else {
            continue;
        };
        // `glob("*.json")` is non-recursive and, on Windows, case-insensitive.
        // Names that start with `.` are then skipped. A directory called
        // `foo.json` is an `OSError` in Python and is skipped.
        if name.starts_with('.') || !is_json_name(&name) || !entry.path().is_file() {
            continue;
        }
        names.push(name);
    }
    names.sort_by(|left, right| name_order(right, left));

    let mut jobs = Vec::new();
    for name in names {
        let path = dir.join(&name);
        let bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(_) => continue,
        };
        // `read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which
        // `list_jobs` does not catch. Invalid JSON and `OSError` skip the file.
        let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
        let data: Value = match serde_json::from_str(text) {
            Ok(value) => value,
            Err(_) => continue,
        };
        if !include_job(&data, policy_id.as_deref(), backup_id.as_deref()) {
            continue;
        }
        jobs.push(data);
        if jobs.len() >= LIST_LIMIT {
            break;
        }
    }
    Ok(jobs)
}

fn include_job(data: &Value, policy_id: Option<&str>, backup_id: Option<&str>) -> bool {
    let Value::Object(_) = data else {
        return false;
    };
    // `str(data.get("jobId", ""))`, not `or ""`. Null renders as "None" and stays
    // in the body; only a missing or empty string id is dropped.
    let job_id = data.get("jobId").map(value_str).unwrap_or_default();
    if job_id.is_empty() {
        return false;
    }
    if let Some(expected) = policy_id {
        if filter_str(data.get("policyId")) != expected {
            return false;
        }
    }
    if let Some(expected) = backup_id {
        if filter_str(data.get("backupId")) != expected {
            return false;
        }
    }
    true
}

/// `str(value or "")`. Falsy JSON (`null`, `false`, `0`, `""`, `[]`, `{}`) is `""`.
fn filter_str(value: Option<&Value>) -> String {
    match value {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

fn is_json_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    if bytes.len() < 5 {
        return false;
    }
    let suffix = &bytes[bytes.len() - 5..];
    #[cfg(windows)]
    {
        suffix.eq_ignore_ascii_case(b".json")
    }
    #[cfg(not(windows))]
    {
        suffix == b".json"
    }
}

/// `sorted(paths, reverse=True)`. The parent is identical, so the file name
/// decides. Windows `pathlib` compares casefolded parts.
fn name_order(left: &str, right: &str) -> std::cmp::Ordering {
    name_key(left)
        .cmp(&name_key(right))
        .then_with(|| left.cmp(right))
}

fn name_key(name: &str) -> std::borrow::Cow<'_, str> {
    #[cfg(windows)]
    {
        std::borrow::Cow::Owned(name.to_lowercase())
    }
    #[cfg(not(windows))]
    {
        std::borrow::Cow::Borrowed(name)
    }
}

fn replication_dir() -> PathBuf {
    crate::data_routes::workspace_root().join(".backup-replication")
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
