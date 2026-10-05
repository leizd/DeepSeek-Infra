//! `GET /api/workspace/backup-runs`.
//!
//! The list is `backup_scheduler.list_runs` with the route's default limit of 50.
//! There is no write on this path. The scheduler database stays the Python
//! worker's: a missing `.backup-scheduler/scheduler.db` answers `{"runs":[]}`
//! and does not create the directory, and an existing file is opened read-only.
//! Setting WAL or creating tables here would make a reader into a second writer.

use std::path::PathBuf;

use axum::extract::Query;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{Json, Router, routing::get};
use deepseek_policy::app_error::{AppError, codes};
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;
use serde_json::{Value, json};

const LIST_LIMIT: i64 = 50;

pub fn router() -> Router {
    Router::new().route("/api/workspace/backup-runs", get(list_runs))
}

#[derive(Debug, Deserialize, Default)]
struct RunsQuery {
    #[serde(rename = "policyId")]
    policy_id: Option<String>,
}

struct StoredRun {
    run_id: String,
    policy_id: String,
    schedule_slot: String,
    phase: String,
    attempt: i64,
    owner_instance_id: Option<String>,
    fencing_token: Option<i64>,
    lease_until: Option<String>,
    reason: Option<String>,
    error: Option<String>,
    backup_id: Option<String>,
    filename: Option<String>,
    created_at: String,
    updated_at: String,
}

async fn list_runs(Query(query): Query<RunsQuery>) -> Response {
    let policy_id = query.policy_id.filter(|value| !value.is_empty());
    match tokio::task::spawn_blocking(move || load_runs(policy_id)).await {
        Ok(Ok(runs)) => Json(json!({"runs": runs})).into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) => error_response(server_error()),
    }
}

fn load_runs(policy_id: Option<String>) -> Result<Vec<Value>, AppError> {
    let path = db_path();
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let conn = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| server_error())?;
    conn.busy_timeout(std::time::Duration::from_millis(30_000))
        .map_err(|_| server_error())?;
    let present: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'backup_runs'",
            [],
            |_| Ok(true),
        )
        .unwrap_or(false);
    if !present {
        return Err(server_error());
    }
    let columns = "run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, \
         fencing_token, lease_until, reason, error, backup_id, filename, created_at, updated_at";
    let mut statement;
    let mut rows = if let Some(policy_id) = policy_id {
        statement = conn
            .prepare(&format!(
                "SELECT {columns} FROM backup_runs WHERE policy_id = ?1 \
                 ORDER BY created_at DESC LIMIT {LIST_LIMIT}"
            ))
            .map_err(|_| server_error())?;
        statement
            .query(rusqlite::params![policy_id])
            .map_err(|_| server_error())?
    } else {
        statement = conn
            .prepare(&format!(
                "SELECT {columns} FROM backup_runs ORDER BY created_at DESC LIMIT {LIST_LIMIT}"
            ))
            .map_err(|_| server_error())?;
        statement.query([]).map_err(|_| server_error())?
    };
    let mut runs = Vec::new();
    loop {
        let Some(row) = rows.next().map_err(|_| server_error())? else {
            break;
        };
        runs.push(to_json(read_stored(row).map_err(|_| server_error())?)?);
    }
    Ok(runs)
}

fn read_stored(row: &rusqlite::Row<'_>) -> rusqlite::Result<StoredRun> {
    Ok(StoredRun {
        run_id: row.get(0)?,
        policy_id: row.get(1)?,
        schedule_slot: row.get(2)?,
        phase: row.get(3)?,
        attempt: row.get(4)?,
        owner_instance_id: row.get(5)?,
        fencing_token: row.get(6)?,
        lease_until: row.get(7)?,
        reason: row.get(8)?,
        error: row.get(9)?,
        backup_id: row.get(10)?,
        filename: row.get(11)?,
        created_at: row.get(12)?,
        updated_at: row.get(13)?,
    })
}

fn to_json(run: StoredRun) -> Result<Value, AppError> {
    let blocked = matches!(
        run.phase.as_str(),
        "blocked" | "blocked-retryable" | "blocked-terminal"
    );
    Ok(json!({
        "runId": run.run_id,
        "policyId": run.policy_id,
        "scheduleSlot": run.schedule_slot,
        "phase": run.phase,
        "attempt": run.attempt,
        "ownerInstanceId": run.owner_instance_id,
        "fencingToken": run.fencing_token,
        "leaseUntil": run.lease_until,
        "nextRetryAt": if blocked { run.lease_until.clone() } else { None },
        "blockedReason": if blocked { run.reason.clone() } else { None },
        "reason": run.reason,
        "error": run.error,
        "backupId": run.backup_id,
        "filename": run.filename,
        "createdAt": run.created_at,
        "updatedAt": run.updated_at,
    }))
}

fn db_path() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-scheduler")
        .join("scheduler.db")
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
