//! `GET /api/automation` and `POST /api/automation`.
//!
//! Completes the Automation Runtime HTTP surface by connecting the root list and
//! action-dispatch endpoint directly to `automation_definition_routes`,
//! `automation_template_routes`, and `automation_run_routes`.

use std::collections::HashMap;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::RawQuery;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_schema::{validate_project_id, validate_workspace_id};
use percent_encoding::percent_decode_str;
use serde_json::{Map, Value, json};

use crate::automation_definition_routes::{
    create_automation, delete_automation, find_automation, load_automations, method_response,
    read_object, update_automation, write_not_owned,
};
use crate::automation_run_routes::{
    condition_matches, execute_run, http_truthy, load_runs, parse_now, run_once, started_at_ms,
    trigger_matches,
};
use crate::automation_template_routes::{
    builtin_template, builtin_templates, create_from_template,
};

const MAX_RUNS: usize = 2_000;

pub fn router() -> Router {
    Router::new().route("/api/automation", any(dispatch))
}

async fn dispatch(
    method: Method,
    RawQuery(raw_query): RawQuery,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        let query = parse_query(raw_query.as_deref());
        let project_id = query.get("projectId").cloned().unwrap_or_default();
        let include_disabled = match query.get("includeDisabled") {
            Some(raw) => http_truthy(&Value::String(raw.clone())),
            None => true,
        };
        return method_response(method, move || {
            list_automations(&project_id, include_disabled)
        })
        .await;
    }
    if method == Method::POST {
        let payload = match read_object(&headers, &body) {
            Ok(value) => value,
            Err(error) => return crate::automation_definition_routes::error_response(error),
        };
        return method_response(Method::POST, move || dispatch_action(&payload)).await;
    }
    method_not_allowed()
}

fn parse_query(raw_query: Option<&str>) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Some(raw) = raw_query else {
        return map;
    };
    for pair in raw.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let decoded_key = percent_decode_str(&key.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned();
        let decoded_val = percent_decode_str(&value.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned();
        map.insert(decoded_key, decoded_val);
    }
    map
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD, POST"));
    response
}

fn automation_sort_key(item: &Value) -> i64 {
    item.get("updatedAtMs")
        .and_then(Value::as_i64)
        .filter(|value| *value != 0)
        .or_else(|| item.get("createdAtMs").and_then(Value::as_i64))
        .unwrap_or(0)
}

fn list_automations(project_id: &str, include_disabled: bool) -> Result<Value, AppError> {
    let items = load_automations_filtered(project_id, include_disabled)?;
    Ok(json!({"ok": true, "automations": items}))
}

fn load_automations_filtered(
    project_id: &str,
    include_disabled: bool,
) -> Result<Vec<Value>, AppError> {
    let safe_project_id = if project_id.is_empty() {
        String::new()
    } else {
        validate_project_id(project_id)?
    };
    let mut items = load_automations()?;
    if !safe_project_id.is_empty() {
        items
            .retain(|item| item.get("projectId").and_then(Value::as_str) == Some(&safe_project_id));
    }
    if !include_disabled {
        items.retain(|item| item.get("enabled").is_some_and(python_truthy));
    }
    items.sort_by_key(|item| std::cmp::Reverse(automation_sort_key(item)));
    Ok(items)
}

