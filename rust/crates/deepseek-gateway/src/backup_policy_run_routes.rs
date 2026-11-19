//! Native S4 manual policy backup execution route:
//! - `POST /api/workspace/backup-policies/{policy_id}/run`
//!
//! Write authority follows `DECLARED_NATIVE_DATA_DOMAINS`: mutating catalog,
//! run plan, spool, index, DR ledger, and retention stores is gated on
//! `DEEPSEEK_RUNTIME_MODE=python_disabled` (`409 NATIVE_*_WRITE_NOT_OWNED`
//! without touching disk in default mode). Under `python_disabled`, this
//! handler executes the full local Object-Set v1 snapshot, age-recipient
//! encryption, atomic publish (Receipt v4 + Commit v4 + catalog entry),
//! DR ledger recording, and retention cleanup.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use axum::Router;
use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;
use deepseek_core::version_info;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::{fsync_dir, sha256_file};
use deepseek_policy::backup_projection::{FileRecord, sha256_bytes, snapshot_root};
use deepseek_policy::backup_retention::chrono;
use deepseek_policy::backup_unlock::{
    BACKUP_SCHEMA, PACKAGE_PURPOSE, encrypt_tree_zip_native,
    encrypt_tree_zip_native_with_plain_metrics, scrub_plaintext_file,
};
use deepseek_policy::backup_writer_lease::TargetWriterLease;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::mutation_gate;
use deepseek_policy::python_json::{dumps_compact, value_str};
use deepseek_storage::object_set::{ObjectInventoryEntry, object_inventory_digest};
use rusqlite::{Connection, params};
use serde_json::{Map, Value, json};

use crate::backup_archive_routes::{
    build_revision, copy_staged_file, gate_to_app_error, hex_digest,
};
use crate::backup_capacity_routes::now_utc_iso;
use crate::backup_catalog_routes::{object_path, registered_target};
use crate::backup_catalog_write_routes::{
    append_catalog_entry, verify_registered_target_ready_readonly,
};
use crate::backup_commit::{previous_commit_hash, target_generation};
use crate::backup_dr_audit_routes::open_dr_ledger_write;
use crate::backup_restore_write_routes::{
    BackupContext, LOCAL_CONTRIBUTORS, collect_contributor_files, finish_json, parse_context,
};
use crate::backup_retention_routes::{
    apply_retention_for_policy, is_valid_safe_id, load_backup_policy,
};
use crate::route_support;

const BACKUP_CATALOG_DOMAIN: &str = "backup_catalog_store";
const BACKUP_RUN_PLAN_DOMAIN: &str = "backup_run_plan_store";
const BACKUP_SPOOL_DOMAIN: &str = "backup_spool_store";
const BACKUP_INDEX_DOMAIN: &str = "backup_index_store";
const BACKUP_DR_LEDGER_DOMAIN: &str = "backup_dr_ledger_store";
const BACKUP_RETENTION_DOMAIN: &str = "backup_retention_store";

const BACKUP_CATALOG_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED";
const BACKUP_RUN_PLAN_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_RUN_PLAN_WRITE_NOT_OWNED";
const BACKUP_SPOOL_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_SPOOL_WRITE_NOT_OWNED";
const BACKUP_INDEX_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_INDEX_WRITE_NOT_OWNED";
const BACKUP_DR_LEDGER_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED";
const BACKUP_RETENTION_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_RETENTION_WRITE_NOT_OWNED";
const TARGET_STORE_NOT_MIGRATED_CODE: &str = "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED";

const DEFAULT_LEASE_SECONDS: i64 = 300;
const OBJECT_SET_V1: &str = "object-set-v1";
const OBJECT_SET_COMPONENT_TARGET_BYTES: u64 = 16 * 1024 * 1024;
const SNAPSHOT_MAX_RETRIES: u32 = 3;

const SCHEDULER_SCHEMA_SQL: &str = "
CREATE TABLE IF NOT EXISTS backup_schedule_slots (
    policy_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    local_date_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, slot_key)
);
CREATE TABLE IF NOT EXISTS backup_runs (
    run_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    schedule_slot TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    owner_instance_id TEXT,
    fencing_token INTEGER,
    lease_until TEXT,
    reason TEXT,
    error TEXT,
    backup_id TEXT,
    filename TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backup_target_health (
    target_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS retention_runs (
    retention_run_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    preview TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);";

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/backup-policies/:policy_id/run",
        any(dispatch_run_policy),
    )
}

async fn dispatch_run_policy(
    method: Method,
    AxumPath(policy_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if let Err(err) = require_run_write_authority() {
        return route_support::app_error_response(err);
    }
    if !body.is_empty() {
        if let Err(err) = route_support::read_json_object(&headers, &body, 64_000) {
            return route_support::app_error_response(err);
        }
    }
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        execute_manual_policy_run(&root, &policy_id)
    })
    .await;
    finish_json(joined)
}

fn require_run_write_authority() -> Result<(), AppError> {
    route_support::require_native_store(BACKUP_CATALOG_DOMAIN, BACKUP_CATALOG_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_RUN_PLAN_DOMAIN, BACKUP_RUN_PLAN_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_SPOOL_DOMAIN, BACKUP_SPOOL_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_INDEX_DOMAIN, BACKUP_INDEX_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_DR_LEDGER_DOMAIN, BACKUP_DR_LEDGER_WRITE_NOT_OWNED)?;
    route_support::require_native_store(BACKUP_RETENTION_DOMAIN, BACKUP_RETENTION_WRITE_NOT_OWNED)
}

struct ClaimedManualRun {
    run_id: String,
    policy_id: String,
    schedule_slot: String,
    instance_id: String,
    fencing_token: i64,
    created_at: String,
}

