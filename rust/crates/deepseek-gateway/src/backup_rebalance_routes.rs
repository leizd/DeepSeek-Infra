//! Backup replica rebalance routes (S4):
//! - `GET /api/workspace/backup-rebalances`
//! - `POST /api/workspace/backup-rebalances`
//!
//! `.backup-rebalance/*.json` belongs to `backup_replication_store` (a declared
//! native data domain). `GET` never creates `.backup-rebalance/`. `POST`
//! enforces `require_native_store("backup_replication_store", ...)` (`409`
//! unless `DEEPSEEK_RUNTIME_MODE=python_disabled`) before touching disk.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::Router;
use axum::body::Bytes;
use axum::extract::Query;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::value_str;
use deepseek_policy::tzif::load_iana_timezone;
use rusqlite::Connection;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::{
    backup_file_candidates, catalog_state, committed_object_inventory, is_object_set,
    list_registered_targets, object_path, open_readonly_sqlite, registered_target, unix_seconds,
    utc_iso_from_unix,
};
use crate::backup_catalog_write_routes::{
    acquire_target_writer, append_catalog_entry, verify_registered_target_ready_readonly,
};
use crate::backup_retention_routes::load_backup_policy;
use crate::route_support;

const BODY_LIMIT: i64 = 16_000;
const LIST_LIMIT: usize = 200;
const MAX_JOBS_PER_TRIGGER: usize = 5;
const WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_REPLICATION_WRITE_NOT_OWNED";

pub fn router() -> Router {
    Router::new().route("/api/workspace/backup-rebalances", any(dispatch_rebalances))
}

#[derive(Debug, Deserialize, Default)]
struct RebalanceQuery {
    #[serde(rename = "policyId")]
    policy_id: Option<String>,
}

fn rebalance_dir() -> PathBuf {
    crate::data_routes::workspace_root().join(".backup-rebalance")
}

async fn dispatch_rebalances(
    method: Method,
    Query(query): Query<RebalanceQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method == Method::GET || method == Method::HEAD {
        let is_head = method == Method::HEAD;
        let policy_id = query.policy_id.filter(|v| !v.is_empty());
        return match tokio::task::spawn_blocking(move || list_rebalance_jobs(policy_id.as_deref()))
            .await
        {
            Ok(Ok(_)) if is_head => StatusCode::OK.into_response(),
            Ok(Ok(jobs)) => axum::Json(json!({"rebalances": jobs})).into_response(),
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
        return match tokio::task::spawn_blocking(move || handle_trigger_rebalance(&headers, &body))
            .await
        {
            Ok(Ok(res)) => axum::Json(res).into_response(),
            Ok(Err(error)) => route_support::app_error_response(error),
            Err(_) => route_support::app_error_response(route_support::unhandled_error()),
        };
    }
    route_support::method_not_allowed()
}

fn is_json_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    if bytes.len() < 5 {
        return false;
    }
    let suffix = &bytes[bytes.len() - 5..];
    #[cfg(windows)]
    {
        suffix.eq_ignore_ascii_case(b".json")
    }
    #[cfg(not(windows))]
    {
        suffix == b".json"
    }
}

fn name_order(left: &str, right: &str) -> std::cmp::Ordering {
    name_key(left)
        .cmp(&name_key(right))
        .then_with(|| left.cmp(right))
}

fn name_key(name: &str) -> std::borrow::Cow<'_, str> {
    #[cfg(windows)]
    {
        std::borrow::Cow::Owned(name.to_lowercase())
    }
    #[cfg(not(windows))]
    {
        std::borrow::Cow::Borrowed(name)
    }
}

