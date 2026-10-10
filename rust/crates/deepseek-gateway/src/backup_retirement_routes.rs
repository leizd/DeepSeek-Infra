//! `POST/GET /api/workspace/backup-retirements` and `GET .../{job_id}`.
//!
//! These three methods are the whole public surface of
//! `deepseek_infra/web/routes/backup_governance.py` for copy retirement. The row
//! they create is only the `requested` phase. Physical GC, receipt/commit marker
//! deletion, and `execute_copy_retirement_job` stay on the Python worker until
//! that executor is the native one. This module does not pretend a job has been
//! reclaimed.
//!
//! The table is `.backup-retirements/retirements.sqlite3`, the same file
//! `backup_retirement.py` owns while Python is still authoritative. A write here
//! is refused with `NATIVE_BACKUP_RETIREMENT_WRITE_NOT_OWNED` unless
//! `DEEPSEEK_RUNTIME_MODE=python_disabled`, and a refused write does not create
//! the directory. The domain is intentionally **not** in
//! [`crate::DECLARED_NATIVE_DATA_DOMAINS`]: that list is for stores whose Python
//! writer is already mechanically denied, and the retirement GC worker is not.
//! Reads of a missing database return an empty list or 404 and do not mkdir.
//! Creating the file on GET would make a reader into a second writer.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Bytes;
use axum::extract::rejection::BytesRejection;
use axum::extract::{Path as AxumPath, Query};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{Json, Router, routing::get};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::value_str;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::Deserialize;
use serde_json::{Value, json};

const BODY_LIMIT: usize = 16_000;
const LIST_LIMIT: i64 = 200;
const WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_RETIREMENT_WRITE_NOT_OWNED";

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backup-retirements",
            get(list_jobs).post(create_job),
        )
        .route("/api/workspace/backup-retirements/:job_id", get(get_job))
}

#[derive(Debug, Deserialize, Default)]
struct ListQuery {
    #[serde(rename = "policyId")]
    policy_id: Option<String>,
    #[serde(rename = "targetId")]
    target_id: Option<String>,
    phase: Option<String>,
}

struct StoredJob {
    job_id: String,
    policy_id: String,
    backup_id: String,
    target_id: String,
    phase: String,
    created_at: String,
    updated_at: String,
    error: Option<String>,
    reason: Option<String>,
    bytes_reclaimed: Option<i64>,
    sim_metadata: Option<String>,
}

async fn create_job(headers: HeaderMap, body: Result<Bytes, BytesRejection>) -> Response {
    let payload = match read_object(&headers, body) {
        Ok(value) => value,
        Err(error) => return error_response(error),
    };
    let policy_id = required_id(&payload, "policyId");
    let backup_id = required_id(&payload, "backupId");
    let target_id = required_id(&payload, "targetId");
    if policy_id.is_empty() || backup_id.is_empty() || target_id.is_empty() {
        return error_response(AppError::invalid_payload(
            "policyId, backupId, and targetId are required",
        ));
    }
    let reason = reason_text(&payload);
    blocking(move || insert_job(policy_id, backup_id, target_id, reason)).await
}

async fn get_job(AxumPath(job_id): AxumPath<String>) -> Response {
    blocking(move || match load_one(&job_id)? {
        Some(job) => Ok(job),
        None => Err(AppError::not_found("Retirement job not found")),
    })
    .await
}

async fn list_jobs(Query(query): Query<ListQuery>) -> Response {
    blocking(move || {
        let jobs = load_list(
            present_filter(query.policy_id),
            present_filter(query.target_id),
            present_filter(query.phase),
        )?;
        Ok(json!({"jobs": jobs}))
    })
    .await
}

async fn blocking(work: impl FnOnce() -> Result<Value, AppError> + Send + 'static) -> Response {
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(server_error()),
    }
}

fn insert_job(
    policy_id: String,
    backup_id: String,
    target_id: String,
    reason: String,
) -> Result<Value, AppError> {
    // Checked before any filesystem touch. A 409 must not create the directory
    // the Python worker still owns.
    if !crate::python_is_de_authorised() {
        return Err(write_not_owned());
    }
    let path = db_path();
    let mut conn = connect_write(&path)?;
    let job_id = format!("retire_{}", SystemEntropy.new_id()?);
    let now = utc_iso_from_unix(unix_seconds());
    let tx = conn.transaction().map_err(|_| server_error())?;
    tx.execute(
        "INSERT INTO copy_retirement_jobs(
            job_id, policy_id, backup_id, target_id, phase, created_at,
            updated_at, error, reason, bytes_reclaimed, sim_metadata
        ) VALUES (?1, ?2, ?3, ?4, 'requested', ?5, ?6, NULL, ?7, 0, '{}')",
        (
            &job_id, &policy_id, &backup_id, &target_id, &now, &now, &reason,
        ),
    )
    .map_err(|_| server_error())?;
    let job = read_job(&tx, &job_id, true)?.ok_or_else(server_error)?;
    tx.commit().map_err(|_| server_error())?;
    Ok(job)
}