struct RunPhaseUpdate<'a> {
    phase: &'a str,
    reason: Option<&'a str>,
    error: Option<&'a str>,
    lease_until: Option<&'a str>,
    backup_id: Option<&'a str>,
    filename: Option<&'a str>,
}

struct DrRecoveryPointRecord<'a> {
    target_id: &'a str,
    policy_id: &'a str,
    backup_id: &'a str,
    committed_at: &'a str,
    object_set_digest: &'a str,
    cipher_bytes: u64,
    logical_bytes: u64,
    manifest: &'a Value,
}

struct EncryptedBlob {
    path: PathBuf,
    digest: String,
    size: u64,
}

struct PayloadComponentMeta {
    component_id: String,
    rel_paths: Vec<String>,
    ciphertext_digest: String,
    ciphertext_size: u64,
    plaintext_size: u64,
    plaintext_sha256: String,
}

struct BuiltObjectSet {
    control: EncryptedBlob,
    payloads: Vec<EncryptedBlob>,
    logical_bytes: u64,
    manifest: Value,
    root_digest: String,
}

struct StagingCleanup {
    dir: PathBuf,
}

impl Drop for StagingCleanup {
    fn drop(&mut self) {
        scrub_staging_tree(&self.dir);
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn scrub_staging_tree(dir: &Path) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if p.is_dir() {
            scrub_staging_tree(&p);
        } else if p.is_file() {
            scrub_plaintext_file(&p);
        }
    }
}

fn instance_id_from_environment() -> String {
    for name in ["DEEPSEEK_INSTANCE_ID", "HOSTNAME", "COMPUTERNAME"] {
        if let Ok(value) = std::env::var(name) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
    }
    format!("local-{}", std::process::id())
}

fn unix_secs_plus(offset_secs: i64) -> i64 {
    let base = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    base.saturating_add(offset_secs)
}

fn utc_iso_from_unix_secs(secs: i64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp(secs, 0)
        .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_else(now_utc_iso)
}

fn open_scheduler_db(root: &Path) -> Result<Connection, AppError> {
    let sched_dir = root.join(".backup-scheduler");
    fs::create_dir_all(&sched_dir).map_err(|_| route_support::unhandled_error())?;
    let db_path = sched_dir.join("scheduler.db");
    let conn = Connection::open(&db_path).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute_batch(SCHEDULER_SCHEMA_SQL)
        .map_err(|_| route_support::unhandled_error())?;
    Ok(conn)
}

fn claim_manual_run(
    root: &Path,
    policy: &Value,
    policy_id: &str,
) -> Result<ClaimedManualRun, AppError> {
    let conn = open_scheduler_db(root)?;
    let instance_id = instance_id_from_environment();
    let now_secs = unix_secs_plus(0);
    let created_at = utc_iso_from_unix_secs(now_secs);
    let lease_until = utc_iso_from_unix_secs(now_secs + DEFAULT_LEASE_SECONDS);
    let timezone = policy
        .get("schedule")
        .and_then(|s| s.get("timezone"))
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "UTC".to_string());

    conn.execute(
        "INSERT INTO backup_counters(name, value) VALUES ('fencing', 0) ON CONFLICT(name) DO NOTHING",
        [],
    )
    .map_err(|_| route_support::unhandled_error())?;
    conn.execute(
        "UPDATE backup_counters SET value = value + 1 WHERE name = 'fencing'",
        [],
    )
    .map_err(|_| route_support::unhandled_error())?;
    let fencing_token: i64 = conn
        .query_row(
            "SELECT value FROM backup_counters WHERE name = 'fencing'",
            [],
            |row| row.get(0),
        )
        .map_err(|_| route_support::unhandled_error())?;

    let slot_hex = SystemEntropy.new_id()?;
    let mut combined = slot_hex;
    while combined.len() < 32 {
        combined.push_str(&SystemEntropy.new_id()?);
    }
    let hex32: String = combined.chars().take(32).collect();
    let run_id = format!("run_{}", hex32.chars().take(16).collect::<String>());
    let schedule_slot = format!("manual/{hex32}");

    conn.execute(
        "INSERT OR IGNORE INTO backup_schedule_slots
         (policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, 'claimed', ?6, ?7)",
        params![
            policy_id,
            schedule_slot,
            created_at,
            created_at,
            timezone,
            run_id,
            created_at,
        ],
    )
    .map_err(|_| route_support::unhandled_error())?;

    conn.execute(
        "INSERT INTO backup_runs
         (run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id, fencing_token, lease_until, reason, error, backup_id, filename, created_at, updated_at)
         VALUES (?1, ?2, ?3, 'leased', 1, ?4, ?5, ?6, 'manual', NULL, NULL, NULL, ?7, ?7)",
        params![
            run_id,
            policy_id,
            schedule_slot,
            instance_id,
            fencing_token,
            lease_until,
            created_at,
        ],
    )
    .map_err(|_| route_support::unhandled_error())?;

    Ok(ClaimedManualRun {
        run_id,
        policy_id: policy_id.to_string(),
        schedule_slot,
        instance_id,
        fencing_token,
        created_at,
    })
}

fn update_run_phase(root: &Path, run_id: &str, update: RunPhaseUpdate<'_>) {
    let Ok(conn) = open_scheduler_db(root) else {
        return;
    };
    let updated_at = now_utc_iso();
    let _ = conn.execute(
        "UPDATE backup_runs
         SET phase = ?1,
             reason = COALESCE(?2, reason),
             error = ?3,
             lease_until = COALESCE(?4, lease_until),
             backup_id = COALESCE(?5, backup_id),
             filename = COALESCE(?6, filename),
             updated_at = ?7
         WHERE run_id = ?8",
        params![
            update.phase,
            update.reason,
            update.error,
            update.lease_until,
            update.backup_id,
            update.filename,
            updated_at,
            run_id,
        ],
    );
}

