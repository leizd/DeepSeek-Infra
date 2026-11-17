//! Read-only S5 workspace backup and restore inspection routes:
//! - `GET|HEAD /api/workspace/backups/{backup_id}`
//! - `GET|HEAD /api/workspace/backups/{backup_id}/download`
//! - `GET|HEAD /api/workspace/restores`
//! - `GET|HEAD /api/workspace/restores/{restore_id}`
//!
//! Mirrors `deepseek_infra.infra.workspace.backups` (`get_session`, `backup_path`,
//! `list_restores`, `get_restore`) cleanly without creating directories or
//! mutating session/restore files.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::extract::Path as AxumPath;
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::file_routes::content_disposition_header;
use deepseek_policy::python_json::value_str;
use serde_json::{Map, Value, json};

use crate::artifact_preview_routes::{build_download_response, error_response, server_error};
use crate::route_support;

const SECRET_TTL_SECONDS: f64 = 300.0;

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backups/:backup_id",
            any(dispatch_get_backup),
        )
        .route(
            "/api/workspace/backups/:backup_id/download",
            any(dispatch_download_backup),
        )
        .route("/api/workspace/restores", any(dispatch_list_restores))
        .route(
            "/api/workspace/restores/:restore_id",
            any(dispatch_get_restore),
        )
}

async fn dispatch_get_backup(method: Method, AxumPath(backup_id): AxumPath<String>) -> Response {
    if method == Method::DELETE {
        return crate::backup_restore_write_routes::dispatch_delete_backup(backup_id).await;
    }
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        get_session(&root, &backup_id)
    })
    .await;
    json_or_head_response(method, joined)
}

async fn dispatch_download_backup(
    method: Method,
    AxumPath(backup_id): AxumPath<String>,
) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        download_payload(&root, &backup_id)
    })
    .await;
    let result = match joined {
        Ok(res) => res,
        Err(_) => Err(server_error()),
    };
    match result {
        Ok((media_type, disposition, bytes)) => {
            build_download_response(method, &media_type, &disposition, bytes)
        }
        Err(error) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Err(error) => error_response(error),
    }
}

async fn dispatch_list_restores(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        list_restores(&root)
    })
    .await;
    json_or_head_response(method, joined)
}

async fn dispatch_get_restore(method: Method, AxumPath(restore_id): AxumPath<String>) -> Response {
    if method == Method::DELETE {
        return crate::backup_restore_write_routes::dispatch_delete_restore(restore_id).await;
    }
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        get_restore(&root, &restore_id)
    })
    .await;
    json_or_head_response(method, joined)
}

