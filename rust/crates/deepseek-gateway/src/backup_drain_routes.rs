//! Target drain routes (S4):
//! - `GET /api/workspace/backup-targets/{target_id}/drain`
//! - `POST /api/workspace/backup-targets/{target_id}/drain`
//! - `POST /api/workspace/backup-targets/{target_id}/drain/cancel`
//!
//! `.backup-drains/drains.sqlite3` belongs to `backup_drain_store` (a declared
//! native data domain). Reads open existing SQLite read-only and return `404`
//! without creating `.backup-drains/` when missing. Mutations fail closed with
//! `409 NATIVE_BACKUP_DRAIN_WRITE_NOT_OWNED` unless `DEEPSEEK_RUNTIME_MODE=python_disabled`.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Value, json};

use crate::backup_catalog_routes::{
    open_readonly_sqlite, registered_target, unix_seconds, utc_iso_from_unix,
};
use crate::route_support;

const BODY_LIMIT: i64 = 16_000;
const WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_DRAIN_WRITE_NOT_OWNED";

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backup-targets/:target_id/drain",
            any(dispatch_drain),
        )
        .route(
            "/api/workspace/backup-targets/:target_id/drain/cancel",
            any(dispatch_drain_cancel),
        )
}

fn drain_db_path() -> PathBuf {
    crate::data_routes::workspace_root()
        .join(".backup-drains")
        .join("drains.sqlite3")
}

fn valid_target_id(target_id: &str) -> bool {
    !target_id.is_empty()
        && target_id != "."
        && target_id != ".."
        && !target_id.contains('/')
        && !target_id.contains('\\')
        && !target_id.contains('\0')
}

async fn dispatch_drain(
    method: Method,
    AxumPath(target_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method == Method::GET || method == Method::HEAD {
        let is_head = method == Method::HEAD;
        return match tokio::task::spawn_blocking(move || read_drain_job(&target_id)).await {
            Ok(Ok(Some(_))) if is_head => StatusCode::OK.into_response(),
            Ok(Ok(Some(job))) => axum::Json(job).into_response(),
            Ok(Ok(None)) => {
                let err = AppError::not_found("No drain job for target");
                if is_head {
                    StatusCode::NOT_FOUND.into_response()
                } else {
                    route_support::app_error_response(err)
                }
            }
            Ok(Err(error)) if is_head => StatusCode::from_u16(error.status)
                .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
                .into_response(),
            Ok(Err(error)) => route_support::app_error_response(error),
            Err(_) => {
                if is_head {
                    StatusCode::INTERNAL_SERVER_ERROR.into_response()
                } else {
                    route_support::app_error_response(route_support::unhandled_error())
                }
            }
        };
    }
    if method == Method::POST {
        return match tokio::task::spawn_blocking(move || {
            handle_start_drain(&target_id, &headers, &body)
        })
        .await
        {
            Ok(Ok(job)) => axum::Json(job).into_response(),
            Ok(Err(error)) => route_support::app_error_response(error),
            Err(_) => route_support::app_error_response(route_support::unhandled_error()),
        };
    }
    route_support::method_not_allowed()
}

async fn dispatch_drain_cancel(
    method: Method,
    AxumPath(target_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method != Method::POST {
        return route_support::method_not_allowed();
    }
    match tokio::task::spawn_blocking(move || handle_cancel_drain(&target_id, &headers, &body))
        .await
    {
        Ok(Ok(job)) => axum::Json(job).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn drain_row_to_json(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let drain_id: String = row.get(0)?;
    let target_id: String = row.get(1)?;
    let phase: String = row.get(2)?;
    let reason: Option<String> = row.get(3)?;
    let started_at: String = row.get(4)?;
    let updated_at: String = row.get(5)?;
    let protected_points: i64 = row.get::<_, Option<i64>>(6)?.unwrap_or(0);
    let remaining_copies: i64 = row.get::<_, Option<i64>>(7)?.unwrap_or(0);
    let active_rebalances: i64 = row.get::<_, Option<i64>>(8)?.unwrap_or(0);
    let bytes_remaining: i64 = row.get::<_, Option<i64>>(9)?.unwrap_or(0);
    let error: Option<String> = row.get(10)?;
    Ok(json!({
        "drainId": drain_id,
        "targetId": target_id,
        "phase": phase,
        "drainState": phase,
        "reason": reason,
        "startedAt": started_at,
        "updatedAt": updated_at,
        "protectedRecoveryPoints": protected_points,
        "remainingRequiredCopies": remaining_copies,
        "activeRebalances": active_rebalances,
        "bytesRemaining": bytes_remaining,
        "error": error,
    }))
}

fn query_drain_job(conn: &Connection, target_id: &str) -> Result<Option<Value>, AppError> {
    let has_table: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'target_drain_jobs'",
            [],
            |_| Ok(true),
        )
        .optional()
        .map_err(|_| route_support::unhandled_error())?
        .unwrap_or(false);
    if !has_table {
        return Ok(None);
    }
    conn.query_row(
        "SELECT drain_id, target_id, phase, reason, started_at, updated_at, \
         protected_recovery_points, remaining_required_copies, active_rebalances, \
         bytes_remaining, error \
         FROM target_drain_jobs WHERE target_id = ?1",
        [target_id],
        drain_row_to_json,
    )
    .optional()
    .map_err(|_| route_support::unhandled_error())
}

fn read_drain_job(target_id: &str) -> Result<Option<Value>, AppError> {
    if !valid_target_id(target_id) {
        return Err(AppError::invalid_payload("Invalid target id"));
    }
    let db = drain_db_path();
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(None);
    };
    query_drain_job(&conn, target_id)
}

fn open_drain_db_rw(path: &Path) -> Result<Connection, AppError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let conn = Connection::open(path).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(Duration::from_millis(5000))
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA busy_timeout=5000;
         CREATE TABLE IF NOT EXISTS target_drain_jobs (
             drain_id TEXT PRIMARY KEY,
             target_id TEXT NOT NULL UNIQUE,
             phase TEXT NOT NULL,
             reason TEXT,
             started_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             protected_recovery_points INTEGER DEFAULT 0,
             remaining_required_copies INTEGER DEFAULT 0,
             active_rebalances INTEGER DEFAULT 0,
             bytes_remaining INTEGER DEFAULT 0,
             error TEXT
         );
         CREATE INDEX IF NOT EXISTS idx_drain_phase ON target_drain_jobs(phase);",
    )
    .map_err(|_| route_support::unhandled_error())?;
    Ok(conn)
}

