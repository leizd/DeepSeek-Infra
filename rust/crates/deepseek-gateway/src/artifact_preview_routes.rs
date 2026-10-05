//! `GET /api/workspace/artifacts/{artifact_id}/preview`.
//!
//! `artifacts.preview_artifact`. A missing `artifacts.json` is an empty list, so a
//! well-formed id is `404 Artifact not found`. The read does not create
//! `.projects`. An explicit `projectId` searches only that project, even when
//! `project.json` is absent. An empty `projectId` scans `list_projects` (newest
//! `updatedAt` first) and therefore only projects that already have `project.json`.
//!
//! This path has no other Python method. `HEAD` follows Starlette and returns
//! the GET status with an empty body. Any other method is `405` with FastAPI's
//! `{"detail": "Method Not Allowed"}`. `GET .../download` stays on the Go proxy.

use std::path::{Path, PathBuf};

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path as AxumPath};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{python_int_opt, python_truthy};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::generated_files::generated_dir;
use deepseek_policy::projects::{self, read_project};
use deepseek_policy::workspace_schema::{
    normalize_artifact_type, normalize_source_ref, normalize_title, python_str,
    redact_sensitive_text, resolve_runtime_path, runtime_relative_path, timestamp_ms_to_iso,
    validate_project_id, validate_workspace_id,
};
use serde_json::{Map, Number, Value, json};

const MAX_ARTIFACTS: usize = 500;
const MAX_ARTIFACT_PREVIEW_CHARS: usize = 100_000;
const TEXT_PREVIEW_TYPES: [&str; 7] = ["svg", "markdown", "md", "csv", "json", "html", "txt"];

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/artifacts/:artifact_id/preview",
        any(dispatch),
    )
}

async fn dispatch(
    method: Method,
    AxumPath(artifact_id): AxumPath<String>,
    OriginalUri(uri): OriginalUri,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return preview_response(method, artifact_id, project_query(&uri)).await;
    }
    method_not_allowed()
}

async fn preview_response(method: Method, artifact_id: String, project_id: String) -> Response {
    let result = match tokio::task::spawn_blocking(move || preview(&artifact_id, &project_id)).await
    {
        Ok(result) => result,
        Err(_) => Err(server_error()),
    };
    match result {
        Ok(_) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => Json(body).into_response(),
        Err(error) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Err(error) => error_response(error),
    }
}

/// `str(query.get("projectId") or "")`. An empty value scans every project.
fn project_query(uri: &Uri) -> String {
    first_query(uri.query().unwrap_or(""), "projectId").unwrap_or_default()
}

fn first_query(query: &str, wanted: &str) -> Option<String> {
    if query.is_empty() {
        return None;
    }
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if percent_decode_query(key) == wanted {
            return Some(percent_decode_query(value));
        }
    }
    None
}

fn percent_decode_query(raw: &str) -> String {
    let spaced = raw.replace('+', " ");
    percent_encoding::percent_decode_str(&spaced)
        .decode_utf8_lossy()
        .into_owned()
}

fn preview(artifact_id: &str, project_id: &str) -> Result<Value, AppError> {
    let safe_artifact_id = validate_workspace_id(artifact_id, "artifact id")?;
    let root = crate::data_routes::workspace_root();
    let projects = project_ids(&root, project_id)?;
    for project_id in projects {
        for artifact in load_artifacts(&root, &project_id)? {
            if artifact.get("artifactId").and_then(Value::as_str) == Some(safe_artifact_id.as_str())
            {
                return preview_body(&root, &artifact);
            }
        }
    }
    Err(AppError::not_found("Artifact not found"))
}

/// Explicit `projectId` is that one id. An empty value is `list_projects`.
fn project_ids(root: &Path, project_id: &str) -> Result<Vec<String>, AppError> {
    if !project_id.is_empty() {
        return Ok(vec![validate_project_id(project_id)?]);
    }
    scan_projects(root)
}

