//! `GET /api/automation/{automation_id}/runs` and `POST /api/automation/{automation_id}/run`.
//!
//! `history.list_runs` reads `.automation/history.json` through `read_json_file`.
//! A missing file, invalid JSON, a non-object, or an unreadable path returns
//! `{"ok": true, "runs": []}` and does not create `.automation`. Invalid UTF-8
//! is the oracle's uncaught `UnicodeDecodeError`: `500 Server error`.
//!
//! The route passes only `automation_id` and `limit`. `projectId` and `status`
//! query keys are ignored. `limit` is `int(query or 100)` before `list_runs`;
//! a non-integer is `500` and the id is not validated. Inside `list_runs`,
//! `0` is falsy so it becomes 100. A negative value clamps to "no cap" and
//! returns every loaded run. Values above 2000 clamp to 2000.
//!
//! Records are normalised, not returned raw. A record `normalize_run_record`
//! rejects is skipped. The last 2000 accepted records are kept, then filtered,
//! then sorted by `startedAtMs` descending. The GET handler never writes the file.
//!
//! `POST .../run` is `runner.run_once`. An empty or missing body is `{}` (the
//! route does not use `read_json_body`'s empty-body 400). `now` is parsed before
//! the automation is loaded. A missing automation is 404 and does not create
//! history. An existing automation is refused with 409 while Python still owns
//! the store, before any history, action, or memory write. `save_item` persists
//! `.projects/{id}/saved-items.json` and, on success, a memory summary. There is
//! no Rust trace store, so `traceId` stays empty. Other action types are refused
//! by the same policy rules, and a type this gateway does not execute yet is
//! recorded as a failed run rather than a fake success or a 501.

use std::time::Instant;

use axum::body::Bytes;
use axum::extract::{Path as AxumPath, Query};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get};
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::browser_safety::{BrowserSettings, evaluate_url_safety};
use deepseek_policy::core_utils::{SystemClock, python_int_opt, python_truthy};
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::memory_schema::add_memory;
use deepseek_policy::projects::{require_project, write_project};
use deepseek_policy::python_json::{json_number_str, value_str};
use deepseek_policy::workspace_schema::{
    new_id, normalize_content, normalize_saved_purpose, normalize_saved_type, normalize_source_ref,
    normalize_tags, normalize_title, read_json_file, timestamp_ms_to_iso, validate_project_id,
    validate_workspace_id, write_json_atomic,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};

const MAX_RUNS: usize = 2_000;
const MAX_SAVED_ITEMS: usize = 1_000;
const BODY_LIMIT: i64 = 2_000_000;
const BROWSER_WRITE_ACTIONS: [&str; 4] = ["click", "type_text", "select", "download"];
const LOCAL_ACTIONS: [&str; 7] = [
    "run_skill",
    "project_summary",
    "media_process",
    "create_artifact",
    "save_item",
    "export_conversation",
    "export_project",
];
const RUN_STATUSES: [&str; 5] = [
    "success",
    "failed",
    "skipped",
    "canceled",
    "requires_confirmation",
];

pub fn router() -> Router {
    Router::new()
        .route("/api/automation/:automation_id/runs", get(list_runs))
        .route("/api/automation/:automation_id/run", any(dispatch_run))
}

#[derive(Debug, Deserialize, Default)]
struct RunsQuery {
    limit: Option<String>,
}

async fn list_runs(
    AxumPath(automation_id): AxumPath<String>,
    Query(query): Query<RunsQuery>,
) -> Response {
    // `int(query or 100)` runs before `list_runs`, so a bad limit is 500 even
    // when the automation id would have been 400.
    let limit = match route_limit(query.limit.as_deref()) {
        Ok(limit) => limit,
        Err(error) => return error_response(error),
    };
    let automation_id = match validate_automation_id(&automation_id) {
        Ok(automation_id) => automation_id,
        Err(error) => return error_response(error),
    };
    match tokio::task::spawn_blocking(move || read_runs(&automation_id, limit)).await {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(server_error()),
    }
}

fn validate_automation_id(automation_id: &str) -> Result<String, AppError> {
    // `validate_automation_id(automation_id) if automation_id else ""`.
    if automation_id.is_empty() {
        return Ok(String::new());
    }
    validate_workspace_id(automation_id, "automation id")
}

fn read_runs(automation_id: &str, limit: RouteLimit) -> Result<Value, AppError> {
    let mut runs = load_runs()?;
    if !automation_id.is_empty() {
        runs.retain(|run| run.get("automationId").and_then(Value::as_str) == Some(automation_id));
    }
    runs.sort_by_key(|run| std::cmp::Reverse(started_at_ms(run)));
    if let Some(cap) = safe_limit(limit) {
        runs.truncate(cap);
    }
    Ok(json!({"ok": true, "runs": runs}))
}

fn started_at_ms(run: &Value) -> i64 {
    // After normalisation this is an int. `0` is falsy, so the sort key is 0.
    match run.get("startedAtMs") {
        Some(value) if python_truthy(value) => value.as_i64().unwrap_or(0),
        _ => 0,
    }
}

pub(crate) fn load_runs() -> Result<Vec<Value>, AppError> {
    let path = crate::data_routes::workspace_root()
        .join(".automation")
        .join("history.json");
    if !path.exists() {
        return Ok(Vec::new());
    }
    // `OSError` (including a directory named `history.json`) uses the default.
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
    let Some(raw_runs) = value.get("runs").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut runs = Vec::new();
    for raw in raw_runs {
        if !raw.is_object() {
            continue;
        }
        match normalize_run_record(raw) {
            Ok(run) => runs.push(run),
            Err(error) if error.message.starts_with("Invalid ") => continue,
            Err(error) => return Err(error),
        }
    }
    if runs.len() > MAX_RUNS {
        runs = runs.split_off(runs.len() - MAX_RUNS);
    }
    Ok(runs)
}

fn normalize_run_record(record: &Value) -> Result<Value, AppError> {
    let run_raw = text_or_empty(record.get("runId"));
    let run_id = if run_raw.trim().is_empty() {
        new_id("auto_run", &SystemEntropy)?
    } else {
        validate_workspace_id(run_raw.trim(), "automation run id")?
    };
    let automation_id =
        validate_workspace_id(&text_or_empty(record.get("automationId")), "automation id")?;
    let project_raw = text_or_empty(record.get("projectId"));
    let project_id = if project_raw.trim().is_empty() {
        String::new()
    } else {
        validate_project_id(project_raw.trim())?
    };
    let started_at_ms = safe_int(record.get("startedAtMs"), SystemEntropy.now_millis());
    let finished_at_ms = safe_int(record.get("finishedAtMs"), started_at_ms);
    let duration_default = finished_at_ms.saturating_sub(started_at_ms);
    let duration_ms = safe_int(record.get("durationMs"), duration_default).max(0);
    let started_at = text_or_else(record.get("startedAt"), || {
        timestamp_ms_to_iso(Some(&Value::from(started_at_ms)))
    });
    let finished_at = text_or_else(record.get("finishedAt"), || {
        timestamp_ms_to_iso(Some(&Value::from(finished_at_ms)))
    });
    Ok(json!({
        "runId": run_id,
        "automationId": automation_id,
        "projectId": project_id,
        "status": normalize_status(record.get("status")),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "startedAtMs": started_at_ms,
        "finishedAtMs": finished_at_ms,
        "durationMs": duration_ms,
        "trigger": object_or(record.get("trigger"), json!({"type": "manual"})),
        "outputs": public_run_outputs(record.get("outputs")),
        "traceId": capped_text(record.get("traceId"), 120),
        "attempts": safe_int(record.get("attempts"), 1).max(1),
        "error": capped_text(record.get("error"), 4_000),
        "skippedReason": capped_text(record.get("skippedReason"), 1_000),
        "logs": string_list(record.get("logs"), 200, 1_000),
        "evidence": object_or(record.get("evidence"), json!({})),
    }))
}