fn non_empty_reason(payload: &serde_json::Map<String, Value>, default_reason: &str) -> String {
    match payload.get("reason") {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(other) if !other.is_null() && other != &Value::Bool(false) => {
            let rendered = deepseek_policy::python_json::value_str(other);
            if rendered.is_empty() {
                default_reason.to_string()
            } else {
                rendered
            }
        }
        _ => default_reason.to_string(),
    }
}

fn handle_start_drain(
    target_id: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Value, AppError> {
    if !valid_target_id(target_id) {
        return Err(AppError::invalid_payload("Invalid target id"));
    }
    let payload = route_support::read_json_object(headers, body, BODY_LIMIT)?;
    route_support::require_native_store("backup_drain_store", WRITE_NOT_OWNED)?;
    let _target = registered_target(target_id).map_err(|err| {
        if err.status == 404 {
            AppError::not_found(format!("Target {target_id} not found in registry"))
        } else {
            err
        }
    })?;
    let reason = non_empty_reason(&payload, "administrative-drain");
    let drain_id = format!("drain_{}", SystemEntropy.new_id()?);
    let now = utc_iso_from_unix(unix_seconds());
    let conn = open_drain_db_rw(&drain_db_path())?;
    conn.execute(
        "INSERT INTO target_drain_jobs(\
            drain_id, target_id, phase, reason, started_at, updated_at, \
            protected_recovery_points, remaining_required_copies, active_rebalances, bytes_remaining, error\
        ) VALUES (?1, ?2, 'draining', ?3, ?4, ?4, 0, 0, 0, 0, NULL) \
        ON CONFLICT(target_id) DO UPDATE SET \
            drain_id = excluded.drain_id, \
            phase = 'draining', \
            reason = excluded.reason, \
            updated_at = excluded.updated_at, \
            error = NULL",
        (&drain_id, target_id, &reason, &now),
    )
    .map_err(|_| route_support::unhandled_error())?;
    query_drain_job(&conn, target_id)
        .map(|opt| opt.unwrap_or_else(|| Value::Object(Default::default())))
}

fn handle_cancel_drain(
    target_id: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Value, AppError> {
    if !valid_target_id(target_id) {
        return Err(AppError::invalid_payload("Invalid target id"));
    }
    let payload = route_support::read_json_object(headers, body, BODY_LIMIT)?;
    route_support::require_native_store("backup_drain_store", WRITE_NOT_OWNED)?;
    let _target = registered_target(target_id).map_err(|err| {
        if err.status == 404 {
            AppError::not_found(format!("Target {target_id} not found in registry"))
        } else {
            err
        }
    })?;
    let reason = non_empty_reason(&payload, "operator-cancelled");
    let now = utc_iso_from_unix(unix_seconds());
    let conn = open_drain_db_rw(&drain_db_path())?;
    conn.execute(
        "UPDATE target_drain_jobs SET phase = 'cancelled', error = ?1, updated_at = ?2 WHERE target_id = ?3",
        (&reason, &now, target_id),
    )
    .map_err(|_| route_support::unhandled_error())?;
    query_drain_job(&conn, target_id)
        .map(|opt| opt.unwrap_or_else(|| Value::Object(Default::default())))
}
