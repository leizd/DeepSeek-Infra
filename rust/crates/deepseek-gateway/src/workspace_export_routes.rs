//! Read-only `GET /api/workspace/exports/{export_id}/download` (`exports.resolve_export` + file download).
//!
//! Reads `.generated/workspace-exports/exports.json` (when `projectId` is empty)
//! and `.projects/{project_id}/exports/exports.json` cleanly without creating
//! directories or writing store files.

use std::path::{Path, PathBuf};

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, OriginalUri, Path as AxumPath};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::file_routes::content_disposition_header;
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_exports;
use deepseek_policy::workspace_schema::{validate_project_id, validate_workspace_id};
use serde_json::Value;

use crate::artifact_preview_routes::{
    build_download_response, error_response, guess_download_media_type, method_not_allowed,
    project_query, read_store_json, scan_projects, server_error,
};
use crate::route_support;

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/exports",
            any(dispatch_create_export).layer(DefaultBodyLimit::max(16_000_000)),
        )
        .route(
            "/api/workspace/exports/:export_id/download",
            any(dispatch_download),
        )
}

async fn dispatch_create_export(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = route_support::require_native_store(
        "generated_files_store",
        "NATIVE_GENERATED_FILES_WRITE_NOT_OWNED",
    ) {
        return route_support::app_error_response(err);
    }
    if let Err(err) = route_support::require_native_store(
        "project_metadata_store",
        crate::PROJECT_METADATA_WRITE_NOT_OWNED,
    ) {
        return route_support::app_error_response(err);
    }
    let payload = match route_support::read_json_object(&headers, &body, 16_000_000) {
        Ok(object) => Value::Object(object),
        Err(err) => return route_support::app_error_response(err),
    };
    let root = crate::data_routes::workspace_root();
    let joined = tokio::task::spawn_blocking(move || {
        workspace_exports::create_export(&root, &payload, &SystemEntropy)
    })
    .await;
    match joined {
        Ok(Ok(value)) => (StatusCode::OK, Json(value)).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => error_response(server_error()),
    }
}

async fn dispatch_download(
    method: Method,
    AxumPath(export_id): AxumPath<String>,
    OriginalUri(uri): OriginalUri,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return download_response(method, export_id, project_query(&uri)).await;
    }
    method_not_allowed()
}

async fn download_response(method: Method, export_id: String, project_id: String) -> Response {
    let result = match tokio::task::spawn_blocking(move || {
        download_payload(&export_id, &project_id)
    })
    .await
    {
        Ok(result) => result,
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

pub(crate) fn export_dir(root: &Path, project_id: &str) -> Result<PathBuf, AppError> {
    workspace_exports::export_dir(root, project_id)
}

pub(crate) fn load_exports(root: &Path, project_id: &str) -> Result<Vec<Value>, AppError> {
    let path = export_dir(root, project_id)?.join("exports.json");
    let Some(value) = read_store_json(&path)? else {
        return Ok(Vec::new());
    };
    let Some(items) = value.get("exports").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    Ok(items
        .iter()
        .filter(|item| item.is_object())
        .cloned()
        .collect())
}

fn resolve_export(root: &Path, export_id: &str, project_id: &str) -> Result<Value, AppError> {
    let safe_export_id = validate_workspace_id(export_id, "export id")?;
    let candidates = if !project_id.is_empty() {
        vec![validate_project_id(project_id)?]
    } else {
        let mut all = vec![String::new()];
        all.extend(scan_projects(root)?);
        all
    };
    for candidate_project_id in candidates {
        for export in load_exports(root, &candidate_project_id)? {
            if export.get("exportId").and_then(Value::as_str) == Some(safe_export_id.as_str()) {
                return Ok(export);
            }
        }
    }
    Err(AppError::not_found("Export not found"))
}

fn resolve_export_path(root: &Path, export: &Value) -> Result<PathBuf, AppError> {
    let raw_path = match export.get("path").filter(|value| python_truthy(value)) {
        Some(value) => value_str(value),
        None => String::new(),
    };
    let candidate = PathBuf::from(&raw_path);
    let path = if candidate.is_absolute() {
        candidate
    } else {
        let project_id = match export.get("projectId").filter(|value| python_truthy(value)) {
            Some(value) => value_str(value),
            None => String::new(),
        };
        export_dir(root, &project_id)?.join(candidate)
    };
    Ok(std::fs::canonicalize(&path).unwrap_or(path))
}

fn download_payload(
    export_id: &str,
    project_id: &str,
) -> Result<(String, String, Vec<u8>), AppError> {
    let root = crate::data_routes::workspace_root();
    let export = resolve_export(&root, export_id, project_id)?;
    let path = resolve_export_path(&root, &export)?;
    if !path.is_file() {
        return Err(AppError::not_found("Export file not found"));
    }
    let bytes = std::fs::read(&path).map_err(|_| server_error())?;
    let media_type = guess_download_media_type(&path).to_string();
    let fallback_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("export.bin")
        .to_string();
    let filename = export
        .get("filename")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(fallback_name);
    let disposition = content_disposition_header("attachment", &filename);
    Ok((media_type, disposition, bytes))
}