fn list_rebalance_jobs(policy_id: Option<&str>) -> Result<Vec<Value>, AppError> {
    let dir = rebalance_dir();
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut names = Vec::new();
    for entry in fs::read_dir(&dir).map_err(|_| route_support::unhandled_error())? {
        let entry = entry.map_err(|_| route_support::unhandled_error())?;
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if name.starts_with('.') || !is_json_name(&name) || !entry.path().is_file() {
            continue;
        }
        names.push(name);
    }
    names.sort_by(|left, right| name_order(right, left));
    let mut jobs = Vec::new();
    for name in names {
        let Ok(bytes) = fs::read(dir.join(&name)) else {
            continue;
        };
        let Ok(text) = std::str::from_utf8(&bytes) else {
            continue;
        };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(text) else {
            continue;
        };
        let job_id = obj.get("jobId").map(value_str).unwrap_or_default();
        if job_id.is_empty() {
            continue;
        }
        if let Some(expected) = policy_id {
            let actual = match obj.get("policyId") {
                Some(v) if python_truthy(v) => value_str(v),
                _ => String::new(),
            };
            if actual != expected {
                continue;
            }
        }
        jobs.push(Value::Object(obj));
        if jobs.len() >= LIST_LIMIT {
            break;
        }
    }
    Ok(jobs)
}

fn inside_maintenance_window(policy: &Value, unix_now: i64) -> bool {
    let Some(mw) = policy
        .get("placement")
        .and_then(Value::as_object)
        .and_then(|p| p.get("maintenanceWindow"))
        .and_then(Value::as_object)
    else {
        return true;
    };
    let tz_name = mw.get("timezone").and_then(Value::as_str).unwrap_or("UTC");
    let tz = load_iana_timezone(tz_name)
        .or_else(|_| load_iana_timezone("UTC"))
        .expect("UTC tz");
    let (hour, minute) = tz.local_hour_minute(unix_now);
    let cur_mins = hour * 60 + minute;
    let Some(start_mins) = parse_hhmm(mw.get("start").and_then(Value::as_str).unwrap_or("00:00"))
    else {
        return true;
    };
    let Some(end_mins) = parse_hhmm(mw.get("end").and_then(Value::as_str).unwrap_or("23:59"))
    else {
        return true;
    };
    if start_mins <= end_mins {
        start_mins <= cur_mins && cur_mins <= end_mins
    } else {
        cur_mins >= start_mins || cur_mins <= end_mins
    }
}

fn parse_hhmm(raw: &str) -> Option<u32> {
    let (h, m) = raw.split_once(':')?;
    let hh = h.trim().parse::<u32>().ok()?;
    let mm = m.trim().parse::<u32>().ok()?;
    if hh >= 24 || mm >= 60 {
        return None;
    }
    Some(hh * 60 + mm)
}

#[derive(Clone)]
struct CopyRecord {
    target_id: String,
    backup_id: String,
    logical_id: String,
    object_set_digest: String,
    committed_at: String,
    recoverable: bool,
    state: String,
    metadata: Map<String, Value>,
}

fn load_policy_copies(policy_id: &str) -> Result<Vec<CopyRecord>, AppError> {
    let dr_db = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    let Some(conn) = open_readonly_sqlite(&dr_db)? else {
        return Ok(Vec::new());
    };
    let has_table: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_point_copies'",
            [],
            |_| Ok(true),
        )
        .unwrap_or(false);
    if !has_table {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare(
            "SELECT target_id, backup_id, logical_id, COALESCE(object_set_digest, ''), committed_at, \
             recoverable, COALESCE(state, 'healthy'), COALESCE(metadata_json, '{}') \
             FROM recovery_point_copies WHERE policy_id = ?1 \
             ORDER BY committed_at DESC, logical_id DESC LIMIT 200",
        )
        .map_err(|_| route_support::unhandled_error())?;
    let mut rows = stmt
        .query([policy_id])
        .map_err(|_| route_support::unhandled_error())?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(|_| route_support::unhandled_error())? {
        let meta_raw: String = row.get(7).map_err(|_| route_support::unhandled_error())?;
        let meta = match serde_json::from_str::<Value>(&meta_raw) {
            Ok(Value::Object(map)) => map,
            _ => Map::new(),
        };
        let recoverable_int: i64 = row.get(5).map_err(|_| route_support::unhandled_error())?;
        out.push(CopyRecord {
            target_id: row.get(0).map_err(|_| route_support::unhandled_error())?,
            backup_id: row.get(1).map_err(|_| route_support::unhandled_error())?,
            logical_id: row.get(2).map_err(|_| route_support::unhandled_error())?,
            object_set_digest: row.get(3).map_err(|_| route_support::unhandled_error())?,
            committed_at: row.get(4).map_err(|_| route_support::unhandled_error())?,
            recoverable: recoverable_int != 0,
            state: row.get(6).map_err(|_| route_support::unhandled_error())?,
            metadata: meta,
        });
    }
    Ok(out)
}