fn extract_policy_recipients(policy: &Value) -> Result<Vec<String>, AppError> {
    let raw = policy
        .get("protection")
        .or_else(|| policy.get("encryption"))
        .and_then(|p| p.get("recipients"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut out = Vec::new();
    for item in raw {
        if let Some(s) = item.as_str() {
            let trimmed = s.trim();
            if !trimmed.is_empty() && !out.iter().any(|existing| existing == trimmed) {
                out.push(trimmed.to_string());
            }
        }
    }
    if out.is_empty()
        || out.len() > 16
        || out
            .iter()
            .any(|item| !item.starts_with("age1") || item.len() > 200)
    {
        return Err(AppError::invalid_payload(
            "Backup policy requires between 1 and 16 age recipients",
        ));
    }
    Ok(out)
}

fn extract_retry_budget(policy: &Value) -> (i64, i64) {
    let retry = policy.get("retry").and_then(Value::as_object);
    let max_attempts = retry
        .and_then(|r| r.get("maxAttempts"))
        .and_then(Value::as_i64)
        .filter(|v| *v >= 1)
        .unwrap_or(3);
    let initial_backoff = retry
        .and_then(|r| r.get("initialBackoffSeconds"))
        .and_then(Value::as_i64)
        .filter(|v| *v >= 1)
        .unwrap_or(60);
    (max_attempts, initial_backoff)
}

fn mark_blocked_outcome(
    root: &Path,
    claim: &ClaimedManualRun,
    policy: &Value,
    target_id: &str,
    reason: &str,
    error_message: &str,
) -> Value {
    let (max_attempts, backoff_secs) = extract_retry_budget(policy);
    let terminal = max_attempts <= 1;
    let phase = if terminal {
        "blocked-terminal"
    } else {
        "blocked-retryable"
    };
    let retry_until = if terminal {
        None
    } else {
        Some(utc_iso_from_unix_secs(unix_secs_plus(backoff_secs)))
    };
    update_run_phase(
        root,
        &claim.run_id,
        RunPhaseUpdate {
            phase,
            reason: Some(reason),
            error: Some(error_message),
            lease_until: retry_until.as_deref(),
            backup_id: None,
            filename: None,
        },
    );
    let mut body = json!({
        "runId": claim.run_id,
        "policyId": claim.policy_id,
        "scheduleSlot": claim.schedule_slot,
        "phase": phase,
        "attempt": 1,
        "fencingToken": claim.fencing_token,
        "targetId": target_id,
        "reason": reason,
        "blockedReason": reason,
        "error": error_message,
        "backupId": Value::Null,
        "filename": Value::Null,
    });
    if !terminal {
        if let Some(obj) = body.as_object_mut() {
            obj.insert("retryInSeconds".into(), json!(backoff_secs));
            obj.insert("nextRetryAt".into(), json!(retry_until));
        }
    }
    body
}

enum ResolvedPolicyTarget {
    Ready {
        target_id: String,
        target_root: PathBuf,
    },
    Blocked {
        target_id: String,
        reason: String,
        message: String,
    },
}

fn resolve_policy_target(root: &Path, policy: &Value) -> Result<ResolvedPolicyTarget, AppError> {
    let raw_target = policy
        .get("primaryTargetId")
        .filter(|v| python_truthy(v))
        .or_else(|| policy.get("targetId").filter(|v| python_truthy(v)))
        .map(value_str)
        .unwrap_or_else(|| "managed-local".to_string());
    if raw_target == "unbound" {
        return Ok(ResolvedPolicyTarget::Blocked {
            target_id: raw_target,
            reason: "blocked-target-unbound".to_string(),
            message: "Backup policy is not bound to a target".to_string(),
        });
    }
    if raw_target == "managed-local" {
        return Ok(ResolvedPolicyTarget::Ready {
            target_id: raw_target,
            target_root: root.join(".backups"),
        });
    }

    let meta = match registered_target(&raw_target) {
        Ok(m) => m,
        Err(err) if err.status == 404 => {
            return Ok(ResolvedPolicyTarget::Blocked {
                target_id: raw_target,
                reason: "blocked-target-unavailable".to_string(),
                message: err.message,
            });
        }
        Err(err) => return Err(err),
    };
    let kind = meta.get("kind").and_then(Value::as_str).unwrap_or("");
    let path_str = meta.get("path").and_then(Value::as_str).unwrap_or("");
    if kind != "filesystem" || path_str.is_empty() {
        return Err(AppError {
            message: "backup policy execution for a remote registered target is not served by the native runtime yet"
                .to_string(),
            code: TARGET_STORE_NOT_MIGRATED_CODE,
            status: 503,
        });
    }
    let target_root = PathBuf::from(path_str);
    match verify_registered_target_ready_readonly(&raw_target, &target_root, &meta) {
        Ok(()) => Ok(ResolvedPolicyTarget::Ready {
            target_id: raw_target,
            target_root,
        }),
        Err(msg) => Ok(ResolvedPolicyTarget::Blocked {
            target_id: raw_target,
            reason: "blocked-target-unavailable".to_string(),
            message: msg,
        }),
    }
}

fn write_frozen_run_plan(
    root: &Path,
    policy: &Value,
    claim: &ClaimedManualRun,
    target_id: &str,
    target_head_hash: &str,
    backup_id: &str,
    context: &BackupContext,
) -> Result<(), AppError> {
    // `backup_target_store.commit_slot_digest` is
    // `sha256(schedule_slot.encode()).hexdigest()` over the slot alone.
    let slot_digest = sha256_bytes(claim.schedule_slot.as_bytes());
    let plan_dir = root.join(".backup-run-plans").join(&claim.policy_id);
    fs::create_dir_all(&plan_dir).map_err(|_| route_support::unhandled_error())?;

    let contributor_ids: Vec<Value> = LOCAL_CONTRIBUTORS
        .iter()
        .map(|spec| json!({"id": spec.id, "schemaVersion": spec.schema_version}))
        .collect();
    let mut plan = json!({
        "schemaVersion": 3,
        "policyId": claim.policy_id,
        "scheduleSlot": claim.schedule_slot,
        "slotDigest": slot_digest,
        "backupId": backup_id,
        "targetId": target_id,
        "targetHeadHash": target_head_hash,
        "snapshotKind": "full",
        "storageProtocol": OBJECT_SET_V1,
        "scope": context.to_json(),
        "protection": policy.get("protection").cloned().unwrap_or_else(|| json!({})),
        "contributors": contributor_ids,
        "createdAt": claim.created_at,
    });
    let mut hash_material = plan.as_object().cloned().unwrap_or_default();
    hash_material.remove("createdAt");
    hash_material.remove("backupId");
    let run_plan_digest = sha256_bytes(dumps_compact(&Value::Object(hash_material)).as_bytes());
    if let Some(obj) = plan.as_object_mut() {
        obj.insert("runPlanDigest".into(), Value::String(run_plan_digest));
    }

    let plan_path = plan_dir.join(format!("{slot_digest}.json"));
    route_support::write_json_atomic(&plan_path, &plan)
}

fn check_frontend_mirror_head(root: &Path, policy: &Value) -> Result<Option<Value>, String> {
    let mode = policy
        .get("frontendMirror")
        .and_then(|m| m.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("best-effort");
    if mode == "excluded" {
        return Ok(None);
    }
    let head_path = root.join(".backup-mirror").join("HEAD.json");
    if head_path.is_file() {
        if let Ok(raw) = fs::read_to_string(&head_path) {
            if let Ok(parsed) = serde_json::from_str::<Value>(&raw) {
                return Ok(Some(parsed));
            }
        }
    }
    if mode == "required" {
        return Err(
            "frontend-mirror-missing: sealed frontend replica required by policy is missing"
                .to_string(),
        );
    }
    Ok(None)
}

fn component_bucket(rel: &str) -> String {
    let parts: Vec<&str> = rel.split('/').collect();
    if parts.len() >= 3 && parts[0] == "payload" && parts[1] == "projects" {
        format!("projects-{}", parts[2])
    } else if parts.len() >= 2 {
        format!("contrib-{}", parts[1])
    } else {
        "misc".to_string()
    }
}

fn sanitize_component_slug(raw: &str) -> String {
    let slug: String = raw
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    if slug.is_empty() {
        "part".to_string()
    } else {
        slug
    }
}

fn partition_payload_components(
    stage_root: &Path,
    rel_paths: &[String],
) -> Vec<(String, Vec<String>)> {
    let mut buckets: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for rel in rel_paths {
        buckets
            .entry(component_bucket(rel))
            .or_default()
            .push(rel.clone());
    }
    let mut out = Vec::new();
    let mut ordinal = 0usize;
    for (bucket, mut paths) in buckets {
        paths.sort();
        let slug = sanitize_component_slug(&bucket);
        let mut current = Vec::new();
        let mut current_bytes: u64 = 0;
        for rel in paths {
            let size = fs::metadata(stage_root.join(&rel))
                .map(|m| m.len())
                .unwrap_or(0);
            if !current.is_empty() && current_bytes + size > OBJECT_SET_COMPONENT_TARGET_BYTES {
                ordinal += 1;
                out.push((format!("{slug}-{ordinal:04}"), std::mem::take(&mut current)));
                current_bytes = 0;
            }
            current.push(rel);
            current_bytes = current_bytes.saturating_add(size);
            if current_bytes >= OBJECT_SET_COMPONENT_TARGET_BYTES {
                ordinal += 1;
                out.push((format!("{slug}-{ordinal:04}"), std::mem::take(&mut current)));
                current_bytes = 0;
            }
        }
        if !current.is_empty() {
            ordinal += 1;
            out.push((format!("{slug}-{ordinal:04}"), current));
        }
    }
    out
}

fn build_object_set_once(
    root: &Path,
    stage_dir: &Path,
    backup_id: &str,
    context: &BackupContext,
    recipients: &[String],
    sealed_frontend: Option<&Value>,
) -> Result<BuiltObjectSet, AppError> {
    let payload_stage = stage_dir.join("payload_work");
    let components_work = stage_dir.join("components_work");
    let control_stage = stage_dir.join("control_stage");
    fs::create_dir_all(&payload_stage).map_err(|_| route_support::unhandled_error())?;
    fs::create_dir_all(&components_work).map_err(|_| route_support::unhandled_error())?;
    fs::create_dir_all(&control_stage).map_err(|_| route_support::unhandled_error())?;

    let mut contributors_meta = Vec::new();
    let mut records = Vec::new();
    let mut file_entries = Vec::new();
    let mut logical_bytes: u64 = 0;
    let mut seen_rel = BTreeSet::new();

    for spec in LOCAL_CONTRIBUTORS {
        let collected = collect_contributor_files(root, &spec, context);
        let mut c_files: u64 = 0;
        let mut c_bytes: u64 = 0;
        for (rel_path, src_path) in collected {
            let archive_rel = format!("payload/{}/{rel_path}", spec.id);
            if !seen_rel.insert(archive_rel.clone()) {
                continue;
            }
            let dst_path = payload_stage.join(&archive_rel);
            copy_staged_file(&src_path, &dst_path)?;
            let size = fs::metadata(&dst_path)
                .map(|m| m.len())
                .map_err(|_| route_support::unhandled_error())?;
            let sha = sha256_file(&dst_path).map_err(|_| route_support::unhandled_error())?;
            logical_bytes = logical_bytes.saturating_add(size);
            c_files += 1;
            c_bytes = c_bytes.saturating_add(size);
            records.push(FileRecord {
                contributor_id: spec.id.to_string(),
                logical_path: archive_rel.clone(),
                size,
                sha256: sha.clone(),
            });
            file_entries.push(json!({
                "contributorId": spec.id,
                "path": archive_rel,
                "size": size,
                "sha256": sha,
            }));
        }
        contributors_meta.push(json!({
            "id": spec.id,
            "schemaVersion": spec.schema_version,
            "dataClass": spec.data_class,
            "files": c_files,
            "bytes": c_bytes,
            "restorePolicy": "replace",
        }));
    }

    records.sort_by(|a, b| a.logical_path.cmp(&b.logical_path));
    let root_digest = snapshot_root(&records);
    let all_rel_paths: Vec<String> = records.iter().map(|r| r.logical_path.clone()).collect();
    let groups = partition_payload_components(&payload_stage, &all_rel_paths);

    let mut path_to_comp = Map::new();
    let mut comp_to_paths = Map::new();
    let mut payload_components = Map::new();
    let mut payload_blobs = Vec::new();
    let mut built_metas = Vec::new();

    for (comp_id, rels) in groups {
        let comp_stage = components_work.join(&comp_id);
        fs::create_dir_all(&comp_stage).map_err(|_| route_support::unhandled_error())?;
        for rel in &rels {
            let src = payload_stage.join(rel);
            let dst = comp_stage.join(rel);
            if let Some(parent) = dst.parent() {
                fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
            }
            fs::copy(&src, &dst).map_err(|_| route_support::unhandled_error())?;
            path_to_comp.insert(rel.clone(), Value::String(comp_id.clone()));
        }
        let age_path = components_work.join(format!("{comp_id}.age"));
        let (plain_size, plain_sha) = encrypt_tree_zip_native_with_plain_metrics(
            &comp_stage,
            &age_path,
            "age-recipient",
            None,
            recipients,
        )
        .map_err(|e| AppError {
            message: format!("Failed to encrypt backup component: {e}"),
            code: codes::INTERNAL,
            status: 500,
        })?;
        let cipher_size = fs::metadata(&age_path)
            .map(|m| m.len())
            .map_err(|_| route_support::unhandled_error())?;
        let cipher_sha = sha256_file(&age_path).map_err(|_| route_support::unhandled_error())?;

        built_metas.push(PayloadComponentMeta {
            component_id: comp_id,
            rel_paths: rels,
            ciphertext_digest: cipher_sha.clone(),
            ciphertext_size: cipher_size,
            plaintext_size: plain_size,
            plaintext_sha256: plain_sha,
        });
        payload_blobs.push(EncryptedBlob {
            path: age_path,
            digest: cipher_sha,
            size: cipher_size,
        });
    }

    for meta in &built_metas {
        comp_to_paths.insert(meta.component_id.clone(), json!(meta.rel_paths));
        payload_components.insert(
            meta.component_id.clone(),
            json!({
                "ciphertextDigest": meta.ciphertext_digest,
                "ciphertextSize": meta.ciphertext_size,
                "plaintextSize": meta.plaintext_size,
                "plaintextSha256": meta.plaintext_sha256,
            }),
        );
    }

    let mut manifest = json!({
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "backupId": backup_id,
        "snapshotKind": "full",
        "storageProtocol": OBJECT_SET_V1,
        "source": {
            "version": version_info().version,
            "revision": build_revision(),
        },
        "snapshot": {
            "kind": "full",
            "rootDigest": root_digest,
        },
        "contributors": contributors_meta,
        "files": file_entries,
    });
    if let Some(mirror) = sealed_frontend {
        if let Some(obj) = manifest.as_object_mut() {
            obj.insert("sealedFrontend".into(), mirror.clone());
        }
    }

    let component_map = json!({
        "schemaVersion": 1,
        "paths": path_to_comp,
        "components": comp_to_paths,
    });
    let payload_index = json!({
        "schemaVersion": 1,
        "storageProtocol": OBJECT_SET_V1,
        "snapshotFormat": "full",
        "payloadComponents": payload_components,
    });

    let cmap_bytes =
        serde_json::to_vec_pretty(&component_map).map_err(|_| route_support::unhandled_error())?;
    let manifest_bytes =
        serde_json::to_vec_pretty(&manifest).map_err(|_| route_support::unhandled_error())?;
    let pindex_bytes =
        serde_json::to_vec_pretty(&payload_index).map_err(|_| route_support::unhandled_error())?;

    let mut sums = String::new();
    for (name, bytes) in [
        ("component-map.json", cmap_bytes.as_slice()),
        ("manifest.json", manifest_bytes.as_slice()),
        ("payload-index.json", pindex_bytes.as_slice()),
    ] {
        let _ = writeln!(sums, "{}  {name}", hex_digest(bytes));
    }

    fs::write(control_stage.join("component-map.json"), &cmap_bytes)
        .map_err(|_| route_support::unhandled_error())?;
    fs::write(control_stage.join("manifest.json"), &manifest_bytes)
        .map_err(|_| route_support::unhandled_error())?;
    fs::write(control_stage.join("payload-index.json"), &pindex_bytes)
        .map_err(|_| route_support::unhandled_error())?;
    fs::write(control_stage.join("checksums.sha256"), sums.as_bytes())
        .map_err(|_| route_support::unhandled_error())?;

    let ctrl_age = stage_dir.join("control.age");
    encrypt_tree_zip_native(&control_stage, &ctrl_age, "age-recipient", None, recipients).map_err(
        |e| AppError {
            message: format!("Failed to encrypt backup control object: {e}"),
            code: codes::INTERNAL,
            status: 500,
        },
    )?;
    let ctrl_size = fs::metadata(&ctrl_age)
        .map(|m| m.len())
        .map_err(|_| route_support::unhandled_error())?;
    let ctrl_digest = sha256_file(&ctrl_age).map_err(|_| route_support::unhandled_error())?;

    Ok(BuiltObjectSet {
        control: EncryptedBlob {
            path: ctrl_age,
            digest: ctrl_digest,
            size: ctrl_size,
        },
        payloads: payload_blobs,
        logical_bytes,
        manifest,
        root_digest,
    })
}

fn write_atomic_bytes(path: &Path, bytes: &[u8]) -> Result<(), AppError> {
    let parent = path.parent().ok_or_else(route_support::unhandled_error)?;
    fs::create_dir_all(parent).map_err(|_| route_support::unhandled_error())?;
    let tmp = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("blob"),
        std::process::id()
    ));
    let mut handle = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&tmp)
        .map_err(|_| route_support::unhandled_error())?;
    handle
        .write_all(bytes)
        .map_err(|_| route_support::unhandled_error())?;
    handle
        .sync_all()
        .map_err(|_| route_support::unhandled_error())?;
    drop(handle);
    fs::rename(&tmp, path).map_err(|_| {
        let _ = fs::remove_file(&tmp);
        route_support::unhandled_error()
    })?;
    fsync_dir(parent);
    Ok(())
}

