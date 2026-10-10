//! `GET /api/scheduler`.
//!
//! `scheduler_status()` plus `dead_letters(limit)`. The admission counters are
//! this process's fresh snapshot: the edge does not take a lease, so they stay
//! at zero the way a newly imported Python scheduler does. `rate_per_second <= 0`
//! reports `float(capacity)` with no clock. A missing `scheduler.sqlite3` is
//! `{count: 0, byReason: {}}` and is not created. An existing file is opened
//! read-only and is not given a schema.

use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use rusqlite::{Connection, OpenFlags};
use serde_json::{Number, Value, json};

const DLQ_TABLE: &str = "scheduler_dead_letters";

pub fn router() -> Router {
    Router::new().route("/api/scheduler", any(dispatch))
}

async fn dispatch(
    method: Method,
    OriginalUri(uri): OriginalUri,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return scheduler_response(method, limit_raw(&uri)).await;
    }
    method_not_allowed()
}

async fn scheduler_response(method: Method, limit: Option<String>) -> Response {
    let joined = tokio::task::spawn_blocking(move || build_body(limit)).await;
    match joined {
        Ok(_body) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => Json(body).into_response(),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => error_response(server_error()),
    }
}

fn build_body(limit: Option<String>) -> Value {
    let settings = load_settings();
    let database = scheduler_db();
    let mut scheduler = snapshot(&settings);
    if let Value::Object(fields) = &mut scheduler {
        fields.insert(
            "deadLetterQueue".to_string(),
            dlq_status(&settings, &database),
        );
    }
    json!({
        "ok": true,
        "scheduler": scheduler,
        "deadLetters": dead_letters(&database, cap_limit(route_limit(limit.as_deref()))),
    })
}

struct Settings {
    enabled: bool,
    max_concurrency: i64,
    max_queue_depth: i64,
    rate: f64,
    burst_capacity: i64,
    dlq_enabled: bool,
}

fn load_settings() -> Settings {
    let max_concurrency = env_int_clamped("SCHEDULER_MAX_CONCURRENCY", 16, 1, 1024);
    let max_queue_depth = env_int_clamped("SCHEDULER_MAX_QUEUE_DEPTH", 256, 1, 100_000);
    let rate = env_float_clamped("SCHEDULER_RATE_PER_SECOND", 0.0, 0.0, 100_000.0).max(0.0);
    // `int(burst) or max_concurrency`: zero is the only falsy integer, and the
    // env parser already clamps a negative burst to zero.
    let burst_raw = env_int_min("SCHEDULER_RATE_BURST", 0, 0);
    let burst = if burst_raw == 0 {
        max_concurrency
    } else {
        burst_raw
    };
    Settings {
        enabled: env_bool("SCHEDULER_ENABLED", true),
        max_concurrency,
        max_queue_depth,
        rate,
        burst_capacity: burst.max(1),
        dlq_enabled: env_bool("SCHEDULER_DLQ_ENABLED", true),
    }
}

fn snapshot(settings: &Settings) -> Value {
    json!({
        "enabled": settings.enabled,
        "inFlight": 0,
        "waiting": 0,
        "maxConcurrency": settings.max_concurrency,
        "maxQueueDepth": settings.max_queue_depth,
        "ratePerSecond": settings.rate,
        "rateBurst": settings.burst_capacity,
        "availableTokens": available_tokens(settings),
        "admitted": 0,
        "shed": 0,
        "timedOut": 0,
        "cancelled": 0,
        "rateLimitedWaits": 0,
        "peakInFlight": 0,
        "byPriority": {},
    })
}

/// `TokenBucket.available` for a bucket that has never called `try_take`.
/// Tokens start at capacity, so a positive rate still reports that capacity.
fn available_tokens(settings: &Settings) -> f64 {
    if settings.rate <= 0.0 {
        return settings.burst_capacity as f64;
    }
    python_round_ndigits(settings.burst_capacity as f64, 3)
}

