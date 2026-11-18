//! Read-only Resilience routes:
//! - `GET /api/workspace/resilience/journal`
//! - `GET /api/workspace/resilience/capacity-forecast`
//! - `GET /api/workspace/resilience/waves`
//!
//! All three read existing SQLite stores via `open_readonly_sqlite` and never
//! create `.resilience-journal`, `.resilience-capacity`, `.resilience-scheduler`,
//! or `.resilience-waves`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::extract::RawQuery;
use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::backup_retention::chrono::{DateTime, Duration as ChronoDuration, Utc};
use percent_encoding::percent_decode_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::{open_readonly_sqlite, utc_iso_from_unix};
use crate::backup_dr_slo_routes;
use crate::backup_retention_routes::load_backup_policy;
use crate::route_support::{self, unhandled_error};

const RISK_DETECTION_LATENCY_MS: &str = "risk_detection_latency_ms";
const REMEDIATION_QUEUE_DELAY_MS: &str = "remediation_queue_delay_ms";
const REPAIR_TIME_MS: &str = "repair_time_ms";
const REBALANCE_TIME_MS: &str = "rebalance_time_ms";
const RISK_CLEAR_LATENCY_MS: &str = "risk_clear_latency_ms";
const DR_READINESS_AGE_HOURS: &str = "dr_readiness_age_hours";
const LEASE_TAKEOVER_TIME_MS: &str = "lease_takeover_time_ms";
const SCHEDULER_STARVATION_AGE_SECONDS: &str = "scheduler_starvation_age_seconds";
const EVIDENCE_FRESHNESS_SECONDS: &str = "evidence_freshness_seconds";

const DEFAULT_FAST_WINDOW_SECONDS: f64 = 3600.0;
const DEFAULT_SLOW_WINDOW_SECONDS: f64 = 86400.0;
const DEFAULT_ERROR_BUDGET_FRACTION: f64 = 0.01;
const DEFAULT_FAST_CRITICAL_THRESHOLD: f64 = 14.4;
const DEFAULT_SLOW_CRITICAL_THRESHOLD: f64 = 6.0;

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/resilience/journal", any(dispatch_journal))
        .route(
            "/api/workspace/resilience/capacity-forecast",
            any(dispatch_capacity_forecast),
        )
        .route("/api/workspace/resilience/waves", any(dispatch_waves))
        .route(
            "/api/workspace/resilience/readiness",
            any(dispatch_readiness),
        )
}

fn parse_query(raw: Option<&str>) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Some(raw) = raw else {
        return map;
    };
    for pair in raw.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, val) = pair.split_once('=').unwrap_or((pair, ""));
        let k = percent_decode_str(&key.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned();
        let v = percent_decode_str(&val.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned();
        map.insert(k, v);
    }
    map
}

pub(crate) fn table_exists(conn: &Connection, name: &str) -> Result<bool, AppError> {
    let found: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            [name],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(found.is_some())
}

fn json_col(raw: Option<String>, fallback: Value) -> Value {
    match raw.as_deref() {
        Some(text) if !text.is_empty() => serde_json::from_str(text).unwrap_or(fallback),
        _ => fallback,
    }
}

