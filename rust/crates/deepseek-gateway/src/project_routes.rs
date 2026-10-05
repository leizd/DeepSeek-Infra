//! Project routes served by Rust, ahead of the general Go control API proxy.
//! Metadata writes run only when `project_metadata_store` is declared and
//! `DEEPSEEK_RUNTIME_MODE=python_disabled`. Saved-item and artifact create,
//! update, and delete use that same gate.

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, rejection::BytesRejection},
    http::{HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{any, get, post},
};
use deepseek_policy::{
    app_error::{AppError, codes},
    core_utils::python_truthy,
    entropy::SystemEntropy,
    projects,
    skills::{analytics, project_integration, registry::Registry},
    workspace_projects, workspace_schema,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::PathBuf;

pub fn router() -> Router {
    Router::new()
        .route("/api/projects", post(legacy_projects))
        .route(
            "/api/workspace/projects",
            get(list_projects).post(create_project),
        )
        .route(
            "/api/workspace/projects/:project_id",
            get(get_project).patch(patch_project).delete(delete_project),
        )
        .route(
            "/api/workspace/projects/:project_id/conversations",
            get(conversations).post(upsert_conversation),
        )
        .route(
            "/api/workspace/projects/:project_id/saved-items",
            get(saved_items).post(post_saved_item),
        )
        .route(
            "/api/workspace/projects/:project_id/saved-items/:saved_id",
            any(saved_item_by_id),
        )
        .route(
            "/api/workspace/projects/:project_id/artifacts",
            get(artifacts).post(post_artifact),
        )
        .route(
            "/api/workspace/projects/:project_id/artifacts/:artifact_id",
            any(artifact_by_id),
        )
        // Skill bindings and skill runs live in project.json, the store this
        // router already writes. Analytics reads the skills run journal.
        .route(
            "/api/workspace/projects/:project_id/skills",
            get(project_skills).patch(patch_project_skills),
        )
        .route(
            "/api/workspace/projects/:project_id/skill-packs/:pack_id/install",
            post(install_skill_pack),
        )
        .route(
            "/api/workspace/projects/:project_id/skill-runs",
            get(project_skill_runs),
        )
        .route(
            "/api/workspace/projects/:project_id/skill-analytics",
            get(project_skill_analytics),
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
        "create" => {
            if let Err(response) = ensure_project_writer() {
                return response;
            }
            let name = workspace_schema::python_str(
                payload.get("name").filter(|value| python_truthy(value)),
            );
            let root = root();
            read_response(move || {
                Ok(json!({
                    "ok": true,
                    "project": workspace_projects::create_project(&name, "", &root, &SystemEntropy)?,
                }))
            })
            .await
        }
        "rename" => {
            if let Err(response) = ensure_project_writer() {
                return response;
            }
            let id = workspace_schema::python_str(
                payload
                    .get("id")
                    .filter(|value| python_truthy(value))
                    .or_else(|| payload.get("projectId"))
                    .filter(|value| python_truthy(value)),
            );
            let name = workspace_schema::python_str(
                payload.get("name").filter(|value| python_truthy(value)),
            );
            let description = payload.get("description").map(|value| {
                workspace_schema::python_str(Some(value).filter(|value| python_truthy(value)))
            });
            let root = root();
            read_response(move || {
                Ok(json!({
                    "ok": true,
                    "project": workspace_projects::rename_project(
                        &id,
                        &name,
                        description.as_deref(),
                        &root,
                        &SystemEntropy,
                    )?,
                }))
            })
            .await
        }
        "delete" => {
            if let Err(response) = ensure_project_writer() {
                return response;
            }
            let id = workspace_schema::python_str(
                payload
                    .get("id")
                    .filter(|value| python_truthy(value))
                    .or_else(|| payload.get("projectId"))
                    .filter(|value| python_truthy(value)),
            );
            let root = root();
            read_response(move || {
                Ok(json!({
                    "ok": true,
                    "deleted": workspace_projects::delete_project(&id, &root)?,
                }))
            })
            .await
        }
        _ => error_response(AppError::invalid_payload("Unsupported project action")),
    }
}

fn ensure_project_writer() -> Result<(), Response> {
    if crate::may_write_native_store("project_metadata_store") {
        Ok(())
    } else {
        Err(project_write_not_owned())
    }
}

fn project_write_not_owned() -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "The project metadata store is still written by the Python runtime, so this gateway refuses to mutate it.",
            "code": crate::PROJECT_METADATA_WRITE_NOT_OWNED,
        })),
    )
        .into_response()
}