fn dlq_status(settings: &Settings, database: &Path) -> Value {
    let mut payload = json!({
        "enabled": settings.dlq_enabled,
        "dbPath": python_path_str(database),
        "count": 0,
        "byReason": {},
    });
    if !database.exists() {
        return payload;
    }
    let conn = match open_readonly(database) {
        Ok(conn) => conn,
        Err(message) => {
            insert_error(&mut payload, message);
            return payload;
        }
    };
    let counted = conn.query_row(&format!("SELECT COUNT(*) FROM {DLQ_TABLE}"), [], |row| {
        row.get::<_, i64>(0)
    });
    let total = match counted {
        Ok(total) => total,
        Err(error) => {
            insert_error(&mut payload, python_sqlite_message(&error.to_string()));
            return payload;
        }
    };
    let grouped = match grouped_reasons(&conn) {
        Ok(grouped) => grouped,
        Err(error) => {
            insert_error(&mut payload, python_sqlite_message(&error.to_string()));
            return payload;
        }
    };
    if let Value::Object(fields) = &mut payload {
        fields.insert("count".to_string(), json!(total));
        fields.insert("byReason".to_string(), Value::Object(grouped));
        fields.insert("recent".to_string(), json!(dead_letters_on(&conn, 10)));
    }
    payload
}

fn grouped_reasons(conn: &Connection) -> rusqlite::Result<serde_json::Map<String, Value>> {
    let mut statement = conn.prepare(&format!(
        "SELECT reason, COUNT(*) FROM {DLQ_TABLE} GROUP BY reason"
    ))?;
    let mut rows = statement.query([])?;
    let mut grouped = serde_json::Map::new();
    while let Some(row) = rows.next()? {
        let count: i64 = row.get(1)?;
        grouped.insert(sqlite_display(row, 0)?, json!(count));
    }
    Ok(grouped)
}

fn dead_letters(database: &Path, limit: usize) -> Vec<Value> {
    if !database.exists() {
        return Vec::new();
    }
    let Ok(conn) = open_readonly(database) else {
        return Vec::new();
    };
    dead_letters_on(&conn, limit)
}

fn dead_letters_on(conn: &Connection, limit: usize) -> Vec<Value> {
    let mut statement = match conn.prepare(&format!(
        "SELECT id, kind, key, reason, attempts, priority, created_at FROM {DLQ_TABLE} \
         ORDER BY created_at DESC LIMIT ?1"
    )) {
        Ok(statement) => statement,
        Err(_) => return Vec::new(),
    };
    let mut rows = match statement.query([limit as i64]) {
        Ok(rows) => rows,
        Err(_) => return Vec::new(),
    };
    let mut letters = Vec::new();
    loop {
        let row = match rows.next() {
            Ok(Some(row)) => row,
            Ok(None) => break,
            Err(_) => return Vec::new(),
        };
        let Ok(letter) = letter_from_row(row) else {
            return Vec::new();
        };
        letters.push(letter);
    }
    letters
}

fn letter_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": sqlite_display(row, 0)?,
        "kind": sqlite_display(row, 1)?,
        "key": sqlite_display(row, 2)?,
        "reason": sqlite_display(row, 3)?,
        "attempts": sqlite_int_or_zero(row, 4)?,
        "priority": sqlite_int_or_zero(row, 5)?,
        "createdAt": sqlite_float_or_zero(row, 6)?,
    }))
}

fn open_readonly(database: &Path) -> Result<Connection, String> {
    // SQLite's bundled Unix VFS reports IOERR for directories, while the
    // Python sqlite3 oracle reports CANTOPEN. Preserve the public error and
    // avoid asking either VFS to open a directory as a database.
    if database.is_dir() {
        return Err("unable to open database file".to_string());
    }
    let conn = Connection::open_with_flags(
        database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| python_sqlite_message(&error.to_string()))?;
    conn.busy_timeout(Duration::from_secs(10))
        .map_err(|error| python_sqlite_message(&error.to_string()))?;
    Ok(conn)
}

/// Python's `str(sqlite3.Error)` omits the path rusqlite appends when the
/// path is a directory.
fn python_sqlite_message(message: &str) -> String {
    if message == "unable to open database file"
        || message.starts_with("unable to open database file:")
    {
        return "unable to open database file".to_string();
    }
    message.to_string()
}

fn insert_error(payload: &mut Value, message: String) {
    if let Value::Object(fields) = payload {
        fields.insert("error".to_string(), Value::String(message));
    }
}

/// `str(query.get("limit", "50"))` then `int`, with `ValueError` → 50.
fn limit_raw(uri: &Uri) -> Option<String> {
    first_query(uri.query().unwrap_or(""), "limit")
}

fn route_limit(raw: Option<&str>) -> i64 {
    parse_python_int(raw.unwrap_or("50")).unwrap_or(50)
}

