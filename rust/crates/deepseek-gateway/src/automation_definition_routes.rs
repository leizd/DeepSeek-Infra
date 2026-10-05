//! `GET`, `PATCH`, and `DELETE /api/automation/{automation_id}`.
//!
//! `registry.get_automation` reads `.automation/automations.json`. A missing
//! file, invalid JSON, or an unreadable path is an empty registry and answers
//! `404 Automation not found`. The directory is not created. Records are
//! `normalize_automation(..., touch=False)`. Invalid records are skipped. Only
//! the last 500 accepted records are visible.
//!
//! `PATCH` is `registry.update_automation` and `DELETE` is
//! `registry.delete_automation`. Both write the same file Python still owns,
//! so they run only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise
//! the answer is `409 NATIVE_AUTOMATION_WRITE_NOT_OWNED` and the directory is
//! not created. A refused or missing-id delete does not rewrite the file.
//! `HEAD` follows GET. Any other method is 405.
//!
//! `create_automation` is the shared writer for
//! `POST /api/automation/templates/{template_id}`. It is not a route on this
//! path. `POST /api/automation/{automation_id}` stays 405. `POST .../run` is
//! served by `automation_run_routes`.

use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{python_int_opt, python_truthy};
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::{json_number_str, value_str};
use deepseek_policy::workspace_schema::{
    new_id, normalize_description, normalize_source_ref, normalize_title, timestamp_ms_to_iso,
    validate_project_id, validate_workspace_id, write_json_atomic,
};
use serde_json::{Map, Value, json};

const BODY_LIMIT: usize = 2_000_000;
const WRITE_NOT_OWNED: &str = "NATIVE_AUTOMATION_WRITE_NOT_OWNED";

const MAX_AUTOMATIONS: usize = 500;
const DEFAULT_ARTIFACT_TYPE: &str = "markdown";
const TRIGGER_TYPES: [&str; 4] = ["manual", "schedule", "interval", "event"];
const EVENT_TYPES: [&str; 4] = [
    "project.updated",
    "media.ready",
    "artifact.created",
    "saved_item.created",
];
const CONDITION_TYPES: [&str; 6] = [
    "always",
    "project_changed",
    "media_ready",
    "new_saved_items",
    "url_changed",
    "artifact_created",
];
const ACTION_TYPES: [&str; 9] = [
    "run_skill",
    "browser_snapshot",
    "browser_check",
    "project_summary",
    "media_process",
    "create_artifact",
    "save_item",
    "export_conversation",
    "export_project",
];
const BROWSER_MODES: [&str; 3] = ["read_only", "write_requires_confirmation", "disabled"];

pub fn router() -> Router {
    Router::new().route("/api/automation/:automation_id", any(dispatch))
}

async fn dispatch(
    method: Method,
    AxumPath(automation_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return method_response(method, move || read_automation(&automation_id)).await;
    }
    if method == Method::PATCH {
        return patch_response(automation_id, headers, body).await;
    }
    if method == Method::DELETE {
        return method_response(Method::DELETE, move || delete_automation(&automation_id)).await;
    }
    method_not_allowed()
}

async fn patch_response(automation_id: String, headers: HeaderMap, body: Bytes) -> Response {
    let patch = match read_object(&headers, &body) {
        Ok(value) => value,
        Err(error) => return error_response(error),
    };
    method_response(Method::PATCH, move || {
        update_automation(&automation_id, &patch)
    })
    .await
}

pub(crate) async fn method_response(
    method: Method,
    work: impl FnOnce() -> Result<Value, AppError> + Send + 'static,
) -> Response {
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => error_response(server_error()),
    }
}

fn read_automation(automation_id: &str) -> Result<Value, AppError> {
    let automation = find_automation(automation_id)?;
    Ok(json!({"ok": true, "automation": automation}))
}

/// The automation object `registry.get_automation` returns, or `404`.
/// This does not create `.automation`.
pub(crate) fn find_automation(automation_id: &str) -> Result<Value, AppError> {
    let safe_id = validate_workspace_id(automation_id, "automation id")?;
    for automation in load_automations()? {
        if automation_id_is(&automation, &safe_id) {
            return Ok(automation);
        }
    }
    Err(AppError::not_found("Automation not found"))
}

