//! Read-only `GET /api/workspace/disaster-recovery/slo` (`backup_dr_readiness.get_dr_slo_status`).
//!
//! Reads `.backup-dr/evidence.sqlite3` (`drill_evidence`), `.restore-staging/*/drill-result.json`,
//! and `.backups/catalog/catalog-v1.jsonl` (`catalog_state`) without creating any directories or
//! mutating SQLite.

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::OptionalExtension;
use serde_json::{Value, json};

use crate::backup_catalog_routes::{
    catalog_state, open_readonly_sqlite, parse_python_iso, utc_iso_from_unix,
};
use crate::route_support::{self, unhandled_error};

pub fn router() -> Router {
    Router::new().route("/api/workspace/disaster-recovery/slo", any(dispatch))
}

async fn dispatch(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(build_body).await;
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => axum::http::StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => {
            axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

fn now_unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn round_places(value: f64, factor: f64) -> f64 {
    (value * factor).round() / factor
}

fn percentile(values: &[f64], p: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let k = ((sorted.len() - 1) as f64) * p;
    let f = k.floor() as usize;
    let c = (f + 1).min(sorted.len() - 1);
    let d = k - (f as f64);
    sorted[f] + d * (sorted[c] - sorted[f])
}

fn json_number_as_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Number(num) => num.as_f64(),
        _ => None,
    }
}

fn parse_dr_time(value: Option<&Value>) -> Option<i64> {
    let raw = value?.as_str()?;
    if raw.is_empty() || raw == "not-a-time" {
        return None;
    }
    let cleaned = raw.replace('Z', "+00:00");
    let has_tz = cleaned.contains('+') || (cleaned.len() > 10 && cleaned[10..].contains('-'));
    if !has_tz {
        return None;
    }
    match parse_python_iso(&cleaned) {
        Ok(Some(epoch)) => Some(epoch),
        _ => None,
    }
}

fn load_drill_items(root: &Path) -> Result<Vec<Value>, AppError> {
    let mut results = Vec::new();
    let dr_db = root.join(".backup-dr").join("evidence.sqlite3");
    if let Some(conn) = open_readonly_sqlite(&dr_db)? {
        let table: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'drill_evidence'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        if table.is_some() {
            let mut stmt = conn
                .prepare(
                    "SELECT drill_id, target_id, policy_id, backup_id, observed_at, result, duration_ms, details_json \
                     FROM drill_evidence ORDER BY observed_at DESC LIMIT 100",
                )
                .map_err(|_| unhandled_error())?;
            let rows = stmt
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, i64>(6)?,
                        row.get::<_, Option<String>>(7)?,
                    ))
                })
                .map_err(|_| unhandled_error())?;
            for row in rows {
                let (
                    drill_id,
                    target_id,
                    policy_id,
                    backup_id,
                    observed_at,
                    result,
                    duration_ms,
                    details_json,
                ) = row.map_err(|_| unhandled_error())?;
                let details = details_json
                    .as_deref()
                    .and_then(|text| serde_json::from_str::<Value>(text).ok())
                    .unwrap_or_else(|| json!({}));
                results.push(json!({
                    "drillId": drill_id,
                    "targetId": target_id,
                    "policyId": policy_id,
                    "backupId": backup_id,
                    "observedAt": observed_at,
                    "result": result,
                    "durationMs": duration_ms,
                    "details": details,
                }));
            }
        }
    }

    let staging_root = root.join(".restore-staging");
    if staging_root.is_dir() {
        let mut entries: Vec<_> = std::fs::read_dir(&staging_root)
            .map_err(|_| unhandled_error())?
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.is_dir())
            .collect();
        entries.sort();
        for dir in entries {
            let result_file = dir.join("drill-result.json");
            if !result_file.is_file() {
                continue;
            }
            if let Ok(text) = std::fs::read_to_string(&result_file) {
                if let Ok(parsed) = serde_json::from_str::<Value>(&text) {
                    if parsed.is_object() {
                        results.push(parsed);
                    }
                }
            }
        }
    }
    Ok(results)
}

