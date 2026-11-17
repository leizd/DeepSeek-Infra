//! Scheduled backup retention routes (`backup_governance.py` + `backup_retention.py`, S3).
//!
//! - `POST /api/workspace/retention/preview` — read-only retention evaluation.
//!   Never creates `.backups`, `.backup-scheduler`, `.backup-retention`, or
//!   `.backup-control` on an uninitialized workspace.
//! - `POST /api/workspace/retention/apply` — transactional two-phase retention
//!   enforcement gated on `backup_retention_store`, `backup_catalog_store`, and
//!   `backup_dr_ledger_store` (`DEEPSEEK_RUNTIME_MODE=python_disabled`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use axum::body::Bytes;
use axum::http::Method;
use axum::response::Response;
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::{fsync_dir, now_iso, sha256_file};
use deepseek_policy::backup_retention::{
    self, PreviewInput, TRASH_JOURNAL_NAME, collect_restore_references, compute_preview,
    get_retention_policy, grace_expired, parse_utc_opt, restore_trash_entry, trash_journal_write,
    validate_preview_snapshot,
};
use deepseek_policy::backup_unlock::inspect_age_file_header;
use deepseek_policy::backup_writer_lease::TargetWriterLease;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use deepseek_policy::tzif::load_iana_timezone;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::{
    backup_file_candidates, catalog_state, committed_object_inventory, is_object_set, list_backups,
    object_path, open_readonly_sqlite, read_entries, registered_target, verify_chain,
};
use crate::backup_catalog_write_routes::{
    acquire_target_writer, append_catalog_entry, verify_registered_target_ready_readonly,
};
use crate::backup_commit::{
    CommitMarker, commit_marker_valid, read_commit_markers, target_generation,
};
use crate::route_support;

const BACKUP_RETENTION_DOMAIN: &str = "backup_retention_store";
const BACKUP_RETENTION_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_RETENTION_WRITE_NOT_OWNED";
const BACKUP_CATALOG_DOMAIN: &str = "backup_catalog_store";
const BACKUP_CATALOG_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED";
const BACKUP_DR_LEDGER_DOMAIN: &str = "backup_dr_ledger_store";
const BACKUP_DR_LEDGER_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED";
const TARGET_STORE_NOT_MIGRATED_CODE: &str = "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED";
const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

const ACTIVE_RUN_PHASES: [&str; 10] = [
    "queued",
    "leased",
    "waiting-for-mirror",
    "snapshotting",
    "encrypting",
    "verifying",
    "publishing",
    "cataloging",
    "pruning",
    "reconciling",
];

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/retention/preview", any(preview_response))
        .route("/api/workspace/retention/apply", any(apply_response))
}

async fn preview_response(method: Method, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || handle_preview(&body)).await;
    match joined {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn apply_response(method: Method, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || handle_apply(&body)).await;
    match joined {
        Ok(Ok(payload)) => Json(payload).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn json_str_or(obj: &Value, key: &str, default: &str) -> String {
    match obj.get(key) {
        Some(value) if python_truthy(value) => {
            let text = value_str(value);
            if text.is_empty() {
                default.to_string()
            } else {
                text
            }
        }
        _ => default.to_string(),
    }
}

fn is_valid_safe_id(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return false;
    }
    let first = bytes[0];
    if !matches!(first, b'a'..=b'z' | b'0'..=b'9') {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'.' | b'_' | b'-'))
}

