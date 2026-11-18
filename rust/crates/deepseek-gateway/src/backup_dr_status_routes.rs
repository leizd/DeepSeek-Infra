//! Read-only `GET /api/workspace/disaster-recovery/status`
//! (`backup_dr_readiness.readiness_status`).
//!
//! Aggregates per-`(targetId, policyId)` scope readiness, target health,
//! local ciphertext cache health, recovery keeper lease health, topology,
//! capacity horizon, and transfer-control projections purely from persisted
//! JSON/SQLite state and local filesystem metadata. Never creates missing
//! directories or mutates SQLite on read.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;

use axum::Json;
use axum::Router;
use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::backup_retention::chrono::Utc;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_capacity_routes::{
    estimate_horizon_readonly, probe_target_capacity_with_probe, round2,
};
use crate::backup_catalog_routes::{
    list_registered_targets, open_readonly_sqlite, utc_iso_from_unix,
};
use crate::backup_continuity_routes::read_continuity_state;
use crate::backup_dr_slo_routes::{now_unix_secs, parse_dr_time, percentile};
use crate::resilience_advisory_routes::list_policies_readonly;
use crate::resilience_read_routes::table_exists;
use crate::route_support::{self, unhandled_error};

const DEFAULT_CACHE_QUOTA_BYTES: u64 = 20 * 1024 * 1024 * 1024;
const DEFAULT_TRANSFER_SPEED: f64 = 35.0 * 1024.0 * 1024.0;
const DEFAULT_CRYPTO_SPEED: f64 = 70.0 * 1024.0 * 1024.0;
const DEFAULT_MATERIALIZE_SPEED: f64 = 140.0 * 1024.0 * 1024.0;
const MIN_OVERHEAD_SECONDS: f64 = 2.0;

const DEFAULT_GLOBAL_BYTES_PER_SECOND: i64 = 200 * 1024 * 1024;
const DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC: i64 = 100 * 1024 * 1024;
const DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC: i64 = 100 * 1024 * 1024;

pub fn router() -> Router {
    Router::new().route("/api/workspace/disaster-recovery/status", any(dispatch))
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

pub(crate) fn table_columns(conn: &Connection, table: &str) -> Result<HashSet<String>, AppError> {
    if !table_exists(conn, table)? {
        return Ok(HashSet::new());
    }
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| unhandled_error())?;
    let mut cols = HashSet::new();
    for row in rows {
        cols.insert(row.map_err(|_| unhandled_error())?);
    }
    Ok(cols)
}

fn list_scopes(dr_conn: Option<&Connection>) -> Result<Vec<(String, String)>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(Vec::new());
    };
    if !table_exists(conn, "recovery_points")? {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT target_id, policy_id FROM recovery_points \
             ORDER BY target_id, policy_id",
        )
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|_| unhandled_error())?;
    let mut scopes = Vec::new();
    for row in rows {
        scopes.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(scopes)
}

#[derive(Clone)]
struct RecoveryPointRow {
    target_id: String,
    policy_id: String,
    backup_id: String,
    committed_at: String,
    snapshot_kind: String,
    parent_backup_id: Option<String>,
    ciphertext_bytes: i64,
    logical_bytes: i64,
    recoverable: bool,
    storage_protocol: Option<String>,
}