fn load_one(job_id: &str) -> Result<Option<Value>, AppError> {
    let path = db_path();
    let Some(conn) = open_for_read(&path)? else {
        return Ok(None);
    };
    let has_reason = column_exists(&conn, "reason")?;
    read_job(&conn, job_id, has_reason)
}

fn load_list(
    policy_id: Option<String>,
    target_id: Option<String>,
    phase: Option<String>,
) -> Result<Vec<Value>, AppError> {
    let path = db_path();
    let Some(conn) = open_for_read(&path)? else {
        return Ok(Vec::new());
    };
    if !table_exists(&conn)? {
        return Err(server_error());
    }
    let has_reason = column_exists(&conn, "reason")?;
    let reason_sql = if has_reason {
        "reason"
    } else {
        "NULL AS reason"
    };
    // Same predicate shape as `list_copy_retirement_jobs`: a missing or empty
    // filter is absent from the SQL, not compared against an empty string.
    let mut sql = format!(
        "SELECT job_id, policy_id, backup_id, target_id, phase, created_at, updated_at, \
         error, {reason_sql}, bytes_reclaimed, sim_metadata \
         FROM copy_retirement_jobs WHERE 1=1"
    );
    let mut values: Vec<&dyn rusqlite::ToSql> = Vec::new();
    if let Some(value) = policy_id.as_ref() {
        sql.push_str(" AND policy_id = ?");
        values.push(value);
    }
    if let Some(value) = target_id.as_ref() {
        sql.push_str(" AND target_id = ?");
        values.push(value);
    }
    if let Some(value) = phase.as_ref() {
        sql.push_str(" AND phase = ?");
        values.push(value);
    }
    sql.push_str(" ORDER BY created_at DESC LIMIT ?");
    let limit = LIST_LIMIT;
    values.push(&limit);
    let mut statement = conn.prepare(&sql).map_err(|_| server_error())?;
    let rows = statement
        .query_map(rusqlite::params_from_iter(values), read_stored)
        .map_err(|_| server_error())?;
    let mut jobs = Vec::new();
    for row in rows {
        jobs.push(to_json(row.map_err(|_| server_error())?)?);
    }
    Ok(jobs)
}

/// Missing file: no connection and no directory. Existing file while Python is
/// still authoritative: read-only, so journal mode and the `reason` migration
/// stay Python's. Once Python is de-authorised this process is the writer, and
/// opening the existing file applies the same schema migration `_connect` does.
fn open_for_read(path: &Path) -> Result<Option<Connection>, AppError> {
    if !path.is_file() {
        return Ok(None);
    }
    if crate::python_is_de_authorised() {
        return Ok(Some(connect_write(path)?));
    }
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| server_error())?;
    conn.busy_timeout(std::time::Duration::from_millis(5000))
        .map_err(|_| server_error())?;
    Ok(Some(conn))
}

fn connect_write(path: &Path) -> Result<Connection, AppError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|_| server_error())?;
    }
    let conn = Connection::open(path).map_err(|_| server_error())?;
    conn.busy_timeout(std::time::Duration::from_millis(5000))
        .map_err(|_| server_error())?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|_| server_error())?;
    ensure_schema(&conn)?;
    Ok(conn)
}

fn ensure_schema(conn: &Connection) -> Result<(), AppError> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS copy_retirement_jobs (
            job_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            reason TEXT,
            bytes_reclaimed INTEGER DEFAULT 0,
            sim_metadata TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_retirement_phase ON copy_retirement_jobs(phase);
        CREATE INDEX IF NOT EXISTS idx_retirement_target ON copy_retirement_jobs(target_id, policy_id);",
    )
    .map_err(|_| server_error())?;
    if !column_exists(conn, "reason")? {
        conn.execute(
            "ALTER TABLE copy_retirement_jobs ADD COLUMN reason TEXT",
            [],
        )
        .map_err(|_| server_error())?;
    }
    Ok(())
}

fn read_job(conn: &Connection, job_id: &str, has_reason: bool) -> Result<Option<Value>, AppError> {
    if !table_exists(conn)? {
        return Err(server_error());
    }
    let reason_sql = if has_reason {
        "reason"
    } else {
        "NULL AS reason"
    };
    let sql = format!(
        "SELECT job_id, policy_id, backup_id, target_id, phase, created_at, updated_at, \
         error, {reason_sql}, bytes_reclaimed, sim_metadata \
         FROM copy_retirement_jobs WHERE job_id = ?1"
    );
    let stored = conn
        .query_row(&sql, [job_id], read_stored)
        .optional()
        .map_err(|_| server_error())?;
    stored.map(to_json).transpose()
}

fn read_stored(row: &rusqlite::Row<'_>) -> rusqlite::Result<StoredJob> {
    Ok(StoredJob {
        job_id: row.get(0)?,
        policy_id: row.get(1)?,
        backup_id: row.get(2)?,
        target_id: row.get(3)?,
        phase: row.get(4)?,
        created_at: row.get(5)?,
        updated_at: row.get(6)?,
        error: row.get(7)?,
        reason: row.get(8)?,
        bytes_reclaimed: row.get(9)?,
        sim_metadata: row.get(10)?,
    })
}