/// `data.projects.list_projects`: invalid directory names raise, a missing
/// `project.json` is skipped, and `public_project`'s `int(updatedAt)` is the
/// sort key. Newest first. Equal timestamps keep directory order.
fn scan_projects(root: &Path) -> Result<Vec<String>, AppError> {
    let directory = projects::projects_dir(root);
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut rows: Vec<(i64, usize, String)> = Vec::new();
    let mut index = 0usize;
    for entry in std::fs::read_dir(&directory).map_err(|_| server_error())? {
        let entry = entry.map_err(|_| server_error())?;
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        // `read_project` raises `Invalid project id` for a bad directory name.
        let safe_id = projects::validate_project_id(&name)?;
        let file = path.join("project.json");
        if !file.exists() {
            continue;
        }
        let bytes = match std::fs::read(&file) {
            Ok(bytes) => bytes,
            Err(_) => continue,
        };
        // `read_text(encoding="utf-8")` is not caught by `read_project`.
        if std::str::from_utf8(&bytes).is_err() {
            return Err(server_error());
        }
        let Some(project) = read_project(&safe_id, root, &SystemEntropy)? else {
            continue;
        };
        // `public_project` evaluates `createdAt` first, then `updatedAt`.
        // A value `int()` rejects is an unhandled exception, not zero.
        let _created = public_clock(project.get("createdAt"))?;
        let updated = public_clock(project.get("updatedAt"))?;
        rows.push((updated, index, safe_id));
        index += 1;
    }
    rows.sort_by(|left, right| right.0.cmp(&left.0).then(left.1.cmp(&right.1)));
    Ok(rows.into_iter().map(|(_, _, id)| id).collect())
}

fn public_clock(value: Option<&Value>) -> Result<i64, AppError> {
    coerced_int(value, 0).map_err(|_| server_error())
}

fn load_artifacts(root: &Path, project_id: &str) -> Result<Vec<Value>, AppError> {
    let path = projects::projects_dir(root)
        .join(project_id)
        .join("artifacts.json");
    let Some(value) = read_store_json(&path)? else {
        return Ok(Vec::new());
    };
    let Some(items) = value.get("artifacts").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut artifacts = Vec::new();
    for raw in items {
        let Some(raw) = raw.as_object() else {
            continue;
        };
        if let Some(artifact) = normalize_artifact(root, project_id, raw)? {
            artifacts.push(artifact);
        }
    }
    if artifacts.len() > MAX_ARTIFACTS {
        let drop_count = artifacts.len() - MAX_ARTIFACTS;
        artifacts.drain(0..drop_count);
    }
    Ok(artifacts)
}

fn normalize_artifact(
    root: &Path,
    project_id: &str,
    raw: &Map<String, Value>,
) -> Result<Option<Value>, AppError> {
    let (created_at, updated_at, version) = load_clock(raw);
    let artifact_id = record_id(raw);
    if artifact_id.is_empty() {
        return Ok(None);
    }
    let (generated, projects_dir, workspace) = roots(root);
    let rel_path = runtime_relative_path(
        &stored_path(raw.get("path")),
        &generated,
        &projects_dir,
        &workspace,
    )?;
    let stored_project = match raw.get("projectId").filter(|value| python_truthy(value)) {
        Some(value) => python_str(Some(value)),
        None => project_id.to_string(),
    };
    let safe_project = validate_project_id(&stored_project)?;
    let artifact_type = normalize_artifact_type(raw.get("type"), &rel_path)?;
    let versions = normalize_versions(root, raw.get("versions"), &rel_path, version, created_at)?;
    let download_url =
        format!("/api/workspace/artifacts/{artifact_id}/download?projectId={safe_project}");
    Ok(Some(json!({
        "artifactId": artifact_id,
        "projectId": safe_project,
        "type": artifact_type,
        "title": normalize_title(raw.get("title"), "Artifact"),
        "path": rel_path,
        "source": normalize_source_ref(raw.get("source").unwrap_or(&Value::Null)),
        "version": version,
        "versions": versions,
        "createdAt": display_time(raw.get("createdAt"), created_at),
        "updatedAt": display_time(raw.get("updatedAt"), updated_at),
        "createdAtMs": created_at,
        "updatedAtMs": updated_at,
        "downloadUrl": download_url,
    })))
}

/// `int(createdAtMs or 0)`, then `int(updatedAtMs or created)`, then `int(version or 1)`.
/// Any failure resets all three, including a created value that already parsed.
fn load_clock(raw: &Map<String, Value>) -> (i64, i64, i64) {
    let parsed = (|| {
        let created_at = coerced_int(raw.get("createdAtMs"), 0)?;
        let updated_at = coerced_int(raw.get("updatedAtMs"), created_at)?;
        let version = coerced_int(raw.get("version"), 1)?;
        Ok::<_, ()>((created_at, updated_at, version))
    })();
    parsed.unwrap_or((0, 0, 1))
}

fn record_id(raw: &Map<String, Value>) -> String {
    if let Some(value) = raw.get("artifactId").filter(|value| python_truthy(value)) {
        return python_str(Some(value));
    }
    if let Some(value) = raw.get("id").filter(|value| python_truthy(value)) {
        return python_str(Some(value));
    }
    String::new()
}