fn list_recovery_points_for_scope(
    conn: &Connection,
    target_id: &str,
    policy_id: Option<&str>,
    limit: i64,
) -> Result<Vec<RecoveryPointRow>, AppError> {
    if !table_exists(conn, "recovery_points")? {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    let base_sql = "SELECT target_id, policy_id, backup_id, committed_at, snapshot_kind, \
                    parent_backup_id, ciphertext_bytes, logical_bytes, recoverable, storage_protocol \
                    FROM recovery_points WHERE target_id = ?";
    if let Some(pid) = policy_id.filter(|s| !s.is_empty()) {
        let mut stmt = conn
            .prepare(&format!(
                "{base_sql} AND policy_id = ? ORDER BY committed_at DESC LIMIT ?"
            ))
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map(rusqlite::params![target_id, pid, limit], map_rp_row)
            .map_err(|_| unhandled_error())?;
        for row in rows {
            out.push(row.map_err(|_| unhandled_error())?);
        }
    } else {
        let mut stmt = conn
            .prepare(&format!("{base_sql} ORDER BY committed_at DESC LIMIT ?"))
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map(rusqlite::params![target_id, limit], map_rp_row)
            .map_err(|_| unhandled_error())?;
        for row in rows {
            out.push(row.map_err(|_| unhandled_error())?);
        }
    }
    Ok(out)
}

fn map_rp_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<RecoveryPointRow> {
    Ok(RecoveryPointRow {
        target_id: row.get(0)?,
        policy_id: row.get(1)?,
        backup_id: row.get(2)?,
        committed_at: row.get(3)?,
        snapshot_kind: row.get(4)?,
        parent_backup_id: row.get::<_, Option<String>>(5)?.filter(|s| !s.is_empty()),
        ciphertext_bytes: row.get(6)?,
        logical_bytes: row.get(7)?,
        recoverable: row.get::<_, i64>(8)? != 0,
        storage_protocol: row.get::<_, Option<String>>(9)?.filter(|s| !s.is_empty()),
    })
}

fn resolve_recoverable_chain(
    conn: &Connection,
    target_id: &str,
    policy_id: &str,
    head_backup_id: &str,
) -> Result<Option<Vec<RecoveryPointRow>>, AppError> {
    let all_points = list_recovery_points_for_scope(conn, target_id, Some(policy_id), 2000)?;
    let by_id: HashMap<String, RecoveryPointRow> = all_points
        .into_iter()
        .map(|item| (item.backup_id.clone(), item))
        .collect();
    let mut chain = Vec::new();
    let mut seen = HashSet::new();
    let mut current_id = Some(head_backup_id.to_string());

    while let Some(cid) = current_id {
        if !seen.insert(cid.clone()) {
            return Ok(None);
        }
        let Some(point) = by_id.get(&cid) else {
            return Ok(None);
        };
        if !point.recoverable {
            return Ok(None);
        }
        chain.push(point.clone());
        match &point.parent_backup_id {
            Some(parent) => current_id = Some(parent.clone()),
            None => {
                if point.snapshot_kind == "incremental" {
                    return Ok(None);
                }
                break;
            }
        }
    }
    chain.reverse();
    Ok(Some(chain))
}

pub(crate) fn latest_recoverable_point(
    dr_conn: Option<&Connection>,
    target_id: &str,
    policy_id: &str,
    now_iso: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(None);
    };
    if !table_exists(conn, "recovery_points")? {
        return Ok(None);
    }
    let mut candidates = Vec::new();
    if policy_id.is_empty() {
        let mut stmt = conn
            .prepare(
                "SELECT backup_id, policy_id, committed_at, snapshot_kind, storage_protocol \
                 FROM recovery_points \
                 WHERE target_id = ? AND recoverable = 1 AND committed_at <= ? \
                 ORDER BY committed_at DESC LIMIT 50",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map(rusqlite::params![target_id, now_iso], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            candidates.push(row.map_err(|_| unhandled_error())?);
        }
    } else {
        let mut stmt = conn
            .prepare(
                "SELECT backup_id, policy_id, committed_at, snapshot_kind, storage_protocol \
                 FROM recovery_points \
                 WHERE target_id = ? AND recoverable = 1 AND committed_at <= ? AND policy_id = ? \
                 ORDER BY committed_at DESC LIMIT 50",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map(rusqlite::params![target_id, now_iso, policy_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            candidates.push(row.map_err(|_| unhandled_error())?);
        }
    }

    for (backup_id, row_policy, committed_at, snapshot_kind, storage_protocol) in candidates {
        if let Some(chain) = resolve_recoverable_chain(conn, target_id, &row_policy, &backup_id)? {
            let ciphertext_bytes: i64 = chain.iter().map(|c| c.ciphertext_bytes).sum();
            let logical_bytes = chain.last().map(|c| c.logical_bytes).unwrap_or(0);
            return Ok(Some(json!({
                "targetId": target_id,
                "policyId": row_policy,
                "backupId": backup_id,
                "committedAt": committed_at,
                "snapshotKind": snapshot_kind,
                "chainLength": chain.len(),
                "ciphertextBytes": ciphertext_bytes,
                "logicalBytes": logical_bytes,
                "recoverable": true,
                "storageProtocol": storage_protocol.filter(|s| !s.is_empty()),
            })));
        }
    }
    Ok(None)
}

pub(crate) fn latest_scrub_outcome(
    dr_conn: Option<&Connection>,
    target_id: &str,
    policy_id: &str,
    now_iso: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(None);
    };
    let cols = table_columns(conn, "scrub_evidence")?;
    if cols.is_empty() {
        return Ok(None);
    }
    let has_policy = cols.contains("policy_id");
    let sql = if !policy_id.is_empty() && has_policy {
        "SELECT result, observed_at FROM scrub_evidence \
         WHERE observed_at <= ? AND target_id = ? AND policy_id = ? \
         ORDER BY observed_at DESC LIMIT 500"
    } else {
        "SELECT result, observed_at FROM scrub_evidence \
         WHERE observed_at <= ? AND target_id = ? \
         ORDER BY observed_at DESC LIMIT 500"
    };
    let mut stmt = conn.prepare(sql).map_err(|_| unhandled_error())?;
    let mut rows_vec = Vec::new();
    if !policy_id.is_empty() && has_policy {
        let rows = stmt
            .query_map(rusqlite::params![now_iso, target_id, policy_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            rows_vec.push(row.map_err(|_| unhandled_error())?);
        }
    } else {
        let rows = stmt
            .query_map(rusqlite::params![now_iso, target_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|_| unhandled_error())?;
        for row in rows {
            rows_vec.push(row.map_err(|_| unhandled_error())?);
        }
    }
    let Some((latest_result, latest_obs)) = rows_vec.first() else {
        return Ok(None);
    };
    Ok(Some(json!({
        "result": latest_result,
        "observedAt": latest_obs,
    })))
}

pub(crate) fn latest_drill_outcome(
    dr_conn: Option<&Connection>,
    target_id: &str,
    policy_id: &str,
    now_iso: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(None);
    };
    if !table_exists(conn, "drill_evidence")? {
        return Ok(None);
    }
    let sql = if policy_id.is_empty() {
        "SELECT result, observed_at, details_json FROM drill_evidence \
         WHERE observed_at <= ? AND target_id = ? ORDER BY observed_at DESC LIMIT 500"
    } else {
        "SELECT result, observed_at, details_json FROM drill_evidence \
         WHERE observed_at <= ? AND target_id = ? AND policy_id = ? ORDER BY observed_at DESC LIMIT 500"
    };
    let mut stmt = conn.prepare(sql).map_err(|_| unhandled_error())?;
    let first: Option<(String, String, Option<String>)> = if policy_id.is_empty() {
        stmt.query_row(rusqlite::params![now_iso, target_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .optional()
        .map_err(|_| unhandled_error())?
    } else {
        stmt.query_row(rusqlite::params![now_iso, target_id, policy_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .optional()
        .map_err(|_| unhandled_error())?
    };
    let Some((result, observed_at, details_json)) = first else {
        return Ok(None);
    };
    let details: Value = details_json
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_else(|| json!({}));
    let drill_kind = details
        .get("drillKind")
        .and_then(Value::as_str)
        .unwrap_or("manual");
    Ok(Some(json!({
        "result": result,
        "observedAt": observed_at,
        "drillKind": drill_kind,
    })))
}

pub(crate) fn target_evidence_row(
    dr_conn: Option<&Connection>,
    target_id: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(None);
    };
    if !table_exists(conn, "target_evidence")? {
        return Ok(None);
    }
    let row = conn
        .query_row(
            "SELECT observed_at, scheduled_ready, status FROM target_evidence WHERE target_id = ?",
            [target_id],
            |r| {
                Ok(json!({
                    "observedAt": r.get::<_, String>(0)?,
                    "scheduledReady": r.get::<_, i64>(1)? != 0,
                    "status": r.get::<_, String>(2)?,
                }))
            },
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(row)
}

pub(crate) fn classify_and_calibrate_rto(
    dr_conn: Option<&Connection>,
    target_id: &str,
    target_kind: &str,
    latest_pt: &Value,
) -> Result<Value, AppError> {
    let logical_bytes = latest_pt
        .get("logicalBytes")
        .and_then(Value::as_i64)
        .filter(|v| *v != 0)
        .or_else(|| latest_pt.get("ciphertextBytes").and_then(Value::as_i64))
        .unwrap_or(0)
        .max(0);
    let chain_len = latest_pt
        .get("chainLength")
        .and_then(Value::as_i64)
        .unwrap_or(1)
        .max(1);
    let storage_proto = latest_pt
        .get("storageProtocol")
        .and_then(Value::as_str)
        .unwrap_or("object-set-v1");
    let format_kind = if storage_proto.contains("object-set") {
        "object-set-v1"
    } else {
        "single-file"
    };
    let size_bucket = if logical_bytes < 10 * 1024 * 1024 {
        "small"
    } else if logical_bytes < 100 * 1024 * 1024 {
        "medium"
    } else {
        "large"
    };
    let depth_bucket = if chain_len <= 3 {
        "shallow"
    } else if chain_len <= 10 {
        "moderate"
    } else {
        "deep"
    };
    let rc_tag = format!("{target_kind}:{format_kind}:{size_bucket}:{depth_bucket}");

    let samples = load_stage_samples_for_target(dr_conn, target_id)?;
    let mut transfer_speeds = Vec::new();
    let mut crypto_speeds = Vec::new();
    let mut materialize_speeds = Vec::new();
    for (rc_val, stage, bytes_count, duration_ms) in samples {
        let sample_tag = rc_val
            .as_object()
            .and_then(|o| o.get("tag"))
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| value_str(&rc_val));
        if !sample_tag.is_empty() && sample_tag != rc_tag && sample_tag != "default" {
            continue;
        }
        if duration_ms <= 0.0 || bytes_count <= 0 {
            continue;
        }
        let speed = bytes_count as f64 / (duration_ms / 1000.0);
        match stage.as_str() {
            "transfer" => transfer_speeds.push(speed),
            "crypto" => crypto_speeds.push(speed),
            "materialize" | "materialization" => materialize_speeds.push(speed),
            _ => {}
        }
    }

    let sample_count = transfer_speeds
        .len()
        .max(crypto_speeds.len())
        .max(materialize_speeds.len());
    let matching_stages = usize::from(!transfer_speeds.is_empty())
        + usize::from(!crypto_speeds.is_empty())
        + usize::from(!materialize_speeds.is_empty());
    let l_bytes = logical_bytes.max(1000) as f64;
    let planning_p50 = ((l_bytes / DEFAULT_TRANSFER_SPEED)
        + (l_bytes / DEFAULT_CRYPTO_SPEED)
        + (l_bytes / DEFAULT_MATERIALIZE_SPEED)
        + MIN_OVERHEAD_SECONDS)
        .round_ties_even()
        .max(1.0) as i64;
    let planning_heuristic = json!({
        "status": "planning-heuristic",
        "isSla": false,
        "p50Seconds": planning_p50,
        "estimatedSeconds": planning_p50,
        "transferBytesPerSecond": DEFAULT_TRANSFER_SPEED,
        "cryptoBytesPerSecond": DEFAULT_CRYPTO_SPEED,
        "materializeBytesPerSecond": DEFAULT_MATERIALIZE_SPEED,
    });

    if matching_stages < 3 || sample_count < 1 {
        return Ok(json!({
            "status": "unavailable",
            "reason": "insufficient-matching-evidence",
            "isSla": false,
            "confidence": "none",
            "sampleCount": sample_count,
            "recoveryClass": rc_tag,
            "planningHeuristic": planning_heuristic,
        }));
    }

    let confidence = if sample_count >= 10 {
        "high"
    } else if sample_count >= 3 {
        "medium"
    } else {
        "low"
    };
    let t_p50 = percentile(&transfer_speeds, 0.50);
    let c_p50 = percentile(&crypto_speeds, 0.50);
    let m_p50 = percentile(&materialize_speeds, 0.50);
    let p50_seconds =
        ((l_bytes / t_p50) + (l_bytes / c_p50) + (l_bytes / m_p50) + MIN_OVERHEAD_SECONDS)
            .round_ties_even()
            .max(1.0) as i64;

    let t_p90 = percentile(&transfer_speeds, 0.10);
    let c_p90 = percentile(&crypto_speeds, 0.10);
    let m_p90 = percentile(&materialize_speeds, 0.10);
    let p90_raw =
        ((l_bytes / t_p90) + (l_bytes / c_p90) + (l_bytes / m_p90) + MIN_OVERHEAD_SECONDS * 1.5)
            .round_ties_even() as i64;
    let p90_seconds = p50_seconds.max(p90_raw);

    Ok(json!({
        "status": "calibrated",
        "isSla": false,
        "confidence": confidence,
        "sampleCount": sample_count,
        "p50Seconds": p50_seconds,
        "p90Seconds": p90_seconds,
        "estimatedSeconds": p50_seconds,
        "recoveryClass": rc_tag,
        "stageEstimates": {
            "transfer": {"p50Seconds": (l_bytes / t_p50).round_ties_even().max(1.0) as i64},
            "crypto": {"p50Seconds": (l_bytes / c_p50).round_ties_even().max(1.0) as i64},
            "materialization": {"p50Seconds": (l_bytes / m_p50).round_ties_even().max(1.0) as i64},
        },
        "planningHeuristic": planning_heuristic,
    }))
}

fn load_stage_samples_for_target(
    dr_conn: Option<&Connection>,
    target_id: &str,
) -> Result<Vec<(Value, String, i64, f64)>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(Vec::new());
    };
    let cols = table_columns(conn, "recovery_stage_samples")?;
    if cols.is_empty() {
        return Ok(Vec::new());
    }
    let query_all = |c: &Connection| -> Result<Vec<(Value, String, i64, f64)>, AppError> {
        let mut stmt = c
            .prepare(
                "SELECT recovery_class_json, stage, bytes, duration_ms \
                 FROM recovery_stage_samples ORDER BY observed_at DESC LIMIT 1000",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map([], map_stage_sample)
            .map_err(|_| unhandled_error())?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(|_| unhandled_error())?);
        }
        Ok(out)
    };
    if cols.contains("target_id") {
        let mut stmt = conn
            .prepare(
                "SELECT recovery_class_json, stage, bytes, duration_ms \
                 FROM recovery_stage_samples WHERE target_id = ? ORDER BY observed_at DESC LIMIT 1000",
            )
            .map_err(|_| unhandled_error())?;
        let rows = stmt
            .query_map([target_id], map_stage_sample)
            .map_err(|_| unhandled_error())?;
        let mut scoped = Vec::new();
        for row in rows {
            scoped.push(row.map_err(|_| unhandled_error())?);
        }
        if scoped.len() >= 3 {
            return Ok(scoped);
        }
    }
    query_all(conn)
}

fn map_stage_sample(row: &rusqlite::Row<'_>) -> rusqlite::Result<(Value, String, i64, f64)> {
    let rc_raw: Option<String> = row.get(0)?;
    let rc = rc_raw
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or_else(|| json!({}));
    Ok((rc, row.get(1)?, row.get(2)?, row.get::<_, f64>(3)?))
}

pub(crate) fn list_logical_recovery_copies(
    dr_conn: Option<&Connection>,
    policy_id: Option<&str>,
    backup_id: Option<&str>,
    object_set_digest: Option<&str>,
    limit: i64,
) -> Result<Vec<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(Vec::new());
    };
    let cols = table_columns(conn, "recovery_point_copies")?;
    if cols.is_empty() {
        return Ok(Vec::new());
    }
    let state_col = if cols.contains("state") {
        "COALESCE(NULLIF(state, ''), 'healthy')"
    } else {
        "'healthy'"
    };
    let osd_col = if cols.contains("object_set_digest") {
        "object_set_digest"
    } else {
        "NULL"
    };
    let role_col = if cols.contains("role") {
        "COALESCE(NULLIF(role, ''), 'primary')"
    } else {
        "'primary'"
    };
    let meta_col = if cols.contains("metadata_json") {
        "metadata_json"
    } else {
        "NULL"
    };
    let mut clauses = Vec::new();
    let mut params: Vec<String> = Vec::new();
    if let Some(pid) = policy_id.filter(|s| !s.is_empty()) {
        clauses.push("policy_id = ?");
        params.push(pid.to_string());
    }
    if let Some(bid) = backup_id.filter(|s| !s.is_empty()) {
        clauses.push("backup_id = ?");
        params.push(bid.to_string());
    }
    if let Some(dig) = object_set_digest.filter(|s| !s.is_empty()) {
        clauses.push("object_set_digest = ?");
        params.push(dig.to_string());
    }
    let where_sql = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    let sql = format!(
        "SELECT target_id, policy_id, backup_id, committed_at, recoverable, {state_col}, {osd_col}, {role_col}, {meta_col} \
         FROM recovery_point_copies{where_sql} ORDER BY committed_at DESC, logical_id DESC LIMIT ?"
    );
    let mut stmt = conn.prepare(&sql).map_err(|_| unhandled_error())?;
    let mut dyn_params: Vec<&dyn rusqlite::ToSql> =
        params.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
    dyn_params.push(&limit);
    let rows = stmt
        .query_map(dyn_params.as_slice(), |row| {
            let osd: Option<String> = row.get(6)?;
            let role: String = row.get(7)?;
            let meta_raw: Option<String> = row.get(8)?;
            let metadata = meta_raw
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(|v| v.is_object())
                .unwrap_or_else(|| json!({}));
            Ok(json!({
                "targetId": row.get::<_, String>(0)?,
                "policyId": row.get::<_, String>(1)?,
                "backupId": row.get::<_, String>(2)?,
                "committedAt": row.get::<_, String>(3)?,
                "recoverable": row.get::<_, i64>(4)? != 0,
                "state": row.get::<_, String>(5)?,
                "objectSetDigest": osd.filter(|s| !s.is_empty()),
                "role": role,
                "metadata": metadata,
            }))
        })
        .map_err(|_| unhandled_error())?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|_| unhandled_error())?);
    }
    Ok(out)
}

