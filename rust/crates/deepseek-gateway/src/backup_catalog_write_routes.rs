//! Native workspace backup catalog mutations and integrity routes.
//!
//! Covers:
//! - `POST|DELETE /api/workspace/backup-catalog/:backup_id/pin`
//! - `POST /api/workspace/backups/:backup_id/scrub`
//! - `POST /api/workspace/backups/:backup_id/verify-unlock`
//!
//! Write authority follows `DECLARED_NATIVE_DATA_DOMAINS`: mutations execute
//! only when `DEEPSEEK_RUNTIME_MODE=python_disabled` (`may_write_native_store`
//! for `backup_catalog_store` and, for scrub evidence, `backup_dr_ledger_store`).
//! Every catalog mutation acquires the filesystem `TargetWriterLease`, asserts
//! lease ownership before writing `events/` and before appending to
//! `catalog-v1.jsonl`, and stamps `targetGeneration` from `crate::backup_commit`.

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::Path as AxumPath;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::{dumps_indent_two, fsync_dir, now_iso, sha256_file};
use deepseek_policy::backup_unlock::{
    ObjectMemberRef, inspect_age_file_header, is_plain_sha256, verify_unlock_legacy,
    verify_unlock_object_set,
};
use deepseek_policy::backup_writer_lease::{TargetWriterLease, active_writer_lease};
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::python_json::{dumps_default_separators, value_str};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value, json};
use zeroize::Zeroizing;

use crate::backup_catalog_routes::{
    backup_file_candidates, catalog_path, catalog_state, committed_object_inventory, entry_hash,
    is_object_set, list_registered_targets, object_path, open_readonly_sqlite, read_entries,
    unhandled,
};
use crate::route_support;

const BACKUP_CATALOG_DOMAIN: &str = "backup_catalog_store";
const BACKUP_CATALOG_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED";
const BACKUP_DR_LEDGER_DOMAIN: &str = "backup_dr_ledger_store";
const BACKUP_DR_LEDGER_WRITE_NOT_OWNED: &str = "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED";
const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";
const CATALOG_SCHEMA_VERSION: i64 = 1;
const TARGET_MARKER_NAME: &str = ".deepseek-infra-backup-target.json";
const TARGET_STORE_NOT_MIGRATED_CODE: &str = "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED";

static NEXT_ADHOC_FENCING_TOKEN: AtomicI64 = AtomicI64::new(0);

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backup-catalog/:backup_id/pin",
            any(pin_dispatch),
        )
        .route(
            "/api/workspace/backups/:backup_id/scrub",
            any(scrub_dispatch),
        )
        .route(
            "/api/workspace/backups/:backup_id/verify-unlock",
            any(verify_unlock_dispatch),
        )
}

fn unallowed_method_response(method: Method) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    // Python's `@api.api_route("/api/{path:path}", methods=["GET", "POST"])`
    // turns unmatched `GET /api/...` requests into JSON 404 instead of 405.
    if method == Method::GET {
        return route_support::route_not_found();
    }
    route_support::method_not_allowed()
}

fn store_not_owned(domain: &str, code: &'static str) -> AppError {
    AppError {
        message: format!(
            "The {domain} is still written by the Python runtime, so this gateway refuses to mutate it."
        ),
        code,
        status: 409,
    }
}

fn require_native_store(domain: &str, code: &'static str) -> Result<(), AppError> {
    if crate::may_write_native_store(domain) {
        Ok(())
    } else {
        Err(store_not_owned(domain, code))
    }
}

struct ResolvedBackup {
    root: PathBuf,
    target_id: String,
    target_meta: Option<Value>,
    record: Value,
}

