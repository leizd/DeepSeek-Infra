//! Read-only resilience capacity forecast and RPO/RTO optimizer routes:
//! - `GET|HEAD /api/workspace/resilience/forecast` (`capacity_forecaster.forecast_all_targets`)
//! - `GET|HEAD /api/workspace/resilience/optimizer` (`rpo_rto_optimizer.generate_placement_recommendations`)
//!
//! Reads `.backup-targets/`, `.backup-policies/`, `.backup-control/control.sqlite3`,
//! `.backup-dr/evidence.sqlite3`, and `.restore-staging/*/drill-result.json` cleanly
//! without creating directories or writing store records.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use axum::Json;
use axum::Router;
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::OptionalExtension;
use serde_json::{Value, json};

use crate::backup_capacity_routes::{
    estimate_horizon_readonly, now_utc_iso, probe_target_capacity_with_probe, round2,
};
use crate::backup_catalog_routes::{list_registered_targets, open_readonly_sqlite};
use crate::backup_dr_slo_routes::load_drill_items;
use crate::route_support::{self, unhandled_error};

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/resilience/forecast", any(dispatch_forecast))
        .route(
            "/api/workspace/resilience/optimizer",
            any(dispatch_optimizer),
        )
}

async fn dispatch_forecast(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(build_forecast_body).await;
    dispatch_json_or_head(method, joined)
}

async fn dispatch_optimizer(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(build_optimizer_body).await;
    dispatch_json_or_head(method, joined)
}