fn dispatch_action(payload: &Value) -> Result<Value, AppError> {
    let action = payload_truthy_str(payload.get("action"), "create")
        .trim()
        .to_ascii_lowercase();
    match action.as_str() {
        "list" => {
            let project_id = payload_truthy_str(payload.get("projectId"), "");
            let include_disabled = payload_bool_default(payload, "includeDisabled", true);
            list_automations(&project_id, include_disabled)
        }
        "create" => {
            let item = automation_payload(payload);
            let created = create_automation(&item)?;
            Ok(json!({"ok": true, "automation": created}))
        }
        "get" => {
            let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
            let item = find_automation(&id)?;
            Ok(json!({"ok": true, "automation": item}))
        }
        "update" => {
            let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
            let patch = patch_payload(payload);
            update_automation(&id, &patch)
        }
        "enable" | "disable" => {
            let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
            let enabled = action == "enable";
            update_automation(&id, &json!({"enabled": enabled}))
        }
        "delete" => {
            let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
            delete_automation(&id)
        }
        "run" => {
            let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
            execute_run(&id, payload)
        }
        "rerun" => action_rerun(payload),
        "list_runs" => action_list_runs(payload),
        "templates" => Ok(builtin_templates()),
        "create_from_template" => {
            let template_id =
                required_id(payload, &["templateId", "id"], "templateId is required")?;
            if builtin_template(&template_id).is_none() {
                return Err(AppError::not_found("Automation template not found"));
            }
            create_from_template(&template_id, payload)
        }
        "simulate" => action_simulate(payload),
        "run_due" => action_run_due(payload),
        _ => Err(AppError::invalid_payload("Unsupported automation action")),
    }
}

fn action_rerun(payload: &Value) -> Result<Value, AppError> {
    let raw_id = required_id(payload, &["runId", "id"], "runId is required")?;
    let safe_run_id = validate_workspace_id(&raw_id, "automation run id")?;
    let runs = load_runs()?;
    let run = runs
        .into_iter()
        .find(|item| item.get("runId").and_then(Value::as_str) == Some(&safe_run_id))
        .ok_or_else(|| AppError::not_found("Automation run not found"))?;
    let automation_id = run
        .get("automationId")
        .and_then(Value::as_str)
        .unwrap_or("");
    let automation = find_automation(automation_id)?;
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let confirmed = payload_bool_default(payload, "confirmed", false);
    let rerun = run_once(
        &automation,
        &json!({"type": "manual", "rerunOf": safe_run_id}),
        None,
        None,
        confirmed,
        true,
    )?;
    Ok(json!({"ok": true, "run": rerun}))
}

fn action_list_runs(payload: &Value) -> Result<Value, AppError> {
    let raw_auto = payload_truthy_str(payload.get("automationId"), "");
    let safe_auto = if raw_auto.is_empty() {
        String::new()
    } else {
        validate_workspace_id(&raw_auto, "automation id")?
    };
    let raw_project = payload_truthy_str(payload.get("projectId"), "");
    let safe_project = if raw_project.is_empty() {
        String::new()
    } else {
        validate_project_id(&raw_project)?
    };
    let status = payload_truthy_str(payload.get("status"), "")
        .trim()
        .to_ascii_lowercase();
    let limit = parse_list_limit(payload.get("limit"));

    let mut runs = load_runs()?;
    if !safe_auto.is_empty() {
        runs.retain(|run| run.get("automationId").and_then(Value::as_str) == Some(&safe_auto));
    }
    if !safe_project.is_empty() {
        runs.retain(|run| run.get("projectId").and_then(Value::as_str) == Some(&safe_project));
    }
    if !status.is_empty() {
        runs.retain(|run| run.get("status").and_then(Value::as_str) == Some(&status));
    }
    runs.sort_by_key(|run| std::cmp::Reverse(started_at_ms(run)));
    let cap = if limit == 0 {
        MAX_RUNS
    } else {
        limit.min(MAX_RUNS)
    };
    runs.truncate(cap);
    Ok(json!({"ok": true, "runs": runs}))
}

fn action_simulate(payload: &Value) -> Result<Value, AppError> {
    let id = required_id(payload, &["automationId", "id"], "automationId is required")?;
    let now = parse_now(payload)?;
    let automation = find_automation(&id)?;
    let trigger = payload
        .get("trigger")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| {
            automation
                .get("trigger")
                .filter(|value| value.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}))
        });
    let event = payload
        .get("event")
        .filter(|value| value.is_object())
        .cloned();
    let (trigger_matched, trigger_reason) =
        trigger_matches(&automation, &trigger, event.as_ref(), now)?;
    let (condition_matched, condition_reason) = condition_matches(&automation, event.as_ref())?;
    Ok(json!({
        "ok": true,
        "automationId": id,
        "triggerMatched": trigger_matched,
        "triggerReason": trigger_reason,
        "conditionMatched": condition_matched,
        "conditionReason": condition_reason,
        "wouldRun": trigger_matched && condition_matched,
    }))
}

