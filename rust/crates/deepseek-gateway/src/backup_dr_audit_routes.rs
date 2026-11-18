//! Resumable DR target audit routes:
//! - `POST /api/workspace/disaster-recovery/audit`
//! - `POST /api/workspace/disaster-recovery/audit/{audit_id}/resume`
//!
//! Writes strictly to `.backup-dr/evidence.sqlite3` under the
//! `backup_dr_ledger_store` native data domain (`DEEPSEEK_RUNTIME_MODE=python_disabled`
//! only; default mode refuses with `409 NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED`
//! without creating `.backup-dr/` or SQLite files).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::{dumps_compact, value_str};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::backup_catalog_routes::{open_readonly_sqlite, registered_target, utc_iso_from_unix};
use crate::backup_catalog_write_routes::verify_registered_target_ready_readonly;
use crate::backup_dr_slo_routes::now_unix_secs;
use crate::backup_dr_status_routes::table_columns;
use crate::route_support::{self, require_native_store, unhandled_error};

const MAX_AUDIT_BODY_BYTES: i64 = 16_000;
const BACKUP_DR_LEDGER_DOMAIN: &str = "backup_dr_ledger_store";
const BACKUP_DR_LEDGER_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED";
const TARGET_STORE_NOT_MIGRATED_CODE: &str = "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED";
const GENESIS_COMMIT_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/disaster-recovery/audit",
            any(dispatch_audit),
        )
        .route(
            "/api/workspace/disaster-recovery/audit/:audit_id/resume",
            any(dispatch_resume),
        )
}

async fn dispatch_audit(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_AUDIT_BODY_BYTES) {
        Ok(map) => map,
        Err(err) => return route_support::app_error_response(err),
    };
    let joined = tokio::task::spawn_blocking(move || run_audit_request(&payload)).await;
    match joined {
        Ok(Ok(val)) => Json(val).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

async fn dispatch_resume(method: Method, AxumPath(audit_id): AxumPath<String>) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let joined = tokio::task::spawn_blocking(move || resume_audit(&audit_id)).await;
    match joined {
        Ok(Ok(val)) => Json(val).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(unhandled_error()),
    }
}

fn truthy_str(value: Option<&Value>) -> String {
    match value {
        Some(val) if python_truthy(val) => value_str(val),
        _ => String::new(),
    }
}

fn non_empty_opt_str(value: Option<&Value>) -> Option<String> {
    let text = truthy_str(value);
    if text.is_empty() { None } else { Some(text) }
}