/// `max(1, min(int(limit or 50), 1000))`. Zero is falsy, so it becomes 50.
fn cap_limit(limit: i64) -> usize {
    let chosen = if limit == 0 { 50 } else { limit };
    chosen.clamp(1, 1000) as usize
}

fn first_query(query: &str, wanted: &str) -> Option<String> {
    if query.is_empty() {
        return None;
    }
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if percent_decode_query(key) == wanted {
            return Some(percent_decode_query(value));
        }
    }
    None
}

fn percent_decode_query(raw: &str) -> String {
    let spaced = raw.replace('+', " ");
    percent_encoding::percent_decode_str(&spaced)
        .decode_utf8_lossy()
        .into_owned()
}

fn scheduler_db() -> PathBuf {
    runtime_root().join(".scheduler").join("scheduler.sqlite3")
}

/// `Path(DEEPSEEK_INFRA_ROOT or DEEPSEEK_MOBILE_ROOT).expanduser().resolve()`.
fn runtime_root() -> PathBuf {
    let infra = std::env::var("DEEPSEEK_INFRA_ROOT").unwrap_or_default();
    let mobile = std::env::var("DEEPSEEK_MOBILE_ROOT").unwrap_or_default();
    let selected = if !infra.trim().is_empty() {
        infra
    } else if !mobile.trim().is_empty() {
        mobile
    } else {
        return python_resolve(Path::new("."));
    };
    python_resolve(Path::new(selected.trim()))
}

pub(crate) fn python_resolve(path: &Path) -> PathBuf {
    let expanded = expanduser(path);
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(&expanded))
            .unwrap_or(expanded)
    };
    // Resolve the longest existing prefix, then append the missing tail.
    // `Path.resolve(strict=False)` does this, and `str(Path)` keeps backslashes.
    let mut resolved = PathBuf::new();
    let mut pending = Vec::new();
    for component in absolute.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => {
                if pending.is_empty() {
                    resolved.push(component);
                }
            }
            Component::CurDir => {}
            Component::ParentDir => {
                if pending.pop().is_none() {
                    resolved.pop();
                }
            }
            Component::Normal(part) => {
                if pending.is_empty() {
                    let candidate = resolved.join(part);
                    match std::fs::canonicalize(&candidate) {
                        Ok(canonical) => resolved = strip_verbatim(canonical),
                        Err(_) => pending.push(part.to_os_string()),
                    }
                } else {
                    pending.push(part.to_os_string());
                }
            }
        }
    }
    for part in pending {
        resolved.push(part);
    }
    resolved
}

fn expanduser(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    let trimmed = text.trim();
    if trimmed == "~" {
        return home_dir().unwrap_or_else(|| PathBuf::from(trimmed));
    }
    if let Some(rest) = trimmed
        .strip_prefix("~/")
        .or_else(|| trimmed.strip_prefix("~\\"))
    {
        return home_dir()
            .map(|home| home.join(rest))
            .unwrap_or_else(|| PathBuf::from(trimmed));
    }
    PathBuf::from(trimmed)
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn strip_verbatim(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        return PathBuf::from(rest.to_string());
    }
    path
}

pub(crate) fn python_path_str(path: &Path) -> String {
    path.display().to_string()
}

pub(crate) fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => {
            matches!(
                raw.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        }
        _ => default,
    }
}

fn env_int(name: &str, default: i64) -> i64 {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => parse_python_int(&raw).unwrap_or(default),
        _ => default,
    }
}

pub(crate) fn env_int_clamped(name: &str, default: i64, minimum: i64, maximum: i64) -> i64 {
    env_int(name, default).clamp(minimum, maximum)
}

pub(crate) fn env_int_min(name: &str, default: i64, minimum: i64) -> i64 {
    env_int(name, default).max(minimum)
}

pub(crate) fn env_float_clamped(name: &str, default: f64, minimum: f64, maximum: f64) -> f64 {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => python_float(&raw)
            .unwrap_or(default)
            .clamp(minimum, maximum),
        _ => default,
    }
}

