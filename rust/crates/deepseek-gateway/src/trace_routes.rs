//! Read-only trace routes: `GET /api/traces`, `GET /api/traces/{trace_id}` and
//! `GET /api/traces/{trace_id}/export.json`.
//!
//! The Rust edge reads the same WAL-mode SQLite trace store Python writes
//! (`.traces/traces.sqlite3`). This module is a read-only port of the original
//! `observability` queries and the `observability/export.py` redaction rules; it
//! never creates the directory, the database or its schema. The Python side is
//! still the authoritative writer for this store (`observability_trace_store` is
//! a declared Rust data domain awaiting cutover), so the routes open the file
//! read-only and treat a missing store like an empty one.
//!
//! `TRACE_ENABLED`, `TRACE_LIST_LIMIT`, `TRACE_INPUT_CHARS` and
//! `TRACE_OUTPUT_CHARS` keep the original env names and clamps. The trace page
//! (`/trace/{trace_id}`) is served by the SPA fallback for now; its original
//! auth + trace-existence checks are a recorded follow-up.

use std::path::PathBuf;
use std::sync::LazyLock;

use axum::extract::{Path as AxumPath, RawQuery};
use axum::http::{HeaderValue, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use regex::Regex;
use rusqlite::{Connection, OpenFlags};
use serde_json::{Map, Value, json};

use crate::route_support;
use crate::scheduler_routes::{env_bool, env_int_clamped};

const TRACE_SCHEMA_VERSION: &str = "trace-export.v1";
const MAX_EXPORT_STRING_CHARS: usize = 2_000;
const MAX_PRIVATE_CONTENT_CHARS: usize = 1_200;

pub fn router() -> Router {
    Router::new()
        .route("/api/traces", any(list_traces_response))
        .route("/api/traces/:trace_id", any(trace_detail_response))
        .route(
            "/api/traces/:trace_id/export.json",
            any(trace_export_response),
        )
}

struct TraceSettings {
    enabled: bool,
    root: PathBuf,
    list_limit: i64,
}

impl TraceSettings {
    fn from_env() -> Self {
        Self {
            enabled: env_bool("TRACE_ENABLED", true),
            root: crate::data_routes::workspace_root(),
            // `_env_int_clamped("TRACE_LIST_LIMIT", 100, 10, 1_000)`
            list_limit: env_int_clamped("TRACE_LIST_LIMIT", 100, 10, 1_000),
        }
    }

    fn directory(&self) -> PathBuf {
        self.root.join(".traces")
    }

    fn database(&self) -> PathBuf {
        self.directory().join("traces.sqlite3")
    }

    fn database_path(&self) -> String {
        self.database().to_string_lossy().to_string()
    }
}

async fn list_traces_response(method: Method, query: RawQuery) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return fallback_for(method);
    }
    let joined = tokio::task::spawn_blocking(move || list_body(query.0.as_deref())).await;
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn trace_detail_response(method: Method, AxumPath(trace_id): AxumPath<String>) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return fallback_for(method);
    }
    let joined = tokio::task::spawn_blocking(move || {
        trace_body(&trace_id).map(|body| body.map(|trace| json!({"ok": true, "trace": trace})))
    })
    .await;
    match joined {
        Ok(Ok(Some(_))) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(Some(body))) => Json(body).into_response(),
        Ok(Ok(None)) if method == Method::HEAD => StatusCode::NOT_FOUND.into_response(),
        Ok(Ok(None)) => trace_not_found(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn trace_export_response(method: Method, AxumPath(trace_id): AxumPath<String>) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return fallback_for(method);
    }
    let exported_id = trace_id.clone();
    let joined = tokio::task::spawn_blocking(move || export_body(&trace_id)).await;
    match joined {
        Ok(Ok(Some(body))) => {
            let mut response = if method == Method::HEAD {
                StatusCode::OK.into_response()
            } else {
                Json(body).into_response()
            };
            if let Ok(value) = HeaderValue::from_str(&format!(
                "attachment; filename=\"{}\"",
                export_filename(&exported_id)
            )) {
                response
                    .headers_mut()
                    .insert(header::CONTENT_DISPOSITION, value);
            }
            response
        }
        Ok(Ok(None)) if method == Method::HEAD => StatusCode::NOT_FOUND.into_response(),
        Ok(Ok(None)) => trace_not_found(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn fallback_for(method: Method) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    // The original `/api/{path:path}` catch-all answers POST with the flat 404.
    if method == Method::POST {
        return route_support::route_not_found();
    }
    route_support::method_not_allowed()
}

fn list_body(query: Option<&str>) -> Result<Value, AppError> {
    let settings = TraceSettings::from_env();
    let limit = requested_limit(query, settings.list_limit);
    let (status, traces) = list_traces(&settings, limit);
    Ok(json!({"ok": true, "tracing": status, "traces": traces}))
}

fn trace_body(trace_id: &str) -> Result<Option<Value>, AppError> {
    let settings = TraceSettings::from_env();
    let trace = get_trace(&settings, trace_id).map(|trace| redact_trace(&trace));
    Ok(trace)
}

fn export_body(trace_id: &str) -> Result<Option<Value>, AppError> {
    let settings = TraceSettings::from_env();
    let Some(trace) = get_trace(&settings, trace_id) else {
        return Ok(None);
    };
    let mut redacted = redact_trace(&trace);
    let export = json!({
        "schemaVersion": TRACE_SCHEMA_VERSION,
        "exportedAt": now_iso_utc(),
        "redaction": {
            "applied": true,
            "stringLimit": MAX_EXPORT_STRING_CHARS,
            "privateContentLimit": MAX_PRIVATE_CONTENT_CHARS,
        },
    });
    if let Value::Object(fields) = &mut redacted {
        fields.insert("_export".to_string(), export);
    }
    Ok(Some(redacted))
}

fn trace_not_found() -> Response {
    route_support::app_error_response(AppError {
        message: "Trace not found".to_string(),
        code: codes::NOT_FOUND,
        status: 404,
    })
}

/// `request.query_params.get("limit", "50")` after `int(...)`; a missing or
/// malformed value is the original 50, while the store clamps to 1..=1000 and
/// treats an explicit 0 like the default list limit.
fn requested_limit(query: Option<&str>, list_limit: i64) -> i64 {
    let mut raw: Option<&str> = None;
    if let Some(query) = query {
        for pair in query.split('&') {
            let mut parts = pair.splitn(2, '=');
            let key = parts.next().unwrap_or_default();
            if key == "limit" {
                raw = Some(parts.next().unwrap_or_default());
            }
        }
    }
    let parsed = raw.and_then(python_int).unwrap_or(50);
    let value = if parsed == 0 { list_limit } else { parsed };
    value.clamp(1, 1_000)
}

/// Python's `int(value)` for a decimal string (surrounding whitespace and
/// underscores are accepted; `+`/`-` signs are accepted).
fn python_int(raw: &str) -> Option<i64> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let cleaned: String = trimmed
        .chars()
        .filter(|character| *character != '_')
        .collect();
    cleaned.parse::<i64>().ok()
}