fn now_utc_iso() -> String {
    utc_iso_from_unix(now_unix_secs())
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn object_key(digest: &str) -> String {
    let prefix: String = digest.chars().take(2).collect();
    format!("objects/sha256/{prefix}/{digest}.age")
}

fn path_from_posix_key(root: &Path, key: &str) -> Option<PathBuf> {
    let clean = key.replace('\\', "/");
    if clean.starts_with('/') {
        return None;
    }
    let mut path = root.to_path_buf();
    for segment in clean.split('/') {
        if segment.is_empty() || segment == "." || segment == ".." {
            return None;
        }
        path.push(segment);
    }
    Some(path)
}

fn compute_hash_without_field(obj: &Map<String, Value>, omit_key: &str) -> String {
    let mut body = Map::new();
    for (key, value) in obj {
        if key != omit_key {
            body.insert(key.clone(), value.clone());
        }
    }
    sha256_hex(dumps_compact(&Value::Object(body)).as_bytes())
}

fn commit_marker_valid(commit: &Map<String, Value>) -> bool {
    let commit_hash = truthy_str(commit.get("commitHash"));
    if commit_hash.is_empty() {
        return false;
    }
    commit_hash == compute_hash_without_field(commit, "commitHash")
}

fn retirement_marker_valid(
    marker: &Map<String, Value>,
    receipt_bytes: &[u8],
    commit: &Map<String, Value>,
) -> bool {
    let Ok(receipt_val) = serde_json::from_slice::<Value>(receipt_bytes) else {
        return false;
    };
    let Some(receipt) = receipt_val.as_object() else {
        return false;
    };
    if marker.get("schemaVersion").and_then(Value::as_i64) != Some(1) {
        return false;
    }
    let marker_hash = truthy_str(marker.get("markerHash"));
    if marker_hash.is_empty() || marker_hash != compute_hash_without_field(marker, "markerHash") {
        return false;
    }
    if !commit_marker_valid(commit) {
        return false;
    }
    let receipt_digest = sha256_hex(receipt_bytes);
    let m_target = truthy_str(marker.get("targetId"));
    let r_target = non_empty_opt_str(receipt.get("targetId")).unwrap_or_else(|| m_target.clone());
    let m_policy = truthy_str(marker.get("policyId"));
    let r_policy = truthy_str(receipt.get("policyId"));
    let c_policy = truthy_str(commit.get("policyId"));
    let m_backup = truthy_str(marker.get("backupId"));
    let r_backup = truthy_str(receipt.get("backupId"));
    let c_backup = truthy_str(commit.get("backupId"));
    let m_rdig = truthy_str(marker.get("receiptDigest"));
    let c_rdig = truthy_str(commit.get("receiptDigest"));
    let m_chash = truthy_str(marker.get("commitHash"));
    let c_chash = truthy_str(commit.get("commitHash"));
    let m_osd = truthy_str(marker.get("objectSetDigest"));
    let expected_osd = non_empty_opt_str(receipt.get("objectSetDigest"))
        .or_else(|| non_empty_opt_str(commit.get("objectSetDigest")))
        .unwrap_or_default();

    m_target == r_target
        && m_policy == r_policy
        && r_policy == c_policy
        && m_backup == r_backup
        && r_backup == c_backup
        && m_rdig == receipt_digest
        && receipt_digest == c_rdig
        && m_chash == c_chash
        && m_osd == expected_osd
}

fn ensure_dr_ledger_schema(conn: &Connection) -> Result<(), AppError> {
    conn.busy_timeout(Duration::from_secs(30))
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         CREATE TABLE IF NOT EXISTS recovery_points (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            snapshot_kind TEXT NOT NULL DEFAULT 'full',
            parent_backup_id TEXT,
            chain_digest TEXT,
            chain_length INTEGER NOT NULL DEFAULT 1,
            ciphertext_bytes INTEGER NOT NULL DEFAULT 0,
            logical_bytes INTEGER NOT NULL DEFAULT 0,
            recoverable INTEGER NOT NULL DEFAULT 1,
            verified_at TEXT NOT NULL,
            storage_protocol TEXT,
            metadata_json TEXT,
            PRIMARY KEY (target_id, policy_id, backup_id)
         );
         CREATE TABLE IF NOT EXISTS logical_recovery_points (
            logical_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            object_set_digest TEXT,
            committed_at TEXT NOT NULL,
            snapshot_kind TEXT NOT NULL DEFAULT 'full',
            retained INTEGER NOT NULL DEFAULT 1,
            retired_at TEXT,
            metadata_json TEXT
         );
         CREATE TABLE IF NOT EXISTS recovery_point_copies (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            logical_id TEXT NOT NULL,
            object_set_digest TEXT,
            committed_at TEXT NOT NULL,
            recoverable INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'primary',
            mode TEXT NOT NULL DEFAULT 'required',
            state TEXT NOT NULL DEFAULT 'healthy',
            last_verified_at TEXT,
            last_scrub_at TEXT,
            last_drill_at TEXT,
            last_repair_at TEXT,
            last_failure TEXT,
            verified_at TEXT,
            metadata_json TEXT,
            PRIMARY KEY (target_id, policy_id, backup_id)
         );
         CREATE TABLE IF NOT EXISTS audit_jobs (
            audit_id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            cursor TEXT,
            target_generation INTEGER,
            previous_commit_hash TEXT,
            records_checked INTEGER NOT NULL DEFAULT 0,
            anomalies_json TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            details_json TEXT
         );
         CREATE TABLE IF NOT EXISTS audit_evidence (
            audit_id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            cursor TEXT,
            records_checked INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            details_json TEXT
         );",
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

fn open_dr_ledger_write() -> Result<Connection, AppError> {
    let dir = crate::data_routes::workspace_root().join(".backup-dr");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let db_path = dir.join("evidence.sqlite3");
    let conn = Connection::open(db_path).map_err(|_| unhandled_error())?;
    ensure_dr_ledger_schema(&conn)?;
    Ok(conn)
}

fn map_audit_job(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let anomalies_raw: Option<String> = row.get(7)?;
    let anomalies = anomalies_raw
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .filter(|v| v.is_array())
        .unwrap_or_else(|| json!([]));
    let details_raw: Option<String> = row.get(11)?;
    let details = details_raw
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| json!({}));
    Ok(json!({
        "auditId": row.get::<_, String>(0)?,
        "targetId": row.get::<_, String>(1)?,
        "phase": row.get::<_, String>(2)?,
        "cursor": row.get::<_, Option<String>>(3)?.filter(|s| !s.is_empty()),
        "targetGeneration": row.get::<_, Option<i64>>(4)?,
        "previousCommitHash": row.get::<_, Option<String>>(5)?.filter(|s| !s.is_empty()),
        "recordsChecked": row.get::<_, Option<i64>>(6)?.unwrap_or(0),
        "anomalies": anomalies,
        "startedAt": row.get::<_, String>(8)?,
        "updatedAt": row.get::<_, String>(9)?,
        "completedAt": row.get::<_, Option<String>>(10)?.filter(|s| !s.is_empty()),
        "details": details,
    }))
}

fn get_audit_job_readonly(audit_id: &str) -> Result<Option<Value>, AppError> {
    let db_path = crate::data_routes::workspace_root()
        .join(".backup-dr")
        .join("evidence.sqlite3");
    let Some(conn) = open_readonly_sqlite(&db_path)? else {
        return Ok(None);
    };
    if table_columns(&conn, "audit_jobs")?.is_empty() {
        return Ok(None);
    }
    conn.query_row(
        "SELECT audit_id, target_id, phase, cursor, target_generation, previous_commit_hash, \
         records_checked, anomalies_json, started_at, updated_at, completed_at, details_json \
         FROM audit_jobs WHERE audit_id = ?",
        [audit_id],
        map_audit_job,
    )
    .optional()
    .map_err(|_| unhandled_error())
}

fn get_open_audit_job(conn: &Connection, target_id: &str) -> Result<Option<Value>, AppError> {
    conn.query_row(
        "SELECT audit_id, target_id, phase, cursor, target_generation, previous_commit_hash, \
         records_checked, anomalies_json, started_at, updated_at, completed_at, details_json \
         FROM audit_jobs WHERE target_id = ? AND phase NOT IN ('completed', 'failed') \
         ORDER BY updated_at DESC LIMIT 1",
        [target_id],
        map_audit_job,
    )
    .optional()
    .map_err(|_| unhandled_error())
}

fn get_audit_job_conn(conn: &Connection, audit_id: &str) -> Result<Option<Value>, AppError> {
    conn.query_row(
        "SELECT audit_id, target_id, phase, cursor, target_generation, previous_commit_hash, \
         records_checked, anomalies_json, started_at, updated_at, completed_at, details_json \
         FROM audit_jobs WHERE audit_id = ?",
        [audit_id],
        map_audit_job,
    )
    .optional()
    .map_err(|_| unhandled_error())
}

struct AuditJobWrite<'a> {
    audit_id: &'a str,
    target_id: &'a str,
    phase: &'a str,
    cursor: Option<&'a str>,
    target_generation: Option<i64>,
    previous_commit_hash: Option<&'a str>,
    records_checked: i64,
    anomalies: &'a [String],
    started_at: &'a str,
    completed_at: Option<&'a str>,
    details: &'a Value,
}

