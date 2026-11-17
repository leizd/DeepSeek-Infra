//! Read-only Resilience routes:
//! - `GET /api/workspace/resilience/journal`
//! - `GET /api/workspace/resilience/capacity-forecast`
//! - `GET /api/workspace/resilience/waves`
//!
//! All three read existing SQLite stores via `open_readonly_sqlite` and never
//! create `.resilience-journal`, `.resilience-capacity`, `.resilience-scheduler`,
//! or `.resilience-waves`.

use std::collections::{BTreeMap, HashMap};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::extract::RawQuery;
use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use percent_encoding::percent_decode_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Value, json};

use crate::backup_catalog_routes::{open_readonly_sqlite, utc_iso_from_unix};
use crate::route_support::{self, unhandled_error};

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/resilience/journal", any(dispatch_journal))
        .route(
            "/api/workspace/resilience/capacity-forecast",
            any(dispatch_capacity_forecast),
        )
        .route("/api/workspace/resilience/waves", any(dispatch_waves))
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

fn table_exists(conn: &Connection, name: &str) -> Result<bool, AppError> {
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
        .query_map(params, |row| {
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
        })
        .map_err(|_| unhandled_error())?;
    let mut results = Vec::new();
    for row in rows {
        results.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(Value::Array(results))
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