fn open_database(settings: &TraceSettings) -> Option<Connection> {
    if !settings.enabled || !settings.database().is_file() {
        return None;
    }
    Connection::open_with_flags(
        settings.database(),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()
}

fn tracing_status(settings: &TraceSettings) -> Value {
    if !settings.enabled {
        return json!({
            "enabled": false,
            "databasePath": settings.database_path(),
            "traceCount": 0,
            "spanCount": 0,
            "lastError": "",
        });
    }
    let (trace_count, span_count) = match open_database(settings) {
        Some(connection) => (
            count_rows(&connection, "trace_runs"),
            count_rows(&connection, "trace_spans"),
        ),
        None => (0, 0),
    };
    json!({
        "enabled": true,
        "databasePath": settings.database_path(),
        "traceCount": trace_count,
        "spanCount": span_count,
        "lastError": "",
    })
}

fn count_rows(connection: &Connection, table: &str) -> i64 {
    connection
        .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap_or(0)
}

fn list_traces(settings: &TraceSettings, limit: i64) -> (Value, Vec<Value>) {
    let status = tracing_status(settings);
    if !settings.enabled {
        return (status, Vec::new());
    }
    let Some(connection) = open_database(settings) else {
        return (status, Vec::new());
    };
    let mut statement = match connection.prepare(
        "SELECT r.trace_id, r.kind, r.title, r.status, r.started_at, r.completed_at, \
         r.duration_ms, r.metadata, r.error, COUNT(s.span_id) AS span_count \
         FROM trace_runs r LEFT JOIN trace_spans s ON s.trace_id = r.trace_id \
         GROUP BY r.trace_id ORDER BY r.started_epoch DESC LIMIT ?1",
    ) {
        Ok(statement) => statement,
        Err(_) => return (status, Vec::new()),
    };
    let rows = statement.query_map([limit], |row| {
        Ok(public_run(
            &row.get::<_, String>(0)?,
            &row.get::<_, String>(1)?,
            &row.get::<_, String>(2)?,
            &row.get::<_, String>(3)?,
            &row.get::<_, String>(4)?,
            &row.get::<_, String>(5)?,
            row.get::<_, i64>(6)?,
            &row.get::<_, String>(7)?,
            &row.get::<_, String>(8)?,
            row.get::<_, i64>(9)?,
        ))
    });
    let traces = match rows {
        Ok(rows) => rows
            .filter_map(Result::ok)
            .map(|trace| redact_trace(&trace))
            .collect(),
        Err(_) => Vec::new(),
    };
    (status, traces)
}

fn get_trace(settings: &TraceSettings, trace_id: &str) -> Option<Value> {
    if !settings.enabled || trace_id.is_empty() {
        return None;
    }
    let connection = open_database(settings)?;
    let run = connection
        .query_row(
            "SELECT trace_id, kind, title, status, started_at, started_epoch, completed_at, \
             duration_ms, metadata, error FROM trace_runs WHERE trace_id = ?1",
            [trace_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, f64>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, i64>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, String>(9)?,
                ))
            },
        )
        .ok()?;
    let started_epoch = run.5;
    let mut spans = Vec::new();
    if let Ok(mut statement) = connection.prepare(
        "SELECT span_id, trace_id, parent_span_id, name, kind, status, started_at, \
         started_epoch, completed_at, duration_ms, input_json, output_json, usage_json, \
         diagnostics_json, cache_hit_rate, total_tokens, error FROM trace_spans \
         WHERE trace_id = ?1 ORDER BY started_epoch ASC",
    ) {
        if let Ok(rows) = statement.query_map([trace_id], |row| {
            Ok(public_span(
                &row.get::<_, String>(0)?,
                &row.get::<_, String>(1)?,
                &row.get::<_, String>(2)?,
                &row.get::<_, String>(3)?,
                &row.get::<_, String>(4)?,
                &row.get::<_, String>(5)?,
                &row.get::<_, String>(6)?,
                row.get::<_, f64>(7)?,
                &row.get::<_, String>(8)?,
                row.get::<_, i64>(9)?,
                &row.get::<_, String>(10)?,
                &row.get::<_, String>(11)?,
                &row.get::<_, String>(12)?,
                &row.get::<_, String>(13)?,
                row.get::<_, f64>(14)?,
                row.get::<_, i64>(15)?,
                &row.get::<_, String>(16)?,
                started_epoch,
            ))
        }) {
            spans.extend(rows.filter_map(Result::ok));
        }
    }
    let mut trace = public_run(
        &run.0, &run.1, &run.2, &run.3, &run.4, &run.6, run.7, &run.8, &run.9, 0,
    );
    let summary = summarize_spans(&spans);
    if let Value::Object(fields) = &mut trace {
        fields.insert("spans".to_string(), Value::Array(spans));
        fields.insert("summary".to_string(), summary);
    }
    Some(trace)
}