fn action_run_due(payload: &Value) -> Result<Value, AppError> {
    let now = parse_now(payload)?;
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let event = payload
        .get("event")
        .filter(|value| value.is_object())
        .cloned();
    let confirmed = payload_bool_default(payload, "confirmed", false);
    let candidates = load_automations_filtered("", false)?;
    let mut runs = Vec::new();
    for automation in candidates {
        let trigger = automation
            .get("trigger")
            .filter(|value| value.is_object())
            .cloned()
            .unwrap_or_else(|| json!({"type": "manual"}));
        let trigger_type = payload_truthy_str(trigger.get("type"), "manual")
            .trim()
            .to_ascii_lowercase();
        if trigger_type == "manual" {
            continue;
        }
        let (matched, _) = trigger_matches(&automation, &trigger, event.as_ref(), now)?;
        if !matched {
            continue;
        }
        let run = run_once(&automation, &trigger, event.as_ref(), now, confirmed, false)?;
        runs.push(run);
    }
    let success = runs
        .iter()
        .filter(|run| run.get("status").and_then(Value::as_str) == Some("success"))
        .count();
    let failed = runs
        .iter()
        .filter(|run| run.get("status").and_then(Value::as_str) == Some("failed"))
        .count();
    let skipped = runs
        .iter()
        .filter(|run| run.get("status").and_then(Value::as_str) == Some("skipped"))
        .count();
    let requires_confirmation = runs
        .iter()
        .filter(|run| run.get("status").and_then(Value::as_str) == Some("requires_confirmation"))
        .count();
    Ok(json!({
        "ok": true,
        "dueCount": runs.len(),
        "runs": runs,
        "summary": {
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "requiresConfirmation": requires_confirmation,
        },
    }))
}

fn payload_truthy_str(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => fallback.to_string(),
    }
}

fn required_id(payload: &Value, keys: &[&str], message: &str) -> Result<String, AppError> {
    for key in keys {
        if let Some(found) = payload.get(*key).filter(|value| python_truthy(value)) {
            let trimmed = value_str(found).trim().to_string();
            if !trimmed.is_empty() {
                return Ok(trimmed);
            }
        }
    }
    Err(AppError::invalid_payload(message))
}

fn payload_bool_default(payload: &Value, key: &str, default: bool) -> bool {
    let Some(object) = payload.as_object() else {
        return default;
    };
    match object.get(key) {
        None => default,
        Some(Value::Bool(flag)) => *flag,
        Some(other) => http_truthy(other),
    }
}

fn parse_list_limit(value: Option<&Value>) -> usize {
    let Some(found) = value.filter(|val| python_truthy(val)) else {
        return 100;
    };
    let text = value_str(found);
    match text.trim().replace('_', "").parse::<i64>() {
        Ok(parsed) => parsed.clamp(0, MAX_RUNS as i64) as usize,
        Err(_) => 100,
    }
}

fn automation_payload(payload: &Value) -> Value {
    for key in ["automation", "config"] {
        if let Some(found) = payload.get(key).filter(|value| value.is_object()) {
            return found.clone();
        }
    }
    stripped_object(payload, &["action"])
}

fn patch_payload(payload: &Value) -> Value {
    for key in ["patch", "automation", "config"] {
        if let Some(found) = payload.get(key).filter(|value| value.is_object()) {
            return found.clone();
        }
    }
    stripped_object(payload, &["action", "automationId", "id"])
}

fn stripped_object(payload: &Value, excluded: &[&str]) -> Value {
    let mut map = Map::new();
    if let Some(object) = payload.as_object() {
        for (key, value) in object {
            if !excluded.contains(&key.as_str()) {
                map.insert(key.clone(), value.clone());
            }
        }
    }
    Value::Object(map)
}