fn install_object_blob(target_root: &Path, blob: &EncryptedBlob) -> Result<(), AppError> {
    let dst = object_path(target_root, &blob.digest);
    if dst.is_file() {
        return Ok(());
    }
    let bytes = fs::read(&blob.path).map_err(|_| route_support::unhandled_error())?;
    write_atomic_bytes(&dst, &bytes)
}

fn record_dr_ledger_recovery_point(entry: &DrRecoveryPointRecord<'_>) -> Result<(), AppError> {
    let conn = open_dr_ledger_write()?;
    let metadata_json = serde_json::to_string(entry.manifest).unwrap_or_else(|_| "{}".to_string());
    conn.execute(
        "INSERT INTO recovery_points (
            target_id, policy_id, backup_id, committed_at, snapshot_kind,
            parent_backup_id, chain_digest, chain_length, ciphertext_bytes,
            logical_bytes, recoverable, verified_at, storage_protocol, metadata_json
        ) VALUES (?1, ?2, ?3, ?4, 'full', NULL, ?5, 1, ?6, ?7, 1, ?8, ?9, ?10)
        ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            committed_at = excluded.committed_at,
            chain_digest = excluded.chain_digest,
            ciphertext_bytes = excluded.ciphertext_bytes,
            logical_bytes = excluded.logical_bytes,
            recoverable = excluded.recoverable,
            verified_at = excluded.verified_at,
            storage_protocol = excluded.storage_protocol,
            metadata_json = excluded.metadata_json",
        params![
            entry.target_id,
            entry.policy_id,
            entry.backup_id,
            entry.committed_at,
            entry.object_set_digest,
            entry.cipher_bytes as i64,
            entry.logical_bytes as i64,
            entry.committed_at,
            OBJECT_SET_V1,
            metadata_json,
        ],
    )
    .map_err(|_| route_support::unhandled_error())?;

    let logical_id = format!("{}:{}", entry.policy_id, entry.backup_id);
    conn.execute(
        "INSERT INTO logical_recovery_points
         (logical_id, policy_id, backup_id, object_set_digest, committed_at, snapshot_kind, retained, retired_at, metadata_json)
         VALUES (?1, ?2, ?3, ?4, ?5, 'full', 1, NULL, ?6)
         ON CONFLICT(logical_id) DO UPDATE SET
            object_set_digest = excluded.object_set_digest,
            committed_at = excluded.committed_at,
            retained = 1,
            retired_at = NULL",
        params![
            logical_id,
            entry.policy_id,
            entry.backup_id,
            entry.object_set_digest,
            entry.committed_at,
            metadata_json,
        ],
    )
    .map_err(|_| route_support::unhandled_error())?;

    conn.execute(
        "INSERT INTO recovery_point_copies
         (target_id, policy_id, backup_id, logical_id, object_set_digest, committed_at, recoverable, role, mode, state, last_verified_at, verified_at, metadata_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, 'primary', 'required', 'healthy', ?6, ?6, ?7)
         ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            object_set_digest = excluded.object_set_digest,
            committed_at = excluded.committed_at,
            recoverable = 1,
            state = 'healthy',
            last_verified_at = excluded.last_verified_at,
            verified_at = excluded.verified_at,
            metadata_json = excluded.metadata_json",
        params![
            entry.target_id,
            entry.policy_id,
            entry.backup_id,
            logical_id,
            entry.object_set_digest,
            entry.committed_at,
            metadata_json,
        ],
    )
    .map_err(|_| route_support::unhandled_error())?;

    Ok(())
}

