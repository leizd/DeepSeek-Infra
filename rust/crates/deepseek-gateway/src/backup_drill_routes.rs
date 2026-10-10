//! `GET /api/workspace/disaster-recovery/drills/{restore_id}`.
//!
//! `backup_recovery_drill.get_recovery_drill` reads an existing
//! `.restore-staging/{id}` session. `drill-result.json` wins over
//! `drill-running.json`. A missing directory is 404 and is not created.
//! This handler never writes the session.
//!
//! The route is one path segment, so it also matches `.../drills/run`. Other
//! methods are forwarded to the Go catch-all proxy. A GET-only registration
//! would answer `POST /api/workspace/disaster-recovery/drills/run` with 405
//! and hide that separate Python route.

use std::path::Path;

use axum::body::Bytes;
use axum::extract::{OriginalUri, Path as AxumPath};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use serde_json::{Value, json};

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/disaster-recovery/drills/:restore_id",
        any(dispatch),
    )
}

async fn dispatch(
    method: Method,
    AxumPath(restore_id): AxumPath<String>,
    headers: HeaderMap,
    uri: OriginalUri,
    body: Bytes,
) -> Response {
    if method == Method::GET {
        return drill_response(restore_id).await;
    }
    crate::control_proxy::proxy_api_to_go(method, headers, uri, body).await
}

async fn drill_response(restore_id: String) -> Response {
    match tokio::task::spawn_blocking(move || read_drill(&restore_id)).await {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(server_error()),
    }
}

fn read_drill(restore_id: &str) -> Result<Value, AppError> {
    // `restore_id.startswith("restore_")` and `restore_id[8:].isalnum()`.
    let Some(suffix) = restore_id.strip_prefix("restore_") else {
        return Err(invalid_payload("Invalid restore id"));
    };
    if suffix.is_empty() || !suffix.chars().all(char::is_alphanumeric) {
        return Err(invalid_payload("Invalid restore id"));
    }
    let root = crate::data_routes::workspace_root()
        .join(".restore-staging")
        .join(restore_id);
    if !root.is_dir() {
        return Err(not_found("Remote restore session not found"));
    }
    let result = root.join("drill-result.json");
    if result.is_file() {
        return read_object(&result);
    }
    let claim = root.join("drill-running.json");
    if claim.is_file() {
        return read_object(&claim);
    }
    Err(not_found("Recovery Drill result not found"))
}

fn read_object(path: &Path) -> Result<Value, AppError> {
    let bytes = std::fs::read(path).map_err(|_| metadata_unavailable())?;
    // `UnicodeDecodeError` is not caught by `_read_json`.
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    let value: Value = serde_json::from_str(text).map_err(|_| metadata_unavailable())?;
    if !value.is_object() {
        return Err(metadata_unavailable());
    }
    Ok(value)
}

fn invalid_payload(message: &str) -> AppError {
    AppError {
        status: 400,
        code: codes::INVALID_PAYLOAD,
        message: message.into(),
    }
}

fn not_found(message: &str) -> AppError {
    AppError {
        status: 404,
        code: codes::NOT_FOUND,
        message: message.into(),
    }
}

fn metadata_unavailable() -> AppError {
    invalid_payload("Recovery Drill metadata is unavailable")
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
