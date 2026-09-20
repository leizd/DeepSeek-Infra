//! Project reads served by Rust, ahead of the general Go control API proxy.
//! Project mutation ownership has not been cut over. These handlers never call
//! the legacy Rust write helpers, which do not yet cover deletion side effects.

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, rejection::BytesRejection},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use deepseek_policy::{
    app_error::{AppError, codes},
    core_utils::python_truthy,
    entropy::SystemEntropy,
    projects, workspace_projects, workspace_schema,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::PathBuf;

pub fn router() -> Router {
    Router::new()
        .route("/api/projects", post(legacy_projects))
        .route(
            "/api/workspace/projects",
            get(list_projects).post(mutation_not_ready),
        )
        .route(
            "/api/workspace/projects/:project_id",
            get(get_project)
                .patch(mutation_not_ready)
                .delete(mutation_not_ready),
        )
        .route(
            "/api/workspace/projects/:project_id/conversations",
            get(conversations).post(mutation_not_ready),
        )
        .route(
            "/api/workspace/projects/:project_id/saved-items",
            get(saved_items).post(mutation_not_ready),
        )
        .route(
            "/api/workspace/projects/:project_id/artifacts",
            get(artifacts).post(mutation_not_ready),
        )
        .layer(DefaultBodyLimit::max(2_000_000))
}

fn root() -> PathBuf {
    std::env::var_os("DEEPSEEK_INFRA_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

async fn mutation_not_ready() -> Response {
    error_response(AppError {status: 501, code: "NATIVE_PROJECTS_MUTATIONS_NOT_READY",
        message: "Native project mutations require the project-store ownership cutover and complete cleanup support".into()})
}

async fn read_response(
    read: impl FnOnce() -> Result<Value, AppError> + Send + 'static,
) -> Response {
    match tokio::task::spawn_blocking(read).await {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(AppError {
            status: 500,
            code: codes::INTERNAL,
            message: "Project reader failed".into(),
        }),
    }
}

fn parse_body(body: Result<Bytes, BytesRejection>) -> Result<Value, AppError> {
    let bytes = body.map_err(|_| AppError {
        status: 413,
        code: codes::UPLOAD_TOO_LARGE,
        message: "Request body is too large".into(),
    })?;
    if bytes.is_empty() {
        return Err(AppError::invalid_payload("Request body is empty"));
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| AppError::invalid_payload(format!("Invalid JSON: {error}")))?;
    if !value.is_object() {
        return Err(AppError::invalid_payload(
            "Request body must be a JSON object",
        ));
    }
    Ok(value)
}

async fn legacy_projects(body: Result<Bytes, BytesRejection>) -> Response {
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let action = payload
        .get("action")
        .filter(|value| python_truthy(value))
        .map(|value| workspace_schema::python_str(Some(value)))
        .unwrap_or_else(|| "list".into());
    match action.trim().to_lowercase().as_str() {
        "list" => {
            let root = root();
            read_response(move || {
                Ok(json!({"projects": projects::list_projects(&root, &SystemEntropy)?}))
            })
            .await
        }
        "get" => {
            let id = payload
                .get("id")
                .filter(|value| python_truthy(value))
                .or_else(|| payload.get("projectId"));
            let id = workspace_schema::python_str(id.filter(|value| python_truthy(value)));
            get_project(Path(id)).await
        }
        "create" | "rename" | "delete" => mutation_not_ready().await,
        _ => error_response(AppError::invalid_payload("Unsupported project action")),
    }
}

async fn list_projects() -> Response {
    let root = root();
    read_response(move || Ok(json!({"ok": true, "projects": workspace_projects::list_projects(&root, &SystemEntropy)?}))).await
}

async fn get_project(Path(id): Path<String>) -> Response {
    let root = root();
    read_response(move || Ok(json!({"ok": true, "project": workspace_projects::get_project(&id, &root, &SystemEntropy)?}))).await
}

async fn conversations(Path(id): Path<String>) -> Response {
    let root = root();
    read_response(move || Ok(json!({"ok": true, "conversations": workspace_projects::list_project_conversations(&id, &root, &SystemEntropy)?}))).await
}

#[derive(Default, Deserialize)]
struct SavedQuery {
    #[serde(default, rename = "type")]
    item_type: String,
    #[serde(default)]
    tags: String,
}

async fn saved_items(Path(id): Path<String>, Query(query): Query<SavedQuery>) -> Response {
    let root = root();
    let tags: Vec<String> = query
        .tags
        .split(',')
        .filter(|tag| !tag.is_empty())
        .map(str::to_string)
        .collect();
    read_response(move || Ok(json!({"ok": true, "savedItems": workspace_projects::list_saved_items(&id, &root, &query.item_type, &tags)?}))).await
}

async fn artifacts(Path(id): Path<String>) -> Response {
    let root = root();
    read_response(move || {
        Ok(json!({"ok": true, "artifacts": workspace_projects::list_artifacts(&id, &root)?}))
    })
    .await
}