/// `registry.update_automation`. The body is already known to be an object.
/// Ownership is checked after the id check and before any filesystem touch.
fn update_automation(automation_id: &str, patch: &Value) -> Result<Value, AppError> {
    let safe_id = validate_workspace_id(automation_id, "automation id")?;
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let mut items = load_automations()?;
    let Some(index) = items
        .iter()
        .position(|item| automation_id_is(item, &safe_id))
    else {
        return Err(AppError::not_found("Automation not found"));
    };
    let current = items[index].clone();
    let merged = merge_patch(&current, patch, &safe_id);
    let updated = normalize_automation(&merged, &current, true)?;
    let project_id = text_field(&updated, "projectId");
    if !project_id.is_empty() {
        let root = crate::data_routes::workspace_root();
        deepseek_policy::projects::require_project(&project_id, &root, &SystemEntropy)?;
    }
    items[index] = updated.clone();
    write_automations(&items)?;
    touch_project(&project_id);
    Ok(json!({"ok": true, "automation": updated}))
}

/// `registry.create_automation`. The caller has already built the payload.
/// Ownership is checked before normalize, so a bad payload is `409` while
/// Python still owns the store and the directory is not created. A cap or
/// duplicate refusal does not rewrite the file.
pub(crate) fn create_automation(payload: &Value) -> Result<Value, AppError> {
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let automation = normalize_automation(payload, &Value::Null, true)?;
    let project_id = text_field(&automation, "projectId");
    if !project_id.is_empty() {
        let root = crate::data_routes::workspace_root();
        deepseek_policy::projects::require_project(&project_id, &root, &SystemEntropy)?;
    }
    let mut items = load_automations()?;
    if items.len() >= MAX_AUTOMATIONS {
        return Err(AppError {
            status: 413,
            code: codes::UPLOAD_TOO_LARGE,
            message: "Too many automations".into(),
        });
    }
    let automation_id = text_field(&automation, "automationId");
    if items
        .iter()
        .any(|item| automation_id_is(item, &automation_id))
    {
        return Err(AppError {
            status: 409,
            code: codes::INVALID_PAYLOAD,
            message: "Automation already exists".into(),
        });
    }
    items.push(automation.clone());
    write_automations(&items)?;
    touch_project(&project_id);
    Ok(automation)
}

/// `registry.delete_automation`. A missing id returns `deleted: 0` and does
/// not rewrite the file. Removing one or more visible rows returns `deleted: 1`.
fn delete_automation(automation_id: &str) -> Result<Value, AppError> {
    let safe_id = validate_workspace_id(automation_id, "automation id")?;
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let items = load_automations()?;
    let kept: Vec<Value> = items
        .iter()
        .filter(|item| !automation_id_is(item, &safe_id))
        .cloned()
        .collect();
    if kept.len() == items.len() {
        return Ok(json!({"ok": true, "deleted": 0}));
    }
    let project_id = items
        .iter()
        .find(|item| automation_id_is(item, &safe_id))
        .map(|item| text_field(item, "projectId"))
        .unwrap_or_default();
    write_automations(&kept)?;
    touch_project(&project_id);
    Ok(json!({"ok": true, "deleted": 1}))
}

fn automation_id_is(item: &Value, automation_id: &str) -> bool {
    item.get("automationId").and_then(Value::as_str) == Some(automation_id)
}