#[allow(clippy::too_many_arguments)]
fn public_run(
    trace_id: &str,
    kind: &str,
    title: &str,
    status: &str,
    started_at: &str,
    completed_at: &str,
    duration_ms: i64,
    metadata: &str,
    error: &str,
    span_count: i64,
) -> Value {
    json!({
        "traceId": trace_id,
        "kind": kind,
        "title": title,
        "status": status,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": duration_ms,
        "metadata": decode_json(metadata),
        "error": error,
        "spanCount": span_count,
    })
}

#[allow(clippy::too_many_arguments)]
fn public_span(
    span_id: &str,
    trace_id: &str,
    parent_span_id: &str,
    name: &str,
    kind: &str,
    status: &str,
    started_at: &str,
    started_epoch: f64,
    completed_at: &str,
    duration_ms: i64,
    input_json: &str,
    output_json: &str,
    usage_json: &str,
    diagnostics_json: &str,
    cache_hit_rate: f64,
    total_tokens: i64,
    error: &str,
    run_started_epoch: f64,
) -> Value {
    let usage = decode_json(usage_json);
    let usage_object = usage.as_object().cloned().unwrap_or_default();
    let tokens = if total_tokens != 0 {
        total_tokens
    } else {
        usage_int(&usage_object, &["total_tokens", "totalTokens"])
    };
    let offset = ((started_epoch - run_started_epoch) * 1000.0) as i64;
    json!({
        "spanId": span_id,
        "traceId": trace_id,
        "parentSpanId": parent_span_id,
        "name": name,
        "kind": kind,
        "status": status,
        "startedAt": started_at,
        "completedAt": completed_at,
        "offsetMs": offset.max(0),
        "durationMs": duration_ms,
        "input": decode_json(input_json),
        "output": decode_json(output_json),
        "usage": usage,
        "diagnostics": decode_json(diagnostics_json),
        "cacheHitRate": cache_hit_rate,
        "totalTokens": tokens,
        "error": error,
    })
}