fn record_snapshot_index(
    root: &Path,
    target_id: &str,
    policy_id: &str,
    backup_id: &str,
    created_at: &str,
    root_digest: &str,
) -> Result<(), AppError> {
    let dir = root.join(".backup-index");
    fs::create_dir_all(&dir).map_err(|_| route_support::unhandled_error())?;
    let db = dir.join("index.sqlite3");
    let conn = Connection::open(&db).map_err(|_| route_support::unhandled_error())?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .map_err(|_| route_support::unhandled_error())?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         CREATE TABLE IF NOT EXISTS committed_snapshots (
            target_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            root_digest TEXT NOT NULL,
            snapshot_kind TEXT NOT NULL DEFAULT 'full',
            committed_at TEXT NOT NULL,
            PRIMARY KEY (target_id, policy_id, backup_id)
         );",
    )
    .map_err(|_| route_support::unhandled_error())?;
    conn.execute(
        "INSERT INTO committed_snapshots (target_id, policy_id, backup_id, root_digest, snapshot_kind, committed_at)
         VALUES (?1, ?2, ?3, ?4, 'full', ?5)
         ON CONFLICT(target_id, policy_id, backup_id) DO UPDATE SET
            root_digest = excluded.root_digest,
            committed_at = excluded.committed_at",
        params![target_id, policy_id, backup_id, root_digest, created_at],
    )
    .map_err(|_| route_support::unhandled_error())?;
    Ok(())
}