async fn create_project(body: Result<Bytes, BytesRejection>) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let name =
        workspace_schema::python_str(payload.get("name").filter(|value| python_truthy(value)));
    let description = workspace_schema::python_str(
        payload
            .get("description")
            .filter(|value| python_truthy(value)),
    );
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "project": workspace_projects::create_project(&name, &description, &root, &SystemEntropy)?,
        }))
    })
    .await
}

async fn patch_project(Path(id): Path<String>, body: Result<Bytes, BytesRejection>) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let name =
        workspace_schema::python_str(payload.get("name").filter(|value| python_truthy(value)));
    let description = if payload.get("description").is_some() {
        Some(workspace_schema::python_str(
            payload
                .get("description")
                .filter(|value| python_truthy(value)),
        ))
    } else {
        None
    };
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "project": workspace_projects::rename_project(
                &id,
                &name,
                description.as_deref(),
                &root,
                &SystemEntropy,
            )?,
        }))
    })
    .await
}

async fn delete_project(Path(id): Path<String>) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "deleted": workspace_projects::delete_project(&id, &root)?,
        }))
    })
    .await
}

async fn upsert_conversation(
    Path(id): Path<String>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "conversation": workspace_projects::upsert_project_conversation(&id, &payload, &root, &SystemEntropy)?,
        }))
    })
    .await
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

fn route_str(payload: &Value, key: &str, fallback: &str) -> String {
    let value = payload.get(key).filter(|value| python_truthy(value));
    if value.is_none() {
        fallback.to_string()
    } else {
        workspace_schema::python_str(value)
    }
}

async fn post_saved_item(Path(id): Path<String>, body: Result<Bytes, BytesRejection>) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let item_type = route_str(&payload, "type", "");
    let title = route_str(&payload, "title", "");
    let content = route_str(&payload, "content", "");
    let purpose = route_str(&payload, "purpose", "reference");
    let source_ref = match payload.get("sourceRef") {
        Some(value) if value.is_object() => value.clone(),
        _ => Value::Null,
    };
    let tags = match payload.get("tags") {
        Some(value) if value.is_array() => value.clone(),
        _ => json!([]),
    };
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "savedItem": workspace_projects::create_saved_item(
                &id,
                &root,
                workspace_projects::SavedItemInput {
                    item_type: &item_type,
                    title: &title,
                    content: &content,
                    source_ref: &source_ref,
                    tags: &tags,
                    purpose: &purpose,
                },
                &SystemEntropy,
            )?,
        }))
    })
    .await
}

async fn saved_item_by_id(
    method: Method,
    Path((project_id, saved_id)): Path<(String, String)>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    if method == Method::DELETE {
        if let Err(response) = ensure_project_writer() {
            return response;
        }
        let root = root();
        return read_response(move || {
            Ok(json!({
                "ok": true,
                "deleted": workspace_projects::delete_saved_item(
                    &project_id,
                    &saved_id,
                    &root,
                    &SystemEntropy,
                )?,
            }))
        })
        .await;
    }
    if method != Method::PATCH {
        return method_not_allowed("PATCH, DELETE");
    }
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "savedItem": workspace_projects::update_saved_item(
                &project_id,
                &saved_id,
                &payload,
                &root,
                &SystemEntropy,
            )?,
        }))
    })
    .await
}