fn dispatch_json_or_head(
    method: Method,
    joined: Result<Result<Value, AppError>, tokio::task::JoinError>,
) -> Response {
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

fn unconstrained_forecast(target_id: &str, used_bytes: &Value, generated_at: &str) -> Value {
    json!({
        "target": target_id,
        "targetId": target_id,
        "status": "unconstrained",
        "totalBytes": null,
        "currentUsedBytes": used_bytes,
        "currentUsedPercent": null,
        "growthRateBytesPerDay": null,
        "forecast": {
            "7d": {"usedPercent": null, "usedBytes": null, "confidence": 1.0},
            "30d": {"usedPercent": null, "usedBytes": null, "confidence": 1.0},
            "90d": {"usedPercent": null, "usedBytes": null, "confidence": 1.0},
        },
        "estimatedDaysToFull": null,
        "confidence": "unavailable",
        "recommendations": [],
        "generatedAt": generated_at,
    })
}

fn forecast_target_readonly(
    target_id: &str,
    target: &Value,
    control_conn: Option<&rusqlite::Connection>,
    dr_conn: Option<&rusqlite::Connection>,
    generated_at: &str,
) -> Result<Value, AppError> {
    let cap = probe_target_capacity_with_probe(target, target_id, control_conn, dr_conn, false)?;
    let horizon = estimate_horizon_readonly(target_id, &cap, control_conn)?;

    let total_opt = cap.get("totalBytes").and_then(Value::as_i64);
    let free_opt = cap.get("freeBytes").and_then(Value::as_i64);
    let used_val = cap.get("usedBytes").cloned().unwrap_or(Value::Null);
    let (Some(total_bytes), Some(free_bytes)) = (total_opt, free_opt) else {
        return Ok(unconstrained_forecast(target_id, &used_val, generated_at));
    };

    let daily_rate = horizon
        .get("bytesPerDayP50")
        .and_then(Value::as_f64)
        .filter(|val| *val > 0.0)
        .or_else(|| {
            horizon
                .get("dailyIngressEstimateBytes")
                .and_then(Value::as_f64)
        })
        .unwrap_or(0.0);

    let cur_used = used_val
        .as_i64()
        .unwrap_or_else(|| (total_bytes - free_bytes).max(0));
    let cur_pct = if total_bytes > 0 {
        round2((cur_used as f64 / total_bytes as f64) * 100.0)
    } else {
        0.0
    };

    let sample_cnt = horizon
        .get("sampleCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let conf_factor = if sample_cnt >= 10 {
        1.0
    } else if sample_cnt >= 3 {
        0.85
    } else {
        0.60
    };

    let horizons = [(7, "7d", 0.95), (30, "30d", 0.91), (90, "90d", 0.82)];
    let mut forecast_map = serde_json::Map::new();
    for (days, label, base_conf) in horizons {
        let projected_growth = (daily_rate * f64::from(days)) as i64;
        let proj_used = if daily_rate > 0.0 {
            total_bytes.min(cur_used.saturating_add(projected_growth))
        } else {
            cur_used
        };
        let proj_pct = if total_bytes > 0 {
            round2((proj_used as f64 / total_bytes as f64) * 100.0)
        } else {
            0.0
        };
        let effective_conf = round2(base_conf * conf_factor);
        forecast_map.insert(
            label.to_string(),
            json!({
                "usedPercent": proj_pct,
                "usedBytes": proj_used,
                "confidence": effective_conf,
            }),
        );
    }

    let days_to_full = horizon
        .get("estimatedDaysToFull")
        .cloned()
        .unwrap_or(Value::Null);
    let days_int = days_to_full.as_i64();
    let mut recommendations = Vec::new();
    if days_int.is_some_and(|days| days < 30) || cur_pct >= 85.0 {
        recommendations.push(Value::String("CREATE_REBALANCE_JOB".to_string()));
    }
    if days_int.is_some_and(|days| days < 14) || cur_pct >= 90.0 {
        recommendations.push(Value::String("EXPAND_TARGET_STORAGE".to_string()));
    }

    let status = horizon
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("healthy");
    let confidence = horizon
        .get("confidence")
        .and_then(Value::as_str)
        .unwrap_or("medium");

    Ok(json!({
        "target": target_id,
        "targetId": target_id,
        "status": status,
        "totalBytes": total_bytes,
        "currentUsedBytes": cur_used,
        "currentUsedPercent": cur_pct,
        "growthRateBytesPerDay": daily_rate as i64,
        "forecast": Value::Object(forecast_map),
        "estimatedDaysToFull": days_to_full,
        "daysToSoftWatermark": horizon.get("daysToSoftWatermark").cloned().unwrap_or(Value::Null),
        "daysToHardWatermark": horizon.get("daysToHardWatermark").cloned().unwrap_or(Value::Null),
        "confidence": confidence,
        "recommendations": recommendations,
        "generatedAt": generated_at,
    }))
}

fn build_forecast_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let control_db = root.join(".backup-control").join("control.sqlite3");
    let dr_db = root.join(".backup-dr").join("evidence.sqlite3");
    let control_conn = open_readonly_sqlite(&control_db)?;
    let dr_conn = open_readonly_sqlite(&dr_db)?;
    let generated_at = now_utc_iso();

    let registered = list_registered_targets()?;
    let mut target_map = HashMap::new();
    let mut ordered_ids = Vec::with_capacity(registered.len() + 1);
    ordered_ids.push("managed-local".to_string());

    for target in registered {
        let tid = target
            .get("targetId")
            .filter(|value| python_truthy(value))
            .map(value_str)
            .unwrap_or_default();
        if tid.is_empty() {
            continue;
        }
        ordered_ids.push(tid.clone());
        target_map.insert(tid, target);
    }

    let mut seen = HashSet::new();
    let mut forecasts = Vec::new();
    let mut overall_status = "healthy";
    let default_local = json!({"targetId": "managed-local", "kind": "managed"});

    for tid in ordered_ids {
        if tid.is_empty() || !seen.insert(tid.clone()) {
            continue;
        }
        let target = target_map.get(&tid).unwrap_or(&default_local);
        let fc = forecast_target_readonly(
            &tid,
            target,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            &generated_at,
        )?;
        let stat = fc.get("status").and_then(Value::as_str).unwrap_or("");
        if stat == "critical" {
            overall_status = "critical";
        } else if stat == "degraded" && overall_status != "critical" {
            overall_status = "degraded";
        }
        forecasts.push(fc);
    }

    Ok(json!({
        "overallStatus": overall_status,
        "targets": forecasts,
        "generatedAt": generated_at,
    }))
}