fn build_backup_id() -> Result<String, AppError> {
    let hex16 = SystemEntropy.new_id()?;
    let short: String = hex16.chars().take(12).collect();
    Ok(format!("bk_pol_{short}"))
}

struct LeaseRunArgs<'a> {
    root: &'a Path,
    policy: &'a Value,
    claim: &'a ClaimedManualRun,
    context: &'a BackupContext,
    recipients: &'a [String],
    target_id: &'a str,
    target_root: &'a Path,
    sealed_frontend: Option<&'a Value>,
    lease: &'a TargetWriterLease,
    backup_id: &'a str,
}

fn run_with_target_lease(args: &LeaseRunArgs<'_>) -> Result<Value, AppError> {
    let LeaseRunArgs {
        root,
        policy,
        claim,
        context,
        recipients,
        target_id,
        target_root,
        sealed_frontend,
        lease,
        backup_id,
    } = *args;
    let head_hash = previous_commit_hash(target_root);
    write_frozen_run_plan(
        root, policy, claim, target_id, &head_hash, backup_id, context,
    )?;

    update_run_phase(
        root,
        &claim.run_id,
        RunPhaseUpdate {
            phase: "snapshotting",
            reason: None,
            error: None,
            lease_until: None,
            backup_id: Some(backup_id),
            filename: None,
        },
    );

    let mut built_opt: Option<BuiltObjectSet> = None;
    let mut last_gen_err: Option<AppError> = None;
    for attempt in 1..=SNAPSHOT_MAX_RETRIES {
        let stage_dir = root
            .join(".backup-spool")
            .join("manual")
            .join(format!("{}-{attempt}", claim.run_id));
        fs::create_dir_all(&stage_dir).map_err(|_| route_support::unhandled_error())?;
        let _cleanup = StagingCleanup {
            dir: stage_dir.clone(),
        };

        let start_gen = mutation_gate::read_generation(root);
        let built = build_object_set_once(
            root,
            &stage_dir,
            backup_id,
            context,
            recipients,
            sealed_frontend,
        )?;
        let end_gen = mutation_gate::read_generation(root);
        if start_gen == end_gen {
            lease.assert_owned()?;
            install_object_blob(target_root, &built.control)?;
            for blob in &built.payloads {
                install_object_blob(target_root, blob)?;
            }
            built_opt = Some(built);
            break;
        }
        last_gen_err = Some(AppError {
            message: "Workspace data changed repeatedly during backup; try again".to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        });
    }

    let Some(built) = built_opt else {
        return Err(last_gen_err.unwrap_or_else(route_support::unhandled_error));
    };

    let filename = format!("{backup_id}.object-set");
    let created_at = now_utc_iso();

    let mut inv = Vec::with_capacity(1 + built.payloads.len());
    inv.push(ObjectInventoryEntry {
        digest: built.control.digest.clone(),
        size: built.control.size,
    });
    for blob in &built.payloads {
        inv.push(ObjectInventoryEntry {
            digest: blob.digest.clone(),
            size: blob.size,
        });
    }
    let osd = object_inventory_digest(&inv).map_err(|_| AppError {
        message: "Failed to compute object-set digest".to_string(),
        code: codes::INTERNAL,
        status: 500,
    })?;
    let total_cipher_bytes: u64 = inv.iter().map(|entry| entry.size).sum();
    let mut objects_vec: Vec<Value> = inv
        .into_iter()
        .map(|entry| json!({"digest": entry.digest, "size": entry.size}))
        .collect();
    objects_vec.sort_by(|a, b| a["digest"].as_str().cmp(&b["digest"].as_str()));

    let receipt = json!({
        "schemaVersion": 4,
        "backupId": backup_id,
        "filename": filename,
        "policyId": claim.policy_id,
        "targetId": target_id,
        "createdAt": created_at,
        "snapshotKind": "full",
        "storageProtocol": OBJECT_SET_V1,
        "controlObjectDigest": built.control.digest,
        "objectSetDigest": osd,
        "objects": objects_vec,
        "size": total_cipher_bytes,
        "logicalBytes": built.logical_bytes,
        "pinned": false,
        "creationVerified": true,
    });
    let receipt_bytes =
        serde_json::to_vec_pretty(&receipt).map_err(|_| route_support::unhandled_error())?;
    let receipt_digest = sha256_bytes(&receipt_bytes);
    let receipt_path = target_root
        .join("receipts")
        .join(format!("{backup_id}.json"));
    lease.assert_owned()?;
    write_atomic_bytes(&receipt_path, &receipt_bytes)?;

    let prev_commit = previous_commit_hash(target_root);
    let target_gen = target_generation(target_root).saturating_add(1);
    let mut cmap: BTreeMap<String, Value> = BTreeMap::new();
    cmap.insert("schemaVersion".into(), json!(4));
    cmap.insert("storageProtocol".into(), json!(OBJECT_SET_V1));
    cmap.insert("backupId".into(), json!(backup_id));
    cmap.insert("policyId".into(), json!(claim.policy_id));
    cmap.insert("targetId".into(), json!(target_id));
    cmap.insert("objectSetDigest".into(), json!(osd));
    cmap.insert("controlObjectDigest".into(), json!(built.control.digest));
    cmap.insert("receiptDigest".into(), json!(receipt_digest));
    cmap.insert("previousCommitHash".into(), json!(prev_commit));
    cmap.insert("targetGeneration".into(), json!(target_gen));
    let mut commit_obj: Map<String, Value> = cmap.into_iter().collect();
    // `backup_publish._commit_hash` is `sha256(_stable_json(body)).hexdigest()`.
    // `hex_digest` only hex-encodes its input, so it produced a hex dump of the
    // JSON instead of a digest; the validator recomputed a real sha256 and every
    // marker was rejected as uncommitted.
    let commit_hash = sha256_bytes(dumps_compact(&Value::Object(commit_obj.clone())).as_bytes());
    commit_obj.insert("commitHash".into(), Value::String(commit_hash.clone()));

    let commit_path = target_root
        .join("commits")
        .join(&claim.policy_id)
        .join(format!("{backup_id}.json"));
    let commit_bytes = serde_json::to_vec_pretty(&Value::Object(commit_obj))
        .map_err(|_| route_support::unhandled_error())?;
    lease.assert_owned()?;
    write_atomic_bytes(&commit_path, &commit_bytes)?;

    update_run_phase(
        root,
        &claim.run_id,
        RunPhaseUpdate {
            phase: "cataloging",
            reason: None,
            error: None,
            lease_until: None,
            backup_id: Some(backup_id),
            filename: Some(&filename),
        },
    );
    append_catalog_entry(target_root, "receipt", &receipt, lease, &created_at)?;
    record_dr_ledger_recovery_point(&DrRecoveryPointRecord {
        target_id,
        policy_id: &claim.policy_id,
        backup_id,
        committed_at: &created_at,
        object_set_digest: &osd,
        cipher_bytes: total_cipher_bytes,
        logical_bytes: built.logical_bytes,
        manifest: &built.manifest,
    })?;
    record_snapshot_index(
        root,
        target_id,
        &claim.policy_id,
        backup_id,
        &created_at,
        &built.root_digest,
    )?;

    update_run_phase(
        root,
        &claim.run_id,
        RunPhaseUpdate {
            phase: "pruning",
            reason: None,
            error: None,
            lease_until: None,
            backup_id: Some(backup_id),
            filename: Some(&filename),
        },
    );
    let retention_outcome = apply_retention_for_policy(policy, lease)?;

    update_run_phase(
        root,
        &claim.run_id,
        RunPhaseUpdate {
            phase: "complete",
            reason: Some("manual"),
            error: None,
            lease_until: None,
            backup_id: Some(backup_id),
            filename: Some(&filename),
        },
    );

    Ok(json!({
        "runId": claim.run_id,
        "policyId": claim.policy_id,
        "scheduleSlot": claim.schedule_slot,
        "attempt": 1,
        "fencingToken": claim.fencing_token,
        "phase": "complete",
        "backupId": backup_id,
        "filename": filename,
        "targetId": target_id,
        "storageProtocol": OBJECT_SET_V1,
        "snapshotKind": "full",
        "objectSetDigest": osd,
        "controlObjectDigest": built.control.digest,
        "receiptDigest": receipt_digest,
        "commitHash": commit_hash,
        "targetGeneration": target_gen,
        "size": total_cipher_bytes,
        "logicalBytes": built.logical_bytes,
        "createdAt": created_at,
        "isFailover": false,
        "failoverTransitioned": false,
        "retention": retention_outcome,
    }))
}