fn method_not_allowed(allow: &'static str) -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static(allow));
    response
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

async fn post_artifact(Path(id): Path<String>, body: Result<Bytes, BytesRejection>) -> Response {
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let artifact_type = route_str(&payload, "type", "");
    let title = route_str(&payload, "title", "");
    let path = route_str(&payload, "path", "");
    let source = match payload.get("source") {
        Some(value) if value.is_object() => value.clone(),
        _ => Value::Null,
    };
    let root = root();
    read_response(move || {
        Ok(json!({
            "ok": true,
            "artifact": workspace_projects::register_artifact(
                &id,
                &root,
                &artifact_type,
                &title,
                &path,
                &source,
                &SystemEntropy,
            )?,
        }))
    })
    .await
}

async fn artifact_by_id(
    method: Method,
    Path((project_id, artifact_id)): Path<(String, String)>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    if method == Method::DELETE {
        if let Err(response) = ensure_project_writer() {
            return response;
        }
        let root = root();
        return read_response(move || {
            Ok(json!({
                "ok": true,
                "deleted": workspace_projects::delete_artifact(
                    &project_id,
                    &artifact_id,
                    &root,
                    &SystemEntropy,
                )?,
            }))
        })
        .await;
    }
    if method != Method::PATCH {
        return method_not_allowed("PATCH, DELETE");
    }
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    let has_path = payload.get("path").is_some_and(python_truthy);
    let path = route_str(&payload, "path", "");
    let source = match payload.get("source") {
        Some(value) if value.is_object() => Some(value.clone()),
        _ => None,
    };
    let root = root();
    read_response(move || {
        let artifact = if has_path {
            workspace_projects::add_artifact_version(
                &project_id,
                &artifact_id,
                &path,
                source.as_ref(),
                &root,
                &SystemEntropy,
            )?
        } else {
            workspace_projects::update_artifact(
                &project_id,
                &artifact_id,
                &payload,
                &root,
                &SystemEntropy,
            )?
        };
        Ok(json!({"ok": true, "artifact": artifact}))
    })
    .await
}

async fn artifacts(Path(id): Path<String>) -> Response {
    let root = root();
    read_response(move || {
        Ok(json!({"ok": true, "artifacts": workspace_projects::list_artifacts(&id, &root)?}))
    })
    .await
}

async fn project_skills(Path(id): Path<String>) -> Response {
    read_response(move || {
        let registry = Registry::from_env();
        Ok(json!({"ok": true, "skills": project_integration::binding(&registry, &id)?}))
    })
    .await
}

async fn patch_project_skills(
    Path(id): Path<String>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    let payload = match parse_body(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    read_response(move || {
        let registry = Registry::from_env();
        let existing = project_integration::binding(&registry, &id)?;
        let desired = desired_skill_binding(&existing, &payload)?;
        Ok(json!({
            "ok": true,
            "skills": project_integration::set_binding(&registry, &id, &desired)?,
        }))
    })
    .await
}

async fn install_skill_pack(
    Path((project_id, pack_id)): Path<(String, String)>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    let payload = match optional_object(body) {
        Ok(payload) => payload,
        Err(error) => return error_response(error),
    };
    if let Err(response) = ensure_project_writer() {
        return response;
    }
    let version = truthy_text(&payload, "version");
    read_response(move || {
        let registry = Registry::from_env();
        Ok(json!({
            "ok": true,
            "skills": project_integration::enable_pack(&registry, &project_id, &pack_id, &version)?,
        }))
    })
    .await
}

#[derive(Default, Deserialize)]
struct LimitQuery {
    #[serde(default)]
    limit: Option<String>,
}

#[derive(Default, Deserialize)]
struct DaysQuery {
    #[serde(default)]
    days: Option<String>,
}

async fn project_skill_runs(Path(id): Path<String>, Query(query): Query<LimitQuery>) -> Response {
    let limit = match skill_run_window(query.limit.as_deref()) {
        Ok(limit) => limit,
        Err(error) => return error_response(error),
    };
    let root = root();
    read_response(move || {
        let project = projects::require_project(&id, &root, &SystemEntropy)?;
        let runs = projects::normalize_skill_runs(project.get("skillRuns"), &SystemEntropy)?;
        Ok(json!({
            "ok": true,
            "skillRuns": runs.into_iter().take(limit).collect::<Vec<_>>(),
        }))
    })
    .await
}

async fn project_skill_analytics(
    Path(id): Path<String>,
    Query(query): Query<DaysQuery>,
) -> Response {
    let days = match analytics_days(query.days.as_deref()) {
        Ok(days) => days,
        Err(error) => return error_response(error),
    };
    read_response(move || {
        let registry = Registry::from_env();
        let payload = json!({"scope": "project", "projectId": id, "skillId": "", "packId": ""});
        Ok(json!({"ok": true, "summary": analytics::summary(&registry, &payload, days)}))
    })
    .await
}

/// `int(query or default)`. A bad literal is the oracle's unhandled `ValueError`,
/// which the HTTP edge turns into `500 {"error":"Server error","code":"internal"}`.
fn query_int(raw: Option<&str>, default: i64) -> Result<i64, AppError> {
    let Some(raw) = raw.filter(|value| !value.is_empty()) else {
        return Ok(default);
    };
    raw.trim().parse::<i64>().map_err(|_| AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    })
}

/// `list_project_skill_runs`: `max(1, min(int(limit or 50), MAX_PROJECT_SKILL_RUNS))`.
fn skill_run_window(raw: Option<&str>) -> Result<usize, AppError> {
    let parsed = query_int(raw, 50)?;
    let value = if parsed == 0 { 50 } else { parsed };
    Ok(value.clamp(1, projects::MAX_PROJECT_SKILL_RUNS as i64) as usize)
}

/// `analytics_summary(..., days=)` clamps the trend window to `1..=30`, and `0` means a week.
fn analytics_days(raw: Option<&str>) -> Result<usize, AppError> {
    let parsed = query_int(raw, 7)?;
    let value = if parsed == 0 { 7 } else { parsed };
    Ok(value.clamp(1, 30) as usize)
}

fn optional_object(body: Result<Bytes, BytesRejection>) -> Result<Value, AppError> {
    let bytes = body.map_err(|_| AppError {
        status: 413,
        code: codes::UPLOAD_TOO_LARGE,
        message: "Request body is too large".into(),
    })?;
    if bytes.is_empty() {
        return Ok(json!({}));
    }
    parse_body(Ok(bytes))
}

fn truthy_text(payload: &Value, key: &str) -> String {
    payload
        .get(key)
        .filter(|value| python_truthy(value))
        .map(|value| workspace_schema::python_str(Some(value)))
        .unwrap_or_default()
}

fn python_str_value(value: &Value) -> String {
    deepseek_policy::python_json::value_str(value)
}

/// The workspace PATCH body, shaped the way `set_project_skill_binding` receives it.
///
/// A missing `enabledPacks` or `enabledPackVersions` keeps the current binding.
/// A present list replaces it. Skill ids are `str(item)` before normalisation.
fn desired_skill_binding(existing: &Value, payload: &Value) -> Result<Value, AppError> {
    let enabled = payload
        .get("enabledSkills")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| Value::String(python_str_value(item)))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let packs = if let Some(items) = payload.get("enabledPacks").and_then(Value::as_array) {
        Value::Array(
            items
                .iter()
                .map(|item| {
                    let text = if let Some(object) = item.as_object() {
                        match object.get("packId") {
                            Some(value) => python_str_value(value),
                            None => "None".to_string(),
                        }
                    } else {
                        python_str_value(item)
                    };
                    Value::String(text)
                })
                .collect(),
        )
    } else {
        existing
            .get("enabledPacks")
            .cloned()
            .unwrap_or_else(|| json!([]))
    };
    let versions = if let Some(items) = payload.get("enabledPackVersions").and_then(Value::as_array)
    {
        Value::Array(
            items
                .iter()
                .filter(|item| item.is_object())
                .cloned()
                .collect(),
        )
    } else {
        let draft = json!({
            "enabledPacks": packs.clone(),
            "enabledPackVersions": [],
            "enabledSkills": [],
            "defaultSkill": "",
            "recentSkills": [],
        });
        let normalized = projects::normalize_project_skills(Some(&draft))?;
        let kept = normalized
            .get("enabledPacks")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut filtered = Vec::new();
        for item in existing
            .get("enabledPackVersions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if !item.is_object() {
                return Err(AppError {
                    status: 500,
                    code: codes::INTERNAL,
                    message: "Server error".into(),
                });
            }
            let pack_id = item.get("packId").unwrap_or(&Value::Null);
            if kept.iter().any(|pack| pack == pack_id) {
                filtered.push(item.clone());
            }
        }
        Value::Array(filtered)
    };
    Ok(json!({
        "enabledSkills": enabled,
        "defaultSkill": truthy_text(payload, "defaultSkill"),
        "enabledPacks": packs,
        "enabledPackVersions": versions,
    }))
}