fn build_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let now_secs = now_unix_secs();
    let drill_items = load_drill_items(&root)?;
    let total_drills = drill_items.len();

    let is_success = |item: &Value| {
        item.get("result").and_then(Value::as_str) == Some("success")
            || item.get("status").and_then(Value::as_str) == Some("success")
    };
    let successful_drills = drill_items.iter().filter(|item| is_success(item)).count();
    let success_rate = if total_drills > 0 {
        (successful_drills as f64) / (total_drills as f64)
    } else {
        1.0
    };

    let mut durations_sec = Vec::new();
    for item in &drill_items {
        let dur_ms = item
            .get("proof")
            .and_then(|proof| proof.get("restoreDurationMs"))
            .and_then(json_number_as_f64);
        if let Some(ms) = dur_ms {
            durations_sec.push(ms / 1000.0);
        } else if let Some(rto) = item.get("rtoSeconds").filter(|val| !val.is_null()) {
            let parsed = match rto {
                Value::Number(num) => num.as_f64(),
                Value::String(text) => text.trim().parse::<f64>().ok(),
                _ => None,
            };
            if let Some(val) = parsed {
                durations_sec.push(val);
            }
        }
    }
    if durations_sec.is_empty() {
        durations_sec.push(1.0);
    }

    let rto_p50 = percentile(&durations_sec, 0.50);
    let rto_p95 = percentile(&durations_sec, 0.95);
    let rto_p99 = percentile(&durations_sec, 0.99);

    let mut rpo_seconds = 0.0f64;
    let backup_dir = root.join(".backups");
    let cat = catalog_state(&backup_dir)?;
    if let Some((_, latest_b)) = cat.iter().last() {
        let created = latest_b
            .get("createdAt")
            .filter(|val| python_truthy(val))
            .or_else(|| latest_b.get("timestamp").filter(|val| python_truthy(val)));
        if let Some(created_epoch) = parse_dr_time(created) {
            rpo_seconds = ((now_secs as i64 - created_epoch) as f64).max(0.0);
        } else if let Some(raw) = created.filter(|val| val.is_number() || val.is_string()) {
            let _ = value_str(raw);
        }
    }

    let mut latest_success_epoch: Option<i64> = None;
    for item in &drill_items {
        if !is_success(item) {
            continue;
        }
        let obs = item
            .get("observedAt")
            .filter(|val| python_truthy(val))
            .or_else(|| item.get("completedAt").filter(|val| python_truthy(val)));
        if let Some(epoch) = parse_dr_time(obs) {
            latest_success_epoch = Some(match latest_success_epoch {
                Some(prev) => prev.max(epoch),
                None => epoch,
            });
        }
    }

    let freshness_days = if let Some(latest) = latest_success_epoch {
        ((now_secs as i64 - latest) as f64).max(0.0) / 86_400.0
    } else if total_drills == 0 {
        0.0
    } else {
        999.0
    };

    let freshness_healthy = freshness_days <= 7.0;
    let success_rate_healthy = success_rate >= 0.999 || total_drills == 0;

    Ok(json!({
        "restoreSuccessRate": round_places(success_rate, 10_000.0),
        "targetSuccessRate": 0.999,
        "successRateHealthy": success_rate_healthy,
        "rtoSeconds": {
            "p50": round_places(rto_p50, 1_000.0),
            "p95": round_places(rto_p95, 1_000.0),
            "p99": round_places(rto_p99, 1_000.0),
        },
        "rpoSeconds": round_places(rpo_seconds, 10.0),
        "evidenceFreshnessDays": round_places(freshness_days, 100.0),
        "targetFreshnessDays": 7.0,
        "freshnessHealthy": freshness_healthy,
        "totalDrillsTested": total_drills,
        "overallSloCompliant": success_rate_healthy && freshness_healthy,
        "evaluatedAt": utc_iso_from_unix(now_secs),
    }))
}