fn calculate_replica_lag(
    dr_conn: Option<&Connection>,
    policy_id: &str,
    replica_target_id: &str,
    primary_target_id: &str,
    now_iso: &str,
) -> Result<(i64, i64), AppError> {
    let copies = list_logical_recovery_copies(dr_conn, Some(policy_id), None, None, 200)?;
    let p_copies: Vec<&Value> = copies
        .iter()
        .filter(|c| {
            c.get("targetId").and_then(Value::as_str) == Some(primary_target_id)
                && c.get("recoverable").and_then(Value::as_bool) == Some(true)
        })
        .collect();
    let r_copies: Vec<&Value> = copies
        .iter()
        .filter(|c| {
            c.get("targetId").and_then(Value::as_str) == Some(replica_target_id)
                && c.get("recoverable").and_then(Value::as_bool) == Some(true)
        })
        .collect();

    let primary_pt = latest_recoverable_point(dr_conn, primary_target_id, policy_id, now_iso)?
        .or_else(|| p_copies.first().cloned().cloned());
    let replica_pt = latest_recoverable_point(dr_conn, replica_target_id, policy_id, now_iso)?
        .or_else(|| r_copies.first().cloned().cloned());

    let Some(p_pt) = primary_pt else {
        return Ok((0, 0));
    };
    let Some(r_pt) = replica_pt else {
        return Ok((999, 999_999));
    };
    let p_sec = p_pt
        .get("committedAt")
        .and_then(|v| parse_dr_time(Some(v)))
        .unwrap_or(0);
    let r_sec = r_pt
        .get("committedAt")
        .and_then(|v| parse_dr_time(Some(v)))
        .unwrap_or(0);
    let lag_seconds = if p_sec > 0 && r_sec > 0 {
        (p_sec - r_sec).max(0)
    } else {
        0
    };
    let p_ids: HashSet<&str> = p_copies
        .iter()
        .filter_map(|c| c.get("backupId").and_then(Value::as_str))
        .collect();
    let r_ids: HashSet<&str> = r_copies
        .iter()
        .filter_map(|c| c.get("backupId").and_then(Value::as_str))
        .collect();
    let lag_points = p_ids.difference(&r_ids).count() as i64;
    Ok((lag_points, lag_seconds))
}

