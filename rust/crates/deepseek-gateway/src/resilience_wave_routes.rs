//! Production closed-loop execution & wave orchestration endpoints (Slice 7c):
//! - `POST /api/workspace/resilience/execute`
//! - `POST /api/workspace/resilience/waves/admit`
//! - `POST /api/workspace/resilience/waves/run`

use std::collections::HashSet;
use std::fs;
use std::path::Path;

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::{dumps_compact, value_str};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::backup_catalog_routes::utc_iso_from_unix;
use crate::backup_dr_slo_routes::load_drill_items;
use crate::resilience_action_exec::{
    execute_autonomous_action_db, read_rebalance_job_fs, read_repair_job_fs,
    record_action_intent_db,
};
use crate::resilience_plan_routes::{read_optional_json_object, write_not_owned};
use crate::resilience_state_support::{
    build_fresh_state_bundle_readonly, get_action_from_conn, new_hex_id, now_unix_secs_and_iso,
    open_journal_rw, parse_utc_epoch, sha256_compact,
};
use crate::route_support::{self, unhandled_error};

const SCHEDULE_IDENTITY_DOMAIN: &[u8] = b"deepseek-infra:resilience-wave-schedule-identity-v1\0";

const WAVES_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS resilience_wave_schedules (
    schedule_id TEXT PRIMARY KEY,
    schedule_digest TEXT,
    status TEXT NOT NULL,
    risk_digest TEXT NOT NULL,
    authority_head_digest TEXT,
    schedule_json TEXT NOT NULL,
    stale_reason TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resilience_wave_states (
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    revalidation_json TEXT,
    admitted_at TEXT,
    verified_at TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, wave_index)
);
CREATE TABLE IF NOT EXISTS resilience_wave_actions (
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    action_json TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    schedule_execution_epoch INTEGER NOT NULL DEFAULT 0,
    wave_execution_epoch INTEGER NOT NULL DEFAULT 0,
    owner_instance_id TEXT,
    lease_until TEXT,
    runner_token TEXT,
    journal_execution_epoch INTEGER NOT NULL DEFAULT 0,
    effect_handle_json TEXT,
    terminal_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_resilience_wave_actions_wave
ON resilience_wave_actions(schedule_id, wave_index, status);
";

const SCHEDULER_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS resilience_scheduler_service (
    policy_id TEXT PRIMARY KEY,
    virtual_runtime REAL NOT NULL,
    virtual_finish REAL NOT NULL,
    actions_served INTEGER NOT NULL,
    bytes_served INTEGER NOT NULL,
    last_scheduled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resilience_scheduler_service_events (
    action_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    service_units REAL NOT NULL,
    bytes_served INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resilience_scheduler_runs (
    schedule_id TEXT PRIMARY KEY,
    schedule_json TEXT NOT NULL,
    scheduled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_scheduler_runs_time
ON resilience_scheduler_runs(scheduled_at);
CREATE TABLE IF NOT EXISTS resilience_service_reservations (
    action_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    execution_epoch INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    estimated_bytes INTEGER NOT NULL,
    reserved_units REAL NOT NULL,
    status TEXT NOT NULL,
    actual_bytes INTEGER,
    actual_duration_ms REAL,
    actual_traffic_class TEXT,
    outcome TEXT,
    release_reason TEXT,
    effect_handle_json TEXT,
    transfer_id TEXT,
    telemetry_digest TEXT,
    settlement_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_service_reservations_schedule
ON resilience_service_reservations(schedule_id, status);
CREATE TABLE IF NOT EXISTS resilience_service_settlement_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    effect_handle_json TEXT,
    transfer_id TEXT,
    telemetry_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_service_settlement_events_action
ON resilience_service_settlement_events(action_id, event_id);
";

const JOURNAL_FAILURE_STATES: [&str; 9] = [
    "COMPENSATED",
    "COMPENSATION_REQUIRED",
    "FAILED_BEFORE_EFFECT",
    "NEEDS_OPERATOR",
    "EFFECT_UNKNOWN",
    "BLOCKED",
    "SKIPPED_NO_LONGER_NEEDED",
    "PREEMPTED",
    "REPLAN_REQUIRED",
];

const JOURNAL_ACTIVE_STATES: [&str; 5] = [
    "CLAIMED",
    "EXECUTING",
    "RECONCILING",
    "VERIFYING",
    "ASSESSING_EFFECT",
];

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/resilience/execute", any(dispatch_execute))
        .route(
            "/api/workspace/resilience/waves/admit",
            any(dispatch_waves_admit),
        )
        .route(
            "/api/workspace/resilience/waves/run",
            any(dispatch_waves_run),
        )
}

async fn dispatch_execute(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let action_id = body_obj
        .get("actionId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if action_id.is_empty() {
        if body_obj.get("type").is_some_and(python_truthy) {
            return route_support::app_error_response(AppError::invalid_payload(
                "Raw action execution not permitted; actions must be materialized from a valid ResiliencePlan bound to a fresh RiskSnapshot",
            ));
        }
        return route_support::app_error_response(AppError::invalid_payload(
            "actionId is required",
        ));
    }

    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        execute_autonomous_action_db(&root, &action_id, "resilience-worker", 120)
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

async fn dispatch_waves_admit(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let schedule_id = body_obj
        .get("scheduleId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if schedule_id.is_empty() {
        return route_support::app_error_response(AppError::invalid_payload(
            "scheduleId is required",
        ));
    }
    let wave_index = match body_obj.get("waveIndex") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(_)) => {
            return route_support::app_error_response(AppError::invalid_payload(
                "waveIndex must be an integer",
            ));
        }
        Some(v) => {
            if let Some(n) = v.as_i64() {
                Some(n)
            } else if let Some(s) = v.as_str() {
                match s.trim().parse::<i64>() {
                    Ok(parsed) => Some(parsed),
                    Err(_) => {
                        return route_support::app_error_response(AppError::invalid_payload(
                            "waveIndex must be an integer",
                        ));
                    }
                }
            } else {
                return route_support::app_error_response(AppError::invalid_payload(
                    "waveIndex must be an integer",
                ));
            }
        }
    };

    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        admit_wave_db(&root, &schedule_id, wave_index)
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

async fn dispatch_waves_run(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let schedule_id = body_obj
        .get("scheduleId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if schedule_id.is_empty() {
        return route_support::app_error_response(AppError::invalid_payload(
            "scheduleId is required",
        ));
    }

    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        run_next_wave_db(&root, &schedule_id, "api-wave-runner", 120)
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

fn ensure_table_columns(
    conn: &Connection,
    table: &str,
    columns: &[(&str, &str)],
) -> Result<(), AppError> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|_| unhandled_error())?;
    let existing: HashSet<String> = stmt
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(|_| unhandled_error())?
        .flatten()
        .collect();
    drop(stmt);
    for (col, def) in columns {
        if !existing.contains(*col) {
            conn.execute(&format!("ALTER TABLE {table} ADD COLUMN {col} {def}"), [])
                .map_err(|_| unhandled_error())?;
        }
    }
    Ok(())
}

fn compute_schedule_digest(schedule: &Value, authority_head_digest: Option<&str>) -> String {
    let mut normalized = schedule.as_object().cloned().unwrap_or_default();
    normalized.remove("scheduleDigest");
    if let Some(ahd) = authority_head_digest {
        normalized.insert(
            "authorityHeadDigest".to_string(),
            Value::String(ahd.to_string()),
        );
    }
    let canonical = dumps_compact(&Value::Object(normalized));
    let mut hasher = Sha256::new();
    hasher.update(SCHEDULE_IDENTITY_DOMAIN);
    hasher.update(canonical.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn open_waves_rw(root: &Path) -> Result<Connection, AppError> {
    let dir = root.join(".resilience-waves");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let conn = Connection::open(dir.join("waves.sqlite3")).map_err(|_| unhandled_error())?;
    conn.execute_batch("PRAGMA busy_timeout = 30000; PRAGMA journal_mode = WAL;")
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(WAVES_SCHEMA)
        .map_err(|_| unhandled_error())?;
    ensure_table_columns(
        &conn,
        "resilience_wave_schedules",
        &[
            ("schedule_digest", "TEXT"),
            ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_instance_id", "TEXT"),
            ("lease_until", "TEXT"),
            ("runner_token", "TEXT"),
        ],
    )?;
    ensure_table_columns(
        &conn,
        "resilience_wave_states",
        &[
            ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_instance_id", "TEXT"),
            ("lease_until", "TEXT"),
            ("runner_token", "TEXT"),
        ],
    )?;
    ensure_table_columns(
        &conn,
        "resilience_wave_actions",
        &[
            ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("schedule_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("wave_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_instance_id", "TEXT"),
            ("lease_until", "TEXT"),
            ("runner_token", "TEXT"),
            ("journal_execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("effect_handle_json", "TEXT"),
            ("terminal_json", "TEXT"),
        ],
    )?;
    let mut stmt = conn
        .prepare(
            "SELECT schedule_id, schedule_json, authority_head_digest
             FROM resilience_wave_schedules
             WHERE schedule_digest IS NULL OR schedule_digest = ''",
        )
        .map_err(|_| unhandled_error())?;
    let missing: Vec<(String, String, Option<String>)> = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .map_err(|_| unhandled_error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unhandled_error())?;
    drop(stmt);
    for (sid, s_raw, ahd) in missing {
        let sched_val: Value = serde_json::from_str(&s_raw).map_err(|_| unhandled_error())?;
        let dig = compute_schedule_digest(&sched_val, ahd.as_deref());
        conn.execute(
            "UPDATE resilience_wave_schedules SET schedule_digest = ?
             WHERE schedule_id = ? AND (schedule_digest IS NULL OR schedule_digest = '')",
            rusqlite::params![dig, sid],
        )
        .map_err(|_| unhandled_error())?;
    }
    Ok(conn)
}

fn open_scheduler_rw(root: &Path) -> Result<Connection, AppError> {
    let dir = root.join(".resilience-scheduler");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let conn = Connection::open(dir.join("service.sqlite3")).map_err(|_| unhandled_error())?;
    conn.execute_batch("PRAGMA busy_timeout = 30000; PRAGMA journal_mode = WAL;")
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(SCHEDULER_SCHEMA)
        .map_err(|_| unhandled_error())?;
    ensure_table_columns(
        &conn,
        "resilience_service_reservations",
        &[
            ("effect_handle_json", "TEXT"),
            ("transfer_id", "TEXT"),
            ("telemetry_digest", "TEXT"),
            ("settlement_started_at", "TEXT"),
        ],
    )?;
    Ok(conn)
}

fn schedule_record_from_conn(
    conn: &Connection,
    schedule_id: &str,
) -> Result<Option<Value>, AppError> {
    let row = conn
        .query_row(
            "SELECT schedule_id, schedule_digest, status, risk_digest, authority_head_digest,
                    schedule_json, stale_reason, execution_epoch, owner_instance_id,
                    lease_until, created_at, updated_at
             FROM resilience_wave_schedules WHERE schedule_id = ?",
            [schedule_id],
            |r| {
                let s_raw: String = r.get(5)?;
                let sched: Value = serde_json::from_str(&s_raw).unwrap_or_else(|_| json!({}));
                Ok(json!({
                    "scheduleId": r.get::<_, String>(0)?,
                    "scheduleDigest": r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    "status": r.get::<_, String>(2)?,
                    "riskDigest": r.get::<_, String>(3)?,
                    "authorityHeadDigest": r.get::<_, Option<String>>(4)?,
                    "schedule": sched,
                    "staleReason": r.get::<_, Option<String>>(6)?,
                    "scheduleExecutionEpoch": r.get::<_, Option<i64>>(7)?.unwrap_or(0),
                    "ownerInstanceId": r.get::<_, Option<String>>(8)?,
                    "leaseUntil": r.get::<_, Option<String>>(9)?,
                    "createdAt": r.get::<_, String>(10)?,
                    "updatedAt": r.get::<_, String>(11)?,
                }))
            },
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(row)
}

fn wave_record_from_conn(
    conn: &Connection,
    schedule_id: &str,
    wave_index: i64,
) -> Result<Option<Value>, AppError> {
    let row = conn
        .query_row(
            "SELECT schedule_id, wave_index, status, revalidation_json, admitted_at,
                    verified_at, execution_epoch, owner_instance_id, lease_until, updated_at
             FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
            rusqlite::params![schedule_id, wave_index],
            |r| {
                let rev_raw: Option<String> = r.get(3)?;
                let rev = rev_raw
                    .as_deref()
                    .and_then(|s| serde_json::from_str::<Value>(s).ok());
                Ok(json!({
                    "scheduleId": r.get::<_, String>(0)?,
                    "waveIndex": r.get::<_, i64>(1)?,
                    "status": r.get::<_, String>(2)?,
                    "revalidation": rev,
                    "admittedAt": r.get::<_, Option<String>>(4)?,
                    "verifiedAt": r.get::<_, Option<String>>(5)?,
                    "waveExecutionEpoch": r.get::<_, Option<i64>>(6)?.unwrap_or(0),
                    "ownerInstanceId": r.get::<_, Option<String>>(7)?,
                    "leaseUntil": r.get::<_, Option<String>>(8)?,
                    "updatedAt": r.get::<_, String>(9)?,
                }))
            },
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(row)
}

fn predecessors_verified(
    conn: &Connection,
    schedule_id: &str,
    wave_index: i64,
) -> Result<bool, AppError> {
    if wave_index <= 0 {
        return Ok(true);
    }
    let pending: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM resilience_wave_actions
             WHERE schedule_id = ? AND wave_index < ? AND status != 'VERIFIED_SUCCESS'",
            rusqlite::params![schedule_id, wave_index],
            |r| r.get(0),
        )
        .map_err(|_| unhandled_error())?;
    let incomplete_waves: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM resilience_wave_states
             WHERE schedule_id = ? AND wave_index < ? AND status != 'COMPLETED'",
            rusqlite::params![schedule_id, wave_index],
            |r| r.get(0),
        )
        .map_err(|_| unhandled_error())?;
    Ok(pending == 0 && incomplete_waves == 0)
}

fn revalidate_wave(schedule: &Value, fresh_state_bundle: &Value) -> Value {
    let mut reasons = Vec::new();
    let planned_risk = schedule
        .get("riskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let fresh_risk = fresh_state_bundle
        .get("riskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if planned_risk.is_empty() {
        reasons.push("PLANNED_RISK_BINDING_MISSING".to_string());
    } else if fresh_risk != planned_risk {
        reasons.push("RISK_SNAPSHOT_STALE".to_string());
    }

    let planned_auth = schedule
        .get("authorityHeadDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let fresh_auth = fresh_state_bundle
        .get("authorityHeadDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if planned_auth.is_empty() {
        reasons.push("PLANNED_AUTHORITY_BINDING_MISSING".to_string());
    } else if fresh_auth != planned_auth {
        reasons.push("AUTHORITY_HEAD_STALE".to_string());
    }

    let auth_ok = fresh_state_bundle
        .get("authorityState")
        .and_then(Value::as_object)
        .is_some_and(|a| {
            a.get("workersAllowed") == Some(&Value::Bool(true))
                && a.get("mutationsAllowed") == Some(&Value::Bool(true))
        });
    if !auth_ok {
        reasons.push("AUTHORITY_MUTATIONS_BLOCKED".to_string());
    }

    let maint_ok = fresh_state_bundle
        .get("maintenanceDecisions")
        .and_then(Value::as_array)
        .is_some_and(|arr| {
            arr.iter()
                .all(|item| item.get("allowed") == Some(&Value::Bool(true)))
        });
    if !maint_ok {
        reasons.push("MAINTENANCE_WINDOW_BLOCKED".to_string());
    }

    let budget_ok = fresh_state_bundle
        .get("budgets")
        .and_then(|b| b.get("admitted"))
        == Some(&Value::Bool(true));
    if !budget_ok {
        reasons.push("RESOURCE_OR_TRANSFER_BUDGET_DENIED".to_string());
    }

    let blast_ok = fresh_state_bundle
        .get("blastSimulation")
        .and_then(|b| b.get("passed"))
        == Some(&Value::Bool(true));
    if !blast_ok {
        reasons.push("BLAST_RADIUS_REVALIDATION_FAILED".to_string());
    }

    json!({
        "fresh": reasons.is_empty(),
        "reasons": reasons,
        "authorityHeadDigest": fresh_auth,
        "riskDigest": fresh_risk,
        "freshStateBundle": fresh_state_bundle,
    })
}

fn release_action_reservation_db(
    root: &Path,
    action_id: &str,
    reason: &str,
    now_iso: &str,
) -> Result<(), AppError> {
    let norm = match reason.to_uppercase().as_str() {
        "PREEMPTED" => "PREEMPTED",
        "STALE" => "STALE",
        "REPLAN" => "REPLAN",
        "EXPIRED" => "EXPIRED",
        "FAILED" => "FAILED",
        "SUPERSEDED" => "SUPERSEDED",
        _ => "UNEXECUTED",
    };
    let next_status = if norm == "EXPIRED" {
        "EXPIRED"
    } else {
        "RELEASED"
    };
    let conn = open_scheduler_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;
    let status: Option<String> = conn
        .query_row(
            "SELECT status FROM resilience_service_reservations WHERE action_id = ?",
            [action_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    if status.as_deref().is_some_and(|s| s != "CONSUMED") {
        let _ = conn.execute(
            "UPDATE resilience_service_reservations
             SET status = ?, release_reason = ?, updated_at = ?
             WHERE action_id = ? AND status IN ('RESERVED', 'CONSUMING', 'RECONCILING')",
            rusqlite::params![next_status, norm, now_iso, action_id],
        );
    }
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;
    Ok(())
}

fn release_schedule_reservations_db(
    root: &Path,
    schedule_id: &str,
    reason: &str,
    now_iso: &str,
) -> Result<(), AppError> {
    let conn = open_scheduler_rw(root)?;
    let mut stmt = conn
        .prepare(
            "SELECT action_id FROM resilience_service_reservations
             WHERE schedule_id = ? AND status IN ('RESERVED', 'CONSUMING', 'RECONCILING')",
        )
        .map_err(|_| unhandled_error())?;
    let ids: Vec<String> = stmt
        .query_map([schedule_id], |r| r.get(0))
        .map_err(|_| unhandled_error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unhandled_error())?;
    drop(stmt);
    drop(conn);
    for aid in ids {
        release_action_reservation_db(root, &aid, reason, now_iso)?;
    }
    Ok(())
}

fn traffic_class_name(raw: i64) -> Result<&'static str, String> {
    match raw {
        0 => Ok("P0_DISASTER_RECOVERY"),
        1 => Ok("P1_BACKUP_PUBLISH"),
        2 => Ok("P2_REQUIRED_REPAIR"),
        3 => Ok("P3_REQUIRED_REPLICATION"),
        4 => Ok("P4_SCRUB_DRILL"),
        5 => Ok("P5_REBALANCE_DRAIN"),
        _ => Err("traffic class unavailable".to_string()),
    }
}

fn read_terminal_effect_telemetry(root: &Path, action: &Value) -> Result<Value, String> {
    let action_id = action
        .get("actionId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if action_id.is_empty() || action.get("state").and_then(Value::as_str) != Some("SUCCEEDED") {
        return Err("Action is not terminal-success".to_string());
    }
    if !action
        .get("verificationResult")
        .is_some_and(Value::is_object)
    {
        return Err("Action outcome is not verified".to_string());
    }
    let execution_epoch = action
        .get("executionEpoch")
        .and_then(Value::as_i64)
        .filter(|e| *e >= 1)
        .ok_or_else(|| "Action execution epoch is unavailable".to_string())?;
    let Some(handle) = action.get("effectHandle").filter(|h| h.is_object()) else {
        return Err("Action effect handle is unavailable".to_string());
    };
    let kind = handle
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_lowercase();

    let mut telemetry_map = serde_json::Map::new();
    telemetry_map.insert("actionId".to_string(), Value::String(action_id.clone()));
    telemetry_map.insert("actionExecutionEpoch".to_string(), json!(execution_epoch));
    telemetry_map.insert("effectHandle".to_string(), handle.clone());
    telemetry_map.insert(
        "outcome".to_string(),
        Value::String("SUCCEEDED".to_string()),
    );

    let validate_scope = |eff: &Value| -> Result<(), String> {
        let params = action.get("parameters").and_then(Value::as_object);
        for f in ["policyId", "backupId"] {
            let exp = params
                .and_then(|p| p.get(f))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let obs = eff
                .get(f)
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if !exp.is_empty() && obs != exp {
                return Err(format!("effect {f} does not match Action"));
            }
        }
        Ok(())
    };

    if kind == "repair" {
        let rid = handle
            .get("repairId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let job = read_repair_job_fs(root, &rid)
            .filter(|j| j.get("repairId").and_then(Value::as_str) == Some(&rid))
            .ok_or_else(|| "repair effect is unavailable".to_string())?;
        if job.get("resilienceActionId").and_then(Value::as_str) != Some(&action_id) {
            return Err("repair effect is not bound to Action".to_string());
        }
        if job.get("phase").and_then(Value::as_str) != Some("healthy") {
            return Err("repair effect is not terminal-success".to_string());
        }
        validate_scope(&job)?;
        let tc_raw = job
            .get("trafficClass")
            .and_then(Value::as_i64)
            .ok_or_else(|| "repair traffic class is unavailable".to_string())?;
        let tc = traffic_class_name(tc_raw)?;
        let bytes = job
            .get("bytesRepaired")
            .and_then(Value::as_i64)
            .filter(|v| *v >= 0)
            .ok_or_else(|| "invalid bytesRepaired".to_string())?;
        let dur_ms = job
            .get("durationMs")
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .ok_or_else(|| "invalid durationMs".to_string())?;
        telemetry_map.insert(
            "effectType".to_string(),
            Value::String("repair".to_string()),
        );
        telemetry_map.insert("transferId".to_string(), Value::String(rid));
        telemetry_map.insert("actualBytesTransferred".to_string(), json!(bytes));
        telemetry_map.insert("actualDurationMs".to_string(), json!(dur_ms));
        telemetry_map.insert("trafficClass".to_string(), Value::String(tc.to_string()));
        telemetry_map.insert(
            "source".to_string(),
            Value::String("repair-job-ledger".to_string()),
        );
        telemetry_map.insert(
            "sourceDigest".to_string(),
            Value::String(sha256_compact(&job)),
        );
    } else if kind == "rebalance" {
        let jid = handle
            .get("jobId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let job = read_rebalance_job_fs(root, &jid)
            .filter(|j| j.get("jobId").and_then(Value::as_str) == Some(&jid))
            .ok_or_else(|| "rebalance effect is unavailable".to_string())?;
        if job.get("resilienceActionId").and_then(Value::as_str) != Some(&action_id) {
            return Err("rebalance effect is not bound to Action".to_string());
        }
        if job.get("phase").and_then(Value::as_str) != Some("complete") {
            return Err("rebalance effect is not terminal-success".to_string());
        }
        validate_scope(&job)?;
        let bytes = job
            .get("bytesTransferred")
            .and_then(Value::as_i64)
            .filter(|v| *v >= 0)
            .ok_or_else(|| "invalid bytesTransferred".to_string())?;
        let dur_ms = job
            .get("durationMs")
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .or_else(|| {
                let st = parse_utc_epoch(job.get("createdAt").and_then(Value::as_str))?;
                let en = parse_utc_epoch(job.get("updatedAt").and_then(Value::as_str))?;
                if en >= st {
                    Some(((en - st) as f64) * 1000.0)
                } else {
                    None
                }
            })
            .ok_or_else(|| "effect duration is unavailable".to_string())?;
        telemetry_map.insert(
            "effectType".to_string(),
            Value::String("rebalance".to_string()),
        );
        telemetry_map.insert("transferId".to_string(), Value::String(jid));
        telemetry_map.insert("actualBytesTransferred".to_string(), json!(bytes));
        telemetry_map.insert("actualDurationMs".to_string(), json!(dur_ms));
        telemetry_map.insert(
            "trafficClass".to_string(),
            Value::String("P5_REBALANCE_DRAIN".to_string()),
        );
        telemetry_map.insert(
            "source".to_string(),
            Value::String("rebalance-job-ledger".to_string()),
        );
        telemetry_map.insert(
            "sourceDigest".to_string(),
            Value::String(sha256_compact(&job)),
        );
    } else if kind == "drill" {
        let drills = load_drill_items(root).map_err(|e| e.message)?;
        let record = drills
            .into_iter()
            .find(|d| {
                d.get("resilienceActionId").and_then(Value::as_str) == Some(&action_id)
                    || d.get("proof")
                        .and_then(|p| p.get("resilienceActionId"))
                        .and_then(Value::as_str)
                        == Some(&action_id)
            })
            .ok_or_else(|| "DR drill effect is unavailable".to_string())?;
        let drill_id = record
            .get("drillId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let bound_id = handle
            .get("drillId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| drill_id.clone());
        if drill_id.is_empty()
            || bound_id != drill_id
            || record.get("result").and_then(Value::as_str) != Some("success")
        {
            return Err("DR drill effect is not terminal-success".to_string());
        }
        let bytes = record
            .get("bytesRestored")
            .and_then(Value::as_i64)
            .filter(|v| *v >= 0)
            .ok_or_else(|| "invalid bytesRestored".to_string())?;
        let dur_ms = record
            .get("rtoSeconds")
            .and_then(Value::as_f64)
            .map(|s| s * 1000.0)
            .filter(|v| v.is_finite() && *v >= 0.0)
            .unwrap_or(0.0);
        telemetry_map.insert("effectType".to_string(), Value::String("drill".to_string()));
        telemetry_map.insert("transferId".to_string(), Value::String(drill_id));
        telemetry_map.insert("actualBytesTransferred".to_string(), json!(bytes));
        telemetry_map.insert("actualDurationMs".to_string(), json!(dur_ms));
        telemetry_map.insert(
            "trafficClass".to_string(),
            Value::String("P4_SCRUB_DRILL".to_string()),
        );
        telemetry_map.insert(
            "source".to_string(),
            Value::String("dr-drill-ledger".to_string()),
        );
        telemetry_map.insert(
            "sourceDigest".to_string(),
            Value::String(sha256_compact(&record)),
        );
    } else {
        return Err("unsupported Action effect handle".to_string());
    }

    let dig = sha256_compact(&Value::Object(telemetry_map.clone()));
    telemetry_map.insert("telemetryDigest".to_string(), Value::String(dig));
    Ok(Value::Object(telemetry_map))
}

fn reservation_row_to_value(conn: &Connection, action_id: &str) -> Result<Option<Value>, AppError> {
    conn.query_row(
        "SELECT action_id, schedule_id, wave_index, execution_epoch, policy_id, estimated_bytes,
                reserved_units, status, actual_bytes, actual_duration_ms, actual_traffic_class,
                outcome, release_reason, effect_handle_json, transfer_id, telemetry_digest,
                settlement_started_at, created_at, updated_at
         FROM resilience_service_reservations WHERE action_id = ?",
        [action_id],
        |r| {
            let eh_raw: Option<String> = r.get(13)?;
            let eh = eh_raw
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok());
            Ok(json!({
                "actionId": r.get::<_, String>(0)?,
                "scheduleId": r.get::<_, String>(1)?,
                "waveIndex": r.get::<_, i64>(2)?,
                "executionEpoch": r.get::<_, i64>(3)?,
                "policyId": r.get::<_, String>(4)?,
                "estimatedBytes": r.get::<_, i64>(5)?,
                "reservedUnits": r.get::<_, f64>(6)?,
                "status": r.get::<_, String>(7)?,
                "actualBytes": r.get::<_, Option<i64>>(8)?,
                "actualDurationMs": r.get::<_, Option<f64>>(9)?,
                "actualTrafficClass": r.get::<_, Option<String>>(10)?,
                "outcome": r.get::<_, Option<String>>(11)?,
                "releaseReason": r.get::<_, Option<String>>(12)?,
                "effectHandle": eh,
                "transferId": r.get::<_, Option<String>>(14)?,
                "telemetryDigest": r.get::<_, Option<String>>(15)?,
                "settlementStartedAt": r.get::<_, Option<String>>(16)?,
                "createdAt": r.get::<_, String>(17)?,
                "updatedAt": r.get::<_, String>(18)?,
            }))
        },
    )
    .optional()
    .map_err(|_| unhandled_error())
}

fn settle_action_from_effect_db(
    root: &Path,
    action_id: &str,
    action: &Value,
    now_iso: &str,
) -> Result<Value, String> {
    let conn = open_scheduler_rw(root).map_err(|e| e.message)?;
    let existing = reservation_row_to_value(&conn, action_id)
        .map_err(|e| e.message)?
        .ok_or_else(|| {
            format!("fair-service reservation is unavailable for Action '{action_id}'")
        })?;
    let st = existing
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(st, "CONSUMED" | "RELEASED" | "EXPIRED") {
        return Ok(existing);
    }
    let state = action
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if state != "SUCCEEDED" {
        if JOURNAL_FAILURE_STATES.contains(&state) {
            drop(conn);
            release_action_reservation_db(root, action_id, "FAILED", now_iso)
                .map_err(|e| e.message)?;
            let conn2 = open_scheduler_rw(root).map_err(|e| e.message)?;
            return reservation_row_to_value(&conn2, action_id)
                .map_err(|e| e.message)?
                .ok_or_else(|| "reservation missing".to_string());
        }
        return Err(format!("Action '{action_id}' is not terminal"));
    }

    let telemetry = read_terminal_effect_telemetry(root, action)?;
    let eh = telemetry
        .get("effectHandle")
        .filter(|h| h.is_object())
        .ok_or_else(|| "terminal telemetry effect handle is required".to_string())?;
    let eh_rendered = dumps_compact(eh);
    let transfer_id = telemetry
        .get("transferId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let telemetry_digest = telemetry
        .get("telemetryDigest")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let t_epoch = telemetry
        .get("actionExecutionEpoch")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let outcome = telemetry
        .get("outcome")
        .and_then(Value::as_str)
        .unwrap_or("SUCCEEDED");
    let byte_count = telemetry
        .get("actualBytesTransferred")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .max(0);
    let dur_ms = telemetry
        .get("actualDurationMs")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0);
    let tc = telemetry
        .get("trafficClass")
        .and_then(Value::as_str)
        .unwrap_or_default();

    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|e| e.to_string())?;
    let cur = reservation_row_to_value(&conn, action_id)
        .map_err(|e| e.message)?
        .ok_or_else(|| {
            format!("fair-service reservation is unavailable for Action '{action_id}'")
        })?;
    let cur_st = cur
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if cur_st == "CONSUMED" {
        let _ = conn.execute_batch("COMMIT;");
        return Ok(cur);
    }
    if cur_st == "RESERVED" {
        let upd = conn
            .execute(
                "UPDATE resilience_service_reservations
                 SET status = 'CONSUMING', execution_epoch = ?, effect_handle_json = ?,
                     transfer_id = ?, telemetry_digest = ?, settlement_started_at = ?,
                     outcome = ?, updated_at = ?
                 WHERE action_id = ? AND status = 'RESERVED'",
                rusqlite::params![
                    t_epoch,
                    eh_rendered,
                    transfer_id,
                    telemetry_digest,
                    now_iso,
                    outcome,
                    now_iso,
                    action_id,
                ],
            )
            .map_err(|e| e.to_string())?;
        if upd != 1 {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err("fair-service reservation settlement claim conflicted".to_string());
        }
        conn.execute(
            "INSERT INTO resilience_service_settlement_events (
                action_id, from_status, to_status, execution_epoch,
                effect_handle_json, transfer_id, telemetry_digest, created_at
             ) VALUES (?, 'RESERVED', 'CONSUMING', ?, ?, ?, ?, ?)",
            rusqlite::params![
                action_id,
                t_epoch,
                eh_rendered,
                transfer_id,
                telemetry_digest,
                now_iso,
            ],
        )
        .map_err(|e| e.to_string())?;
    } else if !matches!(cur_st, "CONSUMING" | "RECONCILING") {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err(format!(
            "fair-service reservation cannot settle from state '{cur_st}'"
        ));
    }

    let policy_id = cur
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or("fleet-global");
    let service_units = ((byte_count.max(1) as f64) / (1024.0 * 1024.0 * 1024.0)).max(0.0001);
    let inserted = conn
        .execute(
            "INSERT OR IGNORE INTO resilience_scheduler_service_events (
                action_id, policy_id, service_units, bytes_served, scheduled_at
             ) VALUES (?, ?, ?, ?, ?)",
            rusqlite::params![action_id, policy_id, service_units, byte_count, now_iso],
        )
        .map_err(|e| e.to_string())?;
    if inserted == 1 {
        let cur_srv: Option<(f64, f64, i64, i64)> = conn
            .query_row(
                "SELECT virtual_runtime, virtual_finish, actions_served, bytes_served
                 FROM resilience_scheduler_service WHERE policy_id = ?",
                [policy_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        let (vr, vf, served, b_served) = cur_srv.unwrap_or((0.0, 0.0, 0, 0));
        conn.execute(
            "INSERT INTO resilience_scheduler_service (
                policy_id, virtual_runtime, virtual_finish, actions_served,
                bytes_served, last_scheduled_at
             ) VALUES (?, ?, ?, ?, ?, ?)
             ON CONFLICT(policy_id) DO UPDATE SET
                virtual_runtime = excluded.virtual_runtime,
                virtual_finish = excluded.virtual_finish,
                actions_served = excluded.actions_served,
                bytes_served = excluded.bytes_served,
                last_scheduled_at = excluded.last_scheduled_at",
            rusqlite::params![
                policy_id,
                vr + service_units,
                vf.max(vr) + service_units,
                served + 1,
                b_served + byte_count,
                now_iso,
            ],
        )
        .map_err(|e| e.to_string())?;
    }

    let term_upd = conn
        .execute(
            "UPDATE resilience_service_reservations
             SET status = 'CONSUMED', actual_bytes = ?, actual_duration_ms = ?,
                 actual_traffic_class = ?, outcome = ?, updated_at = ?
             WHERE action_id = ? AND status = 'CONSUMING'
               AND execution_epoch = ? AND telemetry_digest = ?",
            rusqlite::params![
                byte_count,
                dur_ms,
                tc,
                outcome,
                now_iso,
                action_id,
                t_epoch,
                telemetry_digest,
            ],
        )
        .map_err(|e| e.to_string())?;
    if term_upd != 1 {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err("fair-service terminal settlement conflicted".to_string());
    }
    conn.execute(
        "INSERT INTO resilience_service_settlement_events (
            action_id, from_status, to_status, execution_epoch,
            effect_handle_json, transfer_id, telemetry_digest, created_at
         ) VALUES (?, 'CONSUMING', 'CONSUMED', ?, ?, ?, ?, ?)",
        rusqlite::params![
            action_id,
            t_epoch,
            eh_rendered,
            transfer_id,
            telemetry_digest,
            now_iso,
        ],
    )
    .map_err(|e| e.to_string())?;
    conn.execute_batch("COMMIT;").map_err(|e| e.to_string())?;

    reservation_row_to_value(&conn, action_id)
        .map_err(|e| e.message)?
        .ok_or_else(|| "reservation missing after settlement".to_string())
}

pub(crate) fn admit_wave_db(
    root: &Path,
    schedule_id: &str,
    wave_index_opt: Option<i64>,
) -> Result<Value, AppError> {
    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let conn = open_waves_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;

    let mut chosen_wave_idx: Option<i64> = wave_index_opt;
    let prep_res: Result<(Option<Value>, Option<(Value, Vec<Value>)>), AppError> = (|| {
        let Some(sched) = schedule_record_from_conn(&conn, schedule_id)? else {
            return Err(unhandled_error());
        };
        let sched_st = sched
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if matches!(
            sched_st,
            "FAILED" | "SUPERSEDED" | "PAUSED_REPLAN" | "STALE"
        ) {
            let mut out = sched;
            out["admitted"] = Value::Bool(false);
            out["reason"] = Value::String(sched_st.to_string());
            return Ok((Some(out), None));
        }
        let target_idx = if let Some(wi) = chosen_wave_idx {
            wi
        } else {
            let pending: Option<i64> = conn
                .query_row(
                    "SELECT wave_index FROM resilience_wave_states
                     WHERE schedule_id = ? AND status = 'PENDING'
                     ORDER BY wave_index LIMIT 1",
                    [schedule_id],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|_| unhandled_error())?;
            let Some(wi) = pending else {
                let mut out = sched;
                out["admitted"] = Value::Bool(false);
                out["reason"] = Value::String("NO_PENDING_WAVE".to_string());
                return Ok((Some(out), None));
            };
            wi
        };
        chosen_wave_idx = Some(target_idx);

        let Some(wave) = wave_record_from_conn(&conn, schedule_id, target_idx)? else {
            return Err(unhandled_error());
        };
        let wave_st = wave
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if wave_st != "PENDING" {
            let mut out = wave;
            out["admitted"] = Value::Bool(false);
            out["reason"] = Value::String(wave_st.to_string());
            return Ok((Some(out), None));
        }
        if !predecessors_verified(&conn, schedule_id, target_idx)? {
            let mut out = wave;
            out["admitted"] = Value::Bool(false);
            out["reason"] = Value::String("PREDECESSOR_WAVE_NOT_VERIFIED".to_string());
            return Ok((Some(out), None));
        }

        let mut act_stmt = conn
            .prepare(
                "SELECT action_json FROM resilience_wave_actions
                 WHERE schedule_id = ? AND wave_index = ? ORDER BY action_id",
            )
            .map_err(|_| unhandled_error())?;
        let wave_actions: Vec<Value> = act_stmt
            .query_map(rusqlite::params![schedule_id, target_idx], |r| {
                let raw: String = r.get(0)?;
                Ok(serde_json::from_str::<Value>(&raw).unwrap_or_else(|_| json!({})))
            })
            .map_err(|_| unhandled_error())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| unhandled_error())?;
        drop(act_stmt);

        let claimed = conn
            .execute(
                "UPDATE resilience_wave_states SET status = 'ADMITTING', updated_at = ?
                 WHERE schedule_id = ? AND wave_index = ? AND status = 'PENDING'",
                rusqlite::params![now_iso, schedule_id, target_idx],
            )
            .map_err(|_| unhandled_error())?;
        if claimed != 1 {
            let mut cur =
                wave_record_from_conn(&conn, schedule_id, target_idx)?.unwrap_or_else(|| json!({}));
            let reason = cur
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("CONFLICT")
                .to_string();
            cur["admitted"] = Value::Bool(false);
            cur["reason"] = Value::String(reason);
            return Ok((Some(cur), None));
        }
        Ok((None, Some((sched, wave_actions))))
    })();

    let (early_opt, claimed_ctx) = match prep_res {
        Ok(res) => res,
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(err);
        }
    };
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;
    drop(conn);

    if let Some(early) = early_opt {
        return Ok(early);
    }
    let Some((schedule_payload, wave_actions)) = claimed_ctx else {
        return Err(unhandled_error());
    };
    let wave_index = chosen_wave_idx.unwrap_or(0);

    let bundle_res = build_fresh_state_bundle_readonly(root, &wave_actions, now_secs, &now_iso);
    let fresh_bundle = match bundle_res {
        Ok(b) => b,
        Err(reason_raw) => {
            let code_str = reason_raw.split(':').next().unwrap_or(&reason_raw).trim();
            let detail = reason_raw
                .split_once(':')
                .map(|(_, d)| d.trim())
                .unwrap_or("");
            let revalidation = json!({
                "fresh": false,
                "reasons": [code_str],
                "sourceUnavailable": detail,
                "detail": detail,
            });
            let conn2 = open_waves_rw(root)?;
            let _ = conn2.execute(
                "UPDATE resilience_wave_states
                 SET status = 'PENDING', revalidation_json = ?, updated_at = ?
                 WHERE schedule_id = ? AND wave_index = ? AND status = 'ADMITTING'",
                rusqlite::params![
                    dumps_compact(&revalidation),
                    now_iso,
                    schedule_id,
                    wave_index
                ],
            );
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "admitted": false,
                "status": "WAVE_NOT_ADMITTED",
                "reason": code_str,
                "revalidation": revalidation,
            }));
        }
    };

    let revalidation = revalidate_wave(&schedule_payload, &fresh_bundle);
    let rendered_rev = dumps_compact(&revalidation);
    let is_fresh = revalidation.get("fresh") == Some(&Value::Bool(true));

    let conn2 = open_waves_rw(root)?;
    conn2
        .execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;
    let Some(cur_wave) = wave_record_from_conn(&conn2, schedule_id, wave_index)? else {
        let _ = conn2.execute_batch("ROLLBACK;");
        return Err(unhandled_error());
    };
    if cur_wave.get("status").and_then(Value::as_str) != Some("ADMITTING") {
        let _ = conn2.execute_batch("COMMIT;");
        let mut out = cur_wave.clone();
        let r = cur_wave
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        out["admitted"] = Value::Bool(false);
        out["reason"] = Value::String(r);
        return Ok(out);
    }

    if !is_fresh {
        let reasons_joined = revalidation
            .get("reasons")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(value_str).collect::<Vec<_>>().join(","))
            .unwrap_or_default();
        conn2
            .execute(
                "UPDATE resilience_wave_schedules SET status = 'PAUSED_REPLAN', stale_reason = ?, updated_at = ? WHERE schedule_id = ?",
                rusqlite::params![reasons_joined, now_iso, schedule_id],
            )
            .map_err(|_| unhandled_error())?;
        conn2
            .execute(
                "UPDATE resilience_wave_states SET status = 'PENDING', revalidation_json = ?, updated_at = ? WHERE schedule_id = ? AND wave_index = ?",
                rusqlite::params![rendered_rev, now_iso, schedule_id, wave_index],
            )
            .map_err(|_| unhandled_error())?;
        conn2
            .execute_batch("COMMIT;")
            .map_err(|_| unhandled_error())?;
        drop(conn2);
        release_schedule_reservations_db(root, schedule_id, "STALE", &now_iso)?;
        return Ok(json!({
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "admitted": false,
            "status": "PAUSED_REPLAN",
            "reason": "STALE",
            "revalidation": revalidation,
        }));
    }

    conn2
        .execute(
            "UPDATE resilience_wave_states
             SET status = 'CLAIMING', revalidation_json = ?, admitted_at = ?, updated_at = ?
             WHERE schedule_id = ? AND wave_index = ?",
            rusqlite::params![rendered_rev, now_iso, now_iso, schedule_id, wave_index],
        )
        .map_err(|_| unhandled_error())?;
    conn2
        .execute(
            "UPDATE resilience_wave_schedules SET status = 'RUNNING', updated_at = ? WHERE schedule_id = ?",
            rusqlite::params![now_iso, schedule_id],
        )
        .map_err(|_| unhandled_error())?;
    let mut admitted_wave =
        wave_record_from_conn(&conn2, schedule_id, wave_index)?.ok_or_else(unhandled_error)?;
    conn2
        .execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;
    admitted_wave["admitted"] = Value::Bool(true);
    admitted_wave["revalidation"] = revalidation;
    Ok(admitted_wave)
}

fn set_wave_state_with_fence(
    root: &Path,
    schedule_id: &str,
    wave_index: i64,
    status: &str,
    sched_epoch: i64,
    wave_epoch: i64,
    runner_token: &str,
) -> Result<bool, AppError> {
    let (_, now_iso) = now_unix_secs_and_iso();
    let conn = open_waves_rw(root)?;
    let upd = conn
        .execute(
            "UPDATE resilience_wave_states
             SET status = ?, updated_at = ?
             WHERE schedule_id = ? AND wave_index = ?
               AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?
               AND EXISTS (
                   SELECT 1 FROM resilience_wave_schedules AS schedule
                   WHERE schedule.schedule_id = resilience_wave_states.schedule_id
                     AND schedule.execution_epoch = ?
                     AND schedule.runner_token = ?
                     AND schedule.lease_until >= ?
               )",
            rusqlite::params![
                status,
                now_iso,
                schedule_id,
                wave_index,
                wave_epoch,
                runner_token,
                now_iso,
                sched_epoch,
                runner_token,
                now_iso,
            ],
        )
        .map_err(|_| unhandled_error())?;
    Ok(upd == 1)
}

#[allow(clippy::too_many_arguments)]
fn set_action_state_with_fence(
    root: &Path,
    schedule_id: &str,
    wave_index: i64,
    action_id: &str,
    status: &str,
    outcome: Option<&str>,
    action_epoch: i64,
    sched_epoch: i64,
    wave_epoch: i64,
    runner_token: &str,
    journal_action: Option<&Value>,
    terminal: bool,
) -> Result<bool, AppError> {
    let (_, now_iso) = now_unix_secs_and_iso();
    let journal_epoch = journal_action
        .and_then(|a| a.get("executionEpoch"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let rendered_effect = journal_action
        .and_then(|a| a.get("effectHandle"))
        .filter(|h| h.is_object())
        .map(dumps_compact);
    let rendered_term = journal_action.map(dumps_compact);
    let term_flag: i64 = if terminal { 1 } else { 0 };

    let conn = open_waves_rw(root)?;
    let upd = conn
        .execute(
            "UPDATE resilience_wave_actions
             SET status = ?,
                 outcome = COALESCE(?, outcome),
                 journal_execution_epoch = CASE WHEN ? > 0 THEN ? ELSE journal_execution_epoch END,
                 effect_handle_json = COALESCE(?, effect_handle_json),
                 terminal_json = COALESCE(?, terminal_json),
                 owner_instance_id = CASE WHEN ? != 0 THEN NULL ELSE owner_instance_id END,
                 lease_until = CASE WHEN ? != 0 THEN NULL ELSE lease_until END,
                 runner_token = CASE WHEN ? != 0 THEN NULL ELSE runner_token END,
                 updated_at = ?
             WHERE schedule_id = ? AND wave_index = ? AND action_id = ?
               AND execution_epoch = ? AND schedule_execution_epoch = ?
               AND wave_execution_epoch = ? AND runner_token = ?
               AND EXISTS (
                   SELECT 1 FROM resilience_wave_schedules AS schedule
                   WHERE schedule.schedule_id = resilience_wave_actions.schedule_id
                     AND schedule.execution_epoch = ?
                     AND schedule.runner_token = ?
                     AND schedule.lease_until >= ?
               )
               AND EXISTS (
                   SELECT 1 FROM resilience_wave_states AS wave
                   WHERE wave.schedule_id = resilience_wave_actions.schedule_id
                     AND wave.wave_index = resilience_wave_actions.wave_index
                     AND wave.execution_epoch = ?
                     AND wave.runner_token = ?
                     AND wave.lease_until >= ?
               )",
            rusqlite::params![
                status,
                outcome,
                journal_epoch,
                journal_epoch,
                rendered_effect,
                rendered_term,
                term_flag,
                term_flag,
                term_flag,
                now_iso,
                schedule_id,
                wave_index,
                action_id,
                action_epoch,
                sched_epoch,
                wave_epoch,
                runner_token,
                sched_epoch,
                runner_token,
                now_iso,
                wave_epoch,
                runner_token,
                now_iso,
            ],
        )
        .map_err(|_| unhandled_error())?;
    Ok(upd == 1)
}

#[allow(clippy::too_many_arguments)]
fn fail_action_with_fence(
    root: &Path,
    schedule_id: &str,
    wave_index: i64,
    action_id: &str,
    journal_action: &Value,
    action_epoch: i64,
    sched_epoch: i64,
    wave_epoch: i64,
    runner_token: &str,
) -> Result<bool, AppError> {
    let term_state = journal_action
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("FAILED");
    let ok = set_action_state_with_fence(
        root,
        schedule_id,
        wave_index,
        action_id,
        "FAILED",
        Some(term_state),
        action_epoch,
        sched_epoch,
        wave_epoch,
        runner_token,
        Some(journal_action),
        true,
    )?;
    if !ok {
        return Ok(false);
    }
    let (_, now_iso) = now_unix_secs_and_iso();
    let conn = open_waves_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;
    let w_upd = conn
        .execute(
            "UPDATE resilience_wave_states
             SET status = 'VERIFYING', owner_instance_id = NULL, lease_until = NULL, runner_token = NULL, updated_at = ?
             WHERE schedule_id = ? AND wave_index = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
            rusqlite::params![now_iso, schedule_id, wave_index, wave_epoch, runner_token, now_iso],
        )
        .map_err(|_| unhandled_error())?;
    let s_upd = conn
        .execute(
            "UPDATE resilience_wave_schedules
             SET status = 'FAILED', owner_instance_id = NULL, lease_until = NULL, runner_token = NULL, updated_at = ?
             WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
            rusqlite::params![now_iso, schedule_id, sched_epoch, runner_token, now_iso],
        )
        .map_err(|_| unhandled_error())?;
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;
    Ok(w_upd == 1 && s_upd == 1)
}

fn journal_success_is_verified(action: &Value) -> bool {
    action.get("state").and_then(Value::as_str) == Some("SUCCEEDED")
        && action
            .get("verificationResult")
            .is_some_and(Value::is_object)
}

pub(crate) fn run_next_wave_db(
    root: &Path,
    schedule_id: &str,
    instance_id: &str,
    lease_seconds: u64,
) -> Result<Value, AppError> {
    let conn = open_waves_rw(root)?;
    let Some(sched) = schedule_record_from_conn(&conn, schedule_id)? else {
        return Err(unhandled_error());
    };
    let sched_st = sched
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(
        sched_st,
        "COMPLETED" | "FAILED" | "SUPERSEDED" | "PAUSED_REPLAN" | "STALE"
    ) {
        let mut out = sched;
        out["ran"] = Value::Bool(false);
        out["reason"] = Value::String(sched_st.to_string());
        return Ok(out);
    }

    let active_idx: Option<i64> = conn
        .query_row(
            "SELECT wave_index FROM resilience_wave_states
             WHERE schedule_id = ? AND status IN ('CLAIMING', 'RUNNING', 'EXECUTING', 'VERIFYING')
             ORDER BY wave_index LIMIT 1",
            [schedule_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    let wave_index = if let Some(ai) = active_idx {
        ai
    } else {
        let pend: Option<i64> = conn
            .query_row(
                "SELECT wave_index FROM resilience_wave_states
                 WHERE schedule_id = ? AND status = 'PENDING'
                 ORDER BY wave_index LIMIT 1",
                [schedule_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let Some(pi) = pend else {
            let mut out = sched;
            out["ran"] = Value::Bool(false);
            out["reason"] = Value::String("NO_RUNNABLE_WAVE".to_string());
            return Ok(out);
        };
        pi
    };
    drop(conn);

    if active_idx.is_none() {
        let admission = admit_wave_db(root, schedule_id, Some(wave_index))?;
        if admission.get("admitted") != Some(&Value::Bool(true)) {
            return Ok(admission);
        }
    }

    let (now_secs, now_iso) = now_unix_secs_and_iso();
    let lease_until = utc_iso_from_unix(now_secs + lease_seconds);
    let runner_token = new_hex_id()[..32].to_string();

    let conn = open_waves_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;

    let claim_res: Result<Result<(i64, i64), Value>, AppError> = (|| {
        let s_row: Option<(String, Option<String>, Option<String>)> = conn
            .query_row(
                "SELECT status, runner_token, lease_until FROM resilience_wave_schedules WHERE schedule_id = ?",
                [schedule_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let Some((s_st, s_tok, s_lease)) = s_row else {
            return Err(unhandled_error());
        };
        if matches!(
            s_st.as_str(),
            "COMPLETED" | "FAILED" | "SUPERSEDED" | "PAUSED_REPLAN" | "STALE"
        ) {
            return Ok(Err(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "WAVE_NOT_CLAIMED",
                "reason": s_st,
            })));
        }
        if s_tok.is_some_and(|t| !t.is_empty())
            && s_lease.is_some_and(|l| l.as_str() >= now_iso.as_str())
        {
            return Ok(Err(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "WAVE_NOT_CLAIMED",
                "reason": "SCHEDULE_RUNNER_LEASE_HELD",
            })));
        }

        let w_row: Option<(String, Option<String>, Option<String>)> = conn
            .query_row(
                "SELECT status, runner_token, lease_until FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
                rusqlite::params![schedule_id, wave_index],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let Some((w_st, w_tok, w_lease)) = w_row else {
            return Err(unhandled_error());
        };
        if !matches!(
            w_st.as_str(),
            "CLAIMING" | "RUNNING" | "EXECUTING" | "VERIFYING"
        ) {
            return Ok(Err(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "WAVE_NOT_CLAIMED",
                "reason": w_st,
            })));
        }
        if w_tok.is_some_and(|t| !t.is_empty())
            && w_lease.is_some_and(|l| l.as_str() >= now_iso.as_str())
        {
            return Ok(Err(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "WAVE_NOT_CLAIMED",
                "reason": "WAVE_RUNNER_LEASE_HELD",
            })));
        }

        conn.execute(
            "UPDATE resilience_wave_schedules
             SET status = 'RUNNING', execution_epoch = execution_epoch + 1,
                 owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
             WHERE schedule_id = ?",
            rusqlite::params![instance_id, lease_until, runner_token, now_iso, schedule_id],
        )
        .map_err(|_| unhandled_error())?;
        conn.execute(
            "UPDATE resilience_wave_states
             SET status = 'CLAIMING', execution_epoch = execution_epoch + 1,
                 owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
             WHERE schedule_id = ? AND wave_index = ?",
            rusqlite::params![
                instance_id,
                lease_until,
                runner_token,
                now_iso,
                schedule_id,
                wave_index,
            ],
        )
        .map_err(|_| unhandled_error())?;
        let se: i64 = conn
            .query_row(
                "SELECT execution_epoch FROM resilience_wave_schedules WHERE schedule_id = ?",
                [schedule_id],
                |r| r.get(0),
            )
            .map_err(|_| unhandled_error())?;
        let we: i64 = conn
            .query_row(
                "SELECT execution_epoch FROM resilience_wave_states WHERE schedule_id = ? AND wave_index = ?",
                rusqlite::params![schedule_id, wave_index],
                |r| r.get(0),
            )
            .map_err(|_| unhandled_error())?;
        Ok(Ok((se, we)))
    })();

    let (sched_epoch, wave_epoch) = match claim_res {
        Ok(Ok(ep)) => ep,
        Ok(Err(rej)) => {
            let _ = conn.execute_batch("ROLLBACK;");
            return Ok(rej);
        }
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(err);
        }
    };
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;

    let schedule_rec =
        schedule_record_from_conn(&conn, schedule_id)?.ok_or_else(unhandled_error)?;
    let mut stmt = conn
        .prepare(
            "SELECT action_id, status, action_json FROM resilience_wave_actions
             WHERE schedule_id = ? AND wave_index = ? ORDER BY action_id",
        )
        .map_err(|_| unhandled_error())?;
    let action_rows: Vec<(String, String, String)> = stmt
        .query_map(rusqlite::params![schedule_id, wave_index], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .map_err(|_| unhandled_error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unhandled_error())?;
    drop(stmt);
    drop(conn);

    if !set_wave_state_with_fence(
        root,
        schedule_id,
        wave_index,
        "EXECUTING",
        sched_epoch,
        wave_epoch,
        &runner_token,
    )? {
        return Ok(json!({
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": false,
            "status": "RUNNER_FENCED_OUT",
        }));
    }

    let risk_digest = schedule_rec
        .get("riskDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let plan_digest = schedule_rec
        .get("schedule")
        .and_then(|s| s.get("planDigest"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let mut action_results = Vec::new();

    for (action_id, wa_status, action_raw) in action_rows {
        if wa_status == "VERIFIED_SUCCESS" {
            continue;
        }
        let (ck_secs, ck_iso) = now_unix_secs_and_iso();
        let ck_lease = utc_iso_from_unix(ck_secs + lease_seconds);
        let c_wave = open_waves_rw(root)?;
        c_wave
            .execute_batch("BEGIN IMMEDIATE;")
            .map_err(|_| unhandled_error())?;
        let s_ren = c_wave
            .execute(
                "UPDATE resilience_wave_schedules SET lease_until = ?, updated_at = ?
                 WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
                rusqlite::params![ck_lease, ck_iso, schedule_id, sched_epoch, runner_token, ck_iso],
            )
            .map_err(|_| unhandled_error())?;
        let w_ren = c_wave
            .execute(
                "UPDATE resilience_wave_states SET lease_until = ?, updated_at = ?
                 WHERE schedule_id = ? AND wave_index = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
                rusqlite::params![ck_lease, ck_iso, schedule_id, wave_index, wave_epoch, runner_token, ck_iso],
            )
            .map_err(|_| unhandled_error())?;
        if s_ren != 1 || w_ren != 1 {
            let _ = c_wave.execute_batch("ROLLBACK;");
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "RUNNER_FENCED_OUT",
                "actionId": action_id,
                "reason": "RUNNER_LEASE_HEARTBEAT_LOST",
            }));
        }

        let a_row: Option<(String, Option<String>, Option<String>)> = c_wave
            .query_row(
                "SELECT status, runner_token, lease_until FROM resilience_wave_actions
                 WHERE schedule_id = ? AND wave_index = ? AND action_id = ?",
                rusqlite::params![schedule_id, wave_index, action_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let Some((a_st, a_tok, a_lease)) = a_row else {
            let _ = c_wave.execute_batch("ROLLBACK;");
            return Err(unhandled_error());
        };
        if matches!(
            a_st.as_str(),
            "VERIFIED_SUCCESS" | "FAILED" | "PREEMPTED" | "STALE"
        ) {
            let _ = c_wave.execute_batch("COMMIT;");
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "ACTION_NOT_CLAIMED",
                "actionId": action_id,
                "reason": a_st,
            }));
        }
        if matches!(
            a_st.as_str(),
            "CLAIMED" | "EXECUTING" | "OUTCOME_VERIFICATION"
        ) && a_tok.is_some_and(|t| !t.is_empty())
            && a_lease.is_some_and(|l| l.as_str() >= ck_iso.as_str())
        {
            let _ = c_wave.execute_batch("ROLLBACK;");
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "ACTION_NOT_CLAIMED",
                "actionId": action_id,
                "reason": "ACTION_RUNNER_LEASE_HELD",
            }));
        }
        let a_upd = c_wave
            .execute(
                "UPDATE resilience_wave_actions
                 SET status = 'CLAIMED', execution_epoch = execution_epoch + 1,
                     schedule_execution_epoch = ?, wave_execution_epoch = ?,
                     owner_instance_id = ?, lease_until = ?, runner_token = ?, updated_at = ?
                 WHERE schedule_id = ? AND wave_index = ? AND action_id = ?
                   AND status IN ('PENDING', 'CLAIMED', 'EXECUTING', 'OUTCOME_VERIFICATION')",
                rusqlite::params![
                    sched_epoch,
                    wave_epoch,
                    instance_id,
                    ck_lease,
                    runner_token,
                    ck_iso,
                    schedule_id,
                    wave_index,
                    action_id,
                ],
            )
            .map_err(|_| unhandled_error())?;
        if a_upd != 1 {
            let _ = c_wave.execute_batch("ROLLBACK;");
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "ACTION_NOT_CLAIMED",
                "actionId": action_id,
                "reason": "ACTION_CLAIM_CONFLICT",
            }));
        }
        let action_epoch: i64 = c_wave
            .query_row(
                "SELECT execution_epoch FROM resilience_wave_actions WHERE schedule_id = ? AND action_id = ?",
                rusqlite::params![schedule_id, action_id],
                |r| r.get(0),
            )
            .map_err(|_| unhandled_error())?;
        c_wave
            .execute_batch("COMMIT;")
            .map_err(|_| unhandled_error())?;
        drop(c_wave);

        let action_payload: Value = serde_json::from_str(&action_raw).unwrap_or_else(|_| json!({}));
        let journal_action = match record_action_intent_db(
            root,
            &action_payload,
            "resilience-wave-runner",
            Some(schedule_id),
            &risk_digest,
            &plan_digest,
            &ck_iso,
        ) {
            Ok(j) => j,
            Err(_) => {
                return Ok(json!({
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": false,
                    "status": "ACTION_JOURNAL_UNAVAILABLE",
                    "actionId": action_id,
                    "reason": "AppError",
                }));
            }
        };

        let j_state = journal_action
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let terminal_action = if journal_success_is_verified(&journal_action) {
            journal_action
        } else if JOURNAL_FAILURE_STATES.contains(&j_state.as_str()) {
            if !fail_action_with_fence(
                root,
                schedule_id,
                wave_index,
                &action_id,
                &journal_action,
                action_epoch,
                sched_epoch,
                wave_epoch,
                &runner_token,
            )? {
                return Ok(json!({
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": false,
                    "status": "RUNNER_FENCED_OUT",
                    "actionId": action_id,
                }));
            }
            let (_, fail_iso) = now_unix_secs_and_iso();
            release_action_reservation_db(root, &action_id, "FAILED", &fail_iso)?;
            release_schedule_reservations_db(root, schedule_id, "FAILED", &fail_iso)?;
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": true,
                "status": "FAILED",
                "actionId": action_id,
                "outcome": j_state,
            }));
        } else {
            let j_lease = journal_action
                .get("leaseUntil")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if JOURNAL_ACTIVE_STATES.contains(&j_state.as_str()) && j_lease >= ck_iso.as_str() {
                if !set_action_state_with_fence(
                    root,
                    schedule_id,
                    wave_index,
                    &action_id,
                    "EXECUTING",
                    None,
                    action_epoch,
                    sched_epoch,
                    wave_epoch,
                    &runner_token,
                    Some(&journal_action),
                    false,
                )? {
                    return Ok(json!({
                        "scheduleId": schedule_id,
                        "waveIndex": wave_index,
                        "ran": false,
                        "status": "RUNNER_FENCED_OUT",
                        "actionId": action_id,
                    }));
                }
                return Ok(json!({
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": false,
                    "status": "ACTION_EFFECT_IN_PROGRESS",
                    "actionId": action_id,
                }));
            }
            if !set_action_state_with_fence(
                root,
                schedule_id,
                wave_index,
                &action_id,
                "EXECUTING",
                None,
                action_epoch,
                sched_epoch,
                wave_epoch,
                &runner_token,
                Some(&journal_action),
                false,
            )? {
                return Ok(json!({
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": false,
                    "status": "RUNNER_FENCED_OUT",
                    "actionId": action_id,
                }));
            }
            match execute_autonomous_action_db(root, &action_id, instance_id, lease_seconds) {
                Ok(term) => term,
                Err(_) => {
                    let j_conn = open_journal_rw(root)?;
                    let obs =
                        get_action_from_conn(&j_conn, &action_id)?.unwrap_or_else(|| json!({}));
                    drop(j_conn);
                    if journal_success_is_verified(&obs) {
                        obs
                    } else {
                        let obs_st = obs.get("state").and_then(Value::as_str).unwrap_or_default();
                        if JOURNAL_FAILURE_STATES.contains(&obs_st) {
                            if !fail_action_with_fence(
                                root,
                                schedule_id,
                                wave_index,
                                &action_id,
                                &obs,
                                action_epoch,
                                sched_epoch,
                                wave_epoch,
                                &runner_token,
                            )? {
                                return Ok(json!({
                                    "scheduleId": schedule_id,
                                    "waveIndex": wave_index,
                                    "ran": false,
                                    "status": "RUNNER_FENCED_OUT",
                                    "actionId": action_id,
                                }));
                            }
                            let (_, fail_iso) = now_unix_secs_and_iso();
                            release_action_reservation_db(root, &action_id, "FAILED", &fail_iso)?;
                            release_schedule_reservations_db(
                                root,
                                schedule_id,
                                "FAILED",
                                &fail_iso,
                            )?;
                            return Ok(json!({
                                "scheduleId": schedule_id,
                                "waveIndex": wave_index,
                                "ran": true,
                                "status": "FAILED",
                                "actionId": action_id,
                                "outcome": obs_st,
                            }));
                        }
                        return Ok(json!({
                            "scheduleId": schedule_id,
                            "waveIndex": wave_index,
                            "ran": false,
                            "status": "ACTION_EXECUTION_RETRY_REQUIRED",
                            "actionId": action_id,
                            "reason": "AppError",
                        }));
                    }
                }
            }
        };

        if !journal_success_is_verified(&terminal_action) {
            if !fail_action_with_fence(
                root,
                schedule_id,
                wave_index,
                &action_id,
                &terminal_action,
                action_epoch,
                sched_epoch,
                wave_epoch,
                &runner_token,
            )? {
                return Ok(json!({
                    "scheduleId": schedule_id,
                    "waveIndex": wave_index,
                    "ran": false,
                    "status": "RUNNER_FENCED_OUT",
                    "actionId": action_id,
                }));
            }
            let (_, fail_iso) = now_unix_secs_and_iso();
            release_action_reservation_db(root, &action_id, "FAILED", &fail_iso)?;
            release_schedule_reservations_db(root, schedule_id, "FAILED", &fail_iso)?;
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": true,
                "status": "FAILED",
                "actionId": action_id,
                "outcome": terminal_action.get("state").cloned().unwrap_or(Value::Null),
            }));
        }

        if !set_action_state_with_fence(
            root,
            schedule_id,
            wave_index,
            &action_id,
            "OUTCOME_VERIFICATION",
            None,
            action_epoch,
            sched_epoch,
            wave_epoch,
            &runner_token,
            Some(&terminal_action),
            false,
        )? {
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "RUNNER_FENCED_OUT",
                "actionId": action_id,
            }));
        }

        let (_, settle_iso) = now_unix_secs_and_iso();
        let settlement =
            match settle_action_from_effect_db(root, &action_id, &terminal_action, &settle_iso) {
                Ok(s) if s.get("status").and_then(Value::as_str) == Some("CONSUMED") => s,
                Ok(_) | Err(_) => {
                    return Ok(json!({
                        "scheduleId": schedule_id,
                        "waveIndex": wave_index,
                        "ran": false,
                        "status": "FAIR_SERVICE_SETTLEMENT_REQUIRED",
                        "actionId": action_id,
                    }));
                }
            };

        if !set_action_state_with_fence(
            root,
            schedule_id,
            wave_index,
            &action_id,
            "VERIFIED_SUCCESS",
            Some("SUCCEEDED"),
            action_epoch,
            sched_epoch,
            wave_epoch,
            &runner_token,
            Some(&terminal_action),
            true,
        )? {
            return Ok(json!({
                "scheduleId": schedule_id,
                "waveIndex": wave_index,
                "ran": false,
                "status": "RUNNER_FENCED_OUT",
                "actionId": action_id,
            }));
        }

        action_results.push(json!({
            "actionId": action_id,
            "status": "VERIFIED_SUCCESS",
            "journalExecutionEpoch": terminal_action.get("executionEpoch").and_then(Value::as_i64).unwrap_or(0),
            "effectHandle": terminal_action.get("effectHandle").cloned().unwrap_or(Value::Null),
            "fairServiceSettlement": settlement,
        }));
    }

    if !set_wave_state_with_fence(
        root,
        schedule_id,
        wave_index,
        "VERIFYING",
        sched_epoch,
        wave_epoch,
        &runner_token,
    )? {
        return Ok(json!({
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": false,
            "status": "RUNNER_FENCED_OUT",
        }));
    }

    let (_, comp_iso) = now_unix_secs_and_iso();
    let conn_end = open_waves_rw(root)?;
    conn_end
        .execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;
    let comp_ok: Result<bool, AppError> = (|| {
        let s_own: Option<i64> = conn_end
            .query_row(
                "SELECT 1 FROM resilience_wave_schedules
                 WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
                rusqlite::params![schedule_id, sched_epoch, runner_token, comp_iso],
                |r| r.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        let w_own: Option<i64> = conn_end
            .query_row(
                "SELECT 1 FROM resilience_wave_states
                 WHERE schedule_id = ? AND wave_index = ? AND execution_epoch = ? AND runner_token = ? AND lease_until >= ?",
                rusqlite::params![schedule_id, wave_index, wave_epoch, runner_token, comp_iso],
                |r| r.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        if s_own.is_none() || w_own.is_none() {
            return Ok(false);
        }
        let inc: i64 = conn_end
            .query_row(
                "SELECT COUNT(*) FROM resilience_wave_actions
                 WHERE schedule_id = ? AND wave_index = ? AND status != 'VERIFIED_SUCCESS'",
                rusqlite::params![schedule_id, wave_index],
                |r| r.get(0),
            )
            .map_err(|_| unhandled_error())?;
        if inc != 0 {
            return Ok(false);
        }
        let w_upd = conn_end
            .execute(
                "UPDATE resilience_wave_states
                 SET status = 'COMPLETED', verified_at = ?, owner_instance_id = NULL, lease_until = NULL, runner_token = NULL, updated_at = ?
                 WHERE schedule_id = ? AND wave_index = ? AND execution_epoch = ? AND runner_token = ?",
                rusqlite::params![comp_iso, comp_iso, schedule_id, wave_index, wave_epoch, runner_token],
            )
            .map_err(|_| unhandled_error())?;
        if w_upd != 1 {
            return Ok(false);
        }
        let remaining: i64 = conn_end
            .query_row(
                "SELECT COUNT(*) FROM resilience_wave_states WHERE schedule_id = ? AND status != 'COMPLETED'",
                [schedule_id],
                |r| r.get(0),
            )
            .map_err(|_| unhandled_error())?;
        let next_s_status = if remaining == 0 {
            "COMPLETED"
        } else {
            "RUNNING"
        };
        let s_upd = conn_end
            .execute(
                "UPDATE resilience_wave_schedules
                 SET status = ?, owner_instance_id = NULL, lease_until = NULL, runner_token = NULL, updated_at = ?
                 WHERE schedule_id = ? AND execution_epoch = ? AND runner_token = ?",
                rusqlite::params![next_s_status, comp_iso, schedule_id, sched_epoch, runner_token],
            )
            .map_err(|_| unhandled_error())?;
        Ok(s_upd == 1)
    })();

    let finished = match comp_ok {
        Ok(v) => v,
        Err(err) => {
            let _ = conn_end.execute_batch("ROLLBACK;");
            return Err(err);
        }
    };
    if !finished {
        let _ = conn_end.execute_batch("ROLLBACK;");
        return Ok(json!({
            "scheduleId": schedule_id,
            "waveIndex": wave_index,
            "ran": false,
            "status": "RUNNER_FENCED_OUT",
        }));
    }
    conn_end
        .execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;

    Ok(json!({
        "scheduleId": schedule_id,
        "waveIndex": wave_index,
        "ran": true,
        "status": "COMPLETED",
        "scheduleExecutionEpoch": sched_epoch,
        "waveExecutionEpoch": wave_epoch,
        "actions": action_results,
    }))
}