/// Python `int()` for a query or env string. Overflow clamps to `i64` extremes;
/// the scheduler only uses the value inside `1..=1000` or a settings clamp.
fn parse_python_int(raw: &str) -> Option<i64> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    let (negative, digits) = if let Some(rest) = text.strip_prefix('+') {
        (false, rest)
    } else if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else {
        (false, text)
    };
    if !underscores_between_digits(digits) {
        return None;
    }
    let clean: String = digits
        .chars()
        .filter(|character| *character != '_')
        .collect();
    let magnitude = clean.trim_start_matches('0');
    if magnitude.is_empty() {
        return Some(0);
    }
    if negative && magnitude == "9223372036854775808" {
        return Some(i64::MIN);
    }
    if magnitude.len() > 19 || (magnitude.len() == 19 && magnitude > "9223372036854775807") {
        return Some(if negative { i64::MIN } else { i64::MAX });
    }
    let value: i64 = magnitude.parse().ok()?;
    if negative {
        value.checked_neg()
    } else {
        Some(value)
    }
}

fn underscores_between_digits(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_' {
            let before = index
                .checked_sub(1)
                .and_then(|prior| bytes.get(prior))
                .copied();
            let after = bytes.get(index + 1).copied();
            if !before.is_some_and(|item| item.is_ascii_digit())
                || !after.is_some_and(|item| item.is_ascii_digit())
            {
                return false;
            }
        } else if !byte.is_ascii_digit() {
            return false;
        }
    }
    true
}

fn python_float(raw: &str) -> Option<f64> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    match text.to_ascii_lowercase().as_str() {
        "inf" | "+inf" | "infinity" | "+infinity" => return Some(f64::INFINITY),
        "-inf" | "-infinity" => return Some(f64::NEG_INFINITY),
        "nan" | "+nan" | "-nan" => return Some(f64::NAN),
        _ => {}
    }
    let bytes = text.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte != b'_' {
            continue;
        }
        let before = index
            .checked_sub(1)
            .and_then(|prior| bytes.get(prior))
            .copied();
        let after = bytes.get(index + 1).copied();
        if !before.is_some_and(|item| item.is_ascii_digit())
            || !after.is_some_and(|item| item.is_ascii_digit())
        {
            return None;
        }
    }
    let cleaned: String = text.chars().filter(|character| *character != '_').collect();
    cleaned.parse().ok()
}

/// Python 3 `round(value, digits)` for a non-negative capacity. Half-even.
fn python_round_ndigits(value: f64, digits: i32) -> f64 {
    let factor = 10_f64.powi(digits);
    let scaled = value * factor;
    let floor = scaled.floor();
    let fraction = scaled - floor;
    let rounded = if (fraction - 0.5).abs() < f64::EPSILON {
        if floor as i64 % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        scaled.round()
    };
    rounded / factor
}

fn sqlite_display(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<String> {
    Ok(match row.get_ref(index)? {
        rusqlite::types::ValueRef::Null => "None".to_string(),
        rusqlite::types::ValueRef::Integer(value) => value.to_string(),
        rusqlite::types::ValueRef::Real(value) => {
            let mut rendered = Number::from_f64(value)
                .map(|number| number.to_string())
                .unwrap_or_else(|| value.to_string());
            if !rendered.contains('.') && !rendered.contains('e') && !rendered.contains('E') {
                rendered.push_str(".0");
            }
            rendered
        }
        rusqlite::types::ValueRef::Text(bytes) => String::from_utf8_lossy(bytes).into_owned(),
        rusqlite::types::ValueRef::Blob(bytes) => format!("b'{}'", String::from_utf8_lossy(bytes)),
    })
}

/// `int(value or 0)`. Zero and NULL become 0. A real truncates toward zero.
fn sqlite_int_or_zero(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<i64> {
    Ok(match row.get_ref(index)? {
        rusqlite::types::ValueRef::Null => 0,
        rusqlite::types::ValueRef::Integer(value) => value,
        rusqlite::types::ValueRef::Real(value) => value as i64,
        rusqlite::types::ValueRef::Text(bytes) => {
            parse_python_int(&String::from_utf8_lossy(bytes)).unwrap_or(0)
        }
        rusqlite::types::ValueRef::Blob(_) => 0,
    })
}

/// `float(value or 0.0)`. Zero and NULL become 0.0.
fn sqlite_float_or_zero(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<f64> {
    Ok(match row.get_ref(index)? {
        rusqlite::types::ValueRef::Null => 0.0,
        rusqlite::types::ValueRef::Integer(value) => value as f64,
        rusqlite::types::ValueRef::Real(value) => value,
        rusqlite::types::ValueRef::Text(bytes) => {
            python_float(&String::from_utf8_lossy(bytes)).unwrap_or(0.0)
        }
        rusqlite::types::ValueRef::Blob(_) => 0.0,
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
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
    response
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