struct ScopeInput<'a> {
    dr_conn: Option<&'a Connection>,
    target_id: &'a str,
    policy_id: &'a str,
    policy: Option<&'a Value>,
    targets: &'a [Value],
    lease_h: &'a Value,
    now_secs: u64,
    now_iso: &'a str,
}

fn evaluate_scope_readiness(ctx: &ScopeInput<'_>) -> Result<Value, AppError> {
    let mut reasons = Vec::new();
    let mut scope_status = "available";

    let latest_pt =
        latest_recoverable_point(ctx.dr_conn, ctx.target_id, ctx.policy_id, ctx.now_iso)?;
    let recovery_point = if let Some(pt) = latest_pt.as_ref() {
        let committed_epoch = parse_dr_time(pt.get("committedAt")).unwrap_or(ctx.now_secs as i64);
        let rpo_seconds = (ctx.now_secs as i64 - committed_epoch).max(0);
        let eff_policy = if ctx.policy_id.is_empty() {
            pt.get("policyId").and_then(Value::as_str).unwrap_or("")
        } else {
            ctx.policy_id
        };
        json!({
            "status": "available",
            "backupId": pt.get("backupId").and_then(Value::as_str).unwrap_or(""),
            "targetId": ctx.target_id,
            "policyId": eff_policy,
            "snapshotKind": pt.get("snapshotKind").and_then(Value::as_str).unwrap_or("full"),
            "chainLength": pt.get("chainLength").and_then(Value::as_i64).unwrap_or(1),
            "recoveryPointAt": pt.get("committedAt").cloned().unwrap_or(Value::Null),
            "rpoSeconds": rpo_seconds,
            "source": "dr-evidence-ledger",
        })
    } else {
        let rp_reason = if ctx.policy_id.is_empty() {
            "no-committed-recoverable-point"
        } else {
            "no-policy-recovery-point"
        };
        reasons.push(if ctx.policy_id.is_empty() {
            "no-recoverable-points".to_string()
        } else {
            rp_reason.to_string()
        });
        scope_status = "blocked";
        json!({
            "status": "unavailable",
            "reason": rp_reason,
            "source": "dr-evidence-ledger",
        })
    };

    let objectives = ctx
        .policy
        .and_then(|p| p.get("recoveryObjectives"))
        .and_then(Value::as_object);
    let max_rpo = objectives
        .and_then(|o| o.get("maxRpoSeconds"))
        .and_then(Value::as_i64);
    let max_scrub = objectives
        .and_then(|o| o.get("maxScrubAgeSeconds"))
        .and_then(Value::as_i64);
    let max_drill = objectives
        .and_then(|o| o.get("maxDrillAgeSeconds"))
        .and_then(Value::as_i64);

    if latest_pt.is_some() {
        if let Some(limit_rpo) = max_rpo {
            let rpo_val = recovery_point
                .get("rpoSeconds")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            if rpo_val > limit_rpo {
                reasons.push("rpo-objective-breached".to_string());
                if scope_status == "available" {
                    scope_status = "objective-breached";
                }
            }
        }
    }

    let scrub = match latest_scrub_outcome(ctx.dr_conn, ctx.target_id, ctx.policy_id, ctx.now_iso)?
    {
        Some(record) => {
            let obs_epoch = parse_dr_time(record.get("observedAt")).unwrap_or(ctx.now_secs as i64);
            let age = (ctx.now_secs as i64 - obs_epoch).max(0);
            let ok = record.get("result").and_then(Value::as_str) == Some("success");
            if !ok {
                reasons.push("scrub-failed".to_string());
                if scope_status == "available" {
                    scope_status = "degraded";
                }
            } else if let Some(limit_scrub) = max_scrub {
                if age > limit_scrub {
                    reasons.push("scrub-overdue".to_string());
                    if scope_status == "available" {
                        scope_status = "objective-breached";
                    }
                }
            }
            json!({
                "status": if ok { "ok" } else { "error" },
                "latestCheckedAt": record.get("observedAt").cloned().unwrap_or(Value::Null),
                "latestSuccessfulAt": if ok { record.get("observedAt").cloned().unwrap_or(Value::Null) } else { Value::Null },
                "ageSeconds": age,
                "source": "dr-evidence-ledger",
            })
        }
        None => {
            let reason = if ctx.policy_id.is_empty() {
                "no-evidence"
            } else {
                "no-policy-scrub-evidence"
            };
            if max_scrub.is_some() && latest_pt.is_some() {
                reasons.push(if reason == "no-evidence" {
                    "scrub-overdue".to_string()
                } else {
                    reason.to_string()
                });
                if scope_status == "available" {
                    scope_status = "objective-breached";
                }
            }
            json!({"status": "unavailable", "reason": reason, "source": "dr-evidence-ledger"})
        }
    };

    let drill = match latest_drill_outcome(ctx.dr_conn, ctx.target_id, ctx.policy_id, ctx.now_iso)?
    {
        Some(record) => {
            let obs_epoch = parse_dr_time(record.get("observedAt")).unwrap_or(ctx.now_secs as i64);
            let age = (ctx.now_secs as i64 - obs_epoch).max(0);
            let res_str = record.get("result").and_then(Value::as_str).unwrap_or("");
            let ok = res_str == "success";
            let status_str = if ok {
                "ok"
            } else if res_str == "failed" {
                "error"
            } else {
                "blocked"
            };
            if !ok {
                reasons.push(if res_str == "failed" {
                    "drill-failed".to_string()
                } else {
                    "drill-blocked".to_string()
                });
                if res_str == "failed" {
                    if scope_status == "available" {
                        scope_status = "degraded";
                    }
                } else if scope_status == "available" {
                    scope_status = "objective-breached";
                }
            } else if let Some(limit_drill) = max_drill {
                if age > limit_drill {
                    reasons.push("drill-overdue".to_string());
                    if scope_status == "available" {
                        scope_status = "objective-breached";
                    }
                }
            }
            json!({
                "status": status_str,
                "latestCheckedAt": record.get("observedAt").cloned().unwrap_or(Value::Null),
                "latestSuccessfulAt": if ok { record.get("observedAt").cloned().unwrap_or(Value::Null) } else { Value::Null },
                "ageSeconds": age,
                "drillKind": record.get("drillKind").cloned().unwrap_or(Value::Null),
                "source": "dr-evidence-ledger",
            })
        }
        None => {
            let reason = if ctx.policy_id.is_empty() {
                "no-evidence"
            } else {
                "no-policy-drill-evidence"
            };
            if max_drill.is_some() && latest_pt.is_some() {
                reasons.push(if reason == "no-evidence" {
                    "drill-overdue".to_string()
                } else {
                    reason.to_string()
                });
                if scope_status == "available" {
                    scope_status = "objective-breached";
                }
            }
            json!({"status": "unavailable", "reason": reason, "source": "dr-evidence-ledger"})
        }
    };

    let target_health = match target_evidence_row(ctx.dr_conn, ctx.target_id)? {
        Some(ev) => {
            let ok = ev.get("status").and_then(Value::as_str) == Some("ok")
                || ev.get("scheduledReady").and_then(Value::as_bool) == Some(true);
            if !ok {
                reasons.push("target-unhealthy".to_string());
                scope_status = "blocked";
            }
            json!({
                "status": if ok { "ok" } else { "error" },
                "source": "dr-evidence-ledger",
                "checkedAt": ev.get("observedAt").cloned().unwrap_or(Value::Null),
            })
        }
        None => json!({"status": "ok", "source": "persisted-target-probe"}),
    };

    let t_kind = if ctx.target_id == "managed-local" {
        "managed-local".to_string()
    } else {
        ctx.targets
            .iter()
            .find(|t| t.get("targetId").and_then(Value::as_str) == Some(ctx.target_id))
            .and_then(|t| t.get("kind"))
            .and_then(Value::as_str)
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "filesystem".to_string())
    };
    let rto_estimate = match latest_pt.as_ref() {
        Some(pt) => classify_and_calibrate_rto(ctx.dr_conn, ctx.target_id, &t_kind, pt)?,
        None => json!({
            "status": "unavailable",
            "isSla": false,
            "reason": "recovery-point-unavailable",
            "sampleCount": 0,
        }),
    };

    let replication = replication_summary(ctx, latest_pt.as_ref())?;
    if replication.get("compliance").and_then(Value::as_str) == Some("degraded") {
        let rep_reason = replication
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("replication-objective-breached");
        reasons.push(rep_reason.to_string());
        if scope_status == "available" {
            scope_status = "degraded";
        }
    }

    if ctx.lease_h.get("status").and_then(Value::as_str) == Some("degraded") {
        let lr = ctx
            .lease_h
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("recovery-lease-keeper-degraded");
        reasons.push(lr.to_string());
        if scope_status == "available" {
            scope_status = "degraded";
        }
    }

    let mut write_continuity = json!({
        "status": "nominal",
        "configuredPrimaryTargetId": ctx.target_id,
        "activeWriteTargetId": ctx.target_id,
        "primaryStatus": "healthy",
        "isFailover": false,
    });
    if let Some(pol) = ctx.policy {
        if let Ok(continuity) = read_continuity_state(ctx.policy_id) {
            let primary_id = continuity
                .get("configuredPrimaryTargetId")
                .filter(|v| python_truthy(v))
                .or_else(|| pol.get("primaryTargetId").filter(|v| python_truthy(v)))
                .or_else(|| pol.get("targetId").filter(|v| python_truthy(v)))
                .map(|v| value_str(v))
                .unwrap_or_else(|| ctx.target_id.to_string());
            let active_id = continuity
                .get("activeWriteTargetId")
                .filter(|v| python_truthy(v))
                .map(|v| value_str(v))
                .unwrap_or_else(|| primary_id.clone());
            let is_failover = active_id != primary_id
                || continuity
                    .get("activeWriteTargetRole")
                    .and_then(Value::as_str)
                    == Some("failover");
            let p_live_status = continuity
                .get("targetLiveness")
                .and_then(|tl| tl.get(&primary_id))
                .and_then(|entry| entry.get("status"))
                .and_then(Value::as_str);
            let mut p_status = p_live_status.unwrap_or(if is_failover {
                "unavailable"
            } else {
                "healthy"
            });
            if p_status == "available" {
                p_status = "healthy";
            }
            let failover_reason = continuity
                .get("lastFailoverReason")
                .filter(|v| python_truthy(v))
                .cloned()
                .unwrap_or_else(|| {
                    Value::String(
                        if is_failover {
                            "failover-active"
                        } else {
                            "nominal"
                        }
                        .to_string(),
                    )
                });
            let candidate_targets = if is_failover {
                vec![primary_id.clone(), active_id.clone()]
            } else {
                vec![primary_id.clone()]
            };
            write_continuity = json!({
                "status": if is_failover { "failed-over" } else { "nominal" },
                "configuredPrimaryTargetId": primary_id,
                "activeWriteTargetId": active_id,
                "primaryStatus": p_status,
                "isFailover": is_failover,
                "failoverEpoch": continuity.get("failoverEpoch").and_then(Value::as_i64).unwrap_or(0),
                "policyRevision": continuity.get("policyRevision").and_then(Value::as_i64).unwrap_or(1),
                "lastFailoverAt": continuity.get("lastFailoverAt").cloned().unwrap_or(Value::Null),
                "lastFailbackAt": continuity.get("lastFailbackAt").cloned().unwrap_or(Value::Null),
                "reason": failover_reason,
                "candidateTargetIds": candidate_targets,
            });
            if is_failover {
                reasons.push("write-target-failover".to_string());
                if scope_status == "available" {
                    scope_status = "degraded";
                }
            }
        }
    }

    Ok(json!({
        "scope": {"targetId": ctx.target_id, "policyId": ctx.policy_id},
        "targetId": ctx.target_id,
        "policyId": ctx.policy_id,
        "status": scope_status,
        "reason": reasons.first().cloned(),
        "reasons": reasons,
        "recoverable": scope_status != "blocked",
        "recoveryPoint": recovery_point,
        "rtoEstimate": rto_estimate,
        "scrub": scrub,
        "drill": drill,
        "replication": replication,
        "writeContinuity": write_continuity,
        "committedCopies": replication.get("committedCopies").cloned().unwrap_or(Value::Null),
        "healthyCopies": replication.get("healthyCopies").cloned().unwrap_or(Value::Null),
        "requiredCopies": replication.get("requiredCopies").cloned().unwrap_or(Value::Null),
        "replicationCompliance": replication.get("compliance").cloned().unwrap_or(Value::Null),
        "health": {
            "target": target_health,
            "recoveryLeases": ctx.lease_h,
        },
        "evaluatedAt": ctx.now_iso,
        "source": "dr-evidence-ledger",
    }))
}