fn target_used_ratio_percent(
    target_id: &str,
    target_meta: &Value,
    copies: &[CopyRecord],
) -> Result<Option<f64>, AppError> {
    let kind = target_meta
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("filesystem");
    if kind == "s3" {
        return Err(AppError {
            status: 503,
            code: "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED",
            message: format!(
                "Rebalance capacity evaluation for remote S3 target {target_id} is not migrated to native storage yet"
            ),
        });
    }
    let Some(quota) = target_meta.get("quotaBytes").and_then(Value::as_i64) else {
        return Ok(None);
    };
    if quota <= 0 {
        return Ok(None);
    }
    let mut seen_digests = BTreeSet::new();
    let mut used: i64 = 0;
    for copy in copies {
        if copy.target_id != target_id {
            continue;
        }
        let digest = if !copy.object_set_digest.is_empty() {
            copy.object_set_digest.clone()
        } else {
            copy.backup_id.clone()
        };
        if !seen_digests.insert(digest) {
            continue;
        }
        let size = copy
            .metadata
            .get("physicalBytes")
            .or_else(|| copy.metadata.get("ciphertextBytes"))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        if size > 0 {
            used = used.saturating_add(size);
        }
    }
    let used_pct = (used as f64 / quota as f64) * 100.0;
    Ok(Some(used_pct))
}

fn resolve_local_target_root(
    target_id: &str,
    targets_by_id: &BTreeMap<String, Value>,
) -> Result<PathBuf, AppError> {
    if target_id == "managed-local" {
        return Ok(crate::data_routes::workspace_root().join(".backups"));
    }
    let meta = match targets_by_id.get(target_id) {
        Some(v) => v.clone(),
        None => registered_target(target_id)?,
    };
    let kind = meta
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("filesystem");
    if kind != "filesystem" {
        return Err(AppError {
            status: 503,
            code: "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED",
            message: format!(
                "Rebalance for remote target {target_id} ({kind}) is not migrated yet"
            ),
        });
    }
    let root = PathBuf::from(meta.get("path").and_then(Value::as_str).unwrap_or(""));
    verify_registered_target_ready_readonly(target_id, &root, &meta).map_err(|detail| {
        AppError {
            status: 409,
            code: deepseek_policy::app_error::codes::INVALID_REQUEST,
            message: detail,
        }
    })?;
    Ok(root)
}

fn copy_local_file_atomic(src: &Path, dst: &Path) -> Result<u64, AppError> {
    let bytes = fs::read(src).map_err(|_| route_support::unhandled_error())?;
    let len = bytes.len() as u64;
    if dst.is_file() {
        return Ok(len);
    }
    let parent = dst.parent().ok_or_else(route_support::unhandled_error)?;
    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    let file_name = dst
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or_else(route_support::unhandled_error)?;
    let tmp = parent.join(format!(".{file_name}.{}.tmp", std::process::id()));
    {
        let mut handle = fs::File::create(&tmp).map_err(|_| route_support::unhandled_error())?;
        handle
            .write_all(&bytes)
            .map_err(|_| route_support::unhandled_error())?;
        handle
            .sync_all()
            .map_err(|_| route_support::unhandled_error())?;
    }
    if let Err(err) = fs::rename(&tmp, dst) {
        let _ = fs::remove_file(&tmp);
        return Err(AppError {
            status: 500,
            code: deepseek_policy::app_error::codes::INTERNAL,
            message: format!("Replica copy failed: {err}"),
        });
    }
    Ok(len)
}