fn json_or_head_response(
    method: Method,
    joined: Result<Result<Value, AppError>, tokio::task::JoinError>,
) -> Response {
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

pub(crate) fn validate_backup_id(backup_id: &str) -> Result<(), AppError> {
    let Some(rest) = backup_id.strip_prefix("backup_") else {
        return Err(AppError::invalid_payload("Invalid backup id"));
    };
    if rest.is_empty() || !rest.chars().all(char::is_alphanumeric) {
        return Err(AppError::invalid_payload("Invalid backup id"));
    }
    Ok(())
}

pub(crate) fn validate_restore_id(restore_id: &str) -> Result<(), AppError> {
    let Some(rest) = restore_id.strip_prefix("restore_") else {
        return Err(AppError::invalid_payload("Invalid restore id"));
    };
    if rest.is_empty() || !rest.chars().all(char::is_alphanumeric) {
        return Err(AppError::invalid_payload("Invalid restore id"));
    }
    Ok(())
}

pub(crate) fn read_json_object(path: &Path) -> Result<Map<String, Value>, AppError> {
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("metadata");
    let raw = std::fs::read_to_string(path)
        .map_err(|_| AppError::invalid_payload(format!("Invalid backup metadata: {file_name}")))?;
    let parsed: Value = serde_json::from_str(&raw)
        .map_err(|_| AppError::invalid_payload(format!("Invalid backup metadata: {file_name}")))?;
    match parsed {
        Value::Object(map) => Ok(map),
        _ => Err(AppError::invalid_payload(format!(
            "Invalid backup metadata: {file_name}"
        ))),
    }
}

pub(crate) fn load_session(root: &Path, backup_id: &str) -> Result<Map<String, Value>, AppError> {
    validate_backup_id(backup_id)?;
    let path = root
        .join(".backups")
        .join("sessions")
        .join(backup_id)
        .join("session.json");
    if !path.is_file() {
        return Err(AppError::not_found("Backup session not found"));
    }
    read_json_object(&path)
}

fn get_session(root: &Path, backup_id: &str) -> Result<Value, AppError> {
    let session = load_session(root, backup_id)?;
    let mut out = Map::new();
    out.insert("ok".into(), Value::Bool(true));
    for (key, value) in session {
        if key != "path" && key != "context" {
            out.insert(key, value);
        }
    }
    Ok(Value::Object(out))
}

fn resolve_backup_file(root: &Path, session: &Map<String, Value>) -> Result<PathBuf, AppError> {
    if session.get("phase").and_then(Value::as_str) != Some("ready") {
        return Err(AppError {
            message: "Backup is not ready".into(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }
    let raw_path = session
        .get("path")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    if raw_path.is_empty() {
        return Err(AppError::not_found("Backup file not found"));
    }
    let backup_dir = root.join(".backups");
    let Ok(resolved_backup_dir) = std::fs::canonicalize(&backup_dir) else {
        return Err(AppError::not_found("Backup file not found"));
    };
    let candidate = PathBuf::from(&raw_path);
    let path = if candidate.is_absolute() {
        candidate
    } else {
        root.join(candidate)
    };
    if !path.is_file() {
        return Err(AppError::not_found("Backup file not found"));
    }
    let Some(parent) = path.parent() else {
        return Err(AppError::not_found("Backup file not found"));
    };
    let Ok(resolved_parent) = std::fs::canonicalize(parent) else {
        return Err(AppError::not_found("Backup file not found"));
    };
    if resolved_parent != resolved_backup_dir {
        return Err(AppError::not_found("Backup file not found"));
    }
    Ok(path)
}

fn download_payload(root: &Path, backup_id: &str) -> Result<(String, String, Vec<u8>), AppError> {
    let session = load_session(root, backup_id)?;
    let path = resolve_backup_file(root, &session)?;
    let bytes = std::fs::read(&path).map_err(|_| server_error())?;
    let filename = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("workspace.dsibackup");
    let media_type = if filename.to_lowercase().ends_with(".age") {
        "application/age"
    } else {
        "application/vnd.deepseek-infra.backup+zip"
    }
    .to_string();
    let disposition = content_disposition_header("attachment", filename);
    Ok((media_type, disposition, bytes))
}

pub(crate) fn restore_root(root: &Path, restore_id: &str) -> Result<PathBuf, AppError> {
    validate_restore_id(restore_id)?;
    let dir = root.join(".restore-staging").join(restore_id);
    if !dir.is_dir() {
        return Err(AppError::not_found("Restore plan not found"));
    }
    Ok(dir)
}

pub(crate) fn now_unix_secs_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn restore_secret_state(metadata: &Map<String, Value>, restore_id: &str) -> &'static str {
    if !metadata.get("encrypted").is_some_and(python_truthy) {
        return "not-required";
    }
    let phase = metadata
        .get("phase")
        .filter(|value| !value.is_null())
        .map(value_str)
        .unwrap_or_default();
    if phase == "locked" {
        return "not-required";
    }
    if !restore_id.is_empty() && crate::backup_secret_slots::has_secret(restore_id) {
        return "available";
    }
    if phase == "inspected" {
        if let Some(armed) = metadata.get("secretArmedAt").and_then(Value::as_f64) {
            if (now_unix_secs_f64() - armed) > SECRET_TTL_SECONDS {
                return "expired";
            }
        }
        return "required-for-safety-backup";
    }
    "not-required"
}

fn public_restore(
    metadata: &Map<String, Value>,
    restore_dir: &Path,
    include_frontend: bool,
) -> Result<Value, AppError> {
    let dir_name = restore_dir
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    let plan_doc = read_json_object(&restore_dir.join("plan.json")).ok();
    let manifest_obj = metadata
        .get("manifest")
        .and_then(Value::as_object)
        .or_else(|| {
            plan_doc
                .as_ref()
                .and_then(|plan| plan.get("manifest"))
                .and_then(Value::as_object)
        });
    let secret_source = if !metadata.get("encrypted").is_some_and(python_truthy)
        && plan_doc
            .as_ref()
            .is_some_and(|plan| plan.get("encrypted").is_some_and(python_truthy))
    {
        plan_doc.as_ref().unwrap_or(metadata)
    } else {
        metadata
    };
    let mut frontend_path = restore_dir
        .join("verified")
        .join("frontend")
        .join("state.json");
    if !frontend_path.is_file() {
        frontend_path = restore_dir
            .join("extracted")
            .join("frontend")
            .join("state.json");
    }

    let mut out = Map::new();
    out.insert("ok".into(), Value::Bool(true));
    for (key, value) in metadata {
        if !matches!(
            key.as_str(),
            "contributors" | "identityMap" | "manifest" | "operations"
        ) {
            out.insert(key.clone(), value.clone());
        }
    }
    let restore_id = metadata
        .get("restoreId")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| dir_name.to_string());
    out.insert("restoreId".into(), Value::String(restore_id.clone()));
    out.insert(
        "secretState".into(),
        Value::String(restore_secret_state(secret_source, &restore_id).to_string()),
    );

    let applied = metadata
        .get("contributors")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    let obj = item.as_object()?;
                    if obj.get("swapped").is_some_and(python_truthy) {
                        Some(obj.get("id").cloned().unwrap_or(Value::Null))
                    } else {
                        None
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    out.insert("applied".into(), Value::Array(applied));

    let source_backup_id = manifest_obj
        .and_then(|manifest| manifest.get("backupId"))
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();
    let mut restored_identities = Vec::new();
    if let Some(identity_map) = metadata.get("identityMap").and_then(Value::as_object) {
        let mut entries: Vec<(&String, &str)> = identity_map
            .iter()
            .filter_map(|(original, restored)| restored.as_str().map(|value| (original, value)))
            .collect();
        entries.sort_by(|left, right| left.0.cmp(right.0));
        for (original, restored) in entries {
            restored_identities.push(json!({
                "originalId": original,
                "restoredId": restored,
                "reason": "collision",
                "sourceBackupId": source_backup_id,
            }));
        }
    }
    out.insert(
        "restoredIdentities".into(),
        Value::Array(restored_identities),
    );

    if include_frontend {
        let frontend_val = if frontend_path.is_file() {
            Value::Object(read_json_object(&frontend_path)?)
        } else {
            Value::Null
        };
        out.insert("frontend".into(), frontend_val);
    }
    Ok(Value::Object(out))
}

fn get_restore(root: &Path, restore_id: &str) -> Result<Value, AppError> {
    let dir = restore_root(root, restore_id)?;
    let meta_path = ["transaction.json", "plan.json", "upload.json"]
        .into_iter()
        .map(|name| dir.join(name))
        .find(|path| path.is_file());
    let remote_path = dir.join("remote-fetch.json");
    let Some(meta_path) = meta_path else {
        if remote_path.is_file() {
            let remote = read_json_object(&remote_path)?;
            let resolved_id = remote
                .get("restoreId")
                .filter(|value| python_truthy(value))
                .map(value_str)
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| restore_id.to_string());
            let secret_state = restore_secret_state(&remote, &resolved_id);
            let mut out = Map::new();
            out.insert("ok".into(), Value::Bool(true));
            for (key, value) in remote {
                out.insert(key, value);
            }
            out.insert(
                "secretState".into(),
                Value::String(secret_state.to_string()),
            );
            return Ok(Value::Object(out));
        }
        return Err(AppError::invalid_payload("Restore metadata is unavailable"));
    };
    let metadata = read_json_object(&meta_path)?;
    let mut result = public_restore(&metadata, &dir, true)?;
    if remote_path.is_file() {
        let remote = read_json_object(&remote_path)?;
        let phase = remote
            .get("phase")
            .filter(|value| python_truthy(value))
            .map(value_str)
            .unwrap_or_default();
        if let Some(obj) = result.as_object_mut() {
            obj.insert("remoteRestorePhase".into(), Value::String(phase));
        }
    }
    Ok(result)
}

pub(crate) fn list_restores(root: &Path) -> Result<Value, AppError> {
    let staging = root.join(".restore-staging");
    if !staging.is_dir() {
        return Ok(json!({"ok": true, "restores": []}));
    }
    let Ok(entries) = std::fs::read_dir(&staging) else {
        return Ok(json!({"ok": true, "restores": []}));
    };
    let mut dirs: Vec<(String, PathBuf)> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let path = entry.path();
            if !path.is_dir() {
                return None;
            }
            let name = entry.file_name().to_str()?.to_string();
            Some((name, path))
        })
        .collect();
    dirs.sort_by(|left, right| left.0.cmp(&right.0));

    let mut restores = Vec::new();
    for (name, dir) in dirs {
        let meta_path = [
            "transaction.json",
            "plan.json",
            "upload.json",
            "remote-fetch.json",
        ]
        .into_iter()
        .map(|candidate| dir.join(candidate))
        .find(|path| path.is_file())
        .unwrap_or_else(|| dir.join("plan.json"));
        match read_json_object(&meta_path)
            .and_then(|metadata| public_restore(&metadata, &dir, false))
        {
            Ok(item) => restores.push(item),
            Err(_) => restores.push(json!({
                "restoreId": name,
                "phase": "recovery-required",
                "compatible": false,
            })),
        }
    }
    Ok(json!({"ok": true, "restores": restores}))
}