fn stored_path(value: Option<&Value>) -> String {
    // `runtime_relative_path` raises when the string is empty. A missing path is
    // empty after the truthiness `or`, not `str(None)`.
    match value.filter(|value| python_truthy(value)) {
        Some(value) => python_str(Some(value)),
        None => String::new(),
    }
}

fn normalize_versions(
    root: &Path,
    value: Option<&Value>,
    rel_path: &str,
    version: i64,
    created_at: i64,
) -> Result<Vec<Value>, AppError> {
    let Some(Value::Array(items)) = value else {
        return Ok(vec![default_version(rel_path, version, created_at)]);
    };
    if items.is_empty() {
        return Ok(vec![default_version(rel_path, version, created_at)]);
    }
    let mut versions = Vec::new();
    for raw in items {
        let Some(raw) = raw.as_object() else {
            continue;
        };
        let (raw_version, raw_created) = (|| {
            let raw_version = coerced_int(raw.get("version"), 1)?;
            let raw_created = coerced_int(raw.get("createdAtMs"), created_at)?;
            Ok::<_, ()>((raw_version, raw_created))
        })()
        .unwrap_or((1, created_at));
        let path_text = match raw.get("path").filter(|value| python_truthy(value)) {
            Some(value) => python_str(Some(value)),
            None => rel_path.to_string(),
        };
        let (generated, projects_dir, workspace) = roots(root);
        let path = runtime_relative_path(&path_text, &generated, &projects_dir, &workspace)?;
        versions.push(json!({
            "version": raw_version,
            "path": path,
            "createdAt": display_time(raw.get("createdAt"), raw_created),
            "createdAtMs": raw_created,
        }));
    }
    Ok(versions)
}

fn default_version(rel_path: &str, version: i64, created_at: i64) -> Value {
    json!({
        "version": version,
        "path": rel_path,
        "createdAt": display_time(None, created_at),
        "createdAtMs": created_at,
    })
}

fn display_time(value: Option<&Value>, millis: i64) -> String {
    if value.is_some_and(python_truthy) {
        python_str(value)
    } else {
        timestamp_ms_to_iso(Some(&Value::Number(Number::from(millis))))
    }
}

fn preview_body(root: &Path, artifact: &Value) -> Result<Value, AppError> {
    let (generated, projects, workspace) = roots(root);
    let path = resolve_runtime_path(
        artifact.get("path").and_then(Value::as_str).unwrap_or(""),
        &generated,
        &projects,
        &workspace,
    )?;
    if !path.is_file() {
        return Err(AppError::not_found("Artifact file not found"));
    }
    let artifact_type = artifact
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    let suffix = path
        .extension()
        .map(|extension| extension.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let text_preview = TEXT_PREVIEW_TYPES.contains(&artifact_type.as_str())
        || TEXT_PREVIEW_TYPES.contains(&suffix.as_str());
    if !text_preview {
        let bytes = file_len(&path)?;
        return Ok(json!({
            "ok": true,
            "artifact": artifact,
            "previewAvailable": false,
            "content": "",
            "bytes": bytes,
        }));
    }
    let raw = std::fs::read(&path).map_err(|_| server_error())?;
    let text = String::from_utf8_lossy(&raw);
    let content: String = text.chars().take(MAX_ARTIFACT_PREVIEW_CHARS).collect();
    let bytes = file_len(&path)?;
    Ok(json!({
        "ok": true,
        "artifact": artifact,
        "previewAvailable": true,
        "content": redact_sensitive_text(&content),
        "bytes": bytes,
    }))
}

fn file_len(path: &Path) -> Result<u64, AppError> {
    std::fs::metadata(path)
        .map(|metadata| metadata.len())
        .map_err(|_| server_error())
}

fn roots(root: &Path) -> (PathBuf, PathBuf, PathBuf) {
    (
        generated_dir(root),
        projects::projects_dir(root),
        root.to_path_buf(),
    )
}

/// Missing path, `OSError` (including a directory), invalid JSON, and a non-object
/// are the empty default. Invalid UTF-8 is not caught.
fn read_store_json(path: &Path) -> Result<Option<Value>, AppError> {
    if !path.exists() {
        return Ok(None);
    }
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(None),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    match serde_json::from_str::<Value>(text) {
        Ok(value @ Value::Object(_)) => Ok(Some(value)),
        _ => Ok(None),
    }
}

fn coerced_int(value: Option<&Value>, fallback: i64) -> Result<i64, ()> {
    let chosen = match value {
        Some(value) if python_truthy(value) => value.clone(),
        _ => Value::Number(Number::from(fallback)),
    };
    python_int_opt(Some(&chosen)).ok_or(())
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