fn usage_int(usage: &Map<String, Value>, names: &[&str]) -> i64 {
    for name in names {
        let value = usage.get(*name);
        let parsed = match value {
            Some(Value::Number(number)) => number
                .as_i64()
                .or_else(|| number.as_f64().map(|value| value as i64)),
            Some(Value::String(text)) => python_int(text),
            _ => None,
        };
        if let Some(parsed) = parsed {
            return parsed.max(0);
        }
    }
    0
}

fn summarize_spans(spans: &[Value]) -> Value {
    let total_duration: i64 = spans
        .iter()
        .map(|span| span["durationMs"].as_i64().unwrap_or(0))
        .sum();
    let total_tokens: i64 = spans
        .iter()
        .map(|span| span["totalTokens"].as_i64().unwrap_or(0))
        .sum();
    let mut slowest: Option<&Value> = None;
    let mut slowest_duration = i64::MIN;
    for span in spans {
        let duration = span["durationMs"].as_i64().unwrap_or(0);
        if slowest.is_none() || duration > slowest_duration {
            slowest = Some(span);
            slowest_duration = duration;
        }
    }
    json!({
        "spanCount": spans.len(),
        "totalSpanDurationMs": total_duration,
        "totalTokens": total_tokens,
        "slowestSpan": slowest.and_then(|span| span["name"].as_str()).unwrap_or(""),
        "slowestDurationMs": slowest.and_then(|span| span["durationMs"].as_i64()).unwrap_or(0),
    })
}

fn decode_json(text: &str) -> Value {
    if text.is_empty() {
        return json!({});
    }
    serde_json::from_str(text).unwrap_or_else(|_| json!({}))
}

// --- redaction (port of observability/export.py) ---------------------------------

static BEARER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}").expect("static regex"));
static SECRET_TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(sk-[a-zA-Z0-9][a-zA-Z0-9_-]{8,})\b").expect("static regex"));
static QUERY_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?i)([?&](?:token|api_key|apikey|auth_token|access_token|refresh_token)=)([^&\s"'<>]+)"#,
    )
    .expect("static regex")
});
static HEADER_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?i)\b(api[-_ ]?key|authorization|auth[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token)\s*[:=]\s*([^,\s"'<>]{8,})"#,
    )
    .expect("static regex")
});

const TOKEN_COUNT_KEYS: [&str; 7] = [
    "totaltokens",
    "prompttokens",
    "completiontokens",
    "promptcachehittokens",
    "promptcachemisstokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
];
const SENSITIVE_EXACT_KEYS: [&str; 25] = [
    "apikey",
    "api_key",
    "authorization",
    "auth",
    "authtoken",
    "auth_token",
    "bearertoken",
    "token",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "cookie",
    "setcookie",
    "set_cookie",
    "password",
    "secret",
    "credential",
    "credentials",
    "privatekey",
    "private_key",
    "deepseekapikey",
    "deepseek_api_key",
    "tavilyapikey",
    "tavily_api_key",
];
const PRIVATE_CONTENT_KEYS: [&str; 15] = [
    "content",
    "text",
    "prompt",
    "messages",
    "input",
    "output",
    "filecontent",
    "file_content",
    "filetext",
    "file_text",
    "rawcontent",
    "raw_content",
    "pagecontent",
    "page_content",
    "document",
];

fn redact_trace(trace: &Value) -> Value {
    redact_value(trace, "")
}

fn redact_value(value: &Value, key: &str) -> Value {
    if is_sensitive_key(key) {
        return Value::String("[redacted]".to_string());
    }
    match value {
        Value::Object(fields) => {
            let mut result = Map::new();
            for (field, item) in fields {
                result.insert(field.clone(), redact_value(item, field));
            }
            Value::Object(result)
        }
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| redact_value(item, key)).collect())
        }
        Value::String(text) => {
            Value::String(clip_text(&redact_sensitive_text(text), limit_for_key(key)))
        }
        _ => value.clone(),
    }
}

fn normalize_key(key: &str) -> String {
    key.to_ascii_lowercase()
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || *character == '_')
        .collect()
}

fn is_sensitive_key(key: &str) -> bool {
    let normalized = normalize_key(key);
    if normalized.is_empty() || TOKEN_COUNT_KEYS.contains(&normalized.as_str()) {
        return false;
    }
    if SENSITIVE_EXACT_KEYS.contains(&normalized.as_str()) {
        return true;
    }
    if normalized.ends_with("apikey")
        || normalized.ends_with("secret")
        || normalized.ends_with("password")
        || normalized.ends_with("privatekey")
    {
        return true;
    }
    if normalized.contains("token") && !normalized.ends_with("tokens") {
        return true;
    }
    [
        "authorization",
        "cookie",
        "password",
        "secret",
        "credential",
        "privatekey",
    ]
    .iter()
    .any(|fragment| normalized.contains(fragment))
}