fn text_field(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

/// `{**current, **patch, "automationId": safe_id}`. Patch keys replace current
/// keys. Nested objects are not merged.
fn merge_patch(current: &Value, patch: &Value, safe_id: &str) -> Value {
    let mut merged = current.as_object().cloned().unwrap_or_default();
    if let Some(fields) = patch.as_object() {
        for (key, value) in fields {
            merged.insert(key.clone(), value.clone());
        }
    }
    merged.insert(
        "automationId".to_string(),
        Value::String(safe_id.to_string()),
    );
    Value::Object(merged)
}

fn write_automations(items: &[Value]) -> Result<(), AppError> {
    let root = crate::data_routes::workspace_root();
    let path = root.join(".automation").join("automations.json");
    write_json_atomic(
        &root,
        &path,
        &json!({"automations": Value::Array(items.to_vec())}),
    )
}

/// `registry._touch_project`. Failures are swallowed, including a missing
/// project. This runs only after the automation file has been replaced.
fn touch_project(project_id: &str) {
    if project_id.is_empty() {
        return;
    }
    let root = crate::data_routes::workspace_root();
    let entropy = SystemEntropy;
    let Ok(mut project) = deepseek_policy::projects::require_project(project_id, &root, &entropy)
    else {
        return;
    };
    let Some(fields) = project.as_object_mut() else {
        return;
    };
    fields.insert("updatedAt".to_string(), json!(entropy.now_millis()));
    let _ = deepseek_policy::projects::write_project(&root, &project, &entropy);
}

pub(crate) fn read_object(headers: &HeaderMap, body: &[u8]) -> Result<Value, AppError> {
    let length = content_length(headers)?;
    if length == 0 {
        return Err(AppError::invalid_payload("Request body is empty"));
    }
    if length > BODY_LIMIT {
        return Err(AppError {
            status: 413,
            code: codes::UPLOAD_TOO_LARGE,
            message: "Request body is too large".into(),
        });
    }
    // `raw.decode("utf-8")` is outside the JSON parser. A bad encoding is the
    // generic 500, not `Invalid JSON`.
    let text = std::str::from_utf8(body).map_err(|_| server_error())?;
    let value: Value = serde_json::from_str(text)
        .map_err(|error| AppError::invalid_payload(format!("Invalid JSON: {error}")))?;
    if !value.is_object() {
        return Err(AppError::invalid_payload(
            "Request body must be a JSON object",
        ));
    }
    Ok(value)
}

pub(crate) fn content_length(headers: &HeaderMap) -> Result<usize, AppError> {
    let raw = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("0");
    let length = raw
        .parse::<i64>()
        .map_err(|_| AppError::invalid_payload("Invalid Content-Length"))?;
    if length < 0 {
        return Err(AppError::invalid_payload("Invalid Content-Length"));
    }
    Ok(usize::try_from(length).unwrap_or(usize::MAX))
}

pub(crate) fn write_not_owned() -> AppError {
    AppError {
        status: 409,
        code: WRITE_NOT_OWNED,
        message: "The automation store is still written by the Python runtime, so this gateway refuses to mutate it.".into(),
    }
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response.headers_mut().insert(
        header::ALLOW,
        HeaderValue::from_static("GET, HEAD, PATCH, DELETE"),
    );
    response
}

pub(crate) fn load_automations() -> Result<Vec<Value>, AppError> {
    let path = crate::data_routes::workspace_root()
        .join(".automation")
        .join("automations.json");
    if !path.exists() {
        return Ok(Vec::new());
    }
    // `OSError`, including a directory named `automations.json`, uses the default.
    // `UnicodeDecodeError` is not caught.
    let bytes = match std::fs::read(&path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(Vec::new()),
    };
    let text = std::str::from_utf8(&bytes).map_err(|_| server_error())?;
    let value: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(_) => return Ok(Vec::new()),
    };
    let Some(raw_items) = value.get("automations").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut items = Vec::new();
    for raw in raw_items {
        if !raw.is_object() {
            continue;
        }
        // `except AppError: continue`. A rejected record is not a failed request.
        if let Ok(item) = normalize_automation(raw, raw, false) {
            items.push(item);
        }
    }
    if items.len() > MAX_AUTOMATIONS {
        items = items.split_off(items.len() - MAX_AUTOMATIONS);
    }
    Ok(items)
}

fn normalize_automation(payload: &Value, existing: &Value, touch: bool) -> Result<Value, AppError> {
    if !payload.is_object() {
        return Err(AppError::invalid_payload(
            "Automation payload must be an object",
        ));
    }
    let now = SystemEntropy.now_millis();
    let raw_id = first_truthy([
        payload.get("automationId"),
        payload.get("id"),
        existing.get("automationId"),
    ]);
    let automation_id = if raw_id.trim().is_empty() {
        new_id("auto", &SystemEntropy)?
    } else {
        validate_workspace_id(raw_id.trim(), "automation id")?
    };
    let project_raw = project_text(payload, existing);
    let project_id = if project_raw.trim().is_empty() {
        String::new()
    } else {
        validate_project_id(project_raw.trim())?
    };
    let created_at_ms = safe_int(existing.get("createdAtMs"), now);
    let updated_source = or_value(existing.get("updatedAtMs"), payload.get("updatedAtMs"));
    let updated_at_ms = if touch {
        now
    } else {
        safe_int(updated_source, now)
    };
    let created_at = match existing.get("createdAt") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => timestamp_ms_to_iso(Some(&Value::from(created_at_ms))),
    };
    Ok(json!({
        "automationId": automation_id,
        "id": automation_id,
        "projectId": project_id,
        "name": normalize_title(pick(payload, existing, "name"), "Automation"),
        "description": normalize_description(pick(payload, existing, "description")),
        "enabled": py_bool(pick(payload, existing, "enabled"), true),
        "trigger": normalize_trigger(&picked_or(payload, existing, "trigger", &json!({"type": "manual"})))?,
        "condition": normalize_condition(&picked_or(payload, existing, "condition", &json!({"type": "always"})))?,
        "action": normalize_action(&picked_or(payload, existing, "action", &json!({})))?,
        "output": normalize_output(&picked_or(payload, existing, "output", &json!({}))),
        "policy": normalize_policy(&picked_or(payload, existing, "policy", &json!({}))),
        "metadata": normalize_source_ref(&picked_or(payload, existing, "metadata", &json!({}))),
        "createdAt": created_at,
        "updatedAt": timestamp_ms_to_iso(Some(&Value::from(updated_at_ms))),
        "createdAtMs": created_at_ms,
        "updatedAtMs": updated_at_ms,
    }))
}