struct RebalanceCopyInput<'a> {
    policy_id: &'a str,
    backup_id: &'a str,
    src_target_id: &'a str,
    dst_target_id: &'a str,
    source_copy: &'a CopyRecord,
    targets_by_id: &'a BTreeMap<String, Value>,
    now: &'a str,
}

fn transfer_local_replica_copy(input: &RebalanceCopyInput<'_>) -> Result<u64, AppError> {
    route_support::require_native_store(
        "backup_catalog_store",
        "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED",
    )?;
    route_support::require_native_store(
        "backup_dr_ledger_store",
        "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED",
    )?;
    let src_root = resolve_local_target_root(input.src_target_id, input.targets_by_id)?;
    let dst_root = resolve_local_target_root(input.dst_target_id, input.targets_by_id)?;
    let src_state = catalog_state(&src_root)?;
    let Some(src_record) = src_state.get(input.backup_id) else {
        return Err(AppError::not_found(format!(
            "Source backup {} not found on target {}",
            input.backup_id, input.src_target_id
        )));
    };
    let mut transferred: u64 = 0;
    if is_object_set(src_record) {
        let inventory = committed_object_inventory(src_record)
            .map_err(|()| route_support::unhandled_error())?;
        for obj in inventory {
            let src_obj = object_path(&src_root, &obj.digest);
            let dst_obj = object_path(&dst_root, &obj.digest);
            if src_obj.is_file() {
                transferred =
                    transferred.saturating_add(copy_local_file_atomic(&src_obj, &dst_obj)?);
            }
        }
    } else {
        let candidates = backup_file_candidates(&src_root, src_record);
        for candidate in candidates {
            if candidate.is_file() {
                if let Ok(rel) = candidate.strip_prefix(&src_root) {
                    transferred = transferred
                        .saturating_add(copy_local_file_atomic(&candidate, &dst_root.join(rel))?);
                }
            }
        }
    }

    let lease = acquire_target_writer(&dst_root, input.dst_target_id)?;
    let dst_state = catalog_state(&dst_root)?;
    if !dst_state.contains_key(input.backup_id) {
        append_catalog_entry(&dst_root, "receipt", src_record, &lease, input.now)?;
    }

    let dr_db = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    if let Some(parent) = dr_db.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    let conn = Connection::open(&dr_db).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(Duration::from_millis(5000))
        .map_err(|_| route_support::unhandled_error())?;
    let meta_json =
        serde_json::to_string(&input.source_copy.metadata).unwrap_or_else(|_| "{}".to_string());
    let has_table: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_point_copies'",
            [],
            |_| Ok(true),
        )
        .unwrap_or(false);
    if has_table {
        let osd: Option<&str> = if input.source_copy.object_set_digest.is_empty() {
            None
        } else {
            Some(input.source_copy.object_set_digest.as_str())
        };
        conn.execute(
            "INSERT INTO recovery_point_copies(\
                target_id, policy_id, backup_id, logical_id, object_set_digest, \
                committed_at, recoverable, role, mode, state, verified_at, metadata_json\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, 'replica', 'required', 'healthy', ?7, ?8) \
             ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET \
                recoverable = 1, state = 'healthy', verified_at = excluded.verified_at",
            rusqlite::params![
                input.dst_target_id,
                input.policy_id,
                input.backup_id,
                input.source_copy.logical_id,
                osd,
                input.source_copy.committed_at,
                input.now,
                meta_json,
            ],
        )
        .map_err(|_| route_support::unhandled_error())?;
    }
    Ok(transferred)
}