fn limit_for_key(key: &str) -> usize {
    let normalized = normalize_key(key);
    if PRIVATE_CONTENT_KEYS.contains(&normalized.as_str())
        || normalized.ends_with("content")
        || normalized.ends_with("text")
        || normalized.ends_with("prompt")
        || normalized.ends_with("document")
    {
        return MAX_PRIVATE_CONTENT_CHARS;
    }
    MAX_EXPORT_STRING_CHARS
}

fn redact_sensitive_text(value: &str) -> String {
    let text = BEARER_RE.replace_all(value, "${1}[redacted]").to_string();
    let text = SECRET_TOKEN_RE.replace_all(&text, "[redacted-secret]");
    let text = QUERY_SECRET_RE.replace_all(&text, "${1}[redacted]");
    HEADER_SECRET_RE
        .replace_all(&text, "${1}=[redacted]")
        .to_string()
}

fn clip_text(value: &str, limit: usize) -> String {
    let characters: Vec<char> = value.chars().collect();
    if characters.len() <= limit {
        return value.to_string();
    }
    let mut clipped: String = characters[..limit].iter().collect();
    clipped.push_str(&format!(
        "...[truncated {} chars]",
        characters.len() - limit
    ));
    clipped
}

fn export_filename(trace_id: &str) -> String {
    let safe: String = trace_id
        .chars()
        .filter(|character| character.is_alphanumeric() || *character == '-' || *character == '_')
        .take(32)
        .collect();
    let safe = if safe.is_empty() {
        "export".to_string()
    } else {
        safe
    };
    format!("trace-{safe}.json")
}

// --- UTC timestamp ---------------------------------------------------------------

fn now_iso_utc() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    iso_from_epoch(now.as_secs() as i64, now.subsec_micros())
}

fn iso_from_epoch(seconds: i64, micros: u32) -> String {
    let days = seconds.div_euclid(86_400);
    let remainder = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = remainder / 3_600;
    let minute = (remainder % 3_600) / 60;
    let second = remainder % 60;
    if micros == 0 {
        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
    } else {
        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}Z")
    }
}

/// Howard Hinnant's `civil_from_days`.
fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (year + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests {
    use super::{
        clip_text, export_filename, is_sensitive_key, redact_sensitive_text, requested_limit,
    };

    #[test]
    fn sensitive_keys_follow_the_original_rules() {
        for key in [
            "apiKey",
            "authorization",
            "auth_token",
            "refresh_token",
            "password",
        ] {
            assert!(is_sensitive_key(key), "{key}");
        }
        for key in ["totalTokens", "promptTokens", "name", "kind", "usage"] {
            assert!(!is_sensitive_key(key), "{key}");
        }
    }

    #[test]
    fn sensitive_text_is_redacted() {
        assert_eq!(
            redact_sensitive_text("Bearer abcdefghijklmnop"),
            "Bearer [redacted]"
        );
        assert_eq!(
            redact_sensitive_text("key sk-abcdefghijk12345"),
            "key [redacted-secret]"
        );
        assert_eq!(
            redact_sensitive_text("?token=abcdefgh"),
            "?token=[redacted]"
        );
        assert_eq!(
            redact_sensitive_text("authorization=abcdefghijk"),
            "authorization=[redacted]"
        );
    }

    #[test]
    fn clip_text_counts_characters() {
        assert_eq!(clip_text("abc", 5), "abc");
        assert_eq!(clip_text("abcdef", 3), "abc...[truncated 3 chars]");
    }

    #[test]
    fn export_filename_matches_the_oracle_rules() {
        assert_eq!(export_filename("abc123"), "trace-abc123.json");
        assert_eq!(export_filename("!!!"), "trace-export.json");
    }

    #[test]
    fn requested_limit_matches_python_parsing() {
        assert_eq!(requested_limit(None, 100), 50);
        assert_eq!(requested_limit(Some("limit=5"), 100), 5);
        assert_eq!(requested_limit(Some("limit=abc"), 100), 50);
        assert_eq!(requested_limit(Some("limit=0"), 100), 100);
        assert_eq!(requested_limit(Some("limit=5000"), 100), 1_000);
        assert_eq!(requested_limit(Some("limit=-3"), 100), 1);
        assert_eq!(requested_limit(Some("limit=2&limit=7"), 100), 7);
    }
}