fn upsert_audit_job(conn: &Connection, job: &AuditJobWrite<'_>) -> Result<(), AppError> {
    let now = now_utc_iso();
    let anomalies_json = serde_json::to_string(job.anomalies).map_err(|_| unhandled_error())?;
    let details_json = if job.details.is_null() {
        None
    } else {
        Some(serde_json::to_string(job.details).map_err(|_| unhandled_error())?)
    };
    conn.execute(
        "INSERT INTO audit_jobs (
            audit_id, target_id, phase, cursor, target_generation, previous_commit_hash,
            records_checked, anomalies_json, started_at, updated_at, completed_at, details_json
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
        ON CONFLICT(audit_id) DO UPDATE SET
            phase = excluded.phase,
            cursor = excluded.cursor,
            target_generation = excluded.target_generation,
            previous_commit_hash = excluded.previous_commit_hash,
            records_checked = excluded.records_checked,
            anomalies_json = excluded.anomalies_json,
            updated_at = excluded.updated_at,
            completed_at = excluded.completed_at,
            details_json = excluded.details_json",
        rusqlite::params![
            job.audit_id,
            job.target_id,
            job.phase,
            job.cursor.filter(|s| !s.is_empty()),
            job.target_generation,
            job.previous_commit_hash.filter(|s| !s.is_empty()),
            job.records_checked.max(0),
            anomalies_json,
            job.started_at,
            now,
            job.completed_at.filter(|s| !s.is_empty()),
            details_json,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

struct AuditEvidenceWrite<'a> {
    audit_id: &'a str,
    target_id: &'a str,
    started_at: &'a str,
    completed_at: Option<&'a str>,
    status: &'a str,
    cursor: Option<&'a str>,
    records_checked: i64,
    anomalies_count: usize,
    details: &'a Value,
}

fn record_audit_evidence(
    conn: &Connection,
    evidence: &AuditEvidenceWrite<'_>,
) -> Result<(), AppError> {
    let mut full = evidence.details.as_object().cloned().unwrap_or_default();
    if evidence.anomalies_count > 0 {
        full.insert(
            "anomaliesCount".to_string(),
            json!(evidence.anomalies_count as i64),
        );
    }
    let details_json = if full.is_empty() {
        None
    } else {
        Some(serde_json::to_string(&Value::Object(full)).map_err(|_| unhandled_error())?)
    };
    conn.execute(
        "INSERT INTO audit_evidence (
            audit_id, target_id, started_at, completed_at, status, cursor, records_checked, error, details_json
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, NULL, ?8)
        ON CONFLICT(audit_id) DO UPDATE SET
            target_id = excluded.target_id,
            started_at = excluded.started_at,
            completed_at = excluded.completed_at,
            status = excluded.status,
            cursor = excluded.cursor,
            records_checked = excluded.records_checked,
            error = excluded.error,
            details_json = excluded.details_json",
        rusqlite::params![
            evidence.audit_id,
            evidence.target_id,
            evidence.started_at,
            evidence.completed_at.filter(|s| !s.is_empty()),
            evidence.status,
            evidence.cursor.filter(|s| !s.is_empty()),
            evidence.records_checked.max(0),
            details_json,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

struct RecoveryPointWrite<'a> {
    target_id: &'a str,
    policy_id: &'a str,
    backup_id: &'a str,
    committed_at: &'a str,
    snapshot_kind: &'a str,
    parent_backup_id: Option<&'a str>,
    chain_digest: Option<&'a str>,
    chain_length: i64,
    ciphertext_bytes: i64,
    logical_bytes: i64,
    recoverable: bool,
    storage_protocol: Option<&'a str>,
    metadata: &'a Value,
}

fn record_recovery_point(conn: &Connection, rp: &RecoveryPointWrite<'_>) -> Result<(), AppError> {
    let verified_at = now_utc_iso();
    let meta_json = if rp.metadata.as_object().is_some_and(|m| !m.is_empty()) {
        Some(serde_json::to_string(rp.metadata).map_err(|_| unhandled_error())?)
    } else {
        None
    };
    conn.execute(
        "INSERT INTO recovery_points (
            target_id, policy_id, backup_id, committed_at, snapshot_kind,
            parent_backup_id, chain_digest, chain_length, ciphertext_bytes,
            logical_bytes, recoverable, verified_at, storage_protocol, metadata_json
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
        ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            committed_at = excluded.committed_at,
            snapshot_kind = excluded.snapshot_kind,
            parent_backup_id = excluded.parent_backup_id,
            chain_digest = excluded.chain_digest,
            chain_length = excluded.chain_length,
            ciphertext_bytes = excluded.ciphertext_bytes,
            logical_bytes = excluded.logical_bytes,
            recoverable = excluded.recoverable,
            verified_at = excluded.verified_at,
            storage_protocol = excluded.storage_protocol,
            metadata_json = excluded.metadata_json",
        rusqlite::params![
            rp.target_id,
            rp.policy_id,
            rp.backup_id,
            rp.committed_at,
            rp.snapshot_kind,
            rp.parent_backup_id.filter(|s| !s.is_empty()),
            rp.chain_digest.filter(|s| !s.is_empty()),
            rp.chain_length.max(1),
            rp.ciphertext_bytes.max(0),
            rp.logical_bytes.max(0),
            i64::from(rp.recoverable),
            verified_at,
            rp.storage_protocol.filter(|s| !s.is_empty()),
            meta_json,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

struct CopyWrite<'a> {
    target_id: &'a str,
    policy_id: &'a str,
    backup_id: &'a str,
    committed_at: &'a str,
    object_set_digest: Option<&'a str>,
    recoverable: bool,
    role: &'a str,
    state: &'a str,
    metadata: Option<&'a Value>,
}

fn record_logical_recovery_copy(conn: &Connection, copy: &CopyWrite<'_>) -> Result<(), AppError> {
    let digest = copy.object_set_digest.unwrap_or("");
    let digest_part: String = if digest.is_empty() {
        "na".to_string()
    } else {
        digest.chars().take(16).collect()
    };
    let logical_id = format!("lrp_{}_{}_{}", copy.policy_id, copy.backup_id, digest_part);
    let verified_at = now_utc_iso();
    let meta_json = copy
        .metadata
        .filter(|v| v.as_object().is_some_and(|m| !m.is_empty()))
        .map(serde_json::to_string)
        .transpose()
        .map_err(|_| unhandled_error())?;

    conn.execute(
        "INSERT INTO logical_recovery_points (
            logical_id, policy_id, backup_id, object_set_digest, committed_at, snapshot_kind, retained, metadata_json
        ) VALUES (?1, ?2, ?3, ?4, ?5, 'full', 1, ?6)
        ON CONFLICT(logical_id) DO UPDATE SET
            committed_at = CASE
                WHEN excluded.committed_at > logical_recovery_points.committed_at
                THEN excluded.committed_at ELSE logical_recovery_points.committed_at END,
            metadata_json = COALESCE(excluded.metadata_json, logical_recovery_points.metadata_json)",
        rusqlite::params![
            logical_id,
            copy.policy_id,
            copy.backup_id,
            if digest.is_empty() { None } else { Some(digest) },
            copy.committed_at,
            meta_json,
        ],
    )
    .map_err(|_| unhandled_error())?;

    conn.execute(
        "INSERT INTO recovery_point_copies (
            target_id, policy_id, backup_id, logical_id, object_set_digest, committed_at,
            recoverable, role, mode, state, verified_at, metadata_json
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'required', ?9, ?10, ?11)
        ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            logical_id = excluded.logical_id,
            object_set_digest = excluded.object_set_digest,
            committed_at = excluded.committed_at,
            recoverable = excluded.recoverable,
            role = excluded.role,
            mode = excluded.mode,
            state = excluded.state,
            verified_at = excluded.verified_at,
            metadata_json = excluded.metadata_json",
        rusqlite::params![
            copy.target_id,
            copy.policy_id,
            copy.backup_id,
            logical_id,
            if digest.is_empty() {
                None
            } else {
                Some(digest)
            },
            copy.committed_at,
            i64::from(copy.recoverable),
            copy.role,
            copy.state,
            verified_at,
            meta_json,
        ],
    )
    .map_err(|_| unhandled_error())?;
    Ok(())
}

fn payload_keys(receipt: &Map<String, Value>) -> Vec<String> {
    if truthy_str(receipt.get("storageProtocol")) == "object-set-v1" {
        let mut keys = HashSet::new();
        if let Some(items) = receipt.get("objects").and_then(Value::as_array) {
            for item in items {
                let digest = item
                    .as_object()
                    .map(|o| truthy_str(o.get("digest")))
                    .unwrap_or_default();
                if !digest.is_empty() {
                    keys.insert(object_key(&digest));
                }
            }
        }
        let mut sorted: Vec<String> = keys.into_iter().collect();
        sorted.sort();
        return sorted;
    }
    let digest = truthy_str(receipt.get("objectDigest"));
    if !digest.is_empty() {
        return vec![object_key(&digest)];
    }
    let filename = truthy_str(receipt.get("filename"));
    if filename.is_empty() {
        Vec::new()
    } else {
        vec![filename]
    }
}

struct PayloadCheckOutcome {
    anomalies: Vec<String>,
    governed_retirement: bool,
    retirement_marker: Value,
}

fn validate_payload_lifecycle(
    target_id: &str,
    root: &Path,
    receipt: &Map<String, Value>,
    receipt_bytes: Option<&[u8]>,
    commit: &Map<String, Value>,
) -> PayloadCheckOutcome {
    let backup_id = non_empty_opt_str(receipt.get("backupId"))
        .or_else(|| non_empty_opt_str(commit.get("backupId")))
        .unwrap_or_default();
    let policy_id = non_empty_opt_str(receipt.get("policyId"))
        .or_else(|| non_empty_opt_str(commit.get("policyId")))
        .unwrap_or_default();
    let marker_key = format!("retirements/{policy_id}/{backup_id}.json");
    let marker_val = path_from_posix_key(root, &marker_key)
        .filter(|p| p.is_file())
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object());

    let marker_present = marker_val.is_some();
    let mut governed_retirement = false;
    if let (Some(marker_v), Some(r_bytes)) = (&marker_val, receipt_bytes) {
        if let Some(marker_map) = marker_v.as_object() {
            governed_retirement = retirement_marker_valid(marker_map, r_bytes, commit)
                && truthy_str(marker_map.get("targetId")) == target_id;
        }
    }

    let mut anomalies = Vec::new();
    if marker_present && !governed_retirement {
        anomalies.push(format!("invalid-retirement-marker:{backup_id}"));
    }
    for key in payload_keys(receipt) {
        let present = path_from_posix_key(root, &key).is_some_and(|p| p.is_file());
        if !present && !governed_retirement {
            anomalies.push(format!("missing-payload:{backup_id}:{key}"));
        }
    }
    PayloadCheckOutcome {
        anomalies,
        governed_retirement,
        retirement_marker: marker_val.unwrap_or(Value::Null),
    }
}

fn validate_remote_commit_receipt_binding(
    target_id: &str,
    commit: &Map<String, Value>,
    receipt: Option<&Map<String, Value>>,
    raw_receipt_bytes: Option<&[u8]>,
) -> Vec<String> {
    let mut anomalies = Vec::new();
    if !commit_marker_valid(commit) {
        anomalies.push("invalid-commit-marker".to_string());
        return anomalies;
    }
    let backup_id = truthy_str(commit.get("backupId"));
    if backup_id.is_empty() {
        anomalies.push("missing-backup-id".to_string());
        return anomalies;
    }
    let Some(receipt_map) = receipt.filter(|r| !r.is_empty()) else {
        anomalies.push(format!("missing-receipt:{backup_id}"));
        return anomalies;
    };
    let receipt_digest = if let Some(raw) = raw_receipt_bytes {
        sha256_hex(raw)
    } else {
        let formatted = format!(
            "{}\n",
            serde_json::to_string_pretty(&Value::Object(receipt_map.clone())).unwrap_or_default()
        );
        sha256_hex(formatted.as_bytes())
    };
    let expected = truthy_str(commit.get("receiptDigest"));
    if !expected.is_empty() && receipt_digest != expected {
        anomalies.push(format!("receipt-digest-mismatch:{backup_id}"));
    }
    if truthy_str(receipt_map.get("backupId")) != backup_id {
        anomalies.push(format!("receipt-backup-id-mismatch:{backup_id}"));
    }
    let r_target = truthy_str(receipt_map.get("targetId"));
    if !r_target.is_empty() && r_target != target_id {
        anomalies.push(format!("receipt-target-mismatch:{backup_id}"));
    }
    let r_policy = truthy_str(receipt_map.get("policyId"));
    let c_policy = truthy_str(commit.get("policyId"));
    if !r_policy.is_empty() && !c_policy.is_empty() && r_policy != c_policy {
        anomalies.push(format!("receipt-policy-mismatch:{backup_id}"));
    }
    if truthy_str(receipt_map.get("storageProtocol")) == "object-set-v1" {
        let osd = non_empty_opt_str(receipt_map.get("objectSetDigest"))
            .or_else(|| non_empty_opt_str(commit.get("objectSetDigest")))
            .unwrap_or_default();
        if osd.is_empty() {
            anomalies.push(format!("missing-object-set-digest:{backup_id}"));
        }
        if let Some(objects) = receipt_map.get("objects") {
            let empty_or_invalid = objects.as_array().is_none_or(Vec::is_empty);
            if empty_or_invalid {
                anomalies.push(format!("invalid-object-set-inventory:{backup_id}"));
            }
        }
    }
    anomalies
}

fn build_staged_candidate(
    commit: Value,
    receipt: Value,
    bind_anomalies: Vec<String>,
    payload: PayloadCheckOutcome,
) -> Value {
    let commit_map = commit.as_object();
    let receipt_map = receipt.as_object();
    let backup_id = commit_map
        .map(|m| truthy_str(m.get("backupId")))
        .unwrap_or_default();
    let policy_id = receipt_map
        .and_then(|m| non_empty_opt_str(m.get("policyId")))
        .or_else(|| commit_map.and_then(|m| non_empty_opt_str(m.get("policyId"))))
        .unwrap_or_default();
    let committed_at = commit_map
        .and_then(|m| non_empty_opt_str(m.get("committedAt")))
        .or_else(|| receipt_map.and_then(|m| non_empty_opt_str(m.get("createdAt"))))
        .unwrap_or_else(now_utc_iso);
    let snapshot_kind = receipt_map
        .and_then(|m| non_empty_opt_str(m.get("snapshotKind")))
        .unwrap_or_else(|| "full".to_string());
    let parent_backup_id = receipt_map
        .and_then(|m| m.get("parentBackupId"))
        .cloned()
        .unwrap_or(Value::Null);
    let chain_length = receipt_map
        .and_then(|m| m.get("chainLength"))
        .filter(|v| python_truthy(v))
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let ciphertext_bytes = receipt_map
        .and_then(|m| m.get("size"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let logical_bytes = receipt_map
        .and_then(|m| m.get("logicalBytes"))
        .filter(|v| python_truthy(v))
        .or_else(|| {
            receipt_map
                .and_then(|m| m.get("size"))
                .filter(|v| python_truthy(v))
        })
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let storage_protocol = receipt_map
        .map(|m| truthy_str(m.get("storageProtocol")))
        .unwrap_or_default();
    let object_set_digest = receipt_map
        .and_then(|m| non_empty_opt_str(m.get("objectSetDigest")))
        .or_else(|| commit_map.and_then(|m| non_empty_opt_str(m.get("objectSetDigest"))));

    json!({
        "commit": commit,
        "receipt": receipt,
        "backupId": backup_id,
        "policyId": policy_id,
        "bindAnomalies": bind_anomalies,
        "payloadAnomalies": payload.anomalies,
        "governedRetirement": payload.governed_retirement,
        "retirementMarker": payload.retirement_marker,
        "targetGeneration": commit_map.and_then(|m| m.get("targetGeneration")).cloned().unwrap_or(Value::Null),
        "commitHash": commit_map.map(|m| truthy_str(m.get("commitHash"))).unwrap_or_default(),
        "previousCommitHash": commit_map.map(|m| truthy_str(m.get("previousCommitHash"))).unwrap_or_default(),
        "committedAt": committed_at,
        "snapshotKind": snapshot_kind,
        "parentBackupId": parent_backup_id,
        "chainLength": chain_length,
        "ciphertextBytes": ciphertext_bytes,
        "logicalBytes": logical_bytes,
        "storageProtocol": storage_protocol,
        "objectSetDigest": object_set_digest,
    })
}

fn scan_managed_local(
    root: &Path,
    anomalies: &mut Vec<String>,
    staged_candidates: &mut Vec<Value>,
) -> usize {
    let commits_dir = root.join("commits");
    let receipts_dir = root.join("receipts");
    if !commits_dir.is_dir() {
        return 0;
    }
    let mut commit_files = Vec::new();
    if let Ok(policy_dirs) = fs::read_dir(&commits_dir) {
        for dir_entry in policy_dirs.flatten() {
            let dir_path = dir_entry.path();
            if dir_path.is_dir() {
                if let Ok(files) = fs::read_dir(&dir_path) {
                    for file_entry in files.flatten() {
                        let file_path = file_entry.path();
                        if file_path.is_file()
                            && file_path.extension().and_then(|s| s.to_str()) == Some("json")
                        {
                            commit_files.push(file_path);
                        }
                    }
                }
            }
        }
    }
    commit_files.sort();
    let mut audited = 0usize;
    for c_path in commit_files {
        audited += 1;
        let parsed = fs::read_to_string(&c_path)
            .map_err(|e| e.to_string())
            .and_then(|s| serde_json::from_str::<Value>(&s).map_err(|e| e.to_string()));
        let commit_val = match parsed {
            Ok(v) => v,
            Err(err) => {
                let name = c_path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                anomalies.push(format!("malformed-commit-{name}: {err}"));
                continue;
            }
        };
        let Some(commit_map) = commit_val.as_object() else {
            anomalies.push("invalid-commit-marker".to_string());
            continue;
        };
        let backup_id = truthy_str(commit_map.get("backupId"));
        if backup_id.is_empty() {
            anomalies.push("invalid-commit-marker".to_string());
            continue;
        }
        let r_path = receipts_dir.join(format!("{backup_id}.json"));
        let raw_receipt = fs::read(&r_path).ok();
        let receipt_val = raw_receipt
            .as_deref()
            .and_then(|bytes| serde_json::from_slice::<Value>(bytes).ok())
            .unwrap_or_else(|| json!({}));

        let mut bind_anomalies = Vec::new();
        let receipt_obj_nonempty = receipt_val.as_object().is_some_and(|o| !o.is_empty());
        if raw_receipt.is_none() || !receipt_obj_nonempty {
            bind_anomalies.push(format!("missing-receipt:{backup_id}"));
        } else if let (Some(bytes), Some(r_map)) = (raw_receipt.as_deref(), receipt_val.as_object())
        {
            let exp_digest = truthy_str(commit_map.get("receiptDigest"));
            if !exp_digest.is_empty() && sha256_hex(bytes) != exp_digest {
                bind_anomalies.push(format!("receipt-digest-mismatch:{backup_id}"));
            }
            if truthy_str(r_map.get("backupId")) != backup_id {
                bind_anomalies.push(format!("receipt-backup-id-mismatch:{backup_id}"));
            }
        }
        let payload_outcome = if let Some(r_map) = receipt_val.as_object().filter(|o| !o.is_empty())
        {
            validate_payload_lifecycle(
                "managed-local",
                root,
                r_map,
                raw_receipt.as_deref(),
                commit_map,
            )
        } else {
            PayloadCheckOutcome {
                anomalies: Vec::new(),
                governed_retirement: false,
                retirement_marker: Value::Null,
            }
        };
        anomalies.extend(bind_anomalies.iter().cloned());
        anomalies.extend(payload_outcome.anomalies.iter().cloned());
        staged_candidates.push(build_staged_candidate(
            commit_val,
            receipt_val,
            bind_anomalies,
            payload_outcome,
        ));
    }
    audited
}

fn collect_relative_files(root: &Path, dir: &Path, prefix: &str, out: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut children: Vec<PathBuf> = entries.flatten().map(|e| e.path()).collect();
    children.sort();
    for path in children {
        if path.is_dir() {
            collect_relative_files(root, &path, prefix, out);
        } else if path.is_file() {
            if let Ok(rel) = path.strip_prefix(root) {
                let posix = rel.to_string_lossy().replace('\\', "/");
                if prefix.is_empty() || posix == prefix || posix.starts_with(&format!("{prefix}/"))
                {
                    out.push(posix);
                }
            }
        }
    }
}

fn list_filesystem_objects_page(
    root: &Path,
    prefix: &str,
    cursor: Option<&str>,
    limit: usize,
) -> (Vec<String>, Option<String>) {
    if !root.is_dir() {
        return (Vec::new(), None);
    }
    let prefix_norm = prefix.replace('\\', "/").trim_end_matches('/').to_string();
    let mut matches = Vec::new();
    collect_relative_files(root, root, &prefix_norm, &mut matches);
    matches.sort();

    let mut start = 0usize;
    if let Some(cur) = cursor.filter(|s| !s.is_empty()) {
        if let Some(pos) = matches.iter().position(|k| k == cur) {
            start = pos + 1;
        }
    }
    let end = (start + limit).min(matches.len());
    let page = matches[start..end].to_vec();
    let next_cursor = if page.len() == limit && end < matches.len() {
        page.last().cloned()
    } else {
        None
    };
    (page, next_cursor)
}

fn scan_filesystem_target_page(
    target_id: &str,
    root: &Path,
    cursor: Option<&str>,
    page_size: usize,
    anomalies: &mut Vec<String>,
    staged_candidates: &mut Vec<Value>,
) -> (usize, Option<String>) {
    let (keys, next_cursor) = list_filesystem_objects_page(root, "commits/", cursor, page_size);
    let mut audited = 0usize;
    for key in keys {
        audited += 1;
        if !key.ends_with(".json") {
            continue;
        }
        let commit_val = path_from_posix_key(root, &key)
            .and_then(|p| fs::read_to_string(p).ok())
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .filter(|v| v.is_object());
        let Some(commit_v) = commit_val else {
            anomalies.push(format!("invalid-commit-marker:{key}"));
            continue;
        };
        let commit_map = commit_v.as_object().expect("checked object");
        let backup_id = truthy_str(commit_map.get("backupId"));
        if backup_id.is_empty() {
            anomalies.push(format!("missing-backup-id:{key}"));
            continue;
        }
        let r_key = format!("receipts/{backup_id}.json");
        let raw_receipt = path_from_posix_key(root, &r_key)
            .filter(|p| p.is_file())
            .and_then(|p| fs::read(p).ok());
        let receipt_val = raw_receipt
            .as_deref()
            .and_then(|bytes| serde_json::from_slice::<Value>(bytes).ok())
            .filter(|v| v.is_object())
            .unwrap_or_else(|| json!({}));
        let bind_anomalies = validate_remote_commit_receipt_binding(
            target_id,
            commit_map,
            receipt_val.as_object(),
            raw_receipt.as_deref(),
        );
        let payload_outcome = if let Some(r_map) = receipt_val.as_object().filter(|o| !o.is_empty())
        {
            validate_payload_lifecycle(target_id, root, r_map, raw_receipt.as_deref(), commit_map)
        } else {
            PayloadCheckOutcome {
                anomalies: Vec::new(),
                governed_retirement: false,
                retirement_marker: Value::Null,
            }
        };
        anomalies.extend(bind_anomalies.iter().cloned());
        anomalies.extend(payload_outcome.anomalies.iter().cloned());
        staged_candidates.push(build_staged_candidate(
            commit_v,
            receipt_val,
            bind_anomalies,
            payload_outcome,
        ));
    }
    (audited, next_cursor)
}

pub(crate) fn resolve_target_root(target_id: &str) -> Result<PathBuf, AppError> {
    if target_id == "managed-local" {
        return Ok(crate::data_routes::workspace_root().join(".backups"));
    }
    let target = registered_target(target_id)?;
    let kind = target
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("filesystem");
    let path_str = target.get("path").and_then(Value::as_str).unwrap_or("");
    if kind != "filesystem" || path_str.is_empty() {
        return Err(AppError {
            message: "backup-catalog for a remote registered target is not served by the native runtime yet".to_string(),
            code: TARGET_STORE_NOT_MIGRATED_CODE,
            status: 503,
        });
    }
    let root = PathBuf::from(path_str);
    verify_registered_target_ready_readonly(target_id, &root, &target).map_err(|msg| AppError {
        message: msg,
        code: codes::INVALID_REQUEST,
        status: 409,
    })?;
    Ok(root)
}

fn finalize_complete_audit(
    conn: &Connection,
    target_id: &str,
    target_root: &Path,
    staged_candidates: &[Value],
    anomalies: &mut Vec<String>,
) -> Result<(usize, Option<i64>, Option<String>), AppError> {
    if staged_candidates.is_empty() {
        return Ok((0, None, None));
    }
    let mut chain_anomalies = Vec::new();
    let mut generation_commits: Vec<(i64, &Value)> = staged_candidates
        .iter()
        .filter_map(|c| {
            c.get("targetGeneration")
                .and_then(Value::as_i64)
                .map(|g| (g, c))
        })
        .collect();
    generation_commits.sort_by_key(|(g, _)| *g);

    let mut prev_hash: Option<String> = None;
    let mut prev_gen: Option<i64> = None;
    for (curr_gen, cand) in &generation_commits {
        let curr_hash = truthy_str(cand.get("commitHash"));
        let curr_prev = truthy_str(cand.get("previousCommitHash"));
        let b_id = truthy_str(cand.get("backupId"));
        if let Some(pg) = prev_gen {
            if *curr_gen != pg + 1 {
                chain_anomalies.push(format!("generation-gap:{pg}->{curr_gen}"));
            }
            if let Some(ph) = prev_hash.as_deref().filter(|s| !s.is_empty()) {
                if !curr_prev.is_empty() && curr_prev != ph {
                    chain_anomalies.push(format!("broken-commit-chain:{b_id}"));
                }
            }
        } else if *curr_gen == 1 && !curr_prev.is_empty() && curr_prev != GENESIS_COMMIT_HASH {
            chain_anomalies.push(format!("broken-genesis-commit-hash:{b_id}"));
        }
        prev_gen = Some(*curr_gen);
        prev_hash = Some(curr_hash);
    }

    if target_id != "managed-local" && !generation_commits.is_empty() {
        let head_path = target_root.join("control").join("head.json");
        if head_path.is_file() {
            if let Ok(head_text) = fs::read_to_string(&head_path) {
                if let Ok(head_val) = serde_json::from_str::<Value>(&head_text) {
                    if let Some(head_map) = head_val.as_object() {
                        let (_, latest_c) = generation_commits.last().expect("non-empty");
                        let latest_hash = truthy_str(latest_c.get("commitHash"));
                        let latest_gen = latest_c
                            .get("targetGeneration")
                            .and_then(Value::as_i64)
                            .unwrap_or(0);
                        let head_hash = truthy_str(head_map.get("latestCommitHash"));
                        let head_gen = head_map.get("targetGeneration").and_then(Value::as_i64);
                        if !head_hash.is_empty()
                            && !latest_hash.is_empty()
                            && head_hash != latest_hash
                        {
                            chain_anomalies.push(format!(
                                "head-commit-hash-mismatch:head={head_hash},audited={latest_hash}"
                            ));
                        }
                        if let Some(hg) = head_gen {
                            if hg != latest_gen {
                                chain_anomalies.push(format!(
                                    "head-generation-mismatch:head={hg},audited={latest_gen}"
                                ));
                            }
                        }
                    }
                }
            }
        }
    }

    anomalies.extend(chain_anomalies.iter().cloned());
    let chain_valid = chain_anomalies.is_empty();
    let mut recovery_points_found = 0usize;

    for cand in staged_candidates {
        let b_id = truthy_str(cand.get("backupId"));
        let p_id = truthy_str(cand.get("policyId"));
        let mut bind_a = Vec::new();
        if let Some(arr) = cand.get("bindAnomalies").and_then(Value::as_array) {
            for item in arr {
                bind_a.push(value_str(item));
            }
        }
        let mut payload_a = Vec::new();
        if let Some(arr) = cand.get("payloadAnomalies").and_then(Value::as_array) {
            for item in arr {
                payload_a.push(value_str(item));
            }
        }
        let governed_retirement = cand.get("governedRetirement").is_some_and(python_truthy);
        let recoverable =
            chain_valid && bind_a.is_empty() && payload_a.is_empty() && !governed_retirement;

        let mut all_cand_anomalies = bind_a.clone();
        all_cand_anomalies.extend(payload_a.clone());
        if !chain_valid {
            all_cand_anomalies.extend(chain_anomalies.iter().cloned());
        }
        let metadata = if recoverable {
            cand.get("receipt").cloned().unwrap_or_else(|| json!({}))
        } else if governed_retirement {
            json!({
                "governedRetirement": true,
                "retirementMarker": cand.get("retirementMarker").filter(|v| !v.is_null()).cloned().unwrap_or_else(|| json!({})),
            })
        } else {
            json!({
                "auditRejected": true,
                "anomalies": all_cand_anomalies,
            })
        };

        let committed_at = truthy_str(cand.get("committedAt"));
        let snapshot_kind =
            non_empty_opt_str(cand.get("snapshotKind")).unwrap_or_else(|| "full".to_string());
        let parent_backup_id = non_empty_opt_str(cand.get("parentBackupId"));
        let commit_hash = non_empty_opt_str(cand.get("commitHash"));
        let chain_length = cand.get("chainLength").and_then(Value::as_i64).unwrap_or(1);
        let ciphertext_bytes = cand
            .get("ciphertextBytes")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let logical_bytes = cand
            .get("logicalBytes")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let storage_protocol = non_empty_opt_str(cand.get("storageProtocol"));
        let object_set_digest = non_empty_opt_str(cand.get("objectSetDigest"));
        let role = if target_id == "managed-local" {
            "primary"
        } else {
            "replica"
        };

        record_recovery_point(
            conn,
            &RecoveryPointWrite {
                target_id,
                policy_id: &p_id,
                backup_id: &b_id,
                committed_at: &committed_at,
                snapshot_kind: &snapshot_kind,
                parent_backup_id: parent_backup_id.as_deref(),
                chain_digest: commit_hash.as_deref(),
                chain_length,
                ciphertext_bytes,
                logical_bytes,
                recoverable,
                storage_protocol: storage_protocol.as_deref(),
                metadata: &metadata,
            },
        )?;

        if recoverable {
            recovery_points_found += 1;
            record_logical_recovery_copy(
                conn,
                &CopyWrite {
                    target_id,
                    policy_id: &p_id,
                    backup_id: &b_id,
                    committed_at: &committed_at,
                    object_set_digest: object_set_digest.as_deref(),
                    recoverable: true,
                    role,
                    state: "healthy",
                    metadata: None,
                },
            )?;
        } else if governed_retirement && chain_valid && bind_a.is_empty() && payload_a.is_empty() {
            let ret_meta = json!({
                "retirementMarker": cand.get("retirementMarker").filter(|v| !v.is_null()).cloned().unwrap_or_else(|| json!({})),
            });
            record_logical_recovery_copy(
                conn,
                &CopyWrite {
                    target_id,
                    policy_id: &p_id,
                    backup_id: &b_id,
                    committed_at: &committed_at,
                    object_set_digest: object_set_digest.as_deref(),
                    recoverable: false,
                    role,
                    state: "retired",
                    metadata: Some(&ret_meta),
                },
            )?;
        }
    }

    Ok((recovery_points_found, prev_gen, prev_hash))
}

struct AuditOptions {
    target_id: String,
    page_size: usize,
    cursor: Option<String>,
    audit_id: Option<String>,
    resume: bool,
}

fn execute_audit(opts: &AuditOptions) -> Result<Value, AppError> {
    require_native_store(BACKUP_DR_LEDGER_DOMAIN, BACKUP_DR_LEDGER_WRITE_NOT_OWNED)?;
    let conn = open_dr_ledger_write()?;
    let mut started_at = now_utc_iso();
    let mut anomalies: Vec<String> = Vec::new();
    let mut audited_count: usize = 0;
    let mut recovery_points_found: usize = 0;
    let mut previous_commit_hash: Option<String> = None;
    let mut target_generation: Option<i64> = None;
    let mut staged_candidates: Vec<Value> = Vec::new();

    let existing = if let Some(aid) = opts.audit_id.as_deref() {
        get_audit_job_conn(&conn, aid)?
    } else if opts.resume {
        get_open_audit_job(&conn, &opts.target_id)?
    } else {
        None
    };

    let (a_id, start_cursor) = if let Some(job) = existing {
        let aid = truthy_str(job.get("auditId"));
        let cur = non_empty_opt_str(job.get("cursor"));
        audited_count = job
            .get("recordsChecked")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0) as usize;
        if let Some(arr) = job.get("anomalies").and_then(Value::as_array) {
            for item in arr {
                anomalies.push(value_str(item));
            }
        }
        previous_commit_hash = non_empty_opt_str(job.get("previousCommitHash"));
        target_generation = job.get("targetGeneration").and_then(Value::as_i64);
        if let Some(sa) = non_empty_opt_str(job.get("startedAt")) {
            started_at = sa;
        }
        if let Some(details) = job.get("details").and_then(Value::as_object) {
            recovery_points_found = details
                .get("recoveryPointsFound")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0) as usize;
            if let Some(cands) = details.get("candidates").and_then(Value::as_array) {
                staged_candidates = cands.clone();
            }
        }
        (aid, cur)
    } else {
        let aid = match opts.audit_id.clone() {
            Some(id) => id,
            None => {
                let raw = SystemEntropy.new_id()?;
                let short: String = raw.chars().take(16).collect();
                format!("audit_{short}")
            }
        };
        let empty_details = json!({"candidates": []});
        upsert_audit_job(
            &conn,
            &AuditJobWrite {
                audit_id: &aid,
                target_id: &opts.target_id,
                phase: "scanning",
                cursor: opts.cursor.as_deref(),
                target_generation: None,
                previous_commit_hash: None,
                records_checked: 0,
                anomalies: &[],
                started_at: &started_at,
                completed_at: None,
                details: &empty_details,
            },
        )?;
        (aid, opts.cursor.clone())
    };

    let target_root = match resolve_target_root(&opts.target_id) {
        Ok(root) => root,
        Err(err) => {
            let mut fail_anomalies = anomalies.clone();
            fail_anomalies.push(err.message.clone());
            let fail_details = json!({"error": err.message});
            let completed = now_utc_iso();
            let _ = upsert_audit_job(
                &conn,
                &AuditJobWrite {
                    audit_id: &a_id,
                    target_id: &opts.target_id,
                    phase: "failed",
                    cursor: start_cursor.as_deref(),
                    target_generation: None,
                    previous_commit_hash: None,
                    records_checked: audited_count as i64,
                    anomalies: &fail_anomalies,
                    started_at: &started_at,
                    completed_at: Some(&completed),
                    details: &fail_details,
                },
            );
            let _ = record_audit_evidence(
                &conn,
                &AuditEvidenceWrite {
                    audit_id: &a_id,
                    target_id: &opts.target_id,
                    started_at: &started_at,
                    completed_at: None,
                    status: "failed",
                    cursor: start_cursor.as_deref(),
                    records_checked: audited_count as i64,
                    anomalies_count: 1,
                    details: &fail_details,
                },
            );
            return Err(err);
        }
    };

    let next_cursor = if opts.target_id == "managed-local" {
        audited_count += scan_managed_local(&target_root, &mut anomalies, &mut staged_candidates);
        None
    } else {
        let (page_audited, nxt) = scan_filesystem_target_page(
            &opts.target_id,
            &target_root,
            start_cursor.as_deref(),
            opts.page_size,
            &mut anomalies,
            &mut staged_candidates,
        );
        audited_count += page_audited;
        nxt
    };

    if next_cursor.is_none() && !staged_candidates.is_empty() {
        let (found, tg, pch) = finalize_complete_audit(
            &conn,
            &opts.target_id,
            &target_root,
            &staged_candidates,
            &mut anomalies,
        )?;
        recovery_points_found = found;
        target_generation = tg;
        previous_commit_hash = pch;
    } else if next_cursor.is_some() {
        recovery_points_found = staged_candidates
            .iter()
            .filter(|c| {
                c.get("bindAnomalies")
                    .and_then(Value::as_array)
                    .is_none_or(Vec::is_empty)
            })
            .count();
    }

    let status = if next_cursor.is_none() {
        "completed"
    } else {
        "in-progress"
    };
    let phase = if status == "completed" {
        "completed"
    } else {
        "scanning"
    };
    let updated_at = now_utc_iso();
    let result = json!({
        "auditId": a_id,
        "targetId": opts.target_id,
        "status": status,
        "phase": phase,
        "cursor": next_cursor,
        "objectsAudited": audited_count,
        "recordsChecked": audited_count,
        "recoveryPointsFound": recovery_points_found,
        "anomalies": anomalies,
        "targetGeneration": target_generation,
        "previousCommitHash": previous_commit_hash,
        "auditedAt": started_at,
        "updatedAt": updated_at,
    });

    let details_with_cands = json!({
        "recoveryPointsFound": recovery_points_found,
        "objectsAudited": audited_count,
        "candidates": staged_candidates,
    });
    let completed_opt = (phase == "completed").then_some(updated_at.as_str());
    upsert_audit_job(
        &conn,
        &AuditJobWrite {
            audit_id: &a_id,
            target_id: &opts.target_id,
            phase,
            cursor: next_cursor.as_deref(),
            target_generation,
            previous_commit_hash: previous_commit_hash.as_deref(),
            records_checked: audited_count as i64,
            anomalies: &anomalies,
            started_at: &started_at,
            completed_at: completed_opt,
            details: &details_with_cands,
        },
    )?;

    let evidence_status = if status == "in-progress" {
        "in-progress"
    } else if anomalies.is_empty() {
        "success"
    } else {
        "warning"
    };
    record_audit_evidence(
        &conn,
        &AuditEvidenceWrite {
            audit_id: &a_id,
            target_id: &opts.target_id,
            started_at: &started_at,
            completed_at: completed_opt,
            status: evidence_status,
            cursor: next_cursor.as_deref(),
            records_checked: audited_count as i64,
            anomalies_count: anomalies.len(),
            details: &result,
        },
    )?;

    Ok(result)
}

fn run_audit_request(payload: &Map<String, Value>) -> Result<Value, AppError> {
    let target_id =
        non_empty_opt_str(payload.get("targetId")).unwrap_or_else(|| "managed-local".to_string());
    let page_size = match payload.get("pageSize") {
        None | Some(Value::Null) => 100usize,
        Some(Value::Bool(false)) => 100usize,
        Some(Value::Bool(true)) => 1usize,
        Some(Value::Number(n)) => {
            let v = n.as_i64().unwrap_or(0);
            if v == 0 { 100usize } else { v.max(1) as usize }
        }
        Some(Value::String(s)) if s.is_empty() => 100usize,
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map(|v| if v == 0 { 100 } else { v.max(1) as usize })
            .map_err(|_| AppError::invalid_payload("Invalid pageSize"))?,
        Some(_) => return Err(AppError::invalid_payload("Invalid pageSize")),
    };
    let cursor = non_empty_opt_str(payload.get("cursor"));
    let audit_id = non_empty_opt_str(payload.get("auditId"));
    execute_audit(&AuditOptions {
        target_id,
        page_size,
        cursor,
        audit_id,
        resume: true,
    })
}

fn resume_audit(audit_id: &str) -> Result<Value, AppError> {
    let Some(job) = get_audit_job_readonly(audit_id)? else {
        return Err(AppError::not_found(format!(
            "Audit job not found: {audit_id}"
        )));
    };
    let phase = truthy_str(job.get("phase"));
    if matches!(phase.as_str(), "completed" | "failed") {
        return Ok(json!({
            "auditId": audit_id,
            "targetId": job.get("targetId").cloned().unwrap_or(Value::Null),
            "status": if phase == "completed" { "completed" } else { "failed" },
            "phase": phase,
            "cursor": job.get("cursor").cloned().unwrap_or(Value::Null),
            "recordsChecked": job.get("recordsChecked").cloned().unwrap_or(json!(0)),
            "anomalies": job.get("anomalies").cloned().unwrap_or_else(|| json!([])),
            "resumed": false,
        }));
    }
    let target_id = truthy_str(job.get("targetId"));
    execute_audit(&AuditOptions {
        target_id,
        page_size: 100,
        cursor: None,
        audit_id: Some(audit_id.to_string()),
        resume: true,
    })
}