fn handle_trigger_rebalance(headers: &HeaderMap, body: &[u8]) -> Result<Value, AppError> {
    let payload = route_support::read_json_object(headers, body, BODY_LIMIT)?;
    let policy_id = payload
        .get("policyId")
        .map(value_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if policy_id.is_empty() {
        return Err(AppError::invalid_payload(
            "policyId is required to trigger rebalance",
        ));
    }
    route_support::require_native_store("backup_replication_store", WRITE_NOT_OWNED)?;

    let policy = load_backup_policy(&policy_id)?;
    let Some(repl) = policy.get("replication").and_then(Value::as_object) else {
        return Ok(json!({"status": "skipped", "reason": "replication-disabled"}));
    };
    if !repl.get("enabled").is_some_and(python_truthy) {
        return Ok(json!({"status": "skipped", "reason": "replication-disabled"}));
    }
    let now_secs = unix_seconds();
    if !inside_maintenance_window(&policy, now_secs as i64) {
        return Ok(json!({"status": "skipped", "reason": "outside-maintenance-window"}));
    }

    let min_fd = repl
        .get("minFailureDomains")
        .and_then(Value::as_i64)
        .unwrap_or(1)
        .max(1) as usize;
    let max_per_fd = policy
        .get("placement")
        .and_then(Value::as_object)
        .and_then(|p| p.get("maxCopiesPerFailureDomain"))
        .or_else(|| repl.get("maxCopiesPerFailureDomain"))
        .and_then(Value::as_i64)
        .filter(|v| *v > 0);
    let soft_watermark = policy
        .get("placement")
        .and_then(Value::as_object)
        .and_then(|p| p.get("softWatermarkPercent"))
        .and_then(Value::as_f64)
        .unwrap_or(80.0);

    let all_targets = list_registered_targets()?;
    let mut targets_by_id: BTreeMap<String, Value> = BTreeMap::new();
    for t in all_targets {
        if let Some(id) = t.get("targetId").and_then(Value::as_str) {
            targets_by_id.insert(id.to_string(), t);
        }
    }

    let mut active_targets: Vec<String> = Vec::new();
    if let Some(entries) = repl.get("targets").and_then(Value::as_array) {
        for entry in entries {
            let Some(tid) = entry
                .as_object()
                .and_then(|o| o.get("targetId"))
                .and_then(Value::as_str)
            else {
                continue;
            };
            if tid.is_empty() {
                continue;
            }
            let drain_state = targets_by_id
                .get(tid)
                .and_then(|t| t.get("drainState"))
                .and_then(Value::as_str)
                .unwrap_or("active");
            if drain_state != "draining" {
                active_targets.push(tid.to_string());
            }
        }
    }

    let copies = load_policy_copies(&policy_id)?;
    let mut by_backup: BTreeMap<String, Vec<CopyRecord>> = BTreeMap::new();
    for copy in &copies {
        by_backup
            .entry(copy.backup_id.clone())
            .or_default()
            .push(copy.clone());
    }

    let existing_jobs = list_rebalance_jobs(Some(&policy_id))?;
    let mut jobs_created: usize = 0;

    for (backup_id, copy_list) in &by_backup {
        if jobs_created >= MAX_JOBS_PER_TRIGGER {
            break;
        }
        let healthy: Vec<&CopyRecord> = copy_list
            .iter()
            .filter(|c| c.recoverable && c.state == "healthy")
            .collect();
        if healthy.is_empty() {
            continue;
        }
        let healthy_ids: BTreeSet<String> = healthy.iter().map(|c| c.target_id.clone()).collect();
        let current_fds: BTreeSet<String> = healthy_ids
            .iter()
            .map(|tid| {
                targets_by_id
                    .get(tid)
                    .and_then(|t| t.get("failureDomain"))
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("default")
                    .to_string()
            })
            .collect();
        let has_draining = healthy_ids.iter().any(|tid| {
            targets_by_id
                .get(tid)
                .and_then(|t| t.get("drainState"))
                .and_then(Value::as_str)
                == Some("draining")
        });

        let mut constrained_source: Option<String> = None;
        for tid in &healthy_ids {
            if let Some(meta) = targets_by_id.get(tid) {
                if let Some(used_pct) = target_used_ratio_percent(tid, meta, &copies)? {
                    if used_pct >= soft_watermark {
                        constrained_source = Some(tid.clone());
                        break;
                    }
                }
            }
        }
        let has_capacity_pressure = constrained_source.is_some();
        let needs_rebalance = current_fds.len() < min_fd || has_draining || has_capacity_pressure;
        if !needs_rebalance {
            continue;
        }

        for cand_tid in &active_targets {
            if healthy_ids.contains(cand_tid) {
                continue;
            }
            let cand_fd = targets_by_id
                .get(cand_tid)
                .and_then(|t| t.get("failureDomain"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("default")
                .to_string();
            if let Some(limit) = max_per_fd {
                let in_fd = healthy_ids
                    .iter()
                    .filter(|tid| {
                        targets_by_id
                            .get(*tid)
                            .and_then(|t| t.get("failureDomain"))
                            .and_then(Value::as_str)
                            .filter(|s| !s.is_empty())
                            .unwrap_or("default")
                            == cand_fd
                    })
                    .count() as i64;
                if in_fd + 1 > limit {
                    continue;
                }
            }
            if let Some(meta) = targets_by_id.get(cand_tid) {
                if let Some(used_pct) = target_used_ratio_percent(cand_tid, meta, &copies)? {
                    if used_pct >= soft_watermark {
                        continue;
                    }
                }
            }
            if !has_draining && !has_capacity_pressure && current_fds.contains(&cand_fd) {
                continue;
            }

            let src_tid = constrained_source
                .clone()
                .unwrap_or_else(|| healthy[0].target_id.clone());
            let source_copy = healthy
                .iter()
                .find(|c| c.target_id == src_tid)
                .copied()
                .unwrap_or(healthy[0]);
            let already_active = existing_jobs.iter().any(|job| {
                job.get("backupId").and_then(Value::as_str) == Some(backup_id.as_str())
                    && job.get("destTargetId").and_then(Value::as_str) == Some(cand_tid.as_str())
                    && !matches!(
                        job.get("phase").and_then(Value::as_str).unwrap_or(""),
                        "complete" | "failed" | "cancelled"
                    )
            });
            if already_active {
                jobs_created += 1;
                break;
            }

            let reason = if has_draining {
                "drain-migration"
            } else if has_capacity_pressure {
                "proactive-capacity-rebalance"
            } else {
                "failure-domain-diversity"
            };
            let prune_source = has_draining || has_capacity_pressure;
            let now_str = utc_iso_from_unix(now_secs);
            let job_id = format!("rebalance_{}", SystemEntropy.new_id()?);
            let transferred = transfer_local_replica_copy(&RebalanceCopyInput {
                policy_id: &policy_id,
                backup_id,
                src_target_id: &src_tid,
                dst_target_id: cand_tid,
                source_copy,
                targets_by_id: &targets_by_id,
                now: &now_str,
            })?;
            let job_doc = json!({
                "schemaVersion": 1,
                "jobId": job_id,
                "resilienceActionId": Value::Null,
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": src_tid,
                "destTargetId": cand_tid,
                "reason": reason,
                "pruneSourceAfter": prune_source,
                "phase": "complete",
                "bytesTransferred": transferred,
                "createdAt": now_str,
                "updatedAt": utc_iso_from_unix(unix_seconds()),
            });
            route_support::write_json_atomic(
                &rebalance_dir().join(format!("{job_id}.json")),
                &job_doc,
            )?;
            jobs_created += 1;
            break;
        }
    }

    Ok(json!({
        "status": "completed",
        "jobsCreated": jobs_created,
    }))
}