fn list_policies_readonly(root: &Path) -> Result<Vec<Value>, AppError> {
    let mut by_id: BTreeMap<String, Value> = BTreeMap::new();
    let policy_dir = root.join(".backup-policies");
    if policy_dir.is_dir() {
        let mut entries: Vec<_> = std::fs::read_dir(&policy_dir)
            .map_err(|_| unhandled_error())?
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
            .collect();
        entries.sort();
        for path in entries {
            let Ok(text) = std::fs::read_to_string(&path) else {
                continue;
            };
            let Ok(parsed) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            let Some(pid) = parsed
                .get("policyId")
                .filter(|value| python_truthy(value))
                .map(value_str)
            else {
                continue;
            };
            if !pid.is_empty() {
                by_id.insert(pid, parsed);
            }
        }
    }

    let control_db = root.join(".backup-control").join("control.sqlite3");
    if let Some(conn) = open_readonly_sqlite(&control_db)? {
        let table: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'control_policies'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        if table.is_some() {
            let mut stmt = conn
                .prepare("SELECT payload_json FROM control_policies ORDER BY policy_id")
                .map_err(|_| unhandled_error())?;
            let rows = stmt
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(|_| unhandled_error())?;
            for row in rows {
                let raw = row.map_err(|_| unhandled_error())?;
                let parsed: Value = serde_json::from_str(&raw).map_err(|_| unhandled_error())?;
                if let Some(pid) = parsed
                    .get("policyId")
                    .filter(|value| python_truthy(value))
                    .map(value_str)
                {
                    if !pid.is_empty() {
                        by_id.insert(pid, parsed);
                    }
                }
            }
        }
    }

    Ok(by_id.into_values().collect())
}

fn build_optimizer_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let drills = load_drill_items(&root)?;
    let policies = list_policies_readonly(&root)?;
    let generated_at = now_utc_iso();

    let mut target_stats: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for item in &drills {
        let tid = item
            .get("targetId")
            .filter(|value| python_truthy(value))
            .map(value_str)
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "managed-local".to_string());
        let raw_dur = item
            .get("durationMs")
            .filter(|value| python_truthy(value))
            .or_else(|| {
                item.get("restoreDurationMs")
                    .filter(|value| python_truthy(value))
            });
        if let Some(dur_ms) = raw_dur.and_then(Value::as_f64) {
            if dur_ms > 0.0 {
                target_stats.entry(tid).or_default().push(dur_ms / 1000.0);
            }
        }
    }

    let mut performance = serde_json::Map::new();
    let mut ranked: Vec<(String, f64, usize)> = Vec::new();
    for (tid, mut latencies) in target_stats {
        latencies.sort_by(f64::total_cmp);
        let len = latencies.len();
        let p50 = latencies[len / 2];
        let p90_idx = ((len as f64) * 0.9) as usize;
        let p90 = latencies[p90_idx.min(len - 1)];
        let p50_r = round2(p50);
        let p90_r = round2(p90);
        performance.insert(
            tid.clone(),
            json!({
                "sampleCount": len,
                "p50LatencySeconds": p50_r,
                "p90LatencySeconds": p90_r,
                "minLatencySeconds": round2(latencies[0]),
                "maxLatencySeconds": round2(latencies[len - 1]),
            }),
        );
        if len >= 2 {
            ranked.push((tid, p90_r, len));
        }
    }

    ranked.sort_by(|left, right| left.1.total_cmp(&right.1));
    let mut recommendations = Vec::new();
    if ranked.len() >= 2 {
        let (fastest_tid, fastest_rto, _) = &ranked[0];
        let (slowest_tid, slowest_rto, _) = &ranked[ranked.len() - 1];
        if *slowest_rto > (*fastest_rto) * 1.5 {
            for policy in &policies {
                let pid = policy
                    .get("policyId")
                    .filter(|value| python_truthy(value))
                    .map(value_str)
                    .unwrap_or_default();
                let pol_target = policy
                    .get("targetId")
                    .filter(|value| python_truthy(value))
                    .map(value_str)
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| "managed-local".to_string());
                if pol_target == *slowest_tid {
                    recommendations.push(json!({
                        "type": "PREFERRED_RESTORE_TARGET_ADVISORY",
                        "policyId": pid,
                        "currentPrimaryTarget": slowest_tid,
                        "recommendedTarget": fastest_tid,
                        "reason": format!(
                            "Target '{fastest_tid}' demonstrates lower P90 restore latency ({fastest_rto:.1}s) vs '{slowest_tid}' ({slowest_rto:.1}s)"
                        ),
                        "currentP90RtoSeconds": slowest_rto,
                        "projectedP90RtoSeconds": fastest_rto,
                        "confidence": "high",
                    }));
                }
            }
        }
    }

    Ok(json!({
        "optimizerVersion": 1,
        "generatedAt": generated_at,
        "recommendations": recommendations,
        "targetPerformance": Value::Object(performance),
    }))
}