fn replication_summary(ctx: &ScopeInput<'_>, latest_pt: Option<&Value>) -> Result<Value, AppError> {
    let repl_cfg = ctx
        .policy
        .and_then(|p| p.get("replication"))
        .and_then(Value::as_object);
    let enabled = repl_cfg
        .and_then(|r| r.get("enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !enabled {
        let copies = usize::from(latest_pt.is_some());
        return Ok(json!({
            "enabled": false,
            "committedCopies": copies,
            "healthyCopies": copies,
            "requiredCopies": copies,
            "compliance": if latest_pt.is_some() { "healthy" } else { "unavailable" },
            "copies": [],
        }));
    }
    let cfg = repl_cfg.expect("enabled implies cfg present");
    let backup_id = latest_pt
        .and_then(|p| p.get("backupId"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let object_set_digest = latest_pt
        .and_then(|p| p.get("objectSetDigest").or_else(|| p.get("chainDigest")))
        .and_then(Value::as_str)
        .unwrap_or("");
    let required = cfg
        .get("minCommittedCopies")
        .and_then(Value::as_i64)
        .unwrap_or(1)
        .max(1) as usize;
    let min_fd = cfg
        .get("minFailureDomains")
        .and_then(Value::as_i64)
        .unwrap_or(1)
        .max(1) as usize;

    let logical = if backup_id.is_empty() {
        Vec::new()
    } else {
        list_logical_recovery_copies(
            ctx.dr_conn,
            Some(ctx.policy_id).filter(|s| !s.is_empty()),
            Some(backup_id),
            Some(object_set_digest).filter(|s| !s.is_empty()),
            100,
        )?
    };
    let committed: Vec<&Value> = logical
        .iter()
        .filter(|c| {
            c.get("recoverable").and_then(Value::as_bool) == Some(true)
                && c.get("state").and_then(Value::as_str) == Some("healthy")
        })
        .collect();
    let healthy_count = committed
        .iter()
        .filter(|c| {
            !c.get("targetId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .is_empty()
        })
        .count();
    let mut compliance = if committed.len() >= required {
        "healthy"
    } else {
        "degraded"
    };
    let mut reasons = Vec::new();
    if committed.len() < required {
        reasons.push("required-copy-objective-breached".to_string());
    }

    let target_fds: HashMap<&str, &str> = ctx
        .targets
        .iter()
        .filter_map(|t| {
            let tid = t.get("targetId").and_then(Value::as_str)?;
            let fd = t
                .get("failureDomain")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("default");
            Some((tid, fd))
        })
        .collect();
    let unique_fds: HashSet<&str> = committed
        .iter()
        .map(|c| {
            let tid = c.get("targetId").and_then(Value::as_str).unwrap_or("");
            *target_fds.get(tid).unwrap_or(&"default")
        })
        .collect();
    if unique_fds.len() < min_fd && latest_pt.is_some() {
        compliance = "degraded";
        reasons.push("insufficient-failure-domain-diversity".to_string());
    }

    let max_lag = ctx
        .policy
        .and_then(|p| p.get("recoveryObjectives"))
        .and_then(|o| o.get("maxReplicaLagSeconds"))
        .or_else(|| cfg.get("maxReplicaLagSeconds"))
        .and_then(Value::as_i64);
    let mut targets_info = Vec::new();
    if let Some(entries) = cfg.get("targets").and_then(Value::as_array) {
        for entry in entries {
            let Some(tid) = entry.get("targetId").and_then(Value::as_str) else {
                continue;
            };
            if tid.is_empty() {
                continue;
            }
            let (lag_pts, lag_secs) =
                calculate_replica_lag(ctx.dr_conn, ctx.policy_id, tid, ctx.target_id, ctx.now_iso)?;
            let mode = entry
                .get("mode")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("required");
            targets_info.push(json!({
                "targetId": tid,
                "mode": mode,
                "lagRecoveryPoints": lag_pts,
                "lagSeconds": lag_secs,
            }));
            if let Some(limit) = max_lag {
                if lag_secs > limit {
                    compliance = "degraded";
                    reasons.push(format!("replica-lag-exceeded:{tid}"));
                }
            }
        }
    }

    let copy_rows: Vec<Value> = logical
        .iter()
        .map(|c| {
            json!({
                "targetId": c.get("targetId").cloned().unwrap_or(Value::Null),
                "backupId": c.get("backupId").cloned().unwrap_or(Value::Null),
                "recoverable": c.get("recoverable").and_then(Value::as_bool).unwrap_or(false),
                "state": c.get("state").and_then(Value::as_str).unwrap_or("healthy"),
                "committedAt": c.get("committedAt").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(json!({
        "enabled": true,
        "committedCopies": committed.len(),
        "healthyCopies": healthy_count,
        "requiredCopies": required,
        "compliance": if latest_pt.is_some() { compliance } else { "unavailable" },
        "reason": reasons.first().cloned(),
        "reasons": reasons,
        "primaryTargetId": ctx.target_id,
        "targets": targets_info,
        "copies": copy_rows,
    }))
}

fn cache_health(root: &Path, now_iso: &str) -> Value {
    let cache_root = root.join(".backup-component-cache");
    if !cache_root.is_dir() {
        return json!({
            "status": "unavailable",
            "reason": "not-initialized",
            "source": "local-ciphertext-cache",
        });
    }
    let mut entries = 0usize;
    let mut bytes_used = 0u64;
    let mut partials = 0usize;
    let sha_dir = cache_root.join("sha256");
    if sha_dir.is_dir() {
        if let Ok(buckets) = fs::read_dir(&sha_dir) {
            for bucket in buckets.flatten() {
                if !bucket.path().is_dir() {
                    continue;
                }
                if let Ok(files) = fs::read_dir(bucket.path()) {
                    for file in files.flatten() {
                        let path = file.path();
                        if !path.is_file() {
                            continue;
                        }
                        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                        if name.ends_with(".age") {
                            entries += 1;
                            if let Ok(meta) = fs::metadata(&path) {
                                bytes_used = bytes_used.saturating_add(meta.len());
                            }
                        } else if name.ends_with(".partial") {
                            partials += 1;
                        }
                    }
                }
            }
        }
    }

    let mut pinned = HashSet::new();
    let pins_dir = cache_root.join("pins");
    let valid_pin_result: Result<(), ()> = (|| {
        if !pins_dir.is_dir() {
            return Ok(());
        }
        let rd = fs::read_dir(&pins_dir).map_err(|_| ())?;
        for entry in rd.flatten() {
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            let text = fs::read_to_string(&path).map_err(|_| ())?;
            let parsed: Value = serde_json::from_str(&text).map_err(|_| ())?;
            let Some(obj) = parsed.as_object() else {
                return Err(());
            };
            if obj.get("schemaVersion").and_then(Value::as_i64) != Some(1) {
                return Err(());
            }
            let Some(digests) = obj.get("digests").and_then(Value::as_array) else {
                return Err(());
            };
            for item in digests {
                let Some(hex) = item.as_str() else {
                    return Err(());
                };
                if hex.len() != 64 || !hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
                    return Err(());
                }
                pinned.insert(hex.to_string());
            }
        }
        Ok(())
    })();

    if valid_pin_result.is_err() {
        return json!({
            "status": "error",
            "reason": "pin-metadata-invalid",
            "source": "local-ciphertext-cache",
            "checkedAt": now_iso,
        });
    }
    let status = if bytes_used > DEFAULT_CACHE_QUOTA_BYTES {
        "warning"
    } else {
        "ok"
    };
    json!({
        "status": status,
        "source": "local-ciphertext-cache",
        "checkedAt": now_iso,
        "entries": entries,
        "bytes": bytes_used,
        "partialFiles": partials,
        "pinnedEntries": pinned.len(),
        "quotaBytes": DEFAULT_CACHE_QUOTA_BYTES,
        "verification": "sha256-on-hit",
    })
}

fn recovery_lease_health(root: &Path) -> Value {
    let staging = root.join(".restore-staging");
    let mut total_sessions = 0usize;
    let mut protected_count = 0usize;
    if staging.is_dir() {
        if let Ok(rd) = fs::read_dir(&staging) {
            let mut dirs: Vec<_> = rd.flatten().collect();
            dirs.sort_by_key(|e| e.file_name());
            for entry in dirs {
                let rf = entry.path().join("remote-fetch.json");
                if !rf.is_file() {
                    continue;
                }
                let Ok(text) = fs::read_to_string(&rf) else {
                    continue;
                };
                let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                total_sessions += 1;
                let phase = obj.get("phase").and_then(Value::as_str).unwrap_or("");
                let protected_phase = !phase.is_empty()
                    && !matches!(phase, "aborted" | "complete" | "failed" | "rolled-back");
                let target_id = obj
                    .get("targetId")
                    .or_else(|| obj.get("activeSourceTargetId"))
                    .and_then(Value::as_str)
                    .unwrap_or("managed-local");
                let is_local = target_id == "managed-local" || target_id.starts_with("local");
                let has_holds = obj
                    .get("holds")
                    .and_then(Value::as_array)
                    .is_some_and(|a| !a.is_empty())
                    || obj
                        .get("holdKeys")
                        .and_then(Value::as_array)
                        .is_some_and(|a| !a.is_empty())
                    || obj.get("holdKey").is_some_and(python_truthy);
                if protected_phase && !is_local && has_holds {
                    protected_count += 1;
                }
            }
        }
    }
    let (status, reason) = if protected_count > 0 {
        ("degraded", Value::String("keeper-not-running".to_string()))
    } else {
        ("ok", Value::Null)
    };
    json!({
        "status": status,
        "reason": reason,
        "activeLeases": protected_count,
        "totalSessions": total_sessions,
        "renewedCount": 0,
        "lastTickAt": Value::Null,
        "lastSuccessfulTickAt": Value::Null,
        "consecutiveFailures": 0,
        "protectedJobs": protected_count,
        "renewedLeases": 0,
        "lastFailure": Value::Null,
        "keeperRunning": false,
    })
}

fn transfer_control_projection(control_conn: Option<&Connection>) -> Result<Value, AppError> {
    let mut total = 0usize;
    let mut p0_count = 0usize;
    let mut p2_count = 0usize;
    let mut rebalance_backlog = 0i64;
    if let Some(conn) = control_conn {
        if table_exists(conn, "qos_transfers")? {
            let now_float = Utc::now().timestamp_millis() as f64 / 1000.0;
            let mut stmt = conn
                .prepare(
                    "SELECT traffic_class, estimated_bytes, bytes_transferred \
                     FROM qos_transfers WHERE lease_until >= ? ORDER BY transfer_id",
                )
                .map_err(|_| unhandled_error())?;
            let rows = stmt
                .query_map([now_float], |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                })
                .map_err(|_| unhandled_error())?;
            for row in rows {
                let (tc, est, done) = row.map_err(|_| unhandled_error())?;
                total += 1;
                if tc == 0 {
                    p0_count += 1;
                } else if tc == 2 || tc == 3 {
                    p2_count += 1;
                } else if tc == 5 || tc == 6 {
                    rebalance_backlog = rebalance_backlog.saturating_add((est - done).max(0));
                }
            }
        }
    }
    Ok(json!({
        "activeTransfersTotal": total,
        "activeRecoveryTransfers": p0_count,
        "activeRepairTransfers": p2_count,
        "rebalanceBacklogBytes": rebalance_backlog,
        "backgroundThrottleActive": p0_count > 0,
        "globalBytesPerSecond": DEFAULT_GLOBAL_BYTES_PER_SECOND,
        "globalBandwidthBytesPerSec": DEFAULT_GLOBAL_BYTES_PER_SECOND,
        "reservedRecoveryBytesPerSecond": DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC,
        "reservedDrBandwidthBytesPerSec": DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC,
        "repairReservedBytesPerSecond": DEFAULT_RESERVED_REPAIR_BYTES_PER_SEC,
    }))
}

fn build_body() -> Result<Value, AppError> {
    let root = crate::data_routes::workspace_root();
    let now_secs = now_unix_secs();
    let now_iso = utc_iso_from_unix(now_secs);

    let dr_db = root.join(".backup-dr").join("evidence.sqlite3");
    let dr_conn = open_readonly_sqlite(&dr_db)?;
    let control_db = root.join(".backup-control").join("control.sqlite3");
    let control_conn = open_readonly_sqlite(&control_db)?;

    let policies = list_policies_readonly(&root)?;
    let targets = list_registered_targets()?;
    let mut known_scopes: BTreeSet<(String, String)> =
        list_scopes(dr_conn.as_ref())?.into_iter().collect();
    for pol in &policies {
        let tid = pol
            .get("targetId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("managed-local")
            .to_string();
        let pid = pol
            .get("policyId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !pid.is_empty() {
            known_scopes.insert((tid, pid));
        }
    }
    if known_scopes.is_empty() {
        known_scopes.insert(("managed-local".to_string(), String::new()));
    }

    let policy_by_id: HashMap<&str, &Value> = policies
        .iter()
        .filter_map(|p| p.get("policyId").and_then(Value::as_str).map(|k| (k, p)))
        .collect();
    let lease_h = recovery_lease_health(&root);

    let mut evaluated_scopes = Vec::new();
    let mut worst_severity = 1_i32;
    let mut worst_status = "available".to_string();
    let mut worst_reason: Value = Value::Null;
    for (tid, pid) in &known_scopes {
        let ctx = ScopeInput {
            dr_conn: dr_conn.as_ref(),
            target_id: tid,
            policy_id: pid,
            policy: policy_by_id.get(pid.as_str()).copied(),
            targets: &targets,
            lease_h: &lease_h,
            now_secs,
            now_iso: &now_iso,
        };
        let scope_res = evaluate_scope_readiness(&ctx)?;
        let s_stat = scope_res
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("");
        let sev = match s_stat {
            "blocked" => 4,
            "degraded" => 3,
            "objective-breached" => 2,
            "available" | "ok" => 1,
            _ => 2,
        };
        if sev > worst_severity {
            worst_severity = sev;
            worst_status = s_stat.to_string();
            worst_reason = scope_res.get("reason").cloned().unwrap_or(Value::Null);
        }
        evaluated_scopes.push(scope_res);
    }

    let cache_h = cache_health(&root, &now_iso);
    let overall_target_health = if targets.is_empty() {
        "unavailable"
    } else if targets.iter().any(|t| {
        let probe = t.get("lastProbe").and_then(Value::as_object);
        let st = probe.and_then(|p| p.get("status")).and_then(Value::as_str);
        let ready = probe
            .and_then(|p| p.get("scheduledBackupReady"))
            .and_then(Value::as_bool);
        matches!(st, Some("failed") | Some("error")) || ready == Some(false)
    }) {
        "error"
    } else {
        "ok"
    };

    let primary_scope = evaluated_scopes
        .first()
        .cloned()
        .unwrap_or_else(|| json!({}));
    let primary_rp = primary_scope.get("recoveryPoint").cloned().unwrap_or_else(
        || json!({"status": "unavailable", "reason": "no-committed-recoverable-point"}),
    );
    let primary_rto = primary_scope
        .get("rtoEstimate")
        .cloned()
        .unwrap_or_else(|| json!({"status": "unavailable", "isSla": false}));
    let primary_scrub = primary_scope
        .get("scrub")
        .cloned()
        .unwrap_or_else(|| json!({"status": "unavailable"}));
    let primary_drill = primary_scope
        .get("drill")
        .cloned()
        .unwrap_or_else(|| json!({"status": "unavailable"}));
    let mut primary_health = primary_scope
        .get("health")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_else(Map::new);
    primary_health.insert(
        "target".to_string(),
        json!({"status": overall_target_health, "source": "persisted-target-probe"}),
    );
    primary_health.insert("cache".to_string(), cache_h.clone());

    let any_available = evaluated_scopes.iter().any(|s| {
        s.get("recoveryPoint")
            .and_then(|rp| rp.get("status"))
            .and_then(Value::as_str)
            == Some("available")
    });
    let final_status = if policies.is_empty() && !any_available {
        "warning".to_string()
    } else {
        worst_status
    };

    let target_fds: HashMap<&str, &str> = targets
        .iter()
        .filter_map(|t| {
            let tid = t.get("targetId").and_then(Value::as_str)?;
            let fd = t
                .get("failureDomain")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("default");
            Some((tid, fd))
        })
        .collect();
    let all_copies = list_logical_recovery_copies(dr_conn.as_ref(), None, None, None, 500)?;
    let healthy_copies: Vec<&Value> = all_copies
        .iter()
        .filter(|c| {
            c.get("recoverable").and_then(Value::as_bool) == Some(true)
                && c.get("state").and_then(Value::as_str) == Some("healthy")
        })
        .collect();
    let mut counts_by_fd: BTreeMap<&str, usize> = BTreeMap::new();
    for copy in &healthy_copies {
        let tid = copy.get("targetId").and_then(Value::as_str).unwrap_or("");
        let fd = *target_fds.get(tid).unwrap_or(&"default");
        *counts_by_fd.entry(fd).or_insert(0) += 1;
    }
    let unique_fd_count = counts_by_fd.len();
    let max_fd_copies = counts_by_fd.values().copied().max().unwrap_or(0);
    let topology_status = if unique_fd_count >= 1 && !healthy_copies.is_empty() {
        "healthy"
    } else if !healthy_copies.is_empty() {
        "degraded"
    } else {
        "unavailable"
    };
    let topology_projection = json!({
        "healthyCopies": healthy_copies.len(),
        "failureDomains": unique_fd_count,
        "maxCopiesInSingleDomain": max_fd_copies,
        "status": topology_status,
    });

    let mut constrained_target: Option<String> = None;
    let mut min_free_pct: Option<f64> = None;
    let mut min_days_to_full: i64 = 9999;
    let mut capacity_status = "healthy".to_string();
    for target in &targets {
        let tid = target.get("targetId").and_then(Value::as_str).unwrap_or("");
        if tid.is_empty() {
            continue;
        }
        let cap = probe_target_capacity_with_probe(
            target,
            tid,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            false,
        )?;
        let horizon = estimate_horizon_readonly(tid, &cap, control_conn.as_ref())?;
        if let Some(fp) = horizon.get("freePercent").and_then(Value::as_f64) {
            if min_free_pct.is_none_or(|prev| fp < prev) {
                min_free_pct = Some(round2(fp));
                constrained_target = Some(tid.to_string());
            }
        }
        if let Some(dtf) = horizon.get("estimatedDaysToFull").and_then(Value::as_i64) {
            if dtf < min_days_to_full {
                min_days_to_full = dtf;
            }
        }
        if let Some(st) = horizon.get("status").and_then(Value::as_str) {
            if (st == "critical" || st == "degraded")
                && (st == "critical" || capacity_status != "critical")
            {
                capacity_status = st.to_string();
            }
        }
    }
    let fallback_target = targets
        .first()
        .and_then(|t| t.get("targetId"))
        .and_then(Value::as_str)
        .unwrap_or("managed-local")
        .to_string();
    let capacity_projection = json!({
        "status": capacity_status,
        "mostConstrainedTarget": constrained_target.unwrap_or(fallback_target),
        "freePercent": min_free_pct,
        "estimatedDaysToFull": if min_days_to_full < 9999 { json!(min_days_to_full) } else { Value::Null },
    });

    let transfer_control = transfer_control_projection(control_conn.as_ref())?;

    Ok(json!({
        "status": final_status,
        "reason": worst_reason,
        "evaluatedAt": now_iso,
        "source": "dr-evidence-ledger",
        "scopes": evaluated_scopes,
        "recoveryPoint": primary_rp,
        "rtoEstimate": primary_rto,
        "scrub": primary_scrub,
        "drill": primary_drill,
        "topology": topology_projection,
        "capacity": capacity_projection,
        "transferControl": transfer_control,
        "health": Value::Object(primary_health),
        "cache": cache_h,
        "recoveryLeases": lease_h,
    }))
}