async fn run_get(
    method: Method,
    work: impl FnOnce() -> Result<Value, AppError> + Send + 'static,
) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(_)) if method == Method::HEAD => axum::http::StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => {
            axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

async fn dispatch_journal(method: Method, RawQuery(raw): RawQuery) -> Response {
    let params = parse_query(raw.as_deref());
    let state = params
        .get("state")
        .cloned()
        .filter(|value| !value.is_empty());
    let action_type = params
        .get("type")
        .cloned()
        .filter(|value| !value.is_empty());
    let limit = params
        .get("limit")
        .filter(|text| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|text| text.parse::<i64>().ok())
        .unwrap_or(50);
    run_get(method, move || list_journal(state, action_type, limit)).await
}

fn list_journal(
    state: Option<String>,
    action_type: Option<String>,
    limit: i64,
) -> Result<Value, AppError> {
    let db = crate::data_routes::workspace_root()
        .join(".resilience-journal")
        .join("journal.sqlite3");
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(json!([]));
    };
    if !table_exists(&conn, "resilience_actions")? {
        return Ok(json!([]));
    }
    let mut clauses = Vec::new();
    let mut bind_vals: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(st) = state {
        clauses.push("state = ?");
        bind_vals.push(st.into());
    }
    if let Some(at) = action_type {
        clauses.push("action_type = ?");
        bind_vals.push(at.to_ascii_uppercase().into());
    }
    let where_clause = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    bind_vals.push(limit.into());
    let sql = format!(
        "SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest, \
                state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch, \
                effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect, \
                severity_before, coordination_plan_id, execution_result_json, verification_result_json, \
                decision_proof_json, risk_before_digest, risk_after_digest, error_message, \
                created_at, updated_at \
         FROM resilience_actions{where_clause} ORDER BY created_at DESC LIMIT ?"
    );
    let mut stmt = conn.prepare(&sql).map_err(|_| unhandled_error())?;
    let params = rusqlite::params_from_iter(bind_vals.iter());
    let rows = stmt
        .query_map(params, row_to_action_json)
        .map_err(|_| unhandled_error())?;
    let mut results = Vec::new();
    for row in rows {
        results.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(Value::Array(results))
}

fn row_to_action_json(row: &rusqlite::Row<'_>) -> Result<Value, rusqlite::Error> {
    Ok(json!({
        "actionId": row.get::<_, String>(0)?,
        "planId": row.get::<_, Option<String>>(1)?,
        "type": row.get::<_, String>(2)?,
        "createdBy": row.get::<_, String>(3)?,
        "inputRiskDigest": row.get::<_, Option<String>>(4)?,
        "planDigest": row.get::<_, Option<String>>(5)?,
        "state": row.get::<_, String>(6)?,
        "parameters": json_col(row.get::<_, Option<String>>(7)?, json!({})),
        "ownerInstanceId": row.get::<_, Option<String>>(8)?,
        "leaseUntil": row.get::<_, Option<String>>(9)?,
        "claimToken": row.get::<_, Option<String>>(10)?,
        "executionEpoch": row.get::<_, Option<i64>>(11)?.unwrap_or(0),
        "effectClass": row.get::<_, Option<String>>(12)?,
        "compensationState": row.get::<_, Option<String>>(13)?,
        "effectHandle": json_col(row.get::<_, Option<String>>(14)?, Value::Null),
        "riskSubject": json_col(row.get::<_, Option<String>>(15)?, Value::Null),
        "expectedEffect": row.get::<_, Option<String>>(16)?,
        "severityBefore": row.get::<_, Option<String>>(17)?,
        "coordinationPlanId": row.get::<_, Option<String>>(18)?,
        "executionResult": json_col(row.get::<_, Option<String>>(19)?, Value::Null),
        "verificationResult": json_col(row.get::<_, Option<String>>(20)?, Value::Null),
        "decisionProof": json_col(row.get::<_, Option<String>>(21)?, Value::Null),
        "riskBeforeDigest": row.get::<_, Option<String>>(22)?,
        "riskAfterDigest": row.get::<_, Option<String>>(23)?,
        "error": row.get::<_, Option<String>>(24)?,
        "createdAt": row.get::<_, String>(25)?,
        "updatedAt": row.get::<_, String>(26)?,
    }))
}

pub(crate) fn get_journal_action(action_id: &str) -> Result<Option<Value>, AppError> {
    let db = crate::data_routes::workspace_root()
        .join(".resilience-journal")
        .join("journal.sqlite3");
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(None);
    };
    if !table_exists(&conn, "resilience_actions")? {
        return Ok(None);
    }
    let mut stmt = conn
        .prepare(
            "SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest, \
                    state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch, \
                    effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect, \
                    severity_before, coordination_plan_id, execution_result_json, verification_result_json, \
                    decision_proof_json, risk_before_digest, risk_after_digest, error_message, \
                    created_at, updated_at \
             FROM resilience_actions WHERE action_id = ?1 LIMIT 1",
        )
        .map_err(|_| unhandled_error())?;
    stmt.query_row([action_id], row_to_action_json)
        .optional()
        .map_err(|_| unhandled_error())
}

async fn dispatch_capacity_forecast(method: Method, RawQuery(raw): RawQuery) -> Response {
    let params = parse_query(raw.as_deref());
    let days = params
        .get("horizonDays")
        .filter(|text| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|text| text.parse::<i64>().ok())
        .unwrap_or(90)
        .max(1);
    run_get(method, move || capacity_forecast_snapshot(days)).await
}