fn to_json(job: StoredJob) -> Result<Value, AppError> {
    Ok(json!({
        "jobId": job.job_id,
        "policyId": job.policy_id,
        "backupId": job.backup_id,
        "targetId": job.target_id,
        "phase": job.phase,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "error": job.error,
        "reason": job.reason,
        "bytesReclaimed": job.bytes_reclaimed,
        "simMetadata": sim_metadata_value(job.sim_metadata)?,
    }))
}

/// `json.loads(sim_metadata or "{}")`. Invalid text is the oracle's unhandled
/// `JSONDecodeError`, which the edge turns into `500 Server error`.
fn sim_metadata_value(raw: Option<String>) -> Result<Value, AppError> {
    let text = raw
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "{}".to_string());
    serde_json::from_str(&text).map_err(|_| server_error())
}

fn table_exists(conn: &Connection) -> Result<bool, AppError> {
    conn.query_row(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'copy_retirement_jobs'",
        [],
        |_| Ok(()),
    )
    .optional()
    .map(|row| row.is_some())
    .map_err(|_| server_error())
}

fn column_exists(conn: &Connection, column: &str) -> Result<bool, AppError> {
    let mut statement = conn
        .prepare("PRAGMA table_info(copy_retirement_jobs)")
        .map_err(|_| server_error())?;
    let names = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| server_error())?;
    for name in names {
        if name.map_err(|_| server_error())? == column {
            return Ok(true);
        }
    }
    Ok(false)
}

fn db_path() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-retirements")
        .join("retirements.sqlite3")
}

/// `str(payload.get(key) or "").strip()`.
fn required_id(payload: &Value, key: &str) -> String {
    payload
        .get(key)
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default()
        .trim()
        .to_string()
}

/// `str(payload.get("reason") or "api-retirement-request")`. Not stripped.
fn reason_text(payload: &Value) -> String {
    payload
        .get("reason")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| "api-retirement-request".to_string())
}

fn present_filter(value: Option<String>) -> Option<String> {
    value.filter(|text| !text.is_empty())
}

fn read_object(
    headers: &HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Value, AppError> {
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
    let bytes = body.map_err(|_| AppError {
        status: 413,
        code: codes::UPLOAD_TOO_LARGE,
        message: "Request body is too large".into(),
    })?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| AppError::invalid_payload(format!("Invalid JSON: {error}")))?;
    if !value.is_object() {
        return Err(AppError::invalid_payload(
            "Request body must be a JSON object",
        ));
    }
    Ok(value)
}

fn content_length(headers: &HeaderMap) -> Result<usize, AppError> {
    let raw = headers
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("0");
    let length = raw
        .parse::<i64>()
        .map_err(|_| AppError::invalid_payload("Invalid Content-Length"))?;
    if length < 0 {
        return Err(AppError::invalid_payload("Invalid Content-Length"));
    }
    Ok(length as usize)
}

fn write_not_owned() -> AppError {
    AppError {
        status: 409,
        code: WRITE_NOT_OWNED,
        message: "The backup retirement store is still written by the Python runtime, so this gateway refuses to mutate it.".into(),
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

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

/// `datetime.now(timezone.utc).isoformat(timespec="seconds")` with `Z`.
fn utc_iso_from_unix(unix_secs: u64) -> String {
    let days = (unix_secs / 86_400) as i64;
    let tod = unix_secs % 86_400;
    let hour = tod / 3_600;
    let minute = (tod % 3_600) / 60;
    let second = tod % 60;
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Howard Hinnant's `civil_from_days`. `z` is days since 1970-01-01.
fn civil_from_days(mut z: i64) -> (i64, u32, u32) {
    z += 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { year + 1 } else { year };
    (year, month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_iso_matches_known_unix_instants() {
        assert_eq!(utc_iso_from_unix(0), "1970-01-01T00:00:00Z");
        assert_eq!(utc_iso_from_unix(86_400), "1970-01-02T00:00:00Z");
        assert_eq!(utc_iso_from_unix(1_700_000_000), "2023-11-14T22:13:20Z");
    }

    #[test]
    fn ids_follow_python_str_or_strip_and_reason_does_not_strip() {
        let payload = json!({
            "policyId": "  pol  ",
            "backupId": 12,
            "targetId": false,
            "reason": "  keep  "
        });
        assert_eq!(required_id(&payload, "policyId"), "pol");
        assert_eq!(required_id(&payload, "backupId"), "12");
        assert_eq!(required_id(&payload, "targetId"), "");
        assert_eq!(reason_text(&payload), "  keep  ");
        assert_eq!(reason_text(&json!({})), "api-retirement-request");
        assert_eq!(
            reason_text(&json!({"reason": ""})),
            "api-retirement-request"
        );
    }
}