fn normalize_trigger(value: &Value) -> Result<Value, AppError> {
    let data = value.as_object();
    let trigger_type = text_or(data.and_then(|fields| fields.get("type")), "manual")
        .trim()
        .to_lowercase();
    if !TRIGGER_TYPES.contains(&trigger_type.as_str()) {
        return Err(AppError::invalid_payload(
            "Unsupported automation trigger type",
        ));
    }
    let mut trigger = Map::new();
    trigger.insert("type".to_string(), Value::String(trigger_type));
    let cron: String = text_or(data.and_then(|fields| fields.get("cron")), "")
        .trim()
        .chars()
        .take(120)
        .collect();
    if !cron.is_empty() {
        trigger.insert("cron".to_string(), Value::String(cron));
    }
    let interval = safe_int(data.and_then(|fields| fields.get("intervalSeconds")), 0);
    if interval > 0 {
        trigger.insert("intervalSeconds".to_string(), json!(interval));
    }
    let event = text_or(data.and_then(|fields| fields.get("event")), "")
        .trim()
        .to_lowercase();
    if !event.is_empty() {
        if !EVENT_TYPES.contains(&event.as_str()) {
            return Err(AppError::invalid_payload(
                "Unsupported automation event trigger",
            ));
        }
        trigger.insert("event".to_string(), Value::String(event));
    }
    Ok(Value::Object(trigger))
}

fn normalize_condition(value: &Value) -> Result<Value, AppError> {
    let data = value.as_object();
    let condition_type = text_or(data.and_then(|fields| fields.get("type")), "always")
        .trim()
        .to_lowercase();
    if !CONDITION_TYPES.contains(&condition_type.as_str()) {
        return Err(AppError::invalid_payload(
            "Unsupported automation condition type",
        ));
    }
    let flag = |name: &str| py_bool(data.and_then(|fields| fields.get(name)), false);
    Ok(json!({
        "type": condition_type,
        "sinceLastRun": flag("sinceLastRun"),
        "projectChanged": flag("projectChanged"),
        "newMediaReady": flag("newMediaReady"),
        "newSavedItems": flag("newSavedItems"),
        "urlChanged": flag("urlChanged"),
        "artifactCreated": flag("artifactCreated"),
    }))
}

fn normalize_action(value: &Value) -> Result<Value, AppError> {
    let data = value.as_object();
    let action_type = text_or(data.and_then(|fields| fields.get("type")), "")
        .trim()
        .to_lowercase();
    if !ACTION_TYPES.contains(&action_type.as_str()) {
        return Err(AppError::invalid_payload(
            "Unsupported automation action type",
        ));
    }
    let mut action = match value {
        Value::Object(fields) => fields.clone(),
        _ => Map::new(),
    };
    action.insert("type".to_string(), Value::String(action_type));
    if let Some(input) = action.get("input") {
        if !input.is_object() {
            return Err(AppError::invalid_payload(
                "Automation action input must be an object",
            ));
        }
    }
    Ok(Value::Object(action))
}

fn normalize_output(value: &Value) -> Value {
    let data = value.as_object();
    let mut artifact_type = text_or(
        data.and_then(|fields| fields.get("artifactType")),
        DEFAULT_ARTIFACT_TYPE,
    )
    .trim()
    .to_lowercase();
    artifact_type = artifact_type.trim_start_matches('.').to_string();
    if artifact_type == "md" || artifact_type.is_empty() {
        artifact_type = DEFAULT_ARTIFACT_TYPE.to_string();
    }
    json!({
        "saveToProject": py_bool(data.and_then(|fields| fields.get("saveToProject")), true),
        "createArtifact": py_bool(data.and_then(|fields| fields.get("createArtifact")), true),
        "artifactType": artifact_type,
    })
}