fn find_backup_target(backup_id: &str) -> Result<ResolvedBackup, AppError> {
    let managed_root = crate::data_routes::workspace_root().join(".backups");
    let managed_state = catalog_state(&managed_root)?;
    if let Some(record) = managed_state.get(backup_id) {
        return Ok(ResolvedBackup {
            root: managed_root,
            target_id: "managed-local".to_string(),
            target_meta: None,
            record: record.clone(),
        });
    }

    let targets = list_registered_targets()?;
    let mut has_remote_target = false;
    for target in targets {
        let target_id = target
            .get("targetId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if target_id.is_empty() {
            continue;
        }
        let kind = target.get("kind").and_then(Value::as_str).unwrap_or("");
        let path_str = target.get("path").and_then(Value::as_str).unwrap_or("");
        if kind != "filesystem" || path_str.is_empty() {
            has_remote_target = true;
            continue;
        }
        let root = PathBuf::from(path_str);
        let state = catalog_state(&root)?;
        if let Some(record) = state.get(backup_id) {
            return Ok(ResolvedBackup {
                root,
                target_id,
                target_meta: Some(target),
                record: record.clone(),
            });
        }
    }

    if has_remote_target {
        return Err(AppError {
            message:
                "backup-catalog for a remote registered target is not served by the native runtime yet"
                    .to_string(),
            code: TARGET_STORE_NOT_MIGRATED_CODE,
            status: 503,
        });
    }

    Err(AppError {
        message: "Backup not found in any catalog".to_string(),
        code: codes::NOT_FOUND,
        status: 404,
    })
}

fn read_scheduler_fencing_floor() -> i64 {
    let db = crate::data_routes::workspace_root()
        .join(".backup-scheduler")
        .join("scheduler.db");
    let Ok(Some(connection)) = open_readonly_sqlite(&db) else {
        return 0;
    };
    let table: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'backup_counters'",
            [],
            |row| row.get(0),
        )
        .optional()
        .ok()
        .flatten();
    if table.is_none() {
        return 0;
    }
    connection
        .query_row(
            "SELECT value FROM backup_counters WHERE name = 'fencing'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .ok()
        .flatten()
        .unwrap_or(0)
}

fn read_expired_lease_token(root: &Path) -> i64 {
    if active_writer_lease(root, None) {
        return 0;
    }
    let path = root.join(".target-lock").join("writer.json");
    let Ok(raw) = fs::read_to_string(&path) else {
        return 0;
    };
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) else {
        return 0;
    };
    map.get("fencingToken")
        .and_then(|v| v.as_i64())
        .filter(|v| *v > 0)
        .unwrap_or(0)
}