fn extract_policy_id(body: &Map<String, Value>) -> Result<String, AppError> {
    let raw = match body.get("policyId") {
        Some(value) if python_truthy(value) => value_str(value).trim().to_string(),
        _ => String::new(),
    };
    if !is_valid_safe_id(&raw) {
        return Err(AppError {
            message: "Backup policy field policyId has an invalid identifier".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(raw)
}

fn load_backup_policy(policy_id: &str) -> Result<Value, AppError> {
    let workspace = crate::data_routes::workspace_root();
    let control_db = workspace.join(".backup-control").join("control.sqlite3");
    if let Some(conn) = open_readonly_sqlite(&control_db)? {
        let table: Option<String> = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'control_policies'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| route_support::unhandled_error())?;
        if table.is_some() {
            let payload: Option<String> = conn
                .query_row(
                    "SELECT payload_json FROM control_policies WHERE policy_id = ?1",
                    [policy_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| route_support::unhandled_error())?;
            if let Some(raw) = payload {
                let parsed: Value = serde_json::from_str(&raw).map_err(|_| AppError {
                    message: "Backup policy store is unreadable".to_string(),
                    code: codes::INTERNAL,
                    status: 500,
                })?;
                if parsed.is_object() {
                    return Ok(parsed);
                }
                return Err(AppError {
                    message: "Backup policy store is unreadable".to_string(),
                    code: codes::INTERNAL,
                    status: 500,
                });
            }
        }
    }

    let path = workspace
        .join(".backup-policies")
        .join(format!("{policy_id}.json"));
    if !path.is_file() {
        return Err(AppError {
            message: "Backup policy not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    }
    let unreadable = || AppError {
        message: "Backup policy store is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    };
    let text = fs::read_to_string(&path).map_err(|_| unreadable())?;
    let parsed: Value = serde_json::from_str(&text).map_err(|_| unreadable())?;
    if !parsed.is_object() {
        return Err(unreadable());
    }
    Ok(parsed)
}

struct ResolvedRetentionContext {
    policy_id: String,
    retention: Value,
    target_root: PathBuf,
    target_id_display: String,
    policy_timezone: String,
    tz: deepseek_policy::tzif::IanaTimeZone,
}

fn resolve_target_root(policy: &Value) -> Result<(PathBuf, String), AppError> {
    let target_id = json_str_or(policy, "targetId", "managed-local");
    let workspace = crate::data_routes::workspace_root();
    if target_id == "managed-local" {
        return Ok((workspace.join(".backups"), "managed-local".to_string()));
    }
    let target = registered_target(&target_id)?;
    let kind = target.get("kind").and_then(Value::as_str).unwrap_or("");
    let path_str = target.get("path").and_then(Value::as_str).unwrap_or("");
    if kind != "filesystem" || path_str.is_empty() {
        return Err(AppError {
            message:
                "backup retention for a remote registered target is not served by the native runtime yet"
                    .to_string(),
            code: TARGET_STORE_NOT_MIGRATED_CODE,
            status: 503,
        });
    }
    let root = PathBuf::from(path_str);
    verify_registered_target_ready_readonly(&target_id, &root, &target).map_err(|msg| {
        AppError {
            message: msg,
            code: codes::INVALID_REQUEST,
            status: 409,
        }
    })?;
    Ok((root, target_id))
}

fn resolve_retention_context(
    body: &Map<String, Value>,
) -> Result<ResolvedRetentionContext, AppError> {
    let policy_id = extract_policy_id(body)?;
    let policy = load_backup_policy(&policy_id)?;
    let policy_timezone = json_str_or(&policy, "timezone", "UTC");
    let tz = load_iana_timezone(&policy_timezone)?;
    let retention_policy_id = json_str_or(&policy, "retentionPolicyId", "default");
    let retention = get_retention_policy(
        &crate::data_routes::workspace_root().join(".backup-retention"),
        &retention_policy_id,
    )?;
    let (target_root, target_id_display) = resolve_target_root(&policy)?;
    Ok(ResolvedRetentionContext {
        policy_id,
        retention,
        target_root,
        target_id_display,
        policy_timezone,
        tz,
    })
}

fn catalog_head_hash(root: &Path) -> Result<String, AppError> {
    let entries = read_entries(root)?;
    match entries.last() {
        Some(last) => Ok(last
            .get("entryHash")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| GENESIS_HASH.to_string())),
        None => Ok(GENESIS_HASH.to_string()),
    }
}

fn active_run_backup_ids() -> Result<HashSet<String>, AppError> {
    let db = crate::data_routes::workspace_root()
        .join(".backup-scheduler")
        .join("scheduler.db");
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(HashSet::new());
    };
    let table: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'backup_runs'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| route_support::unhandled_error())?;
    if table.is_none() {
        return Ok(HashSet::new());
    }
    let mut stmt = conn
        .prepare("SELECT phase, backup_id FROM backup_runs")
        .map_err(|_| route_support::unhandled_error())?;
    let rows = stmt
        .query_map([], |row| {
            let phase: String = row.get(0)?;
            let backup_id: Option<String> = row.get(1)?;
            Ok((phase, backup_id))
        })
        .map_err(|_| route_support::unhandled_error())?;
    let mut active = HashSet::new();
    for item in rows {
        let Ok((phase, backup_id)) = item else {
            continue;
        };
        if ACTIVE_RUN_PHASES.contains(&phase.as_str()) {
            if let Some(bid) = backup_id {
                let trimmed = bid.trim();
                if !trimmed.is_empty() {
                    active.insert(trimmed.to_string());
                }
            }
        }
    }
    Ok(active)
}

fn is_symlink_path(path: &Path) -> bool {
    fs::symlink_metadata(path)
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(false)
}

fn valid_commit_markers_by_backup(root: &Path) -> HashMap<String, CommitMarker> {
    let mut map = HashMap::new();
    for marker in read_commit_markers(root) {
        if !commit_marker_valid(&marker.document) {
            continue;
        }
        let backup_id = marker
            .document
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if backup_id.is_empty() {
            continue;
        }
        let replace = match map.get(&backup_id) {
            Some(existing) => marker.generation() > existing.generation(),
            None => true,
        };
        if replace {
            map.insert(backup_id, marker);
        }
    }
    map
}

fn record_requires_commit_marker(record: &Value) -> bool {
    let schema_ver = record
        .get("schemaVersion")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    if schema_ver >= 2 || is_object_set(record) {
        return true;
    }
    matches!(record.get("objectDigest"), Some(v) if python_truthy(v))
}

fn receipt_file_status(root: &Path, backup_id: &str) -> Result<Option<(PathBuf, Value)>, ()> {
    let receipt_path = root.join("receipts").join(format!("{backup_id}.json"));
    if !receipt_path.is_file() {
        return Ok(None);
    }
    let text = fs::read_to_string(&receipt_path).map_err(|_| ())?;
    let parsed: Value = serde_json::from_str(&text).map_err(|_| ())?;
    if !parsed.is_object() {
        return Err(());
    }
    Ok(Some((receipt_path, parsed)))
}

fn marker_matches_record(
    marker: &CommitMarker,
    record: &Value,
    receipt_path: Option<&Path>,
) -> bool {
    if let Some(m_digest) = marker
        .document
        .get("objectDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
    {
        let r_digest = record
            .get("objectDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if m_digest != r_digest {
            return false;
        }
    }
    if is_object_set(record) {
        if let Some(m_set) = marker
            .document
            .get("objectSetDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
        {
            let r_set = record
                .get("objectSetDigest")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if m_set != r_set {
                return false;
            }
        }
    }
    if let Some(m_receipt) = marker
        .document
        .get("receiptDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
    {
        let Some(rpath) = receipt_path else {
            return false;
        };
        let Ok(actual) = sha256_file(rpath) else {
            return false;
        };
        if actual != m_receipt {
            return false;
        }
    }
    true
}

fn payload_blobs_healthy(root: &Path, record: &Value) -> bool {
    if is_object_set(record) {
        let Ok(inventory) = committed_object_inventory(record) else {
            return false;
        };
        for item in inventory {
            let path = object_path(root, &item.digest);
            if !path.is_file() || is_symlink_path(&path) {
                return false;
            }
            if !matches!(inspect_age_file_header(&path), Ok(true)) {
                return false;
            }
        }
        return true;
    }

    let candidates = backup_file_candidates(root, record);
    let existing: Vec<PathBuf> = candidates
        .into_iter()
        .filter(|path| path.is_file() && !is_symlink_path(path))
        .collect();
    if existing.is_empty() {
        return false;
    }
    for path in existing {
        if !matches!(inspect_age_file_header(&path), Ok(true)) {
            return false;
        }
    }
    true
}

fn record_is_healthy(root: &Path, record: &Value, markers: &HashMap<String, CommitMarker>) -> bool {
    if !python_truthy(record.get("creationVerified").unwrap_or(&Value::Null))
        || python_truthy(record.get("trashed").unwrap_or(&Value::Null))
        || python_truthy(record.get("deleted").unwrap_or(&Value::Null))
    {
        return false;
    }
    let backup_id = record
        .get("backupId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if backup_id.is_empty() {
        return false;
    }

    let receipt_info = match receipt_file_status(root, &backup_id) {
        Ok(info) => info,
        Err(()) => return false,
    };
    if record_requires_commit_marker(record) {
        let Some(marker) = markers.get(&backup_id) else {
            return false;
        };
        let Some((ref receipt_path, _)) = receipt_info else {
            return false;
        };
        if !marker_matches_record(marker, record, Some(receipt_path)) {
            return false;
        }
    }
    payload_blobs_healthy(root, record)
}

fn compute_target_preview(ctx: &ResolvedRetentionContext) -> Result<Value, AppError> {
    let all_records = list_backups(&ctx.target_root, Some(&ctx.policy_id))?;
    let markers = valid_commit_markers_by_backup(&ctx.target_root);
    let healthy_ids: Vec<String> = all_records
        .iter()
        .filter(|rec| record_is_healthy(&ctx.target_root, rec, &markers))
        .filter_map(|rec| {
            rec.get("backupId")
                .filter(|v| python_truthy(v))
                .map(value_str)
        })
        .collect();
    let active_ids = active_run_backup_ids()?;
    let restore_refs =
        collect_restore_references(&crate::data_routes::workspace_root().join(".restore-staging"));
    let head_hash = catalog_head_hash(&ctx.target_root)?;
    let tgen = target_generation(&ctx.target_root);
    let now = backup_retention::chrono::Utc::now();

    let target_display = ctx.target_root.to_string_lossy().into_owned();
    Ok(compute_preview(PreviewInput {
        retention: &ctx.retention,
        all_records: &all_records,
        healthy_ids: &healthy_ids,
        active_run_ids: &active_ids,
        restore_references: &restore_refs,
        target_id_display: &target_display,
        target_generation: tgen,
        catalog_head_hash: &head_hash,
        policy_timezone: &ctx.policy_timezone,
        tz: &ctx.tz,
        now,
    }))
}

fn handle_preview(body_bytes: &[u8]) -> Result<Value, AppError> {
    let body = route_support::read_json_object(body_bytes, 64_000)?;
    let ctx = resolve_retention_context(&body)?;
    let mut preview = compute_target_preview(&ctx)?;
    if let Some(obj) = preview.as_object_mut() {
        obj.shift_remove("trashRecords");
    }
    Ok(preview)
}

// --- apply pipeline -----------------------------------------------------------------

fn catalog_corrupt_error(detail: impl Into<String>) -> AppError {
    AppError {
        message: format!("catalog-corrupt: {}", detail.into()),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

fn assert_catalog_committed(root: &Path) -> Result<(), AppError> {
    if !verify_chain(root)? {
        return Err(catalog_corrupt_error(
            "Backup catalog is corrupt; rebuild it from receipts",
        ));
    }
    let state = catalog_state(root)?;
    let markers = valid_commit_markers_by_backup(root);
    for (backup_id, record) in state {
        if python_truthy(record.get("deleted").unwrap_or(&Value::Null)) {
            continue;
        }
        if !record_requires_commit_marker(&record) {
            continue;
        }
        let receipt_info = receipt_file_status(root, &backup_id)
            .map_err(|()| catalog_corrupt_error(format!("receipt {backup_id} is corrupt")))?;
        let Some(marker) = markers.get(&backup_id) else {
            return Err(catalog_corrupt_error(format!(
                "uncommitted catalog entry: {backup_id}"
            )));
        };
        let Some((ref receipt_path, _)) = receipt_info else {
            return Err(catalog_corrupt_error(format!(
                "missing receipt for committed entry: {backup_id}"
            )));
        };
        if !marker_matches_record(marker, &record, Some(receipt_path)) {
            return Err(catalog_corrupt_error(format!(
                "commit marker mismatch for {backup_id}"
            )));
        }
        if !python_truthy(record.get("trashed").unwrap_or(&Value::Null)) {
            if is_object_set(&record) {
                let inventory = committed_object_inventory(&record).map_err(|()| {
                    catalog_corrupt_error(format!("invalid object-set inventory for {backup_id}"))
                })?;
                for item in inventory {
                    if !object_path(root, &item.digest).is_file() {
                        return Err(catalog_corrupt_error(format!(
                            "missing committed object for {backup_id}"
                        )));
                    }
                }
            } else {
                let any_blob = backup_file_candidates(root, &record)
                    .into_iter()
                    .any(|p| p.is_file());
                if !any_blob {
                    return Err(catalog_corrupt_error(format!(
                        "missing ciphertext object for {backup_id}"
                    )));
                }
            }
        }
    }
    Ok(())
}

fn open_scheduler_rw() -> Result<Connection, AppError> {
    let dir = crate::data_routes::workspace_root().join(".backup-scheduler");
    fs::create_dir_all(&dir).map_err(|_| route_support::unhandled_error())?;
    let db_path = dir.join("scheduler.db");
    let conn = Connection::open(&db_path).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS retention_runs (
            retention_run_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status TEXT NOT NULL,
            preview TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );",
    )
    .map_err(|_| route_support::unhandled_error())?;
    Ok(conn)
}

fn record_scheduler_retention_run(
    run_id: &str,
    policy_id: &str,
    target_id: &str,
    status: &str,
    preview: Option<&Value>,
    now_str: &str,
) -> Result<(), AppError> {
    let conn = open_scheduler_rw()?;
    let preview_str = serde_json::to_string(preview.unwrap_or(&json!({})))
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute(
        "INSERT INTO retention_runs(retention_run_id, policy_id, target_id, status, preview, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT(retention_run_id) DO UPDATE SET status = excluded.status, preview = excluded.preview, updated_at = excluded.updated_at",
        rusqlite::params![run_id, policy_id, target_id, status, preview_str, now_str, now_str],
    )
    .map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

fn is_source_held(
    target_id: &str,
    backup_id: &str,
    now: backup_retention::chrono::DateTime<backup_retention::chrono::Utc>,
) -> bool {
    let holds_dir = crate::data_routes::workspace_root()
        .join(".backup-replication")
        .join("holds");
    let Ok(entries) = fs::read_dir(&holds_dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let matches_target = obj
            .get("sourceTargetId")
            .or_else(|| obj.get("targetId"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .as_deref()
            == Some(target_id);
        let matches_backup = obj
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .as_deref()
            == Some(backup_id);
        if !matches_target || !matches_backup {
            continue;
        }
        if let Some(expires) = parse_utc_opt(obj.get("expiresAt")) {
            if expires > now {
                return true;
            }
        } else {
            return true;
        }
    }
    false
}

fn has_other_recoverable_copy(backup_id: &str, current_target_id: &str) -> Result<bool, AppError> {
    let db = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(false);
    };
    let table: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'recovery_point_copies'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| route_support::unhandled_error())?;
    if table.is_none() {
        return Ok(false);
    }
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM recovery_point_copies WHERE backup_id = ?1 AND recoverable = 1 AND target_id != ?2 LIMIT 1",
            rusqlite::params![backup_id, current_target_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| route_support::unhandled_error())?;
    Ok(found.is_some())
}

fn retire_logical_recovery_point(backup_id: &str, retired_at: &str) -> Result<(), AppError> {
    let db = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    if !db.is_file() {
        return Ok(());
    }
    let conn = Connection::open(&db).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .map_err(|_| route_support::unhandled_error())?;
    let table: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'logical_recovery_points'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| route_support::unhandled_error())?;
    if table.is_none() {
        return Ok(());
    }
    conn.execute(
        "UPDATE logical_recovery_points SET retained = 0, retired_at = ?1 WHERE backup_id = ?2",
        rusqlite::params![retired_at, backup_id],
    )
    .map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

fn move_file_preserving(src: &Path, dst: &Path) -> Result<(), AppError> {
    if !src.is_file() {
        return Ok(());
    }
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    }
    match fs::rename(src, dst) {
        Ok(()) => Ok(()),
        Err(_) => {
            fs::copy(src, dst).map_err(|_| route_support::unhandled_error())?;
            fs::remove_file(src).map_err(|_| route_support::unhandled_error())?;
            Ok(())
        }
    }
}

fn unique_file_names(values: Option<&Value>) -> Vec<String> {
    let mut out = Vec::new();
    let Some(arr) = values.and_then(Value::as_array) else {
        return out;
    };
    for item in arr {
        let name = value_str(item);
        if !name.is_empty() && !name.contains('/') && !name.contains('\\') && !name.contains("..") {
            out.push(name);
        }
    }
    out
}

fn move_payload_file_into_trash(root: &Path, trash_dir: &Path, name: &str) -> Result<(), AppError> {
    let dst = trash_dir.join(name);
    if dst.is_file() {
        return Ok(());
    }
    if let Some(stem) = name.strip_suffix(".age") {
        if stem.len() == 64 && stem.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
            let obj_src = object_path(root, stem);
            if obj_src.is_file() {
                return move_file_preserving(&obj_src, &dst);
            }
        }
    }
    for candidate in [root.join("backups").join(name), root.join(name)] {
        if candidate.is_file() {
            return move_file_preserving(&candidate, &dst);
        }
    }
    Ok(())
}

fn recover_trash_journals(
    root: &Path,
    writer: &TargetWriterLease,
    at_iso: &str,
) -> Result<Vec<String>, AppError> {
    let trash_root = root.join(".trash");
    if !trash_root.is_dir() {
        return Ok(Vec::new());
    }
    let mut entry_dirs: Vec<PathBuf> = fs::read_dir(&trash_root)
        .map_err(|_| route_support::unhandled_error())?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    entry_dirs.sort();

    let mut recovered = Vec::new();
    let mut state = catalog_state(root)?;
    for entry_dir in entry_dirs {
        let journal_path = entry_dir.join(TRASH_JOURNAL_NAME);
        if !journal_path.is_file() {
            continue;
        }
        let Ok(text) = fs::read_to_string(&journal_path) else {
            continue;
        };
        let Ok(mut journal) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let phase = journal
            .get("phase")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if phase == "event-committed" {
            continue;
        }
        writer.assert_owned()?;
        let backup_id = journal
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| {
                entry_dir
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or_default()
                    .to_string()
            });
        if backup_id.is_empty() {
            continue;
        }

        for name in unique_file_names(journal.get("payloadNames")) {
            move_payload_file_into_trash(root, &entry_dir, &name)?;
        }
        if let Some(filename) = journal
            .get("filename")
            .filter(|v| python_truthy(v))
            .map(value_str)
        {
            if !filename.contains('/') && !filename.contains('\\') {
                move_payload_file_into_trash(root, &entry_dir, &filename)?;
            }
        }
        for rname in unique_file_names(journal.get("receiptNames")) {
            let receipt_src = root.join("receipts").join(&rname);
            if receipt_src.is_file() {
                move_file_preserving(&receipt_src, &entry_dir.join(&rname))?;
            }
        }
        let default_receipt = root.join("receipts").join(format!("{backup_id}.json"));
        if default_receipt.is_file() {
            move_file_preserving(
                &default_receipt,
                &entry_dir.join(format!("{backup_id}.json")),
            )?;
        }

        if let Some(obj) = journal.as_object_mut() {
            obj.insert(
                "phase".to_string(),
                Value::String("files-moved".to_string()),
            );
        }
        trash_journal_write(&entry_dir, &journal)?;

        let already_trashed = state
            .get(&backup_id)
            .map(|rec| python_truthy(rec.get("trashed").unwrap_or(&Value::Null)))
            .unwrap_or(false);
        if !already_trashed {
            let run_id = journal
                .get("retentionRunId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let rec_at = journal
                .get("recordedAt")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| at_iso.to_string());
            let payload = json!({
                "backupId": backup_id,
                "retentionRunId": run_id,
                "trashedAt": rec_at,
            });
            append_catalog_entry(root, "trash", &payload, writer, at_iso)?;
            state = catalog_state(root)?;
        }
        if let Some(obj) = journal.as_object_mut() {
            obj.insert(
                "phase".to_string(),
                Value::String("event-committed".to_string()),
            );
        }
        trash_journal_write(&entry_dir, &journal)?;
        fsync_dir(&entry_dir);
        recovered.push(backup_id);
    }
    Ok(recovered)
}

fn execute_trash_move(
    root: &Path,
    record: &Value,
    retention_run_id: &str,
    writer: &TargetWriterLease,
    at_iso: &str,
) -> Result<bool, AppError> {
    let backup_id = record
        .get("backupId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if backup_id.is_empty() {
        return Ok(false);
    }
    writer.assert_owned()?;

    let trash_dir = root.join(".trash").join(&backup_id);
    fs::create_dir_all(&trash_dir).map_err(|_| route_support::unhandled_error())?;

    let mut payload_sources = backup_file_candidates(root, record);
    // Avoid double-moving the same path twice.
    let mut seen_paths = HashSet::new();
    payload_sources.retain(|path| seen_paths.insert(path.clone()));

    let mut payload_names = Vec::new();
    for path in &payload_sources {
        if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
            payload_names.push(name.to_string());
        }
    }
    let receipt_path = root.join("receipts").join(format!("{backup_id}.json"));
    let mut receipt_names = Vec::new();
    if receipt_path.is_file() {
        receipt_names.push(format!("{backup_id}.json"));
    }

    let mut journal = json!({
        "schemaVersion": 1,
        "backupId": backup_id,
        "retentionRunId": retention_run_id,
        "filename": record.get("filename").cloned().unwrap_or(Value::Null),
        "objectDigest": record.get("objectDigest").cloned().unwrap_or(Value::Null),
        "payloadNames": payload_names,
        "receiptNames": receipt_names,
        "phase": "intent",
        "recordedAt": at_iso,
    });
    trash_journal_write(&trash_dir, &journal)?;

    for path in &payload_sources {
        if path.is_file() {
            if let Some(name) = path.file_name() {
                move_file_preserving(path, &trash_dir.join(name))?;
            }
        }
    }
    if receipt_path.is_file() {
        move_file_preserving(&receipt_path, &trash_dir.join(format!("{backup_id}.json")))?;
    }

    if let Some(obj) = journal.as_object_mut() {
        obj.insert(
            "phase".to_string(),
            Value::String("files-moved".to_string()),
        );
    }
    trash_journal_write(&trash_dir, &journal)?;

    let payload = json!({
        "backupId": backup_id,
        "retentionRunId": retention_run_id,
        "trashedAt": at_iso,
    });
    append_catalog_entry(root, "trash", &payload, writer, at_iso)?;

    if let Some(obj) = journal.as_object_mut() {
        obj.insert(
            "phase".to_string(),
            Value::String("event-committed".to_string()),
        );
    }
    trash_journal_write(&trash_dir, &journal)?;
    fsync_dir(&trash_dir);
    Ok(true)
}

struct FinalizeOutcome {
    deleted: Vec<String>,
    kept: Vec<String>,
    restored: Vec<String>,
}

fn finalize_retention(
    ctx: &ResolvedRetentionContext,
    writer: &TargetWriterLease,
    now: backup_retention::chrono::DateTime<backup_retention::chrono::Utc>,
) -> Result<FinalizeOutcome, AppError> {
    let at_iso = now_iso(Some(now));
    let grace_hours = ctx
        .retention
        .get("trashGraceHours")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
        .unwrap_or(24);
    let grace = backup_retention::chrono::Duration::hours(grace_hours);

    let all_records = list_backups(&ctx.target_root, Some(&ctx.policy_id))?;
    let active_ids = active_run_backup_ids()?;
    let restore_refs =
        collect_restore_references(&crate::data_routes::workspace_root().join(".restore-staging"));

    // Check which backups must be protected (pinned, active-run, restore-referenced,
    // or ancestor of any non-deleted kept/recoverable snapshot).
    let mut protected_ids = HashSet::<String>::new();
    let protect_pinned = ctx
        .retention
        .get("protectPinned")
        .map(python_truthy)
        .unwrap_or(true);
    let mut active_descendants = HashSet::<String>::new();
    for rec in &all_records {
        let id = rec
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if id.is_empty() {
            continue;
        }
        let filename = json_str_or(rec, "filename", "");
        let sha = json_str_or(rec, "ciphertextSha256", "");
        let trashed = python_truthy(rec.get("trashed").unwrap_or(&Value::Null));
        if !trashed {
            active_descendants.insert(id.clone());
        }
        if protect_pinned && python_truthy(rec.get("pinned").unwrap_or(&Value::Null)) {
            protected_ids.insert(id.clone());
            active_descendants.insert(id.clone());
        }
        if active_ids.contains(&id)
            || restore_refs.contains(&id)
            || (!filename.is_empty() && restore_refs.contains(&filename))
            || (!sha.is_empty() && restore_refs.contains(&sha))
        {
            protected_ids.insert(id.clone());
            active_descendants.insert(id.clone());
        }
    }
    let mut extra_keep = protected_ids.clone();
    let mut extra_reasons = std::collections::BTreeMap::new();
    let _ = backup_retention::protect_snapshot_ancestors(
        &all_records,
        &mut extra_keep,
        &mut extra_reasons,
        Some(&active_descendants),
    );
    protected_ids.extend(extra_keep);

    let mut deleted = Vec::new();
    let mut kept = Vec::new();
    let mut restored = Vec::new();
    for rec in &all_records {
        if !python_truthy(rec.get("trashed").unwrap_or(&Value::Null)) {
            continue;
        }
        if python_truthy(rec.get("deleted").unwrap_or(&Value::Null)) {
            continue;
        }
        let backup_id = rec
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if backup_id.is_empty() {
            continue;
        }
        if protected_ids.contains(&backup_id) {
            writer.assert_owned()?;
            restore_trash_entry(&ctx.target_root, &backup_id)?;
            append_catalog_entry(
                &ctx.target_root,
                "restore-trash",
                &json!({"backupId": backup_id, "restoredAt": at_iso}),
                writer,
                &at_iso,
            )?;
            restored.push(backup_id.clone());
            kept.push(backup_id);
            continue;
        }
        if !grace_expired(rec, now, grace) {
            kept.push(backup_id);
            continue;
        }
        if is_source_held(&ctx.target_id_display, &backup_id, now) {
            kept.push(backup_id);
            continue;
        }
        if has_other_recoverable_copy(&backup_id, &ctx.target_id_display)? {
            kept.push(backup_id);
            continue;
        }
        writer.assert_owned()?;
        let trash_entry = ctx.target_root.join(".trash").join(&backup_id);
        if trash_entry.is_dir() {
            fs::remove_dir_all(&trash_entry).map_err(|_| route_support::unhandled_error())?;
        }
        append_catalog_entry(
            &ctx.target_root,
            "delete",
            &json!({"backupId": backup_id, "deletedAt": at_iso}),
            writer,
            &at_iso,
        )?;
        retire_logical_recovery_point(&backup_id, &at_iso)?;
        deleted.push(backup_id);
    }
    Ok(FinalizeOutcome {
        deleted,
        kept,
        restored,
    })
}

fn clean_preview_for_response(preview: &Value) -> Value {
    let mut copy = preview.clone();
    if let Some(obj) = copy.as_object_mut() {
        obj.shift_remove("trashRecords");
    }
    copy
}

fn apply_response_envelope(
    retention_run_id: &str,
    trashed: Vec<String>,
    recovered_trash: Vec<String>,
    preview: &Value,
    finalized: FinalizeOutcome,
) -> Value {
    let keep = preview.get("keep").cloned().unwrap_or_else(|| json!([]));
    let protected = preview
        .get("protected")
        .cloned()
        .unwrap_or_else(|| json!([]));
    let mut applied = json!({
        "retentionRunId": retention_run_id,
        "trashed": trashed,
        "recoveredTrash": recovered_trash,
        "preview": {
            "keep": keep,
            "protected": protected,
        },
    });
    if !finalized.restored.is_empty() {
        if let Some(obj) = applied.as_object_mut() {
            obj.insert("restored".to_string(), json!(finalized.restored));
        }
    }
    json!({
        "applied": applied,
        "finalized": {
            "deleted": finalized.deleted,
            "kept": finalized.kept,
            "recoveredTrash": [],
        },
    })
}

fn handle_apply(body_bytes: &[u8]) -> Result<Value, AppError> {
    let body = route_support::read_json_object(body_bytes, 64_000)?;
    let ctx = resolve_retention_context(&body)?;

    route_support::require_native_store(BACKUP_RETENTION_DOMAIN, BACKUP_RETENTION_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_CATALOG_DOMAIN, BACKUP_CATALOG_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_DR_LEDGER_DOMAIN, BACKUP_DR_LEDGER_WRITE_NOT_OWNED)?;

    let req_dry_run = body.get("dryRun").map(python_truthy).unwrap_or(false);
    let policy_dry_run = ctx
        .retention
        .get("dryRun")
        .map(python_truthy)
        .unwrap_or(false);
    let dry_run = req_dry_run || policy_dry_run;

    let supplied_preview = match body.get("preview") {
        None | Some(Value::Null) => None,
        Some(other) if other.is_object() => Some(other.clone()),
        Some(_) => {
            return Err(AppError {
                message: "preview must be a JSON object".to_string(),
                code: codes::INVALID_PAYLOAD,
                status: 400,
            });
        }
    };

    if dry_run {
        assert_catalog_committed(&ctx.target_root)?;
        let fresh = compute_target_preview(&ctx)?;
        if let Some(ref preview) = supplied_preview {
            validate_preview_snapshot(
                preview,
                &ctx.retention,
                fresh["targetGeneration"].as_i64().unwrap_or(0),
                fresh["catalogHeadHash"].as_str().unwrap_or(""),
            )?;
        }
        let retention_run_id = supplied_preview
            .as_ref()
            .and_then(|p| p.get("retentionRunId"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| value_str(&fresh["retentionRunId"]));
        return Ok(apply_response_envelope(
            &retention_run_id,
            Vec::new(),
            Vec::new(),
            &fresh,
            FinalizeOutcome {
                deleted: Vec::new(),
                kept: Vec::new(),
                restored: Vec::new(),
            },
        ));
    }

    fs::create_dir_all(&ctx.target_root).map_err(|_| route_support::unhandled_error())?;
    let mut writer = acquire_target_writer(&ctx.target_root, &ctx.target_id_display)?;
    let apply_result = apply_with_lease(&ctx, &writer, supplied_preview.as_ref());
    writer.release();
    apply_result
}

fn apply_with_lease(
    ctx: &ResolvedRetentionContext,
    writer: &TargetWriterLease,
    supplied_preview: Option<&Value>,
) -> Result<Value, AppError> {
    assert_catalog_committed(&ctx.target_root)?;

    let now = backup_retention::chrono::Utc::now();
    let at_iso = now_iso(Some(now));
    let recovered_trash = recover_trash_journals(&ctx.target_root, writer, &at_iso)?;

    let fresh = compute_target_preview(ctx)?;
    if let Some(preview) = supplied_preview {
        validate_preview_snapshot(
            preview,
            &ctx.retention,
            fresh["targetGeneration"].as_i64().unwrap_or(0),
            fresh["catalogHeadHash"].as_str().unwrap_or(""),
        )?;
    }

    let retention_run_id = supplied_preview
        .and_then(|p| p.get("retentionRunId"))
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| value_str(&fresh["retentionRunId"]));

    let clean_preview = clean_preview_for_response(&fresh);
    record_scheduler_retention_run(
        &retention_run_id,
        &ctx.policy_id,
        &ctx.target_id_display,
        "preview",
        Some(&clean_preview),
        &at_iso,
    )?;

    let approved_ids: Option<HashSet<String>> = supplied_preview
        .and_then(|p| p.get("trash"))
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(value_str)
                .filter(|id| !id.is_empty())
                .collect()
        });

    let trash_records = fresh["trashRecords"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut trashed = Vec::new();
    for rec in &trash_records {
        let backup_id = rec
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if backup_id.is_empty() {
            continue;
        }
        if let Some(ref approved) = approved_ids {
            if !approved.contains(&backup_id) {
                continue;
            }
        }
        if execute_trash_move(&ctx.target_root, rec, &retention_run_id, writer, &at_iso)? {
            trashed.push(backup_id);
        }
    }

    record_scheduler_retention_run(
        &retention_run_id,
        &ctx.policy_id,
        &ctx.target_id_display,
        "trashed",
        Some(&clean_preview),
        &at_iso,
    )?;

    let finalized = finalize_retention(ctx, writer, now)?;

    record_scheduler_retention_run(
        &retention_run_id,
        &ctx.policy_id,
        &ctx.target_id_display,
        "completed",
        Some(&clean_preview),
        &at_iso,
    )?;

    Ok(apply_response_envelope(
        &retention_run_id,
        trashed,
        recovered_trash,
        &fresh,
        finalized,
    ))
}
