//! Read-only `GET /api/workspace/backup-capacity/summary` (`backup_capacity.capacity_summary`).
//!
//! Computes target capacity and exhaustion-horizon summaries purely from
//! persisted target records, local filesystem stat calls, physical object
//! accounting in `.backup-control/control.sqlite3` (falling back to
//! `.backup-dr/evidence.sqlite3`), and `capacity_growth_observations`. Never
//! creates `.backup-control/`, mutates SQLite, or calls remote S3 endpoints.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Value, json};

use crate::backup_catalog_routes::{
    list_registered_targets, open_readonly_sqlite, parse_python_iso, utc_iso_from_unix,
};
use crate::route_support::{self, unhandled_error};

const DEFAULT_SOFT_WATERMARK_PERCENT: f64 = 80.0;
const DEFAULT_HARD_WATERMARK_PERCENT: f64 = 90.0;

pub fn router() -> Router {
    Router::new().route("/api/workspace/backup-capacity/summary", any(dispatch))
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

pub(crate) fn now_utc_iso() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    utc_iso_from_unix(secs)
}

pub(crate) fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

fn sqlite_table_exists(conn: &Connection, table: &str) -> Result<bool, AppError> {
    let found: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            [table],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(found.is_some())
}

#[cfg(windows)]
fn filesystem_disk_usage(path: &Path) -> Option<(u64, u64, u64)> {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetDiskFreeSpaceExW(
            lp_directory_name: *const u16,
            lp_free_bytes_available_to_caller: *mut u64,
            lp_total_number_of_bytes: *mut u64,
            lp_total_number_of_free_bytes: *mut u64,
        ) -> i32;
    }
    let wide: Vec<u16> = OsStr::new(path)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut avail: u64 = 0;
    let mut total: u64 = 0;
    let mut total_free: u64 = 0;
    let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut avail, &mut total, &mut total_free) };
    if ok == 0 || total == 0 {
        return None;
    }
    Some((total, total.saturating_sub(total_free), avail))
}

#[cfg(unix)]
fn filesystem_disk_usage(path: &Path) -> Option<(u64, u64, u64)> {
    let output = std::process::Command::new("df")
        .args(["-Pk", "--"])
        .arg(path)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text.lines().skip(1).find(|row| !row.trim().is_empty())?;
    let cols: Vec<&str> = line.split_whitespace().collect();
    if cols.len() < 5 {
        return None;
    }
    let total_kb: u64 = cols[cols.len() - 5].parse().ok()?;
    let used_kb: u64 = cols[cols.len() - 4].parse().ok()?;
    let avail_kb: u64 = cols[cols.len() - 3].parse().ok()?;
    Some((
        total_kb.saturating_mul(1024),
        used_kb.saturating_mul(1024),
        avail_kb.saturating_mul(1024),
    ))
}

fn physical_usage_from_control(
    control_conn: Option<&Connection>,
    target_id: &str,
) -> Result<(u64, u64, u64, usize, &'static str), AppError> {
    let Some(conn) = control_conn else {
        return Ok((0, 0, 0, 0, "unavailable"));
    };
    if !sqlite_table_exists(conn, "target_objects")? {
        return Ok((0, 0, 0, 0, "unavailable"));
    }
    let mut stmt = conn
        .prepare(
            "SELECT object_key, state, live_ref_count, retired_ref_count, size_bytes, \
                    ciphertext_digest, COALESCE(is_physical, 1) AS is_physical \
             FROM target_objects WHERE target_id = ?",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([target_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, i64>(6)?,
            ))
        })
        .map_err(|_| unhandled_error())?;

    let mut by_digest: HashMap<String, (u64, bool, bool)> = HashMap::new();
    let mut physical_rows = 0usize;
    for row in rows {
        let (object_key, state, live_ref, retired_ref, size_bytes, digest_opt, is_physical) =
            row.map_err(|_| unhandled_error())?;
        if is_physical != 1 {
            continue;
        }
        physical_rows += 1;
        let size = size_bytes.max(0) as u64;
        let digest = match digest_opt {
            Some(text) if !text.is_empty() => text,
            _ => object_key,
        };
        let live = live_ref > 0;
        let retired =
            !live && (retired_ref > 0 || state == "retired-pending-gc" || state == "gc-candidate");
        by_digest
            .entry(digest)
            .and_modify(|entry| {
                entry.0 = entry.0.max(size);
                let combined_live = entry.1 || live;
                let combined_retired = (entry.2 || retired) && !combined_live;
                entry.1 = combined_live;
                entry.2 = combined_retired;
            })
            .or_insert((size, live, retired));
    }

    let mut physical = 0u64;
    let mut live_bytes = 0u64;
    let mut retired_pending = 0u64;
    for (size, live, retired) in by_digest.values() {
        physical = physical.saturating_add(*size);
        if *live {
            live_bytes = live_bytes.saturating_add(*size);
        } else if *retired {
            retired_pending = retired_pending.saturating_add(*size);
        }
    }
    let confidence = if physical_rows > 0 {
        "high"
    } else {
        "unavailable"
    };
    Ok((
        physical,
        live_bytes,
        retired_pending,
        physical_rows,
        confidence,
    ))
}