fn capacity_forecast_snapshot(horizon: i64) -> Result<Value, AppError> {
    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let db = crate::data_routes::workspace_root()
        .join(".resilience-capacity")
        .join("capacity.sqlite3");
    let mut latest_by_target: BTreeMap<String, Value> = BTreeMap::new();
    if let Some(conn) = open_readonly_sqlite(&db)? {
        if table_exists(&conn, "resilience_forecast_records")?
            && table_exists(&conn, "resilience_capacity_observations")?
        {
            let mut stmt = conn
                .prepare(
                    "SELECT forecast_id, target_id, target_incarnation, capacity_revision, \
                            horizon_days, forecasted_at, evaluation_due_at, p50_free_bytes, \
                            p90_free_bytes, forecast_digest, observation_set_digest, status, \
                            actual_observation_key, backtest_key, forecast_json, created_at, updated_at \
                     FROM resilience_forecast_records \
                     WHERE horizon_days = ? AND status IN ('ACTIVE', 'DUE') \
                     ORDER BY forecasted_at ASC, forecast_id ASC LIMIT 100000",
                )
                .map_err(|_| unhandled_error())?;
            let rows = stmt
                .query_map([horizon], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, i64>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, i64>(7)?,
                        row.get::<_, i64>(8)?,
                        row.get::<_, String>(9)?,
                        row.get::<_, String>(10)?,
                        row.get::<_, String>(11)?,
                        row.get::<_, Option<String>>(12)?,
                        row.get::<_, Option<String>>(13)?,
                        row.get::<_, String>(14)?,
                        row.get::<_, String>(15)?,
                        row.get::<_, String>(16)?,
                    ))
                })
                .map_err(|_| unhandled_error())?;
            let mut series_stmt = conn
                .prepare(
                    "SELECT target_incarnation, capacity_revision \
                     FROM resilience_capacity_observations \
                     WHERE target_id = ? \
                     ORDER BY observed_at DESC, observation_key DESC LIMIT 1",
                )
                .map_err(|_| unhandled_error())?;
            let mut series_cache: HashMap<String, Option<(String, String)>> = HashMap::new();
            for row in rows {
                let (
                    forecast_id,
                    target_id,
                    incarnation,
                    revision,
                    h_days,
                    forecasted_at,
                    due_at,
                    p50,
                    p90,
                    f_digest,
                    obs_digest,
                    status,
                    actual_obs,
                    backtest_key,
                    forecast_raw,
                    created_at,
                    updated_at,
                ) = row.map_err(|_| unhandled_error())?;
                let latest_series = series_cache.entry(target_id.clone()).or_insert_with(|| {
                    series_stmt
                        .query_row([&target_id], |srow| {
                            Ok((
                                srow.get::<_, Option<String>>(0)?
                                    .filter(|s| !s.is_empty())
                                    .unwrap_or_else(|| format!("legacy:{target_id}")),
                                srow.get::<_, Option<String>>(1)?
                                    .filter(|s| !s.is_empty())
                                    .unwrap_or_else(|| "legacy".to_string()),
                            ))
                        })
                        .optional()
                        .ok()
                        .flatten()
                });
                let Some((cur_inc, cur_rev)) = latest_series else {
                    continue;
                };
                if incarnation != *cur_inc || revision != *cur_rev {
                    continue;
                }
                let forecast_val = match serde_json::from_str::<Value>(&forecast_raw) {
                    Ok(val @ Value::Object(_)) => val,
                    _ => json!({}),
                };
                latest_by_target.insert(
                    target_id.clone(),
                    json!({
                        "forecastId": forecast_id,
                        "targetId": target_id,
                        "targetIncarnation": incarnation,
                        "capacityRevision": revision,
                        "horizonDays": h_days,
                        "forecastedAt": forecasted_at,
                        "evaluationDueAt": due_at,
                        "p50FreeBytes": p50,
                        "p90FreeBytes": p90,
                        "forecastDigest": f_digest,
                        "capacityObservationSetDigest": obs_digest,
                        "status": status,
                        "actualObservationKey": actual_obs.unwrap_or_default(),
                        "backtestKey": backtest_key.unwrap_or_default(),
                        "forecast": forecast_val,
                        "createdAt": created_at,
                        "updatedAt": updated_at,
                    }),
                );
            }
        }
    }
    let targets: Vec<Value> = latest_by_target.into_values().collect();
    Ok(json!({
        "horizonDays": horizon,
        "targets": targets,
        "generatedAt": utc_iso_from_unix(now_secs),
        "source": "durable-forecast-registry",
    }))
}

async fn dispatch_waves(method: Method, RawQuery(raw): RawQuery) -> Response {
    let params = parse_query(raw.as_deref());
    let schedule_id = params
        .get("scheduleId")
        .cloned()
        .filter(|value| !value.is_empty());
    run_get(method, move || waves_snapshot(schedule_id)).await
}

