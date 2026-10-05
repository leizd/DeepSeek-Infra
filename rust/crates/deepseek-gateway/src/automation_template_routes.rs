//! `GET /api/automation/templates` and
//! `POST /api/automation/templates/{template_id}`.
//!
//! `registry.list_templates` returns copies of `BUILTIN_TEMPLATES`. It does not
//! read or create `.automation`. `create_from_template` copies one builtin
//! automation, sets `projectId` from the body, then applies a shallow
//! `overrides` object. The copy is written with `create_automation`, which
//! runs only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. A known template
//! otherwise answers `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` and does not
//! create the directory. An unknown template is 404 before that gate. Any
//! method other than POST on this path is 405.

use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get};
use axum::{Json, Router};
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Value, json};

pub fn router() -> Router {
    Router::new()
        .route("/api/automation/templates", get(list_templates))
        .route("/api/automation/templates/:template_id", any(dispatch))
}

async fn list_templates() -> impl IntoResponse {
    Json(builtin_templates())
}

async fn dispatch(
    method: Method,
    AxumPath(template_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return method_not_allowed();
    }
    let payload = match crate::automation_definition_routes::read_object(&headers, &body) {
        Ok(value) => value,
        Err(error) => return crate::automation_definition_routes::error_response(error),
    };
    // Template identity is resolved before the write gate and before any
    // filesystem touch. An unknown id is 404 even while Python owns the store.
    if builtin_template(&template_id).is_none() {
        return crate::automation_definition_routes::error_response(AppError::not_found(
            "Automation template not found",
        ));
    }
    crate::automation_definition_routes::method_response(Method::POST, move || {
        create_from_template(&template_id, &payload)
    })
    .await
}

/// `registry.create_from_template`. `projectId` uses Python's `or ""`, then a
/// dict `overrides` replaces keys. `overrides.projectId` wins.
fn create_from_template(template_id: &str, payload: &Value) -> Result<Value, AppError> {
    let template = builtin_template(template_id)
        .ok_or_else(|| AppError::not_found("Automation template not found"))?;
    let mut automation = template
        .get("automation")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let project_id = match payload.get("projectId") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    };
    if let Some(fields) = automation.as_object_mut() {
        fields.insert("projectId".to_string(), Value::String(project_id));
        if let Some(overrides) = payload.get("overrides").and_then(Value::as_object) {
            for (key, value) in overrides {
                fields.insert(key.clone(), value.clone());
            }
        }
    }
    let created = crate::automation_definition_routes::create_automation(&automation)?;
    Ok(json!({"ok": true, "automation": created}))
}

fn builtin_template(template_id: &str) -> Option<Value> {
    builtin_templates()
        .get("templates")
        .and_then(Value::as_array)
        .and_then(|items| {
            items
                .iter()
                .find(|item| item.get("templateId").and_then(Value::as_str) == Some(template_id))
                .cloned()
        })
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("POST"));
    response
}

fn builtin_templates() -> Value {
    json!({
        "ok": true,
        "templates": [
            {
                "templateId": "daily_project_summary",
                "name": "Daily Project Summary",
                "description": "Create a local markdown summary of project changes.",
                "automation": {
                    "name": "Daily Project Summary",
                    "description": "Summarize project files, saved items, artifacts, and media.",
                    "trigger": {"type": "schedule", "cron": "0 22 * * *"},
                    "condition": {"type": "project_changed", "sinceLastRun": true},
                    "action": {"type": "project_summary"},
                    "output": {"saveToProject": true, "createArtifact": true, "artifactType": "markdown"},
                    "policy": {"maxRunsPerDay": 3, "allowBrowser": false, "allowNetwork": false}
                }
            },
            {
                "templateId": "weekly_project_export",
                "name": "Weekly Project Export",
                "description": "Export the full project bundle once a week.",
                "automation": {
                    "name": "Weekly Project Export",
                    "trigger": {"type": "schedule", "cron": "0 18 * * 5"},
                    "condition": {"type": "project_changed", "sinceLastRun": true},
                    "action": {"type": "export_project", "format": "zip"},
                    "policy": {"maxRunsPerDay": 1, "allowBrowser": false, "allowNetwork": false}
                }
            },
            {
                "templateId": "webpage_change_watch",
                "name": "Webpage Change Watch",
                "description": "Read a page, diff against the last snapshot, and save a report when it changes.",
                "automation": {
                    "name": "Webpage Change Watch",
                    "trigger": {"type": "interval", "intervalSeconds": 3600},
                    "condition": {"type": "url_changed", "urlChanged": true},
                    "action": {"type": "browser_check", "url": "https://example.com", "selector": "body"},
                    "policy": {
                        "maxRunsPerDay": 12,
                        "allowBrowser": true,
                        "browserMode": "read_only",
                        "allowNetwork": true
                    }
                }
            },
            {
                "templateId": "media_digest",
                "name": "Media Digest",
                "description": "Run media_to_report when new media becomes ready.",
                "automation": {
                    "name": "Media Digest",
                    "trigger": {"type": "event", "event": "media.ready"},
                    "condition": {"type": "media_ready", "newMediaReady": true},
                    "action": {
                        "type": "media_process",
                        "skillId": "media_to_report",
                        "input": {"task": "Write a cited media digest."}
                    },
                    "policy": {"maxRunsPerDay": 10, "allowBrowser": false, "allowNetwork": false}
                }
            },
            {
                "templateId": "saved_items_digest",
                "name": "Saved Items Digest",
                "description": "Create a digest when new saved items are added.",
                "automation": {
                    "name": "Saved Items Digest",
                    "trigger": {"type": "event", "event": "saved_item.created"},
                    "condition": {"type": "new_saved_items", "newSavedItems": true},
                    "action": {"type": "project_summary", "section": "saved_items"},
                    "policy": {"maxRunsPerDay": 6, "allowBrowser": false, "allowNetwork": false}
                }
            },
            {
                "templateId": "artifact_backup",
                "name": "Artifact Backup",
                "description": "Export project artifacts when an artifact is created.",
                "automation": {
                    "name": "Artifact Backup",
                    "trigger": {"type": "event", "event": "artifact.created"},
                    "condition": {"type": "artifact_created", "artifactCreated": true},
                    "action": {"type": "export_project", "format": "zip"},
                    "policy": {"maxRunsPerDay": 10, "allowBrowser": false, "allowNetwork": false}
                }
            }
        ]
    })
}