fn recovery_copy_usage_fallback(
    dr_conn: Option<&Connection>,
    target_id: &str,
) -> Result<(u64, u64, u64), AppError> {
    let Some(conn) = dr_conn else {
        return Ok((0, 0, 0));
    };
    if !sqlite_table_exists(conn, "recovery_point_copies")? {
        return Ok((0, 0, 0));
    }
    let mut stmt = conn
        .prepare(
            "SELECT backup_id, object_set_digest, recoverable, metadata_json \
             FROM recovery_point_copies WHERE target_id = ? LIMIT 5000",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([target_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        })
        .map_err(|_| unhandled_error())?;

    let mut seen_digests = HashSet::new();
    let mut used = 0u64;
    let mut live_bytes = 0u64;
    let mut retired_pending = 0u64;
    for row in rows {
        let (backup_id, obj_digest, recoverable, metadata_json) =
            row.map_err(|_| unhandled_error())?;
        let meta: Value = metadata_json
            .as_deref()
            .and_then(|text| serde_json::from_str(text).ok())
            .unwrap_or(Value::Null);
        let digest = obj_digest
            .filter(|text| !text.is_empty())
            .or_else(|| {
                meta.get("objectSetDigest")
                    .and_then(Value::as_str)
                    .filter(|text| !text.is_empty())
                    .map(str::to_string)
            })
            .unwrap_or(backup_id);
        if !digest.is_empty() && !seen_digests.insert(digest) {
            continue;
        }
        let sz = meta
            .get("physicalBytes")
            .or_else(|| meta.get("ciphertextBytes"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if sz > 0 {
            used = used.saturating_add(sz);
            if recoverable == 0 {
                retired_pending = retired_pending.saturating_add(sz);
            } else {
                live_bytes = live_bytes.saturating_add(sz);
            }
        }
    }
    Ok((used, live_bytes, retired_pending))
}

fn target_capacity_observation(
    control_conn: Option<&Connection>,
    target_id: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = control_conn else {
        return Ok(None);
    };
    if !sqlite_table_exists(conn, "target_capacity_observations")? {
        return Ok(None);
    }
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload_json FROM target_capacity_observations WHERE target_id = ?",
            [target_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    let Some(text) = raw else {
        return Ok(None);
    };
    Ok(serde_json::from_str::<Value>(&text)
        .ok()
        .filter(Value::is_object))
}

pub(crate) fn probe_target_capacity_readonly(
    target: &Value,
    target_id: &str,
    control_conn: Option<&Connection>,
    dr_conn: Option<&Connection>,
) -> Result<Value, AppError> {
    probe_target_capacity_with_probe(target, target_id, control_conn, dr_conn, true)
}

pub(crate) fn probe_target_capacity_with_probe(
    target: &Value,
    target_id: &str,
    control_conn: Option<&Connection>,
    dr_conn: Option<&Connection>,
    probe: bool,
) -> Result<Value, AppError> {
    let now = now_utc_iso();
    let kind = target
        .get("kind")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_else(|| "filesystem".to_string());
    let path_str = target
        .get("path")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();

    if probe && (kind == "filesystem" || !path_str.is_empty()) {
        let dir = Path::new(&path_str);
        if dir.is_dir() {
            if let Some((total, used, free)) = filesystem_disk_usage(dir) {
                let free_pct = if total > 0 {
                    round2((free as f64 / total as f64) * 100.0)
                } else {
                    0.0
                };
                return Ok(json!({
                    "targetId": target_id,
                    "totalBytes": total,
                    "usedBytes": used,
                    "freeBytes": free,
                    "freePercent": free_pct,
                    "observedAt": now,
                    "source": "filesystem",
                }));
            }
        }
    }

    let quota_opt = target
        .get("quotaBytes")
        .and_then(Value::as_i64)
        .filter(|quota| *quota > 0)
        .map(|quota| quota as u64);

    if let Some(quota) = quota_opt {
        if kind == "s3" {
            if let Some(obs) = target_capacity_observation(control_conn, target_id)? {
                return Ok(obs);
            }
            return Ok(json!({
                "targetId": target_id,
                "totalBytes": quota,
                "usedBytes": null,
                "freeBytes": null,
                "freePercent": null,
                "observedAt": now,
                "source": "s3-object-inventory-unavailable",
                "error": "RuntimeError: read-only capacity summary cannot query remote S3 inventory",
            }));
        }
        let (mut physical, mut live_bytes, mut retired_pending, object_count, mut confidence) =
            physical_usage_from_control(control_conn, target_id)?;
        if confidence == "unavailable" || object_count == 0 {
            let (fallback_used, fallback_live, fallback_retired) =
                recovery_copy_usage_fallback(dr_conn, target_id)?;
            physical = fallback_used;
            live_bytes = fallback_live;
            retired_pending = fallback_retired;
            confidence = "low";
        }
        let free = quota.saturating_sub(physical);
        let free_pct = round2((free as f64 / quota as f64) * 100.0);
        return Ok(json!({
            "targetId": target_id,
            "totalBytes": quota,
            "usedBytes": physical,
            "freeBytes": free,
            "freePercent": free_pct,
            "physicalStoredBytes": physical,
            "liveReferencedBytes": live_bytes,
            "retiredPendingGcBytes": retired_pending,
            "controlPlaneBytes": 0,
            "unknownExternalBytes": null,
            "capacityConfidence": confidence,
            "observedAt": now,
            "source": if confidence == "high" {
                "physical-object-index"
            } else {
                "configured-quota-physical-estimate"
            },
        }));
    }

    Ok(json!({
        "targetId": target_id,
        "totalBytes": null,
        "usedBytes": null,
        "freeBytes": null,
        "freePercent": null,
        "physicalStoredBytes": null,
        "liveReferencedBytes": null,
        "retiredPendingGcBytes": null,
        "observedAt": now,
        "source": "unknown",
    }))
}

fn growth_observations_from_control(
    control_conn: Option<&Connection>,
    target_id: &str,
) -> Result<Vec<(i64, i64)>, AppError> {
    let Some(conn) = control_conn else {
        return Ok(Vec::new());
    };
    if !sqlite_table_exists(conn, "capacity_growth_observations")? {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT observed_at, physical_stored_bytes \
             FROM capacity_growth_observations \
             WHERE target_id = ? \
             ORDER BY observed_at DESC, observation_id DESC \
             LIMIT 60",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([target_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|_| unhandled_error())?;

    let mut points = Vec::new();
    for row in rows {
        let (observed_at, stored) = row.map_err(|_| unhandled_error())?;
        let text = observed_at.trim().replace('Z', "+00:00");
        if let Ok(Some(epoch)) = parse_python_iso(&text) {
            points.push((epoch, stored));
        }
    }
    points.sort_by_key(|pair| pair.0);
    Ok(points)
}

pub(crate) fn estimate_horizon_readonly(
    target_id: &str,
    cap: &Value,
    control_conn: Option<&Connection>,
) -> Result<Value, AppError> {
    let free_opt = cap.get("freeBytes").and_then(Value::as_i64);
    let total_opt = cap.get("totalBytes").and_then(Value::as_i64);
    let free_pct_opt = cap.get("freePercent").and_then(Value::as_f64);

    let (Some(free_bytes), Some(total_bytes)) = (free_opt, total_opt) else {
        return Ok(json!({
            "targetId": target_id,
            "status": "unconstrained",
            "freePercent": null,
            "estimatedDaysToFull": 9999,
            "confidence": "unavailable",
            "forecastStatus": "unconstrained",
        }));
    };

    if free_bytes <= 0 {
        return Ok(json!({
            "targetId": target_id,
            "status": "critical",
            "freeBytes": free_bytes,
            "totalBytes": total_bytes,
            "freePercent": free_pct_opt.unwrap_or(0.0),
            "dailyIngressEstimateBytes": null,
            "bytesPerDayP50": null,
            "bytesPerDayP90": null,
            "estimatedDaysToFull": 0,
            "daysToSoftWatermark": 0,
            "daysToHardWatermark": 0,
            "confidence": "high",
            "forecastStatus": "exhausted",
        }));
    }

    let mut status = "healthy";
    if let Some(pct) = free_pct_opt {
        if pct < 10.0 {
            status = "critical";
        } else if pct < 20.0 {
            status = "degraded";
        }
    }

    let points = growth_observations_from_control(control_conn, target_id)?;
    if points.len() < 2 {
        return Ok(json!({
            "targetId": target_id,
            "status": status,
            "freeBytes": free_bytes,
            "totalBytes": total_bytes,
            "freePercent": free_pct_opt,
            "dailyIngressEstimateBytes": null,
            "bytesPerDayP50": null,
            "bytesPerDayP90": null,
            "estimatedDaysToFull": null,
            "daysToSoftWatermark": null,
            "daysToHardWatermark": null,
            "confidence": "unavailable",
            "forecastStatus": "unavailable",
        }));
    }

    let oldest = points[0];
    let newest = points[points.len() - 1];
    let elapsed_seconds = ((newest.0 - oldest.0) as f64).max(1.0);
    let elapsed_days = elapsed_seconds / 86_400.0;
    let mut daily_samples = Vec::with_capacity(points.len() - 1);
    for idx in 1..points.len() {
        let prev = points[idx - 1];
        let cur = points[idx];
        let secs = ((cur.0 - prev.0) as f64).max(1.0);
        daily_samples.push(((cur.1 - prev.1) as f64) * 86_400.0 / secs);
    }
    daily_samples.sort_by(f64::total_cmp);
    let p50 = daily_samples[daily_samples.len() / 2].max(0.0);
    let p90_idx = ((daily_samples.len() as f64 * 0.9) as usize).min(daily_samples.len() - 1);
    let p90 = daily_samples[p90_idx].max(0.0);
    let confidence = if points.len() >= 10 && elapsed_days >= 7.0 {
        "high"
    } else if points.len() >= 3 && elapsed_days >= 1.0 {
        "medium"
    } else {
        "low"
    };

    let daily_rate = p50.max(0.0);
    let (days_to_full, days_soft, days_hard, forecast_status) = if daily_rate <= 0.0 {
        (None, None, None, "stable-or-shrinking")
    } else {
        let d_full = (((free_bytes as f64) / daily_rate).floor() as i64).max(0);
        let soft_free =
            ((total_bytes as f64) * (1.0 - DEFAULT_SOFT_WATERMARK_PERCENT / 100.0)).max(0.0);
        let hard_free =
            ((total_bytes as f64) * (1.0 - DEFAULT_HARD_WATERMARK_PERCENT / 100.0)).max(0.0);
        let d_soft =
            ((((free_bytes as f64 - soft_free).max(0.0)) / daily_rate).floor() as i64).max(0);
        let d_hard =
            ((((free_bytes as f64 - hard_free).max(0.0)) / daily_rate).floor() as i64).max(0);
        if d_full < 7 {
            status = "critical";
        } else if d_full < 30 && status == "healthy" {
            status = "degraded";
        }
        (Some(d_full), Some(d_soft), Some(d_hard), "ok")
    };

    Ok(json!({
        "targetId": target_id,
        "status": status,
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "freePercent": free_pct_opt,
        "dailyIngressEstimateBytes": if daily_rate > 0.0 { daily_rate as i64 } else { 0 },
        "bytesPerDayP50": p50 as i64,
        "bytesPerDayP90": p90 as i64,
        "estimatedDaysToFull": days_to_full,
        "daysToSoftWatermark": days_soft,
        "daysToHardWatermark": days_hard,
        "confidence": confidence,
        "forecastStatus": forecast_status,
        "sampleCount": points.len(),
        "elapsedDays": elapsed_days,
        "updatedAt": now_utc_iso(),
    }))
}

pub(crate) fn build_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let control_db = root.join(".backup-control").join("control.sqlite3");
    let dr_db = root.join(".backup-dr").join("evidence.sqlite3");
    let control_conn = open_readonly_sqlite(&control_db)?;
    let dr_conn = open_readonly_sqlite(&dr_db)?;

    let mut targets = list_registered_targets()?;
    targets.sort_by(|left, right| {
        let lid = left.get("targetId").and_then(Value::as_str).unwrap_or("");
        let rid = right.get("targetId").and_then(Value::as_str).unwrap_or("");
        lid.cmp(rid)
    });
    let mut target_summaries = Vec::with_capacity(targets.len());
    let mut overall_status = "healthy";

    for target in targets {
        let tid = target
            .get("targetId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let cap =
            probe_target_capacity_readonly(&target, &tid, control_conn.as_ref(), dr_conn.as_ref())?;
        let horizon = estimate_horizon_readonly(&tid, &cap, control_conn.as_ref())?;
        let horizon_status = horizon
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("healthy");
        if horizon_status == "critical" {
            overall_status = "critical";
        } else if horizon_status == "degraded" && overall_status != "critical" {
            overall_status = "degraded";
        }
        target_summaries.push(json!({
            "targetId": tid,
            "label": target.get("label").cloned().unwrap_or(Value::Null),
            "kind": target.get("kind").cloned().unwrap_or(Value::Null),
            "capacity": cap,
            "horizon": horizon,
        }));
    }

    Ok(json!({
        "overallStatus": overall_status,
        "targets": target_summaries,
    }))
}