fn waves_snapshot(schedule_id_opt: Option<String>) -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let mut snapshot: Option<Value> = None;
    let schedule_id = if let Some(sid) = schedule_id_opt {
        sid
    } else {
        let sched_db = root.join(".resilience-scheduler").join("service.sqlite3");
        let latest = if let Some(conn) = open_readonly_sqlite(&sched_db)? {
            if table_exists(&conn, "resilience_scheduler_runs")? {
                let raw: Option<String> = conn
                    .query_row(
                        "SELECT schedule_json FROM resilience_scheduler_runs \
                         ORDER BY scheduled_at DESC, rowid DESC LIMIT 1",
                        [],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(|_| unhandled_error())?;
                raw.and_then(|text| serde_json::from_str::<Value>(&text).ok())
                    .filter(Value::is_object)
            } else {
                None
            }
        } else {
            None
        };
        let sid = latest
            .as_ref()
            .and_then(|val| val.get("scheduleId"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        snapshot = latest;
        sid
    };

    if schedule_id.is_empty() {
        return Ok(json!({"schedule": null, "waves": [], "actions": []}));
    }

    let waves_db = root.join(".resilience-waves").join("waves.sqlite3");
    let Some(conn) = open_readonly_sqlite(&waves_db)? else {
        return Ok(json!({
            "schedule": snapshot,
            "waves": [],
            "actions": [],
        }));
    };

    let sched = if table_exists(&conn, "resilience_wave_schedules")? {
        conn.query_row(
            "SELECT schedule_id, schedule_digest, status, risk_digest, authority_head_digest, \
                    schedule_json, stale_reason, execution_epoch, owner_instance_id, lease_until, \
                    created_at, updated_at \
             FROM resilience_wave_schedules WHERE schedule_id = ?",
            [&schedule_id],
            |row| {
                Ok(json!({
                    "scheduleId": row.get::<_, String>(0)?,
                    "scheduleDigest": row.get::<_, String>(1)?,
                    "status": row.get::<_, String>(2)?,
                    "riskDigest": row.get::<_, String>(3)?,
                    "authorityHeadDigest": row.get::<_, Option<String>>(4)?,
                    "schedule": json_col(Some(row.get::<_, String>(5)?), json!({})),
                    "staleReason": row.get::<_, Option<String>>(6)?,
                    "scheduleExecutionEpoch": row.get::<_, Option<i64>>(7)?.unwrap_or(0),
                    "ownerInstanceId": row.get::<_, Option<String>>(8)?,
                    "leaseUntil": row.get::<_, Option<String>>(9)?,
                    "createdAt": row.get::<_, String>(10)?,
                    "updatedAt": row.get::<_, String>(11)?,
                }))
            },
        )
        .optional()
        .map_err(|_| unhandled_error())?
    } else {
        None
    };

    let mut waves = Vec::new();
    if table_exists(&conn, "resilience_wave_states")? {
        let mut stmt = conn
            .prepare(
                "SELECT schedule_id, wave_index, status, revalidation_json, admitted_at, verified_at, \
                        execution_epoch, owner_instance_id, lease_until, updated_at \
                 FROM resilience_wave_states WHERE schedule_id = ? ORDER BY wave_index",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map([&schedule_id], |row| {
                Ok(json!({
                    "scheduleId": row.get::<_, String>(0)?,
                    "waveIndex": row.get::<_, i64>(1)?,
                    "status": row.get::<_, String>(2)?,
                    "revalidation": json_col(row.get::<_, Option<String>>(3)?, Value::Null),
                    "admittedAt": row.get::<_, Option<String>>(4)?,
                    "verifiedAt": row.get::<_, Option<String>>(5)?,
                    "waveExecutionEpoch": row.get::<_, Option<i64>>(6)?.unwrap_or(0),
                    "ownerInstanceId": row.get::<_, Option<String>>(7)?,
                    "leaseUntil": row.get::<_, Option<String>>(8)?,
                    "updatedAt": row.get::<_, String>(9)?,
                }))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            waves.push(row.map_err(|_| unhandled_error())?);
        }
    }

    let mut actions = Vec::new();
    if table_exists(&conn, "resilience_wave_actions")? {
        let mut stmt = conn
            .prepare(
                "SELECT schedule_id, wave_index, action_id, action_json, status, outcome, \
                        execution_epoch, schedule_execution_epoch, wave_execution_epoch, \
                        owner_instance_id, lease_until, journal_execution_epoch, \
                        effect_handle_json, terminal_json, updated_at \
                 FROM resilience_wave_actions WHERE schedule_id = ? ORDER BY wave_index, action_id",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map([&schedule_id], |row| {
                Ok(json!({
                    "scheduleId": row.get::<_, String>(0)?,
                    "waveIndex": row.get::<_, i64>(1)?,
                    "actionId": row.get::<_, String>(2)?,
                    "action": json_col(Some(row.get::<_, String>(3)?), json!({})),
                    "status": row.get::<_, String>(4)?,
                    "outcome": row.get::<_, Option<String>>(5)?,
                    "actionExecutionEpoch": row.get::<_, Option<i64>>(6)?.unwrap_or(0),
                    "scheduleExecutionEpoch": row.get::<_, Option<i64>>(7)?.unwrap_or(0),
                    "waveExecutionEpoch": row.get::<_, Option<i64>>(8)?.unwrap_or(0),
                    "ownerInstanceId": row.get::<_, Option<String>>(9)?,
                    "leaseUntil": row.get::<_, Option<String>>(10)?,
                    "journalExecutionEpoch": row.get::<_, Option<i64>>(11)?.unwrap_or(0),
                    "effectHandle": json_col(row.get::<_, Option<String>>(12)?, Value::Null),
                    "terminal": json_col(row.get::<_, Option<String>>(13)?, Value::Null),
                    "updatedAt": row.get::<_, String>(14)?,
                }))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            actions.push(row.map_err(|_| unhandled_error())?);
        }
    }

    Ok(json!({
        "schedule": sched.or(snapshot),
        "waves": waves,
        "actions": actions,
    }))
}

#[derive(Clone)]
struct SloSample {
    metric_name: String,
    value: f64,
    metadata: Value,
    observed_at: String,
}

async fn dispatch_readiness(method: Method) -> Response {
    run_get(method, fleet_readiness_snapshot).await
}

fn round_scale(value: f64, scale: f64) -> f64 {
    (value * scale).round() / scale
}

fn parse_utc_rfc3339(raw: &str) -> Option<DateTime<Utc>> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    DateTime::parse_from_rfc3339(trimmed)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
}

fn format_utc_iso(dt: DateTime<Utc>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn percentile_linear(values: &[f64], q: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut ordered = values.to_vec();
    ordered.sort_by(|a, b| a.total_cmp(b));
    let index = (ordered.len() - 1) as f64 * q;
    let lower = index as usize;
    let upper = (lower + 1).min(ordered.len() - 1);
    let fraction = index - lower as f64;
    ordered[lower] + fraction * (ordered[upper] - ordered[lower])
}

fn list_open_risk_observations(root: &Path) -> Result<Vec<Value>, AppError> {
    let db_path = root.join(".resilience-risk").join("risk.sqlite3");
    let Some(conn) = open_readonly_sqlite(&db_path)? else {
        return Ok(Vec::new());
    };
    if !table_exists(&conn, "resilience_risk_observations")? {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT risk_subject_digest, current_severity, open_since_at, policy_id \
             FROM resilience_risk_observations \
             WHERE status IN ('OPEN', 'REOPENED', 'UNKNOWN_COVERAGE') \
             ORDER BY open_since_at ASC, risk_subject_digest ASC",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([], |row| {
            Ok(json!({
                "riskSubjectDigest": row.get::<_, String>(0)?,
                "currentSeverity": row.get::<_, String>(1)?,
                "openSinceAt": row.get::<_, Option<String>>(2)?,
                "policyId": row.get::<_, Option<String>>(3)?,
            }))
        })
        .map_err(|_| unhandled_error())?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(out)
}

fn risk_debt_for_observation(risk: &Value, now: DateTime<Utc>) -> Result<f64, AppError> {
    let pid = risk
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let policy = if pid.is_empty() {
        None
    } else {
        match load_backup_policy(&pid) {
            Ok(pol) => Some(pol),
            Err(err) if err.status == 404 => None,
            Err(err) => return Err(err),
        }
    };
    let severity = risk
        .get("currentSeverity")
        .and_then(Value::as_str)
        .unwrap_or("warning")
        .to_lowercase();
    let base_weight = match severity.as_str() {
        "critical" => 10.0,
        "degraded" => 5.0,
        "warning" => 2.0,
        "healthy" => 0.0,
        "blocked" => 15.0,
        _ => 2.0,
    };
    let age_seconds = risk
        .get("openSinceAt")
        .and_then(Value::as_str)
        .and_then(parse_utc_rfc3339)
        .map(|opened| ((now - opened).num_milliseconds() as f64 / 1000.0).max(0.0))
        .unwrap_or(0.0);
    let age_factor = 1.0 + ((age_seconds / 86400.0) * 0.5).min(10.0);
    let pol_crit = policy
        .as_ref()
        .and_then(|p| p.get("criticality").or_else(|| p.get("priority")))
        .and_then(Value::as_str)
        .unwrap_or("standard")
        .to_lowercase();
    let crit_multiplier = match pol_crit.as_str() {
        "critical" => 3.0,
        "high" => 2.0,
        "standard" => 1.0,
        _ => 1.0,
    };
    Ok(round_scale(
        base_weight * age_factor * crit_multiplier,
        1000.0,
    ))
}

fn load_recent_slo_samples(conn: &Connection) -> Result<Vec<SloSample>, AppError> {
    if !table_exists(conn, "resilience_slo_samples")? {
        return Ok(Vec::new());
    }
    let mut metric_stmt = conn
        .prepare("SELECT DISTINCT metric_name FROM resilience_slo_samples ORDER BY metric_name")
        .map_err(|_| unhandled_error())?;
    let metric_rows = metric_stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| unhandled_error())?;
    let mut metrics = Vec::new();
    for row in metric_rows {
        metrics.push(row.map_err(|_| unhandled_error())?);
    }

    let mut samples = Vec::new();
    let mut stmt = conn
        .prepare(
            "SELECT metric_name, value, metadata_json, observed_at \
             FROM resilience_slo_samples \
             WHERE metric_name = ? \
             ORDER BY observed_at DESC, sample_id DESC LIMIT 10000",
        )
        .map_err(|_| unhandled_error())?;
    for metric_name in metrics {
        let rows = stmt
            .query_map([&metric_name], |row| {
                let meta_str: String = row.get(2)?;
                Ok(SloSample {
                    metric_name: row.get(0)?,
                    value: row.get(1)?,
                    metadata: serde_json::from_str(&meta_str).unwrap_or_else(|_| json!({})),
                    observed_at: row.get(3)?,
                })
            })
            .map_err(|_| unhandled_error())?;
        let mut collected = Vec::new();
        for row in rows {
            collected.push(row.map_err(|_| unhandled_error())?);
        }
        collected.reverse();
        samples.extend(collected);
    }
    Ok(samples)
}

fn sample_in_window(sample: &SloSample, now: DateTime<Utc>, window_seconds: Option<f64>) -> bool {
    let Some(win) = window_seconds else {
        return true;
    };
    let Some(obs) = parse_utc_rfc3339(&sample.observed_at) else {
        return false;
    };
    let diff_seconds = (now - obs).num_milliseconds() as f64 / 1000.0;
    diff_seconds <= win
}

fn window_metric_summary(
    items: &[SloSample],
    now: DateTime<Utc>,
    window_seconds: Option<f64>,
) -> Value {
    let ordered: Vec<&SloSample> = items
        .iter()
        .filter(|sample| sample_in_window(sample, now, window_seconds))
        .collect();
    if ordered.is_empty() {
        return json!({
            "p50": Value::Null,
            "p95": Value::Null,
            "p99": Value::Null,
            "samples": 0,
            "coverageSeconds": 0.0,
            "status": "INSUFFICIENT_DATA",
        });
    }
    let values: Vec<f64> = ordered.iter().map(|s| s.value).collect();
    let coverage = ordered
        .first()
        .zip(ordered.last())
        .and_then(|(first, last)| {
            Some(
                ((parse_utc_rfc3339(&last.observed_at)? - parse_utc_rfc3339(&first.observed_at)?)
                    .num_milliseconds() as f64
                    / 1000.0)
                    .max(0.0),
            )
        })
        .unwrap_or(0.0);
    json!({
        "p50": round_scale(percentile_linear(&values, 0.50), 1000.0),
        "p95": round_scale(percentile_linear(&values, 0.95), 1000.0),
        "p99": round_scale(percentile_linear(&values, 0.99), 1000.0),
        "samples": values.len(),
        "coverageSeconds": round_scale(coverage, 1000.0),
        "status": "OK",
    })
}

fn fleet_slo_snapshot(samples: &[SloSample], now: DateTime<Utc>, dr_freshness_hours: f64) -> Value {
    let mut grouped: BTreeMap<String, Vec<SloSample>> = BTreeMap::new();
    for sample in samples {
        grouped
            .entry(sample.metric_name.clone())
            .or_default()
            .push(sample.clone());
    }

    let window_defs: [(&str, Option<f64>); 5] = [
        ("1h", Some(3600.0)),
        ("24h", Some(86400.0)),
        ("7d", Some(604800.0)),
        ("30d", Some(2592000.0)),
        ("lifetime", None),
    ];
    let mut windows = Map::new();
    for (win_name, win_secs) in window_defs {
        let mut per_metric = Map::new();
        for (metric_name, items) in &grouped {
            per_metric.insert(
                metric_name.clone(),
                window_metric_summary(items, now, win_secs),
            );
        }
        windows.insert(win_name.to_string(), Value::Object(per_metric));
    }

    let mut summaries = Map::new();
    let mut sample_counts = Map::new();
    for (metric_name, items) in &grouped {
        let bounded: Vec<&SloSample> = items
            .iter()
            .filter(|sample| sample_in_window(sample, now, Some(86400.0)))
            .collect();
        if bounded.is_empty() {
            continue;
        }
        let values: Vec<f64> = bounded.iter().map(|s| s.value).collect();
        sample_counts.insert(metric_name.clone(), json!(values.len()));
        summaries.insert(
            metric_name.clone(),
            json!({
                "count": values.len(),
                "p50": round_scale(percentile_linear(&values, 0.50), 1000.0),
                "p95": round_scale(percentile_linear(&values, 0.95), 1000.0),
                "latest": round_scale(*values.last().unwrap_or(&0.0), 1000.0),
                "lastObservedAt": bounded.last().map(|s| s.observed_at.clone()).unwrap_or_default(),
            }),
        );
    }

    let p95 = |metric_name: &str| -> f64 {
        let win24 = windows
            .get("24h")
            .and_then(|v| v.get(metric_name))
            .and_then(Value::as_object);
        if win24.and_then(|w| w.get("status")).and_then(Value::as_str) == Some("INSUFFICIENT_DATA")
        {
            return 0.0;
        }
        win24
            .and_then(|w| w.get("p95"))
            .and_then(Value::as_f64)
            .or_else(|| {
                summaries
                    .get(metric_name)
                    .and_then(|s| s.get("p95"))
                    .and_then(Value::as_f64)
            })
            .unwrap_or(0.0)
    };
    let latest_val = |metric_name: &str| -> f64 {
        summaries
            .get(metric_name)
            .and_then(|s| s.get("latest"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
    };

    let evidence_freshness_seconds = grouped
        .get(EVIDENCE_FRESHNESS_SECONDS)
        .and_then(|items| items.last())
        .and_then(|sample| parse_utc_rfc3339(&sample.observed_at))
        .map(|obs| {
            round_scale(
                ((now - obs).num_milliseconds() as f64 / 1000.0).max(0.0),
                1000.0,
            )
        });

    json!({
        "riskDetectionP95Ms": p95(RISK_DETECTION_LATENCY_MS),
        "remediationQueueDelayP95Ms": p95(REMEDIATION_QUEUE_DELAY_MS),
        "repairP95Ms": p95(REPAIR_TIME_MS),
        "rebalanceP95Ms": p95(REBALANCE_TIME_MS),
        "riskClearP95Ms": p95(RISK_CLEAR_LATENCY_MS),
        "drFreshnessHours": round_scale(dr_freshness_hours, 1000.0),
        "leaseTakeoverP95Ms": p95(LEASE_TAKEOVER_TIME_MS),
        "schedulerStarvationAgeSeconds": latest_val(SCHEDULER_STARVATION_AGE_SECONDS),
        "evidenceFreshnessSeconds": evidence_freshness_seconds,
        "sampleCounts": Value::Object(sample_counts),
        "metrics": Value::Object(summaries),
        "windows": Value::Object(windows),
        "evaluatedAt": format_utc_iso(now),
    })
}

fn window_burn_by_indicator(
    conn: &Connection,
    since: DateTime<Utc>,
    until: DateTime<Utc>,
) -> Result<BTreeMap<String, f64>, AppError> {
    if !table_exists(conn, "resilience_slo_burn_observations")? {
        return Ok(BTreeMap::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT indicator, bad_units, total_units, started_at, observed_at \
             FROM resilience_slo_burn_observations \
             WHERE observed_at >= ? AND started_at <= ? \
             ORDER BY indicator, observed_at, observation_id",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([format_utc_iso(since), format_utc_iso(until)], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, f64>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(|_| unhandled_error())?;

    let mut totals: BTreeMap<String, (f64, f64)> = BTreeMap::new();
    for row in rows {
        let (indicator, bad_units, total_units, started_opt, observed_raw) =
            row.map_err(|_| unhandled_error())?;
        let started_str = started_opt.unwrap_or_else(|| observed_raw.clone());
        let (Some(started), Some(observed)) = (
            parse_utc_rfc3339(&started_str),
            parse_utc_rfc3339(&observed_raw),
        ) else {
            continue;
        };
        let fraction = if observed > started {
            let overlap_start = started.max(since);
            let overlap_end = observed.min(until);
            let overlap_ms = (overlap_end - overlap_start).num_milliseconds().max(0) as f64;
            let total_ms = (observed - started).num_milliseconds() as f64;
            overlap_ms / total_ms
        } else if observed >= since && observed <= until {
            1.0
        } else {
            0.0
        };
        let entry = totals.entry(indicator).or_insert((0.0, 0.0));
        entry.0 += bad_units * fraction;
        entry.1 += total_units * fraction;
    }

    let mut rates = BTreeMap::new();
    for (indicator, (bad, total)) in totals {
        let rate = if total > 0.0 {
            bad / total / DEFAULT_ERROR_BUDGET_FRACTION
        } else {
            0.0
        };
        rates.insert(indicator, rate);
    }
    Ok(rates)
}

fn compute_burn_rates(
    conn_opt: Option<&Connection>,
    now: DateTime<Utc>,
) -> Result<Value, AppError> {
    let fast_since = now - ChronoDuration::seconds(DEFAULT_FAST_WINDOW_SECONDS as i64);
    let slow_since = now - ChronoDuration::seconds(DEFAULT_SLOW_WINDOW_SECONDS as i64);
    let (fast_map, slow_map) = match conn_opt {
        Some(conn) => (
            window_burn_by_indicator(conn, fast_since, now)?,
            window_burn_by_indicator(conn, slow_since, now)?,
        ),
        None => (BTreeMap::new(), BTreeMap::new()),
    };

    let indicators: BTreeSet<String> = fast_map.keys().chain(slow_map.keys()).cloned().collect();
    let mut by_indicator = Map::new();
    let mut critical = Vec::new();
    let mut max_fast = 0.0_f64;
    let mut max_slow = 0.0_f64;
    for indicator in indicators {
        let fast = *fast_map.get(&indicator).unwrap_or(&0.0);
        let slow = *slow_map.get(&indicator).unwrap_or(&0.0);
        max_fast = max_fast.max(fast);
        max_slow = max_slow.max(slow);
        if fast > DEFAULT_FAST_CRITICAL_THRESHOLD && slow > DEFAULT_SLOW_CRITICAL_THRESHOLD {
            critical.push(Value::String(indicator.clone()));
        }
        by_indicator.insert(
            indicator,
            json!({
                "fast": round_scale(fast, 1_000_000.0),
                "slow": round_scale(slow, 1_000_000.0),
            }),
        );
    }
    let status = if critical.is_empty() {
        "OK"
    } else {
        "CRITICAL"
    };
    Ok(json!({
        "fast": round_scale(max_fast, 1_000_000.0),
        "slow": round_scale(max_slow, 1_000_000.0),
        "fastWindowSeconds": DEFAULT_FAST_WINDOW_SECONDS,
        "slowWindowSeconds": DEFAULT_SLOW_WINDOW_SECONDS,
        "errorBudgetFraction": DEFAULT_ERROR_BUDGET_FRACTION,
        "fastCriticalThreshold": DEFAULT_FAST_CRITICAL_THRESHOLD,
        "slowCriticalThreshold": DEFAULT_SLOW_CRITICAL_THRESHOLD,
        "byIndicator": Value::Object(by_indicator),
        "criticalIndicators": critical,
        "status": status,
        "evaluatedAt": format_utc_iso(now),
    }))
}

fn latest_schedule_snapshot(root: &Path) -> Result<Value, AppError> {
    let sched_db = root.join(".resilience-scheduler").join("service.sqlite3");
    let Some(conn) = open_readonly_sqlite(&sched_db)? else {
        return Ok(json!({}));
    };
    if !table_exists(&conn, "resilience_scheduler_runs")? {
        return Ok(json!({}));
    }
    let raw: Option<String> = conn
        .query_row(
            "SELECT schedule_json FROM resilience_scheduler_runs \
             ORDER BY scheduled_at DESC, rowid DESC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(raw
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({})))
}

fn fleet_readiness_snapshot() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let now_dt = Utc::now();
    let now_secs = now_dt.timestamp().max(0) as u64;
    let current = DateTime::from_timestamp(now_secs as i64, 0).unwrap_or(now_dt);

    let open_risks = list_open_risk_observations(&root)?;
    let mut open_ages = Vec::new();
    let mut critical_count = 0usize;
    let mut debt_total = 0.0_f64;
    for risk in &open_risks {
        if let Some(opened) = risk
            .get("openSinceAt")
            .and_then(Value::as_str)
            .and_then(parse_utc_rfc3339)
        {
            let age = ((current - opened).num_milliseconds() as f64 / 1000.0).max(0.0);
            open_ages.push(age);
        }
        let sev = risk
            .get("currentSeverity")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        if sev == "critical" || sev == "blocked" {
            critical_count += 1;
        }
        debt_total += risk_debt_for_observation(risk, current)?;
    }

    let dr_slo = backup_dr_slo_routes::build_body_at(now_secs)?;
    let dr_freshness_hours = dr_slo
        .get("evidenceFreshnessDays")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0)
        * 24.0;
    let overall_dr_compliant = dr_slo
        .get("overallSloCompliant")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let slo_db = root.join(".resilience-slo").join("slo.sqlite3");
    let slo_conn = open_readonly_sqlite(&slo_db)?;
    let samples = match slo_conn.as_ref() {
        Some(conn) => load_recent_slo_samples(conn)?,
        None => Vec::new(),
    };
    let slo = fleet_slo_snapshot(&samples, current, dr_freshness_hours);
    let burn_rate = compute_burn_rates(slo_conn.as_ref(), current)?;

    let evidence_sample = samples
        .iter()
        .rev()
        .find(|s| s.metric_name == EVIDENCE_FRESHNESS_SECONDS);
    let proof_freshness = evidence_sample
        .and_then(|s| parse_utc_rfc3339(&s.observed_at))
        .map(|obs| {
            round_scale(
                ((current - obs).num_milliseconds() as f64 / 1000.0).max(0.0),
                1000.0,
            )
        });

    let schedule = latest_schedule_snapshot(&root)?;
    let waves = schedule
        .get("executionWaves")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let deferred = schedule
        .get("deferredCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let unschedulable = schedule
        .get("unschedulableCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);

    let readiness = if critical_count > 0 || burn_rate["status"] == "CRITICAL" {
        "CRITICAL"
    } else if !open_risks.is_empty()
        || unschedulable != 0
        || !overall_dr_compliant
        || evidence_sample.is_none()
    {
        "DEGRADED"
    } else {
        "READY"
    };

    let oldest_age = open_ages.into_iter().fold(0.0_f64, f64::max);
    Ok(json!({
        "fleetReadiness": readiness,
        "slo": slo,
        "riskDebt": {
            "total": round_scale(debt_total, 1000.0),
            "openCount": open_risks.len(),
            "critical": critical_count,
            "oldestRiskAgeSeconds": round_scale(oldest_age, 1000.0),
        },
        "scheduler": {
            "scheduleId": schedule.get("scheduleId").cloned().unwrap_or(Value::Null),
            "waves": waves,
            "deferred": deferred,
            "unschedulable": unschedulable,
        },
        "burnRate": burn_rate,
        "evidence": {
            "lastVerifiedAt": evidence_sample.map(|s| json!(s.observed_at)).unwrap_or(Value::Null),
            "proofFreshnessSeconds": proof_freshness,
            "proofSha256": evidence_sample.and_then(|s| s.metadata.get("proofSha256")).cloned().unwrap_or(Value::Null),
            "scenario": evidence_sample.and_then(|s| s.metadata.get("scenario")).cloned().unwrap_or(Value::Null),
        },
        "generatedAt": format_utc_iso(current),
    }))
}