fn normalize_status(value: Option<&Value>) -> String {
    let raw = match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => "failed".to_string(),
    };
    let status = raw.trim().to_lowercase();
    if RUN_STATUSES.contains(&status.as_str()) {
        status
    } else {
        "failed".to_string()
    }
}

fn public_run_outputs(value: Option<&Value>) -> Value {
    let fields = value.and_then(Value::as_object);
    let pick = |name: &str| fields.and_then(|fields| fields.get(name));
    json!({
        "artifactIds": id_list(pick("artifactIds")),
        "savedItemIds": id_list(pick("savedItemIds")),
        "mediaIds": id_list(pick("mediaIds")),
        "exportIds": id_list(pick("exportIds")),
    })
}

fn id_list(value: Option<&Value>) -> Vec<String> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut result = Vec::new();
    for item in items {
        let text = text_or_empty(Some(item));
        let clipped: String = text.trim().chars().take(120).collect();
        if clipped.is_empty() || result.contains(&clipped) {
            continue;
        }
        result.push(clipped);
    }
    result
}

fn string_list(value: Option<&Value>, count: usize, width: usize) -> Vec<String> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    items
        .iter()
        .take(count)
        .map(|item| {
            let text = text_or_empty(Some(item));
            text.chars().take(width).collect()
        })
        .collect()
}

fn object_or(value: Option<&Value>, fallback: Value) -> Value {
    match value {
        Some(Value::Object(fields)) => Value::Object(fields.clone()),
        _ => fallback,
    }
}

/// `str(value or "")` with no extra strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

fn text_or_else(value: Option<&Value>, fallback: impl FnOnce() -> String) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => fallback(),
    }
}

fn capped_text(value: Option<&Value>, limit: usize) -> String {
    text_or_empty(value).chars().take(limit).collect()
}

/// `int(str(value))`, which rejects bools and floats. `int(True)` is not used.
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

enum RouteLimit {
    Fits(i64),
    PositiveOverflow,
    NegativeOverflow,
}

/// `int(query or 100)`. Only an empty or missing query uses 100 here.
/// The string `"0"` is truthy and parses as 0.
fn route_limit(raw: Option<&str>) -> Result<RouteLimit, AppError> {
    let Some(text) = raw.filter(|text| !text.is_empty()) else {
        return Ok(RouteLimit::Fits(100));
    };
    parse_python_int_literal(text).ok_or_else(server_error)
}

/// `max(0, min(int(limit or 100), 2000))`. `None` means the zero cap, which
/// returns every loaded run.
fn safe_limit(limit: RouteLimit) -> Option<usize> {
    let limit = match limit {
        RouteLimit::Fits(0) => RouteLimit::Fits(100),
        other => other,
    };
    match limit {
        RouteLimit::Fits(value) if value <= 0 => None,
        RouteLimit::Fits(value) => Some((value as usize).min(MAX_RUNS)),
        RouteLimit::PositiveOverflow => Some(MAX_RUNS),
        RouteLimit::NegativeOverflow => None,
    }
}