pub(crate) fn execute_manual_policy_run(root: &Path, policy_id: &str) -> Result<Value, AppError> {
    require_run_write_authority()?;
    if !is_valid_safe_id(policy_id) {
        return Err(AppError {
            message: "Backup policy not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    }
    let policy = load_backup_policy(policy_id)?;
    mutation_gate::assert_mutation_allowed(None, root).map_err(gate_to_app_error)?;
    let recipients = extract_policy_recipients(&policy)?;
    let scope_map = policy
        .get("scope")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let context = parse_context(&scope_map)?;
    let claim = claim_manual_run(root, &policy, policy_id)?;

    let (target_id, target_root) = match resolve_policy_target(root, &policy)? {
        ResolvedPolicyTarget::Ready {
            target_id,
            target_root,
        } => (target_id, target_root),
        ResolvedPolicyTarget::Blocked {
            target_id,
            reason,
            message,
        } => {
            return Ok(mark_blocked_outcome(
                root, &claim, &policy, &target_id, &reason, &message,
            ));
        }
    };

    let sealed_frontend = match check_frontend_mirror_head(root, &policy) {
        Ok(val) => val,
        Err(msg) => {
            return Ok(mark_blocked_outcome(
                root,
                &claim,
                &policy,
                &target_id,
                "frontend-mirror-missing",
                &msg,
            ));
        }
    };

    fs::create_dir_all(&target_root).map_err(|_| route_support::unhandled_error())?;
    let mut lease = TargetWriterLease::new(
        &target_root,
        &target_id,
        claim.run_id.clone(),
        claim.instance_id.clone(),
        claim.fencing_token,
    );
    if let Err(err) = lease.acquire() {
        update_run_phase(
            root,
            &claim.run_id,
            RunPhaseUpdate {
                phase: "failed",
                reason: Some("target-writer-lease-held"),
                error: Some(&err.message),
                lease_until: None,
                backup_id: None,
                filename: None,
            },
        );
        return Err(err);
    }

    let backup_id = build_backup_id()?;
    let res = run_with_target_lease(&LeaseRunArgs {
        root,
        policy: &policy,
        claim: &claim,
        context: &context,
        recipients: &recipients,
        target_id: &target_id,
        target_root: &target_root,
        sealed_frontend: sealed_frontend.as_ref(),
        lease: &lease,
        backup_id: &backup_id,
    });
    lease.release();
    if let Err(ref err) = res {
        update_run_phase(
            root,
            &claim.run_id,
            RunPhaseUpdate {
                phase: "failed",
                reason: Some("manual-run-failed"),
                error: Some(&err.message),
                lease_until: None,
                backup_id: Some(&backup_id),
                filename: None,
            },
        );
    }
    res
}