fn allocate_adhoc_fencing_token(root: &Path) -> i64 {
    let floor = read_scheduler_fencing_floor().max(read_expired_lease_token(root));
    loop {
        let current = NEXT_ADHOC_FENCING_TOKEN.load(Ordering::SeqCst);
        let next = current.max(floor).saturating_add(1);
        if NEXT_ADHOC_FENCING_TOKEN
            .compare_exchange(current, next, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            return next;
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

fn acquire_target_writer(root: &Path, target_id: &str) -> Result<TargetWriterLease, AppError> {
    let hex16 = SystemEntropy.new_id()?;
    let run_id = format!("adhoc-{}", hex16.chars().take(12).collect::<String>());
    let instance_id = instance_id_from_environment();
    let fencing_token = allocate_adhoc_fencing_token(root);
    let mut lease = TargetWriterLease::new(root, target_id, run_id, instance_id, fencing_token);
    lease.acquire()?;
    Ok(lease)
}

fn write_event_file(root: &Path, entry: &Value) -> Result<(), AppError> {
    let digest = entry
        .get("entryHash")
        .and_then(Value::as_str)
        .ok_or_else(unhandled)?;
    let prefix: String = digest.chars().take(2).collect();
    let path = root
        .join("events")
        .join(prefix)
        .join(format!("{digest}.json"));
    let parent = path.parent().ok_or_else(unhandled)?;
    fs::create_dir_all(parent).map_err(|_| unhandled())?;

    let content = format!("{}\n", dumps_indent_two(entry));
    match OpenOptions::new().write(true).create_new(true).open(&path) {
        Ok(mut handle) => {
            if handle.write_all(content.as_bytes()).is_err()
                || handle.flush().is_err()
                || handle.sync_all().is_err()
            {
                drop(handle);
                let _ = fs::remove_file(&path);
                return Err(unhandled());
            }
            fsync_dir(parent);
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(_) => Err(unhandled()),
    }
}

fn append_catalog_entry(
    root: &Path,
    entry_type: &str,
    payload: &Value,
    writer: &TargetWriterLease,
    recorded_at: &str,
) -> Result<Value, AppError> {
    writer.assert_owned()?;
    let path = catalog_path(root);
    let catalog_dir = path.parent().ok_or_else(unhandled)?;
    fs::create_dir_all(catalog_dir).map_err(|_| unhandled())?;

    let entries = read_entries(root)?;
    let head = entries
        .last()
        .and_then(|entry| entry.get("entryHash"))
        .and_then(Value::as_str)
        .unwrap_or(GENESIS_HASH);
    let generation = crate::backup_commit::target_generation(root);
    let hash = entry_hash(entry_type, payload, head);
    let entry = json!({
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "type": entry_type,
        "payload": payload,
        "previousEntryHash": head,
        "entryHash": hash,
        "recordedAt": recorded_at,
        "targetGeneration": generation,
        "writerFencingToken": writer.fencing_token(),
    });

    // Re-check lease ownership immediately before each durable write so a
    // preempted writer cannot publish an event or JSONL line after losing lock.
    writer.assert_owned()?;
    write_event_file(root, &entry)?;
    writer.assert_owned()?;

    let line = format!("{}\n", dumps_default_separators(&entry));
    let mut handle = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|_| unhandled())?;
    handle.write_all(line.as_bytes()).map_err(|_| unhandled())?;
    handle.flush().map_err(|_| unhandled())?;
    handle.sync_all().map_err(|_| unhandled())?;
    fsync_dir(catalog_dir);
    Ok(entry)
}

#[cfg(windows)]
fn has_reparse_attribute(meta: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn has_reparse_attribute(_meta: &fs::Metadata) -> bool {
    false
}

fn is_reparse_or_symlink(path: &Path) -> bool {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return false;
    };
    meta.file_type().is_symlink() || has_reparse_attribute(&meta)
}

fn ciphertext_path(root: &Path, record: &Value) -> PathBuf {
    let candidates = backup_file_candidates(root, record);
    for candidate in &candidates {
        if candidate.is_file() {
            return candidate.clone();
        }
    }
    if let Some(first) = candidates.into_iter().next() {
        return first;
    }
    let filename = record.get("filename").map(value_str).unwrap_or_default();
    root.join("backups").join(filename)
}

fn ciphertext_members(
    root: &Path,
    record: &Value,
) -> Result<Vec<(PathBuf, String, u64)>, AppError> {
    if is_object_set(record) {
        let inventory = committed_object_inventory(record).map_err(|()| AppError {
            message: "Object-set receipt inventory is invalid".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        })?;
        return Ok(inventory
            .into_iter()
            .map(|item| (object_path(root, &item.digest), item.digest, item.size))
            .collect());
    }
    let object_digest = record
        .get("objectDigest")
        .and_then(Value::as_str)
        .unwrap_or("");
    let cipher_sha = record
        .get("ciphertextSha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    let digest = if !object_digest.is_empty() {
        object_digest
    } else {
        cipher_sha
    };
    if !is_plain_sha256(digest) {
        return Ok(Vec::new());
    }
    let size = record.get("size").and_then(Value::as_u64).unwrap_or(0);
    Ok(vec![(
        ciphertext_path(root, record),
        digest.to_string(),
        size,
    )])
}

fn verify_registered_target_ready_readonly(
    target_id: &str,
    root: &Path,
    target_meta: &Value,
) -> Result<(), String> {
    if !root.is_dir() || is_reparse_or_symlink(root) {
        return Err("blocked-target-unavailable: target directory is invalid".to_string());
    }
    let marker = root.join(TARGET_MARKER_NAME);
    if !marker.is_file() {
        return Err("blocked-target-unavailable: target marker is missing".to_string());
    }
    let raw = fs::read_to_string(&marker)
        .map_err(|_| "blocked-target-unavailable: target marker is unreadable".to_string())?;
    let parsed: Value = serde_json::from_str(&raw)
        .map_err(|_| "blocked-target-unavailable: target marker is unreadable".to_string())?;
    let Some(map) = parsed.as_object() else {
        return Err("blocked-target-unavailable: target marker was replaced".to_string());
    };
    let expected_nonce = target_meta
        .get("targetNonce")
        .and_then(Value::as_str)
        .unwrap_or("");
    let marker_id = map.get("targetId").and_then(Value::as_str).unwrap_or("");
    let marker_nonce = map.get("targetNonce").and_then(Value::as_str).unwrap_or("");
    if marker_id != target_id || marker_nonce != expected_nonce {
        return Err("blocked-target-unavailable: target marker was replaced".to_string());
    }
    Ok(())
}

fn join_first_three(names: &[String]) -> String {
    names.iter().take(3).cloned().collect::<Vec<_>>().join(", ")
}

fn file_display_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default()
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn record_scrub_evidence_sqlite(
    target_id: &str,
    backup_id: &str,
    policy_id: &str,
    observed_at: &str,
    ok: bool,
    details: &Value,
) {
    let dir = crate::data_routes::workspace_root().join(".backup-dr");
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let db_path = dir.join("evidence.sqlite3");
    let Ok(connection) = Connection::open(&db_path) else {
        return;
    };
    let _ = connection.busy_timeout(Duration::from_secs(30));
    if connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS scrub_evidence (
                target_id TEXT NOT NULL,
                backup_id TEXT NOT NULL,
                policy_id TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                result TEXT NOT NULL,
                details_json TEXT,
                PRIMARY KEY (target_id, backup_id, policy_id)
            );",
        )
        .is_err()
    {
        return;
    }

    // Legacy databases created before composite PKs may lack `policy_id`.
    let mut has_policy_id = false;
    if let Ok(mut stmt) = connection.prepare("PRAGMA table_info(scrub_evidence)") {
        if let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(1)) {
            let cols: HashSet<String> = rows.filter_map(Result::ok).collect();
            has_policy_id = cols.contains("policy_id");
        }
    }
    if !has_policy_id {
        let _ = connection.execute(
            "ALTER TABLE scrub_evidence ADD COLUMN policy_id TEXT NOT NULL DEFAULT ''",
            [],
        );
    }

    let result_label = if ok { "success" } else { "failed" };
    let details_json = dumps_default_separators(details);
    let primary_insert = connection.execute(
        "INSERT INTO scrub_evidence (target_id, backup_id, policy_id, observed_at, result, details_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(target_id, backup_id, policy_id) DO UPDATE SET
             observed_at = excluded.observed_at,
             result = excluded.result,
             details_json = excluded.details_json",
        params![target_id, backup_id, policy_id, observed_at, result_label, details_json],
    );
    if primary_insert.is_err() {
        let _ = connection.execute(
            "INSERT INTO scrub_evidence (target_id, backup_id, policy_id, observed_at, result, details_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(target_id, backup_id) DO UPDATE SET
                 observed_at = excluded.observed_at,
                 result = excluded.result,
                 details_json = excluded.details_json",
            params![target_id, backup_id, policy_id, observed_at, result_label, details_json],
        );
    }
}

fn execute_pin(backup_id: &str, pinned: bool) -> Result<Value, AppError> {
    let found = find_backup_target(backup_id)?;
    require_native_store(BACKUP_CATALOG_DOMAIN, BACKUP_CATALOG_WRITE_NOT_OWNED)?;
    let mut lease = acquire_target_writer(&found.root, &found.target_id)?;
    let payload = json!({
        "backupId": backup_id,
        "pinned": pinned,
    });
    let recorded_at = now_iso(None);
    append_catalog_entry(&found.root, "pin", &payload, &lease, &recorded_at)?;
    lease.release();
    Ok(json!({
        "backupId": backup_id,
        "pinned": pinned,
    }))
}

fn record_scrub_check(
    checks: &mut Map<String, Value>,
    ok: &mut bool,
    name: &str,
    passed: bool,
    detail: &str,
) {
    *ok = *ok && passed;
    checks.insert(
        name.to_string(),
        Value::String(if passed {
            "PASS".to_string()
        } else {
            format!("FAIL: {detail}")
        }),
    );
}

fn execute_scrub(backup_id: &str) -> Result<Value, AppError> {
    let found = find_backup_target(backup_id)?;
    require_native_store(BACKUP_CATALOG_DOMAIN, BACKUP_CATALOG_WRITE_NOT_OWNED)?;
    require_native_store(BACKUP_DR_LEDGER_DOMAIN, BACKUP_DR_LEDGER_WRITE_NOT_OWNED)?;

    let mut lease = acquire_target_writer(&found.root, &found.target_id)?;
    let mut checks = Map::<String, Value>::new();
    let mut ok = true;

    let (members, exists_checked) = match ciphertext_members(&found.root, &found.record) {
        Ok(list) => (list, false),
        Err(err) => {
            record_scrub_check(
                &mut checks,
                &mut ok,
                "exists",
                false,
                &truncate_chars(&err.message, 80),
            );
            (Vec::new(), true)
        }
    };
    if !exists_checked {
        let missing: Vec<String> = members
            .iter()
            .filter(|(path, _, _)| !path.is_file())
            .map(|(path, _, _)| file_display_name(path))
            .collect();
        let detail = format!("missing {}", join_first_three(&missing));
        record_scrub_check(
            &mut checks,
            &mut ok,
            "exists",
            !members.is_empty() && missing.is_empty(),
            &detail,
        );
    }

    if !members.is_empty() && members.iter().all(|(path, _, _)| path.is_file()) {
        let reparsed: Vec<String> = members
            .iter()
            .filter(|(path, _, _)| is_reparse_or_symlink(path))
            .map(|(path, _, _)| file_display_name(path))
            .collect();
        record_scrub_check(
            &mut checks,
            &mut ok,
            "not-symlink",
            reparsed.is_empty(),
            &format!("reparse point {}", join_first_three(&reparsed)),
        );

        let wrong_sizes: Vec<String> = members
            .iter()
            .filter(|(path, _, expected)| {
                fs::metadata(path).map(|meta| meta.len()).ok() != Some(*expected)
            })
            .map(|(path, _, _)| file_display_name(path))
            .collect();
        record_scrub_check(
            &mut checks,
            &mut ok,
            "size",
            wrong_sizes.is_empty(),
            &format!("size mismatch {}", join_first_three(&wrong_sizes)),
        );

        let wrong_digests: Vec<String> = members
            .iter()
            .filter(|(path, expected, _)| {
                sha256_file(path).ok().as_deref() != Some(expected.as_str())
            })
            .map(|(path, _, _)| file_display_name(path))
            .collect();
        record_scrub_check(
            &mut checks,
            &mut ok,
            "sha256",
            wrong_digests.is_empty(),
            &format!("digest mismatch {}", join_first_three(&wrong_digests)),
        );

        let mut header_failure: Option<String> = None;
        for (path, _, _) in &members {
            match inspect_age_file_header(path) {
                Ok(true) => {}
                Ok(false) => {
                    header_failure = Some(format!("invalid header {}", file_display_name(path)));
                    break;
                }
                Err(msg) => {
                    header_failure = Some(truncate_chars(&msg, 80));
                    break;
                }
            }
        }
        record_scrub_check(
            &mut checks,
            &mut ok,
            "age-header",
            header_failure.is_none(),
            header_failure.as_deref().unwrap_or(""),
        );
    }

    if found.target_id != "managed-local" {
        if let Some(target_meta) = &found.target_meta {
            match verify_registered_target_ready_readonly(
                &found.target_id,
                &found.root,
                target_meta,
            ) {
                Ok(()) => record_scrub_check(&mut checks, &mut ok, "target-marker", true, ""),
                Err(msg) => record_scrub_check(
                    &mut checks,
                    &mut ok,
                    "target-marker",
                    false,
                    &truncate_chars(&msg, 80),
                ),
            }
        }
    }

    let detail = checks
        .iter()
        .filter_map(|(k, v)| {
            let status = v.as_str()?;
            (status != "PASS").then(|| format!("{k}={status}"))
        })
        .collect::<Vec<_>>()
        .join("; ");
    let scrubbed_at = now_iso(None);
    let scrub_payload = json!({
        "backupId": backup_id,
        "ok": ok,
        "detail": truncate_chars(&detail, 200),
        "scrubbedAt": scrubbed_at,
    });
    append_catalog_entry(&found.root, "scrub", &scrub_payload, &lease, &scrubbed_at)?;

    let policy_id = found
        .record
        .get("policyId")
        .and_then(Value::as_str)
        .unwrap_or("");
    let checks_value = Value::Object(checks.clone());
    let evidence_details = json!({
        "checks": checks_value,
        "objectsScrubbed": members.len(),
    });
    record_scrub_evidence_sqlite(
        &found.target_id,
        backup_id,
        policy_id,
        &scrubbed_at,
        ok,
        &evidence_details,
    );

    lease.release();
    Ok(json!({
        "backupId": backup_id,
        "ok": ok,
        "checks": checks_value,
        "objectsScrubbed": members.len(),
        "scrubbedAt": scrubbed_at,
    }))
}

fn execute_verify_unlock(backup_id: &str, identity: Zeroizing<String>) -> Result<Value, AppError> {
    let found = find_backup_target(backup_id)?;
    require_native_store(BACKUP_CATALOG_DOMAIN, BACKUP_CATALOG_WRITE_NOT_OWNED)?;
    let mut lease = acquire_target_writer(&found.root, &found.target_id)?;

    let staged_root = crate::data_routes::workspace_root()
        .join(".restore-staging")
        .join("drills");
    let outcome = if is_object_set(&found.record) {
        let inventory = committed_object_inventory(&found.record).map_err(|()| AppError {
            message: "Object-set receipt inventory is invalid".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        })?;
        let members: Vec<ObjectMemberRef> = inventory
            .into_iter()
            .map(|item| ObjectMemberRef {
                ciphertext_path: object_path(&found.root, &item.digest),
                digest: item.digest,
                size: item.size,
            })
            .collect();
        let control_digest = found
            .record
            .get("controlObjectDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        verify_unlock_object_set(&staged_root, backup_id, control_digest, &members, &identity)?
    } else {
        let source = ciphertext_path(&found.root, &found.record);
        let expected_cipher = found
            .record
            .get("ciphertextSha256")
            .and_then(Value::as_str)
            .unwrap_or("");
        let expected_manifest = found
            .record
            .get("manifestDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        verify_unlock_legacy(
            &staged_root,
            backup_id,
            &source,
            expected_cipher,
            expected_manifest,
            &identity,
        )?
    };
    drop(identity);

    let recorded_at = outcome.verified_at.clone();
    let payload = json!({
        "backupId": backup_id,
        "userUnlockVerifiedAt": recorded_at,
    });
    append_catalog_entry(
        &found.root,
        "unlock-verified",
        &payload,
        &lease,
        &recorded_at,
    )?;
    lease.release();

    Ok(json!({
        "backupId": outcome.backup_id,
        "ok": true,
        "manifestDigest": outcome.manifest_digest,
        "contributors": outcome.contributors,
        "sealedFrontend": outcome.sealed_frontend,
        "userUnlockVerifiedAt": outcome.verified_at,
    }))
}

async fn run_blocking(work: impl FnOnce() -> Result<Value, AppError> + Send + 'static) -> Response {
    let result = match tokio::task::spawn_blocking(work).await {
        Ok(res) => res,
        Err(_) => Err(unhandled()),
    };
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => route_support::app_error_response(error),
    }
}

async fn pin_dispatch(method: Method, AxumPath(backup_id): AxumPath<String>) -> Response {
    let pinned = match method {
        Method::POST => true,
        Method::DELETE => false,
        other => return unallowed_method_response(other),
    };
    run_blocking(move || execute_pin(&backup_id, pinned)).await
}

async fn scrub_dispatch(method: Method, AxumPath(backup_id): AxumPath<String>) -> Response {
    if method != Method::POST {
        return unallowed_method_response(method);
    }
    run_blocking(move || execute_scrub(&backup_id)).await
}

async fn verify_unlock_dispatch(
    method: Method,
    AxumPath(backup_id): AxumPath<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return unallowed_method_response(method);
    }
    let payload = match route_support::read_json_object(&headers, &body, 128_000) {
        Ok(map) => map,
        Err(error) => return route_support::app_error_response(error),
    };
    let identity_text = payload
        .get("identity")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !identity_text.starts_with("AGE-SECRET-KEY-") {
        return route_support::app_error_response(AppError {
            message: "A valid Recovery Identity is required".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let identity = Zeroizing::new(identity_text.to_string());
    run_blocking(move || execute_verify_unlock(&backup_id, identity)).await
}