fn parse_python_int_literal(text: &str) -> Option<RouteLimit> {
    let trimmed = text.trim();
    let (negative, digits) = if let Some(rest) = trimmed.strip_prefix('-') {
        (true, rest)
    } else if let Some(rest) = trimmed.strip_prefix('+') {
        (false, rest)
    } else {
        (false, trimmed)
    };
    let normalised = digits.replace('_', "");
    let shape_ok = !normalised.is_empty()
        && normalised.bytes().all(|byte| byte.is_ascii_digit())
        && !digits.starts_with('_')
        && !digits.ends_with('_')
        && !digits.contains("__");
    if !shape_ok {
        return None;
    }
    if normalised.len() > 39 {
        return Some(if negative {
            RouteLimit::NegativeOverflow
        } else {
            RouteLimit::PositiveOverflow
        });
    }
    let magnitude = normalised.parse::<i128>().ok()?;
    let signed = if negative { -magnitude } else { magnitude };
    if signed > i64::MAX as i128 {
        Some(RouteLimit::PositiveOverflow)
    } else if signed < i64::MIN as i128 {
        Some(RouteLimit::NegativeOverflow)
    } else {
        Some(RouteLimit::Fits(signed as i64))
    }
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

async fn dispatch_run(
    method: Method,
    AxumPath(automation_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return method_not_allowed();
    }
    let payload = match read_run_body(&headers, &body) {
        Ok(value) => value,
        Err(error) => return error_response(error),
    };
    crate::automation_definition_routes::method_response(Method::POST, move || {
        execute_run(&automation_id, &payload)
    })
    .await
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

/// `int(Content-Length or "0") > 0` selects `read_json_body`; otherwise the
/// payload is `{}`. A value `int()` rejects is the route's uncaught
/// `ValueError` (`500`), not `read_json_body`'s `400 Invalid Content-Length`.
/// Zero and negative lengths are the empty object.
fn read_run_body(headers: &HeaderMap, body: &[u8]) -> Result<Value, AppError> {
    let raw = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("0");
    let text = if raw.is_empty() { "0" } else { raw };
    let length = match classify_header_int(text) {
        HeaderInt::Invalid => return Err(server_error()),
        HeaderInt::NonPositive => return Ok(json!({})),
        HeaderInt::TooLarge => {
            return Err(AppError {
                status: 413,
                code: codes::UPLOAD_TOO_LARGE,
                message: "Request body is too large".into(),
            });
        }
        HeaderInt::Fits(length) => length,
    };
    if length > BODY_LIMIT {
        return Err(AppError {
            status: 413,
            code: codes::UPLOAD_TOO_LARGE,
            message: "Request body is too large".into(),
        });
    }
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

enum HeaderInt {
    Invalid,
    NonPositive,
    Fits(i64),
    TooLarge,
}

fn classify_header_int(text: &str) -> HeaderInt {
    let trimmed = text.trim();
    let (negative, digits) = if let Some(rest) = trimmed.strip_prefix('-') {
        (true, rest)
    } else if let Some(rest) = trimmed.strip_prefix('+') {
        (false, rest)
    } else {
        (false, trimmed)
    };
    let normalised = digits.replace('_', "");
    let shape_ok = !normalised.is_empty()
        && normalised.bytes().all(|byte| byte.is_ascii_digit())
        && !digits.starts_with('_')
        && !digits.ends_with('_')
        && !digits.contains("__");
    if !shape_ok {
        return HeaderInt::Invalid;
    }
    if normalised.len() > 18 {
        return if negative {
            HeaderInt::NonPositive
        } else {
            HeaderInt::TooLarge
        };
    }
    let Ok(magnitude) = normalised.parse::<i64>() else {
        return if negative {
            HeaderInt::NonPositive
        } else {
            HeaderInt::TooLarge
        };
    };
    let signed = if negative {
        magnitude.saturating_neg()
    } else {
        magnitude
    };
    if signed <= 0 {
        HeaderInt::NonPositive
    } else if signed > BODY_LIMIT {
        HeaderInt::TooLarge
    } else {
        HeaderInt::Fits(signed)
    }
}

fn execute_run(automation_id: &str, payload: &Value) -> Result<Value, AppError> {
    // `_now_payload` is evaluated before `run_once`, so a bad `now` is 400
    // with no registry read and no history write.
    let now = parse_now(payload)?;
    let automation = crate::automation_definition_routes::find_automation(automation_id)?;
    if !crate::python_is_de_authorised() {
        return Err(crate::automation_definition_routes::write_not_owned());
    }
    let trigger = match payload.get("trigger") {
        Some(value) if value.is_object() => value.clone(),
        _ => json!({"type": "manual"}),
    };
    let event = payload
        .get("event")
        .filter(|value| value.is_object())
        .cloned();
    let run = run_once(
        &automation,
        &trigger,
        event.as_ref(),
        now,
        payload_bool(payload, "confirmed"),
        payload_bool(payload, "force"),
    )?;
    Ok(json!({"ok": true, "run": run}))
}

fn payload_bool(payload: &Value, key: &str) -> bool {
    match payload.get(key) {
        None => false,
        Some(Value::Bool(value)) => *value,
        Some(value) => http_truthy(value),
    }
}

/// `http_utils.truthy`: `str(value or "").strip().lower()` in the four words.
fn http_truthy(value: &Value) -> bool {
    let rendered = match value {
        Value::Null => String::new(),
        Value::Bool(flag) => {
            if *flag {
                "True".to_string()
            } else {
                String::new()
            }
        }
        Value::Number(number) => {
            if number.as_i64() == Some(0)
                || number.as_u64() == Some(0)
                || number.as_f64() == Some(0.0)
            {
                String::new()
            } else if let Some(int) = number.as_i64() {
                int.to_string()
            } else {
                json_number_str(number)
            }
        }
        Value::String(text) if text.is_empty() => String::new(),
        Value::String(text) => text.clone(),
        Value::Array(items) if items.is_empty() => String::new(),
        Value::Object(fields) if fields.is_empty() => String::new(),
        other => value_str(other),
    };
    matches!(
        rendered.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn parse_now(payload: &Value) -> Result<Option<i64>, AppError> {
    match payload.get("now") {
        None => Ok(None),
        Some(value) => parse_now_value(value),
    }
}

fn parse_now_value(value: &Value) -> Result<Option<i64>, AppError> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) => {
            let trimmed = text.trim();
            if trimmed.is_empty() {
                Ok(None)
            } else {
                parse_iso_millis(trimmed).map(Some)
            }
        }
        // `bool` subclasses `int`, so JSON true/false take the numeric branch.
        Value::Bool(flag) => epoch_millis_from_f64(if *flag { 1.0 } else { 0.0 }).map(Some),
        Value::Number(number) => {
            let float = number.as_f64().ok_or_else(server_error)?;
            epoch_millis_from_f64(float).map(Some)
        }
        other => parse_iso_millis(value_str(other).trim()).map(Some),
    }
}

fn now_error() -> AppError {
    AppError::invalid_payload("now must be an ISO timestamp or epoch milliseconds")
}

/// `datetime.fromtimestamp(float(value) / 1000, utc)` then
/// `int(timestamp * 1000)`. Out-of-range values are the uncaught
/// `OverflowError` / `OSError`: `500`, not the ISO 400.
fn epoch_millis_from_f64(value: f64) -> Result<i64, AppError> {
    if !value.is_finite() {
        return Err(server_error());
    }
    let millis = (value / 1000.0 * 1000.0).trunc();
    if !(1.0..253_402_300_799_000.0).contains(&millis) && millis != 0.0 && millis != 1.0 {
        // `fromtimestamp(0)` and `fromtimestamp(0.001)` are in range. Values
        // outside year 1..9999 are the platform error.
        if !(0.0..253_402_300_799_000.0).contains(&millis) {
            return Err(server_error());
        }
    }
    if !(0.0..253_402_300_799_000.0).contains(&millis) {
        return Err(server_error());
    }
    Ok(millis as i64)
}

fn parse_iso_millis(text: &str) -> Result<i64, AppError> {
    let replaced = text.replace('Z', "+00:00");
    let (body, offset_seconds) = split_offset(&replaced)?;
    let (date, time) = split_date_time(body)?;
    let (year, month, day) = parse_ymd(date)?;
    let (hour, minute, second, milli) = if time.is_empty() {
        (0i64, 0i64, 0i64, 0i64)
    } else {
        parse_hms(time)?
    };
    if !(1..=12).contains(&month) || day < 1 || day > i64::from(days_in_month(year, month)) {
        return Err(now_error());
    }
    if !(0..24).contains(&hour) || !(0..60).contains(&minute) || !(0..60).contains(&second) {
        return Err(now_error());
    }
    let days = days_from_civil(year, month as u32, day as u32);
    let local_ms = days * 86_400_000 + hour * 3_600_000 + minute * 60_000 + second * 1_000 + milli;
    Ok(local_ms - offset_seconds * 1_000)
}

fn split_offset(text: &str) -> Result<(&str, i64), AppError> {
    let mut sign_at = None;
    for (index, byte) in text.as_bytes().iter().enumerate().skip(10) {
        if *byte == b'+' || *byte == b'-' {
            sign_at = Some(index);
        }
    }
    let Some(index) = sign_at else {
        return Ok((text, 0));
    };
    Ok((&text[..index], parse_offset(&text[index..])?))
}

fn parse_offset(text: &str) -> Result<i64, AppError> {
    let (sign, rest) = match text.as_bytes().first() {
        Some(b'+') => (1i64, &text[1..]),
        Some(b'-') => (-1i64, &text[1..]),
        _ => return Err(now_error()),
    };
    let mut parts = rest.split(':');
    let hour: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let minute: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let second: i64 = match parts.next() {
        None => 0,
        Some(text) if parts.next().is_none() => text.parse().map_err(|_| now_error())?,
        Some(_) => return Err(now_error()),
    };
    if !(0..24).contains(&hour) || !(0..60).contains(&minute) || !(0..60).contains(&second) {
        return Err(now_error());
    }
    Ok(sign * (hour * 3_600 + minute * 60 + second))
}

fn split_date_time(text: &str) -> Result<(&str, &str), AppError> {
    if text.len() == 10 {
        return Ok((text, ""));
    }
    let bytes = text.as_bytes();
    if bytes.len() > 10 && (bytes[10] == b'T' || bytes[10] == b' ') {
        return Ok((&text[..10], &text[11..]));
    }
    Err(now_error())
}

fn parse_ymd(text: &str) -> Result<(i32, i64, i64), AppError> {
    let mut parts = text.split('-');
    let year: i32 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let month: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let day: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    if parts.next().is_some() || text.len() != 10 {
        return Err(now_error());
    }
    Ok((year, month, day))
}

fn parse_hms(text: &str) -> Result<(i64, i64, i64, i64), AppError> {
    let mut parts = text.split(':');
    let hour: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let minute: i64 = parts
        .next()
        .unwrap_or("")
        .parse()
        .map_err(|_| now_error())?;
    let (second, milli) = match parts.next() {
        None => (0, 0),
        Some(seconds) if parts.next().is_none() => {
            let (whole, frac) = match seconds.split_once('.') {
                Some((whole, frac)) if !frac.is_empty() => (whole, frac),
                Some(_) => return Err(now_error()),
                None => (seconds, ""),
            };
            let second = whole.parse().map_err(|_| now_error())?;
            (second, fraction_millis(frac)?)
        }
        Some(_) => return Err(now_error()),
    };
    Ok((hour, minute, second, milli))
}

fn fraction_millis(frac: &str) -> Result<i64, AppError> {
    if frac.is_empty() {
        return Ok(0);
    }
    if !frac.bytes().all(|byte| byte.is_ascii_digit()) || frac.len() > 12 {
        return Err(now_error());
    }
    let value: f64 = format!("0.{frac}").parse().map_err(|_| now_error())?;
    Ok((value * 1000.0).trunc() as i64)
}

fn days_in_month(year: i32, month: i64) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn is_leap(year: i32) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let m = i64::from(month);
    let d = i64::from(day);
    let mut y = i64::from(year);
    if m <= 2 {
        y -= 1;
    }
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let shifted = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * shifted + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(mut z: i64) -> (i64, i64, i64) {
    z += 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (y + i64::from(month <= 2), month as i64, day as i64)
}

fn civil_parts(millis: i64) -> (i64, i64, i64, i64, i64) {
    let seconds = millis.div_euclid(1000);
    let days = seconds.div_euclid(86_400);
    let tod = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    (year, month, day, tod / 3600, (tod % 3600) / 60)
}

/// Cron weekday: Sunday is 0. Unix day 0 is Thursday, so
/// `(days + 4) mod 7` is the cron weekday of that UTC date.
fn cron_weekday(millis: i64) -> i64 {
    let days = millis.div_euclid(86_400_000);
    (days.rem_euclid(7) + 4).rem_euclid(7)
}

fn run_once(
    automation: &Value,
    trigger: &Value,
    event: Option<&Value>,
    now: Option<i64>,
    confirmed: bool,
    force: bool,
) -> Result<Value, AppError> {
    if !python_truthy(automation.get("enabled").unwrap_or(&Value::Null)) && !force {
        return record_terminal(
            automation,
            trigger,
            "skipped",
            "automation_disabled",
            &[String::from("automation disabled")],
            &json!({}),
            now,
        );
    }
    if !force {
        let (matched, reason) = trigger_matches(automation, trigger, event, now)?;
        if !matched {
            let logs = [reason.clone()];
            return record_terminal(
                automation,
                trigger,
                "skipped",
                &reason,
                &logs,
                &json!({}),
                now,
            );
        }
        let (condition_ok, condition_reason) = condition_matches(automation, event)?;
        if !condition_ok {
            let logs = [condition_reason.clone()];
            return record_terminal(
                automation,
                trigger,
                "skipped",
                &condition_reason,
                &logs,
                &json!({}),
                now,
            );
        }
    }
    let decision = evaluate_policy(automation, trigger, now, confirmed)?;
    if decision.verdict == "needs_confirmation" {
        let evidence = json!({"policy": decision.to_value()});
        return record_terminal(
            automation,
            trigger,
            "requires_confirmation",
            "policy_requires_confirmation",
            &decision.reasons,
            &evidence,
            now,
        );
    }
    if decision.verdict != "allow" {
        let evidence = json!({"policy": decision.to_value()});
        let skipped = format!("policy_denied:{}", decision.reasons.join(","));
        return record_terminal(
            automation,
            trigger,
            "skipped",
            &skipped,
            &decision.reasons,
            &evidence,
            now,
        );
    }
    execute_allowed(automation, trigger, event, &decision, now)
}

struct Decision {
    verdict: &'static str,
    risk: &'static str,
    reasons: Vec<String>,
    policy: Value,
}

impl Decision {
    fn to_value(&self) -> Value {
        json!({
            "verdict": self.verdict,
            "risk": self.risk,
            "reasons": self.reasons,
            "policy": self.policy,
        })
    }
}

fn evaluate_policy(
    automation: &Value,
    trigger: &Value,
    now: Option<i64>,
    confirmed: bool,
) -> Result<Decision, AppError> {
    let policy = match automation.get("policy") {
        Some(value) if value.is_object() => value.clone(),
        _ => json!({}),
    };
    let action = automation.get("action").filter(|value| value.is_object());
    let action_type = python_str_or(action.and_then(|item| item.get("type")), "")
        .trim()
        .to_lowercase();
    if !env_bool("AUTOMATION_ENABLED", true) {
        return Ok(deny("automation_disabled", "high", &policy));
    }
    if python_truthy(policy.get("requiresConfirmation").unwrap_or(&Value::Null)) && !confirmed {
        return Ok(Decision {
            verdict: "needs_confirmation",
            risk: "medium",
            reasons: vec!["automation_requires_confirmation".to_string()],
            policy,
        });
    }
    let max_runs = configured_int(
        policy.get("maxRunsPerDay"),
        env_int_clamped("AUTOMATION_MAX_RUNS_PER_DAY", 50, 1, 10_000),
    )
    .max(1);
    let today = runs_today(&text_or_empty(automation.get("automationId")), now)?;
    if today >= max_runs {
        return Ok(deny("max_runs_per_day_exceeded", "medium", &policy));
    }
    let mut risk = "low";
    if matches!(action_type.as_str(), "browser_snapshot" | "browser_check") {
        risk = "medium";
        if !python_truthy(policy.get("allowBrowser").unwrap_or(&Value::Null)) {
            return Ok(deny("browser_not_allowed", risk, &policy));
        }
        let browser_mode = python_str_or(policy.get("browserMode"), "read_only");
        if browser_mode == "disabled" {
            return Ok(deny("browser_mode_disabled", risk, &policy));
        }
        let url = python_str_or(
            action.and_then(|item| item.get("url").or_else(|| item.get("downloadUrl"))),
            "",
        );
        let url = url.trim().to_string();
        if network_url(&url) && !python_truthy(policy.get("allowNetwork").unwrap_or(&Value::Null)) {
            return Ok(deny("network_not_allowed", risk, &policy));
        }
        if !url.is_empty()
            && !python_truthy(policy.get("allowPrivateHosts").unwrap_or(&Value::Null))
        {
            let (safe, reason) = evaluate_url_safety(&url, &BrowserSettings::from_env());
            if !safe {
                return Ok(deny_owned(
                    format!("unsafe_url:{reason}"),
                    "critical",
                    policy,
                ));
            }
        }
    } else if !LOCAL_ACTIONS.contains(&action_type.as_str()) {
        return Ok(deny("unsupported_action", "high", &policy));
    }
    let mut reasons = Vec::new();
    let browser_write = python_str_or(action.and_then(|item| item.get("browserAction")), "")
        .trim()
        .to_lowercase();
    if !browser_write.is_empty() && BROWSER_WRITE_ACTIONS.contains(&browser_write.as_str()) {
        risk = "high";
        if env_bool("AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE", true) && !confirmed {
            reasons.push("browser_write_requires_confirmation".to_string());
        }
    }
    let trigger_type = python_str_or(trigger.get("type"), "").to_lowercase();
    if trigger_type != "manual"
        && action_type == "run_skill"
        && python_truthy(
            action
                .and_then(|item| item.get("allowNetwork"))
                .unwrap_or(&Value::Null),
        )
    {
        risk = "medium";
        reasons.push("scheduled_skill_network_requires_confirmation".to_string());
    }
    if !reasons.is_empty() {
        reasons.sort();
        reasons.dedup();
        return Ok(Decision {
            verdict: "needs_confirmation",
            risk,
            reasons,
            policy,
        });
    }
    Ok(Decision {
        verdict: "allow",
        risk,
        reasons,
        policy,
    })
}

fn deny(reason: &str, risk: &'static str, policy: &Value) -> Decision {
    Decision {
        verdict: "deny",
        risk,
        reasons: vec![reason.to_string()],
        policy: policy.clone(),
    }
}

fn deny_owned(reason: String, risk: &'static str, policy: Value) -> Decision {
    Decision {
        verdict: "deny",
        risk,
        reasons: vec![reason],
        policy,
    }
}

fn network_url(url: &str) -> bool {
    let Some((scheme, _)) = url.split_once(':') else {
        return false;
    };
    matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https")
}

fn configured_int(value: Option<&Value>, default: i64) -> i64 {
    match value {
        Some(found) if python_truthy(found) => python_int_opt(Some(found)).unwrap_or(default),
        _ => default,
    }
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

fn env_int_clamped(name: &str, default: i64, minimum: i64, maximum: i64) -> i64 {
    let value = match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => {
            python_int_opt(Some(&Value::String(raw))).unwrap_or(default)
        }
        _ => default,
    };
    value.clamp(minimum, maximum)
}

fn runs_today(automation_id: &str, now: Option<i64>) -> Result<i64, AppError> {
    let current = now.unwrap_or_else(|| SystemEntropy.now_millis());
    let start = current.div_euclid(86_400_000) * 86_400_000;
    let end = start + 86_400_000;
    let mut total = 0i64;
    for run in load_runs()? {
        if run.get("automationId").and_then(Value::as_str) != Some(automation_id) {
            continue;
        }
        let started = run.get("startedAtMs").and_then(Value::as_i64).unwrap_or(0);
        let status = run.get("status").and_then(Value::as_str).unwrap_or("");
        if (start..end).contains(&started)
            && status != "skipped"
            && status != "requires_confirmation"
        {
            total += 1;
        }
    }
    Ok(total)
}

fn trigger_matches(
    automation: &Value,
    trigger: &Value,
    event: Option<&Value>,
    now: Option<i64>,
) -> Result<(bool, String), AppError> {
    let trigger_type = python_str_or(trigger.get("type"), "manual")
        .trim()
        .to_lowercase();
    let current = timestamp_ms(now, 0);
    if trigger_type == "manual" {
        return Ok((true, String::new()));
    }
    if trigger_type == "event" {
        let expected = python_str_or(trigger.get("event"), "")
            .trim()
            .to_lowercase();
        let actual = event_name(event);
        let matched = !expected.is_empty() && expected == actual;
        let reason = if expected != actual {
            "event_not_matched".to_string()
        } else {
            String::new()
        };
        return Ok((matched, reason));
    }
    if trigger_type == "interval" {
        let automation_id = text_or_empty(automation.get("automationId"));
        let last = latest_run(&automation_id, &["success", "failed"])?;
        let minimum = env_int_clamped("AUTOMATION_MIN_INTERVAL_SECONDS", 300, 1, 86_400);
        let interval = safe_trigger_int(trigger.get("intervalSeconds"), minimum).max(minimum);
        let Some(last) = last else {
            return Ok((true, String::new()));
        };
        let started = last.get("startedAtMs").and_then(Value::as_i64).unwrap_or(0);
        if current - started >= interval.saturating_mul(1000) {
            return Ok((true, String::new()));
        }
        return Ok((false, "interval_not_due".to_string()));
    }
    if trigger_type == "schedule" {
        let cron = python_str_or(trigger.get("cron"), "");
        let cron = cron.trim();
        if !cron_matches(cron, current) {
            return Ok((false, "schedule_not_due".to_string()));
        }
        let automation_id = text_or_empty(automation.get("automationId"));
        let Some(last) = latest_run(&automation_id, &["success", "failed", "skipped"])? else {
            return Ok((true, String::new()));
        };
        let last_ms = last.get("startedAtMs").and_then(Value::as_i64).unwrap_or(0);
        if same_civil_minute(last_ms, current) {
            return Ok((false, "schedule_already_ran".to_string()));
        }
        return Ok((true, String::new()));
    }
    Ok((false, "unsupported_trigger".to_string()))
}

fn safe_trigger_int(value: Option<&Value>, default: i64) -> i64 {
    value
        .and_then(|item| python_int_opt(Some(item)))
        .unwrap_or(default)
}

fn event_name(event: Option<&Value>) -> String {
    let Some(event) = event else {
        return String::new();
    };
    let primary = text_or_empty(event.get("event"));
    let raw = if primary.is_empty() {
        text_or_empty(event.get("type"))
    } else {
        primary
    };
    raw.trim().to_lowercase()
}

fn same_civil_minute(left: i64, right: i64) -> bool {
    civil_parts(left) == civil_parts(right)
}

fn cron_matches(cron: &str, now_ms: i64) -> bool {
    let parts: Vec<&str> = cron.split_whitespace().collect();
    if parts.len() != 5 {
        return false;
    }
    let (_year, month, day, hour, minute) = civil_parts(now_ms);
    let fields = [
        (parts[0], minute, 0i64, 59i64),
        (parts[1], hour, 0, 23),
        (parts[2], day, 1, 31),
        (parts[3], month, 1, 12),
        (parts[4], cron_weekday(now_ms), 0, 7),
    ];
    fields
        .into_iter()
        .all(|(field, value, minimum, maximum)| cron_field_matches(field, value, minimum, maximum))
}

fn cron_field_matches(field: &str, value: i64, minimum: i64, maximum: i64) -> bool {
    let raw = field.trim();
    if raw == "*" {
        return true;
    }
    raw.split(',')
        .any(|part| cron_part_matches(part.trim(), value, minimum, maximum))
}

fn cron_part_matches(part: &str, value: i64, minimum: i64, maximum: i64) -> bool {
    if part.is_empty() {
        return false;
    }
    let (base, step) = if let Some((base, raw_step)) = part.split_once('/') {
        let Ok(step) = raw_step.parse::<i64>() else {
            return false;
        };
        if step <= 0 {
            return false;
        }
        (base, step)
    } else {
        (part, 1i64)
    };
    let (start, end) = if base == "*" {
        (minimum, maximum)
    } else if let Some((raw_start, raw_end)) = base.split_once('-') {
        let (Ok(start), Ok(end)) = (raw_start.parse::<i64>(), raw_end.parse::<i64>()) else {
            return false;
        };
        if start > end {
            return false;
        }
        (start, end)
    } else {
        let Ok(point) = base.parse::<i64>() else {
            return false;
        };
        (point, point)
    };
    if start < minimum || end > maximum {
        return false;
    }
    if value == 0 && maximum == 7 && start <= 7 && end >= 7 && (7 - start) % step == 0 {
        return true;
    }
    start <= value && value <= end && (value - start) % step == 0
}

fn condition_matches(
    automation: &Value,
    event: Option<&Value>,
) -> Result<(bool, String), AppError> {
    let condition = automation
        .get("condition")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let condition_type = python_str_or(condition.get("type"), "always")
        .trim()
        .to_lowercase();
    if condition_type == "always" {
        return Ok((true, String::new()));
    }
    if condition_type == "project_changed"
        || python_truthy(condition.get("projectChanged").unwrap_or(&Value::Null))
    {
        return project_changed(automation);
    }
    if condition_type == "media_ready"
        || python_truthy(condition.get("newMediaReady").unwrap_or(&Value::Null))
    {
        return Ok(event_flag(event, "media.ready", "media_not_ready"));
    }
    if condition_type == "new_saved_items"
        || python_truthy(condition.get("newSavedItems").unwrap_or(&Value::Null))
    {
        return Ok(event_flag(
            event,
            "saved_item.created",
            "saved_item_not_created",
        ));
    }
    if condition_type == "artifact_created"
        || python_truthy(condition.get("artifactCreated").unwrap_or(&Value::Null))
    {
        return Ok(event_flag(
            event,
            "artifact.created",
            "artifact_not_created",
        ));
    }
    if condition_type == "url_changed"
        || python_truthy(condition.get("urlChanged").unwrap_or(&Value::Null))
    {
        return Ok((true, String::new()));
    }
    Ok((false, "condition_not_met".to_string()))
}

fn event_flag(event: Option<&Value>, expected: &str, reason: &str) -> (bool, String) {
    let actual = event_name(event);
    if actual == expected {
        (true, String::new())
    } else {
        (false, reason.to_string())
    }
}

fn project_changed(automation: &Value) -> Result<(bool, String), AppError> {
    let project_id = text_or_empty(automation.get("projectId"));
    if project_id.is_empty() {
        return Ok((true, String::new()));
    }
    let root = crate::data_routes::workspace_root();
    let project = match require_project(&project_id, &root, &SystemEntropy) {
        Ok(project) => project,
        Err(_) => return Ok((false, "project_not_found".to_string())),
    };
    let updated_at = match project.get("updatedAt") {
        Some(value) if python_truthy(value) => value.as_i64().unwrap_or(0),
        _ => 0,
    };
    let automation_id = text_or_empty(automation.get("automationId"));
    let Some(last) = latest_run(&automation_id, &["success"])? else {
        return Ok((true, String::new()));
    };
    let last_finished = match last.get("finishedAtMs") {
        Some(value) if python_truthy(value) => value.as_i64().unwrap_or(0),
        _ => match last.get("startedAtMs") {
            Some(value) if python_truthy(value) => value.as_i64().unwrap_or(0),
            _ => 0,
        },
    };
    if updated_at > last_finished {
        Ok((true, String::new()))
    } else {
        Ok((false, "project_unchanged".to_string()))
    }
}

fn latest_run(automation_id: &str, statuses: &[&str]) -> Result<Option<Value>, AppError> {
    let mut runs = load_runs()?;
    runs.retain(|run| run.get("automationId").and_then(Value::as_str) == Some(automation_id));
    runs.sort_by_key(|run| std::cmp::Reverse(started_at_ms(run)));
    Ok(runs.into_iter().find(|run| {
        statuses
            .iter()
            .any(|status| run.get("status").and_then(Value::as_str) == Some(*status))
    }))
}

fn execute_allowed(
    automation: &Value,
    trigger: &Value,
    event: Option<&Value>,
    decision: &Decision,
    now: Option<i64>,
) -> Result<Value, AppError> {
    let run_id = new_id("auto_run", &SystemEntropy)?;
    let started_ms = timestamp_ms(now, 0);
    let policy = automation.get("policy").filter(|value| value.is_object());
    let retry = policy
        .and_then(|item| item.get("retry"))
        .filter(|value| value.is_object());
    let max_attempts = configured_int(retry.and_then(|item| item.get("maxAttempts")), 1).max(1);
    let backoff_seconds =
        configured_int(retry.and_then(|item| item.get("backoffSeconds")), 0).max(0);
    let timeout_seconds =
        configured_int(policy.and_then(|item| item.get("timeoutSeconds")), 1_800).max(1);
    let started = Instant::now();
    let mut attempts = 0i64;
    let mut logs: Vec<String> = Vec::new();
    let mut last_error = String::new();
    let mut outputs = empty_outputs();
    let mut raw_result = json!({});
    let mut timeout_checked_at_ms = started_ms;
    let mut attempt_errors: Vec<Value> = Vec::new();
    for attempt in 1..=max_attempts {
        attempts = attempt;
        timeout_checked_at_ms = timestamp_ms(now, elapsed_millis(started));
        if started.elapsed().as_secs_f64() > timeout_seconds as f64 {
            last_error = "Automation run exceeded timeout before attempt".to_string();
            logs.push(last_error.clone());
            attempt_errors.push(json!({
                "attempt": attempt,
                "error": last_error.clone(),
                "timeoutCheckedAtMs": timeout_checked_at_ms,
            }));
            break;
        }
        match run_action(automation, &run_id, trigger, event) {
            Ok(result) => {
                outputs = merge_outputs(outputs, &result.outputs);
                logs.extend(result.logs.into_iter().filter(|line| !line.is_empty()));
                raw_result = result.raw;
                let skipped_reason = result.skipped_reason;
                timeout_checked_at_ms = timestamp_ms(now, elapsed_millis(started));
                if started.elapsed().as_secs_f64() > timeout_seconds as f64 {
                    last_error = "Automation run exceeded timeout".to_string();
                    logs.push(format!("attempt {attempt} failed: {last_error}"));
                    attempt_errors.push(json!({
                        "attempt": attempt,
                        "error": last_error.clone(),
                        "timeoutCheckedAtMs": timeout_checked_at_ms,
                    }));
                    if attempt >= max_attempts
                        || !sleep_for_retry(started, backoff_seconds, timeout_seconds, &mut logs)
                    {
                        break;
                    }
                    continue;
                }
                let status = if skipped_reason.is_empty() {
                    "success"
                } else {
                    "skipped"
                };
                let finished_ms = timestamp_ms(now, elapsed_millis(started));
                return record_with_memory(
                    automation,
                    &run_record(RunRecordInput {
                        run_id: &run_id,
                        automation,
                        status,
                        started_ms,
                        finished_ms,
                        trigger,
                        outputs: &outputs,
                        attempts,
                        error: "",
                        skipped_reason: &skipped_reason,
                        logs: &logs,
                        evidence: &json!({
                            "policy": decision.to_value(),
                            "action": raw_result,
                            "runtime": runtime_evidence(
                                max_attempts,
                                backoff_seconds,
                                timeout_seconds,
                                timeout_checked_at_ms,
                                &attempt_errors,
                            ),
                        }),
                    }),
                );
            }
            Err(error) => {
                last_error = error.message;
                timeout_checked_at_ms = timestamp_ms(now, elapsed_millis(started));
                logs.push(format!("attempt {attempt} failed: {last_error}"));
                attempt_errors.push(json!({
                    "attempt": attempt,
                    "error": last_error.clone(),
                    "timeoutCheckedAtMs": timeout_checked_at_ms,
                }));
                if attempt >= max_attempts
                    || !sleep_for_retry(started, backoff_seconds, timeout_seconds, &mut logs)
                {
                    break;
                }
            }
        }
    }
    let finished_ms = timestamp_ms(now, elapsed_millis(started));
    record_with_memory(
        automation,
        &run_record(RunRecordInput {
            run_id: &run_id,
            automation,
            status: "failed",
            started_ms,
            finished_ms,
            trigger,
            outputs: &outputs,
            attempts: attempts.max(1),
            error: &last_error,
            skipped_reason: "",
            logs: &logs,
            evidence: &json!({
                "policy": decision.to_value(),
                "action": raw_result,
                "runtime": runtime_evidence(
                    max_attempts,
                    backoff_seconds,
                    timeout_seconds,
                    timeout_checked_at_ms,
                    &attempt_errors,
                ),
            }),
        }),
    )
}

fn elapsed_millis(started: Instant) -> i64 {
    i64::try_from(started.elapsed().as_millis()).unwrap_or(i64::MAX)
}

fn sleep_for_retry(
    started: Instant,
    backoff_seconds: i64,
    timeout_seconds: i64,
    logs: &mut Vec<String>,
) -> bool {
    if backoff_seconds <= 0 {
        return true;
    }
    if started.elapsed().as_secs_f64() + backoff_seconds as f64 > timeout_seconds as f64 {
        logs.push("retry backoff skipped: timeout budget exhausted".to_string());
        return false;
    }
    logs.push(format!("retry backoff: {backoff_seconds}s"));
    std::thread::sleep(std::time::Duration::from_secs(backoff_seconds as u64));
    true
}

fn runtime_evidence(
    max_attempts: i64,
    backoff_seconds: i64,
    timeout_seconds: i64,
    timeout_checked_at_ms: i64,
    attempt_errors: &[Value],
) -> Value {
    json!({
        "maxAttempts": max_attempts,
        "backoffSeconds": backoff_seconds,
        "timeoutSeconds": timeout_seconds,
        "timeoutCheckedAtMs": timeout_checked_at_ms,
        "attemptErrors": attempt_errors,
    })
}

struct RunRecordInput<'a> {
    run_id: &'a str,
    automation: &'a Value,
    status: &'a str,
    started_ms: i64,
    finished_ms: i64,
    trigger: &'a Value,
    outputs: &'a Value,
    attempts: i64,
    error: &'a str,
    skipped_reason: &'a str,
    logs: &'a [String],
    evidence: &'a Value,
}

fn run_record(input: RunRecordInput<'_>) -> Value {
    let RunRecordInput {
        run_id,
        automation,
        status,
        started_ms,
        finished_ms,
        trigger,
        outputs,
        attempts,
        error,
        skipped_reason,
        logs,
        evidence,
    } = input;
    json!({
        "runId": run_id,
        "automationId": automation.get("automationId").cloned().unwrap_or(Value::Null),
        "projectId": automation.get("projectId").cloned().unwrap_or(Value::Null),
        "status": status,
        "startedAtMs": started_ms,
        "finishedAtMs": finished_ms,
        "durationMs": finished_ms.saturating_sub(started_ms),
        "startedAt": timestamp_ms_to_iso(Some(&Value::from(started_ms))),
        "finishedAt": timestamp_ms_to_iso(Some(&Value::from(finished_ms))),
        "trigger": trigger,
        "outputs": outputs,
        "traceId": "",
        "attempts": attempts,
        "error": error,
        "skippedReason": skipped_reason,
        "logs": logs,
        "evidence": evidence,
    })
}

/// No Rust trace store is wired. `traceId` stays empty; the oracle comparison
/// strips it. Terminal skips still append a history row.
fn record_terminal(
    automation: &Value,
    trigger: &Value,
    status: &str,
    skipped_reason: &str,
    logs: &[String],
    evidence: &Value,
    now: Option<i64>,
) -> Result<Value, AppError> {
    let current_ms = timestamp_ms(now, 0);
    let run_id = new_id("auto_run", &SystemEntropy)?;
    record_with_memory(
        automation,
        &run_record(RunRecordInput {
            run_id: &run_id,
            automation,
            status,
            started_ms: current_ms,
            finished_ms: current_ms,
            trigger,
            outputs: &empty_outputs(),
            attempts: 1,
            error: "",
            skipped_reason,
            logs,
            evidence,
        }),
    )
}

fn timestamp_ms(now: Option<i64>, elapsed_ms: i64) -> i64 {
    match now {
        // `now_ms()` ignores the monotonic elapsed time.
        None => SystemEntropy.now_millis(),
        Some(base) => base.saturating_add(elapsed_ms.max(0)),
    }
}

fn record_with_memory(automation: &Value, record: &Value) -> Result<Value, AppError> {
    let run = record_run(record)?;
    write_memory_summary(automation, &run);
    Ok(run)
}

fn record_run(record: &Value) -> Result<Value, AppError> {
    let run = normalize_run_record(record)?;
    let run_id = text_or_empty(run.get("runId"));
    let mut runs = load_runs()?;
    runs.retain(|item| item.get("runId").and_then(Value::as_str) != Some(run_id.as_str()));
    runs.push(run.clone());
    if runs.len() > MAX_RUNS {
        runs = runs.split_off(runs.len() - MAX_RUNS);
    }
    let root = crate::data_routes::workspace_root();
    let path = root.join(".automation").join("history.json");
    write_json_atomic(&root, &path, &json!({"runs": runs}))?;
    Ok(run)
}

fn write_memory_summary(automation: &Value, run: &Value) {
    if run.get("status").and_then(Value::as_str) != Some("success") {
        return;
    }
    let project_id = {
        let from_run = text_or_empty(run.get("projectId"));
        if from_run.is_empty() {
            text_or_empty(automation.get("projectId"))
        } else {
            from_run
        }
    };
    let automation_id = {
        let from_run = text_or_empty(run.get("automationId"));
        if from_run.is_empty() {
            text_or_empty(automation.get("automationId"))
        } else {
            from_run
        }
    };
    if project_id.is_empty() && automation_id.is_empty() {
        return;
    }
    let outputs = run.get("outputs");
    let mut counts = Vec::new();
    for (key, label) in [
        ("artifactIds", "artifacts"),
        ("savedItemIds", "saved items"),
        ("mediaIds", "media items"),
        ("exportIds", "exports"),
    ] {
        let count = outputs
            .and_then(|value| value.get(key))
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        if count > 0 {
            counts.push(format!("{count} {label}"));
        }
    }
    let output_summary = if counts.is_empty() {
        "no persisted outputs".to_string()
    } else {
        counts.join(", ")
    };
    let name = text_or_empty(automation.get("name"));
    let name = if name.is_empty() {
        automation_id.clone()
    } else {
        name
    };
    let content = format!("Automation {name} completed successfully with {output_summary}.");
    let run_id = text_or_empty(run.get("runId"));
    let source = json!({
        "kind": "automation",
        "refId": run_id,
        "automationId": automation_id,
        "runId": run_id,
    });
    let scope = if project_id.is_empty() {
        "automation"
    } else {
        "project"
    };
    let root = crate::data_routes::workspace_root();
    let _ = add_memory(
        &content,
        scope,
        "summary",
        &project_id,
        "",
        &automation_id,
        Some(&source),
        0.8,
        "",
        false,
        &root,
        &SystemClock,
    );
}

struct ActionOutput {
    outputs: Value,
    logs: Vec<String>,
    raw: Value,
    skipped_reason: String,
}

fn run_action(
    automation: &Value,
    run_id: &str,
    _trigger: &Value,
    _event: Option<&Value>,
) -> Result<ActionOutput, AppError> {
    let action = automation
        .get("action")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let action_type = python_str_or(action.get("type"), "").trim().to_lowercase();
    if action_type == "save_item" {
        return save_item_action(automation, &action, run_id);
    }
    // Policy already allowed this type. Executing it would call a runtime that
    // is not ported (skill, browser, summary, media, artifact, export). Record
    // the failure instead of inventing a success or answering 501.
    Err(AppError::invalid_payload(format!(
        "{action_type} is not executed by the native gateway yet"
    )))
}

fn save_item_action(
    automation: &Value,
    action: &Value,
    run_id: &str,
) -> Result<ActionOutput, AppError> {
    let project_id = {
        let from_action = text_or_empty(action.get("projectId"));
        if from_action.is_empty() {
            text_or_empty(automation.get("projectId"))
        } else {
            from_action
        }
    };
    if project_id.is_empty() {
        return Err(AppError::invalid_payload(
            "save_item action requires projectId",
        ));
    }
    let title = {
        let from_action = text_or_empty(action.get("title"));
        if !from_action.is_empty() {
            from_action
        } else {
            let name = text_or_empty(automation.get("name"));
            if name.is_empty() {
                "Automation saved item".to_string()
            } else {
                name
            }
        }
    };
    let item = create_saved_item(
        &project_id,
        &python_str_or(action.get("itemType"), "assistant_answer"),
        &title,
        &text_or_empty(action.get("content")),
        &json!({
            "type": "automation",
            "automationId": automation.get("automationId").cloned().unwrap_or(Value::Null),
            "runId": run_id,
        }),
        &save_item_tags(action),
        &python_str_or(action.get("purpose"), "reference"),
    )?;
    let saved_id = text_or_empty(item.get("savedId"));
    Ok(ActionOutput {
        outputs: json!({
            "artifactIds": [],
            "savedItemIds": [saved_id],
            "mediaIds": [],
            "exportIds": [],
        }),
        logs: vec!["saveItem".to_string()],
        raw: json!({"savedItem": item}),
        skipped_reason: String::new(),
    })
}

fn save_item_tags(action: &Value) -> Vec<Value> {
    let mut tags = vec![Value::String("automation".to_string())];
    if let Some(Value::Array(items)) = action.get("tags") {
        for tag in items {
            if let Value::String(text) = tag {
                tags.push(Value::String(text.clone()));
            }
        }
    }
    tags
}

fn create_saved_item(
    project_id: &str,
    item_type: &str,
    title: &str,
    content: &str,
    source_ref: &Value,
    tags: &[Value],
    purpose: &str,
) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let root = crate::data_routes::workspace_root();
    require_project(&safe_project_id, &root, &SystemEntropy)?;
    let mut items = load_saved_items(&safe_project_id)?;
    if items.len() >= MAX_SAVED_ITEMS {
        return Err(AppError {
            status: 413,
            code: codes::UPLOAD_TOO_LARGE,
            message: "Too many saved items".into(),
        });
    }
    let created_at = SystemEntropy.now_millis();
    let item = json!({
        "savedId": new_id("save", &SystemEntropy)?,
        "projectId": safe_project_id,
        "type": normalize_saved_type(Some(&Value::String(item_type.to_string())))?,
        "title": normalize_title(Some(&Value::String(title.to_string())), "Saved item"),
        "content": normalize_content(Some(&Value::String(content.to_string()))),
        "sourceRef": normalize_source_ref(source_ref),
        "tags": normalize_tags(Some(&Value::Array(tags.to_vec()))),
        "purpose": normalize_saved_purpose(Some(&Value::String(purpose.to_string()))),
        "createdAt": timestamp_ms_to_iso(Some(&Value::from(created_at))),
        "createdAtMs": created_at,
    });
    items.push(item.clone());
    write_saved_items(&safe_project_id, &items)?;
    touch_saved_project(&safe_project_id)?;
    Ok(item)
}