fn normalize_policy(value: &Value) -> Value {
    let data = value.as_object();
    let retry = data
        .and_then(|fields| fields.get("retry"))
        .and_then(Value::as_object);
    let mut browser_mode = text_or(
        data.and_then(|fields| fields.get("browserMode")),
        "read_only",
    )
    .trim()
    .to_lowercase();
    if !BROWSER_MODES.contains(&browser_mode.as_str()) {
        browser_mode = "read_only".to_string();
    }
    let max_attempts = safe_int(retry.and_then(|fields| fields.get("maxAttempts")), 1).clamp(1, 5);
    let backoff =
        safe_int(retry.and_then(|fields| fields.get("backoffSeconds")), 0).clamp(0, 3_600);
    json!({
        "requiresConfirmation": py_bool(data.and_then(|fields| fields.get("requiresConfirmation")), false),
        "maxRunsPerDay": safe_int(
            data.and_then(|fields| fields.get("maxRunsPerDay")),
            env_int_clamped("AUTOMATION_MAX_RUNS_PER_DAY", 50, 1, 10_000),
        ),
        "timeoutSeconds": safe_int(
            data.and_then(|fields| fields.get("timeoutSeconds")),
            env_int_clamped("AUTOMATION_RUN_TIMEOUT_SECONDS", 1_800, 1, 86_400),
        ),
        "allowBrowser": py_bool(
            data.and_then(|fields| fields.get("allowBrowser")),
            env_bool("AUTOMATION_ALLOW_BROWSER", false),
        ),
        "browserMode": browser_mode,
        "allowNetwork": py_bool(data.and_then(|fields| fields.get("allowNetwork")), false),
        "allowPrivateHosts": py_bool(data.and_then(|fields| fields.get("allowPrivateHosts")), false),
        "retry": {"maxAttempts": max_attempts, "backoffSeconds": backoff},
    })
}

fn pick<'a>(payload: &'a Value, existing: &'a Value, key: &str) -> Option<&'a Value> {
    payload.get(key).or_else(|| existing.get(key))
}

/// `payload.get(key, existing.get(key) or fallback)`.
fn picked_or<'a>(payload: &'a Value, existing: &'a Value, key: &str, fallback: &'a Value) -> Value {
    if let Some(value) = payload.get(key) {
        return value.clone();
    }
    match existing.get(key) {
        Some(value) if python_truthy(value) => value.clone(),
        _ => fallback.clone(),
    }
}

fn first_truthy(values: [Option<&Value>; 3]) -> String {
    for value in values {
        if let Some(value) = value.filter(|value| python_truthy(value)) {
            return value_str(value);
        }
    }
    String::new()
}

fn project_text(payload: &Value, existing: &Value) -> String {
    if let Some(value) = payload.get("projectId") {
        return value_str(value);
    }
    match existing.get("projectId") {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

fn or_value<'a>(left: Option<&'a Value>, right: Option<&'a Value>) -> Option<&'a Value> {
    match left {
        Some(value) if python_truthy(value) => Some(value),
        _ => right,
    }
}

fn text_or(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => fallback.to_string(),
    }
}

fn py_bool(value: Option<&Value>, default: bool) -> bool {
    match value {
        None | Some(Value::Null) => default,
        Some(Value::Bool(flag)) => *flag,
        Some(other) => matches!(
            value_str(other).trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
    }
}

fn safe_int(value: Option<&Value>, default: i64) -> i64 {
    let Some(value) = value else {
        return default;
    };
    let rendered = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) => json_number_str(number),
        _ => return default,
    };
    python_int_opt(Some(&Value::String(rendered))).unwrap_or(default)
}

fn env_int_clamped(name: &str, default: i64, minimum: i64, maximum: i64) -> i64 {
    let value = match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => {
            python_int_opt(Some(&Value::String(raw))).unwrap_or(default)
        }
        _ => default,
    };
    value.clamp(minimum, maximum)
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => {
            matches!(
                raw.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        }
        _ => default,
    }
}

fn server_error() -> AppError {
    AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    }
}

pub(crate) fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