fn saved_items_path(project_id: &str) -> std::path::PathBuf {
    crate::data_routes::workspace_root()
        .join(".projects")
        .join(project_id)
        .join("saved-items.json")
}

fn load_saved_items(project_id: &str) -> Result<Vec<Value>, AppError> {
    let path = saved_items_path(project_id);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let data = read_json_file(&path, json!({"items": []}));
    let Some(raw_items) = data.get("items").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut items = Vec::new();
    for raw in raw_items {
        if !raw.is_object() {
            continue;
        }
        let primary = text_or_empty(raw.get("savedId"));
        let saved_id = if primary.is_empty() {
            text_or_empty(raw.get("id"))
        } else {
            primary
        };
        if saved_id.is_empty() {
            continue;
        }
        let stored_project = text_or_empty(raw.get("projectId"));
        let project = if stored_project.trim().is_empty() {
            project_id.to_string()
        } else {
            stored_project
        };
        let Ok(project) = validate_project_id(project.trim()) else {
            continue;
        };
        let Ok(item_type) = normalize_saved_type(raw.get("type")) else {
            continue;
        };
        let created_at = safe_int(raw.get("createdAtMs"), 0);
        let created_text = text_or_empty(raw.get("createdAt"));
        let created_at_text = if created_text.is_empty() {
            timestamp_ms_to_iso(Some(&Value::from(created_at)))
        } else {
            created_text
        };
        items.push(json!({
            "savedId": saved_id,
            "projectId": project,
            "type": item_type,
            "title": normalize_title(raw.get("title"), "Saved item"),
            "content": normalize_content(raw.get("content")),
            "sourceRef": normalize_source_ref(raw.get("sourceRef").unwrap_or(&Value::Null)),
            "tags": normalize_tags(raw.get("tags")),
            "purpose": normalize_saved_purpose(raw.get("purpose")),
            "createdAt": created_at_text,
            "createdAtMs": created_at,
        }));
    }
    if items.len() > MAX_SAVED_ITEMS {
        items = items.split_off(items.len() - MAX_SAVED_ITEMS);
    }
    Ok(items)
}

fn write_saved_items(project_id: &str, items: &[Value]) -> Result<(), AppError> {
    let kept = if items.len() > MAX_SAVED_ITEMS {
        &items[items.len() - MAX_SAVED_ITEMS..]
    } else {
        items
    };
    let root = crate::data_routes::workspace_root();
    write_json_atomic(
        &root,
        &saved_items_path(project_id),
        &json!({"items": kept}),
    )
}

fn touch_saved_project(project_id: &str) -> Result<(), AppError> {
    let root = crate::data_routes::workspace_root();
    let entropy = SystemEntropy;
    let mut project = require_project(project_id, &root, &entropy)?;
    if let Some(fields) = project.as_object_mut() {
        fields.insert("updatedAt".to_string(), json!(entropy.now_millis()));
    }
    write_project(&root, &project, &entropy)
}

fn empty_outputs() -> Value {
    json!({"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": []})
}

fn merge_outputs(left: Value, right: &Value) -> Value {
    let mut merged = Map::new();
    for key in ["artifactIds", "savedItemIds", "mediaIds", "exportIds"] {
        let mut values = id_values(left.get(key));
        for value in id_values(right.get(key)) {
            if !value.is_empty() && !values.contains(&value) {
                values.push(value);
            }
        }
        merged.insert(key.to_string(), json!(values));
    }
    Value::Object(merged)
}

fn id_values(value: Option<&Value>) -> Vec<String> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    items
        .iter()
        .map(|item| {
            if python_truthy(item) {
                value_str(item).trim().to_string()
            } else {
                String::new()
            }
        })
        .collect()
}

fn python_str_or(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => default.to_string(),
    }
}
